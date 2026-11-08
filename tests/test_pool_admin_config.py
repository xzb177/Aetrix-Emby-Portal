# -*- coding: utf-8 -*-
"""公共贡献池管理端配置端点（PR-1 地基）：GET/PUT /api/admin/pool/config。

- 读：无存量行时回 16 键代码默认值（补充节修正值）；
- 写：走 config_service 家族 pool 整批校验，非法整批 400 且不落库；
- 权限：canWrite 口径由 get_current_admin 集中判定——只读（viewer）
  角色对本端点的写请求必须被 admin_roles 集中判定拦下（端点只许用
  get_current_admin 依赖，不许自造鉴权）。
"""
from __future__ import annotations

import os

os.environ.setdefault("DATABASE_TYPE", "sqlite")
os.environ.setdefault("REDIS_ENABLED", "false")

import types

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend import admin_roles, models
from backend.api.admin_core import get_current_admin
from backend.database import get_db
from backend.integrations import store

URL = "/api/admin/pool/config"
GIB = 1024 ** 3


@pytest.fixture()
def db():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False},
        poolclass=StaticPool)
    models.Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


@pytest.fixture()
def admin_user(db):
    user = models.WebUser(
        username="poolcfgadmin", password_hash="x",
        is_active=True, is_staff=True, admin_role="super")
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


@pytest.fixture()
def client(db, admin_user):
    from backend.main import app

    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_current_admin] = lambda: admin_user
    try:
        yield TestClient(app, raise_server_exceptions=False)
    finally:
        app.dependency_overrides.clear()


@pytest.fixture(autouse=True)
def _clean_store_cache():
    yield
    store.invalidate()


def test_get_config_returns_defaults(client):
    resp = client.get(URL)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["pool_enabled"] is False
    assert body["pool_audience"] == "all"
    assert body["pool_join_mode"] == "apply_review"
    assert body["pool_enabled_providers"] == "drive"
    assert body["pool_member_capacity_bytes"] == 4_500_000_000_000
    assert body["pool_member_reserve_free_bytes"] == 300 * GIB
    assert body["pool_member_daily_transfer_bytes"] == 600 * GIB
    assert body["pool_member_max_bindings_per_user"] == 10
    # 闲置天数默认 30 → 7（用户 2026-11-09 亲点：30 天太久）。
    assert body["pool_copy_idle_evict_days"] == 7
    assert len(body) == 16


def test_put_config_roundtrips(client, db):
    resp = client.put(URL, json={
        "pool_enabled": True,
        "pool_audience": "whitelist",
        "pool_item_max_copies": 3,
        "pool_member_max_bindings_per_user": 8,
    })
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["pool_enabled"] is True
    assert body["pool_audience"] == "whitelist"
    assert body["pool_item_max_copies"] == 3
    assert body["pool_member_max_bindings_per_user"] == 8
    row = db.query(models.SystemConfig).filter_by(
        key="pool_member_max_bindings_per_user").one()
    assert row.value == "8"
    got = client.get(URL)
    assert got.json()["pool_audience"] == "whitelist"
    # 审计留痕
    log = db.query(models.AdminLog).filter_by(
        action="pool_update_config").one()
    assert log is not None


def test_put_config_invalid_batch_400_and_nothing_persisted(client, db):
    resp = client.put(URL, json={
        "pool_item_max_copies": 5,
        "pool_audience": "nobody",
    })
    assert resp.status_code == 400, resp.text
    assert db.query(models.SystemConfig).filter_by(
        key="pool_item_max_copies").first() is None
    got = client.get(URL)
    assert got.json()["pool_item_max_copies"] == 2


JANITOR_URL = "/api/admin/pool/janitor-config"


def test_get_janitor_config_returns_default_on(client):
    resp = client.get(JANITOR_URL)
    assert resp.status_code == 200, resp.text
    # 闲置主动清理默认开（用户 2026-11-09 亲点）。
    assert resp.json() == {"pool_idle_evict_proactive_enabled": True}


def test_put_janitor_config_roundtrips(client, db):
    resp = client.put(JANITOR_URL, json={
        "pool_idle_evict_proactive_enabled": False})
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"pool_idle_evict_proactive_enabled": False}
    row = db.query(models.SystemConfig).filter_by(
        key="pool_idle_evict_proactive_enabled").one()
    assert row.value == "false"
    got = client.get(JANITOR_URL)
    assert got.json()["pool_idle_evict_proactive_enabled"] is False
    # 审计留痕
    log = db.query(models.AdminLog).filter_by(
        action="pool_update_janitor_config").one()
    assert log is not None


def test_viewer_role_cannot_write_pool_config():
    """canWrite 集中判定：viewer 对 PUT /api/admin/pool/config 被拦，
    operator/super 放行（读不受影响）。"""
    viewer = types.SimpleNamespace(admin_role="viewer", is_staff=True)
    operator = types.SimpleNamespace(admin_role="operator", is_staff=True)
    request = types.SimpleNamespace(
        url=types.SimpleNamespace(path=URL), method="PUT")
    assert admin_roles.blocked_reason(request, viewer) is not None
    assert admin_roles.blocked_reason(request, operator) is None
    get_request = types.SimpleNamespace(
        url=types.SimpleNamespace(path=URL), method="GET")
    assert admin_roles.blocked_reason(get_request, viewer) is None
