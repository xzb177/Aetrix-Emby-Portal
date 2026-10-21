"""WS5 季海报补环测试。

覆盖：
- TmdbClient.season_poster：正常返回 URL；季无海报返回 None；TMDB 404 返回
  None；瞬态错误上抛 TmdbTransientError；L1 缓存命中不二次打网络；
  tmdb_id 为空直接 None。
- _enrich_season_posters_io：只给剧查季、无图季才拉、有图季不拉、预热被调用、
  结果进 fetched["season_posters"]、瞬态错误上抛。
- _apply_season_posters：只写仍无图的季、预热过的落 poster_path、已有图不覆盖、
  空 map 安全。

- 全部在隔离临时 SQLite 里跑，不连生产库；不测真实 TMDB 网络。
"""
import os
import tempfile

os.environ.setdefault("DATABASE_TYPE", "sqlite")
os.environ.setdefault("REDIS_ENABLED", "false")
_fd, _tmppath = tempfile.mkstemp(suffix=".db"); os.close(_fd)
os.environ["DATABASE_URL"] = f"sqlite:///{_tmppath}"
_tmdb_cache_dir = tempfile.mkdtemp(prefix="tmdb_cache_")
os.environ["EMBY_TMDB_CACHE_DIR"] = _tmdb_cache_dir
from backend import database as _dbmod
from sqlalchemy import create_engine as _ce
from sqlalchemy.orm import sessionmaker as _sm
_dbmod.engine = _ce(os.environ["DATABASE_URL"])
_dbmod.configure_session_local(_sm(bind=_dbmod.engine))

import pytest
from types import SimpleNamespace

from backend.database import SessionLocal, init_db
from backend.emby_server import models as em
from backend.emby_server import enrich_worker
from backend.emby_server import tmdb as tmdb_mod
from backend.emby_server.tmdb import TmdbClient, TmdbTransientError

init_db()


@pytest.fixture()
def db():
    s = SessionLocal()
    try:
        s.query(em.MediaStream).delete()
        s.query(em.MediaItem).delete()
        s.commit()
        yield s
    finally:
        s.close()


def _mk_item(db, **kw):
    import uuid as _uuid
    kw.setdefault("guid", _uuid.uuid4().hex)
    it = em.MediaItem(**kw)
    db.add(it)
    db.commit()
    return it


@pytest.fixture(autouse=True)
def _clear_tmdb_client_cache():
    """TmdbClient._cache 是类级共享 + 磁盘二级缓存同进程共享：
    每个测试清掉，避免跨测试污染"""
    TmdbClient._cache.clear()
    import shutil as _shutil
    _shutil.rmtree(_tmdb_cache_dir, ignore_errors=True)
    os.makedirs(_tmdb_cache_dir, exist_ok=True)
    yield
    TmdbClient._cache.clear()


@pytest.fixture()
def series_with_seasons(db):
    series = _mk_item(db, item_type="series", name="测试剧", tmdb_id="87187",
                      library_id=1)
    s1 = _mk_item(db, item_type="season", name="第 1 季", series_id=series.id,
                  parent_id=series.id, season_number=1, library_id=1)
    s2 = _mk_item(db, item_type="season", name="第 2 季", series_id=series.id,
                  parent_id=series.id, season_number=2, library_id=1,
                  primary_image_url="https://img/old.jpg",
                  poster_path="/data/images/old.jpg")
    return series, s1, s2


class TestSeasonPosterClient:
    def _client(self, monkeypatch, get_result, calls):
        c = TmdbClient()
        def fake_get(path, params):
            calls.append(path)
            if isinstance(get_result, Exception):
                raise get_result
            return get_result
        monkeypatch.setattr(c, "_get", fake_get)
        monkeypatch.setattr(tmdb_mod, "image_base",
                            lambda db=None: "https://img.example")
        return c

    def test_ok(self, monkeypatch):
        calls = []
        c = self._client(monkeypatch, {"id": 87187, "poster_path": "/abc.jpg"},
                         calls)
        url = c.season_poster("87187", 1)
        assert url == "https://img.example/w500/abc.jpg"
        assert calls == ["/tv/87187/season/1"]

    def test_cached_no_second_request(self, monkeypatch):
        calls = []
        c = self._client(monkeypatch, {"id": 87187, "poster_path": "/abc.jpg"},
                         calls)
        assert c.season_poster("87187", 1) == "https://img.example/w500/abc.jpg"
        assert c.season_poster("87187", 1) == "https://img.example/w500/abc.jpg"
        assert len(calls) == 1

    def test_no_poster_returns_none(self, monkeypatch):
        calls = []
        c = self._client(monkeypatch, {"id": 87187, "poster_path": None},
                         calls)
        assert c.season_poster("87187", 2) is None

    def test_404_returns_none(self, monkeypatch):
        calls = []
        c = self._client(monkeypatch, None, calls)
        assert c.season_poster("87187", 99) is None

    def test_transient_reraises(self, monkeypatch):
        calls = []
        c = self._client(monkeypatch, TmdbTransientError("boom"), calls)
        with pytest.raises(TmdbTransientError):
            c.season_poster("87187", 1)

    def test_empty_tmdb_id(self, monkeypatch):
        calls = []
        c = self._client(monkeypatch, {"id": 1, "poster_path": "/x.jpg"}, calls)
        assert c.season_poster("", 1) is None
        assert calls == []


class TestSeasonPostersIO:
    def test_only_imageless_seasons_fetched(self, monkeypatch, db,
                                           series_with_seasons):
        # 本用例测的是「预热开」时的 IO 行为；刮削期预热默认关
        # （EMBY_IMAGE_PREWARM），见 test_image_backfill_patrol
        monkeypatch.setenv("EMBY_IMAGE_PREWARM", "1")
        series, s1, s2 = series_with_seasons
        seen = []
        monkeypatch.setattr(
            tmdb_mod.tmdb_client, "season_poster",
            lambda tid, sn: seen.append((tid, sn)) or "https://img.example/w500/s1.jpg")
        prewarmed = []
        monkeypatch.setattr("backend.emby_server.image_store.prewarm",
                            lambda urls: prewarmed.extend(urls) or 1)
        snapshot = SimpleNamespace(id=series.id, item_type="series",
                                   tmdb_id="87187")
        fetched = {"tmdb_id": "87187", "ok": True}
        enrich_worker._enrich_season_posters_io(db, snapshot, fetched)
        # 只给无图的 s1 拉了；有图的 s2 没打请求
        assert seen == [("87187", 1)]
        assert prewarmed == ["https://img.example/w500/s1.jpg"]
        assert fetched["season_posters"] == {s1.id: "https://img.example/w500/s1.jpg"}

    def test_non_series_skipped(self, monkeypatch, db, series_with_seasons):
        series, s1, s2 = series_with_seasons
        called = []
        monkeypatch.setattr(
            tmdb_mod.tmdb_client, "season_poster",
            lambda tid, sn: called.append((tid, sn)))
        snapshot = SimpleNamespace(id=series.id, item_type="movie", tmdb_id="1")
        fetched = {"tmdb_id": "1", "ok": True}
        enrich_worker._enrich_season_posters_io(db, snapshot, fetched)
        assert called == []
        assert "season_posters" not in fetched

    def test_transient_propagates(self, monkeypatch, db, series_with_seasons):
        series, s1, s2 = series_with_seasons
        def boom(tid, sn):
            raise TmdbTransientError("boom")
        monkeypatch.setattr(tmdb_mod.tmdb_client, "season_poster", boom)
        snapshot = SimpleNamespace(id=series.id, item_type="series",
                                   tmdb_id="87187")
        with pytest.raises(TmdbTransientError):
            enrich_worker._enrich_season_posters_io(
                db, snapshot, {"tmdb_id": "87187", "ok": True})

    def test_soft_deleted_and_merged_seasons_excluded(
            self, monkeypatch, db, series_with_seasons):
        from datetime import datetime
        series, s1, s2 = series_with_seasons
        s3 = _mk_item(db, item_type="season", name="第 3 季",
                      series_id=series.id, parent_id=series.id,
                      season_number=3, library_id=1,
                      deleted_at=datetime.now())
        s4 = _mk_item(db, item_type="season", name="第 4 季",
                      series_id=series.id, parent_id=series.id,
                      season_number=4, library_id=1,
                      merged_into_id=s1.id)
        seen = []
        monkeypatch.setattr(
            tmdb_mod.tmdb_client, "season_poster",
            lambda tid, sn: seen.append((tid, sn)) or "https://img.example/x.jpg")
        monkeypatch.setattr("backend.emby_server.image_store.prewarm",
                            lambda urls: 0)
        snapshot = SimpleNamespace(id=series.id, item_type="series",
                                   tmdb_id="87187")
        fetched = {"tmdb_id": "87187", "ok": True}
        enrich_worker._enrich_season_posters_io(db, snapshot, fetched)
        assert seen == [("87187", 1)]  # s3/s4 不在候选里
        assert fetched["season_posters"] == {s1.id: "https://img.example/x.jpg"}


class TestApplySeasonPosters:
    def test_writes_only_imageless(self, monkeypatch, db, series_with_seasons):
        series, s1, s2 = series_with_seasons
        localized = []
        monkeypatch.setattr(
            "backend.emby_server.image_store.localize",
            lambda url, allow_download=True: (
                localized.append((url, allow_download)),
                "/data/images/s1.jpg")[1])
        n = enrich_worker._apply_season_posters(
            db, {s1.id: "https://img.example/w500/s1.jpg",
                 s2.id: "https://img.example/w500/s2.jpg"})
        db.commit()
        assert n == 1
        db.refresh(s1)
        db.refresh(s2)
        assert s1.primary_image_url == "https://img.example/w500/s1.jpg"
        assert s1.poster_path == "/data/images/s1.jpg"
        assert localized == [("https://img.example/w500/s1.jpg", False)]
        # s2 本来有图，不许被覆盖
        assert s2.primary_image_url == "https://img/old.jpg"
        assert s2.poster_path == "/data/images/old.jpg"

    def test_empty_map_safe(self, db, series_with_seasons):
        assert enrich_worker._apply_season_posters(db, {}) == 0
        assert enrich_worker._apply_season_posters(db, None) == 0


class TestVirtualSeasonSeries:
    def test_resolves_parent_series(self, db, series_with_seasons):
        from backend.emby_server import media_routes
        series, s1, s2 = series_with_seasons
        got = media_routes._virtual_season_series(
            db, f"vseason-{series.id}-1")
        assert got is not None and got.id == series.id

    def test_bad_ids_return_none(self, db, series_with_seasons):
        from backend.emby_server import media_routes
        assert media_routes._virtual_season_series(db, "abc") is None
        assert media_routes._virtual_season_series(db, "vseason-0-1") is None
        assert media_routes._virtual_season_series(db, "vseason-x-1") is None
        assert media_routes._virtual_season_series(db, "vseason-1") is None
