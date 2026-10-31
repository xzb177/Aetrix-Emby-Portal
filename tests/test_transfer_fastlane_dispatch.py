# -*- coding: utf-8 -*-
"""转存单道调度（2026-10-29 队列极限优化 + 搬字节关停）。

钉住的口径：
- 滑动窗口：轮中新建的单同轮被补认领，不再等整批跑完
  （旧 pool.map 轮次屏障下，本用例的第二单只能等下一轮）；
- 道内优先级：点播（playback）先于预取/后台被领取；
- 按用户公平：SystemConfig 的 per-owner 上限生效后，一个用户
  灌满也挡不住别人的单；
- 并发上限：同时在飞数不超过道并发键；
- 重试未到期的单不占道、也不挡后面的 due 单；
- 配置键：lane_concurrency / per_owner_inflight 的 SystemConfig
  覆盖与默认值；
- 跨 provider 建单（2026-10-30 重开）：总开关开时正常建单；
  关时才抛 cross_provider_relay_disabled、不落行（#1487 口径）。

全部离线：SQLite :memory: + 假执行器（monkeypatch 只打模块
属性）；真实 Drive/115 不碰。
"""
import os

os.environ.setdefault("DATABASE_TYPE", "sqlite")
os.environ.setdefault("REDIS_ENABLED", "false")

import threading
import time
from datetime import datetime, timedelta
from types import SimpleNamespace

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
def _clean_store_cache():
    """store 热缓存是进程全局：每例前后全清，避免互相污染。"""
    store.invalidate()
    yield
    store.invalidate()


def _job(db, item_id=1, trigger="playback", owner_ref="u1", status="pending",
         **kw):
    row = em.TransferJob(item_id=item_id, trigger=trigger, drive_id="drive1",
                         provider="drive", owner_scope="user",
                         owner_ref=owner_ref, status=status, **kw)
    db.add(row)
    db.commit()
    return row


def _set_config(db, key, value):
    row = (db.query(base_models.SystemConfig)
           .filter(base_models.SystemConfig.key == key).first())
    if row is None:
        db.add(base_models.SystemConfig(key=key, value=value))
    else:
        row.value = value
    db.commit()
    store.invalidate()


def _done_executor(completed):
    """最简假执行器：进来即 done，并记录完成顺序。"""
    def _run(db_session, job_row, drive_api):
        job_row.status = "done"
        job_row.error_kind = None
        job_row.error_detail = None
        job_row.next_retry_at = None
        db_session.commit()
        completed.append(job_row.id)
        return {"job_id": job_row.id, "outcome": "done"}
    return _run


# ---------- 配置键 ----------

def test_lane_concurrency_defaults_and_systemconfig(db):
    """无配置行：快道默认 8、per-owner 默认沿用现行 2；
    SystemConfig 行写入后两键都按行值走（管理端可改口径）。"""
    assert tv.lane_concurrency(db) == 8
    assert tv.per_owner_inflight(db) == 2
    _set_config(db, "transfer_fast_lane_concurrency", "3")
    _set_config(db, "transfer_per_owner_inflight", "5")
    assert tv.lane_concurrency(db) == 3
    assert tv.per_owner_inflight(db) == 5


def test_lane_concurrency_clamps_and_bad_value_falls_back(db):
    """越界钳制、非法值 fail-safe 回默认，绝不把工人打停。"""
    _set_config(db, "transfer_fast_lane_concurrency", "999")
    assert tv.lane_concurrency(db) == 32
    _set_config(db, "transfer_fast_lane_concurrency", "abc")
    assert tv.lane_concurrency(db) == 8


# ---------- 滑动窗口：轮中到达不等待 ----------

def test_sliding_window_picks_up_job_created_mid_round(
        db, db_factory, monkeypatch):
    """并发 1：第一单执行中新建第二单，同轮即被补认领并完成。

    旧轮次屏障实现下 claim 只在轮首发生一次，第二单不在本轮
    claimed 内、只能等下一轮——本断言在旧实现上必然失败。"""
    monkeypatch.setattr(tv, "lane_concurrency", lambda db=None: 1)
    first = _job(db, item_id=1, trigger="playback", owner_ref="u1")
    created = {}

    def _run(db_session, job_row, drive_api):
        if "second" not in created:
            s2 = db_factory()
            try:
                second = _job(s2, item_id=2, trigger="playback",
                              owner_ref="u1")
                created["second"] = second.id
            finally:
                s2.close()
        job_row.status = "done"
        job_row.next_retry_at = None
        db_session.commit()
        return {"job_id": job_row.id, "outcome": "done"}

    monkeypatch.setattr(tv, "run_transfer_job_once", _run)
    stats = tv.process_claimed_round(db_factory, lambda: SimpleNamespace())
    assert stats["processed"] == 2
    assert stats["done"] == 2
    assert first.id in stats["claimed"]
    assert created["second"] in stats["claimed"]
    s3 = db_factory()
    try:
        rows = s3.query(em.TransferJob).all()
        assert all(r.status == "done" for r in rows)
    finally:
        s3.close()


# ---------- 道内优先级 ----------

def test_playback_claimed_before_prefetch(db):
    """先入队的后台单排在点播单之后被领取；limit=1 只领点播单。"""
    _job(db, item_id=1, trigger="prefetch", owner_ref="u1")
    _job(db, item_id=2, trigger="manual", owner_ref="u2")
    fast = _job(db, item_id=3, trigger="playback", owner_ref="u3")
    assert tv.claim_due_jobs(db, limit=1) == [fast.id]


def test_prefetch_claimed_after_playback_when_room(db):
    """有空位时后台单照样被领取，只是排在点播单之后。"""
    slow = _job(db, item_id=1, trigger="auto", owner_ref="u1")
    fast = _job(db, item_id=2, trigger="playback", owner_ref="u2")
    claimed = tv.claim_due_jobs(db, limit=2)
    assert claimed == [fast.id, slow.id]


# ---------- 按用户公平（走新 SystemConfig 键） ----------

def test_per_owner_cap_from_systemconfig_lets_others_claim(db):
    """per-owner 上限由新键设为 1：u1 灌 3 单只领 1，u2 的单照领。"""
    _set_config(db, "transfer_per_owner_inflight", "1")
    for i in range(3):
        _job(db, item_id=10 + i, trigger="playback", owner_ref="u1")
    other = _job(db, item_id=99, trigger="playback", owner_ref="u2")
    claimed = tv.claim_due_jobs(db, limit=10)
    assert other.id in claimed
    assert len(claimed) == 2
    remaining = (db.query(em.TransferJob)
                 .filter(em.TransferJob.status == "pending").all())
    assert len(remaining) == 2
    assert all(r.owner_ref == "u1" for r in remaining)


# ---------- 并发上限不被突破 ----------

def test_lane_concurrency_cap_not_exceeded(tmp_path, monkeypatch):
    """12 个不同 owner 的单、道并发 3：同时在飞峰值 <= 3，全 done。

    本用例自建文件型 SQLite + 默认连接池（每线程独立连接，与
    生产 PG 同构）；全文件共用的 StaticPool 单连接在真并发下会
    串扰事务，不能用来测并发上限。"""
    engine = create_engine(
        f"sqlite:///{tmp_path}/lane.db",
        connect_args={"check_same_thread": False})
    base_models.Base.metadata.create_all(engine)
    em.Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    db = factory()
    monkeypatch.setattr(tv, "lane_concurrency", lambda db=None: 3)
    for i in range(12):
        _job(db, item_id=100 + i, trigger="playback",
             owner_ref=f"u{i}")
    db.close()
    lock = threading.Lock()
    state = {"inflight": 0, "peak": 0}

    def _run(db_session, job_row, drive_api):
        with lock:
            state["inflight"] += 1
            state["peak"] = max(state["peak"], state["inflight"])
        time.sleep(0.05)
        with lock:
            state["inflight"] -= 1
        job_row.status = "done"
        job_row.next_retry_at = None
        db_session.commit()
        return {"job_id": job_row.id, "outcome": "done"}

    monkeypatch.setattr(tv, "run_transfer_job_once", _run)
    stats = tv.process_claimed_round(factory, lambda: SimpleNamespace())
    assert stats["processed"] == 12
    assert stats["done"] == 12
    assert state["peak"] <= 3
    assert state["peak"] >= 2  # 确实并行了，不是串行假过


# ---------- 重试单不占道 ----------

def test_retry_not_due_not_claimed_and_not_blocking(db):
    """next_retry_at 未到的单不被领取，也挡不住后面的 due 单。"""
    future = _job(db, item_id=1, trigger="playback", owner_ref="u1",
                  next_retry_at=datetime.now() + timedelta(seconds=3600))
    due = _job(db, item_id=2, trigger="playback", owner_ref="u2")
    claimed = tv.claim_due_jobs(db, limit=10)
    assert claimed == [due.id]
    fresh = db.query(em.TransferJob).filter(
        em.TransferJob.id == future.id).first()
    assert fresh.status == "pending"


# ---------- 跨 provider 建单（2026-10-30 重开） ----------

def _binding(db, user_id, provider="drive", status="active"):
    cred = em.MountCredential(scope="user", owner_user_id=user_id,
                              status="active")
    db.add(cred)
    db.commit()
    row = em.UserStorageBinding(user_id=user_id, provider=provider,
                                account_label="test",
                                credential_id=cred.id, status=status)
    db.add(row)
    db.commit()
    return row


def test_create_cross_provider_job_creates_when_enabled(db, monkeypatch):
    """总开关默认开：跨 provider 建单正常落行（115 源→用户 Drive 盘）。"""
    monkeypatch.setenv("TRANSFER_VARIANT_ENABLED", "1")
    _binding(db, 7, provider="drive")
    item = SimpleNamespace(id=42, drive_file_id="fid1",
                           file_path="115:/电影/a.mkv")
    job = tv.create_cross_provider_job(db, item, 7, "pan115")
    assert job.status == "pending"
    assert job.provider == "pan115"
    assert job.dest_drive_id == "userdrive:7"
    assert tv._is_cross_job(job) is True
    assert db.query(em.TransferJob).count() == 1


def test_create_cross_provider_job_refused_when_switch_off(db):
    """总开关关：建单抛 cross_provider_relay_disabled，不落行
    （完整回退 2026-10-29 #1487 关停口径）。"""
    _set_config(db, "cross_provider_transfer_enabled", "false")
    item = SimpleNamespace(id=42, drive_file_id="fid1", file_path="x")
    with pytest.raises(tv.TransferError) as excinfo:
        tv.create_cross_provider_job(db, item, 7, "drive")
    assert excinfo.value.kind == "cross_provider_relay_disabled"
    assert db.query(em.TransferJob).count() == 0


# ---------- 池分支：慢单在飞时轮中到达不等待（2026-10-30 生产实锤） ----------

def test_pool_round_tops_up_free_slots_while_slow_job_runs(
        tmp_path, monkeypatch):
    """道并发 4、只有 1 条慢单在飞：轮中先后到达 prefetch 与
    playback 两单（同一事务落行），必须在慢单跑完之前就被补
    认领启动（池里有空位，不许等完成事件）；且同批补认领的
    顺序里 playback 先于先入队的 prefetch。

    生产实锤（job 1056/1057/1058）：慢单跑 5m40s，期间建的
    点播单干等约 4.5 分钟才被认领——池分支 wait 无超时、补
    认领只挂完成事件，守护主循环单线程阻塞在整轮内、建单
    唤醒在轮内无人消费。旧实现下本用例的到达单在慢单放行前
    永远不会启动（断言超时失败，不挂死：慢单有兜底超时）。
    """
    engine = create_engine(
        f"sqlite:///{tmp_path}/pool_topup.db",
        connect_args={"check_same_thread": False})
    base_models.Base.metadata.create_all(engine)
    em.Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    db = factory()
    monkeypatch.setattr(tv, "lane_concurrency", lambda db=None: 4)
    slow = _job(db, item_id=1, trigger="prefetch", owner_ref="u1")
    slow_id = slow.id
    db.close()

    slow_release = threading.Event()
    started = {}
    started_lock = threading.Lock()

    def _run(db_session, job_row, drive_api):
        with started_lock:
            started[job_row.id] = time.monotonic()
        if job_row.id == slow_id:
            # 慢单：等测试放行（兜底超时防异常路径下全套挂死）
            slow_release.wait(timeout=60)
        job_row.status = "done"
        job_row.next_retry_at = None
        db_session.commit()
        return {"job_id": job_row.id, "outcome": "done"}

    monkeypatch.setattr(tv, "run_transfer_job_once", _run)
    outcome_box = {}

    def _round():
        outcome_box["stats"] = tv.process_claimed_round(
            factory, lambda: SimpleNamespace())

    thread = threading.Thread(target=_round, daemon=True)
    thread.start()
    try:
        # 等慢单真正开跑（本轮已认领它，池里还剩 3 个空位）
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            with started_lock:
                if slow_id in started:
                    break
            time.sleep(0.05)
        with started_lock:
            assert slow_id in started
        # 轮中到达：prefetch 先落行、playback 后落行，同一事务
        # 提交，保证两单对认领方同时可见（同批补认领）
        s2 = factory()
        try:
            queued_prefetch = em.TransferJob(
                item_id=2, trigger="prefetch", drive_id="drive1",
                provider="drive", owner_scope="user", owner_ref="u3",
                status="pending")
            playback = em.TransferJob(
                item_id=3, trigger="playback", drive_id="drive1",
                provider="drive", owner_scope="user", owner_ref="u2",
                status="pending")
            s2.add_all([queued_prefetch, playback])
            s2.commit()
            prefetch_id, playback_id = queued_prefetch.id, playback.id
        finally:
            s2.close()
        # 两单必须在慢单仍在飞时就被补认领并启动
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            with started_lock:
                if playback_id in started and prefetch_id in started:
                    break
            time.sleep(0.05)
        with started_lock:
            assert playback_id in started, \
                "点播单未在慢单完成前被补认领（池分支补认领被完成事件堵住）"
            assert prefetch_id in started, \
                "排队 prefetch 未在慢单完成前被补认领"
    finally:
        slow_release.set()
        thread.join(timeout=30)
    stats = outcome_box["stats"]
    assert stats["processed"] == 3
    assert stats["done"] == 3
    # 认领顺序：慢单第一；同批补认领里 playback 先于先入队的 prefetch
    assert stats["claimed"][0] == slow_id
    assert stats["claimed"].index(playback_id) < stats["claimed"].index(
        prefetch_id)
