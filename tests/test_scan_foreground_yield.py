"""扫描前台让路：批次边界调用让路钩子（2026-10-16 网页加速批）。

只验证接线（_iter_prepared 在批次边界调用 _maybe_yield_for_foreground），
不跑真实扫描：_prepare_and_prefetch 等重活全部 monkeypatch 掉。
"""
from __future__ import annotations

import os
from types import SimpleNamespace

os.environ.setdefault("DATABASE_TYPE", "sqlite")

from backend.emby_server import scanner  # noqa: E402


def _run_iter_prepared(monkeypatch, n_files, batch_size=3):
    monkeypatch.setattr(scanner, "SCAN_BATCH", batch_size)
    monkeypatch.setattr(scanner, "_library_exists", lambda db, lib_id: True)
    monkeypatch.setattr(scanner, "_prepare_and_prefetch", lambda db, current, ctx, pool: [])
    monkeypatch.setattr(scanner, "_claim_batch_guids", lambda *a, **k: None)
    monkeypatch.setattr(scanner, "_store_dir_states", lambda *a, **k: None)
    monkeypatch.setattr(scanner, "_source_entry", lambda *a, **k: {})

    calls = []
    monkeypatch.setattr(
        scanner, "_maybe_yield_for_foreground", lambda: calls.append(1)
    )

    commits = []
    db = SimpleNamespace(
        commit=lambda: commits.append(1),
        flush=lambda: None,
        rollback=lambda: None,
    )
    ctx = SimpleNamespace(seen_guids=set(), lib_id=7)
    counted = SimpleNamespace(total=0)
    stats = {}
    scanner._iter_prepared(
        ctx,
        [object() for _ in range(n_files)],
        None,
        db,
        lambda sf, p: None,
        "test",
        counted,
        stats,
        [],
    )
    return calls, commits


class TestScanForegroundYieldWiring:
    def test_yield_called_at_each_batch_boundary(self, monkeypatch):
        # 3 个一批、共 4 个文件：1 个整批边界 + 1 个尾批 = 2 次让路检查
        calls, _ = _run_iter_prepared(monkeypatch, 4, batch_size=3)
        assert len(calls) == 2

    def test_no_files_no_yield(self, monkeypatch):
        calls, _ = _run_iter_prepared(monkeypatch, 0, batch_size=3)
        assert calls == []

    def test_exact_batch_single_yield(self, monkeypatch):
        # 正好一批：整批 1 次 + 尾批为空不触发 = 1 次
        calls, _ = _run_iter_prepared(monkeypatch, 3, batch_size=3)
        assert len(calls) == 1

    def test_yield_never_breaks_scan(self, monkeypatch):
        # 让路钩子抛异常（防御性）：扫描照样跑完
        monkeypatch.setattr(scanner, "SCAN_BATCH", 3)
        monkeypatch.setattr(scanner, "_library_exists", lambda db, lib_id: True)
        monkeypatch.setattr(scanner, "_prepare_and_prefetch", lambda db, current, ctx, pool: [])
        monkeypatch.setattr(scanner, "_claim_batch_guids", lambda *a, **k: None)
        monkeypatch.setattr(scanner, "_store_dir_states", lambda *a, **k: None)
        monkeypatch.setattr(scanner, "_source_entry", lambda *a, **k: {})

        def boom():
            raise RuntimeError("yield hook exploded")

        monkeypatch.setattr(scanner, "_maybe_yield_for_foreground", boom)
        db = SimpleNamespace(
            commit=lambda: None, flush=lambda: None, rollback=lambda: None
        )
        ctx = SimpleNamespace(seen_guids=set(), lib_id=7)
        # 不抛异常就算过（4 个文件正常走完两批）
        scanner._iter_prepared(
            ctx, [object() for _ in range(4)], None, db,
            lambda sf, p: None, "test", SimpleNamespace(total=0), {}, [],
        )
