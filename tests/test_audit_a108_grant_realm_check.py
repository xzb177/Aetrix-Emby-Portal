# -*- coding: utf-8 -*-
"""审计 A-108：管理员直接发放订阅不校验显式 realm_id。

缺口：POST /api/admin/users/{id}/subscriptions 的 grant_subscription 取
``realm_id = request.realm_id or plan.realm_id or realms.active_realm_id(db)``
后直接 _grant_subscription 落库，全程无 get_realm 校验；同文件
economy_create_plan / economy_update_plan 对 realm_id 均有「不存在即
400」的同款校验，发放端点是孤例。

后果：显式传不存在的服 id 时落出 realm_id 为野 id 的订阅行——用户服
卡片只遍历真实服故看不见它，EA 按服过滤的门禁不认它，EM 不过滤的
门禁反而认它，同一份会员在两网关表现倒挂。

本文件钉住：显式传不存在的 realm_id → 400，且无订阅行落库。
"""
import os

os.environ.setdefault("DATABASE_TYPE", "sqlite")
os.environ.setdefault("REDIS_ENABLED", "false")

from decimal import Decimal
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend import models


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
        session.rollback()
        session.close()
        engine.dispose()


def _admin_app(db):
    from backend.api.admin import admin_router
    from backend.api.admin_core import get_current_admin
    from backend.database import get_db
    app = FastAPI()
    app.include_router(admin_router)
    app.dependency_overrides[get_current_admin] = lambda: SimpleNamespace(id=999)
    app.dependency_overrides[get_db] = lambda: db
    return TestClient(app)


def test_admin_grant_rejects_nonexistent_realm(db):
    """显式传不存在的 realm_id：400，且不落任何订阅行。"""
    from backend.api import admin as admin_mod  # noqa: F401 —— 确保路由已注册
    u = models.WebUser(username="a108_user", password_hash="",
                       points=0, is_active=True)
    db.add(u)
    realm = models.ServerRealm(name="测试服", slug="a108", is_active=True)
    db.add(realm)
    db.commit()
    plan = models.SubscriptionPlan(
        name="月卡", price=Decimal("100"), duration_days=30,
        is_active=True, realm_id=realm.id)
    db.add(plan)
    db.commit()

    resp = _admin_app(db).post(
        f"/api/admin/users/{u.id}/subscriptions",
        json={"plan_id": plan.id, "duration_days": 30,
              "realm_id": realm.id + 9999})
    assert resp.status_code == 400, resp.text
    rows = db.query(models.UserSubscription).filter(
        models.UserSubscription.user_id == u.id).all()
    assert rows == []
