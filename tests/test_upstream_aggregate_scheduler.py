"""上游聚合自动调度测试（O-03 起调度只入队，不直接跑聚合）。

只测调度层：假源、假入队函数，不碰真实 DB、不发真实上游请求。
本地 Python 3.12 + pyshim 口径，全量以 GitHub CI 为准。
"""

import os
import threading

os.environ.setdefault("DATABASE_TYPE", "sqlite")
os.environ.setdefault("REDIS_ENABLED", "false")

from backend.emby_server import upstream_aggregate_scheduler as uas


class _FakeSource:
    def __init__(self, source_id, *, enabled=True, source_type="emby"):
        self.id = source_id
        self.enabled = enabled
        self.source_type = source_type


class _FakeSession:
    def __init__(self):
        self.rolled_back = False
        self.closed = False
        self.committed = False

    def rollback(self):
        self.rolled_back = True

    def close(self):
        self.closed = True

    def commit(self):
        self.committed = True


def _session_factory():
    return _FakeSession()


def test_scheduler_disabled_by_default(monkeypatch):
    monkeypatch.delenv("UPSTREAM_AGGREGATE_SCHEDULE_ENABLED", raising=False)
    assert uas.scheduler_enabled() is False


def test_scheduler_enabled_only_on_1(monkeypatch):
    monkeypatch.setenv("UPSTREAM_AGGREGATE_SCHEDULE_ENABLED", "1")
    assert uas.scheduler_enabled() is True
    monkeypatch.setenv("UPSTREAM_AGGREGATE_SCHEDULE_ENABLED", "true")
    assert uas.scheduler_enabled() is False
    monkeypatch.setenv("UPSTREAM_AGGREGATE_SCHEDULE_ENABLED", "0")
    assert uas.scheduler_enabled() is False


def test_interval_default_invalid_and_clamp(monkeypatch):
    monkeypatch.delenv("UPSTREAM_AGGREGATE_SCHEDULE_INTERVAL_SEC", raising=False)
    assert uas.schedule_interval_sec() == 21600.0
    monkeypatch.setenv("UPSTREAM_AGGREGATE_SCHEDULE_INTERVAL_SEC", "not-a-number")
    assert uas.schedule_interval_sec() == 21600.0
    monkeypatch.setenv("UPSTREAM_AGGREGATE_SCHEDULE_INTERVAL_SEC", "10")
    assert uas.schedule_interval_sec() == 60.0
    assert uas.schedule_interval_sec(3600) == 3600.0
    assert uas.schedule_interval_sec(5) == 60.0


def _enqueue_ok(source):
    return {"task_id": f"t-{source.id}", "status": "queued",
            "created": True,
            "view": {"task_id": f"t-{source.id}", "status": "queued",
                     "trigger": "scheduled"}}


def test_run_skips_disabled_and_applecms():
    sources = [_FakeSource(1, enabled=False),
               _FakeSource(2, source_type="applecms"),
               _FakeSource(3)]
    calls = []

    def _enqueue(source):
        calls.append(source.id)
        return _enqueue_ok(source)

    results = uas.run_scheduled_aggregation(
        session_factory=_session_factory, enqueue_fn=_enqueue,
        list_sources_fn=lambda: sources)
    by_id = {r["source_id"]: r for r in results}
    assert by_id[1]["status"] == "skipped"
    assert by_id[1]["reason"] == "source_disabled"
    assert by_id[2]["status"] == "skipped"
    assert by_id[2]["reason"] == "source_type_not_supported"
    assert by_id[3]["status"] == "queued"
    assert by_id[3]["task_id"] == "t-3"
    assert by_id[3]["trigger"] == "scheduled"
    assert calls == [3]


def test_single_source_failure_isolated():
    """O-03：单源入队失败只记该源 failed，不毁整轮。"""
    sources = [_FakeSource(1), _FakeSource(2), _FakeSource(3)]
    seen = []

    def _enqueue(source):
        seen.append(source.id)
        if source.id == 2:
            raise RuntimeError("入队失败（假）")
        return _enqueue_ok(source)

    results = uas.run_scheduled_aggregation(
        session_factory=_session_factory, enqueue_fn=_enqueue,
        list_sources_fn=lambda: sources)
    by_id = {r["source_id"]: r for r in results}
    assert seen == [1, 2, 3]
    assert by_id[1]["status"] == "queued"
    assert by_id[2]["status"] == "failed"
    assert by_id[3]["status"] == "queued"


def test_reused_active_task_is_skipped_task_active():
    """A-120：入队复用进行中任务时调度轮记 skipped/task_active。"""
    def _enqueue(source):
        return {"task_id": "t-old", "status": "running",
                "created": False,
                "view": {"task_id": "t-old", "status": "running",
                         "trigger": "manual"}}

    results = uas.run_scheduled_aggregation(
        session_factory=_session_factory, enqueue_fn=_enqueue,
        list_sources_fn=lambda: [_FakeSource(1)])
    assert results[0]["status"] == "skipped"
    assert results[0]["reason"] == "task_active"
    assert results[0]["task_id"] == "t-old"
    assert results[0]["trigger"] == "manual"


def test_nondict_outcome_is_failed_not_queued():
    """入队返回非 dict 记 failed，不许谎报 queued。"""
    results = uas.run_scheduled_aggregation(
        session_factory=_session_factory,
        enqueue_fn=lambda source: None,
        list_sources_fn=lambda: [_FakeSource(1)])
    assert results[0]["status"] == "failed"
    assert results[0]["source_id"] == 1


def test_list_sources_failure_returns_empty():
    results = uas.run_scheduled_aggregation(
        session_factory=_session_factory, enqueue_fn=_enqueue_ok,
        list_sources_fn=lambda: (_ for _ in ()).throw(RuntimeError("列源失败")))
    assert results == []


def test_start_returns_false_when_disabled(monkeypatch):
    monkeypatch.setenv("UPSTREAM_AGGREGATE_SCHEDULE_ENABLED", "0")
    assert uas.start_upstream_aggregate_scheduler() is False


def test_start_returns_true_and_is_idempotent(monkeypatch):
    monkeypatch.setenv("UPSTREAM_AGGREGATE_SCHEDULE_ENABLED", "1")
    stop = threading.Event()
    try:
        assert uas.start_upstream_aggregate_scheduler(
            stop, run_once=lambda: None, interval_sec=3600) is True
        # 已有存活线程时再起直接返回 True，不双跑。
        assert uas.start_upstream_aggregate_scheduler(
            stop, run_once=lambda: None, interval_sec=3600) is True
    finally:
        stop.set()
        # 给调度线程一个退出窗口；daemon 线程不阻塞测试退出。
        import time
        time.sleep(0.2)
    # 停表置位后线程应已退出；复位全局引用以免污染同进程其他测试。
    uas._scheduler_thread = None


def test_loop_stops_immediately_when_stop_set():
    stop = threading.Event()
    stop.set()
    uas.run_scheduler_loop(stop, run_once=lambda: (_ for _ in ()).throw(
        AssertionError("停表已置位时不应跑轮次")))
