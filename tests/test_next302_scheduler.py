"""302 Next A1：数据集调度器门禁。

不连生产库。覆盖：总闸/数据集子开关任一关着时 start_dataset_scheduler
直接返回 False、不起线程（回归：之前没查门禁，关闸也起线程并让
worker 记“已启动”）；双开时起线程且同一进程只启动一次。
"""

import os

os.environ.setdefault("DATABASE_TYPE", "sqlite")
os.environ.setdefault("REDIS_ENABLED", "false")

import threading

import pytest

from backend.emby_server import next302_gate as gate
from backend.emby_server import tmdb_dataset as dataset


@pytest.fixture(autouse=True)
def _reset_scheduler_flag():
    previous = dataset._SCHEDULER_STARTED
    dataset._SCHEDULER_STARTED = False
    try:
        yield
    finally:
        dataset._SCHEDULER_STARTED = previous


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for key in (gate.MASTER_ENV, gate.DATASET_ENV, gate.SEARCH_ENV,
                gate.DATASET_INTERVAL_ENV):
        monkeypatch.delenv(key, raising=False)


def test_gate_off_starts_no_thread(monkeypatch):
    """总闸关（子开关开也没用）：返回 False，不起同步线程。"""
    monkeypatch.setenv(gate.DATASET_ENV, "1")
    assert dataset.start_dataset_scheduler() is False
    assert dataset._SCHEDULER_STARTED is False
    assert "next302-dataset-sync" not in [
        t.name for t in threading.enumerate()]


def test_sub_switch_off_starts_no_thread(monkeypatch):
    """总闸开、数据集子开关关：同样返回 False，不起线程。"""
    monkeypatch.setenv(gate.MASTER_ENV, "1")
    monkeypatch.setenv(gate.DATASET_ENV, "0")
    assert dataset.start_dataset_scheduler() is False
    assert dataset._SCHEDULER_STARTED is False


def test_gate_on_starts_thread_once(monkeypatch):
    """双开时起线程（用假线程，不真跑同步循环），第二调用返回 False。"""
    monkeypatch.setenv(gate.MASTER_ENV, "1")
    monkeypatch.setenv(gate.DATASET_ENV, "1")
    started = []

    class _FakeThread:
        def __init__(self, *args, **kwargs):
            self.kwargs = kwargs

        def start(self):
            started.append(self.kwargs.get("name"))

    monkeypatch.setattr(dataset.threading, "Thread", _FakeThread)
    assert dataset.start_dataset_scheduler() is True
    assert started == ["next302-dataset-sync"]
    assert dataset.start_dataset_scheduler() is False
    assert len(started) == 1
