"""上游聚合执行面统一到 worker + 跨进程同源互斥（优化 O-03）。

钉死的行为：
- api 侧 enqueue 只登记任务行，**不在本进程执行**（部署重启 api
  不再杀聚合）；执行由 worker 的认领循环 claim_and_execute_once
  完成，FIFO 逐条认领；
- 认领前先清扫心跳过期的 running（标 interrupted）；
- 定时调度改走同一张任务表入队（trigger=scheduled）：同源已有
  进行中任务 → 本轮 skipped（reason=task_active），不再有第二条
  平行执行路径；
- worker 启动接线真实存在（worker.py 调 start_aggregate_worker）。

DB 用内存 SQLite；HTTP 不涉及（聚合函数注入替身）。
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
from backend.emby_server import upstream_aggregate_jobs as jobs
from backend.emby_server import upstream_aggregate_scheduler as uas
from backend.integrations import store as config_store


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
def _clean(db_factory):
    config_store._ttl_cache.clear()
    jobs.reset_for_tests()
    jobs._session_factory = db_factory
    yield
    jobs.reset_for_tests()
    config_store._ttl_cache.clear()


def _source(db, *, name="src-1", enabled=True, source_type="emby"):
    row = em.UpstreamSource(
        name=name, source_type=source_type,
        base_url="https://up.example.com/emby", auth_method="none",
        username=None, sealed_secret=None, enabled=enabled,
        credential_scope="source", health_status="unknown")
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def _summary(source):
    return {"source_id": source.id, "fetched": 1, "aggregates": 1,
            "pairs": [], "paired": 0, "written": 0,
            "mapping_enabled": False, "unpaired_reasons": {},
            "libraries": [], "libraries_discovered": 0,
            "failed_libraries": []}


def test_enqueue_registers_only_worker_claim_executes(db, monkeypatch):
    source = _source(db)
    calls = []

    def fake(db_session, src, page_size=100, **kwargs):
        calls.append(src.id)
        return _summary(src)

    monkeypatch.setattr(jobs, "_aggregate_fn", fake)
    view = jobs.enqueue(source.id, 100)
    assert view["status"] == "queued"
    time.sleep(0.4)  # api 进程内不许再有执行线程偷偷跑
    assert calls == []
    claimed = jobs.claim_and_execute_once()
    assert claimed == view["task_id"]
    assert calls == [source.id]
    snap = jobs.get_task(source.id, view["task_id"])
    assert snap["status"] == "succeeded"
    # 队列已空：再认领返回 None。
    assert jobs.claim_and_execute_once() is None


def test_claim_is_fifo(db, monkeypatch):
    first = _source(db, name="first")
    second = _source(db, name="second")
    order = []

    def fake(db_session, src, page_size=100, **kwargs):
        order.append(src.id)
        return _summary(src)

    monkeypatch.setattr(jobs, "_aggregate_fn", fake)
    v1 = jobs.enqueue(first.id, 100)
    v2 = jobs.enqueue(second.id, 100)
    assert jobs.claim_and_execute_once() == v1["task_id"]
    assert jobs.get_task(second.id, v2["task_id"])["status"] == "queued"
    assert jobs.claim_and_execute_once() == v2["task_id"]
    assert order == [first.id, second.id]


def test_claim_sweeps_stale_running_first(db):
    source = _source(db)
    stale = em.UpstreamAggregateTask(
        task_id="stale-1", source_id=source.id, trigger="manual",
        status="running", page_size=100,
        created_at=datetime.now() - timedelta(hours=1),
        started_at=datetime.now() - timedelta(hours=1),
        heartbeat_at=datetime.now() - timedelta(hours=1))
    db.add(stale)
    db.commit()
    assert jobs.claim_and_execute_once() is None  # 无 queued 可认领
    db.refresh(stale)
    assert stale.status == "interrupted"


def test_active_task_for_reports_inflight(db):
    source = _source(db)
    assert jobs.active_task_for(source.id) is None
    view = jobs.enqueue(source.id, 100)
    active = jobs.active_task_for(source.id)
    assert active is not None
    assert active["task_id"] == view["task_id"]


def test_scheduler_enqueues_and_skips(db, monkeypatch):
    enabled = _source(db, name="on")
    busy = _source(db, name="busy")
    off = _source(db, name="off", enabled=False)
    cms = _source(db, name="cms", source_type="applecms")
    jobs.enqueue(busy.id, 100)  # busy 已有进行中任务
    results = uas.run_scheduled_aggregation(
        session_factory=sessionmaker(bind=db.get_bind()))
    by_id = {r["source_id"]: r for r in results}
    assert by_id[enabled.id]["status"] == "queued"
    assert by_id[enabled.id]["task_id"]
    assert by_id[busy.id]["status"] == "skipped"
    assert by_id[busy.id]["reason"] == "task_active"
    # 默认列源口径（与旧版一致）：未启用与苹果 CMS 源根本不进
    # 候选；它们的 skipped 形态由注入列源的调度单测覆盖。
    assert off.id not in by_id
    assert cms.id not in by_id
    row = (db.query(em.UpstreamAggregateTask)
           .filter(em.UpstreamAggregateTask.task_id
                   == by_id[enabled.id]["task_id"]).one())
    assert row.trigger == "scheduled"
    assert row.status == "queued"  # 调度只入队，不在本进程执行


def test_scheduler_injected_enqueue_and_failure_isolation():
    sources = [SimpleNamespace(id=1, enabled=True, source_type="emby"),
               SimpleNamespace(id=2, enabled=True, source_type="emby"),
               SimpleNamespace(id=3, enabled=True, source_type="emby")]
    seen = []

    def enqueue_fn(source_id):
        seen.append(source_id)
        if source_id == 2:
            raise RuntimeError("入队失败（假）")
        return {"task_id": f"t-{source_id}", "source_id": source_id,
                "status": "queued", "result": None, "error": None}

    results = uas.run_scheduled_aggregation(
        enqueue_fn=enqueue_fn, active_fn=lambda sid: None,
        list_sources_fn=lambda: sources)
    by_id = {r["source_id"]: r for r in results}
    assert seen == [1, 2, 3]
    assert by_id[1]["status"] == "queued"
    assert by_id[2]["status"] == "failed"
    assert by_id[3]["status"] == "queued"


def test_worker_loop_claims_then_waits_and_stops():
    stop = threading.Event()
    calls = []

    def claim_once():
        calls.append(1)
        if len(calls) == 1:
            return "task-x"
        stop.set()
        return None

    jobs.run_worker_loop(stop, claim_once=claim_once, poll_sec=0.01)
    assert len(calls) >= 2  # 认领到一条后立刻再扫，空了才等停表


def test_start_aggregate_worker_idempotent():
    stop = threading.Event()
    try:
        assert jobs.start_aggregate_worker(stop, claim_once=lambda: None,
                                           poll_sec=0.05) is True
        assert jobs.start_aggregate_worker(stop, claim_once=lambda: None,
                                           poll_sec=0.05) is True
    finally:
        stop.set()
        time.sleep(0.2)
    jobs._worker_thread = None


def test_worker_py_wires_aggregate_worker():
    import pathlib

    import backend.worker as worker_mod
    src = pathlib.Path(worker_mod.__file__).read_text(encoding="utf-8")
    assert "start_aggregate_worker(" in src
