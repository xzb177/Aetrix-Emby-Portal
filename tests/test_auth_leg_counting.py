"""鉴权腿计数（播放全切观测口径）的回归测试。

计数只记成功走过的腿、失败静默，绝不影响鉴权本身。
全部离线：内存 SQLite，不发起真实网络请求、不依赖真实 Redis
（redis_call 由 monkeypatch 桩掉）。
"""
from __future__ import annotations

import os

import pytest
from fastapi.security import HTTPAuthorizationCredentials
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from starlette.requests import Request

from backend import models
from backend.emby_server import auth
from backend.emby_server import models as em
from backend.security import create_access_token

TEST_SECRET = "test-only-secret-for-auth-leg-counting"


@pytest.fixture(autouse=True)
def _secret_key(monkeypatch):
    if not (os.environ.get("SECRET_KEY") or "").strip():
        monkeypatch.setenv("SECRET_KEY", TEST_SECRET)


@pytest.fixture()
def db():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False},
                           poolclass=StaticPool)
    models.Base.metadata.create_all(engine)
    em.Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    yield session
    session.close()


def _user(db, username):
    u = models.WebUser(username=username, password_hash="x", is_active=True)
    db.add(u)
    db.commit()
    return u


def _request(path="/", headers=None, query=b"", path_params=None):
    scope = {
        "type": "http", "method": "GET", "path": path, "query_string": query,
        "headers": [(k.lower().encode(), v.encode()) for k, v in (headers or {}).items()],
        "client": ("1.2.3.4", 1), "path_params": path_params or {},
    }
    return Request(scope)


class _FakeRedis:
    def __init__(self):
        self.calls = []

    def hincrby(self, key, field, amount):
        self.calls.append((key, field, amount))
        return 1


@pytest.fixture()
def recorder(monkeypatch):
    fake = _FakeRedis()

    def _redis_call(fn, default=None):
        return fn(fake)

    monkeypatch.setattr("backend.database.redis_call", _redis_call)
    return fake


def _fields(recorder):
    return [field for _, field, _ in recorder.calls]


def test_count_helper_records_each_leg(recorder):
    for leg in ("play_sign", "emby_token", "emby_jwt", "resolve_token", "resolve_jwt"):
        auth._count_auth_leg(leg)
    assert _fields(recorder) == ["play_sign", "emby_token", "emby_jwt",
                                 "resolve_token", "resolve_jwt"]
    assert all(key == auth.AUTH_LEG_COUNTER_KEY for key, _, _ in recorder.calls)


def test_count_helper_failure_is_silent(monkeypatch):
    def _boom(fn, default=None):
        raise RuntimeError("redis down")

    monkeypatch.setattr("backend.database.redis_call", _boom)
    auth._count_auth_leg("emby_token")  # 不许抛


def test_get_emby_user_token_leg_counted(db, recorder):
    user = _user(db, "tok")
    raw = "t" * 40
    db.add(em.EmbyApiToken(token=auth.hash_emby_token(raw), user_id=user.id,
                           device_id="test", is_revoked=False))
    db.commit()
    got = auth.get_emby_user(_request(query=f"api_key={raw}".encode()), db, None)
    assert got.id == user.id
    assert "emby_token" in _fields(recorder)
    assert "emby_jwt" not in _fields(recorder)


def test_get_emby_user_jwt_rejected(db, recorder):
    """JWT 回退已删：请求头里的 JWT 不再被接受，也不记 emby_jwt 腿。"""
    import pytest
    from fastapi import HTTPException

    user = _user(db, "jwt")
    jwt = create_access_token(user.id, {"username": "jwt"})
    with pytest.raises(HTTPException) as exc:
        auth.get_emby_user(
            _request(headers={"Authorization": f"Bearer {jwt}"}), db,
            HTTPAuthorizationCredentials(scheme="Bearer", credentials=jwt))
    assert exc.value.status_code == 401
    assert "emby_jwt" not in _fields(recorder)


def test_resolve_request_user_legs_counted(db, recorder):
    user = _user(db, "res")
    raw = "r" * 40
    db.add(em.EmbyApiToken(token=auth.hash_emby_token(raw), user_id=user.id,
                           device_id="test", is_revoked=False))
    db.commit()
    got = auth.resolve_request_user(db, _request(query=f"api_key={raw}".encode()))
    assert got is not None and got.id == user.id
    assert "resolve_token" in _fields(recorder)

    # JWT 回退已删：Bearer JWT 解析不到用户，且不记 resolve_jwt 腿。
    jwt = create_access_token(user.id, {"username": "res"})
    got = auth.resolve_request_user(
        db, _request(headers={"Authorization": f"Bearer {jwt}"}))
    assert got is None
    assert "resolve_jwt" not in _fields(recorder)


def test_get_play_user_sign_leg_counted(db, recorder, monkeypatch):
    from backend.emby_server import play_sign

    user = _user(db, "sig")
    monkeypatch.setattr(play_sign, "hotlink_enabled", lambda db_: True)
    monkeypatch.setattr(play_sign, "verify_play_sign", lambda *a: True)
    got = auth.get_play_user(
        _request(query=f"sign=s&exp=9999999999&uid={user.id}".encode(),
                 path_params={"item_id": "item-1"}),
        db, None)
    assert got.id == user.id
    assert "play_sign" in _fields(recorder)


def test_count_failure_does_not_break_auth(db, monkeypatch):
    user = _user(db, "ok")
    raw = "k" * 40
    db.add(em.EmbyApiToken(token=auth.hash_emby_token(raw), user_id=user.id,
                           device_id="test", is_revoked=False))
    db.commit()

    def _boom(fn, default=None):
        raise RuntimeError("redis down")

    monkeypatch.setattr("backend.database.redis_call", _boom)
    got = auth.get_emby_user(_request(query=f"api_key={raw}".encode()), db, None)
    assert got.id == user.id
