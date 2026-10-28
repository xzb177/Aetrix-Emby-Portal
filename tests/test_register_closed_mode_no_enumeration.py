"""r4-auth 回归：注册关闭时不得用 409/403 区分泄露名字是否被占用。

旧顺序先查撞名、后查模式：关闭注册时探已占用的名字回 409、
探未占用的名字回 403，等于用注册接口枚举门户名与接线名。
修复：模式检查移到撞名检查之前，关闭时一律 403。
开放模式下的撞名行为不变（仍 409，见 #1396 的既有用例）。
"""
import os

os.environ.setdefault("DATABASE_TYPE", "sqlite")
os.environ.setdefault("REDIS_ENABLED", "false")
os.environ.setdefault("SECRET_KEY", "test-register-closed-key-32chars-min")

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from starlette.requests import Request

from backend import models
from backend.api import emby_portal


@pytest.fixture()
def db():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    models.Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    try:
        yield session
    finally:
        session.rollback()
        session.close()
        engine.dispose()


def _scope():
    return {"type": "http", "method": "POST", "path": "/",
            "headers": [], "client": ("1.2.3.4", 1234)}


def _set_mode(db, value):
    db.add(models.SystemConfig(key="register_ratelimit_enabled", value="false"))
    db.add(models.SystemConfig(key="registration_mode", value=value))
    db.commit()


def _register(db, username):
    return emby_portal.register(
        Request(_scope()),
        emby_portal.RegisterRequest(username=username, password="pw-123456"),
        db,
    )


def test_closed_mode_returns_403_for_taken_and_free_names(db):
    owner = models.WebUser(username="alice", password_hash="x", is_active=True,
                           emby_username="taken_play")
    db.add(owner)
    _set_mode(db, "closed")
    for name in ("alice", "taken_play", "nobody_here"):
        with pytest.raises(HTTPException) as exc:
            _register(db, name)
        assert exc.value.status_code == 403
        assert exc.value.detail == "当前未开放注册"


def test_open_mode_still_409_on_emby_collision(db):
    owner = models.WebUser(username="alice", password_hash="x", is_active=True,
                           emby_username="taken_play")
    db.add(owner)
    _set_mode(db, "open")
    with pytest.raises(HTTPException) as exc:
        _register(db, "taken_play")
    assert exc.value.status_code == 409
    assert exc.value.detail == "用户名已被占用"
