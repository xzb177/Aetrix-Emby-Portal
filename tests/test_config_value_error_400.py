# -*- coding: utf-8 -*-
"""ConfigValueError → 400 统一映射回归测试（perfection-r40）。

背景：config_schema 的约定是「非法值由调用方转成 400」。配置瘦身第 4 批把各管理端
保存接口收敛到 config_service.save_config（值非法时抛 ConfigValueError），但只有
economy/welfare 两个端点各自 try/except 转 400，其余端点会泄漏成 500（全局异常
handler），管理后台拿到的只是「服务器内部错误」。

修复：main.py 注册统一的 ConfigValueError → 400 handler（横切能力只许一套），
各端点不再各自 try/except。本测试用真实 app 做端到端验证：非法值 → 400 +
{"detail": 中文人话}，而不是 500。
"""
import os

os.environ.setdefault("DATABASE_TYPE", "sqlite")
os.environ.setdefault("REDIS_ENABLED", "false")

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend import config_schema, config_service, models
from backend.api.admin_core import get_current_admin
from backend.database import get_db
from backend.integrations import store


@pytest.fixture()
def db():
    # StaticPool + check_same_thread=False：TestClient 在另一个线程里跑 app，
    # 内存库必须跨线程共享同一个连接。
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
        username="cfg400admin",
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
        # raise_server_exceptions=False：让 500 也以响应的形式回来，
        # 便于断言"返回的是 400 而不是 500"。
        yield TestClient(app, raise_server_exceptions=False)
    finally:
        app.dependency_overrides.clear()


@pytest.fixture(autouse=True)
def _clean_store_cache():
    """store 缓存是进程全局的：每个用例后全清，避免互相污染。"""
    yield
    store.invalidate()


def test_save_config_raises_on_out_of_range():
    """统一通道确实会拒绝非法值（抛 ConfigValueError，不写半截）。"""
    with pytest.raises(config_schema.ConfigValueError):
        # welfare_grace_days 的 schema 是 ("int", 0, 3650)
        config_service.save_config(
            None, {"welfare_grace_days": 99999}, schema="welfare")


def test_welfare_invalid_value_returns_400_not_500(client):
    """端到端：非法值 → 400 + detail 中文人话，而不是 500「服务器内部错误」。"""
    r = client.put("/api/admin/welfare/config", json={"welfare_grace_days": 99999})
    assert r.status_code == 400, r.text
    body = r.json()
    assert "detail" in body
    assert "welfare_grace_days" in body["detail"]


def test_welfare_valid_value_still_saves(client, db):
    """合法值不受影响：照常保存并返回 success。"""
    r = client.put("/api/admin/welfare/config", json={"welfare_grace_days": 30})
    assert r.status_code == 200, r.text
    assert r.json()["success"] is True
    row = (
        db.query(models.SystemConfig)
        .filter(models.SystemConfig.key == "welfare_grace_days")
        .first()
    )
    assert row is not None and row.value == "30"


def test_economy_invalid_value_returns_400_not_500(client):
    """economy 家族（曾各自 try/except 的端点）改走统一 handler 后仍是 400。"""
    r = client.put(
        "/api/admin/economy/settings",
        json={"settings": {"invitation_rebate_percent": 101}},  # schema ("int", 0, 100)
    )
    assert r.status_code == 400, r.text
    assert "invitation_rebate_percent" in r.json()["detail"]
