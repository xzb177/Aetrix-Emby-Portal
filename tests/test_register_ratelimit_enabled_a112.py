"""A-112 回归：注册设置 PUT 的 ratelimit_enabled 必须真落库。

背景：2026-10-21 A 类摘面板后，PUT 仍收 ratelimit_enabled 字段、
docstring 仍承诺可改，但落库字典里没有该键，面板开关是死开关
（PUT 200 success 却永远改不动生效值）。方案①恢复写入。
"""

import os

os.environ.setdefault("DATABASE_TYPE", "sqlite")
os.environ.setdefault("REDIS_ENABLED", "false")

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend import models
from backend.api.admin import (
    RegistrationModeRequest,
    get_registration_mode,
    set_registration_mode,
)
from backend.api.emby_portal import _get_register_ratelimit


@pytest.fixture()
def db():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    models.Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    yield session
    session.close()


def _admin(db, username):
    admin = models.WebUser(username=username, password_hash="x", is_staff=True)
    db.add(admin)
    db.commit()
    return admin


def test_put_ratelimit_enabled_false_persists(db):
    """PUT ratelimit_enabled=false 后 GET 回 false，注册侧读取亦为关。"""
    admin = _admin(db, "admin_a112_a")
    result = set_registration_mode(
        RegistrationModeRequest(mode="open", ratelimit_enabled=False),
        current_admin=admin,
        db=db,
    )
    assert result["success"] is True
    assert result["ratelimit_enabled"] is False
    got = get_registration_mode(current_admin=admin, db=db)
    assert got["ratelimit_enabled"] is False
    assert _get_register_ratelimit(db)[0] is False


def test_put_ratelimit_enabled_true_roundtrip(db):
    """先关后开：PUT true 能把已关的开关重新打开。"""
    admin = _admin(db, "admin_a112_b")
    set_registration_mode(
        RegistrationModeRequest(mode="open", ratelimit_enabled=False),
        current_admin=admin,
        db=db,
    )
    assert get_registration_mode(current_admin=admin, db=db)["ratelimit_enabled"] is False
    result = set_registration_mode(
        RegistrationModeRequest(mode="open", ratelimit_enabled=True),
        current_admin=admin,
        db=db,
    )
    assert result["ratelimit_enabled"] is True
    assert get_registration_mode(current_admin=admin, db=db)["ratelimit_enabled"] is True
    assert _get_register_ratelimit(db)[0] is True


def test_put_ratelimit_enabled_none_leaves_value(db):
    """None = 不修改：只改 max/window 时既有开关值保持不变。"""
    admin = _admin(db, "admin_a112_c")
    set_registration_mode(
        RegistrationModeRequest(mode="open", ratelimit_enabled=False),
        current_admin=admin,
        db=db,
    )
    result = set_registration_mode(
        RegistrationModeRequest(mode="open", ratelimit_max=7),
        current_admin=admin,
        db=db,
    )
    assert result["ratelimit_enabled"] is False
    got = get_registration_mode(current_admin=admin, db=db)
    assert got["ratelimit_enabled"] is False
    assert got["ratelimit_max"] == 7


def test_put_ratelimit_enabled_audit_details(db):
    """审计 details 含 ratelimit_enabled，不再只记 mode/max/window。"""
    admin = _admin(db, "admin_a112_d")
    set_registration_mode(
        RegistrationModeRequest(mode="open", ratelimit_enabled=False),
        current_admin=admin,
        db=db,
    )
    row = (
        db.query(models.AdminLog)
        .filter(models.AdminLog.action == "set_registration_mode")
        .order_by(models.AdminLog.id.desc())
        .first()
    )
    assert row is not None
    assert row.details["ratelimit_enabled"] is False


def test_get_ratelimit_enabled_env_takeover(db, monkeypatch):
    """env 显式设置时仍接管：PUT 写 false 后 GET 仍以 env 为准。"""
    admin = _admin(db, "admin_a112_e")
    set_registration_mode(
        RegistrationModeRequest(mode="open", ratelimit_enabled=False),
        current_admin=admin,
        db=db,
    )
    monkeypatch.setenv("REGISTER_RATELIMIT_ENABLED", "true")
    assert get_registration_mode(current_admin=admin, db=db)["ratelimit_enabled"] is True
    assert _get_register_ratelimit(db)[0] is True
