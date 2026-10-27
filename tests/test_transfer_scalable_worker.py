# -*- coding: utf-8 -*-
"""可伸缩转存工人（2026-10-27）：认领互斥、快通道、按用户限流、
租约回收、死令牌只停本用户队、队列可观测、事件唤醒。

全部离线：SQLite :memory: + 桩 drive_api；不碰真实 Drive、不碰
Redis（REDIS_ENABLED=false 时唤醒退回进程内 Event）。
"""
import os

os.environ.setdefault("DATABASE_TYPE", "sqlite")
os.environ.setdefault("REDIS_ENABLED", "false")

from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend import models as base_models
from backend.emby_server import models as em
from backend.emby_server import transfer_variant as tv


@pytest.fixture()
def db_factory():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False},
                           poolclass=StaticPool)
    base_models.Base.metadata.create_all(engine)
    em.Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)


@pytest.fixture()
def db(db_factory):
    session = db_factory()
    yield session
    session.close()


@pytest.fixture(autouse=True)
def _reset_pause_and_wake(monkeypatch):
    tv._paused_until.clear()
    tv._WAKE_EVENT.clear()
    yield
    tv._paused_until.clear()
    tv._WAKE_EVENT.clear()


def _job(db, item_id=1, trigger="auto", status="pending", owner_ref=None,
         owner_scope="user", drive_id="drive1", **kw):
    row = em.TransferJob(item_id=item_id, trigger=trigger, drive_id=drive_id,
                         owner_scope=owner_scope, owner_ref=owner_ref,
                         status=status, **kw)
    db.add(row)
    db.commit()
    return row


def _item(db, item_id=None):
    # run_transfer_job_once 需要条目存在且有 drive_file_id；认领测试
    # 本身不执行任务，故这里只给需要执行的测试建条目。
    row = em.MediaItem(guid=f"sw-{item_id}", library_id=1, item_type="movie",
                       name="测试片", drive_file_id="origfid",
                       file_path="mount://3/电影/a.mkv", size=1000)
    db.add(row)
    db.commit()
    return row


def test_claim_is_mutually_exclusive(db, db_factory, monkeypatch):
    """同一批 pending，两个认领会话互斥：第二个拿不到第一个已认领的单。"""
    monkeypatch.setattr(tv, "TRANSFER_WORKER_PER_USER_MAX", 10)
    for i in range(3):
        _job(db, item_id=10 + i, trigger="auto", owner_ref="u1")
    db.close()
    s1 = db_factory()
    claimed1 = tv.claim_due_jobs(s1, limit=10)
    assert len(claimed1) == 3
    s1.close()
    s2 = db_factory()
    claimed2 = tv.claim_due_jobs(s2, limit=10)
    assert claimed2 == []
    s2.close()
    s3 = db_factory()
    rows = s3.query(em.TransferJob).order_by(em.TransferJob.id).all()
    assert all(r.status == "running" for r in rows)
    assert all(r.lease_expires_at is not None for r in rows)
    s3.close()


def test_fast_lane_before_slow_lane(db, monkeypatch):
    """快通道优先：后建的 playback 单排在先建的 auto/manual 单前面。"""
    monkeypatch.setattr(tv, "TRANSFER_WORKER_PER_USER_MAX", 10)
    _job(db, item_id=1, trigger="auto", owner_ref="u1")
    _job(db, item_id=2, trigger="manual", owner_ref="u2")
    fast = _job(db, item_id=3, trigger="playback", owner_ref="u3")
    claimed = tv.claim_due_jobs(db, limit=1)
    assert claimed == [fast.id]


def test_per_user_cap_blocks_third_job(db, monkeypatch):
    """每用户上限：默认 2，同一用户第 3 单不认领，另一用户单子照常认领。"""
    monkeypatch.setattr(tv, "TRANSFER_WORKER_PER_USER_MAX", 2)
    for i in range(3):
        _job(db, item_id=20 + i, trigger="playback", owner_ref="u1")
    other = _job(db, item_id=99, trigger="playback", owner_ref="u2")
    claimed = tv.claim_due_jobs(db, limit=10)
    assert other.id in claimed
    assert len(claimed) == 3  # u1 两单 + u2 一单
    remaining = (db.query(em.TransferJob)
                 .filter(em.TransferJob.status == "pending").all())
    assert len(remaining) == 1
    assert remaining[0].owner_ref == "u1"


def test_round_robin_fairness(db, monkeypatch):
    """轮流取单：一用户 5 单、另一用户 1 单，limit=3 时不是前者独占 3 单。"""
    monkeypatch.setattr(tv, "TRANSFER_WORKER_PER_USER_MAX", 10)
    for i in range(5):
        _job(db, item_id=30 + i, trigger="playback", owner_ref="uA")
    lone = _job(db, item_id=40, trigger="playback", owner_ref="uB")
    claimed = tv.claim_due_jobs(db, limit=3)
    assert lone.id in claimed
    assert len(claimed) == 3
    # uA 至多 2 单（第一轮 1 单 + 第二轮 1 单），不是 3 单全占。
    owners = [db.query(em.TransferJob).filter(
        em.TransferJob.id == jid).first().owner_ref for jid in claimed]
    assert owners.count("uA") == 2
    assert owners.count("uB") == 1


def test_lease_expiry_reclaims_but_live_lease_stays(db):
    """租约过期回队、未过期不打回；旧行（租约 NULL + updated_at 超 1h）兜底打回。"""
    now = datetime.now()
    expired = _job(db, item_id=51, trigger="auto", status="running",
                   owner_ref="u1")
    expired.lease_expires_at = now - timedelta(seconds=1)
    db.commit()
    live = _job(db, item_id=52, trigger="auto", status="running",
                owner_ref="u2")
    live.lease_expires_at = now + timedelta(seconds=300)
    db.commit()
    old = _job(db, item_id=53, trigger="auto", status="running",
               owner_ref="u3")
    old.lease_expires_at = None
    old.updated_at = now - timedelta(seconds=3700)
    db.commit()
    count = tv.reclaim_expired_leases(db, now=now)
    assert count == 2
    assert db.query(em.TransferJob).filter(
        em.TransferJob.id == expired.id).first().status == "pending"
    assert db.query(em.TransferJob).filter(
        em.TransferJob.id == live.id).first().status == "running"
    assert db.query(em.TransferJob).filter(
        em.TransferJob.id == old.id).first().status == "pending"


def test_dead_token_pauses_only_that_user(db, monkeypatch):
    """死令牌只停本用户队：暂停 u1 后，u1 的单跳过，u2 的单照常认领。"""
    monkeypatch.setattr(tv, "TRANSFER_WORKER_PER_USER_MAX", 10)
    _job(db, item_id=61, trigger="playback", owner_ref="u1")
    other = _job(db, item_id=62, trigger="playback", owner_ref="u2")
    tv.pause_user_queue("user", "u1", seconds=900)
    assert tv.is_user_paused("user", "u1") is True
    assert tv.is_user_paused("user", "u2") is False
    claimed = tv.claim_due_jobs(db, limit=10)
    assert claimed == [other.id]


def test_queue_stats_median_p95_and_alerts(db):
    """队列可观测：深度、等待中位数/p95、超阈值告警（warning 级口径）。"""
    now = datetime.now()
    for age in (10, 20, 30, 100):
        row = _job(db, item_id=70 + age, trigger="auto", owner_ref="u1")
        row.created_at = now - timedelta(seconds=age)
        db.commit()
    stats = tv.queue_stats(db, now=now)
    assert stats["queue_depth"] == 4
    assert stats["due_count"] == 4
    assert stats["waiting_p95_sec"] >= stats["waiting_median_sec"]
    assert stats["waiting_max_sec"] == 100.0
    assert tv.queue_alerts({"queue_depth": 1, "waiting_p95_sec": 1}) == []
    alerts = tv.queue_alerts({"queue_depth": 500, "waiting_p95_sec": 999})
    assert len(alerts) == 2


def test_wakeup_event_is_set_on_notify(monkeypatch):
    """事件唤醒：notify 置进程内 Event；Redis 不可用时不抛错、不丢任务。"""
    monkeypatch.setenv("REDIS_ENABLED", "false")
    assert tv._WAKE_EVENT.is_set() is False
    assert tv.notify_transfer_wakeup(123) is True
    assert tv._WAKE_EVENT.is_set() is True


def test_sweep_interval_is_not_high_frequency_polling():
    """兜底扫仍是 60 秒，绝不是 5 秒高频轮询（回归：改短即本测试红）。"""
    assert tv.worker_sweep_sec() == 60


def test_process_claimed_round_runs_fast_job(db_factory, db, monkeypatch):
    """认领轮端到端（桩 drive_api）：playback 单被认领并执行 done，
    租约在终态被清掉。"""
    monkeypatch.setattr(tv, "TRANSFER_WORKER_CONCURRENCY", 1)
    monkeypatch.setattr(tv, "TRANSFER_WORKER_PER_USER_MAX", 2)
    item = _item(db)
    job = _job(db, item_id=item.id, trigger="playback", owner_ref="7",
               owner_scope="user", drive_id="drive1")

    class _FakeUserClient:
        def __init__(self, *a, **k):
            pass

    # run_transfer_job_once 对 user 任务走 _run_drive_user_job，需要
    # 绑定与令牌桩；这里直接用猴子补丁把执行器换成最简 done 路径，
    # 只验证认领轮的认领/清租约/统计骨架，不伪造 Drive copy 语义。
    def _fake_run(db_session, job_row, drive_api):
        job_row.status = "done"
        job_row.error_kind = None
        job_row.error_detail = None
        job_row.next_retry_at = None
        db_session.commit()
        return {"job_id": job_row.id, "outcome": "done"}

    monkeypatch.setattr(tv, "run_transfer_job_once", _fake_run)
    stats = tv.process_claimed_round(db_factory, lambda: SimpleNamespace())
    assert stats["done"] == 1
    assert stats["claimed"] == [job.id]
    fresh = db.query(em.TransferJob).filter(
        em.TransferJob.id == job.id).first()
    assert fresh.status == "done"
    assert fresh.lease_expires_at is None
