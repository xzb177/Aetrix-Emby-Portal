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
    ScanTask, STATE_QUEUED, STATE_RUNNING,
    _try_merge_scan_task, _merge_scope_into, _scope_covered_by,
)


@dataclasses.dataclass(frozen=True)
class _FakeSnap:
    limit_prefixes: tuple = ()
    targeted: bool = False
    name: str = "测试库"
    mount_ids: tuple = ()
    paths: tuple = ("/strm/测试",)


def _task(prefixes=(), targeted=False, state=STATE_QUEUED):
    return ScanTask(
        library_id=46, name="欧美剧", trigger="chase-new",
        snapshot=_FakeSnap(limit_prefixes=tuple(prefixes), targeted=targeted),
        state=state,
    )


def test_queued_whole_library_upgrades_targeted():
    """排队中：整库请求把定向任务升级为整库（不丢范围）。"""
    existing = _task(prefixes=("/strm/剧集/欧美剧/abc",), targeted=True)
    new_snap = _FakeSnap(limit_prefixes=(), targeted=False)
    assert _try_merge_scan_task(existing, new_snap, "strm-gen") is True
    assert existing.snapshot.limit_prefixes == ()
    assert existing.snapshot.targeted is False
    assert existing.pending_rescan_snapshot is None  # 排队中直接合并，无需补扫


def test_queued_targeted_union_prefixes():
    """排队中：定向 + 定向 → 前缀取并集。"""
    existing = _task(prefixes=("/a", "/b"), targeted=True)
    new_snap = _FakeSnap(limit_prefixes=("/b", "/c"), targeted=True)
    assert _try_merge_scan_task(existing, new_snap, "chase-new") is True
    assert existing.snapshot.limit_prefixes == ("/a", "/b", "/c")
    assert existing.pending_rescan_snapshot is None


def test_queued_targeted_into_whole_noop():
    """排队中：定向请求并入整库任务 → 超集，无需改动。"""
    existing = _task(prefixes=(), targeted=False)
    new_snap = _FakeSnap(limit_prefixes=("/a",), targeted=True)
    assert _try_merge_scan_task(existing, new_snap, "chase-new") is True
    assert existing.snapshot.limit_prefixes == ()
    assert existing.pending_rescan_snapshot is None


def test_running_wider_scope_registers_rescan():
    """在跑：新请求范围更宽（整库）→ 登记补扫，快照不动，仍返回合并。"""
    existing = _task(prefixes=("/a",), targeted=True, state=STATE_RUNNING)
    new_snap = _FakeSnap(limit_prefixes=(), targeted=False)
    assert _try_merge_scan_task(existing, new_snap, "strm-gen") is True
    # 运行中的快照不得改动
    assert existing.snapshot.limit_prefixes == ("/a",)
    assert existing.snapshot.targeted is True
    # 但补扫已登记为整库
    assert existing.pending_rescan_snapshot is not None
    assert existing.pending_rescan_snapshot.limit_prefixes == ()
    assert existing.pending_rescan_snapshot.targeted is False
    assert existing.pending_rescan_trigger == "strm-gen"


def test_running_covered_scope_no_rescan():
    """在跑：新请求范围已被当前任务覆盖 → 无需补扫。"""
    existing = _task(prefixes=("/a", "/b"), targeted=True, state=STATE_RUNNING)
    new_snap = _FakeSnap(limit_prefixes=("/a",), targeted=True)
    assert _try_merge_scan_task(existing, new_snap, "chase-new") is True
    assert existing.pending_rescan_snapshot is None


def test_running_pending_scope_merges():
    """在跑：多次登记补扫时范围合并（整库胜出）。"""
    existing = _task(prefixes=("/a",), targeted=True, state=STATE_RUNNING)
    _try_merge_scan_task(existing, _FakeSnap(("/b",), True), "chase-new")
    assert existing.pending_rescan_snapshot.limit_prefixes == ("/b",)
    _try_merge_scan_task(existing, _FakeSnap((), False), "strm-gen")
    assert existing.pending_rescan_snapshot.limit_prefixes == ()
    assert existing.pending_rescan_snapshot.targeted is False


def test_merge_scope_into_whole_wins():
    s = _merge_scope_into(_FakeSnap(("/a",), True), _FakeSnap((), False))
    assert s.limit_prefixes == () and s.targeted is False


def test_scope_covered_by():
    assert _scope_covered_by(_FakeSnap((), False), _FakeSnap(("/a",), True)) is True
    assert _scope_covered_by(_FakeSnap(("/a", "/b"), True), _FakeSnap(("/a",), True)) is True
    assert _scope_covered_by(_FakeSnap(("/a",), True), _FakeSnap(("/b",), True)) is False
    assert _scope_covered_by(_FakeSnap(("/a",), True), _FakeSnap((), False)) is False
