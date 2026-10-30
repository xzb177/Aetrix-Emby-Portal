"""公益服转换代码单测试：支付方法列表跟购买开关走＋新注册冷启动赠送。

背景（2026-10-28 公益服货币系统设计）：
- 购买开关关了，GET /payment/methods 仍硬编码 alipay/wxpay enabled=True，
  前端显示“可用”、用户点单才 503——列表必须跟开关走。
- 新注册默认送 30 天公益资格（channel=register_gift）＋ 30 天查看行
  （points_spent=0 标记赠送），均不扣积分；只改注册成功路径。

只测本单两处改动：不碰购买流程、不碰现有用户行、不碰积分扣减逻辑。
本地 Python 3.12 + pyshim 口径，全量以 GitHub CI 为准。
"""

import asyncio
import os
from datetime import datetime, timedelta

os.environ.setdefault("DATABASE_TYPE", "sqlite")
os.environ.setdefault("REDIS_ENABLED", "false")

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend import models
from backend.api.economy import payment_methods
from backend.integrations import store
from backend.api.emby_portal import _grant_register_gifts


@pytest.fixture()
def db():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    models.Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    store.invalidate()  # store 缓存只按键名不分库，进场先清防串味
    yield session
    session.close()
    store.invalidate()


def _set_config(db, key, value):
    db.add(models.SystemConfig(key=key, value=value))
    db.commit()
    store.invalidate(key)  # 与生产写时失效同口径


def _make_user(db, username="newbie"):
    user = models.WebUser(username=username, password_hash="x" * 60, is_active=True)
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def _methods_by_id(db):
    return {m.id: m.enabled for m in asyncio.run(payment_methods(db))}


# ==================== 支付方法列表 ====================

def test_payment_methods_purchase_on_default(db):
    """开关未配（默认 true）：alipay/wxpay 启用，无 qqpay。"""
    by_id = _methods_by_id(db)
    assert by_id == {"alipay": True, "wxpay": True}


def test_payment_methods_purchase_on_explicit(db):
    """开关显式 true：行为与默认一致。"""
    _set_config(db, "subscription_purchase_enabled", "true")
    by_id = _methods_by_id(db)
    assert by_id == {"alipay": True, "wxpay": True}


def test_payment_methods_purchase_off_disables_all(db):
    """开关 false：alipay/wxpay 一律 enabled=False（购买口本身不动，只改列表）。"""
    _set_config(db, "subscription_purchase_enabled", "false")
    by_id = _methods_by_id(db)
    assert by_id == {"alipay": False, "wxpay": False}


def test_payment_methods_purchase_off_qqpay_also_disabled(db):
    """开关 false 且 qqpay 已配：qqpay 照常出现在列表里，但同样 disabled。"""
    _set_config(db, "subscription_purchase_enabled", "false")
    _set_config(db, "payment_qqpay_enabled", "true")
    by_id = _methods_by_id(db)
    assert by_id == {"alipay": False, "wxpay": False, "qqpay": False}


def test_payment_methods_purchase_on_qqpay_enabled(db):
    """开关 true 且 qqpay 已配：qqpay 启用（现有行为不变）。"""
    _set_config(db, "subscription_purchase_enabled", "true")
    _set_config(db, "payment_qqpay_enabled", "true")
    by_id = _methods_by_id(db)
    assert by_id == {"alipay": True, "wxpay": True, "qqpay": True}


# ==================== 新注册冷启动赠送 ====================

def test_grant_register_gifts_creates_both_rows_no_points_spent(db):
    """开关开（新用户 0 分）：赠送后资格＋查看行齐全，积分一分没扣。"""
    _set_config(db, "register_gift_welfare_enabled", "true")
    _set_config(db, "register_gift_view_enabled", "true")
    user = _make_user(db)
    before = datetime.now()
    _grant_register_gifts(db, user)
    after = datetime.now()

    db.refresh(user)
    assert user.is_welfare is True
    assert user.welfare_grant_channel == "register_gift"
    assert before + timedelta(days=30) - timedelta(seconds=5) <= user.welfare_expires_at
    assert user.welfare_expires_at <= after + timedelta(days=30) + timedelta(seconds=5)
    assert user.points == 0

    rows = (
        db.query(models.EmbyViewUnlock)
        .filter(models.EmbyViewUnlock.user_id == user.id)
        .all()
    )
    assert len(rows) == 1
    assert rows[0].points_spent == 0
    assert before + timedelta(days=30) - timedelta(seconds=5) <= rows[0].expires_at
    assert rows[0].expires_at <= after + timedelta(days=30) + timedelta(seconds=5)


def test_grant_register_gifts_view_row_not_duplicated(db):
    """开关开时重复赠送：查看行不重复建（唯一约束 user+realm），资格按
    grant_welfare 语义在未过期到期日上累加，不缩短。"""
    _set_config(db, "register_gift_welfare_enabled", "true")
    _set_config(db, "register_gift_view_enabled", "true")
    user = _make_user(db)
    _grant_register_gifts(db, user)
    db.refresh(user)
    first_expiry = user.welfare_expires_at
    first_view = (
        db.query(models.EmbyViewUnlock)
        .filter(models.EmbyViewUnlock.user_id == user.id)
        .one()
    )
    first_view_expires = first_view.expires_at

    _grant_register_gifts(db, user)
    db.refresh(user)
    rows = (
        db.query(models.EmbyViewUnlock)
        .filter(models.EmbyViewUnlock.user_id == user.id)
        .all()
    )
    assert len(rows) == 1
    assert rows[0].expires_at == first_view_expires
    assert user.welfare_expires_at > first_expiry


def test_grant_register_gifts_existing_view_row_untouched(db):
    """已有查看行（非新用户场景的防御分支）：原行到期日与花费一字不动。"""
    from backend import realms

    _set_config(db, "register_gift_view_enabled", "true")
    user = _make_user(db)
    realm_id = realms.active_realm_id(db)
    far = datetime.now() + timedelta(days=365)
    db.add(models.EmbyViewUnlock(
        user_id=user.id, realm_id=realm_id,
        unlocked_at=datetime.now(), expires_at=far, points_spent=50,
    ))
    db.commit()

    _grant_register_gifts(db, user)
    rows = (
        db.query(models.EmbyViewUnlock)
        .filter(models.EmbyViewUnlock.user_id == user.id)
        .all()
    )
    assert len(rows) == 1
    assert rows[0].expires_at == far
    assert rows[0].points_spent == 50


# ==================== 送资格总开关（2026-10-29 默认关） ====================

def _view_rows(db, user):
    return (
        db.query(models.EmbyViewUnlock)
        .filter(models.EmbyViewUnlock.user_id == user.id)
        .all()
    )


def test_grant_register_gifts_welfare_off_by_default(db):
    """资格开关未配（默认关）：不送公益资格；凭证开关单独打开时
    查看行照常建（points_spent=0）——两开关独立。"""
    _set_config(db, "register_gift_view_enabled", "true")
    user = _make_user(db)
    _grant_register_gifts(db, user)

    db.refresh(user)
    assert not user.is_welfare
    assert user.welfare_grant_channel != "register_gift"
    assert user.points == 0

    rows = _view_rows(db, user)
    assert len(rows) == 1
    assert rows[0].points_spent == 0


def test_grant_register_gifts_welfare_off_explicit_false(db):
    """资格开关显式 false：资格不送；凭证开关打开时查看行照建。"""
    _set_config(db, "register_gift_welfare_enabled", "false")
    _set_config(db, "register_gift_view_enabled", "true")
    user = _make_user(db)
    _grant_register_gifts(db, user)

    db.refresh(user)
    assert not user.is_welfare
    assert user.welfare_grant_channel != "register_gift"
    assert len(_view_rows(db, user)) == 1


def test_grant_register_gifts_days_zero_means_never_expires_when_on(db):
    """天数 0 是“永不过期”不是关闭：开关开＋天数 0 时送的是永不过期
    资格（到期日为空），查看行照建。关闭只能靠开关本身（见上面两测）。"""
    _set_config(db, "register_gift_welfare_enabled", "true")
    _set_config(db, "register_gift_view_enabled", "true")
    _set_config(db, "register_gift_welfare_days", "0")
    user = _make_user(db)
    _grant_register_gifts(db, user)

    db.refresh(user)
    assert user.is_welfare is True
    assert user.welfare_grant_channel == "register_gift"
    assert user.welfare_expires_at is None
    assert len(_view_rows(db, user)) == 1


# ==================== 送凭证总开关（2026-10-29 默认关） ====================

def test_grant_register_gifts_view_off_by_default(db):
    """凭证开关未配（默认关）：不建查看行；资格开关同样未配，两样都不送。"""
    user = _make_user(db)
    _grant_register_gifts(db, user)

    db.refresh(user)
    assert not user.is_welfare
    assert user.points == 0
    assert _view_rows(db, user) == []


def test_grant_register_gifts_view_off_explicit_false(db):
    """凭证开关显式 false：与默认关一致，不建查看行。"""
    _set_config(db, "register_gift_view_enabled", "false")
    user = _make_user(db)
    _grant_register_gifts(db, user)

    assert _view_rows(db, user) == []


def test_grant_register_gifts_welfare_on_view_off(db):
    """新默认组合：资格开＋凭证关——资格照送（channel=register_gift），
    查看行不建，积分一分没扣。"""
    _set_config(db, "register_gift_welfare_enabled", "true")
    user = _make_user(db)
    _grant_register_gifts(db, user)

    db.refresh(user)
    assert user.is_welfare is True
    assert user.welfare_grant_channel == "register_gift"
    assert user.points == 0
    assert _view_rows(db, user) == []


def test_grant_register_gifts_view_on_creates_row_welfare_stays_off(db):
    """凭证开关单独开：查看行照建 30 天（points_spent=0），资格不送。"""
    _set_config(db, "register_gift_view_enabled", "true")
    user = _make_user(db)
    before = datetime.now()
    _grant_register_gifts(db, user)
    after = datetime.now()

    db.refresh(user)
    assert not user.is_welfare
    assert user.welfare_grant_channel != "register_gift"
    rows = _view_rows(db, user)
    assert len(rows) == 1
    assert rows[0].points_spent == 0
    assert before + timedelta(days=30) - timedelta(seconds=5) <= rows[0].expires_at
    assert rows[0].expires_at <= after + timedelta(days=30) + timedelta(seconds=5)
