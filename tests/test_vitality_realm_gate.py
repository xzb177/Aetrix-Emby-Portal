"""活力门 realm 化（两服共存章程 P0-1）：活力值只拦公益（free）服播放。

钉住的口径：

1. **付费服不误拦**：paid 服＋有该服订阅＋活力低于阈值的公益用户，应放行
   （改前 ``ensure_playback_allowed`` 无条件查活力，此用例在旧代码上必红）。
2. **公益服行为不变**：free 服低活力照旧 403，且文案是活力文案不是付费墙。
3. **没传 realm_id 时按当前服判**：当前服是 free 服时，``realm_id=None``
   也必须照旧拦——专杀误用 ``realms.is_free_realm(db, None)`` 的写法
   （那个同名函数传 None 恒为 False，会把活力门整体关掉）；
   守卫只能用 ``subscriptions`` 模块自己的 scoped 版 ``is_free_realm``。
4. **顺序不变**：paid 服无订阅时先撞付费墙（can_play 在前），不是活力文案。

全部离线：内存 SQLite，只测 ``backend/subscriptions.ensure_playback_allowed``
的真调用（既有播放类测试全是 monkeypatch stub 它，这里补上真调用基线）。
fixture 工厂沿用 tests/test_user_grants.py 的口径。
"""
from __future__ import annotations

from datetime import datetime, timedelta

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend import models, realms, subscriptions, vitality
from backend.integrations import store as _store


@pytest.fixture()
def db():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
    models.Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    # 活力配置走 store 的进程级 TTL 缓存（只按键名、不分库）：别的测试文件
    # 可能在同进程里缓存过别的值，进场先全清，退场再清，别污染邻居。
    _store.invalidate(*vitality.VITALITY_DEFAULTS.keys())
    yield session
    session.close()
    _store.invalidate(*vitality.VITALITY_DEFAULTS.keys())


def _realm(db, name, slug, access_mode):
    realm = models.ServerRealm(
        name=name, slug=slug, access_mode=access_mode, is_active=True,
    )
    db.add(realm)
    db.commit()
    return realm


def _welfare_user(db, username="alice", vitality_value=2):
    """公益用户，活力 2 < 阈值 3（缺省配置下会被活力门拦）。"""
    user = models.WebUser(
        username=username, password_hash="x",
        is_welfare=True, vitality=vitality_value,
    )
    db.add(user)
    db.commit()
    return user


def _plan(db, realm, name="月卡"):
    plan = models.SubscriptionPlan(
        name=name, price=10, duration_days=30, realm_id=realm.id,
    )
    db.add(plan)
    db.commit()
    return plan


def _sub(db, user, realm, plan):
    sub = models.UserSubscription(
        user_id=user.id, plan_id=plan.id, realm_id=realm.id,
        start_date=datetime.now() - timedelta(days=1),
        end_date=datetime.now() + timedelta(days=30),
        status="active",
    )
    db.add(sub)
    db.commit()
    return sub


def _set_active_realm(db, realm):
    db.add(models.SystemConfig(key=realms.CONFIG_ACTIVE_REALM, value=str(realm.id)))
    db.commit()


# ---------------------------------------------------------------------------
# 1. 主用例：付费服＋低活力＋有订阅 → 放行（旧代码必红）
# ---------------------------------------------------------------------------

def test_paid_realm_low_vitality_with_subscription_allowed(db):
    paid = _realm(db, "付费服", "paid", "paid")
    user = _welfare_user(db)
    _sub(db, user, paid, _plan(db, paid))

    # 不抛即放行：活力门只管公益服，付费服订阅用户不再被活力误拦
    subscriptions.ensure_playback_allowed(db, user, paid.id)


# ---------------------------------------------------------------------------
# 2. 锚：公益服低活力 → 照旧拦，且是活力文案
# ---------------------------------------------------------------------------

def test_free_realm_low_vitality_still_blocked(db):
    free = _realm(db, "公益服", "main", "free")
    user = _welfare_user(db)

    with pytest.raises(HTTPException) as exc:
        subscriptions.ensure_playback_allowed(db, user, free.id)
    assert exc.value.status_code == 403
    assert "活力" in str(exc.value.detail)


# ---------------------------------------------------------------------------
# 3. 防误写锚：当前服为公益服＋不传 realm_id＋低活力 → 照旧拦
#    （误用 realms.is_free_realm(db, None) 的守卫会在这里误放行）
# ---------------------------------------------------------------------------

def test_none_realm_with_free_active_realm_still_blocked(db):
    free = _realm(db, "公益服", "main", "free")
    _set_active_realm(db, free)
    user = _welfare_user(db)

    with pytest.raises(HTTPException) as exc:
        subscriptions.ensure_playback_allowed(db, user, None)
    assert exc.value.status_code == 403
    assert "活力" in str(exc.value.detail)


# ---------------------------------------------------------------------------
# 4. 顺序锚：付费服无订阅 → 先撞付费墙，不是活力文案
# ---------------------------------------------------------------------------

def test_paid_realm_without_subscription_hits_paywall_first(db):
    paid = _realm(db, "付费服", "paid", "paid")
    user = _welfare_user(db)

    with pytest.raises(HTTPException) as exc:
        subscriptions.ensure_playback_allowed(db, user, paid.id)
    assert exc.value.status_code == 403
    assert "活力" not in str(exc.value.detail)
    assert str(exc.value.detail) == subscriptions.gate_message(db)
