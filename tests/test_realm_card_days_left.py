"""服卡片 days_left 口径统一（2026-10-23）。

发现页/接线员页曾在前端自算剩余天数（补到当天 23:59:59 + 向上取整 /
UTC 裸时间戳按本地再解释），与「我的」页后端值差 1~2 天。修法：
``_user_realm_cards`` 下发后端算好的 ``days_left``，公式与
``backend/api/user.py`` 的 ``days_left`` 逐字一致，前端直接显示。
"""
import os

os.environ.setdefault("DATABASE_TYPE", "sqlite")
os.environ.setdefault("REDIS_ENABLED", "false")

from datetime import datetime, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend import models
from backend.emby_server import portal


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


def _make_user(db, username="u1"):
    u = models.WebUser(username=username, password_hash="", points=0,
                       is_active=True, member_xp=0, member_level=1)
    db.add(u)
    db.commit()
    return u


def _make_realm(db, slug="paid", access_mode="paid"):
    r = models.ServerRealm(name=f"服-{slug}", slug=slug, access_mode=access_mode)
    db.add(r)
    db.commit()
    return r


def _make_sub(db, user, realm, end_date):
    plan = models.SubscriptionPlan(name="月卡", duration_days=30, price=18)
    db.add(plan)
    db.commit()
    sub = models.UserSubscription(
        user_id=user.id, plan_id=plan.id, realm_id=realm.id, status="active",
        start_date=end_date - timedelta(days=30), end_date=end_date,
    )
    db.add(sub)
    db.commit()
    return sub


def _card_for(db, user, realm):
    cards = portal._user_realm_cards(user, db, None)
    return next((c for c in cards if c["id"] == realm.id), None)


def test_days_left_floor_formula(db):
    """到期 = now + 29 天 19 小时 → 后端口径向下取整 = 29（前端旧算法会给 31）。"""
    user = _make_user(db)
    realm = _make_realm(db)
    end = datetime.now() + timedelta(days=29, hours=19)
    sub = _make_sub(db, user, realm, end)
    card = _card_for(db, user, realm)
    assert card is not None and card["subscribed"]
    assert card["days_left"] == 29
    # 与 api/user.py 同公式对拍（同一 end_date 代入）
    assert card["days_left"] == max(0, (sub.end_date - datetime.now()).days)


def test_days_left_last_day_is_zero(db):
    """到期只剩几小时：后端给 0（今天到期仍可用），不是前端旧算法的 1。"""
    user = _make_user(db)
    realm = _make_realm(db)
    _make_sub(db, user, realm, datetime.now() + timedelta(hours=5))
    card = _card_for(db, user, realm)
    assert card is not None
    assert card["days_left"] == 0


def test_days_left_none_without_subscription(db):
    """没订阅的服卡片 days_left 为 None，前端据此不显示天数。"""
    user = _make_user(db)
    realm = _make_realm(db, slug="free", access_mode="free")
    card = _card_for(db, user, realm)
    assert card is not None
    assert card["days_left"] is None
