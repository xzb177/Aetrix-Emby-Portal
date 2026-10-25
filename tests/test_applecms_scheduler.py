# -*- coding: utf-8 -*-
"""苹果 CMS 定时调度测试（后续四件之 ①，全离线、不碰真实 DB）。

调度线程的等待间隔有 60 秒下限：线程启动测试用「首轮即置位
stop_event」的 run_once，让循环在等待处立即返回，不等满间隔。
"""
import os
import threading

os.environ.setdefault("DATABASE_TYPE", "sqlite")
os.environ.setdefault("REDIS_ENABLED", "false")

import pytest

from backend.emby_server import applecms_scheduler as sched
from backend.emby_server import applecms_sync as sync


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    monkeypatch.setenv(sync.APPLECMS_ENABLED_ENV, "0")
    monkeypatch.setenv(sync.UPSTREAM_SYNC_ENABLED_ENV, "0")
    monkeypatch.delenv(sched.SYNC_INTERVAL_ENV, raising=False)
    yield


def test_interval_default_clamp_and_invalid(monkeypatch):
    assert sched.sync_interval_sec() == 21600.0
    monkeypatch.setenv(sched.SYNC_INTERVAL_ENV, "30")
    assert sched.sync_interval_sec() == 60.0
    monkeypatch.setenv(sched.SYNC_INTERVAL_ENV, "3600")
    assert sched.sync_interval_sec() == 3600.0
    monkeypatch.setenv(sched.SYNC_INTERVAL_ENV, "not-a-number")
    assert sched.sync_interval_sec() == 21600.0
    assert sched.sync_interval_sec(explicit=10) == 60.0
    assert sched.sync_interval_sec(explicit=7200) == 7200.0


def test_gates_off_scheduler_does_not_start(monkeypatch):
    assert sched.scheduler_enabled() is False
    assert sched.start_applecms_scheduler() is False


def test_run_scheduled_sync_isolates_single_source_failure():
    calls = []

    def trigger(db, source_id):
        calls.append(source_id)
        if source_id == 2:
            raise RuntimeError("boom")
        return f"ok-{source_id}"

    results = sched.run_scheduled_sync(
        list_sources_fn=lambda: [1, 2, 3], trigger_fn=trigger)
    assert calls == [1, 2, 3]
    assert results == ["ok-1", "ok-3"]


def test_run_scheduled_sync_list_failure_returns_empty():
    assert sched.run_scheduled_sync(
        list_sources_fn=lambda: (_ for _ in ()).throw(
            RuntimeError("list boom")),
        trigger_fn=lambda db, sid: (_ for _ in ()).throw(
            AssertionError("不许调 trigger"))) == []


def test_run_scheduler_loop_stop_preset_runs_nothing():
    calls = []
    event = threading.Event()
    event.set()
    sched.run_scheduler_loop(event, run_once=lambda: calls.append(1),
                             interval_sec=3600)
    assert calls == []


def test_start_scheduler_runs_once_then_stops(monkeypatch):
    monkeypatch.setenv(sync.APPLECMS_ENABLED_ENV, "1")
    monkeypatch.setenv(sync.UPSTREAM_SYNC_ENABLED_ENV, "1")
    stop = threading.Event()
    calls = []

    def run_once():
        calls.append(1)
        stop.set()

    assert sched.start_applecms_scheduler(stop, run_once=run_once) is True
    thread = sched._scheduler_thread
    assert thread is not None
    thread.join(timeout=5)
    assert calls == [1]
    assert not thread.is_alive()


def test_start_scheduler_idempotent_while_alive(monkeypatch):
    monkeypatch.setenv(sync.APPLECMS_ENABLED_ENV, "1")
    monkeypatch.setenv(sync.UPSTREAM_SYNC_ENABLED_ENV, "1")
    stop = threading.Event()
    entered = threading.Event()
    release = threading.Event()
    calls = []

    def run_once():
        calls.append(1)
        entered.set()
        release.wait(timeout=5)
        stop.set()

    assert sched.start_applecms_scheduler(stop, run_once=run_once) is True
    assert entered.wait(timeout=5)
    first = sched._scheduler_thread
    assert sched.start_applecms_scheduler(stop, run_once=run_once) is True
    assert sched._scheduler_thread is first
    release.set()
    first.join(timeout=5)
    assert calls == [1]
    assert not first.is_alive()
