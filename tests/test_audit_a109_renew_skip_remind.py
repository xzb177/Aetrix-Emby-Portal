# -*- coding: utf-8 -*-
"""审计 A-109：自动续订遇套餐下架/无积分价时静默跳过，无失败提醒。

缺口：subscriptions._auto_renew_one 里 ``plan is None / not is_active /
points_price is None`` 分支仅 logger.info 后 return "skipped"；而余额
不足、能量购买通道关闭、来源限定不足三路都落 renew_failed 去重记录
并发提醒——此路独缺。用户开启自动续订时校验过套餐在售，但套餐可在
开启后被下架，用户仍以为自动续订会处理，到期才发现断了。

本文件钉住：套餐下架后进入续订窗口的订阅 → 产出 renew_failed 去重
提醒（SubscriptionReminder 行 + notice）、不扣款、不顺延；再跑一轮
不再重复提醒。
"""
import os

os.environ.setdefault("DATABASE_TYPE", "sqlite")
os.environ.setdefault("REDIS_ENABLED", "false")

from datetime import datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend import models
from backend.database import Base
import backend.emby_server.models as em_models  # noqa: F401  注册相关表
from backend import subscriptions as subs
from backend.integrations import store


@pytest.fixture()
def db():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    # realm_config 走 store 进程级 TTL 缓存（只按键名、不分库）：进场
    # 全清防邻居测试文件的缓存串味，退场再清（同
    # test_subscription_points_gate 的 fixture 口径）。
    store.invalidate()
    yield session
    session.close()
    store.invalidate()


def _realm(db, slug="main", access_mode="paid"):
    r = models.ServerRealm(name=slug, slug=slug, access_mode=access_mode,
                           is_active=True)
    db.add(r)
    db.commit()
    db.refresh(r)
    return r


def _user(db, username):
    u = models.WebUser(username=username, password_hash="x",
                       is_active=True, points=0, member_level=1)
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


def _plan(db, realm_id, points_price=400):
    p = models.SubscriptionPlan(
        name="月卡", price=Decimal("18.00"),
        points_price=Decimal(points_price) if points_price is not None else None,
        duration_days=30, is_active=True, realm_id=realm_id)
    db.add(p)
    db.commit()
    db.refresh(p)
    return p


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


def _balance(db, uid):
    db.expire_all()
    return db.query(models.WebUser.points).filter(
        models.WebUser.id == uid).scalar() or 0


def test_auto_renew_delisted_plan_reminds_instead_of_silent_skip(db):
    """套餐在开启自动续订后被下架：走 renew_failed 提醒，不静默跳过。"""
    r = _realm(db)
    plan = _plan(db, r.id, points_price=400)
    u = _user(db, "renew_delisted")
    _log(db, u, 1000, "checkin")
    sub = _active_sub(db, u, plan, r)
    old_end = sub.end_date
    # 套餐在用户开启自动续订之后被下架
    plan.is_active = False
    db.commit()

    out = subs.auto_renew_subscriptions(db)
    assert out["renewed"] == 0
    assert out["skipped"] == 0
    assert out["failed"] == 1
    assert len(out["notices"]) == 1
    notice = out["notices"][0]
    assert "下架" in notice["title"] + notice["content"]
    assert "未执行" in notice["title"] + notice["content"]

    # 提醒落了去重行；未扣款、未顺延、无续订台账
    rows = db.query(models.SubscriptionReminder).filter(
        models.SubscriptionReminder.subscription_id == sub.id,
        models.SubscriptionReminder.kind == subs.RENEW_FAILED_KIND).all()
    assert len(rows) == 1
    db.refresh(sub)
    assert sub.end_date == old_end
    assert _balance(db, u.id) == 1000
    assert db.query(models.PointsLog).filter(
        models.PointsLog.type == "subscription_auto_renew").count() == 0

    # 去重：再跑一轮不再重复提醒
    out2 = subs.auto_renew_subscriptions(db)
    assert out2["notices"] == []
