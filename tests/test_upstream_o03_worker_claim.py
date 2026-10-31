"""O-03 执行面统一到 worker + A-120 调度入队（先红后绿）。

钉死的行为：
- api 入队不启动执行线程：enqueue 后 _submit 未被调用，行停在 queued；
- worker 认领循环把 queued 任务提交执行并跑到终态；
- 同源并发入队去重：两条 enqueue 同一 task_id，认领只执行一次；
- 同源两条已入队任务认领时串行：第二条等第一条终态后才跑（不双写）；
- A-120a：手动任务 running 时调度轮不启动第二路执行（返回
  skipped/task_active，任务数不变）；
- A-120b：调度轮任务失败后管理端可见任务行并可续跑；
- worker.py 里认领工人接线真实存在。

本地 Python 3.12 + pyshim 口径，全量以 GitHub CI 为准。
"""

import inspect
import os
import threading
import time
from types import SimpleNamespace

os.environ.setdefault("DATABASE_TYPE", "sqlite")
os.environ.setdefault("REDIS_ENABLED", "false")

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend import models as base_models
from backend.api import admin_upstream_sources as admin_ups
from backend.emby_server import models as em
from backend.emby_server import upstream_aggregate_jobs as jobs
from backend.emby_server import upstream_aggregate_run as uar
from backend.emby_server import upstream_aggregate_scheduler as uas


@pytest.fixture()
def db_factory():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False},
        poolclass=StaticPool)
    base_models.Base.metadata.create_all(engine)
    em.Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)


@pytest.fixture()
def db(db_factory):
    session = db_factory()
    yield session
    session.close()


@pytest.fixture()
def staff():
    return SimpleNamespace(id=7, is_staff=True, username="admin")


@pytest.fixture(autouse=True)
def _clean_jobs(db_factory):
    from backend.integrations import store as config_store
    config_store._ttl_cache.clear()
    jobs.reset_for_tests()
    jobs._session_factory = db_factory
    yield
    jobs.reset_for_tests()
    config_store._ttl_cache.clear()


def _source(db, *, enabled=True, source_type="emby", name="src-1"):
    row = em.UpstreamSource(
        name=name, source_type=source_type,
        base_url="https://up.example.com/emby", auth_method="none",
        username=None, sealed_secret=None, enabled=enabled,
        credential_scope="source", health_status="unknown")
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def _summary(source, **overrides):
    result = {
        "source_id": source.id,
        "fetched": 4,
        "aggregates": 3,
        "pairs": [{"upstream_item_id": "u1"}],
        "paired": 2,
        "written": 1,
        "mapping_enabled": False,
        "unpaired_reasons": {},
        "libraries": [{
            "library_id": "lib-1", "library_name": "电影",
            "fetched": 4, "aggregates": 3, "paired": 2, "written": 1,
            "status": "ok",
        }],
        "libraries_discovered": 2,
        "failed_libraries": [],
    }
    result.update(overrides)
    return result


def _wait_status(source_id, task_id, wanted, timeout=5.0):
    """轮询等终态：sqlite StaticPool 跨线程偶发 InterfaceError 时重试，
    只认最终状态，不放宽断言。"""
    from sqlalchemy.exc import SQLAlchemyError

    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        try:
            snap = jobs.get_task(source_id, task_id)
        except SQLAlchemyError:
            time.sleep(0.05)
            continue
        if snap is not None and snap["status"] == wanted:
            return snap
        last = snap
        time.sleep(0.05)
    raise AssertionError(
        f"任务未到 {wanted}：当前={last['status'] if last else None}")


def test_enqueue_does_not_start_execution_in_api_process(
        db, monkeypatch):
    """O-03：api 只入队，不在 api 进程起执行线程。"""
    source = _source(db)
    started = []
    monkeypatch.setattr(jobs, "_submit", lambda tid: started.append(tid))
    view = jobs.enqueue(source.id, 100)
    assert view["status"] == "queued"
    assert started == []
    row = (db.query(em.UpstreamAggregateTask)
           .filter(em.UpstreamAggregateTask.task_id == view["task_id"])
           .one())
    assert row.status == "queued"


def test_claim_loop_submits_queued_task_and_runs_to_terminal(
        db, monkeypatch):
    """worker 认领：queued 任务被提交执行并跑到终态。"""
    source = _source(db)
    monkeypatch.setattr(jobs, "_aggregate_fn",
                        lambda db_session, src, page_size=100, **kw:
                        _summary(src))
    view = jobs.enqueue(source.id, 100)
    assert view["status"] == "queued"
    submitted = jobs.claim_queued_tasks()
    assert submitted == [view["task_id"]]
    snap = _wait_status(source.id, view["task_id"], "succeeded")
    assert snap["result"]["written"] == 1
    assert snap["error"] is None


def test_same_source_concurrent_enqueue_is_deduped_and_runs_once(
        db, monkeypatch):
    """同源并发入队去重：两条 enqueue 同一 task_id，认领只执行一次。"""
    source = _source(db)
    calls = {"n": 0}

    def record(db_session, src, page_size=100, **kwargs):
        calls["n"] += 1
        return _summary(src)

    monkeypatch.setattr(jobs, "_aggregate_fn", record)
    first = jobs.enqueue(source.id, 100)
    again = jobs.enqueue(source.id, 100)
    assert again["task_id"] == first["task_id"]
    submitted = jobs.claim_queued_tasks()
    assert submitted == [first["task_id"]]
    _wait_status(source.id, first["task_id"], "succeeded")
    assert calls["n"] == 1
    nxt = jobs.enqueue(source.id, 100)
    assert nxt["task_id"] != first["task_id"]


def test_claim_serializes_two_queued_tasks_for_same_source(
        db, db_factory, monkeypatch):
    """同源两条已入队任务认领时串行：第二条等第一条终态后才跑。"""
    source = _source(db)
    # 建行用独立会话并立即关闭，避免夹具会话与执行线程争同一连接。
    session = db_factory()
    row1 = jobs._create_row(session, source_id=source.id,
                            page_size=100, trigger="manual")
    row2 = jobs._create_row(session, source_id=source.id,
                            page_size=100, trigger="manual")
    session.commit()
    tid1, tid2 = row1.task_id, row2.task_id
    session.close()
    db.close()

    state = {"current": 0, "peak": 0, "calls": 0}
    guard = threading.Lock()

    def slow(db_session, src, page_size=100, **kwargs):
        with guard:
            state["current"] += 1
            state["peak"] = max(state["peak"], state["current"])
            state["calls"] += 1
        time.sleep(0.3)
        with guard:
            state["current"] -= 1
        return _summary(src)

    monkeypatch.setattr(jobs, "_aggregate_fn", slow)
    jobs.claim_queued_tasks()
    _wait_status(source.id, tid1, "succeeded")
    # 第一条终态前第二条不许被认领执行：行仍停在 queued。
    row2_snap = jobs.get_task(source.id, tid2)
    assert row2_snap["status"] == "queued"
    jobs.claim_queued_tasks()
    _wait_status(source.id, tid2, "succeeded")
    assert state["calls"] == 2
    assert state["peak"] == 1


def test_scheduler_skips_when_manual_task_running_a120a(
        db, db_factory, monkeypatch):
    """A-120a：手动任务 running 时调度轮不启动第二路执行。"""
    source = _source(db)
    entered = threading.Event()
    release = threading.Event()
    calls = {"n": 0}

    def slow(db_session, src, page_size=100, **kwargs):
        calls["n"] += 1
        entered.set()
        assert release.wait(5)
        return _summary(src)

    monkeypatch.setattr(jobs, "_aggregate_fn", slow)
    view = jobs.enqueue(source.id, 100)
    thread = threading.Thread(
        target=jobs.execute_task, args=(view["task_id"],), daemon=True)
    thread.start()
    assert entered.wait(2)
    _wait_status(source.id, view["task_id"], "running")

    results = uas.run_scheduled_aggregation(session_factory=db_factory)
    by_id = {r["source_id"]: r for r in results}
    assert by_id[source.id]["status"] == "skipped"
    assert by_id[source.id]["reason"] == "task_active"
    assert by_id[source.id]["task_id"] == view["task_id"]
    rows = (db.query(em.UpstreamAggregateTask)
            .filter(em.UpstreamAggregateTask.source_id == source.id)
            .all())
    assert len(rows) == 1
    assert calls["n"] == 1

    release.set()
    thread.join(3)
    assert not thread.is_alive()


def test_scheduled_failure_visible_and_resumable_a120b(
        db, db_factory, staff, monkeypatch):
    """A-120b：调度轮任务失败后管理端可见任务行并可续跑。"""
    source = _source(db)

    def boom(db_session, src, page_size=100, **kwargs):
        raise uar.UpstreamAggregateRunError(
            409, "upstream_credential_invalid",
            "上游凭据失效，请管理员重录该源凭据")

    monkeypatch.setattr(jobs, "_aggregate_fn", boom)
    results = uas.run_scheduled_aggregation(session_factory=db_factory)
    assert len(results) == 1
    assert results[0]["status"] == "queued"
    task_id = results[0]["task_id"]
    assert results[0]["trigger"] == "scheduled"

    # worker 认领执行（测试里同步跑同一入口）→ 失败落任务行。
    jobs.execute_task(task_id)
    snap = admin_ups.get_upstream_aggregate_task(
        source.id, task_id, staff=staff)
    assert snap["status"] == "failed"
    assert snap["error"]["code"] == "upstream_credential_invalid"

    resumed = admin_ups.resume_upstream_aggregate_task(
        source.id, task_id, staff=staff, db=db)
    assert resumed["task_id"] != task_id
    assert resumed["status"] == "queued"


def test_worker_wiring_starts_aggregate_claim_worker():
    """worker.py 里认领工人接线真实存在（只接线，不改调度接线）。"""
    import backend.worker as worker_mod

    src = inspect.getsource(worker_mod)
    assert "start_aggregate_worker" in src
    assert "upstream_aggregate_worker" in src
