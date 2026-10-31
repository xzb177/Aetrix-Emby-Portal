"""A-107：管理员延长订阅不清提醒去重行，新周期到期提醒被旧行压住。

回归：extend_subscription 延长后，该订阅的 SubscriptionReminder 行应被清掉
（与 _grant_subscription 顺延同口径）；新周期 7d/3d/1d/expired 四档窗口
collect_due 均重新产出提醒。退款回滚（缩短 end_date）不适用此清理，不在本单。
"""

import os

os.environ.setdefault("DATABASE_TYPE", "sqlite")
os.environ.setdefault("REDIS_ENABLED", "false")

from datetime import datetime, timedelta

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend import models
from backend import reminders
from backend.api.admin_core import admin_router, get_current_admin
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


def _make_user(db, username):
    u = models.WebUser(username=username, password_hash="test", points=1000,
                       is_active=True, is_staff=True, member_xp=0, member_level=1)
    db.add(u)
    db.commit()
    return u


def _make_sub(db, user, days_left=5):
    plan = models.SubscriptionPlan(name="P30", price=100, duration_days=30)
    db.add(plan)
    db.commit()
    now = datetime.now()
    sub = models.UserSubscription(
        user_id=user.id, plan_id=plan.id, realm_id=None,
        start_date=now - timedelta(days=25),
        end_date=now + timedelta(days=days_left),
        status="active")
    db.add(sub)
    db.commit()
    return sub, now


def _admin_client(db, admin):
    app = FastAPI()
    app.include_router(admin_router)
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_current_admin] = lambda: admin
    return TestClient(app)


def _extend(client, sub_id, days=30):
    r = client.post(f"/api/admin/subscriptions/{sub_id}/extend",
                    json={"days": days})
    assert r.status_code == 200, r.text
    return r


def _reminders(db, sub_id):
    return db.query(models.SubscriptionReminder).filter(
        models.SubscriptionReminder.subscription_id == sub_id).all()


def test_extend_clears_reminders_and_new_cycle_reemits(db):
    admin = _make_user(db, "a107admin")
    u = _make_user(db, "a107user")
    sub, now = _make_sub(db, u)
    old_end = sub.end_date
    # 上一周期四档全发过
    for kind in ("7d", "3d", "1d", "expired"):
        db.add(models.SubscriptionReminder(
            subscription_id=sub.id, user_id=u.id, realm_id=None,
            kind=kind, sent_at=now - timedelta(days=20)))
    db.commit()

    _extend(_admin_client(db, admin), sub.id, days=30)

    # 延长后旧去重行被清掉（与 _grant_subscription 顺延同口径）
    db.expire_all()
    assert _reminders(db, sub.id) == []
    sub = db.query(models.UserSubscription).filter(
        models.UserSubscription.id == sub.id).first()
    assert sub.end_date == old_end + timedelta(days=30)
    new_end = sub.end_date

    # 新周期四档窗口逐档重新产出
    for kind, delta in (("7d", 7), ("3d", 3), ("1d", 1)):
        out = reminders.collect_due(db, now=new_end - timedelta(days=delta))
        kinds = [it["kind"] for it in out["reminders"]
                 if it["subscription_id"] == sub.id]
        assert kinds == [kind], (kind, out)
        db.add(models.SubscriptionReminder(
            subscription_id=sub.id, user_id=u.id, realm_id=None,
            kind=kind, sent_at=new_end - timedelta(days=delta)))
        db.commit()
    out = reminders.collect_due(db, now=new_end + timedelta(seconds=1))
    expired = [it["kind"] for it in out["expired"]
               if it["subscription_id"] == sub.id]
    assert expired == ["expired"]


def test_extend_without_prior_reminders_unchanged(db):
    admin = _make_user(db, "a107admin2")
    u = _make_user(db, "a107user2")
    sub, now = _make_sub(db, u)
    old_end = sub.end_date

    _extend(_admin_client(db, admin), sub.id, days=30)

    db.expire_all()
    assert _reminders(db, sub.id) == []
    sub = db.query(models.UserSubscription).filter(
        models.UserSubscription.id == sub.id).first()
    assert sub.end_date == old_end + timedelta(days=30)
    out = reminders.collect_due(db, now=sub.end_date - timedelta(days=7))
    kinds = [it["kind"] for it in out["reminders"]
             if it["subscription_id"] == sub.id]
    assert kinds == ["7d"]
