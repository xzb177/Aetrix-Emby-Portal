"""上游聚合自动调度骨架测试。

只测调度层：假源、假聚合函数，不碰真实 DB、不发真实上游请求。
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


def test_run_skips_disabled_and_applecms():
    sources = [_FakeSource(1, enabled=False),
               _FakeSource(2, source_type="applecms"),
               _FakeSource(3)]
    calls = []

    def _aggregate(db, source):
        calls.append(source.id)
        return {"fetched": 7, "written": 3}

    results = uas.run_scheduled_aggregation(
        session_factory=_session_factory, aggregate_fn=_aggregate,
        list_sources_fn=lambda: sources)
    by_id = {r["source_id"]: r for r in results}
    assert by_id[1]["status"] == "skipped"
    assert by_id[2]["status"] == "skipped"
    assert by_id[3]["status"] == "ok"
    assert by_id[3]["fetched"] == 7 and by_id[3]["written"] == 3
    assert calls == [3]


def test_single_source_failure_isolated():
    sources = [_FakeSource(1), _FakeSource(2), _FakeSource(3)]
    seen = []

    def _aggregate(db, source):
        seen.append(source.id)
        if source.id == 2:
            raise RuntimeError("上游不可用（假）")
        return {"fetched": 1, "written": 0}

    results = uas.run_scheduled_aggregation(
        session_factory=_session_factory, aggregate_fn=_aggregate,
        list_sources_fn=lambda: sources)
    by_id = {r["source_id"]: r for r in results}
    assert seen == [1, 2, 3]
    assert by_id[1]["status"] == "ok"
    assert by_id[2]["status"] == "failed"
    assert by_id[3]["status"] == "ok"


def test_session_open_failure_isolated_per_source():
    """完美审查第四轮：开会话失败只记该源 failed，不毁整轮。

    修前开会话在 try 外：第一个源开会话一抛，整轮抛掉、已跑完的
    源结果一并丢失，与模块“单源失败隔离”宣称不符。
    """
    sources = [_FakeSource(1), _FakeSource(2)]
    attempts = []

    def _flaky_factory():
        attempts.append(1)
        if len(attempts) == 1:
            raise RuntimeError("会话打不开（假）")
        return _FakeSession()

    results = uas.run_scheduled_aggregation(
        session_factory=_flaky_factory,
        aggregate_fn=lambda db, source: {"fetched": 1, "written": 0},
        list_sources_fn=lambda: sources)
    by_id = {r["source_id"]: r for r in results}
    assert by_id[1]["status"] == "failed"
    assert by_id[2]["status"] == "ok"


def test_nondict_outcome_is_failed_not_ok():
    """完美审查第四轮：聚合返回非 dict 记 failed，不许谎报 ok。

    修前非 dict 被当 ok（fetched/written 全 None），调度日志谎称成功。
    """
    results = uas.run_scheduled_aggregation(
        session_factory=_session_factory,
        aggregate_fn=lambda db, source: None,
        list_sources_fn=lambda: [_FakeSource(1)])
    assert results[0]["status"] == "failed"
    assert results[0]["source_id"] == 1


def test_list_sources_failure_returns_empty():
    results = uas.run_scheduled_aggregation(
        session_factory=_session_factory, aggregate_fn=lambda db, s: {},
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
