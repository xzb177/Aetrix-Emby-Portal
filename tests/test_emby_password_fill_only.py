"""播放密码不被门户登录覆盖，且不再自动生成接线用户名。

接线员页显式设置的播放密码与门户密码共用 emby_password 字段：
门户每次登录经 _issue_auth_response 调 ensure_emby_credentials 同步，
把用户自设的播放密码顶成门户密码——播放器攥着旧凭据拿 401，
表现为「能进界面、资源数量不显示」。修法：门户登录改 fill_only，
仅当已有接线用户名且 emby_password 为空时才补齐。

没有接线用户名时，fill_only 什么都不写（不发明用户名，也不把门户密码
当成播放密码）。显式设置（不带 fill_only）仍可改密码，但不发明用户名。
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


def test_ensure_does_not_invent_username(db):
    user = _make_user(db)
    ensure_emby_credentials(db, user)
    db.refresh(user)
    assert user.emby_username is None
    assert user.emby_password is None


def test_fill_only_does_nothing_without_username(db):
    user = _make_user(db)
    ensure_emby_credentials(db, user, password="portal-password", fill_only=True)
    db.refresh(user)
    assert user.emby_username is None
    assert user.emby_password is None


def test_fill_only_does_not_clobber_existing_playback_password(db):
    user = _make_user(db)
    user.emby_username = "u1_play"
    db.commit()
    # 用户在接线员页显式设置播放密码（默认模式写入）
    ensure_emby_credentials(db, user, password="play-password")
    assert verify_emby_password("play-password", user.emby_password)
    # 门户登录同步（fill_only）：不许覆盖，也不改用户名
    ensure_emby_credentials(db, user, password="portal-password", fill_only=True)
    db.refresh(user)
    assert user.emby_username == "u1_play"
    assert verify_emby_password("play-password", user.emby_password)
    assert not verify_emby_password("portal-password", user.emby_password)


def test_fill_only_fills_when_username_exists_and_password_empty(db):
    user = _make_user(db)
    user.emby_username = "u1_play"
    db.commit()
    assert not user.emby_password
    ensure_emby_credentials(db, user, password="portal-password", fill_only=True)
    db.refresh(user)
    assert user.emby_username == "u1_play"
    assert verify_emby_password("portal-password", user.emby_password)


def test_explicit_set_still_overwrites_without_inventing_username(db):
    user = _make_user(db)
    user.emby_username = "u1_play"
    db.commit()
    ensure_emby_credentials(db, user, password="old-password")
    # 显式设置路径（set_emby_password 走默认模式）：照常覆盖
    ensure_emby_credentials(db, user, password="new-password")
    db.refresh(user)
    assert user.emby_username == "u1_play"
    assert verify_emby_password("new-password", user.emby_password)
    assert not verify_emby_password("old-password", user.emby_password)


def test_explicit_set_without_username_stores_password_only(db):
    """管理员重置 / 部署自检走显式模式：可以只写密码哈希，仍然不发明用户名。"""
    user = _make_user(db)
    ensure_emby_credentials(db, user, password="admin-reset")
    db.refresh(user)
    assert user.emby_username is None
    assert verify_emby_password("admin-reset", user.emby_password)
