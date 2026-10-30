# -*- coding: utf-8 -*-
"""管理端转存配置 PUT 端点 transfer_cross_aging_sec 透传回归测试。

背景：PR-B（#1553）把 transfer_cross_aging_sec 注册进 config_schema，
transfer_variant 的 transfer_config_payload / write_transfer_config 也都
接好了，但 backend/api/admin_economy.py 的 TransferConfigRequest 没有
这个字段、transfer_update_config 也没透传——管理员在后台（#1554 的新
字段）改值点保存会被 Pydantic 静默忽略：GET 能看、PUT 存不进。

本测试锁端点级往返：PUT 带新值 → 响应/回读/落库/审计四处均为新值；
不带该字段 → 默认 1800；越界值 → 400 且不落库。
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
    # app，内存库必须跨线程共享同一个连接（同 test_config_value_error_400）。
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
        username="agingcfgadmin",
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


#: TransferConfigRequest 其余八键的现行默认值（整包 PUT 口径）。
BASE_PAYLOAD = {
    "cross_provider_transfer_enabled": True,
    "transfer_cross_lane_concurrency": 2,
    "transfer_fast_lane_concurrency": 8,
    "transfer_per_owner_inflight": 2,
    "pan115_rapid_upload_enabled": True,
    "pan115_multipart_upload_enabled": True,
    "transfer_rebind_waiver_window_days": 7,
    "transfer_rebind_waiver_max_uses": 1,
}

URL = "/api/admin/transfer-config"


def test_put_aging_sec_roundtrips_persists_and_audits(client, db):
    resp = client.put(
        URL, json={**BASE_PAYLOAD, "transfer_cross_aging_sec": 900})
    assert resp.status_code == 200
    assert resp.json()["transfer_cross_aging_sec"] == 900
    # 落库：SystemConfig 行与生效值读取口径一致
    row = db.query(models.SystemConfig).filter_by(
        key="transfer_cross_aging_sec").one()
    assert row.value == "900"
    assert tv.cross_aging_sec(db) == 900
    # 回读端点同值
    got = client.get(URL)
    assert got.status_code == 200
    assert got.json()["transfer_cross_aging_sec"] == 900
    # 审计明细里带上新键
    log = db.query(models.AdminLog).filter_by(
        action="transfer_update_config").one()
    details = log.details
    if isinstance(details, str):
        details = json.loads(details)
    assert details["transfer_cross_aging_sec"] == 900


def test_put_without_aging_field_defaults_to_1800(client, db):
    resp = client.put(URL, json=BASE_PAYLOAD)
    assert resp.status_code == 200
    assert resp.json()["transfer_cross_aging_sec"] == 1800
    assert tv.cross_aging_sec(db) == 1800


def test_put_aging_sec_out_of_range_is_400_and_not_persisted(client, db):
    resp = client.put(
        URL, json={**BASE_PAYLOAD, "transfer_cross_aging_sec": 10})
    assert resp.status_code == 400
    assert tv.cross_aging_sec(db) == 1800
    got = client.get(URL)
    assert got.status_code == 200
    assert got.json()["transfer_cross_aging_sec"] == 1800
