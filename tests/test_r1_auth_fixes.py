"""R1 审计修复回归（360° 审计 R1 切片 1：A-001 / A-003 / A-004）。

A-001：管理员重置用户密码、upsert_admin 重置既有管理员密码时必须 bump
token_version，与自助改密同口径——否则处置盗号的主路径不失效旧会话，
被盗 refresh（30 天）仍可持续换新 access。
A-003：门户登录 / 管理员登录 / Emby AuthenticateByName 三入口在用户
不存在时也要执行一次等价 bcrypt 校验（dummy hash），消除可枚举
用户名的时序差。断言用 verify 调用计数，不测毫秒数。
A-004：门户 / 管理员改密端点的旧密码校验必须与登录同口径限流，
且失败必须落安全日志（LoginLog），否则持被盗 access 可不限速猜旧密码。

全部离线：内存 SQLite + 进程内路由。限流桶按 TestClient IP 隔离，
每个测试用独立 IP，避免进程级限流器跨用例串扰。
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
    verify_password,
)

ROOT = pathlib.Path(__file__).resolve().parents[1]
TEST_SECRET = "test-secret-key-for-r1-auth-fixes-0123456789abcdef"  # secret-scan: allow —— 测试密钥


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


def _user(db, username, password, **kw):  # secret-scan: allow —— 测试用假密码
    u = models.WebUser(username=username, password_hash=hash_password(password),
                       is_active=True, token_version=0, **kw)
    db.add(u)
    db.commit()
    return u


def _client(db, ip, *routers):
    from backend.database import get_db

    app = FastAPI()
    for router in routers:
        app.include_router(router)

    def _get_db():
        yield db

    app.dependency_overrides[get_db] = _get_db
    return TestClient(app, client=(ip, 50000))


def _bearer(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


# ===========================================================================
# A-001：管理员重置密码必须失效旧会话
# ===========================================================================

def test_a001_admin_reset_password_invalidates_victim_sessions(db):
    from backend.api import admin as _admin_api  # noqa: F401 — 注册 /users 路由
    from backend.api import emby_portal
    from backend.api.admin_core import admin_router

    victim = _user(db, "victim", "victim-old-pass-1")
    boss = _user(db, "boss", "boss-pass-12345", is_staff=True, admin_role="super")

    # 组装双路由 app（portal auth + admin）
    client = _client(db, "10.11.0.1", emby_portal.auth_router, admin_router)

    access0 = create_access_token(victim.id, {"username": "victim"}, token_version=0)
    refresh0 = create_refresh_token(victim.id, token_version=0)

    # 重置前旧会话有效
    r = client.get("/api/user/auth/me", headers=_bearer(access0))
    assert r.status_code == 200, r.text

    boss_token = create_access_token(boss.id, {"username": "boss"})
    r = client.post(f"/api/admin/users/{victim.id}/reset-password",
                    json={"new_password": "victim-new-pass-2"}, headers=_bearer(boss_token))
    assert r.status_code == 200, r.text

    db.refresh(victim)
    assert victim.token_version == 1, "重置密码必须 bump token_version"

    # 旧 access 失效
    r = client.get("/api/user/auth/me", headers=_bearer(access0))
    assert r.status_code == 401, r.text
    # 旧 refresh 不能再换新 access
    r = client.post("/api/user/auth/refresh", json={"refresh_token": refresh0})
    assert r.status_code == 401, r.text
    # 新版本号签发的 token 仍可用（只动了版本号，没误伤机制本身）
    access1 = create_access_token(victim.id, {"username": "victim"}, token_version=1)
    r = client.get("/api/user/auth/me", headers=_bearer(access1))
    assert r.status_code == 200, r.text


def test_a001_upsert_admin_reset_bumps_token_version(db, monkeypatch):
    from backend import admin_accounts

    monkeypatch.setattr("backend.emby_server.auth.ensure_emby_credentials",
                        lambda *a, **k: None)
    keeper = _user(db, "keeper", "keeper-pass-123", is_staff=True, admin_role="super")

    user, created, applied = admin_accounts.upsert_admin(db, "keeper", "Brand-New-Pass-123")
    assert not created and applied
    db.refresh(keeper)
    assert keeper.token_version == 1, "upsert 重置既有管理员密码必须 bump token_version"

    # 不改密码的 upsert 不应再动版本号
    admin_accounts.upsert_admin(db, "keeper")
    db.refresh(keeper)
    assert keeper.token_version == 1

    # 新建账号无旧会话，版本号保持 0
    _, created2, _ = admin_accounts.upsert_admin(db, "newbie", "Brand-New-Pass-123")
    assert created2
    newbie = db.query(models.WebUser).filter_by(username="newbie").first()
    assert (newbie.token_version or 0) == 0


# ===========================================================================
# A-003：登录时序——用户不存在也要走一次 verify（计数断言）
# ===========================================================================

class _VerifyCounter:
    def __init__(self, real):
        self.real = real
        self.calls = 0

    def __call__(self, plain, hashed):
        self.calls += 1
        return self.real(plain, hashed)


def test_a003_portal_login_unknown_user_still_verifies(db, monkeypatch):
    from backend.api import emby_portal

    _user(db, "alice", "alice-pass-123")
    counter = _VerifyCounter(verify_password)
    monkeypatch.setattr("backend.api.emby_portal.verify_password", counter)
    client = _client(db, "10.12.0.1", emby_portal.auth_router)

    r = client.post("/api/user/auth/login",
                    json={"username": "ghost", "password": "whatever-1"})
    assert r.status_code == 401, r.text
    assert counter.calls == 1, "用户不存在时也必须执行一次等价 bcrypt 校验"

    counter.calls = 0
    r = client.post("/api/user/auth/login",
                    json={"username": "alice", "password": "wrong-pass-1"})
    assert r.status_code == 401, r.text
    assert counter.calls == 1


def test_a003_admin_login_unknown_user_still_verifies(db, monkeypatch):
    from backend.api import admin_auth  # noqa: F401 — 注册 /auth 路由
    from backend.api.admin_core import admin_router

    _user(db, "boss", "boss-pass-12345", is_staff=True, admin_role="super")
    counter = _VerifyCounter(verify_password)
    monkeypatch.setattr("backend.api.admin_auth.verify_password", counter)
    client = _admin_client(db, "10.12.0.2")

    r = client.post("/api/admin/auth/login",
                    json={"username": "ghost", "password": "whatever-1"})
    assert r.status_code == 401, r.text
    assert counter.calls == 1, "用户不存在时也必须执行一次等价 bcrypt 校验"

    counter.calls = 0
    r = client.post("/api/admin/auth/login",
                    json={"username": "boss", "password": "wrong-pass-1"})
    assert r.status_code == 401, r.text
    assert counter.calls == 1


def _admin_client(db, ip):
    from backend.api.admin_core import admin_router

    return _client(db, ip, admin_router)


def test_a003_emby_authenticate_unknown_user_still_verifies(db, monkeypatch):
    from backend.emby_server import api as emby_api

    u = _user(db, "playeruser", "portal-pass-123")
    u.emby_username = "player1"
    u.emby_password = hash_password("player-pass-123")
    db.commit()

    counter = _VerifyCounter(verify_password)
    # 不存在用户路径走 handler 内对 backend.security.verify_password 的调用；
    # 存在用户路径走 emby_server.auth.verify_emby_password → 其模块内 verify_password。
    monkeypatch.setattr("backend.security.verify_password", counter)
    monkeypatch.setattr("backend.emby_server.auth.verify_password", counter)
    client = _client(db, "10.12.0.3", emby_api.emby_router)

    r = client.post("/Users/AuthenticateByName",
                    json={"Username": "ghost", "Pw": "whatever-1"})
    assert r.status_code == 401, r.text
    assert counter.calls == 1, "用户不存在时也必须执行一次等价 bcrypt 校验"

    counter.calls = 0
    r = client.post("/Users/AuthenticateByName",
                    json={"Username": "player1", "Pw": "wrong-pass-1"})
    assert r.status_code == 401, r.text
    assert counter.calls == 1


# ===========================================================================
# A-004：改密端点旧密码校验限流 + 失败留痕
# ===========================================================================

def test_a004_portal_change_password_rate_limited_and_logged(db):
    from backend.api import emby_portal

    carol = _user(db, "carol", "carol-old-pass-1")
    client = _client(db, "10.13.0.1", emby_portal.auth_router)
    access = create_access_token(carol.id, {"username": "carol"}, token_version=0)

    for i in range(8):
        r = client.post("/api/user/auth/change-password",
                        json={"old_password": "wrong-pass", "new_password": "carol-new-pass-2"},
                        headers=_bearer(access))
        assert r.status_code == 400, (i, r.text)
    r = client.post("/api/user/auth/change-password",
                    json={"old_password": "wrong-pass", "new_password": "carol-new-pass-2"},
                    headers=_bearer(access))
    assert r.status_code == 429, r.text

    rows = (db.query(models.LoginLog)
            .filter(models.LoginLog.reason == "password_change_failed",
                    models.LoginLog.user_id == carol.id).count())
    assert rows == 8, "每次旧密码校验失败都必须落一条安全日志"


def test_a004_portal_change_password_correct_old_still_works(db):
    from backend.api import emby_portal

    dave = _user(db, "dave", "dave-old-pass-1")
    client = _client(db, "10.13.0.2", emby_portal.auth_router)
    access = create_access_token(dave.id, {"username": "dave"}, token_version=0)

    r = client.post("/api/user/auth/change-password",
                    json={"old_password": "dave-old-pass-1", "new_password": "dave-new-pass-2"},
                    headers=_bearer(access))
    assert r.status_code == 200, r.text
    db.refresh(dave)
    assert verify_password("dave-new-pass-2", dave.password_hash)


def test_a004_admin_change_password_rate_limited_and_logged(db):
    from backend.api import admin_auth  # noqa: F401 — 注册 /auth 路由

    boss = _user(db, "boss", "boss-old-pass-12", is_staff=True, admin_role="super")
    client = _admin_client(db, "10.13.0.3")
    access = create_access_token(boss.id, {"username": "boss", "staff": True},
                                 token_version=0)

    for i in range(8):
        r = client.post("/api/admin/auth/change-password",
                        json={"old_password": "wrong-pass", "new_password": "boss-new-pass-34"},
                        headers=_bearer(access))
        assert r.status_code == 400, (i, r.text)
    r = client.post("/api/admin/auth/change-password",
                    json={"old_password": "wrong-pass", "new_password": "boss-new-pass-34"},
                    headers=_bearer(access))
    assert r.status_code == 429, r.text

    rows = (db.query(models.LoginLog)
            .filter(models.LoginLog.reason == "admin_password_change_failed",
                    models.LoginLog.user_id == boss.id).count())
    assert rows == 8, "每次旧密码校验失败都必须落一条安全日志"
