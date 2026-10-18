"""r48 后端完美迭代：订阅域 3 项修复。

F1（Bug A）：兑换码折扣权益分支从原价重算，静默丢弃会员等级折扣，
    且响应仍返回非零 member_discount_pct/amount（误报）。
    修法：与优惠券分支同口径——先会员折扣得中间价，再对中间价算权益（叠加）。
F2（Bug B）：coupon/quote 不考虑自动兑换码折扣权益，预览价高于实付价。
    修法：code 可空；空码 + subscription 时按「自动最优权益」口径试算，与下单一致。
F3（BUG-1）：订阅到期提醒去重键（订阅行 × 档位）永不重置——续费顺延复用同一行，
    下一周期 7d/3d/1d/expired 全被旧记录挡住，再也收不到提醒。
    修法：_grant_subscription 顺延同一行时清掉该订阅的 subscription_reminders 行。
"""
import os

os.environ.setdefault("DATABASE_TYPE", "sqlite")
os.environ.setdefault("REDIS_ENABLED", "false")

from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend import models
from backend.api import economy
from backend.api.economy import _grant_subscription
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


def _make_user(db, username="u1", member_level=3):
    u = models.WebUser(username=username, password_hash="", points=1000,
                       is_active=True, member_xp=0, member_level=member_level)
    db.add(u)
    db.commit()
    return u


def _make_level(db, level=3, discount_pct=5):
    lv = models.MemberLevel(level=level, name=f"Lv{level}",
                            xp_threshold=500, discount_pct=discount_pct,
                            is_active=True)
    db.add(lv)
    db.commit()
    return lv


def _make_plan(db, price=100, days=30):
    p = models.SubscriptionPlan(name="远行执照", price=price,
                                duration_days=days, is_active=True)
    db.add(p)
    db.commit()
    return p


def _make_credit(db, user_id, discount_pct=80):
    c = models.ExchangeDiscountCredit(
        user_id=user_id, discount_pct=discount_pct, status="unused")
    db.add(c)
    db.commit()
    return c


def _set_cfg(db, key, value):
    cfg = db.query(models.SystemConfig).filter(models.SystemConfig.key == key).first()
    if cfg:
        cfg.value = str(value)
    else:
        db.add(models.SystemConfig(key=key, value=str(value)))
    db.commit()


def _gateway_cfg(db):
    _set_cfg(db, "payment_gateway_url", "https://pay.example.com")
    _set_cfg(db, "payment_partner_id", "10001")
    _set_cfg(db, "payment_partner_key", "testkey")
    _set_cfg(db, "site_url", "https://app.example.com")


def _user_app(db, user):
    app = FastAPI()
    app.include_router(economy.router)
    app.dependency_overrides[get_current_user] = lambda: user
    app.dependency_overrides[get_db] = lambda: db
    return TestClient(app)


# ---------------- F1：兑换码权益与会员折扣叠加 ----------------

def test_credit_stacks_with_member_discount(db):
    u = _make_user(db)
    _make_level(db, level=3, discount_pct=5)
    plan = _make_plan(db, price=100, days=30)
    _make_credit(db, u.id, discount_pct=80)  # 实付 80%
    _gateway_cfg(db)

    resp = _user_app(db, u).post(
        "/api/user/economy/payment/order",
        json={"kind": "subscription", "item_id": plan.id})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    # 会员折扣 5%：100 -> 95；权益 80%：95 -> 76；总优惠 24
    assert body["member_discount_pct"] == 5
    assert body["member_discount_amount"] == 5.0
    assert body["exchange_discount_pct"] == 80
    assert body["discount_amount"] == 24.0
    assert body["amount"] == 76.0
    assert body["list_price"] == 100.0


def test_credit_without_member_discount_unchanged(db):
    # Lv1 无会员折扣：行为与修前一致（只算权益）
    u = _make_user(db, member_level=1)
    _make_level(db, level=1, discount_pct=0)
    plan = _make_plan(db, price=100, days=30)
    _make_credit(db, u.id, discount_pct=80)
    _gateway_cfg(db)

    resp = _user_app(db, u).post(
        "/api/user/economy/payment/order",
        json={"kind": "subscription", "item_id": plan.id})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["discount_amount"] == 20.0
    assert body["amount"] == 80.0


# ---------------- F2：quote 空码按自动权益试算 ----------------

def test_quote_empty_code_applies_auto_credit(db):
    u = _make_user(db)
    _make_level(db, level=3, discount_pct=5)
    plan = _make_plan(db, price=100, days=30)
    _make_credit(db, u.id, discount_pct=80)

    resp = _user_app(db, u).post(
        "/api/user/economy/payment/coupon/quote",
        json={"code": "", "kind": "subscription", "item_id": plan.id})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    # 与下单口径一致：会员折扣 5 + 权益（95 的 80% 实付）= 总优惠 24，实付 76
    assert body["member_discount_pct"] == 5
    assert body["exchange_discount_pct"] == 80
    assert body["discount_amount"] == 24.0
    assert body["paid_amount"] == 76.0


def test_quote_empty_code_recharge_still_requires_code(db):
    u = _make_user(db)
    resp = _user_app(db, u).post(
        "/api/user/economy/payment/coupon/quote",
        json={"code": "", "kind": "recharge", "item_id": 1})
    assert resp.status_code == 400, resp.text
    assert "请填写优惠码" in resp.json()["detail"]


# ---------------- F3：续费顺延清掉提醒去重 ----------------

def test_grant_subscription_extension_resets_reminders(db):
    u = _make_user(db)
    plan = _make_plan(db, price=100, days=30)
    now = datetime.now()
    sub = models.UserSubscription(
        user_id=u.id, plan_id=plan.id, realm_id=None,
        start_date=now - timedelta(days=25), end_date=now + timedelta(days=5),
        status="active")
    db.add(sub)
    db.commit()
    # 上一周期已发过 7d/1d 提醒
    for kind in ("7d", "1d"):
        db.add(models.SubscriptionReminder(
            subscription_id=sub.id, user_id=u.id, realm_id=None,
            kind=kind, sent_at=now - timedelta(days=20)))
    db.commit()

    # 续费：顺延同一行
    new_sub = _grant_subscription(db, u, plan, 30, "test", "test:1")
    db.commit()
    assert new_sub.id == sub.id
    assert new_sub.end_date > now + timedelta(days=30)

    # 去重记录被清掉，新周期可重新提醒
    left = db.query(models.SubscriptionReminder).filter(
        models.SubscriptionReminder.subscription_id == sub.id).all()
    assert left == []


def test_grant_subscription_new_row_keeps_nothing(db):
    # 新建订阅（无顺延）：本来就没有提醒记录，不报错
    u = _make_user(db)
    plan = _make_plan(db, price=100, days=30)
    sub = _grant_subscription(db, u, plan, 30, "test", "test:2")
    db.commit()
    assert sub.id is not None
    assert sub.end_date > datetime.now() + timedelta(days=29)


# ---------------- F4（BUG-2）：管理员手动授予不产生双 active 行 ----------------

def _admin_app(db):
    from backend.api.admin import admin_router
    from backend.api.admin_core import get_current_admin
    app = FastAPI()
    app.include_router(admin_router)
    app.dependency_overrides[get_current_admin] = lambda: SimpleNamespace(id=999)
    app.dependency_overrides[get_db] = lambda: db
    return TestClient(app)


def test_admin_grant_extends_existing_instead_of_new_row(db):
    from backend.api import admin as admin_mod  # noqa: F401 —— 确保路由已注册
    u = _make_user(db)
    realm = models.ServerRealm(name="测试服", slug="test", is_active=True)
    db.add(realm)
    db.commit()
    plan = _make_plan(db, price=100, days=30)
    plan.realm_id = realm.id
    db.commit()
    now = datetime.now()
    old = models.UserSubscription(
        user_id=u.id, plan_id=plan.id, realm_id=realm.id,
        start_date=now - timedelta(days=25), end_date=now + timedelta(days=5),
        status="active")
    db.add(old)
    db.commit()
    old_end = old.end_date

    resp = _admin_app(db).post(
        f"/api/admin/users/{u.id}/subscriptions",
        json={"plan_id": plan.id, "duration_days": 30})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    # 顺延同一行，不新建第二条 active 行
    assert body["subscription_id"] == old.id
    rows = db.query(models.UserSubscription).filter(
        models.UserSubscription.user_id == u.id,
        models.UserSubscription.status == "active").all()
    assert len(rows) == 1
    assert rows[0].end_date == old_end + timedelta(days=30)


def test_admin_grant_new_user_creates_row(db):
    from backend.api import admin as admin_mod  # noqa: F401
    u = _make_user(db)
    plan = _make_plan(db, price=100, days=30)

    resp = _admin_app(db).post(
        f"/api/admin/users/{u.id}/subscriptions",
        json={"plan_id": plan.id, "duration_days": 30})
    assert resp.status_code == 200, resp.text
    rows = db.query(models.UserSubscription).filter(
        models.UserSubscription.user_id == u.id).all()
    assert len(rows) == 1
    assert rows[0].end_date > datetime.now() + timedelta(days=29)
