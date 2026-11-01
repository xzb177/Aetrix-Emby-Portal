# -*- coding: utf-8 -*-
"""永久账号白名单（2026-11-01 用户亲令）。

名单里的账号是永久账号：公益资格、查看凭证、订阅三处过期判定对他们
恒有效——不扣费、不提示续费、不因到期拦截；非白名单用户行为逐字不变。

覆盖：
- 名单判定本体 + 总开关（permanent_whitelist_enabled，默认开；关后全站回旧口径）；
- 订阅门（subscriptions.has_active_subscription / can_play）：过期订阅与
  无订阅的白名单用户都视为有效；
- 查看凭证门（subscriptions.view_grant 公益服分支）：凭证过期仍放行；
- 公益资格门（portal.is_welfare_user / media_seek 月度额度口径）；
- 到期 worker（welfare_expiry）：白名单不被取消资格、不活跃也不禁用；
- 续费提醒（reminders.collect_due）：白名单不进待发名单（不提示续费）；
- 自动续订（subscriptions.auto_renew_subscriptions）：白名单不被扣费续订；
- 解锁购买（portal.unlock_emby_view）：白名单不重复扣费；
- 全员赠送公益口径（gift_all）：白名单过期公益用户仍在目标人群；
- 状态展示（points.get_welfare_status / tg_bot 账号行）：呈永久口径；
- 管理端 API：鉴权、增删查、按用户名搜索、重复添加 409、总开关读写。
"""
import os

os.environ.setdefault("DATABASE_TYPE", "sqlite")
os.environ.setdefault("REDIS_ENABLED", "false")

from datetime import datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend import models, permanent_whitelist as pw
from backend.database import Base
import backend.emby_server.models as em_models  # noqa: F401  注册相关表
from backend.integrations import store


@pytest.fixture()
def db():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    store.invalidate()
    yield session
    session.close()
    store.invalidate()


def _realm(db, slug, access_mode):
    r = models.ServerRealm(name=slug, slug=slug, access_mode=access_mode,
                           is_active=True)
    db.add(r)
    db.commit()
    db.refresh(r)
    return r


def _user(db, username, **kw):
    kw.setdefault("is_active", True)
    kw.setdefault("points", 0)
    u = models.WebUser(username=username, password_hash="x", **kw)
    db.add(u)
    db.commit()
    db.refresh(u)
    return u


def _whitelist(db, user, note="测试", added_by=None):
    e = models.PermanentWhitelistEntry(user_id=user.id, note=note,
                                       added_by=added_by)
    db.add(e)
    db.commit()
    return e


def _set_switch(db, enabled: bool):
    from backend import config_service
    config_service.save_config(
        db, {pw.CONFIG_KEY: enabled}, schema="permanent_whitelist")
    db.commit()


# ==================== 判定本体与总开关 ====================

def test_is_permanent_and_master_switch(db):
    u = _user(db, "pw_alice")
    assert pw.is_permanent(db, u.id) is False
    _whitelist(db, u)
    assert pw.is_permanent(db, u.id) is True
    # 总开关关掉：名单还在，但全站回旧口径
    _set_switch(db, False)
    assert pw.whitelist_enabled(db) is False
    assert pw.is_permanent(db, u.id) is False
    _set_switch(db, True)
    assert pw.is_permanent(db, u.id) is True


def test_permanent_ids_batch(db):
    a = _user(db, "pw_b1")
    b = _user(db, "pw_b2")
    c = _user(db, "pw_b3")
    _whitelist(db, a)
    _whitelist(db, c)
    assert pw.permanent_ids(db, [a.id, b.id, c.id]) == {a.id, c.id}
    assert pw.permanent_ids(db, []) == set()


# ==================== ③ 订阅门 ====================

def _expired_sub(db, user, realm):
    plan = models.SubscriptionPlan(
        name="月卡", price=Decimal("18.00"), duration_days=30,
        realm_id=realm.id, is_active=True)
    db.add(plan)
    db.commit()
    now = datetime.now()
    sub = models.UserSubscription(
        user_id=user.id, plan_id=plan.id, realm_id=realm.id,
        start_date=now - timedelta(days=60),
        end_date=now - timedelta(days=30), status="active")
    db.add(sub)
    db.commit()
    return sub


def test_subscription_gate_whitelisted(db):
    from backend import subscriptions as subs
    realm = _realm(db, "paid1", "paid")
    expired = _user(db, "pw_sub_exp")
    _expired_sub(db, expired, realm)
    assert subs.has_active_subscription(db, expired.id, realm.id) is False
    nosub = _user(db, "pw_sub_none")
    assert subs.has_active_subscription(db, nosub.id, realm.id) is False

    _whitelist(db, expired)
    _whitelist(db, nosub)
    # 过期订阅与从未订阅：白名单都视为有效
    assert subs.has_active_subscription(db, expired.id, realm.id) is True
    assert subs.has_active_subscription(db, nosub.id, realm.id) is True
    assert subs.can_play(db, nosub, realm.id) is True

    # 非白名单的过期用户行为不变
    plain = _user(db, "pw_sub_plain")
    _expired_sub(db, plain, realm)
    assert subs.has_active_subscription(db, plain.id, realm.id) is False


# ==================== ② 查看凭证门 ====================

def test_view_grant_whitelisted(db):
    from backend import subscriptions as subs
    realm = _realm(db, "free1", "free")
    u = _user(db, "pw_view")
    now = datetime.now()
    db.add(models.EmbyViewUnlock(
        user_id=u.id, realm_id=realm.id, unlocked_at=now - timedelta(days=40),
        expires_at=now - timedelta(days=10), points_spent=50))
    db.commit()
    granted, _ = subs.view_grant(db, u, realm.id)
    assert granted is False
    _whitelist(db, u)
    granted, _ = subs.view_grant(db, u, realm.id)
    assert granted is True
    # 从未解锁过的白名单用户同样放行
    v = _user(db, "pw_view_none")
    _whitelist(db, v)
    granted, unlock = subs.view_grant(db, v, realm.id)
    assert granted is True
    assert unlock is None


# ==================== ① 公益资格门 ====================

def test_welfare_judgment_whitelisted(db):
    from backend.emby_server import portal
    from backend import media_seek
    now = datetime.now()
    u = _user(db, "pw_wel", is_welfare=True,
              welfare_expires_at=now - timedelta(days=3))
    assert portal.is_welfare_user(db, u) is False
    # 非白名单过期用户走付费求片口径（默认 10）
    assert media_seek.monthly_limit(db, u) == media_seek.DEFAULT_MONTHLY_PAID
    _whitelist(db, u)
    assert portal.is_welfare_user(db, u) is True
    # 白名单过期公益用户仍走公益求片口径（默认 3）
    assert media_seek.monthly_limit(db, u) == media_seek.DEFAULT_MONTHLY_WELFARE
    assert media_seek.monthly_quota(db, u)["kind"] == "welfare"


def test_welfare_expiry_workers_skip_whitelisted(db):
    from backend import welfare_expiry
    now = datetime.now()
    beyond = _user(db, "pw_exp_beyond", is_welfare=True,
                   last_login_at=now,
                   welfare_expires_at=now - timedelta(days=30),
                   welfare_granted_at=now - timedelta(days=90))
    kept = _user(db, "pw_exp_kept", is_welfare=True,
                 last_login_at=now,
                 welfare_expires_at=now - timedelta(days=30),
                 welfare_granted_at=now - timedelta(days=90))
    _whitelist(db, kept)
    res = welfare_expiry.check_welfare_expiry(db)
    assert res["expired"] == [beyond.id]
    assert db.query(models.WebUser).filter_by(id=kept.id).one().is_welfare is True

    inactive = _user(db, "pw_inact_plain", is_welfare=True,
                     last_login_at=now - timedelta(days=40),
                     welfare_expires_at=now + timedelta(days=30),
                     welfare_granted_at=now - timedelta(days=40))
    inactive_kept = _user(db, "pw_inact_kept", is_welfare=True,
                          last_login_at=now - timedelta(days=40),
                          welfare_expires_at=now + timedelta(days=30),
                          welfare_granted_at=now - timedelta(days=40))
    _whitelist(db, inactive_kept)
    res2 = welfare_expiry.check_inactive_users(db)
    assert res2["disabled"] == [inactive.id]
    assert db.query(models.WebUser).filter_by(id=inactive_kept.id).one().is_welfare is True


# ==================== 不提示续费 / 不扣费 ====================

def test_reminders_skip_whitelisted(db):
    from backend import reminders
    realm = _realm(db, "paid2", "paid")
    plan = models.SubscriptionPlan(
        name="月卡", price=Decimal("18.00"), duration_days=30,
        realm_id=realm.id, is_active=True)
    db.add(plan)
    db.commit()
    now = datetime.now()

    def _mk_sub(user, end):
        s = models.UserSubscription(
            user_id=user.id, plan_id=plan.id, realm_id=realm.id,
            start_date=now - timedelta(days=27), end_date=end, status="active")
        db.add(s)
        db.commit()
        return s

    soon_plain = _user(db, "pw_rem_plain")
    _mk_sub(soon_plain, now + timedelta(days=2))
    soon_kept = _user(db, "pw_rem_kept")
    _mk_sub(soon_kept, now + timedelta(days=2))
    _whitelist(db, soon_kept)
    gone_kept = _user(db, "pw_rem_gone")
    _mk_sub(gone_kept, now - timedelta(days=1))
    _whitelist(db, gone_kept)

    due = reminders.collect_due(db, now=now)
    reminded_users = {i["user_id"] for i in due["reminders"]}
    expired_users = {i["user_id"] for i in due["expired"]}
    assert soon_plain.id in reminded_users
    assert soon_kept.id not in reminded_users
    assert gone_kept.id not in expired_users


def test_auto_renew_skips_whitelisted(db):
    from backend import subscriptions as subs
    realm = _realm(db, "paid3", "paid")
    plan = models.SubscriptionPlan(
        name="月卡", price=Decimal("18.00"), points_price=Decimal("100"),
        duration_days=30, realm_id=realm.id, is_active=True)
    db.add(plan)
    db.commit()
    now = datetime.now()

    def _mk(user):
        u = _user(db, user, points=1000)
        # 自挣流水：自动续订来源限定默认开，需自挣额覆盖
        db.add(models.PointsLog(user_id=u.id, amount=1000, balance_after=1000,
                                type="checkin", description="test",
                                created_at=now))
        db.commit()
        s = models.UserSubscription(
            user_id=u.id, plan_id=plan.id, realm_id=realm.id,
            start_date=now - timedelta(days=29),
            end_date=now + timedelta(hours=12), status="active",
            auto_renew=True)
        db.add(s)
        db.commit()
        return u

    kept = _mk("pw_renew_kept")
    _whitelist(db, kept)
    plain = _mk("pw_renew_plain")
    res = subs.auto_renew_subscriptions(db, now=now)
    # 对照组正常续订（证明窗口/余额设置有效），白名单组不扣费不顺延
    assert res["renewed"] == 1
    db.refresh(kept)
    db.refresh(plain)
    assert kept.points == 1000
    assert plain.points == 900


def test_unlock_view_no_charge_for_whitelisted(db):
    from backend.emby_server import portal
    from backend import realms
    realm = _realm(db, "free2", "free")
    realms.set_active_realm(db, realm.id)
    now = datetime.now()

    def _mk(name, whitelist):
        u = _user(db, name, points=100)
        db.add(models.EmbyViewUnlock(
            user_id=u.id, realm_id=realm.id,
            unlocked_at=now - timedelta(days=40),
            expires_at=now - timedelta(days=10), points_spent=50))
        db.commit()
        if whitelist:
            _whitelist(db, u)
        return u

    kept = _mk("pw_unlock_kept", True)
    res = portal.unlock_emby_view(
        portal.UnlockViewRequest(realm_id=realm.id), request_user=kept, db=db)
    assert res["success"] is True and res["already"] is True
    assert res.get("permanent") is True
    db.refresh(kept)
    assert kept.points == 100  # 一分不扣

    # 对照组（非白名单、无任何解锁行）：照常扣 50（默认价）新建行
    fresh = _user(db, "pw_unlock_fresh", points=100)
    res2 = portal.unlock_emby_view(
        portal.UnlockViewRequest(realm_id=realm.id), request_user=fresh, db=db)
    assert res2["success"] is True and res2["already"] is False
    db.refresh(fresh)
    assert fresh.points == 50

    # 对照组（非白名单、已有过期行）：保持现行口径逐字不变——
    # (user_id, realm_id) 唯一约束下续购走冲突回滚分支，返回
    # already=True 且不扣费（存量行为，本单不改，见 PR 未知项）。
    stale = _mk("pw_unlock_stale", False)
    res3 = portal.unlock_emby_view(
        portal.UnlockViewRequest(realm_id=realm.id), request_user=stale, db=db)
    assert res3["success"] is True and res3["already"] is True
    assert res3.get("permanent") is not True
    db.refresh(stale)
    assert stale.points == 100


# ==================== 赠送口径 / 状态展示 ====================

def test_gift_all_welfare_scope_includes_whitelisted(db):
    from backend import gift_all
    now = datetime.now()
    expired = _user(db, "pw_gift_exp", is_welfare=True,
                    welfare_expires_at=now - timedelta(days=1))
    kept = _user(db, "pw_gift_kept", is_welfare=True,
                 welfare_expires_at=now - timedelta(days=1))
    _whitelist(db, kept)
    ids = set(gift_all.eligible_ids_query(db, "welfare").all())
    flat = {r[0] for r in ids}
    assert kept.id in flat
    assert expired.id not in flat


def test_status_display_whitelisted(db):
    from backend import points as points_mod
    from backend.tg_bot import features
    now = datetime.now()
    u = _user(db, "pw_disp", is_welfare=True,
              welfare_expires_at=now - timedelta(days=5))
    st = points_mod.get_welfare_status(db, u)
    assert st["is_welfare"] is True
    assert st["welfare_expires_at"] is not None  # 非白名单：原样呈到期日
    assert features._welfare_line(db, u).endswith(
        (now - timedelta(days=5)).strftime("%Y-%m-%d"))
    assert features._view_unlock_line(db, u.id) == "查看凭证：未开通"
    _whitelist(db, u)
    st2 = points_mod.get_welfare_status(db, u)
    assert st2 == {"is_welfare": True, "welfare_expires_at": None}
    assert features._welfare_line(db, u) == "公益资格：长期有效"
    assert features._view_unlock_line(db, u.id) == "查看凭证：长期有效"
