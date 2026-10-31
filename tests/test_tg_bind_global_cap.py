"""绑定码猜错计数口径（A-111 改版）：分布式猜错不再作废受害者绑定码。

旧口径（已废）：全站累计猜错达上限即作废全部待验证绑定码——本身就是
可用性 DoS：几个 TG 账号串通猜错即可让所有人的绑定码失效。
新口径：猜错只影响猜错者本人——无归属的猜错只记入全站失败信号窗，
越线后只锁定触发的那个 TG 账号；可归属到某个码行的猜错只累计该行。
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
    handlers._reset_bind_failure_state()
    yield session
    session.close()
    store.invalidate()
    handlers._reset_bind_failure_state()


def _user(db, name):
    u = models.WebUser(username=name, password_hash="x")
    db.add(u)
    db.commit()
    return u


def test_distributed_bruteforce_no_longer_invalidates_pending_code(db):
    from backend import tg_bind

    victim = _user(db, "gvictim")
    row = models.TgBindCode(user_id=victim.id, code="654321",
                            expires_at=datetime.now() + timedelta(minutes=10))
    db.add(row)
    db.commit()
    # 每个攻击账号只猜 2 次（远低于单账号限流），但账号很多：
    # 旧口径下全站累计 60 次早已把受害者的码作废
    guess = 100000
    for i in range(30):
        attacker = 7_100_000 + i
        for _ in range(2):
            handlers.verify_bind_code(db, attacker, attacker, str(guess))
            guess += 1
    db.refresh(row)
    assert (row.fail_count or 0) == 0, "他人的猜错不得记到受害者绑定码上"
    assert row.expires_at > datetime.now(), "受害者绑定码不得被分布式猜错作废"
    # 网页端也不再把该码判为「因猜错超限失效」
    assert tg_bind._latest_code_expired_by_attempts(db, victim) is False
    # 受害者本人仍可正常绑定
    reply = handlers.verify_bind_code(db, 7_300_000, 7_300_000, "654321")
    assert reply and "绑定成功" in reply


def test_few_global_failures_do_not_invalidate(db):
    user = _user(db, "gok")
    db.add(models.TgBindCode(user_id=user.id, code="222333",
                             expires_at=datetime.now() + timedelta(minutes=10)))
    db.commit()
    for i in range(3):
        handlers.verify_bind_code(db, 7_400_000 + i, 7_400_000 + i, str(300000 + i))
    reply = handlers.verify_bind_code(db, 7_500_000, 7_500_000, "222333")
    assert reply and "绑定成功" in reply
