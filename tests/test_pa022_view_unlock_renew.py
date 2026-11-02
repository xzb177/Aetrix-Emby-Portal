# -*- coding: utf-8 -*-
"""PA-022 回归：公益服查看解锁过期后续开。

旧实现：前置检查只认未过期行，过期行走到新建 INSERT 撞
uq_emby_view_unlock_user_realm 唯一约束→回滚（扣费撤销）→重读不滤
过期→返回 success/already=True 且 expires_at 仍过期——钱没扣、权限
没给、接口谎报成功（subscriptions.view_grant 仍判无权限）。

钉住的口径：
1. 过期行再次解锁＝续开：原地更新 expires_at、正常扣费（一人一服
   仍一行），返回的成功必须对应 view_grant 真实生效。
2. 未过期重复解锁幂等：不重复扣费、到期时间不变。
3. 能量不足时 409，绝不报成功、行仍过期。
价格/天数一律走 SystemConfig（emby_view_unlock_points/days），测试里
显式写配置行，不依赖代码默认值。
"""

import os

os.environ.setdefault("DATABASE_TYPE", "sqlite")
os.environ.setdefault("REDIS_ENABLED", "false")

from datetime import datetime, timedelta

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend import models
from backend.emby_server.portal import UnlockViewRequest, unlock_emby_view

_TABLES = ("web_users", "server_realms", "system_configs",
           "emby_view_unlocks", "points_logs")


@pytest.fixture()
def db():
    engine = create_engine("sqlite:///:memory:")
    for t in _TABLES:
        models.Base.metadata.tables[t].create(engine, checkfirst=True)
    session = sessionmaker(bind=engine)()
    yield session
    session.close()


def _setup(db, points=1000, price=200, days=30, row_expires=None,
           with_row=True):
    db.add(models.SystemConfig(key="emby_view_unlock_points",
                               value=str(price)))
    db.add(models.SystemConfig(key="emby_view_unlock_days",
                               value=str(days)))
    realm = models.ServerRealm(name="公益服", slug="pa022free",
                               access_mode="free")
    db.add(realm)
    u = models.WebUser(username="pa022user", password_hash="x",
                       is_active=True, points=points)
    db.add(u)
    db.commit()
    if with_row:
        db.add(models.EmbyViewUnlock(
            user_id=u.id, realm_id=realm.id,
            unlocked_at=datetime.now() - timedelta(days=40),
            expires_at=row_expires, points_spent=50))
        db.commit()
    return realm, u


def _row(db, u, realm):
    return (db.query(models.EmbyViewUnlock)
              .filter(models.EmbyViewUnlock.user_id == u.id,
                      models.EmbyViewUnlock.realm_id == realm.id)
              .first())


def test_expired_row_renews_charges_and_grants(db):
    from backend import subscriptions
    expired = datetime.now() - timedelta(days=1)
    realm, u = _setup(db, row_expires=expired)
    out = unlock_emby_view(UnlockViewRequest(realm_id=realm.id), u, db)
    assert out["success"] is True
    assert out["already"] is False
    assert out["points_spent"] == 200
    assert out["balance"] == 800
    db.expire_all()
    u2 = db.query(models.WebUser).filter(models.WebUser.id == u.id).first()
    assert int(u2.points) == 800, "续开必须真扣费"
    rows = (db.query(models.EmbyViewUnlock)
              .filter(models.EmbyViewUnlock.user_id == u.id,
                      models.EmbyViewUnlock.realm_id == realm.id).all())
    assert len(rows) == 1, "一人一服一行：续开原地更新，不新建第二行"
    row = rows[0]
    assert row.expires_at is not None
    assert datetime.now() + timedelta(days=29) < row.expires_at \
        < datetime.now() + timedelta(days=31), "按配置 30 天从现在重算"
    granted, _ = subscriptions.view_grant(db, u2, realm.id)
    assert granted is True, "报成功必须对应真实生效的查看权限"


def test_unexpired_row_repeat_no_double_charge(db):
    future = datetime.now() + timedelta(days=10)
    realm, u = _setup(db, row_expires=future)
    out = unlock_emby_view(UnlockViewRequest(realm_id=realm.id), u, db)
    assert out["success"] is True
    assert out["already"] is True
    db.expire_all()
    u2 = db.query(models.WebUser).filter(models.WebUser.id == u.id).first()
    assert int(u2.points) == 1000, "未过期重复解锁不许重复扣费"
    row = _row(db, u, realm)
    assert abs((row.expires_at - future).total_seconds()) < 60


def test_first_unlock_creates_row_and_charges(db):
    realm, u = _setup(db, with_row=False)
    out = unlock_emby_view(UnlockViewRequest(realm_id=realm.id), u, db)
    assert out["success"] is True and out["already"] is False
    db.expire_all()
    row = _row(db, u, realm)
    assert row is not None and row.expires_at > datetime.now()
    u2 = db.query(models.WebUser).filter(models.WebUser.id == u.id).first()
    assert int(u2.points) == 800


def test_expired_row_insufficient_points_no_fake_success(db):
    expired = datetime.now() - timedelta(days=1)
    realm, u = _setup(db, points=100, row_expires=expired)
    with pytest.raises(HTTPException) as exc:
        unlock_emby_view(UnlockViewRequest(realm_id=realm.id), u, db)
    assert exc.value.status_code == 409
    db.expire_all()
    u2 = db.query(models.WebUser).filter(models.WebUser.id == u.id).first()
    assert int(u2.points) == 100
    row = _row(db, u, realm)
    assert row.expires_at <= datetime.now(), "失败后行仍过期，绝不假续上"


def test_expired_row_renews_to_permanent_when_days_zero(db):
    expired = datetime.now() - timedelta(days=1)
    realm, u = _setup(db, days=0, row_expires=expired)
    out = unlock_emby_view(UnlockViewRequest(realm_id=realm.id), u, db)
    assert out["success"] is True and out["already"] is False
    assert out["expires_at"] is None
    db.expire_all()
    row = _row(db, u, realm)
    assert row.expires_at is None, "天数配置 0=永久：续开后应为永久行"
