"""回归：上游集号 0（第 0 集 / 特别篇）不得打死整轮聚合。

生产实证（2026-11-01 02:02）：聚合任务跑完 5/12 个库后整轮
failed——某单集 episode_index=0 写映射时被 record_mapping 的
正整数校验抛 ValueError，该异常不在逐库隔离的捕获面内，一颗
坏数据打死整轮。

口径（查证结论，见 PR body）：episode_index 的实际载荷是上游
Emby 集号（IndexNumber），0 是合法集号（本地 MediaItem.
episode_number 同口径：无正数约束、扫描 S00E00 原样落 0；
配对层聚合键 e0 独立成集、O-08 对齐只拒 None）。映射层改按
非负校验接纳 0、负数仍拒；同时 run 写映射循环对单对的校验类
ValueError 做 per-item 兜底（计 episode_index_invalid 并继续），
单个条目数据问题绝不许再打死整轮。

DB 用内存 SQLite，HTTP 全部注入替身，开关用 monkeypatch。
本地口径，全量以 GitHub CI 为准。
"""

import os

os.environ.setdefault("DATABASE_TYPE", "sqlite")
os.environ.setdefault("REDIS_ENABLED", "false")

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from urllib.parse import parse_qs, urlsplit

from backend import models as base_models
from backend.emby_server import models as em
from backend.emby_server import upstream_aggregate_run as uar
from backend.emby_server import upstream_item_mappings as uim


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
def _category_route_library(db):
    # 备注-分类对位：本地行都在 library_id=1，备同名对位库。
    db.add(em.Library(id=1, guid="lib-guid-1", name="聚合库"))
    db.flush()


def _source(db):
    row = em.UpstreamSource(
        name="src-1", source_type="emby",
        base_url="https://up.example.com/emby", auth_method="none",
        enabled=True, credential_scope="source", health_status="unknown",
        note="测试-聚合库")
    db.add(row)
    db.flush()
    return row


def _local(db, *, item_id, name, item_type, tmdb=None, series_id=None,
           season=None, episode=None):
    row = em.MediaItem(
        id=item_id, guid=f"guid-{item_id}", library_id=1,
        item_type=item_type, name=name, tmdb_id=tmdb,
        series_id=series_id, season_number=season,
        episode_number=episode, merged_into_id=None)
    db.add(row)
    db.flush()
    return row


def _episode_raw(item_id, *, season, episode, tmdb=None, series_name="三体"):
    return {"Id": item_id, "Name": f"第 {episode} 集", "Type": "Episode",
            "SeriesName": series_name, "ParentIndexNumber": season,
            "IndexNumber": episode,
            "ProviderIds": ({"Tmdb": tmdb} if tmdb else {}),
            "MediaSources": []}


def _movie_raw(item_id, *, tmdb="278"):
    return {"Id": item_id, "Name": "肖申克的救赎", "Type": "Movie",
            "ProductionYear": 1994,
            "ProviderIds": ({"Tmdb": tmdb} if tmdb else {}),
            "MediaSources": [{"Id": f"ms-{item_id}", "Name": "1080p"}]}


class _FakeUpstream:
    """MediaFolders 返回多库；Items 按 ParentId 分库返回。"""

    def __init__(self, libraries, by_parent):
        self.libraries = libraries
        self.by_parent = by_parent

    def get(self, url, headers, timeout):
        if url.split("?")[0].endswith("/Library/MediaFolders"):
            return 200, {"Items": [
                {"Id": lib_id, "Name": name}
                for lib_id, name in self.libraries]}
        query = parse_qs(urlsplit(url).query)
        parent = query.get("ParentId", [None])[0]
        items = self.by_parent.get(parent, [])
        start = int(query["StartIndex"][0])
        limit = int(query["Limit"][0])
        return 200, {"Items": items[start:start + limit],
                     "TotalRecordCount": len(items)}


def _mappings(db):
    return {r.upstream_item_id: r
            for r in db.query(em.UpstreamItemMapping).all()}


# ---------------------------------------------------------------------------
# 整轮复现：第 0 集（IndexNumber=0）配上后写映射，整轮必须跑完
# ---------------------------------------------------------------------------


def test_run_episode_index_zero_completes_and_writes(db, monkeypatch):
    source = _source(db)
    # 本地特别篇行：S00E00，集号 0，与本地口径一致。
    _local(db, item_id=31, name="第 0 集", item_type="episode",
           tmdb="555", season=0, episode=0)
    _local(db, item_id=10, name="肖申克的救赎", item_type="movie",
           tmdb="278")
    fake = _FakeUpstream(
        [("lib-a", "剧集库"), ("lib-b", "电影库")],
        {"lib-a": [_episode_raw("u-e0", season=0, episode=0, tmdb="555")],
         "lib-b": [_movie_raw("u-m")]})
    monkeypatch.setenv(uim.MAPPING_ENABLED_ENV, "1")
    result = uar.run_manual_aggregation(db, source, http_get=fake.get)
    assert result["failed_libraries"] == []
    assert [lib["status"] for lib in result["libraries"]] == ["ok", "ok"]
    assert result["paired"] == 2
    assert result["written"] == 2
    assert result["unpaired_reasons"] == {}
    rows = _mappings(db)
    assert rows["u-e0"].episode_index == 0
    assert rows["u-e0"].evidence == "tmdb"
    assert rows["u-m"].episode_index is None


def test_run_episode_index_zero_via_series_alignment(db, monkeypatch):
    # O-08 系列级对齐路径同样产出 0：无自身身份的 S00E00，靠系列
    # tmdb + 季集号等值对齐本地 0 号行，写映射不得崩。
    source = _source(db)
    _local(db, item_id=30, name="三体", item_type="series", tmdb="111")
    _local(db, item_id=31, name="第 0 集", item_type="episode",
           series_id=30, season=0, episode=0)
    fake = _FakeUpstream(
        [("lib-a", "剧集库")],
        {"lib-a": [
            {"Id": "u-s", "Name": "三体", "Type": "Series",
             "ProductionYear": 2023, "ProviderIds": {"Tmdb": "111"},
             "MediaSources": []},
            _episode_raw("u-e0", season=0, episode=0),
        ]})
    monkeypatch.setenv(uim.MAPPING_ENABLED_ENV, "1")
    result = uar.run_manual_aggregation(db, source, http_get=fake.get)
    assert result["failed_libraries"] == []
    assert result["written"] == 2
    rows = _mappings(db)
    assert rows["u-e0"].episode_index == 0
    assert rows["u-e0"].evidence == "series+episode"


# ---------------------------------------------------------------------------
# per-item 兜底：坏集号（负数）只丢这一对，同库其余与后续库不受影响
# ---------------------------------------------------------------------------


def test_run_invalid_episode_index_isolated_per_item(db, monkeypatch):
    source = _source(db)
    _local(db, item_id=31, name="坏集号", item_type="episode",
           tmdb="556", season=1, episode=1)
    _local(db, item_id=32, name="正常集", item_type="episode",
           tmdb="557", season=1, episode=3)
    _local(db, item_id=10, name="肖申克的救赎", item_type="movie",
           tmdb="278")
    fake = _FakeUpstream(
        [("lib-a", "剧集库"), ("lib-b", "电影库")],
        {"lib-a": [
            _episode_raw("u-bad", season=1, episode=-1, tmdb="556"),
            _episode_raw("u-good", season=1, episode=3, tmdb="557"),
        ],
         "lib-b": [_movie_raw("u-m")]})
    monkeypatch.setenv(uim.MAPPING_ENABLED_ENV, "1")
    result = uar.run_manual_aggregation(db, source, http_get=fake.get)
    assert result["failed_libraries"] == []
    assert [lib["status"] for lib in result["libraries"]] == ["ok", "ok"]
    # 坏对去向明确：配对成立但写被拒，计 episode_index_invalid。
    assert result["paired"] == 3
    assert result["written"] == 2
    assert result["unpaired_reasons"] == {"episode_index_invalid": 1}
    by_uid = {p["upstream_item_id"]: p for p in result["pairs"]}
    assert by_uid["u-bad"]["written"] is False
    assert by_uid["u-good"]["written"] is True
    assert by_uid["u-m"]["written"] is True
    rows = _mappings(db)
    assert set(rows) == {"u-good", "u-m"}
    assert rows["u-good"].episode_index == 3
