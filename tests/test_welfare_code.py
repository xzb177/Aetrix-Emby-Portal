# -*- coding: utf-8 -*-
"""资格码（reward_type=welfare）回归：卡码直发公益资格＋打包查看行。

定版口径（2026-10-30 用户亲点 G1）：
- 核销后发 N 天公益资格（复用 grant_welfare 单一入口，channel=code_redeem）；
- 生成时带「打包查看凭证」开关（默认开）：核销时 EmbyViewUnlock 行同天数
  一并到账（同一事务），points_spent=0 标记赠送（同注册赠送口径）；
- 防倒卖沿用既有码体系：指名（target_username）、一码一用、过期作废；
- 管理员月配额 welfare_code_monthly_quota（默认 30，<=0 不限）只卡资格码。
"""
import os
from datetime import datetime, timedelta

os.environ.setdefault("DATABASE_TYPE", "sqlite")
os.environ.setdefault("REDIS_ENABLED", "false")

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend import codes, models
from backend.api import admin_ops as ops


@pytest.fixture()
def db():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    models.Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    session.add(models.ServerRealm(name="公益服", slug="main", is_active=True))
    session.commit()
    yield session
    session.close()


def _make_user(db, username, **kw):
    u = models.WebUser(username=username, password_hash="x", **kw)
    db.add(u)
    db.commit()
    db.refresh(u)
    return u


def _make_code(db, code_str, **kw):
    kw.setdefault("code_type", codes.CODE_TYPE_RENEW)
    kw.setdefault("max_uses", 1)
    kw.setdefault("use_count", 0)
    kw.setdefault("is_active", True)
    kw.setdefault("days", 30)
    kw.setdefault("reward_type", "welfare")
    c = models.RegistrationCode(code=code_str, **kw)
    db.add(c)
    db.commit()
    db.refresh(c)
    return c


def _unlock(db, user_id):
    return db.query(models.EmbyViewUnlock).filter(
        models.EmbyViewUnlock.user_id == user_id).first()


# ==================== 核销：资格到账 ====================

def test_welfare_code_grants_welfare(db):
    user = _make_user(db, "alice")
    _make_code(db, "WEL-AAAA")
    result = codes.redeem_code(db, user, "WEL-AAAA")
    db.commit()
    assert result["success"] is True
    assert result["reward_type"] == "welfare"
    db.refresh(user)
    assert user.is_welfare is True
    assert user.welfare_expires_at is not None
    assert user.welfare_expires_at > datetime.now() + timedelta(days=29)
    log = db.query(models.WelfareGrantLog).filter(
        models.WelfareGrantLog.user_id == user.id).first()
    assert log is not None and log.channel == "code_redeem" and log.days == 30


def test_welfare_code_bundles_view_unlock_by_default(db):
    user = _make_user(db, "alice")
    _make_code(db, "WEL-BBBB")
    result = codes.redeem_code(db, user, "WEL-BBBB")
    db.commit()
    assert result["success"] is True
    assert result.get("view_bundled") is True
    row = _unlock(db, user.id)
    assert row is not None
    assert row.points_spent == 0
    assert row.expires_at is not None
    assert row.expires_at > datetime.now() + timedelta(days=29)


def test_welfare_code_without_view_bundle(db):
    user = _make_user(db, "alice")
    _make_code(db, "WEL-CCCC", welfare_include_view=False)
    result = codes.redeem_code(db, user, "WEL-CCCC")
    db.commit()
    assert result["success"] is True
    assert result.get("view_bundled") is False
    assert _unlock(db, user.id) is None
    db.refresh(user)
    assert user.is_welfare is True


def test_welfare_code_extends_existing_view_unlock(db):
    user = _make_user(db, "alice")
    now = datetime.now()
    db.add(models.EmbyViewUnlock(
        user_id=user.id, realm_id=1, unlocked_at=now - timedelta(days=20),
        expires_at=now + timedelta(days=10), points_spent=50))
    db.commit()
    _make_code(db, "WEL-DDDD")
    result = codes.redeem_code(db, user, "WEL-DDDD")
    db.commit()
    assert result["success"] is True
    row = _unlock(db, user.id)
    # 在原到期上叠 30 天（≈40 天后），不是从今天重算 30 天
    assert row.expires_at > now + timedelta(days=39)


def test_welfare_code_stacks_on_existing_welfare(db):
    user = _make_user(db, "alice", is_welfare=True,
                      welfare_expires_at=datetime.now() + timedelta(days=5))
    _make_code(db, "WEL-EEEE")
    result = codes.redeem_code(db, user, "WEL-EEEE")
    db.commit()
    assert result["success"] is True
    db.refresh(user)
    assert user.welfare_expires_at > datetime.now() + timedelta(days=34)


# ==================== 核销：防倒卖口径 ====================

def test_named_welfare_code_rejected_for_other_user(db):
    alice = _make_user(db, "alice")
    bob = _make_user(db, "bob")
    code = _make_code(db, "WEL-NAME", target_username="alice")
    result = codes.redeem_code(db, bob, "WEL-NAME")
    db.rollback()
    assert result["success"] is False
    assert result.get("error_code") == codes.CODE_ERROR_NAMED_OTHER
    db.refresh(bob)
    assert not bob.is_welfare
    db.refresh(code)
    assert (code.use_count or 0) == 0
    # 指名本人可用
    result2 = codes.redeem_code(db, alice, "WEL-NAME")
    db.commit()
    assert result2["success"] is True


def test_welfare_code_single_use(db):
    alice = _make_user(db, "alice")
    bob = _make_user(db, "bob")
    _make_code(db, "WEL-ONCE")
    assert codes.redeem_code(db, alice, "WEL-ONCE")["success"] is True
    db.commit()
    again = codes.redeem_code(db, bob, "WEL-ONCE")
    db.rollback()
    assert again["success"] is False
    # 既有全类型口径：max_uses 用满即自动停用，第二人命中的是「已停用」
    # （或抢占失败的「已用尽」），两种都算明确拒绝、不发奖
    assert again.get("error_code") in (codes.CODE_ERROR_USED_UP, codes.CODE_ERROR_DISABLED)
    self_again = codes.redeem_code(db, alice, "WEL-ONCE")
    db.rollback()
    assert self_again["success"] is False


def test_expired_welfare_code_rejected(db):
    user = _make_user(db, "alice")
    _make_code(db, "WEL-OLD", expires_at=datetime.now() - timedelta(days=1))
    result = codes.redeem_code(db, user, "WEL-OLD")
    db.rollback()
    assert result["success"] is False
    assert result.get("error_code") == codes.CODE_ERROR_EXPIRED
    db.refresh(user)
    assert not user.is_welfare


def test_welfare_code_preview(db):
    _make_user(db, "alice")
    _make_code(db, "WEL-PREV")
    preview = codes.preview_code(db, "WEL-PREV", "alice")
    assert preview["valid"] is True
    assert preview["reward_type"] == "welfare"


# ==================== 生成：配额 ====================

@pytest.fixture()
def admin(db):
    return _make_user(db, "admin_test", is_staff=True, is_active=True)


def _gen_request(**overrides):
    payload = {"count": 1, "code_type": 2, "reward_type": "welfare", "days": 30}
    payload.update(overrides)
    return ops.CodeGenerateRequest(**payload)


def _set_quota(db, value):
    row = db.query(models.SystemConfig).filter(
        models.SystemConfig.key == "welfare_code_monthly_quota").first()
    if row is None:
        db.add(models.SystemConfig(key="welfare_code_monthly_quota", value=str(value)))
    else:
        row.value = str(value)
    db.commit()


def test_generate_welfare_code_stores_bundle_flag(db, admin):
    res = ops.generate_codes(_gen_request(welfare_include_view=False), admin, db)
    assert res["success"] is True
    code = db.query(models.RegistrationCode).filter(
        models.RegistrationCode.reward_type == "welfare").first()
    assert code is not None
    assert code.welfare_include_view is False


def test_generate_welfare_code_quota_blocks_over_limit(db, admin):
    _set_quota(db, 1)
    assert ops.generate_codes(_gen_request(), admin, db)["success"] is True
    with pytest.raises(HTTPException) as exc:
        ops.generate_codes(_gen_request(), admin, db)
    assert exc.value.status_code == 400


def test_generate_welfare_code_quota_counts_batch(db, admin):
    _set_quota(db, 2)
    with pytest.raises(HTTPException) as exc:
        ops.generate_codes(_gen_request(count=3), admin, db)
    assert exc.value.status_code == 400
    assert ops.generate_codes(_gen_request(count=2), admin, db)["success"] is True


def test_generate_welfare_code_quota_zero_means_unlimited(db, admin):
    _set_quota(db, 0)
    assert ops.generate_codes(_gen_request(count=5), admin, db)["success"] is True


def test_generate_quota_only_applies_to_welfare(db, admin):
    _set_quota(db, 1)
    assert ops.generate_codes(_gen_request(), admin, db)["success"] is True
    # 其他类型不受配额影响
    res = ops.generate_codes(
        _gen_request(reward_type="points", points_value=50, count=3), admin, db)
    assert res["success"] is True


def test_generate_quota_is_per_admin(db, admin):
    other_admin = _make_user(db, "admin_two", is_staff=True, is_active=True)
    _set_quota(db, 1)
    assert ops.generate_codes(_gen_request(), admin, db)["success"] is True
    assert ops.generate_codes(_gen_request(), other_admin, db)["success"] is True


# ==================== 既有三类码不回归（抽样） ====================

def test_subscription_code_still_grants_membership(db):
    user = _make_user(db, "alice")
    _make_code(db, "SUB-AAAA", reward_type="subscription")
    result = codes.redeem_code(db, user, "SUB-AAAA")
    db.commit()
    assert result["success"] is True
    assert result["reward_type"] == "subscription"
    sub = db.query(models.UserSubscription).filter(
        models.UserSubscription.user_id == user.id).first()
    assert sub is not None
    db.refresh(user)
    assert not user.is_welfare


def test_points_code_still_grants_points(db):
    user = _make_user(db, "alice", points=0)
    _make_code(db, "PTS-AAAA", reward_type="points", points_value=50)
    result = codes.redeem_code(db, user, "PTS-AAAA")
    db.commit()
    assert result["success"] is True
    assert result["reward_type"] == "points"
    db.refresh(user)
    assert (user.points or 0) == 50
    assert not user.is_welfare
