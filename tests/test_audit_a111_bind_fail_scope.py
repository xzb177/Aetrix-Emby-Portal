"""A-111 回归：绑定码猜错计数不得跨用户作废他人待验证绑定码。

旧行为（缺陷）：每次猜错把全部待验证绑定码 fail_count+1，累计 20 次即把
所有人的码一并作废——4 个 TG 账号各猜错 5 次（或单号 40 分钟）即可
DoS 全站待绑码。
新行为：
- 猜错只允许影响猜错者本人：全站失败仅作速率信号，越线后只锁定触发的
  那个 TG 账号一个窗口期，绝不触碰他人的绑定码行；
- 可归属到某个已存在码行的猜错（提交的字符串命中一个当前不可绑定的码行）
  只给该行 fail_count+1，达上限只作废该行；
- 受害者的待验证绑定码 fail_count 恒为 0、可正常绑定。
"""
from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta

os.environ.setdefault("DATABASE_TYPE", "sqlite")
os.environ.setdefault("REDIS_ENABLED", "false")
sys.path.insert(0, ".")

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend import models
from backend.integrations import store
from backend.tg_bot import handlers


@pytest.fixture()
def db():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool,
    )
    models.Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    store.invalidate()
    _reset_failure_state()
    yield session
    session.close()
    store.invalidate()
    _reset_failure_state()


def _reset_failure_state():
    reset = getattr(handlers, "_reset_bind_failure_state", None)
    if reset is not None:
        reset()


def _user(db, name):
    u = models.WebUser(username=name, password_hash="x")
    db.add(u)
    db.commit()
    return u


def _pending_code(db, user, code):
    row = models.TgBindCode(user_id=user.id, code=code,
                            expires_at=datetime.now() + timedelta(minutes=10))
    db.add(row)
    db.commit()
    return row


def _wrong(db, tg_id, code):
    return handlers.verify_bind_code(db, tg_id, tg_id, code)


def test_a_distributed_failures_never_invalidate_victim_code(db):
    victim = _user(db, "a111victim")
    row = _pending_code(db, victim, "654321")
    # 10 个攻击账号各猜错 2 次：全站累计 20 次，旧行为此刻已把受害者码作废
    for i in range(10):
        attacker = 8_110_000 + i
        _wrong(db, attacker, str(300000 + i))
        _wrong(db, attacker, str(310000 + i))
    db.refresh(row)
    assert (row.fail_count or 0) == 0, "他人的猜错不得记到受害者绑定码上"
    assert row.expires_at > datetime.now(), "受害者绑定码不得被他人猜错作废"
    # 受害者用自己的 TG 仍可正常绑定
    reply = handlers.verify_bind_code(db, 8_190_000, 8_190_000, "654321")
    assert reply and "绑定成功" in reply


def test_b_attributed_failures_void_only_that_code(db):
    # Bot 发起的码行（user_id 为空）：Bot 流程按字符串能命中该行但不可绑，
    # 猜错可归属到这一行——累计达上限应只作废这一行
    bot_row = models.TgBindCode(telegram_id=8_120_000, code="555555",
                                expires_at=datetime.now() + timedelta(minutes=10))
    db.add(bot_row)
    victim = _user(db, "a111bystander")
    victim_row = _pending_code(db, victim, "777888")
    db.commit()
    for i in range(10):
        attacker = 8_121_000 + i
        _wrong(db, attacker, "555555")
        _wrong(db, attacker, "555555")
    db.refresh(bot_row)
    db.refresh(victim_row)
    assert (bot_row.fail_count or 0) >= handlers.BIND_GLOBAL_FAIL_MAX
    assert bot_row.expires_at <= datetime.now(), "被反复猜错的码行本身应失效"
    assert (victim_row.fail_count or 0) == 0
    assert victim_row.expires_at > datetime.now(), "不相干的待验证码不得被牵连作废"
    reply = handlers.verify_bind_code(db, 8_129_000, 8_129_000, "777888")
    assert reply and "绑定成功" in reply


def test_c_global_signal_locks_out_only_the_guessing_account(db):
    # 全站失败达阈值后，只有越线的那个猜错账号被加严锁定；
    # 其他账号（含拿正确码的正常用户）不受影响
    user = _user(db, "a111normal")
    _pending_code(db, user, "246810")
    last_attacker = None
    for i in range(10):
        last_attacker = 8_130_000 + i
        _wrong(db, last_attacker, str(400000 + i))
        _wrong(db, last_attacker, str(410000 + i))
    # 越线账号：即便提交正确码也被锁定拒绝（与单账号限流同文案）
    reply = handlers.verify_bind_code(db, last_attacker, last_attacker, "246810")
    assert reply and "过于频繁" in reply
    db.refresh(user)
    assert user.telegram_id is None
    # 换一个干净账号提交同一个正确码：正常绑定（也证明该码没有被作废）
    reply = handlers.verify_bind_code(db, 8_139_000, 8_139_000, "246810")
    assert reply and "绑定成功" in reply


def test_d_normal_flow_and_account_rate_limit_unchanged(db):
    user = _user(db, "a111plain")
    _pending_code(db, user, "135790")
    reply = handlers.verify_bind_code(db, 8_140_000, 8_140_000, "135790")
    assert reply and "绑定成功" in reply
    db.refresh(user)
    assert user.telegram_id == 8_140_000
