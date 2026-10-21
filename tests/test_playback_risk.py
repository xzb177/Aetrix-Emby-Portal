"""播放风控看板（2026-10-21）：存在性正缓存 TTL env 化 + 计数 + 叠播聚合。"""
import os

os.environ.setdefault("DATABASE_TYPE", "sqlite")
os.environ.setdefault("REDIS_ENABLED", "false")

import logging
import uuid
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

from backend.database import SessionLocal, init_db
from backend.emby_server import api, drive_changes, playback_risk
from backend.emby_server import models as em
from backend.models import WebUser

init_db()

DRIVE = "0AShared"
FID = "fileX"


@pytest.fixture(autouse=True)
def _iso(monkeypatch):
    monkeypatch.setattr(playback_risk, "_redis", lambda: None)
    # 测试里不拉后台线程，刷写由用例直接调 _flush_once。
    monkeypatch.setattr(playback_risk, "_ensure_flusher", lambda: None)
    playback_risk.reset()
    api._drive_exists_cache.clear()
    monkeypatch.setattr(drive_changes, "get_token_for_drive",
                        lambda drive_id, force_refresh=False: "tok")
    yield
    api._drive_exists_cache.clear()
    playback_risk.reset()


def _http(monkeypatch, code):
    calls = []

    def fake(url, *, params=None, headers=None, timeout=None):
        calls.append(url)
        return SimpleNamespace(status_code=code)

    monkeypatch.setattr(api, "_drive_exists_http_get", fake)
    return calls


def test_ttl_env_default_and_bounds(monkeypatch, caplog):
    """解析在启动时做一次。热路径读缓存值，非法值不会每次播放再 warning。"""
    monkeypatch.delenv("DRIVE_EXISTS_CACHE_TTL_SEC", raising=False)
    assert api._drive_exists_cache_ttl_from_env() == 600
    monkeypatch.setenv("DRIVE_EXISTS_CACHE_TTL_SEC", "-5")
    assert api._drive_exists_cache_ttl_from_env() == 0
    monkeypatch.setenv("DRIVE_EXISTS_CACHE_TTL_SEC", "120")
    assert api._drive_exists_cache_ttl_from_env() == 120
    monkeypatch.setenv("DRIVE_EXISTS_CACHE_TTL_SEC", "abc")
    with caplog.at_level(logging.WARNING):
        assert api._drive_exists_cache_ttl_from_env() == 600
    assert any("DRIVE_EXISTS_CACHE_TTL_SEC" in r.message for r in caplog.records)
    caplog.clear()
    with caplog.at_level(logging.WARNING):
        for _ in range(5):
            api.drive_exists_cache_ttl()
    assert not any("DRIVE_EXISTS_CACHE_TTL_SEC" in r.message for r in caplog.records)


def test_cache_hit_miss_counted(monkeypatch):
    monkeypatch.setattr(api, "_cache_ttl_sec", 600)
    calls = _http(monkeypatch, 200)
    for _ in range(3):
        api._drive_file_exists_sync(DRIVE, FID)
    assert len(calls) == 1
    c = playback_risk.process_counters()
    assert c["exists_cache_miss"] == 1 and c["exists_cache_hit"] == 2


def test_cache_entry_uses_env_ttl(monkeypatch):
    monkeypatch.setattr(api, "_cache_ttl_sec", 600)
    _http(monkeypatch, 200)
    api._drive_file_exists_sync(DRIVE, FID)
    import time
    exp = api._drive_exists_cache[(DRIVE, FID)]
    assert 590 < exp - time.monotonic() <= 600


def test_ttl_zero_disables_cache(monkeypatch):
    monkeypatch.setattr(api, "_cache_ttl_sec", 0)
    calls = _http(monkeypatch, 200)
    api._drive_file_exists_sync(DRIVE, FID)
    api._drive_file_exists_sync(DRIVE, FID)
    assert len(calls) == 2
    assert api._drive_exists_cache == {}


def test_404_and_errors_not_cached_and_counted(monkeypatch):
    calls = _http(monkeypatch, 404)
    for _ in range(2):
        with pytest.raises(api._DriveFileGone):
            api._drive_file_exists_sync(DRIVE, FID)
    assert len(calls) == 2
    _http(monkeypatch, 429)
    with pytest.raises(api._DriveFileUnavailable):
        api._drive_file_exists_sync(DRIVE, FID)
    _http(monkeypatch, 403)
    with pytest.raises(api._DriveFileUnavailable):
        api._drive_file_exists_sync(DRIVE, FID)
    assert api._drive_exists_cache == {}
    c = playback_risk.process_counters()
    assert c["exists_404"] == 2 and c["exists_503"] == 2
    assert c["drive_429"] == 1 and c["drive_403"] == 1


def test_cache_size_bound(monkeypatch):
    monkeypatch.setattr(api, "_DRIVE_EXISTS_CACHE_MAX", 5)
    for i in range(12):
        api._drive_exists_cache_put(DRIVE, f"f{i}")
    assert len(api._drive_exists_cache) <= 5


class _Redis:
    def __init__(self, boom=False):
        self.ops = []
        self.boom = boom

    def hincrby(self, key, name, n):
        if self.boom:
            raise RuntimeError("slow redis")
        self.ops.append(("hincrby", key, name, n))

    def hsetnx(self, key, field, val):
        if self.boom:
            raise RuntimeError("slow redis")
        self.ops.append(("hsetnx", key, field, val))


def test_bump_accumulates_until_flush(monkeypatch):
    rc = _Redis()
    monkeypatch.setattr(playback_risk, "_redis", lambda: rc)
    playback_risk.bump("redirect_302")
    playback_risk.bump("redirect_302", 2)
    assert rc.ops == []
    assert playback_risk.process_counters()["redirect_302"] == 3
    playback_risk._flush_once()
    assert ("hincrby", playback_risk.REDIS_KEY, "redirect_302", 3) in rc.ops
    rc.ops.clear()
    playback_risk._flush_once()
    assert rc.ops == []


def test_flush_redis_missing_drops_pending():
    playback_risk.bump("exists_404")
    playback_risk._flush_once()
    assert playback_risk._pending["exists_404"] == 0
    assert playback_risk.process_counters()["exists_404"] == 1


def test_flush_redis_error_does_not_raise(monkeypatch):
    monkeypatch.setattr(playback_risk, "_redis", lambda: _Redis(boom=True))
    playback_risk.bump("drive_429", 4)
    playback_risk._flush_once()
    assert playback_risk._pending["drive_429"] == 4
    assert playback_risk.process_counters()["drive_429"] == 4


def test_observe_traffic_counts_302():
    from starlette.responses import RedirectResponse
    api._observe_traffic(RedirectResponse("https://x", status_code=302))
    assert playback_risk.process_counters()["redirect_302"] == 1


def test_share_rate_limit_counted(monkeypatch):
    from backend.emby_server import drive_share
    monkeypatch.setattr(drive_share, "_http_post",
                        lambda *a, **k: SimpleNamespace(status_code=429, text="", json=lambda: {}))
    drive_share.ensure_shared("fid", "tok")
    assert playback_risk.process_counters()["drive_429"] == 1


def test_snapshot_stacking():
    db = SessionLocal()
    try:
        g = lambda: uuid.uuid4().hex  # noqa: E731
        lib = em.Library(guid=g(), name="风控库", collection_type="tvshows")
        db.add(lib); db.flush()
        series = em.MediaItem(guid=g(), library_id=lib.id, item_type="series", name="叠播剧")
        db.add(series); db.flush()
        ep = em.MediaItem(guid=g(), library_id=lib.id, item_type="episode", name="第1集",
                          series_id=series.id, drive_file_id="dfid")
        mv = em.MediaItem(guid=g(), library_id=lib.id, item_type="movie", name="单播片")
        db.add_all([ep, mv]); db.flush()
        users = [WebUser(username="risk_" + g()[:8], password_hash="x") for _ in range(3)]
        db.add_all(users); db.flush()
        now = datetime(2099, 1, 1, 12, 0, 0)
        fresh, stale = now - timedelta(seconds=30), now - timedelta(minutes=10)
        rows = [
            (users[0], ep, fresh, None), (users[1], ep, fresh, None),
            (users[2], mv, fresh, None),
            (users[2], ep, stale, None),          # 超时不算
            (users[0], mv, fresh, fresh),         # 已结束不算
        ]
        for u, it, lu, ended in rows:
            db.add(em.PlaybackSession(session_key=g(), user_id=u.id, item_id=it.id,
                                      start_time=lu, last_update_at=lu, ended_at=ended))
        db.flush()
        # onupdate 不触发（只 insert）；last_update_at 按传入值
        out = playback_risk.active_sessions(db, now=now)
        assert out["active_sessions"] == 3
        top = out["top_items"]
        assert top[0]["item_id"] == ep.id and top[0]["viewers"] == 2
        assert top[0]["stacked"] is True and top[0]["series_name"] == "叠播剧"
        assert top[0]["is_drive"] is True
        assert top[1]["item_id"] == mv.id and top[1]["stacked"] is False
        assert out["stacked_items"] == 1
        snap = playback_risk.snapshot(db)
        assert snap["scope"] == "process" and snap["exists_cache_ttl_sec"] == 600
        assert set(snap["counters"]) == set(playback_risk.COUNTER_NAMES)
    finally:
        db.rollback()
        db.close()
