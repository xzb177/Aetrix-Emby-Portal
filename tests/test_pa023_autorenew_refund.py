"""PA-023 回归：自动续订订单退款必须退回当初扣的能量。

背景（审计 R10 实证）：subscriptions._auto_renew_one 落单
payment_method='auto_renew'、扣款台账 type='subscription_auto_renew'、
ref_id='auto_renew:{sub_id}:{分钟}'；而 orders_admin.refund_order 的积分
退回分支此前只认 payment_method='points' + subscription_buy 台账，两项全
不命中 → 天数回滚了、能量退回 0 且无提示。手动积分购买的同款问题已修，
自动续订漏网。本文件走真实退款端点验证三条口径：

1. 自动续订单退款 → 天数回滚 + 能量全额退回（与原扣费一致）；
2. 手动积分单退款行为不变（天数回滚 + 能量退回）；
3. 续订单查不到扣款台账时不凭空退能量；现金单退款不碰能量。
"""
import asyncio
from datetime import datetime, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend import models
from backend.models import Base
from backend.api import orders_admin


@pytest.fixture
def db():
    # 退款端点把冲正下放线程池执行，内存库必须跨线程共用同一连接
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    yield session
    session.close()


@pytest.fixture(autouse=True)
def _mute_notify(monkeypatch):
    """退款成功后的站内通知与本单无关，静音避免触碰真实通知通道。"""

    async def _noop(*args, **kwargs):
        return True

    monkeypatch.setattr(
        "backend.notifications.notify_admin_event_quiet", _noop)


def _admin(db):
    admin = models.WebUser(username="pa023-admin", password_hash="x",
                           is_active=True, points=0)
    db.add(admin)
    db.commit()
    db.refresh(admin)
    return admin


def _user_with_sub(db, username, points):
    user = models.WebUser(username=username, password_hash="x",
                          is_active=True, points=points)
    db.add(user)
    plan = models.SubscriptionPlan(name="远行", price=100, duration_days=30,
                                   realm_id=None)
    db.add(plan)
    db.commit()
    now = datetime.now()
    sub = models.UserSubscription(
        user_id=user.id, plan_id=plan.id, realm_id=None,
        start_date=now, end_date=now + timedelta(days=30), status="active")
    db.add(sub)
    db.commit()
    db.refresh(user)
    return user, plan, sub, now


def _refund(db, admin, order_id):
    return asyncio.run(orders_admin.refund_order(
        order_id=order_id,
        request=orders_admin.RefundRequest(reason="PA-023 回归测试"),
        current_admin=admin,
        db=db,
    ))


def test_auto_renew_order_refund_returns_points(db):
    """自动续订单退款：天数回滚 30 天 + 实付 900 能量全额退回。"""
    admin = _admin(db)
    user, plan, sub, now = _user_with_sub(db, "pa023-renew", points=100)
    order = models.SubscriptionOrder(
        order_id="AR-PA023-1", user_id=user.id, plan_id=plan.id,
        item_name="远行", amount=plan.price, payment_method="auto_renew",
        status="paid", paid_at=now, subscription_id=sub.id, days_granted=30)
    db.add(order)
    # 与 _auto_renew_one 完全一致的扣款台账
    db.add(models.PointsLog(
        user_id=user.id, amount=-900, balance_after=100,
        type="subscription_auto_renew", description="自动续订 - 远行",
        ref_id=f"auto_renew:{sub.id}:{now.strftime('%Y%m%d%H%M')}"))
    db.commit()

    result = _refund(db, admin, "AR-PA023-1")

    assert result["revoked_days"] == 30
    assert result["revoked_points"] == -900
    db.expire_all()
    user2 = db.query(models.WebUser).filter_by(id=user.id).first()
    assert user2.points == 1000
    sub2 = db.query(models.UserSubscription).filter_by(id=sub.id).first()
    assert sub2.end_date <= now + timedelta(seconds=5)
    assert sub2.status == "cancelled"
    refund_log = (
        db.query(models.PointsLog)
        .filter(models.PointsLog.user_id == user.id,
                models.PointsLog.type == "refund",
                models.PointsLog.ref_id == "refund:AR-PA023-1")
        .first()
    )
    assert refund_log is not None
    assert refund_log.amount == 900
    order2 = db.query(models.SubscriptionOrder).filter_by(
        order_id="AR-PA023-1").first()
    assert order2.status == "refunded"


def test_manual_points_order_refund_unchanged(db):
    """手动积分单退款行为不变：天数回滚 30 天 + 实付 500 能量退回。"""
    admin = _admin(db)
    user, plan, sub, now = _user_with_sub(db, "pa023-manual", points=500)
    order = models.SubscriptionOrder(
        order_id="PT-PA023-1", user_id=user.id, plan_id=plan.id,
        item_name="远行", amount=plan.price, payment_method="points",
        status="paid", paid_at=now, subscription_id=sub.id, days_granted=30)
    db.add(order)
    db.add(models.PointsLog(
        user_id=user.id, amount=-500, balance_after=500,
        type="subscription_buy", description="积分购买订阅 - 远行",
        ref_id="subscription_points:PT-PA023-1"))
    db.commit()

    result = _refund(db, admin, "PT-PA023-1")

    assert result["revoked_days"] == 30
    assert result["revoked_points"] == -500
    db.expire_all()
    user2 = db.query(models.WebUser).filter_by(id=user.id).first()
    assert user2.points == 1000


def test_auto_renew_refund_without_ledger_returns_no_points(db):
    """续订单查不到扣款台账：只回滚天数，不凭空退能量。"""
    admin = _admin(db)
    user, plan, sub, now = _user_with_sub(db, "pa023-nolog", points=100)
    order = models.SubscriptionOrder(
        order_id="AR-PA023-2", user_id=user.id, plan_id=plan.id,
        item_name="远行", amount=plan.price, payment_method="auto_renew",
        status="paid", paid_at=now, subscription_id=sub.id, days_granted=30)
    db.add(order)
    db.commit()

    result = _refund(db, admin, "AR-PA023-2")

    assert result["revoked_days"] == 30
    assert result["revoked_points"] == 0
    db.expire_all()
    user2 = db.query(models.WebUser).filter_by(id=user.id).first()
    assert user2.points == 100


def test_cash_order_refund_does_not_touch_points(db):
    """现金订阅单退款：只回滚天数，能量余额不动。"""
    admin = _admin(db)
    user, plan, sub, now = _user_with_sub(db, "pa023-cash", points=100)
    order = models.SubscriptionOrder(
        order_id="CS-PA023-1", user_id=user.id, plan_id=plan.id,
        item_name="远行", amount=plan.price, payment_method="alipay",
        status="paid", paid_at=now, subscription_id=sub.id, days_granted=30)
    db.add(order)
    db.commit()

    result = _refund(db, admin, "CS-PA023-1")

    assert result["revoked_days"] == 30
    assert result["revoked_points"] == 0
    db.expire_all()
    user2 = db.query(models.WebUser).filter_by(id=user.id).first()
    assert user2.points == 100
