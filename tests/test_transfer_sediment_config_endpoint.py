# -*- coding: utf-8 -*-
"""管理端转存配置端点的指纹沉淀字段透传回归测试（四齐：注册＋
字段＋校验＋回读）。

锁端点级往返：PUT 带沉淀新值 → 响应/回读/落库均为新值；不带字段
→ 默认（总开关开、需求/追剧档开、热门/新入库档关、日预算 200
GiB）；越界值 → 400 且不落库。
"""
import os

os.environ.setdefault("DATABASE_TYPE", "sqlite")
os.environ.setdefault("REDIS_ENABLED", "false")

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend import models
from backend.api.admin_core import get_current_admin
from backend.database import get_db
from backend.integrations import store


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
        session.close()
        engine.dispose()


@pytest.fixture()
def admin_user(db):
    user = models.WebUser(
        username="sedimentcfgadmin",
        password_hash="x",
        is_active=True,
        is_staff=True,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


@pytest.fixture()
def client(db, admin_user):
    from backend.main import app

    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_current_admin] = lambda: admin_user
    store.invalidate()
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.pop(get_db, None)
        app.dependency_overrides.pop(get_current_admin, None)
        store.invalidate()


def _full_payload(**overrides):
    payload = {
        "cross_provider_transfer_enabled": True,
        "transfer_cross_lane_concurrency": 2,
        "transfer_cross_aging_sec": 1800,
        "transfer_fast_lane_concurrency": 8,
        "transfer_per_owner_inflight": 2,
        "pan115_rapid_upload_enabled": True,
        "pan115_multipart_upload_enabled": True,
        "transfer_rebind_waiver_window_days": 7,
        "transfer_rebind_waiver_max_uses": 1,
        "transfer_cross_cloudpull_enabled": False,
        "transfer_cross_cloudpull_ttl_sec": 43200,
        "transfer_cross_cloudpull_timeout_sec": 3600,
        "transfer_manifest_accounting_enabled": True,
        "transfer_manifest_takeover_enabled": True,
        "quark_play_enabled": True,
        "transfer_paused_categories": "[]",
    }
    payload.update(overrides)
    return payload


def test_sediment_defaults_present_on_get(client):
    resp = client.get("/api/admin/transfer-config")
    assert resp.status_code == 200
    data = resp.json()
    assert data["transfer_sediment_enabled"] is True
    assert data["transfer_sediment_tier_demand_enabled"] is True
    assert data["transfer_sediment_tier_watching_enabled"] is True
    assert data["transfer_sediment_tier_hot_enabled"] is False
    assert data["transfer_sediment_tier_new_enabled"] is False
    assert data["transfer_sediment_daily_budget_gib"] == 200
    assert data["transfer_sediment_fused"] is False


def test_sediment_put_roundtrip(client, db):
    resp = client.put("/api/admin/transfer-config", json=_full_payload(
        transfer_sediment_enabled=False,
        transfer_sediment_tier_watching_enabled=False,
        transfer_sediment_daily_budget_gib=50,
    ))
    assert resp.status_code == 200
    data = resp.json()
    assert data["transfer_sediment_enabled"] is False
    assert data["transfer_sediment_tier_watching_enabled"] is False
    assert data["transfer_sediment_daily_budget_gib"] == 50

    resp = client.get("/api/admin/transfer-config")
    data = resp.json()
    assert data["transfer_sediment_enabled"] is False
    assert data["transfer_sediment_daily_budget_gib"] == 50

    row = (db.query(models.SystemConfig)
           .filter(models.SystemConfig.key
                   == "transfer_sediment_daily_budget_gib").first())
    assert row is not None and row.value == "50"


def test_sediment_put_out_of_range_400_and_not_saved(client, db):
    resp = client.put("/api/admin/transfer-config", json=_full_payload(
        transfer_sediment_daily_budget_gib=0,
    ))
    assert resp.status_code == 400
    row = (db.query(models.SystemConfig)
           .filter(models.SystemConfig.key
                   == "transfer_sediment_daily_budget_gib").first())
    assert row is None
