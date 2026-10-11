"""grant_welfare 首次发放的回归测试。

背景：welfare_expires_at 为 None 曾同时表示「永不过期」和「从未开通」，
导致从未开通的用户首次发放有限天数时被误判为永久资格。
本文件覆盖修复后的到期时间计算分支。
"""
import sys
from datetime import datetime, timedelta

sys.path.insert(0, ".")

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend import models
from backend.emby_server import portal


@pytest.fixture()
def db():
    """每个 case 一个独立的内存库 + 独立 session，避免互相污染。"""
    engine = create_engine("sqlite:///:memory:")
    models.Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


def _seed_user(db, username):
    """最小 seed：只需要用户名和密码哈希。"""
    user = models.WebUser(username=username, password_hash="x")
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def test_first_grant_limited_days_is_not_permanent(db):
    """① 从未开通 + days=30 → 到期时间约为 now+30 天，而不是 None（永久）。"""
    user = _seed_user(db, "first_grant_30")
    assert user.is_welfare in (False, None)
    assert user.welfare_expires_at is None

    portal.grant_welfare(db, user, channel="points_redeem", days=30)
    db.refresh(user)

    assert user.is_welfare is True
    assert user.welfare_expires_at is not None, "首次发放有限天数被误判为永不过期"
    delta = user.welfare_expires_at - datetime.now()
    assert timedelta(days=29) <= delta <= timedelta(days=31)
    assert portal.is_welfare_user(db, user) is True


def test_first_grant_zero_days_is_permanent(db):
    """② 从未开通 + days=0 → None（永不过期）。"""
    user = _seed_user(db, "first_grant_zero")

    portal.grant_welfare(db, user, channel="welfare_admin", days=0)
    db.refresh(user)

    assert user.is_welfare is True
    assert user.welfare_expires_at is None
    assert portal.is_welfare_user(db, user) is True


def test_permanent_user_keeps_permanent_on_limited_days(db):
    """③ 已是永久（is_welfare=True, expires_at=None）+ days=30 → 仍为 None。"""
    user = _seed_user(db, "permanent_user")
    user.is_welfare = True
    user.welfare_expires_at = None
    db.add(user)
    db.commit()

    portal.grant_welfare(db, user, channel="points_redeem", days=30)
    db.refresh(user)

    assert user.is_welfare is True
    assert user.welfare_expires_at is None, "永久资格被有限天数降级"


def test_expired_grant_starts_from_now(db):
    """④ 已过期（expires_at=昨天）+ days=30 → 从 now 开始，约为 now+30 天。"""
    user = _seed_user(db, "expired_user")
    user.is_welfare = True
    user.welfare_expires_at = datetime.now() - timedelta(days=1)
    db.add(user)
    db.commit()

    portal.grant_welfare(db, user, channel="lottery", days=30)
    db.refresh(user)

    assert user.welfare_expires_at is not None
    delta = user.welfare_expires_at - datetime.now()
    assert timedelta(days=29) <= delta <= timedelta(days=31)


def test_active_grant_accumulates_on_existing_expiry(db):
    """⑤ 未过期（expires_at=明天）+ days=30 → 在明天基础上累加，约为 now+31 天。"""
    user = _seed_user(db, "active_user")
    user.is_welfare = True
    user.welfare_expires_at = datetime.now() + timedelta(days=1)
    db.add(user)
    db.commit()

    portal.grant_welfare(db, user, channel="points_redeem", days=30)
    db.refresh(user)

    assert user.welfare_expires_at is not None
    delta = user.welfare_expires_at - datetime.now()
    assert timedelta(days=30) <= delta <= timedelta(days=32)
