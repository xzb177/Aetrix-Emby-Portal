"""PA-006 回归：排队/在跑任务合并时不得丢弃 force_full。

管理员点的「全量扫描」在同库已有排队或在跑任务时被合并静默降为增量，
界面仍报 success。本文件先红后绿：修前 force_full 相关断言应红。
"""
import dataclasses
import sys
import os
import types

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from backend.emby_server import scan_queue
from backend.emby_server.scan_queue import (
    ScanTask, STATE_QUEUED, STATE_RUNNING,
    _try_merge_scan_task, _merge_scope_into,
)
from backend.emby_server.scanner import _incremental_on


@dataclasses.dataclass(frozen=True)
class _FakeSnap:
    limit_prefixes: tuple = ()
    targeted: bool = False
    force_full: bool = False
    name: str = "测试库"
    mount_ids: tuple = ()
    paths: tuple = ("/strm/测试",)


def _task(state=STATE_QUEUED, force_full=False, prefixes=(), targeted=False):
    return ScanTask(
        library_id=46, name="欧美剧", trigger="chase-new",
        snapshot=_FakeSnap(limit_prefixes=tuple(prefixes),
                           targeted=targeted, force_full=force_full),
        state=state,
    )


def test_merge_scope_into_force_full_upgrades():
    """合并：新快照 force_full=True 时结果必须升格（只升不降）。"""
    merged = _merge_scope_into(_FakeSnap(), _FakeSnap(force_full=True))
    assert merged.force_full is True
    assert merged.limit_prefixes == () and merged.targeted is False


def test_merge_scope_into_force_full_never_downgrades():
    """合并：旧快照已是全量，新请求是增量时不得降回增量。"""
    merged = _merge_scope_into(_FakeSnap(force_full=True), _FakeSnap())
    assert merged.force_full is True


def test_queued_full_request_upgrades_incremental_task():
    """排队中：增量任务 + 全量请求 → 快照升格为全量，无需补扫。"""
    existing = _task(state=STATE_QUEUED, force_full=False)
    assert _try_merge_scan_task(existing, _FakeSnap(force_full=True), "manual") is True
    assert existing.snapshot.force_full is True
    assert existing.pending_rescan_snapshot is None


def test_running_covered_but_full_registers_rescan():
    """在跑：范围已被覆盖，但新请求要全量 → 必须登记全量补扫。"""
    existing = _task(state=STATE_RUNNING, force_full=False)
    assert _try_merge_scan_task(existing, _FakeSnap(force_full=True), "manual") is True
    assert existing.snapshot.force_full is False  # 在跑快照不得改动
    assert existing.pending_rescan_snapshot is not None
    assert existing.pending_rescan_snapshot.force_full is True


def test_running_pending_force_full_upgrades():
    """在跑：已登记的增量补扫被全量请求覆盖 → 补扫快照升格为全量。"""
    existing = _task(state=STATE_RUNNING, force_full=False,
                     prefixes=("/x",), targeted=True)
    _try_merge_scan_task(existing, _FakeSnap(("/a",), True), "chase-new")
    assert existing.pending_rescan_snapshot.force_full is False
    _try_merge_scan_task(existing, _FakeSnap(force_full=True), "manual")
    assert existing.pending_rescan_snapshot.force_full is True


def test_rescan_reentry_keeps_force_full():
    """补扫重入队：全量补扫快照再与增量请求合并时不得丢全量。"""
    existing = _task(state=STATE_QUEUED, force_full=True)
    assert _try_merge_scan_task(existing, _FakeSnap(), "chase-new") is True
    assert existing.snapshot.force_full is True


def test_merged_full_snapshot_disables_incremental():
    """补扫快照 force_full=True 时 _incremental_on 必须返回 False。"""
    merged = _merge_scope_into(_FakeSnap(), _FakeSnap(force_full=True))
    ctx = types.SimpleNamespace(snap=merged)
    assert _incremental_on(ctx) is False
