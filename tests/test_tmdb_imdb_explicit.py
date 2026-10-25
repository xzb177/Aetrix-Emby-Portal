"""TMDB→IMDb 显式解析步（已拍板放行）。

钉死：
① 识别段内：绑定有 TMDB 身份却无 IMDb 身份时显式解析一步并落库，
   只填空缺、永不覆盖，不降级标题猜；
② 已有证据优先：TV 详情的 external_ids.imdb_id 不发请求就能读到；
③ 无取数器时只读已有证据；取数器异常 / 非 movie·series 不拖垮识别段；
④ 分发把生产 TMDB 客户端传进识别段（不断言网络，只断言接线本身）。
"""

import uuid

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend import models as base_models
from backend.emby_server import enrich_worker
from backend.emby_server import identify_two_stage as ts
from backend.emby_server import models as em


@pytest.fixture()
def db_factory():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False},
                           poolclass=StaticPool)
    base_models.Base.metadata.create_all(engine)
    em.Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)


@pytest.fixture()
def db(db_factory):
    session = db_factory()
    yield session
    session.close()


def _make_item(db, name="某剧", item_type="series", **kw):
    lib = em.Library(guid=uuid.uuid4().hex, name="剧集库",
                     collection_type="tvshows")
    db.add(lib)
    db.flush()
    kw.setdefault("enrich_status", "pending")
    item = em.MediaItem(
        guid=uuid.uuid4().hex, library_id=lib.id, item_type=item_type,
        name=name, sort_name=name, file_path=f"/m/{uuid.uuid4().hex}.mkv",
        production_year=2021, **kw)
    db.add(item)
    db.flush()
    return item


def _stub_fetcher(payload=None, exc=None):
    calls = []

    def _fetch(tmdb_id, kind):
        calls.append((tmdb_id, kind))
        if exc is not None:
            raise exc
        return payload

    _fetch.calls = calls
    return _fetch


# ---------- ① 已有证据：TV external_ids 不发请求 ----------

def test_resolve_reads_tv_external_ids_without_network(db):
    item = _make_item(db)
    fetched = {
        "tmdb_id": "60300",
        "tmdb_details": {"external_ids": {"imdb_id": "tt0903747"}},
    }
    binding = ts.apply_identity(db, item, fetched)
    db.flush()
    assert binding["imdb_id"] == "tt0903747"
    assert item.imdb_id == "tt0903747"
    assert "imdb_id_source" not in binding["evidence"]


def test_resolve_prefers_top_level_imdb_over_external_ids(db):
    item = _make_item(db)
    fetched = {
        "tmdb_id": "60300",
        "tmdb_details": {
            "imdb_id": "tt1111111",
            "external_ids": {"imdb_id": "tt2222222"},
        },
    }
    binding = ts.apply_identity(db, item, fetched)
    assert binding["imdb_id"] == "tt1111111"
    assert item.imdb_id == "tt1111111"


# ---------- ② 网络解析：取数器注入 ----------

def test_resolve_calls_fetcher_for_movie_external_ids(db):
    item = _make_item(db, item_type="movie")
    fetcher = _stub_fetcher({"external_ids": {"imdb_id": "tt7654321"}})
    binding = ts.apply_identity(
        db, item, {"tmdb_id": "123"}, tmdb_details_fetcher=fetcher)
    db.flush()
    assert fetcher.calls == [("123", "movie")]
    assert binding["imdb_id"] == "tt7654321"
    assert item.imdb_id == "tt7654321"
    assert binding["evidence"]["imdb_id_source"] == "tmdb_explicit"


def test_resolve_calls_fetcher_for_series_top_level_imdb(db):
    item = _make_item(db)
    fetcher = _stub_fetcher({"imdb_id": "tt3333333"})
    binding = ts.apply_identity(
        db, item, {"tmdb_id": "60300"}, tmdb_details_fetcher=fetcher)
    assert fetcher.calls == [("60300", "series")]
    assert item.imdb_id == "tt3333333"


def test_resolve_fetcher_empty_means_no_imdb(db):
    item = _make_item(db)
    fetcher = _stub_fetcher({})
    binding = ts.apply_identity(
        db, item, {"tmdb_id": "60300"}, tmdb_details_fetcher=fetcher)
    assert fetcher.calls == [("60300", "series")]
    assert binding["imdb_id"] is None
    assert not item.imdb_id
    assert item.tmdb_id == "60300"


# ---------- ③ 不覆盖、不猜、不拖垮 ----------

def test_resolve_never_overwrites_existing_imdb(db):
    item = _make_item(db, tmdb_id="111", imdb_id="tt0000001")
    fetcher = _stub_fetcher({"imdb_id": "tt9999999"})
    ts.apply_identity(
        db, item, {"tmdb_id": "111"}, tmdb_details_fetcher=fetcher)
    assert fetcher.calls == []
    assert item.imdb_id == "tt0000001"


def test_resolve_fetcher_failure_keeps_tmdb(db):
    item = _make_item(db)
    fetcher = _stub_fetcher(exc=RuntimeError("boom"))
    binding = ts.apply_identity(
        db, item, {"tmdb_id": "60300"}, tmdb_details_fetcher=fetcher)
    assert binding is not None
    assert item.tmdb_id == "60300"
    assert not item.imdb_id


def test_resolve_skips_network_for_episode(db):
    series = _make_item(db)
    item = _make_item(db, name="第 1 集", item_type="episode",
                      series_id=series.id, season_number=1, episode_number=1)
    fetcher = _stub_fetcher({"imdb_id": "tt4444444"})
    binding = ts.apply_identity(
        db, item, {"tmdb_id": "60300"}, tmdb_details_fetcher=fetcher)
    assert fetcher.calls == []
    assert binding is not None
    assert item.tmdb_id == "60300"
    assert not item.imdb_id


def test_resolve_without_tmdb_never_calls_fetcher(db):
    item = _make_item(db)
    fetcher = _stub_fetcher({"imdb_id": "tt5555555"})
    imdb_id, source = ts.resolve_imdb_id(item, {}, fetcher)
    assert imdb_id is None
    assert source is None
    assert fetcher.calls == []


# ---------- ④ 分发接线：生产客户端传进识别段 ----------

def test_dispatcher_passes_production_client(db, monkeypatch):
    from backend.emby_server.tmdb import tmdb_client

    seen = {}
    original = ts.apply_identity

    def _recording(db_, item_, fetched_, tmdb_details_fetcher=None):
        seen["fetcher"] = tmdb_details_fetcher
        return original(db_, item_, fetched_,
                        tmdb_details_fetcher=tmdb_details_fetcher)

    monkeypatch.setattr(ts, "apply_identity", _recording)
    item = _make_item(db)
    fetched = {
        "tmdb_id": "60300",
        "tmdb_details": {
            "overview": "详情简介",
            "vote_average": 8.7,
            "poster_path": "/tmdb_poster.jpg",
            "backdrop_path": "/tmdb_backdrop.jpg",
            "imdb_id": "tt0903747",
        },
    }
    enrich_worker._enrich_apply_two_stage(
        db, item, fetched, identify=True, fill=False)
    # bound method 每次取属性都是新对象，== 比较（同函数 + 同实例）才成立。
    assert seen["fetcher"] == tmdb_client.details
    assert item.tmdb_id == "60300"
    assert item.imdb_id == "tt0903747"
