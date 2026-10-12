"""播放链路修复（fix/playback-attack）：P0-2 get_episodes
Fields=MediaSources / P1 详情页 .strm 预热。
（P0-1 跳转缓存已删除，相关日志测试一并移除）

before/after 证据：每个用例的 docstring 注明"before"在旧代码上的表现
（旧代码上跑这些用例会失败），"after"为新行为。
"""
from __future__ import annotations

import asyncio
import os
import time

import pytest

os.environ.setdefault("DATABASE_TYPE", "sqlite")
os.environ.setdefault("REDIS_ENABLED", "false")

from backend.emby_server import api
from backend.emby_server import models as em
from tests.test_items_dedup_sql import _req, seeded  # noqa: F401  复用独立种子库



# ---------------------------------------------------------------------------
# P0-2：get_episodes 支持 Fields=MediaSources
# ---------------------------------------------------------------------------

def test_get_episodes_fields_mediasources(seeded):
    """before：无论带不带 Fields，集数 MediaSources 恒为 []（_item_dto 默认）。
    after：Fields 含 MediaSources 时每集下发 1 条 MediaSource；不带时保持 []。"""
    db, user = seeded.db, seeded.user
    series = db.query(em.MediaItem).filter(
        em.MediaItem.item_type == "series", em.MediaItem.tmdb_id == "500").first()
    assert series is not None
    orig_scope = api._library_scope
    api._library_scope = lambda db, user: None  # noqa: E731
    try:
        plain = api.get_episodes(item_id=series.guid, request=_req({}),
                                 user=user, db=db)
        assert plain["Items"], "种子数据应有集"
        assert all(i["MediaSources"] == [] for i in plain["Items"]), \
            "不带 Fields 时保持旧行为：MediaSources 为 []"

        full = api.get_episodes(
            item_id=series.guid,
            request=_req({"Fields": "Overview,MediaSources"}),
            user=user, db=db)
        assert full["TotalRecordCount"] == plain["TotalRecordCount"]
        assert full["Items"], "种子数据应有集"
        for item in full["Items"]:
            assert len(item["MediaSources"]) == 1, \
                f"Fields=MediaSources 时每集应下发 1 条 MediaSource: {item['Id']}"
            assert item["MediaSources"][0]["Id"] == item["Id"]

        # 大小写不敏感
        lower = api.get_episodes(
            item_id=series.guid, request=_req({"Fields": "mediasources"}),
            user=user, db=db)
        assert all(len(i["MediaSources"]) == 1 for i in lower["Items"])
    finally:
        api._library_scope = orig_scope


# ---------------------------------------------------------------------------
# P1：详情页预热 .strm 链路（BackgroundTasks，同步端点保持同步）
# ---------------------------------------------------------------------------

class _FakeBackgroundTasks:
    """只收任务不执行，断言调度行为用。"""
    def __init__(self):
        self.tasks = []

    def add_task(self, func, *args, **kwargs):
        self.tasks.append((func, args, kwargs))


def _first_movie(seeded):
    db = seeded.db
    return db.query(em.MediaItem).filter(em.MediaItem.item_type == "movie").first()


def test_detail_page_schedules_strm_prewarm(seeded, monkeypatch):
    """before：get_item_detail 没有 background_tasks 参数，详情页从不触发预热。
    after：.strm 详情页调度一个后台任务；运行它会调到 strm_prewarm.maybe_warm。"""
    db, user = seeded.db, seeded.user
    movie = _first_movie(seeded)
    movie.file_path = "/media/t.strm"
    db.commit()
    orig_scope = api._library_scope
    api._library_scope = lambda db, user: None  # noqa: E731
    calls = []

    async def _fake_maybe_warm(_db, item):
        calls.append((getattr(item, "file_path", None),
                      getattr(item, "guid", None)))

    monkeypatch.setattr("backend.emby_server.strm_prewarm.maybe_warm",
                        _fake_maybe_warm)
    try:
        bg = _FakeBackgroundTasks()
        payload = api.get_item_detail(item_id=movie.guid, request=_req({}),
                                      user=user, db=db, background_tasks=bg)
        assert payload["Id"] == movie.guid  # 详情页正常返回
        assert len(bg.tasks) == 1, "before: 0 个任务（无预热）；after: 1 个"
        # 运行后台任务：应调到 maybe_warm，并关闭独立 session
        asyncio.run(bg.tasks[0][0]())
        assert calls == [("/media/t.strm", movie.guid)], calls
    finally:
        api._library_scope = orig_scope
        movie.file_path = "/m/1.mkv"
        db.commit()


def test_detail_page_no_prewarm_for_non_strm(seeded):
    """非 .strm 详情页不调度预热（端点侧先过滤，避免无谓的后台任务+读配置）。"""
    db, user = seeded.db, seeded.user
    movie = _first_movie(seeded)
    assert movie.file_path.endswith(".mkv")
    orig_scope = api._library_scope
    api._library_scope = lambda db, user: None  # noqa: E731
    try:
        bg = _FakeBackgroundTasks()
        api.get_item_detail(item_id=movie.guid, request=_req({}),
                            user=user, db=db, background_tasks=bg)
        assert bg.tasks == [], "非 .strm 不应调度预热任务"
    finally:
        api._library_scope = orig_scope


def test_detail_page_works_without_background_tasks(seeded):
    """background_tasks 默认为 None：现有直接调用方（无该参数）不受影响。"""
    db, user = seeded.db, seeded.user
    movie = _first_movie(seeded)
    orig_scope = api._library_scope
    api._library_scope = lambda db, user: None  # noqa: E731
    try:
        payload = api.get_item_detail(item_id=movie.guid, request=_req({}),
                                      user=user, db=db)
        assert payload["Id"] == movie.guid
    finally:
        api._library_scope = orig_scope
