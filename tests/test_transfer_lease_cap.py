# -*- coding: utf-8 -*-
"""PanSave A 线 PR-3：转存工人租约续租总时长硬上限。

口子原型（2026-11-02 租约复核遗留）：执行线程真死锁、但心跳
线程还活时，_heartbeat 每 60 秒无条件续租，租约永不过期，
reclaim_expired_leases 永远轮不到这行——行永不回收、槽位永占。

修法：续租加总时长硬上限（自本轮认领 claimed_at 起算，可配
TRANSFER_WORKER_LEASE_MAX_TOTAL_SEC，默认 8h）；超限后
_renew_lease_once 拒绝续租并让心跳退出，租约自然到期后由
既有看门狗 reclaim_expired_leases 正常回收。先红后绿：
_renew_lease_once 与上限口径不存在时本文件即红。

全部离线：SQLite + 行级构造，不起真线程、不碰真实盘。
"""
import os

os.environ.setdefault("DATABASE_TYPE", "sqlite")
os.environ.setdefault("REDIS_ENABLED", "false")

from datetime import datetime, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend import models as base_models
from backend.emby_server import models as em
from backend.emby_server import transfer_variant as tv
from backend.integrations import store


@pytest.fixture()
def db_factory():
    engine = create_engine("sqlite://",
                           connect_args={"check_same_thread": False},
                           poolclass=StaticPool)
    base_models.Base.metadata.create_all(engine)
    em.Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)


@pytest.fixture()
def db(db_factory):
    session = db_factory()
    store.invalidate()
    yield session
    session.close()
    store.invalidate()


def _running_job(db, *, claimed_at=None, created_at=None,
                 lease_expires_at=None, status="running"):
    row = em.TransferJob(item_id=1, trigger="playback",
                         drive_id="drive1", provider="drive",
                         owner_scope="user", owner_ref="7",
                         status=status, claimed_by="w-test")
    if created_at is not None:
        row.created_at = created_at
    if claimed_at is not None:
        row.claimed_at = claimed_at
    if lease_expires_at is not None:
        row.lease_expires_at = lease_expires_at
    db.add(row)
    db.commit()
    return row


def test_renew_under_cap_extends_lease(db, db_factory):
    now = datetime.now()
    job = _running_job(db, claimed_at=now - timedelta(seconds=100),
                       lease_expires_at=now + timedelta(seconds=10))
    assert tv._renew_lease_once(db_factory, job.id, "w-test") is True
    db.expire_all()
    fresh = db.query(em.TransferJob).get(job.id)
    # 续到约 now + 一条租约（300s），明显大于原到期
    assert fresh.lease_expires_at > now + timedelta(seconds=200)


def test_renew_over_cap_stops_and_keeps_lease(db, db_factory,
                                              monkeypatch):
    monkeypatch.setattr(tv, "TRANSFER_WORKER_LEASE_MAX_TOTAL_SEC", 3600)
    now = datetime.now()
    lease = now + timedelta(seconds=120)
    job = _running_job(db, claimed_at=now - timedelta(seconds=3700),
                       lease_expires_at=lease)
    assert tv._renew_lease_once(db_factory, job.id, "w-test") is False
    db.expire_all()
    fresh = db.query(em.TransferJob).get(job.id)
    assert fresh.lease_expires_at == lease  # 没续，留给看门狗回收


def test_renew_terminal_or_missing_stops(db, db_factory):
    job = _running_job(db, status="done",
                       claimed_at=datetime.now(),
                       lease_expires_at=datetime.now())
    assert tv._renew_lease_once(db_factory, job.id, "w-test") is False
    assert tv._renew_lease_once(db_factory, 999999, "w-test") is False


def test_renew_legacy_row_anchors_on_created_at(db, db_factory,
                                                monkeypatch):
    monkeypatch.setattr(tv, "TRANSFER_WORKER_LEASE_MAX_TOTAL_SEC", 3600)
    now = datetime.now()
    # claimed_at 为 NULL 的存量行：以 created_at 为锚，超限同样停续
    job = _running_job(db, created_at=now - timedelta(seconds=3700),
                       lease_expires_at=now + timedelta(seconds=120))
    assert job.claimed_at is None
    assert tv._renew_lease_once(db_factory, job.id, "w-test") is False


def test_deadlocked_job_recovered_after_cap(db, db_factory, monkeypatch):
    """口子端到端：死锁行（执行死、心跳活）续租被上限截停后，
    租约到期即被既有看门狗打回 pending——不再永占槽位。"""
    monkeypatch.setattr(tv, "TRANSFER_WORKER_LEASE_MAX_TOTAL_SEC", 3600)
    now = datetime.now()
    job = _running_job(
        db, claimed_at=now - timedelta(seconds=3700),
        lease_expires_at=now - timedelta(seconds=5))  # 停续后已过期
    assert tv._renew_lease_once(db_factory, job.id, "w-test") is False
    reclaimed = tv.reclaim_expired_leases(db, now=now)
    assert reclaimed == 1
    db.expire_all()
    fresh = db.query(em.TransferJob).get(job.id)
    assert fresh.status == "pending"
    assert fresh.lease_expires_at is None


def test_lease_cap_default_and_floor(monkeypatch):
    # 默认 28800（8h）：合 96 个 300s 租约周期
    assert tv.TRANSFER_WORKER_LEASE_MAX_TOTAL_SEC == 28800
    assert tv.worker_lease_max_total_sec() == 28800
    # 配得比一条租约还短时钳到租约时长（不许自杀式配置）
    monkeypatch.setattr(tv, "TRANSFER_WORKER_LEASE_MAX_TOTAL_SEC", 10)
    assert tv.worker_lease_max_total_sec() == tv.worker_lease_sec()
    # 正常配置原样生效
    monkeypatch.setattr(tv, "TRANSFER_WORKER_LEASE_MAX_TOTAL_SEC", 7200)
    assert tv.worker_lease_max_total_sec() == 7200
