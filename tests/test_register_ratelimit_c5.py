"""C5 新用户注册限流回归测试。

背景：注册接口曾硬编码「同一 IP 每小时最多 5 次」限流；C5 将其改为
管理后台可配置（开关 / 窗口内最大次数 / 窗口秒数），默认保持原有行为。
"""

import os

os.environ.setdefault("DATABASE_TYPE", "sqlite")
os.environ.setdefault("REDIS_ENABLED", "false")

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend import models
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


def _set_config(db, key, value):
    db.add(models.SystemConfig(key=key, value=value))
    db.commit()


def test_get_register_ratelimit_defaults(db):
    """SystemConfig 为空时回退默认：启用 / 5 次 / 3600 秒。"""
    assert _get_register_ratelimit(db) == (True, 5, 3600)


def test_get_register_ratelimit_custom(db):
    """写入三条配置后按配置返回。"""
    _set_config(db, "register_ratelimit_enabled", "false")
    _set_config(db, "register_ratelimit_max", "10")
    _set_config(db, "register_ratelimit_window", "7200")
    assert _get_register_ratelimit(db) == (False, 10, 7200)


def test_get_register_ratelimit_bad_values(db):
    """脏数据容错：数字转失败回退默认；enabled 只有显式 "true" 才启用。

    注意实现细节：enabled 的判定是 `value.strip().lower() == "true"`，
    所以 "yes" 会被判为 False（关闭），这是有意为之的严格语义。
    """
    _set_config(db, "register_ratelimit_enabled", "yes")
    _set_config(db, "register_ratelimit_max", "abc")
    _set_config(db, "register_ratelimit_window", "")
    assert _get_register_ratelimit(db) == (False, 5, 3600)


def test_get_register_ratelimit_non_positive_fallback(db):
    """非正数值回退默认：防直接改库的脏数据（管理后台写入时已有校验）。"""
    _set_config(db, "register_ratelimit_enabled", "true")
    _set_config(db, "register_ratelimit_max", "0")
    _set_config(db, "register_ratelimit_window", "-5")
    assert _get_register_ratelimit(db) == (True, 5, 3600)


def test_set_registration_rejects_bad_ratelimit(db):
    """PUT 校验：max < 1 或 window < 60 时抛 400。"""
    from backend.api.admin import RegistrationModeRequest, set_registration_mode

    admin = models.WebUser(username="admin_c5", password_hash="x", is_staff=True)
    db.add(admin)
    db.commit()

    with pytest.raises(HTTPException) as exc:
        set_registration_mode(
            RegistrationModeRequest(mode="open", ratelimit_max=0),
            current_admin=admin,
            db=db,
        )
    assert exc.value.status_code == 400

    with pytest.raises(HTTPException) as exc:
        set_registration_mode(
            RegistrationModeRequest(mode="open", ratelimit_window=30),
            current_admin=admin,
            db=db,
        )
    assert exc.value.status_code == 400


def test_ratelimit_config_roundtrip(db):
    """经管理接口写入限流配置后，GET 能读回；原有 mode/message 不受影响。"""
    from backend.api.admin import (
        RegistrationModeRequest,
        get_registration_mode,
        set_registration_mode,
    )

    admin = models.WebUser(username="admin_c5b", password_hash="x", is_staff=True)
    db.add(admin)
    db.commit()

    result = set_registration_mode(
        RegistrationModeRequest(
            mode="open",
            message="",
            ratelimit_enabled=False,
            ratelimit_max=3,
            ratelimit_window=600,
        ),
        current_admin=admin,
        db=db,
    )
    assert result["success"] is True

    got = get_registration_mode(current_admin=admin, db=db)
    assert got["mode"] == "open"
    # 2026-10-21 A 类摘面板：ratelimit_enabled 不再经面板写（PUT 传 False
    # 被忽略），GET 回显生效值（缺省开）；max/window 照常写读
    assert got["ratelimit_enabled"] is True
    assert got["ratelimit_max"] == 3
    assert got["ratelimit_window"] == 600

    # 注册侧读取到同一份配置
    assert _get_register_ratelimit(db) == (True, 3, 600)


def test_register_uses_configured_limit():
    """滑动窗口限流器按配置值工作：max=2 时第 3 次被拒绝。"""
    from backend.ratelimit import check_rate_limit, _limiter

    key = "register:c5-test-ip"
    _limiter.reset(key)
    allowed1, _ = check_rate_limit(key, 2, 60)
    allowed2, _ = check_rate_limit(key, 2, 60)
    allowed3, retry = check_rate_limit(key, 2, 60)
    assert allowed1 is True
    assert allowed2 is True
    assert allowed3 is False
    assert retry >= 1
    _limiter.reset(key)
