"""r33 后端完美迭代：充值/订单域 4 项修复。

F1: 退款不回滚会员 XP —— member_level.add_xp 支持负增量（钳制 0、等级可降级），
    orders_admin._reverse_xp 按账本精确撤回履约时发的经验（幂等）。
F2: 自定义金额充值带优惠码从「静默忽略」改为明确 400。
F3: 积分转账每日上限的日界线改上海自然日（UTC 入库帧对齐）。
F4: 关单/退款时把兑换码折扣权益还回未用（与优惠券释放同一道理）。
"""
import os

os.environ.setdefault("DATABASE_TYPE", "sqlite")
os.environ.setdefault("REDIS_ENABLED", "false")

from datetime import datetime, timezone
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend import member_level as ml
from backend import models
from backend.api import economy, orders_admin
from backend.api.admin_core import get_current_admin
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


def _make_user(db, username="u1", points=1000):
    u = models.WebUser(username=username, password_hash="", points=points,
                       is_active=True, member_xp=0, member_level=1)
    db.add(u)
    db.commit()
    return u


def _set_cfg(db, key, value):
    cfg = db.query(models.SystemConfig).filter(models.SystemConfig.key == key).first()
    if cfg:
        cfg.value = str(value)
    else:
        db.add(models.SystemConfig(key=key, value=str(value)))
    db.commit()


# ---------------- F1: add_xp 负增量 ----------------

def test_add_xp_negative_partial_reversal(db):
    u = _make_user(db)
    ml.add_xp(db, u, 100, "recharge", "recharge:RB1")
    db.commit()
    res = ml.add_xp(db, u, -30, "refund", "refund:RB1")
    db.commit()
    assert res["added"] is True
    db.refresh(u)
    assert u.member_xp == 70
    rows = db.query(models.MemberXpLog).filter(
        models.MemberXpLog.user_id == u.id).order_by(models.MemberXpLog.id).all()
    assert [r.xp_delta for r in rows] == [100, -30]
    assert rows[1].xp_after == 70
    assert rows[1].ref_id == "refund:RB1"


def test_add_xp_negative_clamp_zero(db):
    u = _make_user(db)
    ml.add_xp(db, u, 20, "recharge", "recharge:RB2")
    db.commit()
    ml.add_xp(db, u, -50, "refund", "refund:RB2")
    db.commit()
    db.refresh(u)
    assert u.member_xp == 0  # 钳制在 0，不扣成负数


def test_add_xp_zero_noop(db):
    u = _make_user(db)
    res = ml.add_xp(db, u, 0, "refund", "refund:RB3")
    assert res == {"added": False}
    assert db.query(models.MemberXpLog).count() == 0


def _seed_levels(db):
    db.add(models.MemberLevel(level=1, name="L1", xp_threshold=0,
                              discount_pct=0, is_active=True))
    db.add(models.MemberLevel(level=2, name="L2", xp_threshold=100,
                              discount_pct=5, is_active=True))
    db.commit()


def test_add_xp_negative_downgrade(db):
    _seed_levels(db)
    u = _make_user(db)
    ml.add_xp(db, u, 120, "recharge", "recharge:RB4")
    db.commit()
    db.refresh(u)
    assert u.member_level == 2
    res = ml.add_xp(db, u, -50, "refund", "refund:RB4")
    db.commit()
    db.refresh(u)
    assert u.member_xp == 70
    assert u.member_level == 1  # 掉回阈值以下就降级
    assert res["leveled_down"] is True
    assert res["leveled_up"] is False


def test_reverse_xp_idempotent(db):
    """按账本撤回：第一次撤回实发数，第二次 0（不重复扣）。"""
    u = _make_user(db)
    ml.add_xp(db, u, 100, "recharge", "recharge:RB9")
    db.commit()
    first = orders_admin._reverse_xp(db, u, "RB9", "recharge")
    db.commit()
    db.refresh(u)
    assert first == 100
    assert u.member_xp == 0
    second = orders_admin._reverse_xp(db, u, "RB9", "recharge")
    db.commit()
    db.refresh(u)
    assert second == 0
    assert u.member_xp == 0


def test_reverse_xp_no_grant_noop(db):
    """履约时经验发放失败（无账本行）时撤回 0，不报错。"""
    u = _make_user(db)
    assert orders_admin._reverse_xp(db, u, "RB404", "recharge") == 0


# ---------------- F4: 折扣权益还回 ----------------

def _make_credit(db, user_id, order_id):
    c = models.ExchangeDiscountCredit(
        user_id=user_id, exchange_code_id=None, discount_pct=85,
        status="used", used_order_id=order_id)
    db.add(c)
    db.commit()
    return c


def test_restore_discount_credit(db):
    u = _make_user(db)
    _make_credit(db, u.id, "RB7")
    assert orders_admin._restore_discount_credit(db, "RB7") is True
    c = db.query(models.ExchangeDiscountCredit).filter(
        models.ExchangeDiscountCredit.user_id == u.id).first()
    assert c.status == "unused"
    assert c.used_order_id is None
    # 幂等：第二次无可用行
    assert orders_admin._restore_discount_credit(db, "RB7") is False


def _admin_app(db):
    app = FastAPI()
    app.include_router(orders_admin.admin_orders_router)
    app.dependency_overrides[get_current_admin] = lambda: SimpleNamespace(id=999)
    app.dependency_overrides[get_db] = lambda: db
    return TestClient(app)


def test_close_order_restores_credit(db):
    u = _make_user(db)
    order = models.RechargeOrder(
        order_id="RB8", user_id=u.id, package_id=0, amount=12,
        price=10, payment_method="alipay", status="pending")
    db.add(order)
    _make_credit(db, u.id, "RB8")
    db.commit()

    resp = _admin_app(db).post("/api/admin/economy/orders/RB8/close")
    assert resp.status_code == 200, resp.text
    db.expire_all()
    c = db.query(models.ExchangeDiscountCredit).filter(
        models.ExchangeDiscountCredit.user_id == u.id).first()
    assert c.status == "unused"
    assert c.used_order_id is None
    o = db.query(models.RechargeOrder).filter(
        models.RechargeOrder.order_id == "RB8").first()
    assert o.status == "closed"


# ---------------- F2: 自定义充值 + 优惠码 ----------------

def _user_app(db, user):
    app = FastAPI()
    app.include_router(economy.router)
    app.dependency_overrides[get_current_user] = lambda: user
    app.dependency_overrides[get_db] = lambda: db
    return TestClient(app)


def _gateway_cfg(db):
    _set_cfg(db, "payment_gateway_url", "https://pay.example.com")
    _set_cfg(db, "payment_partner_id", "10001")
    _set_cfg(db, "payment_partner_key", "testkey")
    _set_cfg(db, "site_url", "https://app.example.com")


def test_custom_recharge_with_coupon_400(db):
    u = _make_user(db)
    _gateway_cfg(db)
    resp = _user_app(db, u).post(
        "/api/user/economy/payment/order",
        json={"kind": "recharge", "custom_amount": 10.0, "coupon_code": "ABC123"})
    assert resp.status_code == 400, resp.text
    assert "暂不支持使用优惠券" in resp.json()["detail"]


def test_custom_recharge_without_coupon_ok(db):
    u = _make_user(db)
    _gateway_cfg(db)
    resp = _user_app(db, u).post(
        "/api/user/economy/payment/order",
        json={"kind": "recharge", "custom_amount": 10.0})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["success"] is True
    assert body["pay_url"].startswith("https://pay.example.com/submit.php?")


# ---------------- F3: 上海口径日界线 ----------------

def test_today_start_utc_naive_frame():
    ts = economy._today_start_utc_naive()
    assert ts.tzinfo is None
    sh = ts.replace(tzinfo=timezone.utc).astimezone(ZoneInfo("Asia/Shanghai"))
    assert (sh.hour, sh.minute, sh.second, sh.microsecond) == (0, 0, 0, 0)


def test_today_start_utc_naive_is_shanghai_midnight():
    # 与「上海自然日 00:00」是同一时刻（容差 1 天内的表示差异）
    ts = economy._today_start_utc_naive()
    now_sh = datetime.now(ZoneInfo("Asia/Shanghai")).replace(
        hour=0, minute=0, second=0, microsecond=0)
    expected = now_sh.astimezone(timezone.utc).replace(tzinfo=None)
    assert ts == expected
