"""审计 A-117 / A-118：上游聚合任务层。

A-117：_submit 线程启动裸调——start() 失败时异常穿出、管理端回
500，但行已是 queued；全仓无 queued 清扫，去重又把 queued 计入
进行中，致后续触发永复用死行、续跑亦点不动。
A-118：聚合心跳只在库边界刷新——窗/分页循环内零心跳，单库跑超
120 秒心跳阈值即被误判死亡，二次触发先标 interrupted 再放行第
二轮，双跑并发写映射。

HTTP 全部注入替身；DB 用内存 SQLite。
"""

import os

os.environ.setdefault("DATABASE_TYPE", "sqlite")
os.environ.setdefault("REDIS_ENABLED", "false")

import threading
import time
from datetime import datetime, timedelta
from urllib.parse import parse_qs, urlsplit

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend import models as base_models
from backend.emby_server import models as em
from backend.emby_server import upstream_aggregate_jobs as jobs
from backend.emby_server import upstream_aggregate_run as uar
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


def _source(db, *, enabled=True, name="src-1"):
    row = em.UpstreamSource(
        name=name, source_type="emby",
        base_url="https://up.example.com/emby", auth_method="none",
        username=None, sealed_secret=None, enabled=enabled,
        credential_scope="source", health_status="unknown")
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def _task_row(db_factory, task_id):
    session = db_factory()
    try:
        return (session.query(em.UpstreamAggregateTask)
                .filter(em.UpstreamAggregateTask.task_id == task_id).one())
    finally:
        session.close()


def _hold_submit(monkeypatch):
    started = []
    monkeypatch.setattr(jobs, "_submit", lambda tid: started.append(tid))
    return started


class _BoomThread:
    """start() 固定抛错的线程替身（模拟线程耗尽）。"""

    def __init__(self, *args, **kwargs):
        pass

    def start(self):
        raise RuntimeError("can't start new thread")


# ---------------------------------------------------------------------------
# A-117
# ---------------------------------------------------------------------------

def test_a117_submit_failure_marks_failed_not_500(db, monkeypatch):
    """start() 失败时 enqueue 不抛异常：行落 failed、视图带失败态，
    管理端不再回 500 悬案。"""
    source = _source(db)
    monkeypatch.setattr(jobs.threading, "Thread", _BoomThread)
    view = jobs.enqueue(source.id, 100)
    assert view["status"] == "failed"
    row = _task_row(jobs._session_factory, view["task_id"])
    assert row.status == "failed"
    assert row.error_json is not None
    assert "task_start_failed" in row.error_json


def test_a117_stale_queued_row_swept_next_enqueue_creates_fresh(
        db, db_factory, monkeypatch):
    """超龄 queued 行在入队维护中被清扫：旧行 failed、下一触发新建
    任务，不再永复用死行。"""
    source = _source(db)
    stale = em.UpstreamAggregateTask(
        task_id="stale-queued", source_id=source.id, trigger="manual",
        status="queued", page_size=100,
        created_at=datetime.now() - timedelta(hours=1),
        heartbeat_at=datetime.now() - timedelta(hours=1))
    db.add(stale)
    db.commit()
    started = _hold_submit(monkeypatch)
    view = jobs.enqueue(source.id, 100)
    assert view["task_id"] != "stale-queued"
    assert view["status"] == "queued"
    assert started == [view["task_id"]]
    row = _task_row(db_factory, "stale-queued")
    assert row.status == "failed"
    assert "queued_stale" in (row.error_json or "")


def test_a117_failed_task_resumable(db, monkeypatch):
    """submit 失败落 failed 的任务可 resume：新建任务、跳过已完成库。"""
    source = _source(db)
    old = em.UpstreamAggregateTask(
        task_id="old-failed", source_id=source.id, trigger="manual",
        status="failed", page_size=100,
        libraries_json=(
            '[{"library_id": "lib-1", "library_name": "电影", "fetched": 1,'
            ' "aggregates": 1, "paired": 1, "written": 1, "status": "ok"}]'),
        created_at=datetime.now() - timedelta(hours=1),
        heartbeat_at=datetime.now() - timedelta(hours=1))
    db.add(old)
    db.commit()
    started = _hold_submit(monkeypatch)
    view = jobs.resume_task(source.id, "old-failed")
    assert view is not None
    assert view["task_id"] != "old-failed"
    assert view["resume_of"] == "old-failed"
    assert started == [view["task_id"]]
    row = _task_row(jobs._session_factory, view["task_id"])
    assert row.skip_libraries_json is not None
    assert "lib-1" in row.skip_libraries_json


# ---------------------------------------------------------------------------
# A-118
# ---------------------------------------------------------------------------

def test_a118_live_thread_not_marked_stale_on_second_enqueue(
        db, db_factory, monkeypatch):
    """本进程活线程的任务，心跳已冻（长库窗内无心跳的模拟）时二次
    触发也不标 interrupted：返回 running 视图、不新建任务、不起
    第二线程；放行后首轮正常完成。"""
    source = _source(db)
    release = threading.Event()
    calls = []

    def fake(db_session, src, page_size=100, **kwargs):
        calls.append(1)
        release.wait(timeout=30)
        return {
            "source_id": src.id, "fetched": 1, "aggregates": 1,
            "pairs": [], "paired": 0, "written": 0, "mapping_enabled": False,
            "unpaired_reasons": {}, "libraries": [],
            "libraries_discovered": 0, "failed_libraries": [],
        }

    monkeypatch.setattr(jobs, "_aggregate_fn", fake)
    started = _hold_submit(monkeypatch)
    view = jobs.enqueue(source.id, 100)
    task_id = view["task_id"]
    worker = threading.Thread(target=jobs.execute_task, args=(task_id,),
                              daemon=True)
    worker.start()
    try:
        # 认领轮询：正常毫秒级；15 秒是给高负载下线程调度抖动的
        # 宽限（曾有一次 13 秒慢轮合跑失败），不断言时序、只断言终态。
        deadline = time.time() + 15
        while time.time() < deadline:
            if _task_row(db_factory, task_id).status == "running":
                break
            time.sleep(0.05)
        assert _task_row(db_factory, task_id).status == "running"
        # 模拟长库：执行线程在窗内不刷心跳，心跳冻在认领时刻。
        session = db_factory()
        try:
            row = (session.query(em.UpstreamAggregateTask)
                   .filter(em.UpstreamAggregateTask.task_id == task_id).one())
            row.heartbeat_at = datetime.now() - timedelta(hours=1)
            session.commit()
        finally:
            session.close()
        started.clear()
        again = jobs.enqueue(source.id, 100)
        assert again["task_id"] == task_id
        assert again["status"] == "running"
        assert started == []
        assert _task_row(db_factory, task_id).status == "running"
    finally:
        # 红态下断言先失败也须放行工作线程，避免孤儿线程在用例
        #  teardown 后继续跑、输出干扰性异常。
        release.set()
        worker.join(timeout=10)
    assert not worker.is_alive()
    assert len(calls) == 1
    snap = jobs.get_task(source.id, task_id)
    assert snap["status"] == "succeeded"


def test_a118_window_heartbeat_fires_per_window(db, monkeypatch):
    """窗级心跳：单库 5 条、窗 2 条时 on_heartbeat 触发 3 次（2/2/1）。"""
    source = _source(db)
    for i in range(1, 6):
        row = em.MediaItem(
            id=100 + i, guid=f"guid-{i}", library_id=1,
            item_type="movie", name=f"片{i}", tmdb_id=str(i),
            merged_into_id=None)
        db.add(row)
    db.commit()
    monkeypatch.setattr(uar, "WINDOW_RECORDS", 2)

    def fake_get(url, headers, timeout):
        if url.split("?")[0].endswith("/Library/MediaFolders"):
            return 200, {"Items": [{"Id": "lib-1", "Name": "电影"}]}
        query = parse_qs(urlsplit(url).query)
        start = int(query["StartIndex"][0])
        limit = int(query["Limit"][0])
        items = [{"Id": f"u-{i}", "Name": f"片{i}", "Type": "Movie",
                  "ProductionYear": 2000 + i,
                  "ProviderIds": {"Tmdb": str(i)}} for i in range(1, 6)]
        return 200, {"Items": items[start:start + limit],
                     "TotalRecordCount": len(items)}

    beats = []
    result = uar.run_manual_aggregation(
        db, source, http_get=fake_get,
        on_heartbeat=lambda event: beats.append(event))
    assert result["fetched"] == 5
    assert len(beats) == 3
    assert all(b["type"] == "heartbeat" for b in beats)


def test_a118_heartbeat_handler_only_refreshes_running(
        db, db_factory, monkeypatch):
    """心跳回调只刷新 running 行：已被标 interrupted 的行不许被残留
    线程的心跳复活。"""
    source = _source(db)
    running = em.UpstreamAggregateTask(
        task_id="hb-running", source_id=source.id, trigger="manual",
        status="running", page_size=100,
        created_at=datetime.now() - timedelta(hours=1),
        heartbeat_at=datetime.now() - timedelta(hours=1))
    dead = em.UpstreamAggregateTask(
        task_id="hb-dead", source_id=source.id, trigger="manual",
        status="interrupted", page_size=100,
        created_at=datetime.now() - timedelta(hours=1),
        heartbeat_at=datetime.now() - timedelta(hours=1))
    db.add_all([running, dead])
    db.commit()
    handler = jobs._make_heartbeat_handler("hb-running")
    handler({"type": "heartbeat"})
    row = _task_row(db_factory, "hb-running")
    assert row.heartbeat_at > datetime.now() - timedelta(minutes=1)
    dead_handler = jobs._make_heartbeat_handler("hb-dead")
    dead_handler({"type": "heartbeat"})
    row = _task_row(db_factory, "hb-dead")
    assert row.heartbeat_at < datetime.now() - timedelta(minutes=59)
