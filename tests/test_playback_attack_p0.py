"""播放链路修复（fix/playback-attack）：P0-1 跳转缓存 debug 日志 / P0-2 get_episodes
Fields=MediaSources / P1 详情页 .strm 预热。

before/after 证据：每个用例的 docstring 注明"before"在旧代码上的表现
（旧代码上跑这些用例会失败），"after"为新行为。
"""
from __future__ import annotations

import asyncio
import logging
import os
import time

import pytest

os.environ.setdefault("DATABASE_TYPE", "sqlite")
os.environ.setdefault("REDIS_ENABLED", "false")

from backend.emby_server import api
from backend.emby_server import models as em
from tests.test_items_dedup_sql import _req, seeded  # noqa: F401  复用独立种子库

STREAM_LOG = "backend.emby_server.streaming"


def _skip_records(caplog):
    return [r for r in caplog.records
            if r.name == STREAM_LOG and "redirect_cache_skip" in r.getMessage()]


def _write_records(caplog):
    return [r for r in caplog.records
            if r.name == STREAM_LOG and "redirect_cache_write" in r.getMessage()]


# ---------------------------------------------------------------------------
# P0-1：跳转缓存写入决策 debug 日志（只加日志，不改逻辑）
# ---------------------------------------------------------------------------

def test_redirect_cache_skip_logs_no_hops(caplog):
    """before：该分支直接 return，无任何日志——生产 0 写入时无法区分原因。
    after：记 reason=no_hops，带 file_id/hops/cached_hit/status。"""
    import backend.emby_server.streaming as st
    fid = "PA_LOG_NOHOPS"
    st._redirect_cache_invalidate(fid)
    try:
        with caplog.at_level(logging.DEBUG, logger=STREAM_LOG):
            st._maybe_cache_drive_redirect(
                fid, "https://drive.usercontent.google.com/download?id=X",
                0, False, 200)
        recs = _skip_records(caplog)
        assert recs, "before: 无日志，无法定位拦截分支"
        msg = recs[0].getMessage()
        assert "reason=no_hops" in msg, msg
        assert fid in msg, msg
        assert st._redirect_cache_get(fid) is None  # 逻辑不变：仍不写
    finally:
        st._redirect_cache_invalidate(fid)


def test_redirect_cache_skip_logs_bad_terminal_status(caplog):
    """before：403/404/410 分支无日志。after：记 reason=bad_terminal_status。"""
    import backend.emby_server.streaming as st
    fid = "PA_LOG_BADSTATUS"
    st._redirect_cache_invalidate(fid)
    try:
        with caplog.at_level(logging.DEBUG, logger=STREAM_LOG):
            st._maybe_cache_drive_redirect(
                fid, "https://drive.usercontent.google.com/download?id=X",
                2, False, 403)
        recs = _skip_records(caplog)
        assert recs, "before: 无日志，无法定位拦截分支"
        assert any("reason=bad_terminal_status" in r.getMessage() for r in recs)
        assert st._redirect_cache_get(fid) is None
    finally:
        st._redirect_cache_invalidate(fid)


def test_redirect_cache_skip_logs_hostname_mismatch(caplog):
    """before：hostname 拦截分支无日志（两大嫌疑之一）。
    after：记 reason=hostname_mismatch，带实际 host + 脱敏 URL。"""
    import backend.emby_server.streaming as st
    fid = "PA_LOG_HOST"
    st._redirect_cache_invalidate(fid)
    try:
        with caplog.at_level(logging.DEBUG, logger=STREAM_LOG):
            st._maybe_cache_drive_redirect(
                fid, "https://example.com/dl/x?foo=bar", 2, False, 200)
        recs = _skip_records(caplog)
        assert recs, "before: 无日志，无法定位拦截分支"
        msg = recs[0].getMessage()
        assert "reason=hostname_mismatch" in msg, msg
        assert "example.com" in msg, msg
        assert st._redirect_cache_get(fid) is None
    finally:
        st._redirect_cache_invalidate(fid)


def test_redirect_cache_skip_logs_signature_expired(caplog):
    """before：签名过期放弃写入分支无日志（两大嫌疑之二）。
    after：记 reason=signature_expired，带 sig_ttl。"""
    import backend.emby_server.streaming as st
    fid = "PA_LOG_EXPIRED"
    st._redirect_cache_invalidate(fid)
    try:
        expired = ("https://drive.usercontent.google.com/download?id=X&expires=%d"
                   % int(time.time() - 5))
        with caplog.at_level(logging.DEBUG, logger=STREAM_LOG):
            st._redirect_cache_store_drive_url(fid, expired)
        recs = _skip_records(caplog)
        assert recs, "before: 无日志，无法定位拦截分支"
        assert any("reason=signature_expired" in r.getMessage() for r in recs)
        assert st._redirect_cache_get(fid) is None  # 逻辑不变：过期仍不写
    finally:
        st._redirect_cache_invalidate(fid)


def test_redirect_cache_write_logs(caplog):
    """after：成功写入记 redirect_cache_write，带 ttl/sig_ttl/脱敏 URL。"""
    import backend.emby_server.streaming as st
    fid = "PA_LOG_WRITE"
    st._redirect_cache_invalidate(fid)
    try:
        good = "https://drive.usercontent.google.com/download?id=X"
        with caplog.at_level(logging.DEBUG, logger=STREAM_LOG):
            st._maybe_cache_drive_redirect(fid, good, 2, False, 200)
        recs = _write_records(caplog)
        assert recs, "成功写入应记一条 redirect_cache_write"
        msg = recs[0].getMessage()
        assert fid in msg and "ttl=" in msg, msg
        assert st._redirect_cache_get(fid) == good  # 逻辑不变：照常写入
    finally:
        st._redirect_cache_invalidate(fid)


def test_redirect_cache_logs_never_contain_secret_values(caplog):
    """脱敏铁律：日志里只出现参数名，绝不出现签名值/密钥。

    before：无日志可言；after：每条日志的参数值都被剥掉，只剩参数名。
    """
    import backend.emby_server.streaming as st
    secret = "SECRET_SIG_VALUE_abc123xyz"
    fid = "PA_LOG_SECRET"
    st._redirect_cache_invalidate(fid)
    try:
        poisoned = ("https://other.example.com/x?signature=%s&expires=9999999999"
                    % secret)
        good = ("https://drive.usercontent.google.com/download?id=X&signature=%s"
                % secret)
        with caplog.at_level(logging.DEBUG, logger=STREAM_LOG):
            st._maybe_cache_drive_redirect(fid, poisoned, 2, False, 200)
            st._redirect_cache_store_drive_url(fid, good)
        texts = [r.getMessage() for r in caplog.records if r.name == STREAM_LOG]
        assert texts, "应有日志产生"
        for t in texts:
            assert secret not in t, f"日志泄漏签名值: {t}"
            assert "abc123xyz" not in t, f"日志泄漏签名值片段: {t}"
        joined = " ".join(texts)
        assert "signature" in joined, "参数名应保留（定位证据）"
    finally:
        st._redirect_cache_invalidate(fid)


def test_desensitized_url_for_log():
    """脱敏 helper：保留 scheme/host/path + 参数名，丢弃一切参数值。"""
    from backend.emby_server.streaming import _desensitized_url_for_log as d
    out = d("https://drive.usercontent.google.com/download?id=X&signature=S3CR3T&expires=123")
    assert "S3CR3T" not in out
    assert "drive.usercontent.google.com" in out
    assert "signature" in out and "expires" in out and "id" in out
    assert d("https://h/x") == "https://h/x"
    assert d("not a url %%") == "<unparseable>" or "not a url" in d("not a url %%")


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
