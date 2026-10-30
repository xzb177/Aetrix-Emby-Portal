# -*- coding: utf-8 -*-
"""形态 B 配置端点接线回归（#1554/#1555 教训：请求模型字段＋
端点透传＋审计键＋端点回读测试四齐才算接完）：transfer_cross_
cloudpull_* 三键（开关默认关、签名 TTL 默认 43200、轮询超时
默认 3600）须能被 PUT 保存、落请求模型而非默认值、回读一致、
审计明细含三键；越界值 400 且不落库。纯 SystemConfig 键，
不进 env.example。
"""
import json
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
from backend.emby_server import transfer_variant as tv
from backend.integrations import store


@pytest.fixture()
def db():
    # StaticPool + check_same_thread=False：TestClient 在另一个线程里跑
    # app，内存库必须跨线程共享同一个连接（同 aging 端点测试）。
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
        username="cloudpullcfgadmin",
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
    try:
        yield TestClient(app, raise_server_exceptions=False)
    finally:
        app.dependency_overrides.clear()


@pytest.fixture(autouse=True)
def _clean_store_cache():
    """store 缓存是进程全局的：每个用例后全清，避免互相污染。"""
    yield
    store.invalidate()


#: TransferConfigRequest 其余九键的现行默认值（整包 PUT 口径）。
BASE_PAYLOAD = {
    "cross_provider_transfer_enabled": True,
    "transfer_cross_lane_concurrency": 2,
    "transfer_cross_aging_sec": 1800,
    "transfer_fast_lane_concurrency": 8,
    "transfer_per_owner_inflight": 2,
    "pan115_rapid_upload_enabled": True,
    "pan115_multipart_upload_enabled": True,
    "transfer_rebind_waiver_window_days": 7,
    "transfer_rebind_waiver_max_uses": 1,
}

URL = "/api/admin/transfer-config"


def test_put_cloudpull_roundtrips_persists_and_audits(client, db):
    resp = client.put(URL, json={
        **BASE_PAYLOAD,
        "transfer_cross_cloudpull_enabled": True,
        "transfer_cross_cloudpull_ttl_sec": 20000,
        "transfer_cross_cloudpull_timeout_sec": 900,
    })
    assert resp.status_code == 200, resp.text
    body = resp.json()
    # 请求模型字段：缺任何一个都会被 pydantic 默默丢成缺省
    assert body["transfer_cross_cloudpull_enabled"] is True
    assert body["transfer_cross_cloudpull_ttl_sec"] == 20000
    assert body["transfer_cross_cloudpull_timeout_sec"] == 900
    # 端点透传落库：不是默认值
    row = db.query(models.SystemConfig).filter_by(
        key="transfer_cross_cloudpull_enabled").one()
    assert row.value == "true"
    row = db.query(models.SystemConfig).filter_by(
        key="transfer_cross_cloudpull_ttl_sec").one()
    assert row.value == "20000"
    row = db.query(models.SystemConfig).filter_by(
        key="transfer_cross_cloudpull_timeout_sec").one()
    assert row.value == "900"
    # 执行侧读取口径同值
    assert tv.cross_cloudpull_enabled(db) is True
    assert tv.cross_cloudpull_ttl_sec(db) == 20000
    assert tv.cross_cloudpull_timeout_sec(db) == 900
    # 回读端点同值
    got = client.get(URL)
    assert got.status_code == 200
    assert got.json()["transfer_cross_cloudpull_enabled"] is True
    assert got.json()["transfer_cross_cloudpull_ttl_sec"] == 20000
    assert got.json()["transfer_cross_cloudpull_timeout_sec"] == 900
    # 审计明细里带上三键
    log = db.query(models.AdminLog).filter_by(
        action="transfer_update_config").one()
    details = log.details
    if isinstance(details, str):
        details = json.loads(details)
    assert details["transfer_cross_cloudpull_enabled"] is True
    assert details["transfer_cross_cloudpull_ttl_sec"] == 20000
    assert details["transfer_cross_cloudpull_timeout_sec"] == 900


def test_put_without_cloudpull_fields_uses_defaults(client, db):
    resp = client.put(URL, json=BASE_PAYLOAD)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    # 用户亲批：新开关默认关；TTL 默认 43200（12 小时）、超时 3600
    assert body["transfer_cross_cloudpull_enabled"] is False
    assert body["transfer_cross_cloudpull_ttl_sec"] == 43200
    assert body["transfer_cross_cloudpull_timeout_sec"] == 3600
    assert tv.cross_cloudpull_enabled(db) is False
    assert tv.cross_cloudpull_ttl_sec(db) == 43200
    assert tv.cross_cloudpull_timeout_sec(db) == 3600


def test_put_cloudpull_out_of_range_is_400_and_not_persisted(client, db):
    resp = client.put(URL, json={
        **BASE_PAYLOAD, "transfer_cross_cloudpull_ttl_sec": 100})
    assert resp.status_code == 400
    resp = client.put(URL, json={
        **BASE_PAYLOAD, "transfer_cross_cloudpull_ttl_sec": 900000})
    assert resp.status_code == 400
    resp = client.put(URL, json={
        **BASE_PAYLOAD, "transfer_cross_cloudpull_timeout_sec": 10})
    assert resp.status_code == 400
    # 越界不落库，生效值仍是默认
    assert tv.cross_cloudpull_ttl_sec(db) == 43200
    assert tv.cross_cloudpull_timeout_sec(db) == 3600
    got = client.get(URL)
    assert got.status_code == 200
    assert got.json()["transfer_cross_cloudpull_ttl_sec"] == 43200
    assert got.json()["transfer_cross_cloudpull_timeout_sec"] == 3600
