"""FMBY 未闭环第 2 路：#13 远端身份认亲 + #14 豆瓣 Frodo 桥。

#13 钉死：识别段此前完全不认远端身份（identify_two_stage 对
drive_file_id / content_hash / scan_title / identity_json 零引用），
扫描器认亲层再全，刮削识别段也用不上。本单把认亲接进识别段：
extract_binding 无绑定时，按 drive_file_id → content_hash 层序向
已绑定的 donor 行继承（只填空缺、永不覆盖；歧义不认；movie /
series 以外不继承；DB 异常不拖垮识别段）。

#14 钉死：桥此前只有 mock 单测，从无真实端点验证。本单补两道：
① _frodo_candidates 的熔断 / 封禁两道闸在补资料层真实生效（闸关
时 Frodo 函数根本不被调用）；② opt-in live probe（默认跳过，见
test_live_probe_reports_outcome）：只在 DOUBAN_FRODO_LIVE_PROBE=1
时向真实端点发一次请求，断言只限“不崩、返回形状合法”，成功与否
都如实上报——2026-10-27 两路出口实测均为 HTTP 400 code 104
invalid_apikey（内置 key 被拒），故该 probe 今天预期返回 None，
不许把它写成“桥已通”的证据；真通须用户提供有效自备 key 后复核。
"""
import os
import uuid

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend import models as base_models
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


def _make_item(db, item_type="movie", name="某片", **kw):
    lib = em.Library(guid=uuid.uuid4().hex, name="库",
                     collection_type="movies")
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


def _stage_record(db, item_id):
    return (db.query(em.EnrichStageRecord)
            .filter(em.EnrichStageRecord.item_id == item_id).first())


# ---------- #13：远端身份继承 ----------

def test_inherit_by_file_id(db):
    donor = _make_item(db, tmdb_id="60300", imdb_id="tt0903747",
                       drive_file_id="fid-1")
    item = _make_item(db, drive_file_id="fid-1")
    binding = ts.apply_identity(db, item, {})
    assert binding is not None
    assert binding["provider"] == "tmdb"
    assert binding["external_id"] == "60300"
    assert binding["confidence"] == "inherited"
    assert binding["evidence"]["remote_identity_layer"] == "file_id"
    assert binding["evidence"]["donor_item_id"] == donor.id
    assert item.tmdb_id == "60300"
    assert item.imdb_id == "tt0903747"
    record = _stage_record(db, item.id)
    assert record is not None and record.confidence == "inherited"


def test_inherit_ambiguous_file_id_returns_none(db):
    _make_item(db, tmdb_id="111", drive_file_id="fid-9")
    _make_item(db, tmdb_id="222", drive_file_id="fid-9")
    item = _make_item(db, drive_file_id="fid-9")
    assert ts.apply_identity(db, item, {}) is None
    assert not item.tmdb_id and not item.imdb_id
    assert _stage_record(db, item.id) is None


def test_inherit_by_content_hash(db):
    _make_item(db, tmdb_id="777", content_hash="abc123",
               content_hash_algo="md5", size=12345)
    item = _make_item(db, content_hash="abc123",
                      content_hash_algo="md5", size=12345)
    binding = ts.apply_identity(db, item, {})
    assert binding is not None
    assert binding["confidence"] == "inherited"
    assert binding["evidence"]["remote_identity_layer"] == "content_hash"
    assert item.tmdb_id == "777"


def test_inherit_content_hash_algo_mismatch_returns_none(db):
    _make_item(db, tmdb_id="777", content_hash="abc123",
               content_hash_algo="md5", size=12345)
    item = _make_item(db, content_hash="abc123",
                      content_hash_algo="sha1", size=12345)
    assert ts.apply_identity(db, item, {}) is None
    assert not item.tmdb_id


def test_inherit_skips_episode(db):
    _make_item(db, tmdb_id="60300", drive_file_id="fid-ep")
    item = _make_item(db, item_type="episode", drive_file_id="fid-ep")
    assert ts.apply_identity(db, item, {}) is None
    assert not item.tmdb_id


def test_inherit_skips_merged_donor(db):
    target = _make_item(db)
    _make_item(db, tmdb_id="60300", drive_file_id="fid-m",
               merged_into_id=target.id)
    item = _make_item(db, drive_file_id="fid-m")
    assert ts.apply_identity(db, item, {}) is None
    assert not item.tmdb_id


def test_inherit_never_overwrites_existing_identity(db):
    _make_item(db, tmdb_id="60300", drive_file_id="fid-ow")
    item = _make_item(db, imdb_id="tt0000001", drive_file_id="fid-ow")
    binding = ts.apply_identity(db, item, {})
    assert binding is not None
    assert binding["provider"] == "imdb"
    assert item.imdb_id == "tt0000001"
    assert not item.tmdb_id


# ---------- #14：Frodo 补资料层闸门 ----------

def test_frodo_candidates_breaker_closed_never_calls_frodo(db, monkeypatch):
    from backend.emby_server.metasources import breaker
    from backend.emby_server.metasources import douban_frodo as frodo

    monkeypatch.setattr(breaker, "allow", lambda provider: False)

    def _boom(*args, **kwargs):
        raise AssertionError("熔断中不许调 Frodo")

    monkeypatch.setattr(frodo, "find_by_imdb", _boom)
    monkeypatch.setattr(frodo, "search_by_title", _boom)
    item = _make_item(db, imdb_id="tt0111161")
    assert ts._frodo_candidates(item, {}) == {}


def test_frodo_candidates_ban_period_never_calls_frodo(db, monkeypatch):
    from backend.emby_server import douban as douban_leg
    from backend.emby_server.metasources import breaker
    from backend.emby_server.metasources import douban_frodo as frodo

    monkeypatch.setattr(breaker, "allow", lambda provider: True)
    monkeypatch.setattr(douban_leg, "ban_remaining", lambda: 3600.0)

    def _boom(*args, **kwargs):
        raise AssertionError("封禁期不许调 Frodo")

    monkeypatch.setattr(frodo, "find_by_imdb", _boom)
    monkeypatch.setattr(frodo, "search_by_title", _boom)
    item = _make_item(db, imdb_id="tt0111161")
    assert ts._frodo_candidates(item, {}) == {}


def test_frodo_candidates_imdb_bridge_maps_only_nonempty(db, monkeypatch):
    from backend.emby_server import douban as douban_leg
    from backend.emby_server.metasources import breaker
    from backend.emby_server.metasources import douban_frodo as frodo

    monkeypatch.setattr(breaker, "allow", lambda provider: True)
    monkeypatch.setattr(douban_leg, "ban_remaining", lambda: 0.0)
    monkeypatch.setattr(
        frodo, "find_by_imdb",
        lambda imdb_id, year=None, kind="": {
            "id": "1292052", "title": "肖申克的救赎",
            "overview": "银行家安迪", "rating": 9.7, "image": "",
        })
    item = _make_item(db, imdb_id="tt0111161")
    got = ts._frodo_candidates(item, {})
    assert got["overview"] == "银行家安迪"
    assert got["community_rating"] == 9.7
    assert "poster_path" not in got  # 空图不算候选


# ---------- #14：opt-in 真实端点探查（默认跳过，不进 CI） ----------

@pytest.mark.skipif(
    os.getenv("DOUBAN_FRODO_LIVE_PROBE") != "1",
    reason="真实端点探查须显式开启（DOUBAN_FRODO_LIVE_PROBE=1），默认跳过")
def test_live_probe_reports_outcome_without_crashing():
    """向真实 Frodo/v2 端点发一次 IMDb 桥请求，只验形状不验成功。

    2026-10-27 两路出口实测：HTTP 400 code 104 invalid_apikey，
    故今天预期返回 None；换有效自备 key（DOUBAN_FRODO_APIKEY）后
    复核时，成功应返回带 id 的 dict。任何情况下代码都不许抛异常、
    成功载荷的 id 必须非空。熔断状态在 finally 里复位，不污染他测。
    """
    from backend.emby_server.metasources import breaker
    from backend.emby_server.metasources import douban_frodo as frodo

    breaker.reset_all()
    try:
        result = frodo.find_by_imdb("tt0111161")
    finally:
        breaker.reset_all()
    assert result is None or (
        isinstance(result, dict) and bool(result.get("id")))
