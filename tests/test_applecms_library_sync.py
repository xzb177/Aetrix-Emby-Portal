# -*- coding: utf-8 -*-
"""苹果 CMS 库同步工人回归（新媒体库类型一期·同步单）。

口径（2026-11-02 定案）：
- 同步只拉 provide ``ac=list`` 分页，**不拉 detail**（播放地址绝不
  落库、元数据由 TMDB 补全链补）；行建成托管行（guid=hosted_guid、
  upstream_source_id=主来源、probe_status=done、enrich_status 保持
  pending 交给 enrich 补身份），对不上身份的行靠现行货架规矩不上架。
- 去重标题+年份键（aggregate_key_for）：跨源同片一行多映射。
- 一期只做电影：剧集形态（vod_serial>1）跳过计数；cms_type_map
  顶级分类 "out" 跳过、没写到默认收。
- 映射开关关 = 整批 blocked 不建行（没映射的行播不了，不建孤儿行）。
- 增量走 h=24 窗口、全量不带 h；进度写回库行 scan_stats/last_scan_at。
"""

from __future__ import annotations

import json
import urllib.parse

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend import models as base_models
from backend.emby_server import applecms_library_sync as als
from backend.emby_server import models as em
from backend.emby_server.upstream_import import hosted_guid


def _maps_for(db, item_id):
    return (db.query(em.UpstreamItemMapping)
            .filter(em.UpstreamItemMapping.local_item_id == item_id)
            .all())


CLASS_TREE = [
    {"type_id": 1, "type_pid": 0, "type_name": "电影"},
    {"type_id": 2, "type_pid": 1, "type_name": "动作片"},
    {"type_id": 5, "type_pid": 0, "type_name": "综艺"},
]


def _entry(vod_id, name, *, year="2021", type_id=2, serial="0",
           vod_time="2024-05-01 10:00:00"):
    return {
        "vod_id": vod_id, "vod_name": name, "vod_year": year,
        "type_id": type_id, "type_name": "动作片", "vod_serial": serial,
        "vod_time": vod_time, "vod_pic": "http://img.example/x.jpg",
        "vod_content": "简介文本", "vod_class": "动作",
    }


SOURCE7_PAGE = {
    "code": 1, "page": 1, "pagecount": 1, "limit": 20, "total": 4,
    "class": CLASS_TREE,
    "list": [
        _entry(101, "沙丘"),
        _entry(102, "庆余年", serial="36"),               # 剧集形态
        _entry(103, "快乐大本营", type_id=5, serial="1"),  # 综艺（map out）
        _entry(104, "无年份片", year=""),
    ],
}

SOURCE8_PAGE = {
    "code": 1, "page": 1, "pagecount": 1, "limit": 20, "total": 2,
    "class": CLASS_TREE,
    "list": [
        _entry("55", "沙丘"),       # 与源 7 同片（标题+年份键去重）
        _entry("56", "流浪地球", year="2019"),
    ],
}


class FakeHttpGet:
    """按 base_url 路由的 provide 桩；记录全部请求 URL 供断言。"""

    def __init__(self):
        self.urls = []

    def __call__(self, url, headers=None, timeout=None):
        self.urls.append(url)
        host = urllib.parse.urlparse(url).netloc
        payload = {"s7.example": SOURCE7_PAGE,
                   "s8.example": SOURCE8_PAGE}[host]
        # fetcher 的 http_get 契约：返回 (status, 已解析的 dict)
        return 200, json.loads(json.dumps(payload, ensure_ascii=False))


@pytest.fixture()
def db(monkeypatch):
    monkeypatch.setenv("UPSTREAM_ITEM_MAPPING_ENABLED", "1")
    engine = create_engine("sqlite:///:memory:")
    base_models.Base.metadata.create_all(engine)
    em.Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    try:
        yield session
    finally:
        session.close()


def _seed(db, *, type_map=None):
    for sid, host in ((7, "s7.example"), (8, "s8.example")):
        db.add(em.UpstreamSource(
            id=sid, name=f"源{sid}", source_type="applecms",
            base_url=f"http://{host}", enabled=True,
            user_agent="Mozilla/5.0 (test)"))
    lib = em.Library(
        guid="lib-cms-movies", name="CMS电影",
        collection_type="movies", is_enabled=True,
        kind="applecms", upstream_source_ids="7,8",
        cms_type_map=json.dumps(type_map, ensure_ascii=False)
        if type_map is not None else None)
    db.add(lib)
    db.commit()
    return lib


def _movies(db, lib):
    return (db.query(em.MediaItem)
            .filter(em.MediaItem.library_id == lib.id)
            .order_by(em.MediaItem.name)
            .all())


def test_sync_builds_hosted_rows_and_dedupes(db):
    lib = _seed(db, type_map={"综艺": "out"})
    http = FakeHttpGet()
    stats = als.sync_library(db, lib, http_get=http)
    assert stats["blocked"] is None
    rows = _movies(db, lib)
    # 沙丘（跨源去重一行）+ 无年份片 + 流浪地球 = 3 行
    assert [r.name for r in rows] == ["无年份片", "沙丘", "流浪地球"]
    dune = next(r for r in rows if r.name == "沙丘")
    assert dune.upstream_source_id == 7  # 先到源为主来源
    assert dune.guid == hosted_guid(7, "101")
    assert dune.probe_status == "done"
    assert dune.enrich_status == "pending"  # 身份交给 enrich 补 TMDB
    assert dune.tmdb_id is None
    assert dune.production_year == 2021
    maps = _maps_for(db, dune.id)
    assert {(m.source_id, m.upstream_item_id) for m in maps} == {
        (7, "101"), (8, "55")}
    assert all(m.confidence == 0.7 and m.evidence == "title+year"
               for m in maps)
    assert stats["added"] == 3 and stats["mapped"] == 4
    assert stats["skipped_series_shape"] == 1
    assert stats["skipped_category"] == 1
    assert stats["seen_by_source"] == {
        7: ["101", "102", "103", "104"], 8: ["55", "56"]}
    # detail 一次都不许拉：全部请求都是 ac=list
    assert http.urls and all("ac=list" in u for u in http.urls)
    # 进度写回库行
    db.refresh(lib)
    assert lib.last_scan_at is not None
    saved = json.loads(lib.scan_stats)
    assert saved["added"] == 3 and saved["cms_sync"]["mapped"] == 4


def test_sync_rerun_is_idempotent(db):
    lib = _seed(db, type_map={"综艺": "out"})
    als.sync_library(db, lib, http_get=FakeHttpGet())
    stats2 = als.sync_library(db, lib, http_get=FakeHttpGet())
    assert stats2["added"] == 0 and stats2["existing"] == 4  # 按条目计：沙丘两源各记一次
    assert len(_movies(db, lib)) == 3
    total_maps = sum(
        len(_maps_for(db, r.id)) for r in _movies(db, lib))
    assert total_maps == 4


def test_sync_blocked_when_mapping_off(db, monkeypatch):
    monkeypatch.setenv("UPSTREAM_ITEM_MAPPING_ENABLED", "0")
    lib = _seed(db)
    stats = als.sync_library(db, lib, http_get=FakeHttpGet())
    assert stats["blocked"] == "mapping_off"
    assert _movies(db, lib) == []


def test_incremental_uses_h24_window_full_does_not(db):
    lib = _seed(db)
    http_inc = FakeHttpGet()
    als.sync_library(db, lib, http_get=http_inc)
    assert all("h=24" in u for u in http_inc.urls)
    http_full = FakeHttpGet()
    als.sync_library(db, lib, http_get=http_full, full=True)
    assert all("h=" not in u.split("ac=list")[-1] or "h=24" not in u
               for u in http_full.urls)


def test_type_map_default_in_and_top_category_resolution(db):
    # 没写 cms_type_map：综艺条目默认收（全收口径）；type_id 子类要
    # 沿分类树回溯到顶级名再查表——这里用 {"电影": "out"} 验证回溯：
    # 动作片（子类）的顶级是电影，应被 out 掉。
    lib = _seed(db, type_map={"电影": "out"})
    stats = als.sync_library(db, lib, http_get=FakeHttpGet())
    names = [r.name for r in _movies(db, lib)]
    assert "沙丘" not in names and "流浪地球" not in names
    assert stats["skipped_category"] >= 3


def test_dedupe_via_name_candidate_path_across_runs(db):
    """跨轮去重候选路径回归（试点实测抓到的崩溃）：第二轮换源喂
    同片时 run_cache 是空的，必须走「按名查候选行→对键」路径，
    不许崩、要挂上第二条映射而不是建重复行。"""
    lib = _seed(db, type_map={"综艺": "out"})
    lib.upstream_source_ids = "7"
    db.commit()
    als.sync_library(db, lib, http_get=FakeHttpGet())
    lib.upstream_source_ids = "7,8"
    db.commit()
    stats = als.sync_library(db, lib, http_get=FakeHttpGet())
    assert stats["errors"] == []
    dunes = [r for r in _movies(db, lib) if r.name == "沙丘"]
    assert len(dunes) == 1
    assert {(m.source_id, m.upstream_item_id)
            for m in _maps_for(db, dunes[0].id)} == {(7, "101"), (8, "55")}
    assert stats["dedupe_hits"] >= 1


def test_round_gate_off_by_default(db, monkeypatch):
    monkeypatch.delenv("APPLECMS_LIBRARY_SYNC_ENABLED", raising=False)
    _seed(db)
    assert als.sync_enabled() is False
    assert als.run_library_sync_round(
        session_factory=lambda: db, http_get=FakeHttpGet()) == []
