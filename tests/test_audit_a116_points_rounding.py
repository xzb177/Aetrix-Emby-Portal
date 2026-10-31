"""A-116 回归：能量价/充值换算三处 int() 截断改四舍五入。

旧口径（int 向零截断）与同文件折扣口径（ROUND_HALF_UP）分裂：
- 套餐价 99.9：手动购买与自动续订均应收 100 点（旧：99 点）
- 自定义充值 3 元 x 1.2：应得 4 点（旧：3 点）
- 自定义充值 7 元 x 1.2：新旧同为 8 点（无变化对照）
"""
import os

os.environ.setdefault("DATABASE_TYPE", "sqlite")
os.environ.setdefault("REDIS_ENABLED", "false")

from decimal import Decimal

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend import models
from backend import subscriptions
from backend.api import economy
from backend.api.user import get_current_user
from backend.database import get_db


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


def _make_user(db, username="u1"):
    u = models.WebUser(username=username, password_hash="", points=100000,
                       is_active=True, member_xp=0, member_level=1)
    db.add(u)
    db.commit()
    return u


def _make_plan(db, points_price):
    p = models.SubscriptionPlan(
        name="P", price=Decimal("0"), points_price=Decimal(str(points_price)),
        duration_days=30, realm_id=None, is_active=True)
    db.add(p)
    db.commit()
    return p


def _set_cfg(db, key, value):
    cfg = db.query(models.SystemConfig).filter(models.SystemConfig.key == key).first()
    if cfg:
        cfg.value = str(value)
    else:
        db.add(models.SystemConfig(key=key, value=str(value)))
    db.commit()


def _user_app(db, user):
    app = FastAPI()
    app.include_router(economy.router)
    app.dependency_overrides[get_current_user] = lambda: user
    app.dependency_overrides[get_db] = lambda: db
    return TestClient(app)


def test_manual_purchase_rounds_half_up(db):
    u = _make_user(db)
    plan = _make_plan(db, "99.9")
    res = economy._points_subscription_price(db, u, plan)
    assert res["list_points"] == 100


def test_auto_renew_rounds_half_up(db):
    u = _make_user(db)
    plan = _make_plan(db, "99.9")
    res = subscriptions._auto_renew_cost(db, u, plan)
    assert res["list_points"] == 100
    assert res["paid_points"] == 100


def _recharge_points(db, user, amount):
    _set_cfg(db, "payment_gateway_url", "https://pay.example.com")
    _set_cfg(db, "payment_partner_id", "10001")
    _set_cfg(db, "payment_partner_key", "testkey")
    _set_cfg(db, "site_url", "https://app.example.com")
    resp = _user_app(db, user).post(
        "/api/user/economy/payment/order",
        json={"kind": "recharge", "custom_amount": amount})
    assert resp.status_code == 200, resp.text
    order = db.query(models.RechargeOrder).order_by(
        models.RechargeOrder.id.desc()).first()
    return order.amount


def test_custom_recharge_3_yuan_ratio_1_2(db):
    u = _make_user(db)
    assert _recharge_points(db, u, 3.0) == 4


def test_custom_recharge_7_yuan_ratio_1_2_unchanged(db):
    u = _make_user(db)
    assert _recharge_points(db, u, 7.0) == 8
