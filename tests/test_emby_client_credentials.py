"""接线账号：用户自己设用户名和密码，用户名设好后不可改。

打开账号卡和门户登录都不再发明 ``emby_{id}_{hex}``。
``POST /api/user/emby/password`` 是唯一写入接线用户名的入口。
"""
import asyncio
import json
import os
from datetime import datetime, timedelta

os.environ.setdefault("DATABASE_TYPE", "sqlite")
os.environ.setdefault("REDIS_ENABLED", "false")
os.environ.setdefault("SECRET_KEY", "test-emby-credentials-key-32chars-min")

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend import models
from backend.api.emby_portal import _issue_auth_response
from backend.emby_server.auth import verify_emby_password
from backend.emby_server.portal import (
    SetPasswordRequest,
    _account_card,
    get_server_info,
    set_emby_password,
)
from backend.security import hash_password


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


def _make_user(db, username="u1"):
    user = models.WebUser(
        username=username, password_hash="x", points=0,
        is_active=True, member_xp=0, member_level=1,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def _subscribe(db, user):
    from backend import realms

    realm = realms.ensure_default_realm(db)
    plan = models.SubscriptionPlan(
        name=f"月卡-{user.id}", price=1, duration_days=30, realm_id=realm.id,
    )
    db.add(plan)
    db.flush()
    db.add(models.UserSubscription(
        user_id=user.id, plan_id=plan.id, realm_id=realm.id, status="active",
        end_date=datetime.now() + timedelta(days=30),
    ))
    db.commit()


def _set(db, user, **body):
    req = SetPasswordRequest(**body)
    return set_emby_password(req, user, db)


def _raises(db, user, **body):
    with pytest.raises(HTTPException) as caught:
        _set(db, user, **body)
    return caught.value


def test_login_does_not_invent_username_or_password(db):
    user = _make_user(db)
    out = _issue_auth_response(user, db, plain_password="portal-secret")
    assert out.user.emby_username is None
    db.refresh(user)
    assert user.emby_username is None
    assert not user.emby_password


def test_login_fill_only_when_username_exists_but_password_missing(db):
    user = _make_user(db)
    user.emby_username = "kept_name"
    db.commit()
    _issue_auth_response(user, db, plain_password="portal-secret")
    db.refresh(user)
    assert user.emby_username == "kept_name"
    assert verify_emby_password("portal-secret", user.emby_password)
    _issue_auth_response(user, db, plain_password="other-secret")
    db.refresh(user)
    assert user.emby_username == "kept_name"
    assert verify_emby_password("portal-secret", user.emby_password)
    assert not verify_emby_password("other-secret", user.emby_password)


def test_first_time_requires_username_and_password(db):
    user = _make_user(db)
    err = _raises(db, user, password="secret1")
    assert err.status_code == 400
    assert err.detail == "首次设置接线账号需要同时提供用户名和密码"
    db.refresh(user)
    assert user.emby_username is None
    assert not user.emby_password

    err = _raises(db, user, username="   ", password="secret1")
    assert err.status_code == 400
    assert "用户名" in err.detail
    db.refresh(user)
    assert user.emby_username is None


def test_first_time_rejects_bad_username_without_writing_password(db):
    user = _make_user(db)
    for bad in ("ab", "has space", "中文名", "a" * 33, "bad-name"):
        err = _raises(db, user, username=bad, password="secret1")
        assert err.status_code == 400, bad
        assert err.detail == "用户名只能包含字母、数字、下划线，长度 3-32 位"
    db.refresh(user)
    assert user.emby_username is None
    assert not user.emby_password


def test_short_password_does_not_set_username(db):
    user = _make_user(db)
    err = _raises(db, user, username="good_name", password="ab")
    assert err.status_code == 400
    assert "密码" in err.detail
    db.refresh(user)
    assert user.emby_username is None
    assert not user.emby_password


def test_first_time_sets_both_and_response_has_no_plaintext(db):
    user = _make_user(db, "carol")
    body = _set(db, user, username="  carol_play  ", password="secret1")
    assert body == {"success": True, "emby_username": "carol_play"}
    assert "secret1" not in json.dumps(body)
    db.refresh(user)
    assert user.emby_username == "carol_play"
    assert verify_emby_password("secret1", user.emby_password)
    assert user.emby_password != "secret1"


def test_username_may_match_own_portal_username(db):
    user = _make_user(db, "carol")
    body = _set(db, user, username="carol", password="secret1")
    assert body["emby_username"] == "carol"


def test_register_rejects_portal_username_matching_other_emby_username(db):
    """注册门户名不能撞别人已有的接线名（AuthenticateByName 两个字段都认）。"""
    from starlette.requests import Request

    from backend.api import emby_portal

    owner = _make_user(db, "alice")
    owner.emby_username = "taken_play"
    db.add(models.SystemConfig(key="register_ratelimit_enabled", value="false"))
    db.commit()

    scope = {
        "type": "http", "method": "POST", "path": "/", "headers": [],
        "client": ("1.2.3.4", 1234),
    }
    req = emby_portal.RegisterRequest(username=" taken_play ", password="secret123")
    with pytest.raises(HTTPException) as exc:
        emby_portal.register(Request(scope), req, db)
    assert exc.value.status_code == 409
    assert exc.value.detail == "用户名已被占用"
    assert db.query(models.WebUser).filter(models.WebUser.username == "taken_play").first() is None
    db.refresh(owner)
    assert owner.emby_username == "taken_play"

    ok = emby_portal.register(
        Request(scope),
        emby_portal.RegisterRequest(username="fresh_user", password="secret123"),
        db,
    )
    assert ok.user.username == "fresh_user"
    stored = db.query(models.WebUser).filter_by(username="fresh_user").one()
    assert stored.emby_username is None


def test_username_unique_against_other_emby_and_portal_names(db):
    owner = _make_user(db, "alice")
    owner.emby_username = "taken_emby"
    db.commit()
    other = _make_user(db, "bob")
    err = _raises(db, other, username="taken_emby", password="secret1")
    assert err.status_code == 409
    assert err.detail == "用户名已被占用"
    err = _raises(db, other, username="alice", password="secret1")
    assert err.status_code == 409
    db.refresh(other)
    assert other.emby_username is None
    assert not other.emby_password


def test_username_unique_race_returns_409(db, monkeypatch):
    owner = _make_user(db, "owner")
    owner.emby_username = "taken_name"
    db.commit()
    user = _make_user(db, "newbie")
    monkeypatch.setattr(
        "backend.emby_server.portal._emby_username_taken", lambda *args, **kwargs: False,
    )
    err = _raises(db, user, username="taken_name", password="secret1")
    assert err.status_code == 409
    assert err.detail == "用户名已被占用"
    db.rollback()
    stored = db.query(models.WebUser).filter(models.WebUser.username == "newbie").one()
    assert stored.emby_username is None


def test_username_immutable_password_only_after_set(db):
    user = _make_user(db)
    _set(db, user, username="play_name", password="secret1")

    err = _raises(db, user, username="other_name", password="secret2")
    assert err.status_code == 400
    assert err.detail == "用户名设好后不可修改"
    db.refresh(user)
    assert user.emby_username == "play_name"
    assert verify_emby_password("secret1", user.emby_password)

    err = _raises(db, user, username="", password="secret2")
    assert err.status_code == 400
    assert err.detail == "用户名设好后不可修改"

    body = _set(db, user, username="play_name", password="secret2")
    assert body == {"success": True, "emby_username": "play_name"}
    body = _set(db, user, password="secret3")
    assert body["emby_username"] == "play_name"
    db.refresh(user)
    assert user.emby_username == "play_name"
    assert verify_emby_password("secret3", user.emby_password)
    assert not verify_emby_password("secret1", user.emby_password)


def test_account_card_unset_does_not_invent_credentials(db, monkeypatch):
    monkeypatch.setenv("EMBY_PUBLIC_URL", "http://media.example.com:8000")
    user = _make_user(db, "viewer")
    _subscribe(db, user)

    card = _account_card(user, db)
    assert card["emby_username"] == ""
    assert card["emby_password"] is None
    assert card["has_password"] is False
    assert card["import_schemes"] == {}
    assert card["view_permission"]["granted"] is True

    class _Req:
        query_params: dict = {}
        headers: dict = {}

    seen = asyncio.run(get_server_info(_Req(), user, db))
    assert seen["emby_username"] == ""
    assert seen["has_password"] is False
    assert seen["import_schemes"] == {}
    assert seen["emby_password"] is None
    db.refresh(user)
    assert user.emby_username is None
    assert not user.emby_password


def test_account_card_schemes_omit_password(db, monkeypatch):
    monkeypatch.setenv("EMBY_PUBLIC_URL", "http://media.example.com:8000")
    user = _make_user(db, "viewer2")
    user.emby_username = "viewer_play"
    user.emby_password = hash_password("secret-play")
    db.commit()
    _subscribe(db, user)

    card = _account_card(user, db)
    assert card["emby_username"] == "viewer_play"
    assert card["emby_password"] is None
    assert card["has_password"] is True
    schemes = card["import_schemes"]
    assert set(schemes) == {"Rex", "senplayer", "VidHub"}
    blob = json.dumps(schemes)
    assert "viewer_play" in blob
    assert "password=" not in blob
    assert "secret-play" not in blob
