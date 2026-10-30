"""扫描队列派发失败回归测试（审计 A-050）。

问题：``scan_queue._dispatch_locked`` 先把任务离队、标 RUNNING、占挂载，
最后才 ``Thread.start()``，全程无 try/except。start 失败（线程耗尽）时
任务永久卡 RUNNING、挂载与并行槽位永久被占；``_scheduler_loop`` 自身也无
try/except，派发异常会直接杀死调度线程。

修法：start 失败时回滚（出 _RUNNING、按名释放挂载、记 failed 进历史，
不自动重入队——重入队会在 _pump_locked 的 while 循环里立即再派发、
死循环）；调度循环单轮异常不退出。

本文件只碰内存队列状态，不依赖真实 Redis / 真实扫描。
"""
import os
import sys
import threading

os.environ.setdefault("DATABASE_TYPE", "sqlite")
os.environ.setdefault("REDIS_ENABLED", "false")

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest

from backend.emby_server import scan_queue as sq
from backend.emby_server.scan_queue import (
    ScanTask,
    STATE_FAILED,
    STATE_QUEUED,
    STATE_RUNNING,
)


@pytest.fixture()
def _clean_queue_state():
    """快照并恢复 scan_queue 进程内全局状态，避免污染同进程其他测试。"""
    saved = (
        list(sq._QUEUE),
        dict(sq._RUNNING),
        dict(sq._MOUNT_OWNER),
        list(sq._HISTORY),
        sq._SCHEDULER,
    )
    sq._QUEUE.clear()
    sq._RUNNING.clear()
    sq._MOUNT_OWNER.clear()
    sq._HISTORY.clear()
    sq._SCHEDULER = None
    try:
        yield
    finally:
        sq._QUEUE.clear()
        sq._QUEUE.extend(saved[0])
        sq._RUNNING.clear()
        sq._RUNNING.update(saved[1])
        sq._MOUNT_OWNER.clear()
        sq._MOUNT_OWNER.update(saved[2])
        sq._HISTORY.clear()
        sq._HISTORY.extend(saved[3])
        sq._SCHEDULER = saved[4]


def _boom(self):
    raise RuntimeError("can't start new thread")


def test_dispatch_start_failure_marks_failed_and_releases(
    monkeypatch, _clean_queue_state
):
    """start 失败：任务记 failed、不在 _RUNNING、挂载已释放、可再次入队派发。"""
    task = ScanTask(library_id=99, name="测试库", trigger="manual",
                    mount_ids=(7,))
    sq._QUEUE.append(task)
    monkeypatch.setattr(threading.Thread, "start", _boom)
    with sq._COND:
        dispatched = sq._pump_locked()
    assert dispatched == 0
    assert task.state == STATE_FAILED
    assert task.result == STATE_FAILED
    assert task.error and "线程" in task.error
    assert task.finished_at is not None
    assert 99 not in sq._RUNNING
    assert sq._MOUNT_OWNER.get(7) is None
    assert task not in sq._QUEUE
    assert task in sq._HISTORY

    # 槽位与挂载已释放：新任务可再次入队并正常派发（start 改为只记录不真跑，
    # 避免 _run_task 需要 DB；派发成功以进入 _RUNNING + 占挂载为准）。
    started = []

    def _record(self):
        started.append(self)

    monkeypatch.setattr(threading.Thread, "start", _record)
    retry = ScanTask(library_id=99, name="测试库", trigger="manual",
                     mount_ids=(7,))
    sq._QUEUE.append(retry)
    with sq._COND:
        dispatched = sq._pump_locked()
    assert dispatched == 1
    assert retry.state == STATE_RUNNING
    assert sq._RUNNING.get(99) is retry
    assert sq._MOUNT_OWNER.get(7) == 99
    assert len(started) == 1


def test_scheduler_loop_survives_pump_exception(
    monkeypatch, _clean_queue_state
):
    """调度循环：单轮派发异常不杀死调度线程，后续轮次仍能跑。"""
    calls = []

    def _flaky_pump():
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("pump boom")
        return 0

    monkeypatch.setattr(sq, "_pump_locked", _flaky_pump)
    # 队列里留一个任务：pump 返回 0 时走 wait 分支（不退出），线程保持存活。
    sq._QUEUE.append(ScanTask(library_id=98, name="测试库", trigger="manual"))
    thread = threading.Thread(target=sq._scheduler_loop, name="scan-scheduler-test")
    thread.daemon = True
    thread.start()
    try:
        import time

        deadline = time.time() + 5.0
        while time.time() < deadline and len(calls) < 2:
            time.sleep(0.05)
        assert len(calls) >= 2, "调度循环在首轮异常后没有继续跑下一轮"
        assert thread.is_alive(), "调度线程在派发异常后死了"
    finally:
        # 清空队列 + 唤醒，让调度循环走「队列空 → 退出」分支正常结束。
        with sq._COND:
            sq._QUEUE.clear()
            sq._COND.notify_all()
        thread.join(timeout=5.0)
        assert not thread.is_alive()
