"""扫描队列合并逻辑：新请求的扫描范围不得被静默丢弃。

回归测试（2026-10-13 生产实锤）：生成器的整库扫描被追新（chase-new）的
定向扫描合并吞掉，新文件目录永不被遍历，added 恒 0。
"""
import dataclasses
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from backend.emby_server import scan_queue
from backend.emby_server.scan_queue import (
    ScanTask, STATE_QUEUED, STATE_RUNNING, _try_merge_scan_task,
)


@dataclasses.dataclass(frozen=True)
class _FakeSnap:
    limit_prefixes: tuple = ()
    targeted: bool = False


def _task(prefixes=(), targeted=False, state=STATE_QUEUED):
    return ScanTask(
        library_id=46, name="欧美剧", trigger="chase-new",
        snapshot=_FakeSnap(limit_prefixes=tuple(prefixes), targeted=targeted),
        state=state,
    )


def test_whole_library_upgrades_queued_targeted():
    """整库请求 + 排队中的定向任务 → 升级为整库（不丢范围）。"""
    existing = _task(prefixes=("/strm/剧集/欧美剧/abc",), targeted=True)
    new_snap = _FakeSnap(limit_prefixes=(), targeted=False)
    assert _try_merge_scan_task(existing, new_snap) is True
    assert existing.snapshot.limit_prefixes == ()
    assert existing.snapshot.targeted is False


def test_targeted_union_prefixes():
    """定向 + 排队中的定向 → 前缀取并集。"""
    existing = _task(prefixes=("/a", "/b"), targeted=True)
    new_snap = _FakeSnap(limit_prefixes=("/b", "/c"), targeted=True)
    assert _try_merge_scan_task(existing, new_snap) is True
    assert existing.snapshot.limit_prefixes == ("/a", "/b", "/c")
    assert existing.snapshot.targeted is True


def test_targeted_into_whole_library_noop():
    """定向请求 + 已有整库任务 → 无需改动（整库是超集），合并成功。"""
    existing = _task(prefixes=(), targeted=False)
    new_snap = _FakeSnap(limit_prefixes=("/a",), targeted=True)
    assert _try_merge_scan_task(existing, new_snap) is True
    assert existing.snapshot.limit_prefixes == ()
    assert existing.snapshot.targeted is False


def test_running_task_not_merged():
    """已有任务正在跑 → 不合并（调用方应另起排队任务）。"""
    existing = _task(prefixes=("/a",), targeted=True, state=STATE_RUNNING)
    new_snap = _FakeSnap(limit_prefixes=(), targeted=False)
    assert _try_merge_scan_task(existing, new_snap) is False
    # 运行中的任务快照不得被改动
    assert existing.snapshot.limit_prefixes == ("/a",)
    assert existing.snapshot.targeted is True


def test_pick_locked_skips_same_library_running():
    """_pick_locked：同库已有任务在跑时，排队任务不得被派发（防并发扫同库）。"""
    running = _task(state=STATE_RUNNING)
    queued = _task(state=STATE_QUEUED)
    queued2_lib = ScanTask(
        library_id=47, name="纪录片", trigger="manual",
        snapshot=_FakeSnap(), state=STATE_QUEUED)
    scan_queue._RUNNING[46] = running
    scan_queue._QUEUE.extend([queued, queued2_lib])
    try:
        picked = scan_queue._pick_locked()
        # lib 46 的排队任务必须被跳过，只能选 lib 47 的
        assert picked is queued2_lib
    finally:
        scan_queue._RUNNING.pop(46, None)
        scan_queue._QUEUE.clear()
