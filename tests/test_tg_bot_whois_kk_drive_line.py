# -*- coding: utf-8 -*-
"""TG Bot /kk 精简卡「网盘绑定」一行新设计（用户 2026-11-08 拍板）。

口径：
- 一行汇总：``💾 谷歌盘 ×N：标签 状态 · 标签 状态``，用绑定自设
  标签认盘；正常只打 ✓ 不写字，异常才写文字（已暂停 / 已失效 /
  已过期）；在公共池供片的绑定（PoolMember 未退出）状态后加「池」。
- 本设计取代 2026-11-01「精简卡只出 provider＋状态、无 label」
  口径：标签是用户自设的展示名，用户 2026-11-08 明确批准进群卡；
  隐私红线不变——邮箱 / 账号原文 / 凭据 id 绝不出现。
- 无标签绑定回退「盘N」序号；零绑定显示「💾 未绑定」。
"""
from __future__ import annotations

import os
import sys

os.environ.setdefault("DATABASE_TYPE", "sqlite")
os.environ.setdefault("REDIS_ENABLED", "false")
sys.path.insert(0, ".")

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend import models
from backend.emby_server import models as em
from backend.integrations import store
from backend.tg_bot import whois


@pytest.fixture()
def db():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    for table in (
        models.WebUser.__table__,
        models.SystemConfig.__table__,
        models.UserSubscription.__table__,
        em.UserStorageBinding.__table__,
        em.PoolMember.__table__,
    ):
        table.create(engine, checkfirst=True)
    session = sessionmaker(bind=engine)()
    store.invalidate()
    yield session
    session.close()
    store.invalidate()


def _user(db, username="target"):
    u = models.WebUser(username=username)
    u.password_hash = ""
    db.add(u)
    db.commit()
    return u


def _bind(db, user_id, label, provider="drive", status="active", cred=101):
    row = em.UserStorageBinding(
        user_id=user_id, account_label=label, provider=provider,
        credential_id=cred, status=status)
    db.add(row)
    db.commit()
    return row


def _pool(db, user_id, binding, status):
    m = em.PoolMember(
        user_id=user_id, binding_id=binding.id,
        provider=binding.provider, status=status)
    db.add(m)
    db.commit()
    return m


def test_four_active_drives_one_line_with_labels(db):
    u = _user(db)
    for i, label in enumerate(("123", "456", "789", "101")):
        _bind(db, u.id, label, cred=100 + i)
    line = whois._binding_lite_line(db, u.id)
    assert line == "💾 谷歌盘 ×4：123 ✓ · 456 ✓ · 789 ✓ · 101 ✓"
    # 旧渲染的重复文案绝不出现
    assert "谷歌网盘｜正常" not in line
    assert line.count("谷歌网盘") == 0


def test_suspended_pool_drive_shows_pause_and_pool_mark(db):
    u = _user(db)
    _bind(db, u.id, "123", cred=100)
    b101 = _bind(db, u.id, "101", cred=101)
    _pool(db, u.id, b101, "suspended")
    line = whois._binding_lite_line(db, u.id)
    assert line == "💾 谷歌盘 ×2：123 ✓ · 101 ⏸已暂停·池"


def test_active_pool_drive_shows_pool_mark_after_check(db):
    u = _user(db)
    b = _bind(db, u.id, "456", cred=100)
    _pool(db, u.id, b, "active")
    line = whois._binding_lite_line(db, u.id)
    assert line == "💾 谷歌盘 ×1：456 ✓池"


def test_exited_pool_member_has_no_pool_mark(db):
    u = _user(db)
    b = _bind(db, u.id, "456", cred=100)
    _pool(db, u.id, b, "exited")
    line = whois._binding_lite_line(db, u.id)
    assert line == "💾 谷歌盘 ×1：456 ✓"


def test_invalid_and_expired_status_text(db):
    u = _user(db)
    _bind(db, u.id, "主盘", status="invalid", cred=100)
    _bind(db, u.id, "副盘", status="expired", cred=101)
    line = whois._binding_lite_line(db, u.id)
    assert line == "💾 谷歌盘 ×2：主盘 ✕已失效 · 副盘 ✕已过期"


def test_blank_label_falls_back_to_serial(db):
    u = _user(db)
    _bind(db, u.id, "123", cred=100)
    _bind(db, u.id, "", cred=101)
    _bind(db, u.id, "  ", cred=102)
    line = whois._binding_lite_line(db, u.id)
    assert line == "💾 谷歌盘 ×3：123 ✓ · 盘2 ✓ · 盘3 ✓"


def test_no_bindings(db):
    u = _user(db)
    assert whois._binding_lite_line(db, u.id) == "💾 未绑定"


def test_mixed_providers_grouped(db):
    u = _user(db)
    _bind(db, u.id, "主盘", provider="drive", cred=100)
    _bind(db, u.id, "副盘", provider="pan115", status="expired", cred=101)
    line = whois._binding_lite_line(db, u.id)
    assert line == "💾 谷歌盘 ×1：主盘 ✓；115 网盘 ×1：副盘 ✕已过期"


def test_card_level_line_and_no_credentials(db):
    u = _user(db)
    _bind(db, u.id, "123", cred=987654)
    card = whois.render_whois_lite(db, u)
    assert "💾 谷歌盘 ×1：123 ✓" in card
    assert "987654" not in card
