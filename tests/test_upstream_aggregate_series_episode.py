"""O-08 系列级单集配对：单集自身无身份时，先配系列、再按季集号对齐。

红线（配错集比配错电影严重）：季集号缺失/对不齐不配（仍记
episode_no_identity）；同系列同季同集多候选不配（新原因
episode_multi_candidate）；系列本身多候选不配（新原因
episode_series_ambiguous）；单集自身身份路径优先级与口径不变，
带自身身份的单集不走系列级路径（范围边界，见模块头）。

只测配对纯部分 + 一条端到端：DB 用内存 SQLite，HTTP 全部注入
替身，开关用 monkeypatch。本地口径，全量以 GitHub CI 为准。
"""

import os

os.environ.setdefault("DATABASE_TYPE", "sqlite")
os.environ.setdefault("REDIS_ENABLED", "false")

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend import models as base_models
from backend.emby_server import models as em
from backend.emby_server import upstream_aggregation as agg
from backend.emby_server import upstream_aggregate_run as uar
from backend.emby_server import upstream_item_mappings as uim
from backend.emby_server import upstream_sources as ups
from backend.integrations import store as config_store


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


@pytest.fixture(autouse=True)
def _clear_config_cache():
    # store.get_value 有 60 秒进程内 TTL 缓存（只按键名）：不清会
    # 让先跑用例读到的「无此键」盖住后跑用例刚写进 DB 的值
    # （与 test_upstream_aggregate_fetch_slim 同款处理）。
    config_store._ttl_cache.clear()
    yield
    config_store._ttl_cache.clear()


def _set_config(db, key, value):
    row = (db.query(base_models.SystemConfig)
           .filter(base_models.SystemConfig.key == key).first())
    if row is None:
        db.add(base_models.SystemConfig(key=key, value=value))
    else:
        row.value = value
    db.flush()
    config_store._ttl_cache.clear()


def _local(db, *, item_id, name, item_type, tmdb=None, imdb=None,
           scan_title=None, scan_year=None, year=None,
           series_id=None, season=None, episode=None):
    row = em.MediaItem(
        id=item_id, guid=f"guid-{item_id}", library_id=1,
        item_type=item_type, name=name, tmdb_id=tmdb, imdb_id=imdb,
        scan_title=scan_title, scan_year=scan_year, production_year=year,
        series_id=series_id, season_number=season,
        episode_number=episode, merged_into_id=None)
    db.add(row)
    db.flush()
    return row


def _series_local(db, *, item_id=30, tmdb="111", name="三体", **kw):
    return _local(db, item_id=item_id, name=name, item_type="series",
                  tmdb=tmdb, **kw)


def _episode_local(db, *, item_id=31, series_id=30, season=1, episode=1):
    return _local(db, item_id=item_id, name=f"第 {episode} 集",
                  item_type="episode", series_id=series_id,
                  season=season, episode=episode)


def _rec(item_id, name, item_type, **kw):
    return agg.UpstreamCatalogRecord(
        source_id="1", upstream_item_id=item_id, name=name,
        item_type=item_type, **kw)


def _series_rec(item_id="u-s", name="三体", tmdb="111", year=2023):
    return _rec(item_id, name, "Series", tmdb_id=tmdb,
                production_year=year)


def _episode_rec(item_id="u-e1", series_name="三体", season=1, episode=1,
                 tmdb=None, imdb=None, year=2023):
    return _rec(item_id, "第 1 集", "Episode", series_name=series_name,
                season_number=season, episode_number=episode,
                tmdb_id=tmdb, imdb_id=imdb, production_year=year)


def _pair(db, records):
    items = agg.build_aggregates(records)
    return uar.pair_aggregates(db, items)


# ---------------------------------------------------------------------------
# 正路：系列命中 + 季集对齐 → series+episode / 0.9
# ---------------------------------------------------------------------------


def test_pairs_via_inbatch_series_tmdb(db):
    _series_local(db)
    ep = _episode_local(db)
    pairs, unpaired = _pair(db, [_series_rec(), _episode_rec()])
    ep_pairs = [p for p in pairs if p["item_type"] == "Episode"]
    assert len(ep_pairs) == 1
    assert ep_pairs[0]["local_item_id"] == ep.id
    assert ep_pairs[0]["upstream_item_id"] == "u-e1"
    assert ep_pairs[0]["evidence"] == "series+episode"
    assert ep_pairs[0]["confidence"] == 0.9
    assert ep_pairs[0]["episode_index"] == 1
    assert unpaired == {}


def test_pairs_via_inbatch_series_title_year(db):
    _local(db, item_id=30, name="三体", item_type="series",
           scan_title="三体", scan_year=2023, year=2023)
    _episode_local(db)
    pairs, unpaired = _pair(db, [
        _series_rec(tmdb=None), _episode_rec()])
    ep_pairs = [p for p in pairs if p["item_type"] == "Episode"]
    assert len(ep_pairs) == 1
    assert ep_pairs[0]["evidence"] == "series+episode"
    assert ep_pairs[0]["confidence"] == 0.9
    assert unpaired == {}


def test_pairs_via_local_title_fallback_when_series_not_in_batch(db):
    # 本窗只有单集（系列记录在别的窗）：本地同名系列唯一时仍可配。
    _series_local(db, tmdb=None, scan_title="三体")
    _episode_local(db)
    pairs, unpaired = _pair(db, [_episode_rec()])
    assert len(pairs) == 1
    assert pairs[0]["evidence"] == "series+episode"
    assert pairs[0]["confidence"] == 0.9
    assert unpaired == {}


def test_confidence_configurable_via_system_config(db):
    _series_local(db)
    _episode_local(db)
    _set_config(db, "upstream_aggregate_series_episode_confidence", "0.8")
    pairs, _ = _pair(db, [_series_rec(), _episode_rec()])
    ep_pairs = [p for p in pairs if p["item_type"] == "Episode"]
    assert ep_pairs[0]["confidence"] == 0.8


def test_confidence_invalid_config_falls_back_to_default(db):
    _series_local(db)
    _episode_local(db)
    _set_config(db, "upstream_aggregate_series_episode_confidence", "abc")
    pairs, _ = _pair(db, [_series_rec(), _episode_rec()])
    ep_pairs = [p for p in pairs if p["item_type"] == "Episode"]
    assert ep_pairs[0]["confidence"] == 0.9


# ---------------------------------------------------------------------------
# 红线：缺号 / 对不齐 / 多候选 → 不配
# ---------------------------------------------------------------------------


def test_missing_episode_number_stays_unpaired(db):
    _series_local(db)
    _episode_local(db)
    pairs, unpaired = _pair(db, [
        _series_rec(), _episode_rec(episode=None)])
    assert [p for p in pairs if p["item_type"] == "Episode"] == []
    assert unpaired == {"episode_no_identity": 1}


def test_missing_season_number_stays_unpaired(db):
    _series_local(db)
    _episode_local(db)
    pairs, unpaired = _pair(db, [
        _series_rec(), _episode_rec(season=None)])
    assert [p for p in pairs if p["item_type"] == "Episode"] == []
    assert unpaired == {"episode_no_identity": 1}


def test_numbers_not_matching_local_stays_unpaired(db):
    _series_local(db)
    _episode_local(db, episode=1)
    pairs, unpaired = _pair(db, [
        _series_rec(), _episode_rec(episode=5)])
    assert [p for p in pairs if p["item_type"] == "Episode"] == []
    assert unpaired == {"episode_no_identity": 1}


def test_multi_candidate_episode_not_paired(db):
    _series_local(db)
    _episode_local(db, item_id=31)
    _episode_local(db, item_id=32)
    pairs, unpaired = _pair(db, [_series_rec(), _episode_rec()])
    assert [p for p in pairs if p["item_type"] == "Episode"] == []
    assert unpaired == {"episode_multi_candidate": 1}


def test_ambiguous_local_series_not_paired(db):
    _series_local(db, item_id=30, tmdb=None, scan_title="三体")
    _series_local(db, item_id=40, tmdb=None, name="三体",
                  scan_title="三体")
    _episode_local(db, item_id=31, series_id=30)
    _episode_local(db, item_id=41, series_id=40)
    pairs, unpaired = _pair(db, [_episode_rec()])
    assert pairs == []
    assert unpaired == {"episode_series_ambiguous": 1}


def test_unresolved_inbatch_series_blocks_title_fallback(db):
    # 本窗系列带 tmdb 999、本地无此身份；本地另有同名系列（别的
    # 版本）：不许靠标题回落把单集配进疑似翻拍版（防错红线）。
    _series_local(db, item_id=30, tmdb="111", scan_title="三体")
    _episode_local(db)
    pairs, unpaired = _pair(db, [
        _series_rec(tmdb="999"), _episode_rec()])
    assert [p for p in pairs if p["item_type"] == "Episode"] == []
    assert unpaired == {"identity_no_local_match": 1,
                        "episode_no_identity": 1}


def test_no_series_name_stays_unpaired(db):
    _series_local(db)
    _episode_local(db)
    pairs, unpaired = _pair(db, [
        _series_rec(), _episode_rec(series_name="")])
    assert [p for p in pairs if p["item_type"] == "Episode"] == []
    assert unpaired == {"episode_no_identity": 1}


# ---------------------------------------------------------------------------
# 自身身份路径不受影响（优先级与范围边界）
# ---------------------------------------------------------------------------


def test_own_identity_episode_still_pairs_by_tmdb(db):
    _series_local(db)
    ep = _episode_local(db)
    ep.tmdb_id = "555"
    db.flush()
    pairs, unpaired = _pair(db, [
        _series_rec(), _episode_rec(tmdb="555")])
    ep_pairs = [p for p in pairs if p["item_type"] == "Episode"]
    assert len(ep_pairs) == 1
    assert ep_pairs[0]["local_item_id"] == ep.id
    assert ep_pairs[0]["evidence"] == "tmdb"
    assert ep_pairs[0]["confidence"] == 1.0
    assert unpaired == {}


def test_own_identity_episode_not_rescued_by_series_path(db):
    # 带自身身份但本地无命中：仍按旧口径记 episode_no_identity，
    # 不走系列级路径（本单范围只限「自身无身份」的单集）。
    _series_local(db)
    _episode_local(db)
    pairs, unpaired = _pair(db, [
        _series_rec(), _episode_rec(tmdb="999999")])
    assert [p for p in pairs if p["item_type"] == "Episode"] == []
    assert unpaired == {"episode_no_identity": 1}


# ---------------------------------------------------------------------------
# 端到端：run 写映射时单集带 series+episode / 0.9
# ---------------------------------------------------------------------------


class _FakeItems:
    def __init__(self, pages):
        self.pages = pages

    def get(self, url, headers, timeout):
        from urllib.parse import parse_qs, urlsplit
        if url.split("?")[0].endswith("/Library/MediaFolders"):
            return 200, {"Items": [{"Id": "lib-1", "Name": "库"}]}
        query = parse_qs(urlsplit(url).query)
        start = int(query["StartIndex"][0])
        limit = int(query["Limit"][0])
        flat = [raw for items, _ in self.pages for raw in items]
        total = self.pages[0][1] if self.pages else 0
        return 200, {"Items": flat[start:start + limit],
                     "TotalRecordCount": total}


def _source(db):
    # 备注-分类对位（2026-10-31）：run 先按源备注找对位库，本文件
    # 本地行都在 library_id=1（见 test_run_* 的 _route_library）。
    row = em.UpstreamSource(
        name="src-1", source_type="emby",
        base_url="https://up.example.com/emby", auth_method="none",
        enabled=True, credential_scope="source", health_status="unknown",
        note="测试-聚合库")
    db.add(row)
    db.flush()
    return row


def test_run_writes_series_episode_mappings(db, monkeypatch):
    # 备注-分类对位：本地行在 library_id=1，备同名对位库「聚合库」。
    db.add(em.Library(id=1, guid="lib-guid-1", name="聚合库"))
    db.flush()
    source = _source(db)
    _series_local(db)
    _episode_local(db, item_id=31, episode=1)
    _episode_local(db, item_id=32, episode=2)
    pages = _FakeItems([([
        {"Id": "u-s", "Name": "三体", "Type": "Series",
         "ProductionYear": 2023, "ProviderIds": {"Tmdb": "111"},
         "MediaSources": []},
        {"Id": "u-e1", "Name": "第 1 集", "Type": "Episode",
         "SeriesName": "三体", "ParentIndexNumber": 1, "IndexNumber": 1,
         "ProviderIds": {}, "MediaSources": []},
        {"Id": "u-e2", "Name": "第 2 集", "Type": "Episode",
         "SeriesName": "三体", "ParentIndexNumber": 1, "IndexNumber": 2,
         "ProviderIds": {}, "MediaSources": []},
    ], 3)])
    monkeypatch.setenv(uim.MAPPING_ENABLED_ENV, "1")
    result = uar.run_manual_aggregation(db, source, http_get=pages.get)
    assert result["paired"] == 3
    assert result["unpaired_reasons"] == {}
    rows = {r.upstream_item_id: r
            for r in db.query(em.UpstreamItemMapping).all()}
    assert rows["u-s"].evidence == "tmdb"
    for uid, idx in (("u-e1", 1), ("u-e2", 2)):
        assert rows[uid].evidence == "series+episode"
        assert rows[uid].confidence == 0.9
        assert rows[uid].episode_index == idx
