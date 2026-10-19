"""r48 遗留 WS4a：订阅口径三项回归测试

⑪ 积分买订阅与人民币路径同口径（先会员等级折扣得中间价，再叠加优惠券/权益）；
⑫ 续买不同档位时 plan_id 跟随新档；
⑬ auto_renew 真能力：开关接口 + 定时续订（余额足续 / 不足不续且只提醒一次 / 重复执行不双续）。
"""
from datetime import datetime, timedelta
from decimal import Decimal

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend import models
from backend.models import Base
from backend.api import economy
from backend.api.economy import _grant_subscription
from backend import subscriptions as subs
from backend.api.user import set_my_subscription_auto_renew, AutoRenewRequest


@pytest.fixture
def db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    S = sessionmaker(bind=engine)
    s = S()
    yield s
    s.close()


def _user(db, username="u1", points=0, level=1):
    u = models.WebUser(username=username, password_hash="x", points=points,
                       member_level=level, is_active=True)
    db.add(u)
    db.commit()
    db.refresh(u)
    return u


def _realm(db, access_mode="paid"):
    r = models.ServerRealm(name="付费服", slug="paid1", access_mode=access_mode)
    db.add(r)
    db.commit()
    db.refresh(r)
    return r


def _plan(db, realm_id, name="月卡", price="18.00", points_price="400", days=30):
    p = models.SubscriptionPlan(
        name=name, price=Decimal(price),
        points_price=Decimal(points_price) if points_price is not None else None,
        duration_days=days, is_active=True, realm_id=realm_id)
    db.add(p)
    db.commit()
    db.refresh(p)
    return p


def _level(db, level, discount_pct):
    db.add(models.MemberLevel(level=level, name=f"Lv{level}",
                              xp_threshold=0, discount_pct=discount_pct,
                              is_active=True))
    db.commit()


def _order_req(plan_id, **kw):
    base = dict(kind="subscription", item_id=plan_id,
                payment_method="alipay", pay_with_points=True)
    base.update(kw)
    return economy.CreateOrderRequest(**base)


def _balance(db, uid):
    db.expire_all()
    return db.query(models.WebUser.points).filter(
        models.WebUser.id == uid).scalar() or 0


def _gateway_cfg(db):
    # create_payment_order 入口先校验支付网关配置（积分支付也一样），测试里种上
    for k, v in (("payment_gateway_url", "https://pay.example"),
                 ("payment_partner_id", "1"),
                 ("payment_partner_key", "k")):
        db.add(models.SystemConfig(key=k, value=v))
    db.commit()


# ==================== ⑪ 积分口径 ====================

def test_points_purchase_applies_member_discount(db):
    """积分购买先走会员等级折扣：Lv 折扣 10%，400 积分价实付 360"""
    r = _realm(db)
    _level(db, 2, 10)
    u = _user(db, "buyer", points=1000, level=2)
    plan = _plan(db, r.id, points_price="400")
    _gateway_cfg(db)
    res = economy.create_payment_order(None, _order_req(plan.id),
                                       current_user=u, db=db)
    assert res["paid_with_points"] is True
    assert res["points_cost"] == 360
    assert res["member_discount_pct"] == 10
    assert res["member_discount_points"] == 40
    assert _balance(db, u.id) == 640
    log = db.query(models.PointsLog).filter(
        models.PointsLog.user_id == u.id,
        models.PointsLog.type == "subscription_buy").first()
    assert log is not None and log.amount == -360


def test_points_purchase_no_discount_when_no_level(db):
    """没有等级配置时按原价（兼容旧行为）"""
    r = _realm(db)
    u = _user(db, "buyer", points=1000, level=1)
    plan = _plan(db, r.id, points_price="400")
    _gateway_cfg(db)
    res = economy.create_payment_order(None, _order_req(plan.id),
                                       current_user=u, db=db)
    assert res["points_cost"] == 400
    assert _balance(db, u.id) == 600


def test_points_purchase_percent_coupon_on_intermediate(db):
    """百分比券对「会员折扣后的中间价」再打折：400 → 会员9折 360 → 券8折 288"""
    r = _realm(db)
    _level(db, 2, 10)
    u = _user(db, "buyer", points=1000, level=2)
    plan = _plan(db, r.id, points_price="400")
    _gateway_cfg(db)
    db.add(models.CouponCode(code="P80", kind="subscription",
                             discount_type="percent", value=80,
                             is_active=True, per_user_limit=0, max_uses=0))
    db.commit()
    res = economy.create_payment_order(
        None, _order_req(plan.id, coupon_code="p80"),
        current_user=u, db=db)
    assert res["points_cost"] == 288
    assert res["coupon_code"] == "P80"
    assert res["coupon_discount_points"] == 72
    assert _balance(db, u.id) == 712
    usage = db.query(models.CouponUsage).filter(
        models.CouponUsage.order_id == res["order_id"]).first()
    assert usage is not None and usage.status == "consumed"


def test_points_purchase_fixed_coupon_rejected(db):
    """人民币直减券不能用于能量抵扣：明确 400，不静默忽略"""
    r = _realm(db)
    u = _user(db, "buyer", points=1000)
    plan = _plan(db, r.id, points_price="400")
    _gateway_cfg(db)
    db.add(models.CouponCode(code="FIX10", kind="subscription",
                             discount_type="fixed", value=10,
                             is_active=True, per_user_limit=0, max_uses=0))
    db.commit()
    with pytest.raises(HTTPException) as ei:
        economy.create_payment_order(
            None, _order_req(plan.id, coupon_code="FIX10"),
            current_user=u, db=db)
    assert ei.value.status_code == 400
    assert "能量抵扣" in ei.value.detail
    assert _balance(db, u.id) == 1000  # 没扣款


def test_points_purchase_auto_applies_best_credit(db):
    """没填优惠券时自动用最优兑换码折扣权益（与人民币下单同口径）"""
    r = _realm(db)
    u = _user(db, "buyer", points=1000)
    plan = _plan(db, r.id, points_price="400")
    _gateway_cfg(db)
    db.add(models.ExchangeDiscountCredit(user_id=u.id, status="unused",
                                         discount_pct=85))
    db.commit()
    res = economy.create_payment_order(None, _order_req(plan.id),
                                       current_user=u, db=db)
    assert res["exchange_discount_pct"] == 85
    assert res["exchange_discount_points"] == 60  # 400 的 15%
    assert res["points_cost"] == 340
    credit = db.query(models.ExchangeDiscountCredit).first()
    assert credit.status == "used"
    assert credit.used_order_id == res["order_id"]


def test_points_quote_matches_order(db):
    """quote 预览与下单同口径：预览价 == 实付价"""
    r = _realm(db)
    _level(db, 2, 10)
    u = _user(db, "buyer", points=1000, level=2)
    plan = _plan(db, r.id, points_price="400")
    _gateway_cfg(db)
    q = economy.coupon_quote(
        economy.CouponQuoteRequest(kind="subscription", item_id=plan.id,
                                   pay_with_points=True),
        current_user=u, db=db)
    assert q["pay_with_points"] is True
    assert q["paid_amount"] == 360.0
    assert q["member_discount_pct"] == 10
    res = economy.create_payment_order(None, _order_req(plan.id),
                                       current_user=u, db=db)
    assert res["points_cost"] == int(q["paid_amount"])


# ==================== ⑫ plan_id 跟随 ====================

def test_grant_renew_updates_plan_id(db):
    """续买不同档位：同一行顺延，plan_id 跟随新档，天数按新档"""
    r = _realm(db)
    u = _user(db, "u1")
    plan_a = _plan(db, r.id, name="月卡", days=30)
    plan_b = _plan(db, r.id, name="季卡", days=90)
    s1 = _grant_subscription(db, u, plan_a, 30, "purchase", "o1",
                             realm_id=r.id)
    db.commit()
    first_end = s1.end_date
    s2 = _grant_subscription(db, u, plan_b, 90, "purchase", "o2",
                             realm_id=r.id)
    db.commit()
    assert s2.id == s1.id  # 同一行
    assert s2.plan_id == plan_b.id  # 跟随新档
    assert s2.end_date == first_end + timedelta(days=90)  # 天数按新档


# ==================== ⑬ 自动续订 ====================

def _active_sub(db, user, plan, realm, hours_left=12, auto_renew=True):
    now = datetime.now()
    s = models.UserSubscription(
        user_id=user.id, plan_id=plan.id, realm_id=realm.id,
        start_date=now - timedelta(days=20),
        end_date=now + timedelta(hours=hours_left),
        status="active", auto_renew=auto_renew)
    db.add(s)
    db.commit()
    db.refresh(s)
    return s


def test_auto_renew_success(db):
    """余额足：自动续订一期，扣积分、顺延、落订单与台账"""
    r = _realm(db)
    _level(db, 2, 10)
    u = _user(db, "u1", points=1000, level=2)
    plan = _plan(db, r.id, points_price="400", days=30)
    sub = _active_sub(db, u, plan, r, hours_left=12)
    old_end = sub.end_date
    out = subs.auto_renew_subscriptions(db)
    assert out["renewed"] == 1 and out["failed"] == 0
    assert out["notices"] == []
    db.refresh(sub)
    assert sub.end_date == old_end + timedelta(days=30)
    assert _balance(db, u.id) == 640  # 400 九折 = 360
    order = db.query(models.SubscriptionOrder).filter(
        models.SubscriptionOrder.payment_method == "auto_renew").first()
    assert order is not None and order.status == "paid"
    assert order.subscription_id == sub.id
    log = db.query(models.PointsLog).filter(
        models.PointsLog.type == "subscription_auto_renew").first()
    assert log is not None and log.amount == -360


def test_auto_renew_insufficient_balance_notifies_once(db):
    """余额不足：不续订，落去重记录并返回一次通知；再跑不再打扰"""
    r = _realm(db)
    u = _user(db, "u1", points=10)
    plan = _plan(db, r.id, points_price="400", days=30)
    sub = _active_sub(db, u, plan, r, hours_left=12)
    old_end = sub.end_date
    out = subs.auto_renew_subscriptions(db)
    assert out["renewed"] == 0 and out["failed"] == 1
    assert len(out["notices"]) == 1
    assert out["notices"][0]["user_id"] == u.id
    db.refresh(sub)
    assert sub.end_date == old_end  # 没动
    assert _balance(db, u.id) == 10
    # 第二轮：去重记录已在，不再出通知
    out2 = subs.auto_renew_subscriptions(db)
    assert out2["failed"] == 1 and out2["notices"] == []


def test_auto_renew_idempotent_no_double_renew(db):
    """重复执行不双续：续完后 end_date 已推出 24h 窗口"""
    r = _realm(db)
    u = _user(db, "u1", points=10000)
    plan = _plan(db, r.id, points_price="400", days=30)
    _active_sub(db, u, plan, r, hours_left=12)
    out1 = subs.auto_renew_subscriptions(db)
    assert out1["renewed"] == 1
    out2 = subs.auto_renew_subscriptions(db)
    assert out2["renewed"] == 0
    assert _balance(db, u.id) == 10000 - 400
    assert db.query(models.SubscriptionOrder).filter(
        models.SubscriptionOrder.payment_method == "auto_renew").count() == 1


def test_auto_renew_skipped_when_no_points_price(db):
    """套餐无积分价：跳过（人民币自动扣款未开通），不报错不打扰"""
    r = _realm(db)
    u = _user(db, "u1", points=10000)
    plan = _plan(db, r.id, points_price=None, days=30)
    _active_sub(db, u, plan, r, hours_left=12)
    out = subs.auto_renew_subscriptions(db)
    assert out["skipped"] == 1 and out["renewed"] == 0
    assert out["notices"] == []


def test_auto_renew_outside_window_untouched(db):
    """还有 30 天到期的订阅不在窗口内，不动"""
    r = _realm(db)
    u = _user(db, "u1", points=10000)
    plan = _plan(db, r.id, points_price="400", days=30)
    _active_sub(db, u, plan, r, hours_left=24 * 30)
    out = subs.auto_renew_subscriptions(db)
    assert out == {"renewed": 0, "failed": 0, "skipped": 0, "errors": 0,
                   "notices": []}


def test_auto_renew_toggle_endpoint(db):
    """开关接口：开/关；无积分价套餐拒绝开启；已失效拒绝开启"""
    r = _realm(db)
    u = _user(db, "u1", points=100)
    plan = _plan(db, r.id, points_price="400")
    sub = _active_sub(db, u, plan, r, auto_renew=False)
    res = set_my_subscription_auto_renew(
        sub.id, AutoRenewRequest(enabled=True), current_user=u, db=db)
    assert res["auto_renew"] is True
    res = set_my_subscription_auto_renew(
        sub.id, AutoRenewRequest(enabled=False), current_user=u, db=db)
    assert res["auto_renew"] is False

    plan2 = _plan(db, r.id, name="无积分价", points_price=None)
    sub2 = _active_sub(db, u, plan2, r, auto_renew=False)
    with pytest.raises(HTTPException) as ei:
        set_my_subscription_auto_renew(
            sub2.id, AutoRenewRequest(enabled=True), current_user=u, db=db)
    assert ei.value.status_code == 400
    assert "人民币自动扣款尚未开通" in ei.value.detail

    # 别人的订阅动不了
    other = _user(db, "u2", points=100)
    with pytest.raises(HTTPException) as ei2:
        set_my_subscription_auto_renew(
            sub.id, AutoRenewRequest(enabled=True), current_user=other, db=db)
    assert ei2.value.status_code == 404


def test_auto_renew_clears_failed_notice_after_success(db):
    """失败提醒过一次后，用户补足能量、下一轮续订成功：提醒记录被清掉"""
    r = _realm(db)
    u = _user(db, "u1", points=10)
    plan = _plan(db, r.id, points_price="400", days=30)
    sub = _active_sub(db, u, plan, r, hours_left=12)
    out1 = subs.auto_renew_subscriptions(db)
    assert out1["failed"] == 1 and len(out1["notices"]) == 1
    # 用户充值
    u.points = 1000
    db.commit()
    out2 = subs.auto_renew_subscriptions(db)
    assert out2["renewed"] == 1
    # 提醒记录随续订被清掉（_grant_subscription 的清理逻辑）
    left = db.query(models.SubscriptionReminder).filter(
        models.SubscriptionReminder.subscription_id == sub.id).count()
    assert left == 0
