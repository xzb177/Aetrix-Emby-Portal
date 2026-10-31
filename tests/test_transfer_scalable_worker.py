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
def _boom_factory():
    raise RuntimeError("SA client factory exploded")


def _read_job(db_factory, job_id):
    s = db_factory()
    try:
        return s.query(em.TransferJob).filter(em.TransferJob.id == job_id).first()
    finally:
        s.close()


def test_execute_claimed_job_factory_escape_retries_with_backoff(db_factory, db):
    """A-013：工厂逃逸不再只 rollback——计次 1、pending + 退避、租约清掉。"""
    item = _item(db)
    job = _job(db, item_id=item.id, status="running", claimed_by="w1",
               lease_expires_at=datetime.now() + timedelta(seconds=3600),
               attempts=0)
    outcome = tv._execute_claimed_job(job.id, db_factory, _boom_factory)
    assert outcome == "retried"
    fresh = _read_job(db_factory, job.id)
    assert fresh.status == "pending"
    assert fresh.attempts == 1
    assert fresh.error_kind == "retriable"
    assert fresh.lease_expires_at is None
    assert fresh.claimed_by is None
    assert fresh.next_retry_at is not None
    assert fresh.next_retry_at > datetime.now()
    assert fresh.next_retry_at <= datetime.now() + timedelta(seconds=120)


def test_execute_claimed_job_factory_escape_at_max_fails_terminal(db_factory, db):
    """A-013：逃逸计次到 3 次即终态 failed/worker_error，不再被认领。"""
    item = _item(db)
    job = _job(db, item_id=item.id, status="running", claimed_by="w1",
               lease_expires_at=datetime.now() + timedelta(seconds=3600),
               attempts=2)
    outcome = tv._execute_claimed_job(job.id, db_factory, _boom_factory)
    assert outcome == "failed"
    fresh = _read_job(db_factory, job.id)
    assert fresh.status == "failed"
    assert fresh.attempts == 3
    assert fresh.error_kind == "worker_error"
    assert fresh.lease_expires_at is None
    assert fresh.claimed_by is None


def test_execute_claimed_job_in_run_escape_counts_once(db_factory, db, monkeypatch):
    """A-013：run 内已计次的逃逸不再重复计次（1 次逃逸 = attempts 1，不是 2）。"""
    item = _item(db)
    job = _job(db, item_id=item.id, status="running", claimed_by="w1",
               lease_expires_at=datetime.now() + timedelta(seconds=3600),
               attempts=0)

    def _raising_run(db_session, job_row, drive_api):
        job_row.status = "running"
        job_row.attempts += 1
        db_session.commit()
        raise RuntimeError("executor exploded after counting")

    monkeypatch.setattr(tv, "run_transfer_job_once", _raising_run)
    outcome = tv._execute_claimed_job(job.id, db_factory, lambda: SimpleNamespace())
    assert outcome == "retried"
    fresh = _read_job(db_factory, job.id)
    assert fresh.attempts == 1
    assert fresh.status == "pending"
    assert fresh.error_kind == "retriable"


def test_execute_claimed_job_escape_three_cycles_never_reclaimed(db_factory, db):
    """A-013：逃逸 3 轮后行终态，租约回收与认领都不再捡它。"""
    item = _item(db)
    job = _job(db, item_id=item.id, status="running", claimed_by="w1",
               lease_expires_at=datetime.now() + timedelta(seconds=3600),
               attempts=0)
    for i in range(3):
        outcome = tv._execute_claimed_job(job.id, db_factory, _boom_factory)
        if i < 2:
            assert outcome == "retried"
            # 模拟租约回收打回 pending 后被重新认领
            s = db_factory()
            row = s.query(em.TransferJob).filter(em.TransferJob.id == job.id).first()
            row.status = "running"
            row.lease_expires_at = datetime.now() + timedelta(seconds=3600)
            row.claimed_by = "w1"
            row.next_retry_at = None
            s.commit()
            s.close()
    assert outcome == "failed"
    fresh = _read_job(db_factory, job.id)
    assert fresh.status == "failed"
    assert fresh.attempts == 3
    s = db_factory()
    try:
        assert tv.reclaim_expired_leases(s) == 0
        assert job.id not in tv.claim_due_jobs(s, worker_id="w2")
    finally:
        s.close()


def test_run_transfer_job_once_attempts_guard_fails_terminal(db_factory, db):
    """A-013：执行入口兜底门控——attempts 已达上限的行直接终态，不再执行。"""
    item = _item(db)
    job = _job(db, item_id=item.id, status="pending", attempts=3)
    result = tv.run_transfer_job_once(db, job, SimpleNamespace())
    assert result["outcome"] == "failed"
    fresh = _read_job(db_factory, job.id)
    assert fresh.status == "failed"
    assert fresh.attempts == 3
    assert fresh.error_kind == "retriable"


def test_start_transfer_daemon_reclaims_expired_lease_on_startup(
        db_factory, db, monkeypatch):
    """启动即回收：守护启动返回前，死工人留下的过期租约单已被打回
    pending，不等主循环转一轮（2026-10-31 生产实证：部署重建窗口
    三单被旧进程认领后进程消失，租约过期后 12 分钟无人回收，用户
    点播恒 409；手工重启工人后约 1 分钟即恢复——差的就是启动这步
    与独立周期兜底）。主循环本体以模块属性补丁换成空转，只验启动
    回收；旧代码无启动回收，本测试红。"""
    now = datetime.now()
    job = _job(db, item_id=61, trigger="auto", status="running",
               owner_ref="u1")
    job.lease_expires_at = now - timedelta(seconds=1)
    db.commit()
    live = _job(db, item_id=63, trigger="auto", status="running",
                owner_ref="u2")
    live.lease_expires_at = now + timedelta(seconds=300)
    db.commit()
    job_id, live_id = job.id, live.id
    db.close()
    monkeypatch.setattr(tv, "_daemon_main_loop", lambda *a, **k: None)
    tv.start_transfer_daemon(session_factory=db_factory,
                             drive_api_factory=lambda: SimpleNamespace())
    fresh = _read_job(db_factory, job_id)
    assert fresh.status == "pending"
    assert fresh.lease_expires_at is None
    assert fresh.claimed_by is None
    live_fresh = _read_job(db_factory, live_id)
    assert live_fresh.status == "running"


def test_lease_reclaim_loop_recycles_without_wakeup(db_factory, db):
    """周期兜底独立于唤醒与认领：没有任何唤醒消息、没有任何认领
    调用，看门狗一个周期内就把过期租约单打回 pending，且打回后能
    被原认领路径重新认领（回收不许再搭认领/主循环迭代的车——主
    循环一轮可被长单执行或每日巡检占住十几分钟，2026-10-31 实证
    僵单正是死在这条耦合上）。"""
    import threading
    import time
    now = datetime.now()
    job = _job(db, item_id=62, trigger="auto", status="running",
               owner_ref="u1")
    job.lease_expires_at = now - timedelta(seconds=1)
    db.commit()
    job_id = job.id
    db.close()
    stop = threading.Event()
    thread = threading.Thread(target=tv._lease_reclaim_loop,
                              args=(stop, db_factory), daemon=True)
    thread.start()
    try:
        deadline = time.monotonic() + 10
        status = None
        while time.monotonic() < deadline:
            status = _read_job(db_factory, job_id).status
            if status == "pending":
                break
            time.sleep(0.05)
        assert status == "pending"
    finally:
        stop.set()
        thread.join(timeout=5)
    s = db_factory()
    try:
        claimed = tv.claim_due_jobs(s, worker_id="w-after-reclaim")
        assert job_id in claimed
    finally:
        s.close()


def test_reclaim_leases_once_swallows_errors():
    """回收兜底自身绝不许炸：会话工厂抛错时只记日志、返回 0，
    启动路径与看门狗循环都不因此中断（工人其它兜底同口径）。"""
    def _boom():
        raise RuntimeError("db down")
    assert tv._reclaim_leases_once(_boom, source="test") == 0
