"""r43 后台任务抢主链路：janitor 轻/重分离。

EA（播放前台）只跑轻量清理（heavy=False），图片/缩略图/分类回填等重活
由 worker 进程做——原来两边每 600 秒各跑一整套。
"""
from __future__ import annotations

import os

os.environ.setdefault("DATABASE_TYPE", "sqlite")
os.environ.setdefault("REDIS_ENABLED", "false")

import time  # noqa: E402

import pytest  # noqa: E402
from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

from backend import database as _database  # noqa: E402
from backend import models as _core_models  # noqa: E402,F401 — 注册共享 Base 全部表
from backend.emby_server import facets as _facets  # noqa: E402
from backend.emby_server import image_store as _image_store  # noqa: E402
from backend.emby_server import maintenance as maint  # noqa: E402
from backend.emby_server import models as em  # noqa: E402
from backend.emby_server import streaming as _streaming  # noqa: E402
from backend.emby_server import tmdb_cache as _tmdb_cache  # noqa: E402


@pytest.fixture()
def mem_db(monkeypatch):
    engine = create_engine("sqlite://")
    em.Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    # janitor_tick 在函数内 from backend.database import SessionLocal
    monkeypatch.setattr(_database, "SessionLocal", Session)
    try:
        yield Session
    finally:
        engine.dispose()


def _patch_sections(monkeypatch):
    """把 janitor_tick 内各段函数全换成记录调用的桩，返回 (light_calls, heavy_calls)。"""
    light_calls, heavy_calls = [], []

    def _rec(calls, name, ret=0):
        def _fn(*a, **k):
            calls.append(name)
            return ret
        return _fn

    # ---- 轻量（EA 保留）----
    # 2026-10-17 转码删除：reap_stale_transcodes / cleanup_transcode_orphans 已随整套删除
    monkeypatch.setattr(maint, "reap_stale_playback_sessions", _rec(light_calls, "reap_stale_playback_sessions"))
    monkeypatch.setattr(maint, "prune_playback_sessions", _rec(light_calls, "prune_playback_sessions"))
    monkeypatch.setattr(maint, "purge_expired_emby_tokens", _rec(light_calls, "purge_expired_emby_tokens"))
    monkeypatch.setattr(maint, "prune_subtitle_cache", _rec(light_calls, "prune_subtitle_cache"))
    monkeypatch.setattr(maint, "_idle_gc_if_quiet", _rec(light_calls, "idle_gc", ret=1))
    # ---- 重活（EA 跳过）----
    monkeypatch.setattr(_facets, "ensure_backfill", _rec(heavy_calls, "facets_backfill"))
    monkeypatch.setattr(_facets, "prune_orphans", _rec(heavy_calls, "facets_orphans"))
    monkeypatch.setattr(maint, "prune_scan_dir_states", _rec(heavy_calls, "scan_dir_states"))
    monkeypatch.setattr(maint, "prune_scan_runs", _rec(heavy_calls, "scan_runs"))
    monkeypatch.setattr(_image_store, "prune",
                        lambda db: (heavy_calls.append("images_prune"),
                                    {"removed": 0, "freed_bytes": 0})[1])
    monkeypatch.setattr(_image_store, "backfill_thumbnails",
                        lambda db, **kw: (heavy_calls.append("thumbs_backfill"),
                                          {"generated": 0, "done": True, "last_id": 0})[1])
    monkeypatch.setattr(maint, "prune_ai_usage", _rec(heavy_calls, "ai_usage"))
    monkeypatch.setattr(maint, "optimize_query_plans", _rec(heavy_calls, "query_plans"))
    monkeypatch.setattr(_tmdb_cache, "prune",
                        lambda **kw: (heavy_calls.append("tmdb_prune"),
                                      {"expired_removed": 0, "evicted": 0})[1])
    return light_calls, heavy_calls


class TestJanitorHeavySplit:
    def test_light_mode_skips_heavy(self, mem_db, monkeypatch):
        light_calls, heavy_calls = _patch_sections(monkeypatch)
        result = maint.janitor_tick(heavy=False)
        assert heavy_calls == [], f"轻量模式不得跑重活，实际跑了: {heavy_calls}"
        assert light_calls, "轻量清理必须照常跑"
        # 结果口径不变：键齐全，重活值归零
        for key in ("item_facets_backfilled", "item_facets_orphans",
                    "scan_dir_states_pruned", "scan_runs_pruned",
                    "images_pruned", "images_freed_bytes", "ai_usage_pruned",
                    "thumbs_backfilled", "tmdb_cache_pruned"):
            assert key in result, f"结果缺键 {key}"
            assert result[key] in (0, False), f"{key} 轻量模式应为 0，实际 {result[key]}"
        assert "thumbs_backfill_skipped_active" not in result

    def test_full_mode_runs_heavy(self, mem_db, monkeypatch):
        light_calls, heavy_calls = _patch_sections(monkeypatch)
        result = maint.janitor_tick()  # 默认 heavy=True，老调用方行为不变
        assert light_calls, "轻量清理必须跑"
        for name in ("facets_backfill", "facets_orphans", "scan_dir_states",
                     "scan_runs", "images_prune", "thumbs_backfill",
                     "ai_usage", "query_plans", "tmdb_prune"):
            assert name in heavy_calls, f"全量模式漏了重活: {name}"
        assert result["thumbs_backfilled"] == 0
        assert result["thumbs_backfill_done"] is True

    def test_start_janitor_passes_heavy(self, mem_db, monkeypatch):
        seen = []
        monkeypatch.setattr(maint, "janitor_tick",
                            lambda heavy=True: seen.append(heavy) or {})
        old = maint._JANITOR_STARTED
        maint._JANITOR_STARTED = False
        try:
            assert maint.start_janitor(interval_seconds=3600, heavy=False) is True
            deadline = time.time() + 5
            while not seen and time.time() < deadline:
                time.sleep(0.05)
            assert seen == [False], f"EA 轻量模式必须透传 heavy=False，实际 {seen}"
        finally:
            maint._JANITOR_STARTED = old
