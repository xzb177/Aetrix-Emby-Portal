# -*- coding: utf-8 -*-
"""上游聚合加速（2026-11-01 连夜单）：批量配对写映射 / 进库批量
定位 / 拉取有界并发。

背景证据（parent 19:40 亲查 + 本地探针实测）：限额试点在国产剧库
（61k 条）磨 85+ 分钟未完，进程 CPU 满转——成本不在拉取也不在
配对（pair_aggregates 一窗 5000 条仅 102 SQL），而在：
- 写映射逐对 SELECT+flush（3100 对 = 6200 SQL）；
- 进库逐集定位/查行/保季/写映射约 7 SQL/集（一窗 1900 集补集
  = 13601 SQL），其中无 SeriesId 单集的标题回落每集全表扫描
  两遍（800 剧规模实测 44ms/集，随本地剧总数线性涨）。

本文件钉死加速口径：
- ``record_mappings_batch`` 与逐条 ``record_mapping`` 落库结果
  等价；校验不过的条目被隔离报告、不连累整批；开关关时全 None；
- ``import_unpaired_series_tree`` 的统计与落库行与旧口径一致，
  但 SELECT 数有硬上限（批量定位/预取后不随集数线性涨）；
- 拉取并发键 ``upstream_aggregate_fetch_concurrency``：默认 4、
  钳 [1, 16]、config_schema 已注册、save_config 校验同口径；
- 预取并发只改请求时序不改结果：记录顺序/集合与串行逐字一致、
  总量准确时请求页集合不多不少；增量（带 sort_by）恒串行
  （水位早停语义不许被预取越界请求破坏）；页错误仍在消费到
  该页时按原映射抛出；
- 剧数限额拒绝的剧，其集在定位前按 SeriesId 确定性跳过
  （不触发标题回落扫描）——旧行为回归守卫。
"""

from __future__ import annotations

import os

os.environ.setdefault("DATABASE_TYPE", "sqlite")
os.environ.setdefault("REDIS_ENABLED", "false")

import threading
import time
import uuid
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import backend.models as base_models
from backend import config_schema, config_service
from backend.api import admin_upstream_sources as admin_ups
from backend.emby_server import models as em
from backend.emby_server import upstream_aggregation as agg
from backend.emby_server import upstream_aggregate_run as uar
from backend.emby_server import upstream_import as uimp
from backend.emby_server import upstream_item_mappings as uim
from backend.integrations import store as config_store


@pytest.fixture()
def db_factory():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False},
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
    config_store._ttl_cache.clear()
    yield
    config_store._ttl_cache.clear()


@pytest.fixture()
def mapping_on(monkeypatch):
    monkeypatch.setenv("UPSTREAM_ITEM_MAPPING_ENABLED", "1")


def _set_config(db, key, value):
    row = (db.query(base_models.SystemConfig)
           .filter(base_models.SystemConfig.key == key).first())
    if row is None:
        db.add(base_models.SystemConfig(key=key, value=value))
    else:
        row.value = value
    db.flush()
    config_store._ttl_cache.clear()


def _source(db, *, name="src-accel"):
    row = em.UpstreamSource(
        name=name, source_type="emby",
        base_url="https://up.example.com/emby", auth_method="none",
        enabled=True, credential_scope="source",
        health_status="unknown", import_enabled=True)
    db.add(row)
    db.flush()
    return row


# ---------------------------------------------------------------------------
# 并发键：默认值 / 钳制 / 注册表 / 保存校验 / 管理端点
# ---------------------------------------------------------------------------

def test_fetch_concurrency_default_and_clamp(db):
    assert uar.configured_fetch_concurrency(db) == 4
    _set_config(db, "upstream_aggregate_fetch_concurrency", "8")
    assert uar.configured_fetch_concurrency(db) == 8
    _set_config(db, "upstream_aggregate_fetch_concurrency", "1")
    assert uar.configured_fetch_concurrency(db) == 1
    _set_config(db, "upstream_aggregate_fetch_concurrency", "16")
    assert uar.configured_fetch_concurrency(db) == 16
    _set_config(db, "upstream_aggregate_fetch_concurrency", "99")
    assert uar.configured_fetch_concurrency(db) == 16
    _set_config(db, "upstream_aggregate_fetch_concurrency", "0")
    assert uar.configured_fetch_concurrency(db) == 1
    _set_config(db, "upstream_aggregate_fetch_concurrency", "abc")
    assert uar.configured_fetch_concurrency(db) == 4


def test_fetch_concurrency_schema_registration(db):
    assert (config_schema.UPSTREAM_AGGREGATE_CONFIG_SCHEMA[
        "upstream_aggregate_fetch_concurrency"] == ("int", 1, 16))
    saved = config_service.save_config(
        db, {"upstream_aggregate_fetch_concurrency": 6},
        schema="upstream_aggregate")
    assert saved == ["upstream_aggregate_fetch_concurrency"]
    with pytest.raises(config_schema.ConfigValueError):
        config_service.save_config(
            db, {"upstream_aggregate_fetch_concurrency": 0},
            schema="upstream_aggregate")
    with pytest.raises(config_schema.ConfigValueError):
        config_service.save_config(
            db, {"upstream_aggregate_fetch_concurrency": 17},
            schema="upstream_aggregate")


def test_fetch_concurrency_admin_endpoint(db):
    view = admin_ups.get_upstream_fetch_concurrency_config(
        staff=None, db=db)
    assert view == {"concurrency": 4}
    view = admin_ups.put_upstream_fetch_concurrency_config(
        admin_ups.UpstreamFetchConcurrencyConfigIn(concurrency=12),
        staff=None, db=db)
    assert view == {"concurrency": 12}
    db.commit()
    assert uar.configured_fetch_concurrency(db) == 12
    with pytest.raises(config_schema.ConfigValueError):
        admin_ups.put_upstream_fetch_concurrency_config(
            admin_ups.UpstreamFetchConcurrencyConfigIn(concurrency=64),
            staff=None, db=db)


# ---------------------------------------------------------------------------
# record_mappings_batch：与逐条等价 / 非法隔离 / 开关关
# ---------------------------------------------------------------------------

def _mapping_entries():
    return [
        dict(local_item_id=10, source_id=3, upstream_item_id="up-a",
             confidence=0.9, evidence="tmdb"),
        dict(local_item_id=10, source_id=3, upstream_item_id="up-b",
             confidence=0.7, evidence="title+year", episode_index=5),
        dict(local_item_id=11, source_id=3, upstream_item_id="up-a",
             confidence=1.0, evidence="tmdb", episode_index=0),
        # 与第一条同三元组重复写：更新置信度/证据，episode_index
        # 传 None 不覆盖（本组第一条本就 None，结果应为 None）。
        dict(local_item_id=10, source_id=3, upstream_item_id="up-a",
             confidence=0.5, evidence="imdb"),
        # 与第二条同三元组：episode_index 非 None 才覆盖 → 7。
        dict(local_item_id=10, source_id=3, upstream_item_id="up-b",
             confidence=0.8, evidence="imdb", episode_index=7),
    ]


def _mapping_rows(db):
    return sorted(
        (r.local_item_id, r.source_id, r.upstream_item_id,
         r.confidence, r.evidence, r.episode_index)
        for r in db.query(em.UpstreamItemMapping).all())


def test_batch_mapping_matches_single_writes(db_factory, mapping_on):
    entries = _mapping_entries()
    db1 = db_factory()
    for entry in entries:
        uim.record_mapping(db1, **entry)
    db1.commit()
    expected = _mapping_rows(db1)
    db1.close()

    db2 = db_factory()
    results, invalid = uim.record_mappings_batch(db2, entries)
    db2.commit()
    assert invalid == set()
    assert all(row is not None for row in results)
    assert _mapping_rows(db2) == expected
    db2.close()


def test_batch_mapping_isolates_invalid_entries(db, mapping_on):
    entries = _mapping_entries() + [
        dict(local_item_id=12, source_id=3, upstream_item_id="up-bad",
             confidence=0.5, evidence="tmdb", episode_index=-1),
        dict(local_item_id=12, source_id=3, upstream_item_id="  ",
             confidence=0.5, evidence="tmdb"),
    ]
    results, invalid = uim.record_mappings_batch(db, entries)
    db.commit()
    assert invalid == {5, 6}
    assert results[5] is None and results[6] is None
    assert all(row is not None for row in results[:5])
    assert (db.query(em.UpstreamItemMapping)
            .filter(em.UpstreamItemMapping.upstream_item_id == "up-bad")
            .count()) == 0


def test_batch_mapping_switch_off(db):
    results, invalid = uim.record_mappings_batch(db, _mapping_entries())
    assert invalid == set()
    assert all(row is None for row in results)
    assert db.query(em.UpstreamItemMapping).count() == 0


# ---------------------------------------------------------------------------
# 拉取预取并发：结果一致 / 请求页不多不少 / 增量恒串行 / 错误定位
# ---------------------------------------------------------------------------

class _FakeUpstream:
    """替身上游：按 StartIndex/Limit 切页，记录在途并发与请求页。"""

    def __init__(self, total, *, fail_starts=()):
        self.total = total
        self.fail_starts = set(fail_starts)
        self.lock = threading.Lock()
        self.in_flight = 0
        self.max_in_flight = 0
        self.requested = []

    def get(self, url, headers, timeout):
        params = parse_qs(urlsplit(url).query)
        start = int(params["StartIndex"][0])
        limit = int(params["Limit"][0])
        with self.lock:
            self.in_flight += 1
            self.max_in_flight = max(self.max_in_flight, self.in_flight)
            self.requested.append(start)
        try:
            time.sleep(0.05)
            if start in self.fail_starts:
                return 403, {"Message": "forbidden"}
            items = [
                {"Id": str(i), "Name": f"条目{i}", "Type": "Series"}
                for i in range(start, min(start + limit, self.total))]
            return 200, {"Items": items,
                         "TotalRecordCount": self.total}
        finally:
            with self.lock:
                self.in_flight -= 1


def _fake_source():
    return SimpleNamespace(
        id=7, base_url="https://up.example.com/emby",
        user_agent=None, referer=None, extra_headers_json=None)


def test_prefetch_concurrent_same_results_and_pages(db):
    fake = _FakeUpstream(total=95)
    records = list(uar.iter_upstream_records_for_parent(
        _fake_source(), "lib-1", http_get=fake.get, api_key="k",
        page_size=10, fetch_concurrency=4))
    assert [r.upstream_item_id for r in records] == [
        str(i) for i in range(95)]
    assert sorted(fake.requested) == list(range(0, 95, 10))
    assert 2 <= fake.max_in_flight <= 4


def test_prefetch_sequential_when_concurrency_1(db):
    fake = _FakeUpstream(total=95)
    records = list(uar.iter_upstream_records_for_parent(
        _fake_source(), "lib-1", http_get=fake.get, api_key="k",
        page_size=10, fetch_concurrency=1))
    assert len(records) == 95
    assert fake.max_in_flight == 1
    assert fake.requested == list(range(0, 95, 10))


def test_prefetch_disabled_for_incremental_sort(db):
    fake = _FakeUpstream(total=95)
    seen_pages = {"n": 0}

    def _on_page(page_records, total):
        seen_pages["n"] += 1
        return seen_pages["n"] >= 2  # 第二页后早停

    records = list(uar.iter_upstream_records_for_parent(
        _fake_source(), "lib-1", http_get=fake.get, api_key="k",
        page_size=10, fetch_concurrency=4,
        sort_by="DateLastContentAdded", on_page=_on_page))
    assert len(records) == 20
    assert fake.requested == [0, 10]
    assert fake.max_in_flight == 1


def test_prefetch_error_surfaces_at_its_page(db):
    fake = _FakeUpstream(total=95, fail_starts={30})
    got = []
    with pytest.raises(uar.UpstreamAggregateRunError) as excinfo:
        for record in uar.iter_upstream_records_for_parent(
                _fake_source(), "lib-1", http_get=fake.get, api_key="k",
                page_size=10, fetch_concurrency=4):
            got.append(record.upstream_item_id)
    assert excinfo.value.code == "upstream_credential_invalid"
    assert got == [str(i) for i in range(30)]


# ---------------------------------------------------------------------------
# 进库批量化：统计/落库不变，SELECT 数有硬上限
# ---------------------------------------------------------------------------

def _line(source_id, upstream_id, episode_index=None):
    return agg.UpstreamLine(
        line_index=0, line_key=f"{source_id}:{upstream_id}",
        source_id=str(source_id), upstream_item_id=upstream_id,
        episode_index=episode_index)


def _agg_series(source_id, upstream_id, name, tmdb):
    return agg.UpstreamAggregateItem(
        aggregate_key=f"tmdb:{tmdb}", name=name, item_type="Series",
        tmdb_id=tmdb, lines=[_line(source_id, upstream_id)])


def _agg_episode(source_id, upstream_id, series_name, episode,
                 series_upstream_id=""):
    return agg.UpstreamAggregateItem(
        aggregate_key=f"title:{series_name}::s1:e{episode}",
        name=series_name, item_type="Episode",
        season_number=1, series_name=series_name,
        episode_name=f"第{episode}集",
        series_upstream_id=series_upstream_id,
        lines=[_line(source_id, upstream_id, episode)])


def _seed_local_series(db, library_id, name, tmdb, *, episodes=30):
    series = em.MediaItem(
        guid=uuid.uuid4().hex, library_id=library_id,
        item_type="series", name=name, tmdb_id=tmdb,
        production_year=2021)
    db.add(series)
    db.flush()
    season = em.MediaItem(
        guid=uuid.uuid4().hex, library_id=library_id,
        item_type="season", name="第 1 季", parent_id=series.id,
        series_id=series.id, season_number=1)
    db.add(season)
    db.flush()
    for number in range(1, episodes + 1):
        db.add(em.MediaItem(
            guid=uuid.uuid4().hex, library_id=library_id,
            item_type="episode", name=f"第{number}集",
            parent_id=season.id, series_id=series.id,
            season_number=1, episode_number=number))
    db.flush()
    return series


def test_import_series_tree_batched_select_budget(db, mapping_on):
    source = _source(db)
    library = em.Library(
        guid=uuid.uuid4().hex, name="国产剧",
        collection_type="tvshows", is_enabled=True, is_virtual=False)
    db.add(library)
    db.flush()
    # 30 部已配对本地剧（各有 30 集）+ 1 部不在本窗的本地剧 X。
    local_ids = {}
    for s in range(30):
        row = _seed_local_series(
            db, library.id, f"本地剧{s}", str(200000 + s))
        local_ids[s] = row.id
    _seed_local_series(db, library.id, "本地剧X", "299999")
    db.commit()

    items = []
    pairs = []
    for s in range(30):
        items.append(_agg_series(source.id, f"S{s}", f"本地剧{s}",
                                 str(200000 + s)))
        pairs.append({
            "aggregate_key": f"tmdb:{200000 + s}", "name": f"本地剧{s}",
            "item_type": "Series", "local_item_id": local_ids[s],
            "upstream_item_id": f"S{s}", "episode_index": None,
            "confidence": 1.0, "evidence": "tmdb"})
        for e in range(31, 41):  # 每部补 10 集（带 SeriesId 定位）
            items.append(_agg_episode(
                source.id, f"S{s}E{e}", f"本地剧{s}", e,
                series_upstream_id=f"S{s}"))
    # 200 集无 SeriesId、剧不在本窗 → 旧实现每集两次全表扫描。
    for e in range(31, 231):
        items.append(_agg_episode(
            source.id, f"XE{e}", "本地剧X", e))

    engine = db.get_bind()
    counter = {"select": 0}

    def _count(conn, cursor, statement, parameters, context, executemany):
        if statement.lstrip().upper().startswith("SELECT"):
            counter["select"] += 1

    event.listen(engine, "before_cursor_execute", _count)
    try:
        stats = uimp.import_unpaired_series_tree(
            db, source, items, pairs,
            upstream_library_name="国产剧",
            upstream_library_id="lib-guochan",
            upstream_collection_type="tvshows")
    finally:
        event.remove(engine, "before_cursor_execute", _count)
    db.commit()

    assert stats["episodes_imported"] == 500
    assert stats["imported"] == 500
    assert stats["seasons_created"] == 0
    assert stats["no_series"] == 0
    assert stats["series_imported"] == 0
    # 落库行核对：500 集全挂对剧、季、集号。
    hosted_eps = (db.query(em.MediaItem)
                  .filter(em.MediaItem.upstream_source_id == source.id,
                          em.MediaItem.item_type == "episode")
                  .all())
    assert len(hosted_eps) == 500
    assert all(ep.series_id and ep.season_number == 1
               for ep in hosted_eps)
    assert (db.query(em.UpstreamItemMapping)
            .filter(em.UpstreamItemMapping.source_id == source.id)
            .count()) == 500
    # 批量化硬指标：旧实现同形态约 3000+ 条 SELECT（逐集约 6.5 条
    # + 标题回落全表扫描），批量后只许两位数级。
    assert counter["select"] < 150, counter


def test_import_rejected_series_episodes_skip_resolution(db, mapping_on):
    """剧数限额拒绝的剧：其集按 SeriesId 跳过，不触发标题回落。"""
    source = _source(db)
    library = em.Library(
        guid=uuid.uuid4().hex, name="国产剧",
        collection_type="tvshows", is_enabled=True, is_virtual=False)
    db.add(library)
    db.flush()
    _seed_local_series(db, library.id, "同名本地剧", "288888")
    db.commit()

    # 无身份外的新剧（有 tmdb）+ 其集：预算 0 → 整树拒绝；集名
    # 与本地同名剧同名，若误走标题回落会被挂错剧——必须跳过。
    items = [
        agg.UpstreamAggregateItem(
            aggregate_key="tmdb:277777", name="同名本地剧",
            item_type="Series", tmdb_id="277777",
            lines=[_line(source.id, "NS1")]),
    ] + [
        _agg_episode(source.id, f"NS1E{e}", "同名本地剧", e,
                     series_upstream_id="NS1")
        for e in range(1, 6)
    ]
    stats = uimp.import_unpaired_series_tree(
        db, source, items, [],
        upstream_library_name="国产剧",
        upstream_library_id="lib-guochan",
        upstream_collection_type="tvshows",
        series_limit=0)
    db.commit()
    assert stats["series_limited"] == 6  # 1 剧 + 5 集
    assert stats["episodes_imported"] == 0
    assert (db.query(em.MediaItem)
            .filter(em.MediaItem.upstream_source_id == source.id)
            .count()) == 0
