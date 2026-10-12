"""P0-3 回归测试：更换测试目标时不得把已保存的 API 密钥发给任意新主机。

背景：test_server / save_server 中 saved_key 无条件发给 request.url 指定的
任意主机。external 模式省略密钥测试其他主机时，原服务 API 密钥被直接外泄。

修复：只在目标地址与已保存地址一致时才允许复用旧密钥；换地址必须显式提供新密钥。

全部离线：_same_url 纯函数测试 + 端点级 mock probe 测试，不发起真实网络请求。
"""
from __future__ import annotations

import os

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend import models
from backend.emby_server import models as em
from backend.security import create_access_token, hash_password

TEST_SECRET = "test-only-secret-key-0123456789abcdef"


@pytest.fixture(autouse=True)
def _secret_key(monkeypatch):
    if not (os.environ.get("SECRET_KEY") or "").strip():
        monkeypatch.setenv("SECRET_KEY", TEST_SECRET)


# ===========================================================================
# _same_url 纯函数测试
# ===========================================================================

def test_same_url_identical():
    from backend.api.emby_servers import _same_url
    assert _same_url("http://emby.local:8096", "http://emby.local:8096")


def test_same_url_trailing_slash_and_case():
    from backend.api.emby_servers import _same_url
    assert _same_url("http://Emby.Local:8096/", "http://emby.local:8096")
    assert _same_url("https://emby.local/", "https://emby.local")


def test_same_url_default_port_folded():
    from backend.api.emby_servers import _same_url
    assert _same_url("http://emby.local:80", "http://emby.local")
    assert _same_url("https://emby.local:443", "https://emby.local")


def test_same_url_different_host():
    from backend.api.emby_servers import _same_url
    assert not _same_url("http://emby.local:8096", "http://evil.example.com:8096")
    assert not _same_url("http://emby.local:8096", "http://emby.local:8097")


def test_same_url_different_scheme():
    from backend.api.emby_servers import _same_url
    assert not _same_url("http://emby.local", "https://emby.local")


def test_same_url_invalid_fail_closed():
    from backend.api.emby_servers import _same_url
    # 解析失败 → 判为不同（fail-closed，不复用密钥）
    assert not _same_url("http://emby.local:8096", "")
    assert not _same_url("", "")
    assert not _same_url("not-a-url", "http://emby.local")


# ===========================================================================
# 端点级测试：test_server 不得把旧密钥发给新主机
# ===========================================================================

@pytest.fixture()
def db():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False},
                           poolclass=StaticPool)
    models.Base.metadata.create_all(engine)
    em.Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    yield session
    session.close()


def _admin(db):
    u = models.WebUser(username="boss", password_hash=hash_password("x"),
                       is_staff=True, is_active=True, admin_role="super",
                       token_version=0)
    db.add(u)
    db.commit()
    return u


@pytest.fixture()
def server_client(db, monkeypatch):
    from backend.api import emby_servers
    from backend.api.admin_core import admin_router
    from backend.database import get_db

    # mock probe：记录收到的 api_key，不发起真实请求
    seen = {}

    async def fake_probe(mode, url, api_key=""):
        seen["mode"] = mode
        seen["url"] = url
        seen["api_key"] = api_key
        return {"ok": True}

    monkeypatch.setattr(emby_servers, "probe", fake_probe)

    # mock 配置读写：用内存字典代替 DB（run_in_threadpool 调的是同步函数）
    config = {"emby_external_url": "http://emby.local:8096",
              "emby_external_api_key": "SECRET-SAVED-KEY"}

    def fake_value(db_, key, default="", realm_id=None):
        return config.get(key, default)

    def fake_save_value(db_, key, new_value, description, realm_id=None):
        config[key] = new_value

    monkeypatch.setattr(emby_servers, "value", fake_value)
    monkeypatch.setattr(emby_servers, "save_value", fake_save_value)

    # realms.active_realm_id 下放线程池执行，mock 为同步函数
    import backend.api.emby_servers as es_mod
    monkeypatch.setattr(es_mod.realms, "active_realm_id", lambda db_: 1)

    app = FastAPI()
    app.include_router(admin_router)
    # emby_servers 的 router 也要挂载（test_server 在上面）
    app.include_router(emby_servers.router)

    def _get_db():
        yield db

    app.dependency_overrides[get_db] = _get_db
    with TestClient(app) as c:
        yield c, seen, config


def _bearer(user) -> dict:
    token = create_access_token(user.id, {"username": user.username, "staff": True},
                                token_version=user.token_version or 0)
    return {"Authorization": f"Bearer {token}"}


def test_test_server_same_url_reuses_saved_key(db, server_client):
    admin = _admin(db)
    client, seen, _ = server_client

    r = client.post("/api/admin/emby/servers/test",
                    json={"mode": "external", "url": "http://emby.local:8096",
                          "api_key": "", "enabled": False},
                    headers=_bearer(admin))
    assert r.status_code == 200, r.text
    # 同地址：允许复用旧密钥
    assert seen["api_key"] == "SECRET-SAVED-KEY"


def test_test_server_different_url_requires_new_key(db, server_client):
    admin = _admin(db)
    client, seen, _ = server_client

    # 换地址但不给新密钥：必须 400，不能把旧密钥发给新主机
    r = client.post("/api/admin/emby/servers/test",
                    json={"mode": "external", "url": "http://evil.example.com:8096",
                          "api_key": "", "enabled": False},
                    headers=_bearer(admin))
    assert r.status_code == 400, r.text
    assert "API 密钥" in r.text
    # probe 根本没被调用（没外泄）
    assert "api_key" not in seen


def test_test_server_different_url_with_new_key_ok(db, server_client):
    admin = _admin(db)
    client, seen, _ = server_client

    r = client.post("/api/admin/emby/servers/test",
                    json={"mode": "external", "url": "http://evil.example.com:8096",
                          "api_key": "NEW-KEY-FOR-NEW-HOST", "enabled": False},
                    headers=_bearer(admin))
    assert r.status_code == 200, r.text
    # 用的是新密钥，不是旧密钥
    assert seen["api_key"] == "NEW-KEY-FOR-NEW-HOST"
    assert seen["url"] == "http://evil.example.com:8096"


# ===========================================================================
# P1-3 / P1-4 回归测试：save_server 先校验后落库；managed_ea 不碰外部密钥
# ===========================================================================

def test_save_server_managed_ea_never_touches_external_key(db, server_client, monkeypatch):
    """P1-4：managed_ea 换地址不给密钥时，不得复用 emby_external_api_key 探测。"""
    from unittest.mock import AsyncMock

    from backend.api import emby_servers

    client, seen, config = server_client
    admin = _admin(db)
    # save_server 在 managed_ea 探测成功后会做 EA 挂载体检（真实 HTTP），mock 掉
    monkeypatch.setattr(emby_servers, "refresh_mount_health",
                        AsyncMock(return_value={"ok": True}))
    # registry.upsert_legacy 真写 DB，用空实现代替（只验证密钥链路）
    monkeypatch.setattr(emby_servers.registry, "upsert_legacy", lambda *a, **k: None)

    r = client.put("/api/admin/emby/servers",
                   json={"mode": "managed_ea", "url": "http://ea-new.local:8096",
                         "api_key": "", "enabled": False},
                   headers=_bearer(admin))
    assert r.status_code == 200, r.text
    # 外部密钥没有进入探测链路；managed_ea 探测本来就不需要密钥
    assert seen["mode"] == "managed_ea"
    assert seen["api_key"] == "", f"外部密钥被复用给了 managed_ea 新地址: {seen}"


def test_save_server_external_new_url_without_key_rejected_before_persist(db, server_client):
    """P1-3：external 换地址不给新密钥 → 400 必须发生在落库之前，新地址不得写入。"""
    client, seen, config = server_client
    admin = _admin(db)

    r = client.put("/api/admin/emby/servers",
                   json={"mode": "external", "url": "http://evil.example.com:8096",
                         "api_key": "", "enabled": False},
                   headers=_bearer(admin))
    assert r.status_code == 400, r.text
    assert "API 密钥" in r.text
    # probe 根本没被调用（没外泄）
    assert "api_key" not in seen
    # P1-3：新地址没有被落库
    assert config["emby_external_url"] == "http://emby.local:8096"


def test_save_server_external_new_url_with_new_key_persists(db, server_client):
    """给了新密钥时正常落库：探测用新密钥，新地址写入配置。"""
    client, seen, config = server_client
    admin = _admin(db)

    r = client.put("/api/admin/emby/servers",
                   json={"mode": "external", "url": "http://new-emby.local:8096",
                         "api_key": "BRAND-NEW-KEY", "enabled": False},
                   headers=_bearer(admin))
    assert r.status_code == 200, r.text
    assert seen["api_key"] == "BRAND-NEW-KEY"
    assert seen["url"] == "http://new-emby.local:8096"
    assert config["emby_external_url"] == "http://new-emby.local:8096"
    assert config["emby_external_api_key"] == "BRAND-NEW-KEY"
