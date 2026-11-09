# -*- coding: utf-8 -*-
"""播放交付计划后台化（起播优化件二）。

PlaybackInfo 内同步跑 ensure_plan 时，计划缺失/过期会现算
build_plan（探针串行，起播分析实测 3.1–5.7 秒），而本次
PlaybackInfo 根本不消费其返回值。改成后台线程建计划后，
本次请求不再等 build；计划的消费方（video_stream 只读、
转存完成刷新）逐字不变。
"""
import inspect
import threading
import time
from types import SimpleNamespace

import pytest

from backend.emby_server import delivery_plan as dp


class _Query:
    def __init__(self, row):
        self._row = row

    def filter_by(self, **kw):
        return self

    def one_or_none(self):
        return self._row


class _FakeSession:
    def __init__(self, user, item):
        self._user = user
        self._item = item
        self.closed = False

    def query(self, model):
        name = getattr(model, "__name__", "")
        if name == "WebUser":
            return _Query(self._user)
        return _Query(self._item)

    def close(self):
        self.closed = True


def _wait_until(predicate, timeout=5.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


def test_spawn_builds_plan_in_background(monkeypatch):
    """后台线程自开会话、调 ensure_plan、关会话；触发口不阻塞。"""
    user = SimpleNamespace(id=7)
    item = SimpleNamespace(id=5)
    calls = []
    fake_session = _FakeSession(user, item)
    monkeypatch.setattr("backend.database.SessionLocal",
                        lambda: fake_session)
    monkeypatch.setattr(dp, "ensure_plan",
                        lambda db, u, it: calls.append((db, u, it)))
    assert dp.spawn_ensure_plan(7, 5) is True
    assert _wait_until(lambda: len(calls) == 1)
    assert calls[0] == (fake_session, user, item)
    assert fake_session.closed is True


def test_spawn_dedupes_inflight(monkeypatch):
    """同 (用户, 条目) 在途时第二次触发直接返回，不重复建计划。"""
    user = SimpleNamespace(id=7)
    item = SimpleNamespace(id=5)
    calls = []
    started = threading.Event()
    release = threading.Event()

    def _slow_ensure(db, u, it):
        calls.append((u, it))
        started.set()
        release.wait(timeout=5)

    monkeypatch.setattr("backend.database.SessionLocal",
                        lambda: _FakeSession(user, item))
    monkeypatch.setattr(dp, "ensure_plan", _slow_ensure)
    assert dp.spawn_ensure_plan(7, 5) is True
    assert started.wait(timeout=5)
    assert dp.spawn_ensure_plan(7, 5) is True  # 在途，去重
    assert len(calls) == 1
    release.set()
    assert _wait_until(lambda: (7, 5) not in dp._PLAN_INFLIGHT)
    assert dp.spawn_ensure_plan(7, 5) is True  # 完成后可再触发
    assert _wait_until(lambda: len(calls) == 2)


def test_spawn_none_ids_rejected():
    assert dp.spawn_ensure_plan(None, 5) is False
    assert dp.spawn_ensure_plan(7, None) is False


def test_spawn_thread_failure_releases_key(monkeypatch):
    """起线程失败返回 False，且不把在途键烂在集合里。"""

    class _BoomThread:
        def __init__(self, *a, **kw):
            raise RuntimeError("起不来")

        def is_alive(self):
            # 接口面对齐（构造即抛错、本无实例）：防同类替身被存进常驻
            # 线程槽位后 .is_alive() 撞 AttributeError（卫生类别 2/4）。
            return False

    monkeypatch.setattr(threading, "Thread", _BoomThread)
    assert dp.spawn_ensure_plan(7, 5) is False
    assert (7, 5) not in dp._PLAN_INFLIGHT


def test_playback_info_does_not_block_on_ensure_plan():
    """回归守卫：PlaybackInfo 源码里不再同步等 ensure_plan，
    改走后台触发口；返回值本来就被丢弃，改动不影响本次响应。"""
    from backend.emby_server import api as api_mod
    source = inspect.getsource(api_mod.playback_info)
    assert "spawn_ensure_plan" in source
    assert "await run_db(_dp.ensure_plan" not in source
