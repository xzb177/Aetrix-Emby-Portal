"""PA-048 回归测试：用户端登出必须使旧会话立即失效。

背景：此前 user_frontend 的 logout() 只清本地，从未调用后端注销口；
后端 /api/user/auth/logout（吊销 jti＋bump token_version）早已实现却无人调用，
偷到的 refresh 可在 30 天内不断换票。本单前端已接线；本文件覆盖服务端一侧：
登出后旧 refresh 换票必须 401、旧 access 调 /me 必须 401。

全部离线：内存 SQLite + 进程内路由，不发起真实网络请求。
"""
from __future__ import annotations

import os
import pathlib

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend import models
from backend.emby_server import models as em
from backend.security import (
    create_access_token,
    create_refresh_token,
    hash_password,
)

ROOT = pathlib.Path(__file__).resolve().parents[1]
TEST_SECRET = "pa048-test-secret-not-a-real-key"


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


def _user(db, username="u1"):
    u = models.WebUser(username=username, password_hash=hash_password("pw-123456"),
                       is_staff=False, is_active=True, token_version=0)
    db.add(u)
    db.commit()
    return u


@pytest.fixture()
def user_client(db):
    from backend.api.emby_portal import auth_router
    from backend.database import get_db

    app = FastAPI()
    app.include_router(auth_router)

    def _get_db():
        yield db

    app.dependency_overrides[get_db] = _get_db
    with TestClient(app) as c:
        yield c


def test_user_logout_invalidates_old_refresh_and_access(db, user_client):
    u = _user(db)
    access = create_access_token(u.id, {"username": u.username}, token_version=0)
    refresh = create_refresh_token(u.id, token_version=0)

    r = user_client.post("/api/user/auth/logout", json={},
                         headers={"Authorization": f"Bearer {access}"})
    assert r.status_code == 200

    db.refresh(u)
    assert u.token_version == 1, "登出必须 bump token_version，否则旧 refresh 仍可换票"

    r = user_client.post("/api/user/auth/refresh", json={"refresh_token": refresh})
    assert r.status_code == 401, "登出后旧 refresh 换票必须被拒"

    r = user_client.get("/api/user/auth/me",
                        headers={"Authorization": f"Bearer {access}"})
    assert r.status_code == 401, "登出后旧 access 调 /me 必须被拒"
