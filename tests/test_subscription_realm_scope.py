# -*- coding: utf-8 -*-
"""回归测试：订阅查询在缺省 realm_id 时按服作用域解析（EM / EA 语义）。

修复点：backend/subscriptions.py 的 has_active_subscription / active_subscription
在缺省 realm_id 时改用 _scoped_realm_id（显式服 → 本进程的服 → 面板当前服），
不再退回跨服口径（修复：甲服订阅在面板当前服=乙付费服时被误判有效）。
"""
from __future__ import annotations

from datetime import datetime, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend import models, realms
from backend.models import Base
from backend.subscriptions import (
    active_subscription,
    can_play,
    has_active_subscription,
    set_process_realm_resolver,
    view_grant,
)


@pytest.fixture()
def db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


@pytest.fixture(autouse=True)
def _reset_process_realm_resolver():
    """进程级解析器是模块全局状态，用例前后恢复为 None（EM 语义）。"""
    set_process_realm_resolver(None)
    try:
        yield
    finally:
        set_process_realm_resolver(None)


def _realm(db, name, slug):
    realm = models.ServerRealm(name=name, slug=slug, access_mode="paid", is_active=True)
    db.add(realm)
    db.commit()
    return realm


def _user(db, username="u1"):
    user = models.WebUser(username=username, password_hash="x")
    db.add(user)
    db.commit()
    return user


def _plan(db, realm, name="月卡"):
    plan = models.SubscriptionPlan(name=name, realm_id=realm.id, price=30, duration_days=30)
    db.add(plan)
    db.commit()
    return plan


def _sub(db, user, plan, realm):
    now = datetime.now()
    sub = models.UserSubscription(
        user_id=user.id, plan_id=plan.id, realm_id=realm.id,
        status="active",
        start_date=now - timedelta(days=1),
        end_date=now + timedelta(days=29),
    )
    db.add(sub)
    db.commit()
    return sub


def test_em_defaults_to_panel_current_realm(db):
    """EM：不装进程级解析器，缺省 realm_id 按面板当前服判定，不跨服。"""
    a = _realm(db, "甲服", "a")
    b = _realm(db, "乙服", "b")
    user = _user(db)
    _sub(db, user, _plan(db, a), a)  # 用户只在甲服有生效中订阅
    realms.set_active_realm(db, b.id)  # 面板当前服 = 乙服（付费）

    assert has_active_subscription(db, user.id) is False
    assert active_subscription(db, user.id) is None
    assert can_play(db, user) is False
    assert view_grant(db, user) == (False, None)


def test_explicit_realm_unaffected(db):
    """显式传 realm_id 时仍按显式服判定，不受面板当前服影响。"""
    a = _realm(db, "甲服", "a")
    b = _realm(db, "乙服", "b")
    user = _user(db)
    _sub(db, user, _plan(db, a), a)
    realms.set_active_realm(db, b.id)

    assert has_active_subscription(db, user.id, a.id) is True
    sub = active_subscription(db, user.id, a.id)
    assert sub is not None
    assert sub.realm_id == a.id
    assert can_play(db, user, a.id) is True
    assert view_grant(db, user, a.id)[0] is True


def test_panel_current_realm_subscription_grants(db):
    """面板当前服上有生效订阅时，缺省查询放行。"""
    _realm(db, "甲服", "a")
    b = _realm(db, "乙服", "b")
    user = _user(db)
    _sub(db, user, _plan(db, b), b)
    realms.set_active_realm(db, b.id)

    assert has_active_subscription(db, user.id) is True
    assert can_play(db, user) is True


def test_ea_process_realm_takes_precedence(db):
    """EA：装了进程级解析器时，其返回的服优先于面板当前服（EA 行为不变）。"""
    a = _realm(db, "甲服", "a")
    b = _realm(db, "乙服", "b")
    user = _user(db)
    _sub(db, user, _plan(db, a), a)
    realms.set_active_realm(db, b.id)  # 面板当前服 = 乙服
    set_process_realm_resolver(lambda: a.id)  # 本进程的服 = 甲服

    assert has_active_subscription(db, user.id) is True
    assert can_play(db, user) is True
