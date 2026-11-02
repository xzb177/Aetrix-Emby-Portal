# -*- coding: utf-8 -*-
"""永久账号白名单管理端 API（/api/admin/permanent-whitelist/*）。

真实 app + 隔离临时 SQLite：鉴权保持真实依赖（无登录 → 401/403），
管理员经 dependency_overrides 注入（同 test_admin_dashboard 口径）。
覆盖：名单列表/添加/移除、按用户名或 ID 搜索、重复添加 409、
不存在用户 400、移除不存在 404、总开关读写且即时影响判定。
"""
import os
import tempfile

os.environ.setdefault("DATABASE_TYPE", "sqlite")
os.environ.setdefault("REDIS_ENABLED", "false")
_fd, _tmppath = tempfile.mkstemp(suffix=".db")
os.close(_fd)
os.environ["DATABASE_URL"] = "sqlite:///" + _tmppath

from backend import database as _dbmod
from sqlalchemy import create_engine as _ce
from sqlalchemy.orm import sessionmaker as _sm

_dbmod.engine = _ce(os.environ["DATABASE_URL"])
_dbmod.configure_session_local(_sm(bind=_dbmod.engine))

import pytest
from fastapi.testclient import TestClient

from backend import models
from backend.database import SessionLocal, get_db, init_db

init_db()

BASE = "/api/admin/permanent-whitelist"


@pytest.fixture()
def db():
    s = SessionLocal()
    try:
        s.query(models.PermanentWhitelistEntry).delete()
        s.query(models.SystemConfig).filter(
            models.SystemConfig.key == "permanent_whitelist_enabled").delete()
        s.query(models.WebUser).delete()
        s.commit()
        yield s
    finally:
        s.close()


@pytest.fixture()
def admin(db):
    u = models.WebUser(username="pwadmin", is_staff=True, is_active=True,
                       password_hash="test")
    db.add(u)
    db.commit()
    db.refresh(u)
    return u


@pytest.fixture()
def member(db):
    u = models.WebUser(username="pwmember", is_staff=False, is_active=True,
                       password_hash="test")
    db.add(u)
    db.commit()
    db.refresh(u)
    return u


@pytest.fixture()
def client():
    from backend.main import app
    app.dependency_overrides[get_db] = lambda: SessionLocal()
    try:
        yield TestClient(app, raise_server_exceptions=False)
    finally:
        app.dependency_overrides.clear()


@pytest.fixture()
def admin_client(client, admin):
    from backend.api.admin_core import get_current_admin
    from backend.main import app
    app.dependency_overrides[get_current_admin] = lambda: admin
    try:
        yield client
    finally:
        del app.dependency_overrides[get_current_admin]


def test_entries_require_auth(client, member):
    r = client.get(f"{BASE}/entries")
    assert r.status_code in (401, 403)
    r = client.post(f"{BASE}/entries", json={"user_id": member.id})
    assert r.status_code in (401, 403)


def test_add_list_search_remove_flow(admin_client, admin, member):
    # 搜索：按用户名命中、按 ID 命中
    r = admin_client.get(f"{BASE}/search", params={"q": "pwmem"})
    assert r.status_code == 200
    hits = r.json()["users"]
    assert [h["id"] for h in hits] == [member.id]
    r = admin_client.get(f"{BASE}/search", params={"q": str(member.id)})
    assert [h["id"] for h in r.json()["users"]] == [member.id]

    # 添加
    r = admin_client.post(f"{BASE}/entries",
                          json={"user_id": member.id, "note": "站主小号"})
    assert r.status_code == 200
    entry = r.json()["entry"]
    assert entry["user_id"] == member.id
    assert entry["username"] == "pwmember"
    assert entry["note"] == "站主小号"
    assert entry["added_by"] == admin.id

    # 重复添加 → 409
    r = admin_client.post(f"{BASE}/entries", json={"user_id": member.id})
    assert r.status_code == 409

    # 列表能看到
    r = admin_client.get(f"{BASE}/entries")
    assert r.status_code == 200
    body = r.json()
    assert body["enabled"] is True
    assert [e["user_id"] for e in body["entries"]] == [member.id]

    # 判定即时生效
    from backend import permanent_whitelist as pw
    s = SessionLocal()
    try:
        assert pw.is_permanent(s, member.id) is True
    finally:
        s.close()

    # 移除 → 再移除 404
    r = admin_client.delete(f"{BASE}/entries/{member.id}")
    assert r.status_code == 200
    r = admin_client.delete(f"{BASE}/entries/{member.id}")
    assert r.status_code == 404


def test_add_missing_user_400(admin_client):
    r = admin_client.post(f"{BASE}/entries", json={"user_id": 999999})
    assert r.status_code == 400


def test_config_toggle(admin_client, member):
    r = admin_client.get(f"{BASE}/config")
    assert r.status_code == 200
    assert r.json()["enabled"] is True  # 默认开
    r = admin_client.put(f"{BASE}/config", json={"enabled": False})
    assert r.status_code == 200
    assert r.json()["enabled"] is False
    r = admin_client.get(f"{BASE}/config")
    assert r.json()["enabled"] is False
