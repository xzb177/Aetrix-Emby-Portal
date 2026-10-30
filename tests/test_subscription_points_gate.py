# -*- coding: utf-8 -*-
"""两服共存 P1-4＋P1-5：能量买订阅来源限定＋购买开关按服化。

钉住的口径（2026-10-30 定版，执行计划第四、五节）：
1. P1-4 手动购买：能量买订阅在折后实付定下后、扣款前判定来源——
   开关 subscription_buy_self_earned_only（默认开）时，自挣能量
   （points.self_earned_balance，#1550 的全仓唯一分类实现）不足以
   覆盖折后实付 → 400 明确报错（说清是来源不够、不是余额不够）。
   判定按折后实付，不按原价。
2. P1-4 自动续订侧门：无人值守续订同样判来源，不足时不扣款、
   走现成的 renew_failed 通知路径；该服能量购买通道关着同样停续。
3. 独立开关 subscription_points_purchase_enabled（默认开）：该服
   能量购买通道总开关，只拦能量下单口，在线支付不受影响。
4. P1-5：subscription_purchase_enabled 按服化——支付方式呈现、
   套餐列表、下单硬拦三处读点改走 realms.realm_config；A 服关
   不影响 B 服；默认服（legacy）仍读原键、行为与改前一致。
   recharge_enabled / exchange_enabled 不按服（计划裁定，不在本单）。
5. 两个新开关注册三处齐：config_schema.ECONOMY_CONFIG_SCHEMA、
   admin_economy.ECONOMY_CONFIG_KEYS（回包/保存/审计同一通道）、
   消费侧缺省 true；管理端 PUT 改值 → GET 回读为新值。
"""
import os

os.environ.setdefault("DATABASE_TYPE", "sqlite")
os.environ.setdefault("REDIS_ENABLED", "false")

import asyncio
from datetime import datetime, timedelta
from decimal import Decimal

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend import models, realms
from backend.database import Base
import backend.emby_server.models as em_models  # noqa: F401  注册相关表
from backend import subscriptions as subs
from backend.api import admin_economy, economy
from backend.integrations import store
from backend import ratelimit


@pytest.fixture()
def db():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    # realm_config 走 store 进程级 TTL 缓存（只按键名、不分库）：进场
    # 全清防邻居测试文件的缓存串味，退场再清，别污染后面的人。
    store.invalidate()
    ratelimit._limiter._hits.clear()
    yield session
    session.close()
    store.invalidate()
    ratelimit._limiter._hits.clear()


def _set_config(db, key, value):
    cfg = db.query(models.SystemConfig).filter(models.SystemConfig.key == key).first()
    if cfg:
        cfg.value = str(value)
    else:
        db.add(models.SystemConfig(key=key, value=str(value)))
    db.commit()
    store.invalidate(key)  # 与生产写时失效同口径


def _gateway(db):
    _set_config(db, "payment_gateway_url", "https://pay.example")
    _set_config(db, "payment_partner_id", "1")
    _set_config(db, "payment_partner_key", "k")


def _realm(db, slug="main", access_mode="paid"):
    r = models.ServerRealm(name=slug, slug=slug, access_mode=access_mode,
                           is_active=True)
    db.add(r)
    db.commit()
    db.refresh(r)
    return r


def _user(db, username, level=1):
    u = models.WebUser(username=username, password_hash="x",
                       is_active=True, points=0, member_level=level)
    db.add(u)
    db.commit()
    db.refresh(u)
    return u


def _log(db, user, amount, type_):
    """写一条积分流水并同步余额（保持 台账合计 == 余额 的一致性）。"""
    new_bal = int(user.points or 0) + amount
    db.add(models.PointsLog(user_id=user.id, amount=amount,
                            balance_after=new_bal, type=type_,
                            description="test", created_at=datetime.now()))
    user.points = new_bal
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def _plan(db, realm_id, points_price=400, price="18.00", days=30, name="月卡"):
    p = models.SubscriptionPlan(
        name=name, price=Decimal(price),
        points_price=Decimal(points_price) if points_price is not None else None,
        duration_days=days, is_active=True, realm_id=realm_id)
    db.add(p)
    db.commit()
    db.refresh(p)
    return p


def _level(db, level, discount_pct):
    db.add(models.MemberLevel(level=level, name=f"Lv{level}", xp_threshold=0,
                              discount_pct=discount_pct, is_active=True))
    db.commit()


def _buy(db, user, plan, pay_with_points=True):
    req = economy.CreateOrderRequest(
        kind="subscription", item_id=plan.id,
        payment_method="alipay", pay_with_points=pay_with_points)
    return economy.create_payment_order(None, req, current_user=user, db=db)


def _balance(db, uid):
    db.expire_all()
    return db.query(models.WebUser.points).filter(
        models.WebUser.id == uid).scalar() or 0


# ==================== 1. 手动购买来源限定 ====================

def test_points_buy_rejected_when_balance_not_self_earned(db):
    """余额充足但全是转入：开关默认开 → 400 且文案说清是来源问题，不扣款。"""
    _gateway(db)
    r = _realm(db)
    plan = _plan(db, r.id, points_price=400)
    u = _user(db, "rich_transferred")
    _log(db, u, 1000, "transfer_in")
    with pytest.raises(HTTPException) as ei:
        _buy(db, u, plan)
    assert ei.value.status_code == 400
    assert "自挣" in ei.value.detail
    assert _balance(db, u.id) == 1000
    assert db.query(models.UserSubscription).count() == 0
    assert db.query(models.PointsLog).filter(
        models.PointsLog.type == "subscription_buy").count() == 0


def test_points_buy_succeeds_with_self_earned(db):
    """自挣充足：正常购买、按实付扣款、订阅开通。"""
    _gateway(db)
    r = _realm(db)
    plan = _plan(db, r.id, points_price=400)
    u = _user(db, "hard_worker")
    _log(db, u, 500, "checkin")
    res = _buy(db, u, plan)
    assert res["success"] is True and res["points_cost"] == 400
    assert _balance(db, u.id) == 100
    assert db.query(models.UserSubscription).filter(
        models.UserSubscription.user_id == u.id).count() == 1


def test_points_buy_allowed_when_self_earned_gate_off(db):
    """开关关：任意来源可买（转入能量照常购买）。"""
    _gateway(db)
    r = _realm(db)
    _set_config(db, "subscription_buy_self_earned_only", "false")
    plan = _plan(db, r.id, points_price=400)
    u = _user(db, "gate_off")
    _log(db, u, 1000, "transfer_in")
    res = _buy(db, u, plan)
    assert res["success"] is True
    assert _balance(db, u.id) == 600


def test_points_buy_discount_judged_on_paid_amount(db):
    """折扣按折后实付判：原价 400、九折实付 360；自挣 380 放行、350 拦。"""
    _gateway(db)
    r = _realm(db)
    _level(db, 2, 10)
    plan = _plan(db, r.id, points_price=400)
    ok = _user(db, "discount_ok", level=2)
    _log(db, ok, 380, "checkin")
    res = _buy(db, ok, plan)
    assert res["success"] is True and res["points_cost"] == 360
    assert _balance(db, ok.id) == 20

    ng = _user(db, "discount_ng", level=2)
    _log(db, ng, 350, "chat")
    with pytest.raises(HTTPException) as ei:
        _buy(db, ng, plan)
    assert ei.value.status_code == 400
    assert "自挣" in ei.value.detail
    assert _balance(db, ng.id) == 350


def test_points_channel_disabled_rejects_points_order_only(db):
    """能量通道关：能量下单 403；在线支付下单不受影响。"""
    _gateway(db)
    r = _realm(db)
    _set_config(db, "subscription_points_purchase_enabled", "false")
    plan = _plan(db, r.id, points_price=400)
    u = _user(db, "channel_off")
    _log(db, u, 1000, "checkin")
    with pytest.raises(HTTPException) as ei:
        _buy(db, u, plan, pay_with_points=True)
    assert ei.value.status_code == 403
    assert _balance(db, u.id) == 1000
    res = _buy(db, u, plan, pay_with_points=False)
    assert res["success"] is True
    order = db.query(models.SubscriptionOrder).filter(
        models.SubscriptionOrder.user_id == u.id).first()
    assert order is not None and order.status == "pending"


# ==================== 2. 自动续订来源限定 ====================

def _active_sub(db, user, plan, realm, hours_left=12):
    now = datetime.now()
    s = models.UserSubscription(
        user_id=user.id, plan_id=plan.id, realm_id=realm.id,
        start_date=now - timedelta(days=20),
        end_date=now + timedelta(hours=hours_left),
        status="active", auto_renew=True)
    db.add(s)
    db.commit()
    db.refresh(s)
    return s


def test_auto_renew_blocked_when_not_self_earned(db):
    """续订侧门：余额足但全是转入 → 不扣款、不续，走 renew_failed 通知。"""
    r = _realm(db)
    plan = _plan(db, r.id, points_price=400)
    u = _user(db, "renew_transferred")
    _log(db, u, 1000, "transfer_in")
    sub = _active_sub(db, u, plan, r)
    old_end = sub.end_date
    out = subs.auto_renew_subscriptions(db)
    assert out["renewed"] == 0 and out["failed"] == 1
    assert len(out["notices"]) == 1
    assert "自挣" in out["notices"][0]["title"] + out["notices"][0]["content"]
    db.refresh(sub)
    assert sub.end_date == old_end
    assert _balance(db, u.id) == 1000
    assert db.query(models.PointsLog).filter(
        models.PointsLog.type == "subscription_auto_renew").count() == 0


def test_auto_renew_succeeds_with_self_earned(db):
    """自挣充足：自动续订正常扣款顺延。"""
    r = _realm(db)
    plan = _plan(db, r.id, points_price=400)
    u = _user(db, "renew_worker")
    _log(db, u, 1000, "checkin")
    sub = _active_sub(db, u, plan, r)
    old_end = sub.end_date
    out = subs.auto_renew_subscriptions(db)
    assert out["renewed"] == 1 and out["failed"] == 0
    db.refresh(sub)
    assert sub.end_date == old_end + timedelta(days=30)
    assert _balance(db, u.id) == 600


def test_auto_renew_allowed_when_gate_off(db):
    """来源限定关：转入能量也可自动续订。"""
    r = _realm(db)
    _set_config(db, "subscription_buy_self_earned_only", "false")
    plan = _plan(db, r.id, points_price=400)
    u = _user(db, "renew_gate_off")
    _log(db, u, 1000, "transfer_in")
    _active_sub(db, u, plan, r)
    out = subs.auto_renew_subscriptions(db)
    assert out["renewed"] == 1
    assert _balance(db, u.id) == 600


def test_auto_renew_blocked_when_points_channel_disabled(db):
    """该服能量购买通道关：自动续订停扣，走 renew_failed 通知路径。"""
    r = _realm(db)
    _set_config(db, "subscription_points_purchase_enabled", "false")
    plan = _plan(db, r.id, points_price=400)
    u = _user(db, "renew_channel_off")
    _log(db, u, 1000, "checkin")
    sub = _active_sub(db, u, plan, r)
    old_end = sub.end_date
    out = subs.auto_renew_subscriptions(db)
    assert out["renewed"] == 0 and out["failed"] == 1
    assert len(out["notices"]) == 1
    db.refresh(sub)
    assert sub.end_date == old_end
    assert _balance(db, u.id) == 1000


# ==================== 3. 购买开关按服化 ====================

def test_purchase_switch_scoped_per_realm(db):
    """A 服（默认服）开、B 服关：B 服列表不呈现且下单被拦，A 服照常。"""
    _gateway(db)
    ra = _realm(db, slug="main")
    rb = _realm(db, slug="paidb")
    plan_a = _plan(db, ra.id, name="A月卡")
    plan_b = _plan(db, rb.id, name="B月卡")
    realms.set_realm_config(db, "subscription_purchase_enabled", "false",
                            realm_id=rb.id)
    db.commit()

    listed_b = economy.payment_plans(realm_id=rb.id, db=db)
    assert listed_b["enabled"] is False and listed_b["plans"] == []
    listed_a = economy.payment_plans(realm_id=ra.id, db=db)
    assert listed_a["enabled"] is True
    assert [p["id"] for p in listed_a["plans"]] == [plan_a.id]

    u = _user(db, "realm_buyer")
    _log(db, u, 1000, "checkin")
    with pytest.raises(HTTPException) as ei:
        _buy(db, u, plan_b)
    assert ei.value.status_code == 403
    res = _buy(db, u, plan_a)
    assert res["success"] is True

    # 支付方式呈现跟当前服（默认服=A，开）走；把默认服的开关关掉后全灭
    methods = asyncio.run(economy.payment_methods(db=db))
    assert all(m.enabled for m in methods)
    _set_config(db, "subscription_purchase_enabled", "false")
    methods = asyncio.run(economy.payment_methods(db=db))
    assert methods and all(not m.enabled for m in methods)


def test_purchase_switch_legacy_realm_default_unchanged(db):
    """默认服无任何配置行时行为与改前一致：列表开、能量购买可下单。"""
    _gateway(db)
    r = _realm(db)
    plan = _plan(db, r.id, points_price=400)
    listed = economy.payment_plans(realm_id=r.id, db=db)
    assert listed["enabled"] is True and len(listed["plans"]) == 1
    u = _user(db, "legacy_buyer")
    _log(db, u, 500, "checkin")
    res = _buy(db, u, plan)
    assert res["success"] is True


# ==================== 4. 管理端注册与回读往返 ====================

def _admin(db):
    admin = models.WebUser(username="admin", password_hash="x", is_staff=True)
    db.add(admin)
    db.commit()
    db.refresh(admin)
    return admin


def test_admin_settings_defaults_and_roundtrip(db):
    """新两键：无行时回包缺省 true；PUT 改值 → 落库 → GET 回读为新值；
    按服化的 subscription_purchase_enabled 同通道往返。"""
    admin = _admin(db)
    got = admin_economy.economy_get_settings(current_admin=admin, db=db)["settings"]
    assert got["subscription_buy_self_earned_only"] == "true"
    assert got["subscription_points_purchase_enabled"] == "true"

    res = admin_economy.economy_update_settings(
        admin_economy.EconomySettingsRequest(settings={
            "subscription_buy_self_earned_only": "false",
            "subscription_points_purchase_enabled": "false",
            "subscription_purchase_enabled": "false",
        }), current_admin=admin, db=db)
    assert set(res["changed"]) >= {
        "subscription_buy_self_earned_only",
        "subscription_points_purchase_enabled",
        "subscription_purchase_enabled",
    }
    got = admin_economy.economy_get_settings(current_admin=admin, db=db)["settings"]
    assert got["subscription_buy_self_earned_only"] == "false"
    assert got["subscription_points_purchase_enabled"] == "false"
    assert got["subscription_purchase_enabled"] == "false"

    res = admin_economy.economy_update_settings(
        admin_economy.EconomySettingsRequest(settings={
            "subscription_buy_self_earned_only": "true",
        }), current_admin=admin, db=db)
    assert res["changed"] == ["subscription_buy_self_earned_only"]
    got = admin_economy.economy_get_settings(current_admin=admin, db=db)["settings"]
    assert got["subscription_buy_self_earned_only"] == "true"
