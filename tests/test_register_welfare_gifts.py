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
    yield session
    session.close()


def _set_config(db, key, value):
    db.add(models.SystemConfig(key=key, value=value))
    db.commit()


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
    """新用户（0 分）：赠送后资格＋查看行齐全，积分一分没扣。"""
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
    """重复赠送：查看行不重复建（唯一约束 user+realm），资格按 grant_welfare
    语义在未过期到期日上累加，不缩短。"""
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
