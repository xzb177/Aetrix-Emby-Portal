"""识别 / 补资料两段式 + Frodo 补资料源（清单第 13 项，FMBY #17 / #14）。

钉死：
① 识别段只写身份列 + 段记录，不写资料字段；无绑定返回 None 不写库；
② 补资料段只填空缺、永不覆盖，拒绝未绑定条目；
③ 两段开关独立；识别开补资料关时本条回 pending 等补资料段认领；
④ Frodo 熔断 / 封禁 / 异常时只返回空，不挡其他源、不出旧数、不抛异常。
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


def _make_item(db, name="某剧", **kw):
    lib = em.Library(guid=uuid.uuid4().hex, name="剧集库",
                     collection_type="tvshows")
    db.add(lib)
    db.flush()
    kw.setdefault("enrich_status", "pending")
    item = em.MediaItem(
        guid=uuid.uuid4().hex, library_id=lib.id, item_type="series",
        name=name, sort_name=name, file_path=f"/m/{uuid.uuid4().hex}.mkv",
        production_year=2021, **kw)
    db.add(item)
    db.flush()
    return item


def _fetched(**kw):
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
    fetched.update(kw)
    return fetched


def _stage_record(db, item_id):
    return (db.query(em.EnrichStageRecord)
            .filter(em.EnrichStageRecord.item_id == item_id).first())


# ---------- ① 识别段 ----------

def test_identify_only_writes_identity_columns(db):
    item = _make_item(db)
    binding = ts.apply_identity(db, item, _fetched())
    db.flush()
    assert binding["provider"] == "tmdb"
    assert binding["external_id"] == "60300"
    assert item.tmdb_id == "60300"
    assert item.imdb_id == "tt0903747"
    # 资料字段一个都不写。
    assert not item.overview
    assert not item.poster_path
    assert not item.backdrop_path
    assert item.community_rating is None
    record = _stage_record(db, item.id)
    assert record is not None
    assert record.stage == "identified"
    assert record.confidence == "search"
    assert record.external_id == "60300"
    assert "fetched" in (record.evidence_json or "")


def test_identify_explicit_confidence_from_nfo(db):
    item = _make_item(db)
    binding = ts.apply_identity(
        db, item, _fetched(tmdb_id=None, nfo_data={"tmdb_id": "60300"}))
    assert binding["confidence"] == "explicit"
    assert item.tmdb_id == "60300"


def test_identify_none_writes_nothing(db):
    item = _make_item(db)
    assert ts.apply_identity(db, item, {}) is None
    assert not item.tmdb_id
    assert not item.imdb_id
    assert _stage_record(db, item.id) is None


def test_identify_never_overwrites_existing_identity(db):
    item = _make_item(db, tmdb_id="111", imdb_id="tt0000001")
    ts.apply_identity(db, item, _fetched())
    assert item.tmdb_id == "111"
    assert item.imdb_id == "tt0000001"


# ---------- ② 补资料段：只填空缺 ----------

def test_fill_only_missing_fields(db):
    item = _make_item(db, tmdb_id="60300", overview="已有简介")
    filled = ts.apply_fill(db, item, _fetched())
    assert "overview" not in filled
    assert item.overview == "已有简介"
    assert item.poster_path == "/tmdb_poster.jpg"
    assert item.backdrop_path == "/tmdb_backdrop.jpg"
    assert item.community_rating == 8.7
    assert _stage_record(db, item.id).stage == "filled"


def test_fill_refuses_unbound_item(db):
    item = _make_item(db, overview=None)
    assert ts.apply_fill(db, item, _fetched()) == []
    assert not item.overview
    assert not item.poster_path
    assert _stage_record(db, item.id) is None


def test_fill_never_overwrites_existing_rating(db):
    item = _make_item(db, tmdb_id="60300", community_rating=9.9)
    ts.apply_fill(db, item, _fetched())
    assert item.community_rating == 9.9


# ---------- ③ 两段开关独立 ----------

def test_switches_are_independent(monkeypatch):
    monkeypatch.setattr(ts, "IDENTIFY_STAGE_ENABLED", 1)
    monkeypatch.setattr(ts, "FILL_STAGE_ENABLED", 0)
    assert ts.identify_enabled() and not ts.fill_enabled()
    assert ts.any_enabled()
    monkeypatch.setattr(ts, "IDENTIFY_STAGE_ENABLED", 0)
    monkeypatch.setattr(ts, "FILL_STAGE_ENABLED", 1)
    assert not ts.identify_enabled() and ts.fill_enabled()
    monkeypatch.setattr(ts, "FILL_STAGE_ENABLED", 0)
    assert not ts.any_enabled()


def test_wrapper_identify_only_returns_to_pending(db, monkeypatch):
    monkeypatch.setattr(ts, "IDENTIFY_STAGE_ENABLED", 1)
    monkeypatch.setattr(ts, "FILL_STAGE_ENABLED", 0)
    item = _make_item(db, enrich_claim_token="tok")
    enrich_worker._enrich_apply_two_stage(db, item, _fetched())
    assert item.tmdb_id == "60300"
    assert not item.overview  # 补资料段关着，资料不动
    assert item.enrich_status == "pending"
    assert item.enrich_claim_token is None
    assert item.enrich_next_retry_at is not None


def test_wrapper_fill_marks_done(db, monkeypatch):
    monkeypatch.setattr(ts, "IDENTIFY_STAGE_ENABLED", 0)
    monkeypatch.setattr(ts, "FILL_STAGE_ENABLED", 1)
    item = _make_item(db, tmdb_id="60300", enrich_claim_token="tok")
    enrich_worker._enrich_apply_two_stage(db, item, _fetched())
    assert item.poster_path == "/tmdb_poster.jpg"
    assert item.enrich_status == "done"
    assert item.enrich_attempts == 0
    assert item.enrich_claim_token is None


def test_wrapper_unbound_falls_back_to_existing_apply(db, monkeypatch):
    monkeypatch.setattr(ts, "IDENTIFY_STAGE_ENABLED", 0)
    monkeypatch.setattr(ts, "FILL_STAGE_ENABLED", 1)
    called = {}

    def _fake_apply(db_, item_, fetched_):
        called["ran"] = True

    monkeypatch.setattr(enrich_worker, "_enrich_apply", _fake_apply)
    item = _make_item(db)
    enrich_worker._enrich_apply_two_stage(db, item, _fetched())
    assert called.get("ran") is True
    assert not item.poster_path  # 未绑定：补资料段一字不写


# ---------- ④ Frodo：熔断 / 封禁 / 异常只返回空 ----------

def _frodo_on(monkeypatch):
    monkeypatch.setattr(ts, "IDENTIFY_FRODO_FILL_ENABLED", 1)
    monkeypatch.setattr(
        "backend.emby_server.metasources.breaker.allow", lambda p: True)
    monkeypatch.setattr(
        "backend.emby_server.douban.ban_remaining", lambda: 0)


def test_frodo_breaker_closed_returns_empty(db, monkeypatch):
    monkeypatch.setattr(ts, "IDENTIFY_FRODO_FILL_ENABLED", 1)
    monkeypatch.setattr(
        "backend.emby_server.metasources.breaker.allow", lambda p: False)
    item = _make_item(db, imdb_id="tt0903747")
    assert ts.fill_from_frodo(item) == {}


def test_frodo_banned_returns_empty(db, monkeypatch):
    _frodo_on(monkeypatch)
    monkeypatch.setattr(
        "backend.emby_server.douban.ban_remaining", lambda: 3600)
    item = _make_item(db, imdb_id="tt0903747")
    assert ts.fill_from_frodo(item) == {}


def test_frodo_exception_returns_empty(db, monkeypatch):
    _frodo_on(monkeypatch)
    monkeypatch.setattr(
        "backend.emby_server.metasources.douban_frodo.find_by_imdb",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    item = _make_item(db, imdb_id="tt0903747")
    assert ts.fill_from_frodo(item) == {}


def test_frodo_success_fills_gaps_only(db, monkeypatch):
    _frodo_on(monkeypatch)
    monkeypatch.setattr(
        "backend.emby_server.metasources.douban_frodo.find_by_imdb",
        lambda imdb_id, year=None: {
            "id": "1292052", "title": "某剧", "overview": "豆瓣简介",
            "rating": 9.1, "image": "/douban_poster.jpg", "genres": [],
        })
    item = _make_item(db, tmdb_id="60300", imdb_id="tt0903747",
                      poster_path="/已有海报.jpg")
    filled = ts.apply_fill(db, item, {"tmdb_details": {}})
    assert item.overview == "豆瓣简介"
    assert item.community_rating == 9.1
    assert item.poster_path == "/已有海报.jpg"  # 已有海报不被 Frodo 覆盖
    assert "overview" in filled and "community_rating" in filled
    assert "poster_path" not in filled


def test_frodo_switch_off_returns_empty(db, monkeypatch):
    monkeypatch.setattr(ts, "IDENTIFY_FRODO_FILL_ENABLED", 0)
    item = _make_item(db, imdb_id="tt0903747")
    assert ts.fill_from_frodo(item) == {}
