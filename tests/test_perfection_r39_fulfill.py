"""r39 后端完美迭代：履约副作用失败不再静默。

F1: _fulfill_order 里会员经验累加 / 邀请返利失败原来只记一行日志就提交，
    「钱收了、经验/返利没到」用户无从知晓。现改为：失败时往 pending 里追加
    一条 economy.fulfill_partial 站内信（提交后由 send_fulfill_notifications
    发出），受影响的用户凭此联系客服/服主补发。

全部用隔离的内存 SQLite，不碰生产库。
"""
import os

os.environ.setdefault("DATABASE_TYPE", "sqlite")
os.environ.setdefault("REDIS_ENABLED", "false")

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend import member_level as ml
from backend import models
from backend.api import economy
from backend.notifications import AdminEvent


@pytest.fixture()
def db():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    models.Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    ml.ensure_member_levels_seeded(session)
    try:
        yield session
    finally:
        session.rollback()
        session.close()
        engine.dispose()


def _make_user(db, username, points=0):
    u = models.WebUser(username=username, password_hash="x", points=points,
                       is_active=True, member_xp=0, member_level=1)
    db.add(u)
    db.commit()
    return u


def _make_recharge_order(db, user, order_id="R39T1", amount=100, price=10):
    o = models.RechargeOrder(order_id=order_id, user_id=user.id, package_id=1,
                             amount=amount, price=price, status="pending")
    db.add(o)
    db.commit()
    return o


def _partial_items(pending):
    return [p for p in pending
            if p.get("event_type") == AdminEvent.FULFILL_PARTIAL]


def test_recharge_xp_failure_not_silent(db, monkeypatch):
    """经验累加抛异常：订单照常履约（积分到账），但 payer 必须收到补发提示。"""
    u = _make_user(db, "r39xp")
    o = _make_recharge_order(db, u)

    def _boom(*a, **k):
        raise RuntimeError("xp boom")

    monkeypatch.setattr(ml, "add_xp", _boom)

    pending = economy._fulfill_order(db, recharge_order=o)
    db.commit()

    db.refresh(o)
    db.refresh(u)
    assert o.status == "paid"          # 主体履约不被阻塞
    assert u.points == 100             # 积分照常到账
    partials = _partial_items(pending)
    assert len(partials) == 1
    assert partials[0]["user_id"] == u.id
    assert "经验" in partials[0]["title"]
    assert o.order_id in partials[0]["content"]


def test_recharge_rebate_failure_notifies_inviter(db, monkeypatch):
    """返利抛异常：邀请人（受影响方）必须收到补发提示，而不是只有一行日志。"""
    inviter = _make_user(db, "r39inviter")
    invitee = _make_user(db, "r39invitee")
    code = models.InvitationCode(code="R39CODE", user_id=inviter.id,
                                 reward_points=0)
    db.add(code)
    db.flush()
    db.add(models.InvitationRecord(inviter_id=inviter.id,
                                   invitee_id=invitee.id,
                                   code_id=code.id))
    db.commit()
    o = _make_recharge_order(db, invitee, order_id="R39T2")

    import backend.api.invitation as inv_mod

    def _boom(*a, **k):
        raise RuntimeError("rebate boom")

    monkeypatch.setattr(inv_mod, "apply_rebate", _boom)

    pending = economy._fulfill_order(db, recharge_order=o)
    db.commit()

    db.refresh(o)
    assert o.status == "paid"
    partials = _partial_items(pending)
    assert len(partials) == 1
    assert partials[0]["user_id"] == inviter.id
    assert "返利" in partials[0]["title"]


def test_recharge_happy_path_no_partial_message(db):
    """正常履约：没有任何 fulfill_partial 噪音，行为与旧版一致。"""
    u = _make_user(db, "r39ok")
    o = _make_recharge_order(db, u, order_id="R39T3")

    pending = economy._fulfill_order(db, recharge_order=o)
    db.commit()

    db.refresh(o)
    db.refresh(u)
    assert o.status == "paid"
    assert u.points == 100
    assert _partial_items(pending) == []
    # 成功通知仍在
    assert any(p.get("event_type") == "economy.recharge_success"
               for p in pending)


def test_subscription_xp_failure_not_silent(db, monkeypatch):
    """订阅履约的经验失败：同样要通知到付款用户。"""
    u = _make_user(db, "r39sub")
    plan = models.SubscriptionPlan(name="月卡", price=30, duration_days=30)
    db.add(plan)
    db.commit()
    o = models.SubscriptionOrder(order_id="R39S1", user_id=u.id,
                                 plan_id=plan.id, amount=30, status="pending")
    db.add(o)
    db.commit()

    def _boom(*a, **k):
        raise RuntimeError("sub xp boom")

    monkeypatch.setattr(ml, "add_xp", _boom)

    pending = economy._fulfill_order(db, subscription_order=o)
    db.commit()

    db.refresh(o)
    assert o.status == "paid"
    partials = _partial_items(pending)
    assert len(partials) == 1
    assert partials[0]["user_id"] == u.id
    assert "经验" in partials[0]["title"]
    assert any(p.get("event_type") == "economy.subscription_success"
               for p in pending)
