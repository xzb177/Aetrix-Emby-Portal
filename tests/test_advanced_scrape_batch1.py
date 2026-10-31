"""高级刮削第一批（FMBY 候选 2 / 5）。

钉死：
① 中文聚合：开关关时返回空、不问源；开时逐字段按源优先级取第一个
   非空值，源失败隔离、未知源忽略、只填 needed 字段；
② 版本缓存：键按（身份，语言，地区）分版本（语言小写 / 地区大写
   归一化），TTL 内命中、过期后 get 视同未命中、lookup 交 stale；
   按身份作废只清该身份的全部版本；
③ 补资料段接线：聚合开关关时 apply_fill 行为不变；开时聚合补缺口，
   且聚合里的 Frodo 腿不受独立 Frodo 开关管辖（聚合自己的开关说了算）。
"""
import uuid

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend import models as base_models
from backend.emby_server import identify_two_stage as ts
from backend.emby_server import models as em
from backend.emby_server.metasources import version_cache as vc
from backend.emby_server.metasources import zh_aggregate as zh


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


def _agg_on(monkeypatch):
    monkeypatch.setattr(zh, "ZH_AGGREGATE_ENABLED", 1)
    monkeypatch.setattr(zh, "_CACHE", vc.VersionCache(ttl_sec=3600))
    monkeypatch.setattr(vc, "VERSION_CACHE_ENABLED", 0)
    monkeypatch.delenv("ZH_AGGREGATE_SOURCES", raising=False)


# ---------------------------------------------------------------------------
# 版本缓存键与过期语义
# ---------------------------------------------------------------------------

def test_version_key_normalizes_language_region():
    assert vc.version_key("60300", "zh_TW", "cn") == \
        vc.version_key("60300", "zh-tw", "CN")
    assert vc.version_key("60300", "", "") != vc.version_key("60300", "zh-cn", "")


def test_version_key_empty_identity_rejected():
    with pytest.raises(ValueError):
        vc.version_key("  ")


def test_version_cache_fresh_stale_miss():
    cache = vc.VersionCache(ttl_sec=100)
    assert cache.lookup("60300", "zh-cn", "") == ("miss", None)
    cache.put("60300", "zh-cn", "", {"overview": "简介"}, now=1000)
    assert cache.get("60300", "zh-cn", "", now=1050) == {"overview": "简介"}
    assert cache.lookup("60300", "zh-cn", "", now=1050) == (
        "hit", {"overview": "简介"})
    assert cache.get("60300", "zh-cn", "", now=1101) is None
    assert cache.lookup("60300", "zh-cn", "", now=1101) == (
        "stale", {"overview": "简介"})


def test_version_cache_put_refreshes_expiry():
    cache = vc.VersionCache(ttl_sec=100)
    cache.put("60300", "", "", "v1", now=1000)
    cache.put("60300", "", "", "v2", now=1050)
    assert cache.get("60300", "", "", now=1100) == "v2"


def test_version_cache_invalidate_is_identity_scoped():
    cache = vc.VersionCache(ttl_sec=3600)
    cache.put("60300", "zh-cn", "", "a", now=0)
    cache.put("60300", "en-us", "", "b", now=0)
    cache.put("60301", "zh-cn", "", "c", now=0)
    assert cache.invalidate("60300") == 2
    assert cache.get("60300", "zh-cn", "", now=0) is None
    assert cache.get("60301", "zh-cn", "", now=0) == "c"


def test_version_cache_purge_expired_counts():
    cache = vc.VersionCache(ttl_sec=100)
    cache.put("60300", "", "", "a", now=0)
    cache.put("60301", "", "", "b", now=500)
    assert cache.purge_expired(now=200) == 1
    assert len(cache) == 1

# ---------------------------------------------------------------------------
# 中文聚合
# ---------------------------------------------------------------------------

def test_aggregate_switch_off_asks_no_source(monkeypatch):
    monkeypatch.setattr(zh, "ZH_AGGREGATE_ENABLED", 0)
    calls = []
    monkeypatch.setattr(
        zh, "_source_douban_frodo",
        lambda item, fetched: calls.append("frodo") or {"overview": "x"})
    candidates, provenance = zh.aggregate(
        _FakeItem(), {}, ["overview"])
    assert (candidates, provenance) == ({}, {})
    assert calls == []


class _FakeItem:
    tmdb_id = "60300"
    imdb_id = None
    name = "某剧"


def test_aggregate_per_field_priority_and_provenance(monkeypatch):
    _agg_on(monkeypatch)
    monkeypatch.setattr(
        zh, "_source_douban_frodo",
        lambda item, fetched: {"overview": "豆瓣简介",
                               "poster_path": "/豆瓣海报.jpg"})
    monkeypatch.setattr(
        zh, "_source_tmdb_details",
        lambda item, fetched: {"overview": "TMDB 简介",
                               "backdrop_path": "/tmdb 背景.jpg"})
    candidates, provenance = zh.aggregate(_FakeItem(), {}, None)
    assert candidates["overview"] == "豆瓣简介"  # Frodo 优先
    assert candidates["poster_path"] == "/豆瓣海报.jpg"
    assert candidates["backdrop_path"] == "/tmdb 背景.jpg"  # Frodo 没有才让位
    assert provenance == {"overview": "douban_frodo",
                          "poster_path": "douban_frodo",
                          "backdrop_path": "tmdb_details"}


def test_aggregate_only_needed_fields(monkeypatch):
    _agg_on(monkeypatch)
    monkeypatch.setattr(
        zh, "_source_douban_frodo",
        lambda item, fetched: {"overview": "豆瓣简介",
                               "poster_path": "/豆瓣海报.jpg"})
    candidates, _ = zh.aggregate(_FakeItem(), {}, ["overview"])
    assert candidates == {"overview": "豆瓣简介"}


def test_aggregate_unknown_source_ignored(monkeypatch):
    _agg_on(monkeypatch)
    monkeypatch.setenv("ZH_AGGREGATE_SOURCES", "nope,douban_frodo")
    monkeypatch.setattr(
        zh, "_source_douban_frodo",
        lambda item, fetched: {"overview": "豆瓣简介"})
    candidates, provenance = zh.aggregate(_FakeItem(), {}, ["overview"])
    assert candidates == {"overview": "豆瓣简介"}
    assert provenance == {"overview": "douban_frodo"}


def test_aggregate_source_exception_isolated(monkeypatch):
    _agg_on(monkeypatch)
    monkeypatch.setattr(
        zh, "_source_douban_frodo",
        lambda item, fetched: (_ for _ in ()).throw(RuntimeError("boom")))
    monkeypatch.setattr(
        zh, "_source_tmdb_details",
        lambda item, fetched: {"overview": "TMDB 简介"})
    candidates, _ = zh.aggregate(_FakeItem(), {}, ["overview"])
    assert candidates == {"overview": "TMDB 简介"}


def test_aggregate_version_cache_hit_skips_sources(monkeypatch):
    _agg_on(monkeypatch)
    monkeypatch.setattr(vc, "VERSION_CACHE_ENABLED", 1)
    calls = []
    monkeypatch.setattr(
        zh, "_source_douban_frodo",
        lambda item, fetched: calls.append("frodo") or {"overview": "豆瓣简介"})
    monkeypatch.setattr(
        zh, "_source_tmdb_details",
        lambda item, fetched: calls.append("tmdb") or {})
    first, _ = zh.aggregate(_FakeItem(), {}, ["overview"])
    second, _ = zh.aggregate(_FakeItem(), {}, ["overview"])
    assert first == second == {"overview": "豆瓣简介"}
    assert calls == ["frodo"]  # 第二次命中版本缓存，不再问源


def test_aggregate_version_cache_off_calls_sources_twice(monkeypatch):
    _agg_on(monkeypatch)  # _agg_on 已把版本缓存开关置 0
    calls = []
    monkeypatch.setattr(
        zh, "_source_douban_frodo",
        lambda item, fetched: calls.append("frodo") or {"overview": "豆瓣简介"})
    zh.aggregate(_FakeItem(), {}, ["overview"])
    zh.aggregate(_FakeItem(), {}, ["overview"])
    assert calls == ["frodo", "frodo"]


def test_aggregate_no_identity_skips_cache(monkeypatch):
    _agg_on(monkeypatch)
    monkeypatch.setattr(vc, "VERSION_CACHE_ENABLED", 1)
    calls = []
    monkeypatch.setattr(
        zh, "_source_douban_frodo",
        lambda item, fetched: calls.append("frodo") or {"overview": "豆瓣简介"})

    class _NoId:
        tmdb_id = None
        imdb_id = None

    zh.aggregate(_NoId(), {}, ["overview"])
    zh.aggregate(_NoId(), {}, ["overview"])
    assert calls == ["frodo", "frodo"]  # 无身份不缓存，每次都问源


# ---------------------------------------------------------------------------
# 补资料段接线
# ---------------------------------------------------------------------------

def test_apply_fill_aggregate_off_changes_nothing(db, monkeypatch):
    monkeypatch.setattr(ts, "FILL_STAGE_ENABLED", 1)
    monkeypatch.setattr(ts, "IDENTIFY_FRODO_FILL_ENABLED", 0)
    monkeypatch.setattr(zh, "ZH_AGGREGATE_ENABLED", 0)
    item = _make_item(db, tmdb_id="60300")
    filled = ts.apply_fill(db, item, {"tmdb_details": {}})
    assert filled == []
    assert item.overview in (None, "")


def test_apply_fill_aggregate_fills_gap(db, monkeypatch):
    monkeypatch.setattr(ts, "FILL_STAGE_ENABLED", 1)
    monkeypatch.setattr(ts, "IDENTIFY_FRODO_FILL_ENABLED", 0)
    _agg_on(monkeypatch)
    monkeypatch.setattr(
        zh, "_source_douban_frodo",
        lambda item, fetched: {"overview": "聚合简介",
                               "poster_path": "/聚合海报.jpg"})
    item = _make_item(db, tmdb_id="60300", imdb_id="tt0903747")
    filled = ts.apply_fill(db, item, {"tmdb_details": {}})
    assert item.overview == "聚合简介"
    # 聚合海报同样走远程 URL 口径（2026-11-01 修复）。
    from backend.emby_server.tmdb import image_base
    assert item.primary_image_url == f"{image_base()}/w500/聚合海报.jpg"
    assert "overview" in filled and "primary_image_url" in filled


def test_frodo_candidates_ungated_but_breaker_gated(db, monkeypatch):
    monkeypatch.setattr(
        "backend.emby_server.metasources.breaker.allow", lambda p: True)
    monkeypatch.setattr(
        "backend.emby_server.douban.ban_remaining", lambda: 0)
    monkeypatch.setattr(
        "backend.emby_server.metasources.douban_frodo.find_by_imdb",
        lambda imdb_id, year=None: {
            "id": "1292052", "title": "某剧", "overview": "豆瓣简介",
            "rating": 9.1, "image": "/豆瓣海报.jpg", "genres": [],
        })
    item = _make_item(db, imdb_id="tt0903747")
    got = ts._frodo_candidates(item)
    assert got["overview"] == "豆瓣简介"
    assert got["community_rating"] == 9.1
