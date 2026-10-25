"""播放密码不被门户登录覆盖（2026-10-23）。

接线员页显式设置的播放密码与门户密码共用 emby_password 字段：
门户每次登录经 _issue_auth_response 调 ensure_emby_credentials 同步，
把用户自设的播放密码顶成门户密码——播放器攥着旧凭据拿 401，
表现为「能进界面、资源数量不显示」。修法：门户登录改 fill_only，
仅当 emby_password 为空时才补齐；显式设置（set_emby_password）不带
fill_only，行为不变。
"""
import os

os.environ.setdefault("DATABASE_TYPE", "sqlite")
os.environ.setdefault("REDIS_ENABLED", "false")

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend import models
from backend.emby_server.auth import ensure_emby_credentials, verify_emby_password


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


def _make_user(db):
    u = models.WebUser(username="u1", password_hash="", points=0,
                       is_active=True, member_xp=0, member_level=1)
    db.add(u)
    db.commit()
    return u


def test_fill_only_does_not_clobber_existing_playback_password(db):
    user = _make_user(db)
    # 用户在接线员页显式设置播放密码（默认模式写入）
    ensure_emby_credentials(db, user, password="play-password")
    assert verify_emby_password("play-password", user.emby_password)
    # 门户登录同步（fill_only）：不许覆盖
    ensure_emby_credentials(db, user, password="portal-password", fill_only=True)
    db.refresh(user)
    assert verify_emby_password("play-password", user.emby_password)
    assert not verify_emby_password("portal-password", user.emby_password)


def test_fill_only_fills_when_empty(db):
    user = _make_user(db)
    assert not user.emby_password
    ensure_emby_credentials(db, user, password="portal-password", fill_only=True)
    db.refresh(user)
    assert verify_emby_password("portal-password", user.emby_password)
    # 用户名补齐不受 fill_only 影响
    assert user.emby_username


def test_explicit_set_still_overwrites(db):
    user = _make_user(db)
    ensure_emby_credentials(db, user, password="old-password")
    # 显式设置路径（set_emby_password 走默认模式）：照常覆盖
    ensure_emby_credentials(db, user, password="new-password")
    db.refresh(user)
    assert verify_emby_password("new-password", user.emby_password)
    assert not verify_emby_password("old-password", user.emby_password)
