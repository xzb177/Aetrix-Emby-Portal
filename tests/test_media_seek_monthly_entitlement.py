"""订阅分档 PR-2：求片月度额度接入权益单口（media_seek.monthly_limit）。

钉住的判定顺序（设计 reports/subscription-tier-design-20261024.md §3.2）：

1. welfare 优先且口径不变：is_welfare 有效 → ``welfare_request_monthly``
   （默认 3），**即使买了声明更高额度的套餐也仍是 3**——一期有意保留
   的语义（红线「公益口径不动」），不是 bug；
2. 非 welfare 且当前套餐声明了 ``media_seek_monthly_limit`` → 用声明值；
3. 没声明（无订阅 / 未声明该键 / 订阅过期 / 权益值非法）→ 回落现行
   ``paid_request_monthly``（默认 10），不抛错。

三处消费方（提交拦截 / 额度快照 / 管理端查看）都只经 monthly_limit /
monthly_quota 收口，本文件只测这一层；monthly_quota 的形状（kind 仍为
welfare/paid 二值）在权益命中时断言不变。
"""
import os

os.environ.setdefault("DATABASE_TYPE", "sqlite")
os.environ.setdefault("REDIS_ENABLED", "false")

from datetime import datetime, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend import entitlements, media_seek, models

KEY_SEEK = "media_seek_monthly_limit"


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


@pytest.fixture(autouse=True)
def _clear_entitlement_caches():
    entitlements._PLAN_SNAPSHOT_CACHE.clear()
    entitlements._USER_PLAN_CACHE.clear()
    yield
    entitlements._PLAN_SNAPSHOT_CACHE.clear()
    entitlements._USER_PLAN_CACHE.clear()


def _make_user(db, username="u1", **kw):
    u = models.WebUser(username=username, password_hash="", points=0,
                       is_active=True, **kw)
    db.add(u)
    db.commit()
    return u


def _make_plan(db, name="远行执照", entitlements_value=None, **kw):
    p = models.SubscriptionPlan(
        name=name, price=100, duration_days=30, is_active=True,
        entitlements=entitlements_value, **kw)
    db.add(p)
    db.commit()
    return p


def _make_sub(db, user, plan, days=30, status="active"):
    now = datetime.now()
    s = models.UserSubscription(
        user_id=user.id, plan_id=plan.id, realm_id=None,
        start_date=now, end_date=now + timedelta(days=days), status=status)
    db.add(s)
    db.commit()
    return s


# ---------------- welfare 优先：套餐声明跳不过公益口径 ----------------

def test_welfare_with_entitled_plan_still_welfare_limit(db):
    """welfare 用户买了声明 20 的套餐，月度仍走公益口径 3（一期有意如此）。"""
    user = _make_user(db, is_welfare=True)
    plan = _make_plan(db, entitlements_value={KEY_SEEK: 20})
    _make_sub(db, user, plan)
    assert media_seek.monthly_limit(db, user) == media_seek.DEFAULT_MONTHLY_WELFARE == 3
    assert media_seek.monthly_quota(db, user)["kind"] == "welfare"


def test_welfare_without_subscription_still_welfare_limit(db):
    user = _make_user(db, is_welfare=True)
    assert media_seek.monthly_limit(db, user) == 3


# ---------------- 非 welfare：声明生效 ----------------

def test_paid_plan_declared_limit_wins(db):
    user = _make_user(db)
    plan = _make_plan(db, entitlements_value={KEY_SEEK: 20})
    _make_sub(db, user, plan)
    assert media_seek.monthly_limit(db, user) == 20
    quota = media_seek.monthly_quota(db, user)
    assert quota["monthly_limit"] == 20
    assert quota["kind"] == "paid", "权益命中时 kind 形状不变，仍报 paid"


# ---------------- 非 welfare：没声明回落现行 paid 口径 ----------------

def test_paid_subscribed_but_key_undeclared_falls_back(db):
    """有订阅、套餐只声明了别的键 → media_seek_monthly_limit 按缺键回落 10。"""
    user = _make_user(db)
    plan = _make_plan(db, entitlements_value={"concurrent_streams": 4})
    _make_sub(db, user, plan)
    assert media_seek.monthly_limit(db, user) == media_seek.DEFAULT_MONTHLY_PAID == 10


def test_paid_no_subscription_falls_back(db):
    user = _make_user(db)
    assert media_seek.monthly_limit(db, user) == 10


def test_paid_expired_subscription_falls_back(db):
    """套餐声明了 20，但订阅已过期 → 视为无套餐，回落 10。"""
    user = _make_user(db)
    plan = _make_plan(db, entitlements_value={KEY_SEEK: 20})
    _make_sub(db, user, plan, days=-1)
    assert media_seek.monthly_limit(db, user) == 10


# ---------------- 坏数据：回落不抛 ----------------

def test_paid_bad_entitlement_value_falls_back(db):
    """权益值是字符串（人工改库写坏）→ 按缺键回落 10，绝不抛错。"""
    user = _make_user(db)
    plan = _make_plan(db, entitlements_value={KEY_SEEK: "abc"})
    _make_sub(db, user, plan)
    assert media_seek.monthly_limit(db, user) == 10


def test_paid_out_of_range_entitlement_value_falls_back(db):
    """权益值越界（0，小于注册表下限 1）→ 按缺键回落 10。"""
    user = _make_user(db)
    plan = _make_plan(db, entitlements_value={KEY_SEEK: 0})
    _make_sub(db, user, plan)
    assert media_seek.monthly_limit(db, user) == 10
