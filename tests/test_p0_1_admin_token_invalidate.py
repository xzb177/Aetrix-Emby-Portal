"""P0-1 回归测试：管理员改密/登出必须使旧 token 失效。

背景：此前管理员改密只改 password_hash，不 bump token_version、不吊销 jti，
且 get_current_admin 不做新鲜度检查，管理后台也没有登出接口。
被盗的管理员账号即使改密，攻击者手里的 access（2h）+ refresh（30d）仍全部有效。

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
    decode_token,
    hash_password,
)

ROOT = pathlib.Path(__file__).resolve().parents[1]
TEST_SECRET = "test-only-secret-key-0123456789abcdef"


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


def _admin(db, username="boss", password="old-password-123"):  # secret-scan: allow —— 测试用假密码，非真实密钥
    u = models.WebUser(username=username, password_hash=hash_password(password),
                       is_staff=True, is_active=True, token_version=0,
                       admin_role="super")
    db.add(u)
    db.commit()
    return u


@pytest.fixture()
def admin_client(db):
    from backend.api import admin_auth  # noqa: F401 — 注册 /auth/* 路由
    from backend.api.admin_core import admin_router
    from backend.database import get_db

    app = FastAPI()
    app.include_router(admin_router)

    def _get_db():
        yield db

    app.dependency_overrides[get_db] = _get_db
    with TestClient(app) as c:
        yield c


def _bearer(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


# ===========================================================================
# 1. 改密后旧 access token 必须失效（token_version+1）
# ===========================================================================

def test_change_password_bumps_token_version(db, admin_client):
    admin = _admin(db)
    old_tv = admin.token_version or 0

    old_access = create_access_token(admin.id, {"username": admin.username, "staff": True},
                                     token_version=old_tv)
    # 改密前旧 token 有效
    r = admin_client.get("/api/admin/auth/me", headers=_bearer(old_access))
    assert r.status_code == 200, r.text

    # 改密
    r = admin_client.post(
        "/api/admin/auth/change-password",
        json={"old_password": "old-password-123", "new_password": "new-password-456"},
        headers=_bearer(old_access),
    )
    assert r.status_code == 200, r.text

    db.refresh(admin)
    assert admin.token_version == old_tv + 1

    # 改密后旧 token 必须 401（新鲜度检查：tv 对不上）
    r = admin_client.get("/api/admin/auth/me", headers=_bearer(old_access))
    assert r.status_code == 401, r.text

    # 新签发的 token（带新 tv）有效
    new_access = create_access_token(admin.id, {"username": admin.username, "staff": True},
                                     token_version=admin.token_version)
    r = admin_client.get("/api/admin/auth/me", headers=_bearer(new_access))
    assert r.status_code == 200, r.text


def test_change_password_wrong_old_password_no_tv_bump(db, admin_client):
    admin = _admin(db)
    old_tv = admin.token_version or 0
    access = create_access_token(admin.id, {"username": admin.username, "staff": True},
                                 token_version=old_tv)

    r = admin_client.post(
        "/api/admin/auth/change-password",
        json={"old_password": "wrong-password", "new_password": "new-password-456"},
        headers=_bearer(access),
    )
    assert r.status_code == 400, r.text
    db.refresh(admin)
    assert (admin.token_version or 0) == old_tv  # 失败时版本号不动


# ===========================================================================
# 2. 登出后旧 access token 必须失效（jti 吊销）
# ===========================================================================

def test_logout_revokes_access_token(db, admin_client):
    admin = _admin(db)
    access = create_access_token(admin.id, {"username": admin.username, "staff": True},
                                 token_version=admin.token_version or 0)

    r = admin_client.get("/api/admin/auth/me", headers=_bearer(access))
    assert r.status_code == 200, r.text

    # 登出
    r = admin_client.post("/api/admin/auth/logout", json={}, headers=_bearer(access))
    assert r.status_code == 200, r.text

    # 登出后同一 token 必须 401（jti 已吊销）
    r = admin_client.get("/api/admin/auth/me", headers=_bearer(access))
    assert r.status_code == 401, r.text


def test_logout_revokes_refresh_token_too(db, admin_client):
    admin = _admin(db)
    tv = admin.token_version or 0
    access = create_access_token(admin.id, {"username": admin.username, "staff": True},
                                 token_version=tv)
    refresh = create_refresh_token(admin.id, token_version=tv)

    # 登出时附带 refresh_token
    r = admin_client.post("/api/admin/auth/logout", json={"refresh_token": refresh},
                          headers=_bearer(access))
    assert r.status_code == 200, r.text

    # 被吊销的 refresh 再去换 token 必须失败
    from backend.security import is_jti_revoked
    payload = decode_token(refresh, expected_type="refresh")
    assert payload is not None
    assert is_jti_revoked(db, payload.get("jti"))


def test_logout_cannot_revoke_other_users_refresh(db, admin_client):
    admin = _admin(db)
    other = _admin(db, username="other", password="other-pass-123")
    tv = admin.token_version or 0
    access = create_access_token(admin.id, {"username": admin.username, "staff": True},
                                 token_version=tv)
    other_refresh = create_refresh_token(other.id, token_version=other.token_version or 0)

    # 尝试用别人的 refresh token 去登出：必须被忽略，不能吊销别人的
    r = admin_client.post("/api/admin/auth/logout", json={"refresh_token": other_refresh},
                          headers=_bearer(access))
    assert r.status_code == 200, r.text

    from backend.security import is_jti_revoked
    payload = decode_token(other_refresh, expected_type="refresh")
    assert not is_jti_revoked(db, payload.get("jti"))


def test_logout_without_token_still_clears_local(db, admin_client):
    # 无 token 调登出：鉴权层先 401，这是预期的（前端本地清理不受影响）
    r = admin_client.post("/api/admin/auth/logout", json={})
    assert r.status_code == 401
