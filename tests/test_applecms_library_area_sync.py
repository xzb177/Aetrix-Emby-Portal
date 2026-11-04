# -*- coding: utf-8 -*-
"""苹果 CMS 库同步工人·地区分流回归（抓详情分流单，2026-11-02）。

口径：
- 库配了 ``cms_area_bucket`` 才分流：条目过分类白名单后判桶，
  list 带 ``vod_area`` 用 list 的、不打 detail；没有才对该
  （源, vod）打**一次** detail 取 area，detail 结果落
  ``applecms_detail_cache``，跨库/跨轮复用，绝不重复打。
- 桶不符 → 计 ``skipped_area`` 不建行不记映射；detail 失败/消失
  → 计 ``detail_errors`` 本轮跳过、下轮重试，绝不猜桶。
- detail 顺带 harvest（海报/简介/评分/演员等）按只填空落行，
  与多源合并填充同一落点口径；身份字段不碰。
- 没配桶的库（总库）行为逐字不变：零 detail 请求。
- 一轮调度里配桶库先于总库跑（guid 先到先锚：地区库先认领，
  总库只收剩下的其他/未知地区）。
- 存量迁移例程与工人共用同一缓存与同一 ``area_bucket``：干跑
  只计数不动行，真跑只改 ``library_id``。
"""

from __future__ import annotations

import json
import os
import urllib.parse

os.environ.setdefault("DATABASE_TYPE", "sqlite")
os.environ.setdefault("REDIS_ENABLED", "false")

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend import models as base_models
from backend.emby_server import applecms_area_migration as aam
from backend.emby_server import applecms_library_sync as als
from backend.emby_server import models as em
from backend.emby_server import portal
from backend.integrations import store


CLASS_TREE = [
    {"type_id": 1, "type_pid": 0, "type_name": "电影片"},
    {"type_id": 2, "type_pid": 1, "type_name": "动作片"},
]


def _fake_douban(name, year):
    import zlib
    return str(100000 + zlib.crc32(f"{name}|{year}".encode()) % 900000)


def _entry(vod_id, name, *, year="2021", area=None):
    entry = {
        "vod_id": vod_id, "vod_name": name, "vod_year": year,
        "type_id": 2, "type_name": "动作片", "vod_serial": "0",
        "vod_time": "2024-05-01 10:00:00",
        "vod_douban_id": _fake_douban(name, year),
    }
    if area is not None:
        entry["vod_area"] = area
    return entry


def _detail(vod_id, name, *, year="2021", area="", **extra):
    detail = {
        "vod_id": vod_id, "vod_name": name, "vod_year": year,
        "vod_area": area, "type_name": "动作片",
        "vod_play_from": "直链线路",
        "vod_play_url": "第1集$https://cdn.example.com/x.m3u8",
        "vod_douban_id": _fake_douban(name, year),
    }
    detail.update(extra)
    return detail


# 源 7：list 精简字段集（无 vod_area），地区只在 detail。
S7_LIST = {
    "code": 1, "page": 1, "pagecount": 1, "limit": 20, "total": 4,
    "class": CLASS_TREE,
    "list": [
        _entry(301, "日本片"),
        _entry(302, "失败片"),
        _entry(303, "甲片"),
        _entry("304", "同片", year="2020"),
    ],
}
S7_DETAILS = {
    "301": _detail(301, "日本片", area="日本",
                   vod_pic="http://img.example/jp.jpg",
                   vod_blurb="日本片简介", vod_score="8.1",
                   vod_actor="演员甲,演员乙", vod_director="导演甲",
                   vod_remarks="HD"),
    "302": "HTTP500",
    "303": _detail(303, "甲片", area="美国"),
    "304": _detail("304", "同片", year="2020", area="日本",
                   vod_blurb="乙源简介", vod_score="7.5",
                   vod_pic="http://img.example/dup.jpg"),
}

# 源 8：与源 7 同片不同 vod（标题+年份去重落同一行）。
S8_LIST = {
    "code": 1, "page": 1, "pagecount": 1, "limit": 20, "total": 1,
    "class": CLASS_TREE,
    "list": [_entry("404", "同片", year="2020")],
}
S8_DETAILS = {
    "404": _detail("404", "同片", year="2020", area="日本",
                   vod_blurb="丙源简介", vod_score="9.0"),
}

# 源 9：list 全字段型，自带 vod_area。
S9_LIST = {
    "code": 1, "page": 1, "pagecount": 1, "limit": 20, "total": 2,
    "class": CLASS_TREE,
    "list": [
        _entry(901, "美国片", area="美国"),
        _entry(902, "大陆片", area="中国大陆"),
    ],
}
S9_DETAILS = {}


class FakeHttpGet:
    """按 host + ac 路由的 provide 桩；detail 可按 vod 替换/置错。"""

    def __init__(self):
        self.urls = []
        self.lists = {"s7.example": S7_LIST, "s8.example": S8_LIST,
                      "s9.example": S9_LIST}
        self.details = {"s7.example": dict(S7_DETAILS),
                        "s8.example": dict(S8_DETAILS),
                        "s9.example": dict(S9_DETAILS)}

    def detail_urls(self):
        return [u for u in self.urls if "ac=detail" in u]

    def __call__(self, url, headers=None, timeout=None):
        self.urls.append(url)
        parsed = urllib.parse.urlparse(url)
        host = parsed.netloc
        query = urllib.parse.parse_qs(parsed.query)
        ac = (query.get("ac") or [""])[0]
        if ac == "list":
            return 200, json.loads(json.dumps(
                self.lists[host], ensure_ascii=False))
        if ac == "detail":
            vod_id = (query.get("ids") or [""])[0]
            payload = self.details[host].get(vod_id)
            if payload == "HTTP500":
                return 500, {"code": 0, "msg": "boom"}
            if payload is None:
                return 200, {"code": 1, "page": 1, "pagecount": 0,
                             "list": []}
            return 200, {"code": 1, "page": 1, "pagecount": 1,
                         "list": [json.loads(json.dumps(
                             payload, ensure_ascii=False))]}
        raise AssertionError(f"未预期的请求: {url}")


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


def _seed_source(db, sid, host):
    db.add(em.UpstreamSource(
        id=sid, name=f"源{sid}", source_type="applecms",
        base_url=f"http://{host}", enabled=True,
        user_agent="Mozilla/5.0 (test)"))


def _seed_lib(db, guid, name, source_ids, *, bucket=None):
    lib = em.Library(
        guid=guid, name=name, collection_type="movies", is_enabled=True,
        kind="applecms", upstream_source_ids=source_ids,
        cms_type_map=json.dumps({"电影片": "in"}, ensure_ascii=False),
        cms_area_bucket=bucket)
    db.add(lib)
    db.commit()
    return lib


def _rows(db, lib):
    return (db.query(em.MediaItem)
            .filter(em.MediaItem.library_id == lib.id)
            .all())


def test_provisional_marker_single_value():
    """源站暂定填充标记全仓只许一个值（两处常量以注释锚定）。"""
    from backend.emby_server import tmdb
    assert als.CMS_METADATA_SOURCE == "applecms"
    assert tmdb.PROVISIONAL_METADATA_SOURCE == als.CMS_METADATA_SOURCE


# ---------------------------------------------------------------------------
# list 自带 area：不打 detail 直接判桶
# ---------------------------------------------------------------------------

def test_list_area_routes_without_detail(db):
    _seed_source(db, 9, "s9.example")
    lib = _seed_lib(db, "lib-huayu", "华语电影", "9", bucket="huayu")
    http = FakeHttpGet()
    stats = als.sync_library(db, lib, http_get=http,
                             sleep_fn=lambda s: None)
    assert stats["blocked"] is None
    assert [r.name for r in _rows(db, lib)] == ["大陆片"]
    assert stats["added"] == 1
    assert stats["skipped_area"] == 1          # 美国片桶不符
    assert http.detail_urls() == []            # list 有 area，零 detail


# ---------------------------------------------------------------------------
# list 无 area：打一次 detail 判桶 + harvest 落行；跨库命中缓存
# ---------------------------------------------------------------------------

def test_detail_area_routes_and_harvests(db):
    _seed_source(db, 7, "s7.example")
    lib = _seed_lib(db, "lib-rihan", "日韩电影", "7", bucket="rihan")
    http = FakeHttpGet()
    stats = als.sync_library(db, lib, http_get=http,
                             sleep_fn=lambda s: None)
    rows = _rows(db, lib)
    assert sorted(r.name for r in rows) == ["同片", "日本片"]
    row = next(r for r in rows if r.name == "日本片")
    # harvest 只填空落点（与多源合并填充同口径）
    assert row.primary_image_url == "http://img.example/jp.jpg"
    assert row.overview == "日本片简介"
    assert row.community_rating == 8.1
    assert row.countries == "日本"
    assert row.tagline == "HD"
    assert row.metadata_source == "applecms"
    people = (db.query(em.EmbyPerson)
              .filter(em.EmbyPerson.item_id == row.id)
              .order_by(em.EmbyPerson.sort_order).all())
    assert [p.name for p in people] == ["演员甲", "演员乙"]
    # 甲片（美国）桶不符跳过；失败片 detail 失败跳过
    assert stats["skipped_area"] == 1
    assert stats["detail_errors"] == 1
    # detail 只对无 list area 的条目打：301/302/303/304 各一次
    assert len(http.detail_urls()) == 4
    saved = json.loads(lib.scan_stats)
    assert saved["cms_sync"]["skipped_area"] == 1
    assert saved["cms_sync"]["detail_errors"] == 1


def test_second_library_reuses_detail_cache(db):
    _seed_source(db, 7, "s7.example")
    rihan = _seed_lib(db, "lib-rihan", "日韩电影", "7", bucket="rihan")
    huayu = _seed_lib(db, "lib-huayu", "华语电影", "7", bucket="huayu")
    http = FakeHttpGet()
    als.sync_library(db, rihan, http_get=http, sleep_fn=lambda s: None)
    calls_after_first = len(http.detail_urls())
    assert calls_after_first == 4
    stats = als.sync_library(db, huayu, http_get=http,
                             sleep_fn=lambda s: None)
    # 第二个库只多打一次：失败片不缓存失败、重试一次，其余全命中缓存
    assert len(http.detail_urls()) == calls_after_first + 1
    assert _rows(db, huayu) == []
    assert stats["skipped_area"] == 3          # 日本片/甲片/同片都不是华语
    assert stats["detail_errors"] == 1         # 失败片仍失败（不缓存失败）


def test_detail_failure_retries_next_round(db):
    _seed_source(db, 7, "s7.example")
    lib = _seed_lib(db, "lib-rihan", "日韩电影", "7", bucket="rihan")
    http = FakeHttpGet()
    als.sync_library(db, lib, http_get=http, sleep_fn=lambda s: None)
    first_calls = len(http.detail_urls())
    # 源站恢复后下一轮：失败片重新打 detail 并建成行
    http.details["s7.example"]["302"] = _detail(302, "失败片", area="韩国")
    stats = als.sync_library(db, lib, http_get=http,
                             sleep_fn=lambda s: None)
    assert len(http.detail_urls()) == first_calls + 1   # 只重试失败的那条
    assert stats["detail_errors"] == 0
    assert sorted(r.name for r in _rows(db, lib)) == ["同片", "失败片", "日本片"]


# ---------------------------------------------------------------------------
# harvest 只填空：后到源不覆盖已有非空值
# ---------------------------------------------------------------------------

def test_harvest_fills_only_empty_fields(db):
    _seed_source(db, 7, "s7.example")
    _seed_source(db, 8, "s8.example")
    lib = _seed_lib(db, "lib-rihan", "日韩电影", "7,8", bucket="rihan")
    http = FakeHttpGet()
    als.sync_library(db, lib, http_get=http, sleep_fn=lambda s: None)
    rows = [r for r in _rows(db, lib) if r.name == "同片"]
    assert len(rows) == 1                       # 标题+年份去重一行
    row = rows[0]
    # 先到源 7 的 detail 先落：简介/海报来自源 7
    assert row.overview == "乙源简介"
    assert row.primary_image_url == "http://img.example/dup.jpg"
    # 源 8 的评分更高也不覆盖（先到先得，只填空）
    assert row.community_rating == 7.5


# ---------------------------------------------------------------------------
# 总库（没配桶）行为不变：零 detail
# ---------------------------------------------------------------------------

def test_buckless_library_never_fetches_detail(db):
    _seed_source(db, 7, "s7.example")
    lib = _seed_lib(db, "lib-total", "在线电影", "7")
    http = FakeHttpGet()
    stats = als.sync_library(db, lib, http_get=http,
                             sleep_fn=lambda s: None)
    assert stats["blocked"] is None
    assert len(_rows(db, lib)) == 4
    assert http.detail_urls() == []
    assert stats.get("skipped_area", 0) == 0


# ---------------------------------------------------------------------------
# 一轮调度顺序：配桶库先认领，总库只收剩下的
# ---------------------------------------------------------------------------

def test_round_runs_bucket_libraries_first(db, monkeypatch):
    monkeypatch.setenv("APPLECMS_LIBRARY_SYNC_ENABLED", "1")
    _seed_source(db, 7, "s7.example")
    total = _seed_lib(db, "lib-total", "在线电影", "7")
    rihan = _seed_lib(db, "lib-rihan", "日韩电影", "7", bucket="rihan")
    http = FakeHttpGet()
    factory = sessionmaker(bind=db.get_bind())
    results = als.run_library_sync_round(
        session_factory=factory, http_get=http)
    by_lib = {r["library_id"]: r for r in results}
    assert by_lib[rihan.id]["added"] == 2       # 日本片 + 同片
    assert by_lib[total.id]["added"] == 2       # 只剩甲片（美国）+ 失败片
    assert by_lib[total.id]["skipped_other_library"] == 2
    owners = {r.name: r.library_id for r in
              db.query(em.MediaItem).all()}
    assert owners["日本片"] == rihan.id
    assert owners["甲片"] == total.id


# ---------------------------------------------------------------------------
# 存量迁移例程：干跑只计数，真跑只改 library_id
# ---------------------------------------------------------------------------

def _seed_stock(db):
    _seed_source(db, 7, "s7.example")
    total = _seed_lib(db, "lib-total", "在线电影", "7")
    oumei = _seed_lib(db, "lib-oumei", "欧美电影", "7", bucket="oumei")
    rows = {}
    for vod, name, year in (("a1", "美国片", "2021"),
                            ("a2", "印度片", "2021"),
                            ("a3", "消失片", "2021")):
        row = em.MediaItem(
            guid=f"hosted-7-{vod}", library_id=total.id,
            item_type="movie", name=name, production_year=int(year),
            upstream_source_id=7, probe_status="done")
        db.add(row)
        db.flush()
        db.add(em.UpstreamItemMapping(
            local_item_id=row.id, source_id=7, upstream_item_id=vod,
            confidence=0.7, evidence="title+year"))
        rows[vod] = row
    db.commit()
    return total, oumei, rows


def test_migration_dry_run_counts_only(db):
    total, oumei, rows = _seed_stock(db)
    http = FakeHttpGet()
    http.details["s7.example"] = {
        "a1": _detail("a1", "美国片", area="美国"),
        "a2": _detail("a2", "印度片", area="印度"),
        "a3": "HTTP500",
    }
    result = aam.run_area_migration(
        db, from_library_id=total.id, targets={"oumei": oumei.id},
        apply=False, http_get=http)
    assert result["buckets"] == {"oumei": 1}
    assert result["other"] == 1
    assert result["unresolved"] == 1
    # 干跑不动行
    assert all(r.library_id == total.id for r in rows.values())


def test_migration_apply_moves_only_bucketed_rows(db):
    total, oumei, rows = _seed_stock(db)
    http = FakeHttpGet()
    http.details["s7.example"] = {
        "a1": _detail("a1", "美国片", area="美国"),
        "a2": _detail("a2", "印度片", area="印度"),
        "a3": "HTTP500",
    }
    aam.run_area_migration(
        db, from_library_id=total.id, targets={"oumei": oumei.id},
        apply=False, http_get=http)
    calls = len(http.detail_urls())
    result = aam.run_area_migration(
        db, from_library_id=total.id, targets={"oumei": oumei.id},
        apply=True, http_get=http)
    assert result["buckets"] == {"oumei": 1}
    # 真跑命中干跑留下的缓存：a1/a2 不再重复打 detail（a3 失败不缓存）
    assert len(http.detail_urls()) == calls + 1
    db.refresh(rows["a1"])
    db.refresh(rows["a2"])
    assert rows["a1"].library_id == oumei.id
    assert rows["a1"].guid == "hosted-7-a1"     # guid 不动
    assert rows["a2"].library_id == total.id    # 其他地区留总库


def _seed_stock_bulk(db, specs):
    """按 specs=[(vod, area 或特殊标记)] 批量种存量行。

    area 为 "HTTP500" 表示 detail 失败（unresolved）；vod 以
    "nomap-" 开头表示不建映射行（no_mapping）；其余按 area 判桶。
    返回 (total, oumei, rows_by_vod)。
    """
    _seed_source(db, 7, "s7.example")
    total = _seed_lib(db, "lib-total", "在线电影", "7")
    oumei = _seed_lib(db, "lib-oumei", "欧美电影", "7", bucket="oumei")
    details = {}
    rows = {}
    for vod, area in specs:
        row = em.MediaItem(
            guid=f"hosted-7-{vod}", library_id=total.id,
            item_type="movie", name=f"片-{vod}",
            production_year=2021, upstream_source_id=7,
            probe_status="done")
        db.add(row)
        db.flush()
        rows[vod] = row
        if not vod.startswith("nomap-"):
            db.add(em.UpstreamItemMapping(
                local_item_id=row.id, source_id=7, upstream_item_id=vod,
                confidence=0.7, evidence="title+year"))
        if area == "HTTP500":
            details[vod] = "HTTP500"
        elif not vod.startswith("nomap-"):
            details[vod] = _detail(vod, f"片-{vod}", area=area)
    db.commit()
    http = FakeHttpGet()
    http.details["s7.example"] = details
    return total, oumei, rows, http


def test_migration_loads_rows_in_chunks_not_all_at_once(db, monkeypatch):
    """分页回归：现规模下不许再一次性 .all() 全量载入。

    把分页大小压到 2、种 5 行，监听主表（emby_items）扫描语句：
    分块实现至少发 3 次主表查询；旧的全量实现只发 1 次。
    """
    from sqlalchemy import event

    specs = [(f"b{i}", "美国") for i in range(5)]
    total, oumei, rows, http = _seed_stock_bulk(db, specs)
    monkeypatch.setattr(aam, "_PAGE_SIZE", 2, raising=False)

    engine = db.get_bind()
    statements = []

    def _capture(conn, cursor, statement, parameters, context, executemany):
        if "FROM emby_items" in statement:
            statements.append(statement)

    event.listen(engine, "before_cursor_execute", _capture)
    try:
        result = aam.run_area_migration(
            db, from_library_id=total.id, targets={"oumei": oumei.id},
            apply=True, http_get=http)
    finally:
        event.remove(engine, "before_cursor_execute", _capture)

    assert result["scanned"] == 5
    assert result["moved"] == 5
    assert result["buckets"] == {"oumei": 5}
    assert len(statements) >= 3, (
        f"主表扫描只发了 {len(statements)} 次查询，仍是全量载入")
    for row in rows.values():
        db.refresh(row)
        assert row.library_id == oumei.id


def test_migration_paginated_result_matches_unpaginated_semantics(
        db, monkeypatch):
    """分页等价：跨页混合口径（迁/其他/未知/失败/无映射）逐行对账。

    7 行、页大小 2（跨 4 页）：计数 dict 与每行最终归属必须与
    旧全量实现口径逐字一致，一行不多、一行不少。
    """
    specs = [
        ("m1", "美国"),        # oumei → 迁
        ("m2", "印度"),        # other → 留
        ("m3", "美国"),        # oumei → 迁
        ("m4", ""),            # unknown → 留
        ("m5", "HTTP500"),     # unresolved → 留
        ("nomap-6", None),     # no_mapping → 留
        ("m7", "美国"),        # oumei → 迁（末页）
    ]
    total, oumei, rows, http = _seed_stock_bulk(db, specs)
    monkeypatch.setattr(aam, "_PAGE_SIZE", 2, raising=False)

    result = aam.run_area_migration(
        db, from_library_id=total.id, targets={"oumei": oumei.id},
        apply=True, http_get=http)

    assert result["scanned"] == 7
    assert result["moved"] == 3
    assert result["buckets"] == {"oumei": 3}
    assert result["other"] == 1
    assert result["unknown"] == 1
    assert result["unresolved"] == 1
    assert result["no_mapping"] == 1
    assert result["no_source"] == 0
    expected = {
        "m1": oumei.id, "m2": total.id, "m3": oumei.id,
        "m4": total.id, "m5": total.id, "nomap-6": total.id,
        "m7": oumei.id,
    }
    for vod, want_lib in expected.items():
        db.refresh(rows[vod])
        assert rows[vod].library_id == want_lib, vod
        assert rows[vod].guid == f"hosted-7-{vod}"  # guid 不动


# ---------------------------------------------------------------------------
# 管理端 API：建库/改库收发地区桶
# ---------------------------------------------------------------------------

@pytest.fixture()
def client():
    engine = create_engine("sqlite://",
                           connect_args={"check_same_thread": False},
                           poolclass=StaticPool)
    base_models.Base.metadata.create_all(engine)
    em.Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    staff = base_models.WebUser(username="boss", password_hash="x",
                                is_staff=True)
    session.add(staff)
    src = em.UpstreamSource(
        name="非凡资源", source_type="applecms",
        base_url="https://cms.example.invalid", auth_method="api_key",
        enabled=True)
    session.add(src)
    session.commit()
    session.refresh(src)
    def _get_db():
        yield session

    app = FastAPI()
    app.include_router(portal.admin_emby_router)
    app.dependency_overrides[portal.require_staff] = lambda: staff
    app.dependency_overrides[portal.get_db] = _get_db
    store.invalidate()
    with TestClient(app) as c:
        yield c, session, src.id
    store.invalidate()
    session.close()


def test_api_create_library_with_area_bucket(client):
    c, db, src_id = client
    res = c.post("/api/admin/emby/libraries", json={
        "name": "华语电影", "collection_type": "movies",
        "kind": "applecms", "upstream_source_ids": [src_id],
        "cms_area_bucket": "huayu",
    })
    assert res.status_code == 200, res.text
    lib = db.query(em.Library).filter(
        em.Library.id == res.json()["id"]).first()
    assert lib.cms_area_bucket == "huayu"
    row = c.get("/api/admin/emby/libraries").json()["libraries"][0]
    assert row["cms_area_bucket"] == "huayu"


def test_api_create_library_rejects_bad_bucket(client):
    c, _db, src_id = client
    res = c.post("/api/admin/emby/libraries", json={
        "name": "乱桶库", "collection_type": "movies",
        "kind": "applecms", "upstream_source_ids": [src_id],
        "cms_area_bucket": "dongya",
    })
    assert res.status_code == 400


def test_api_update_library_bucket_set_and_clear(client):
    c, db, src_id = client
    res = c.post("/api/admin/emby/libraries", json={
        "name": "在线电影", "collection_type": "movies",
        "kind": "applecms", "upstream_source_ids": [src_id],
    })
    lib_id = res.json()["id"]
    lib = db.query(em.Library).filter(em.Library.id == lib_id).first()
    assert lib.cms_area_bucket is None          # 缺省 = 不分地区
    res = c.put(f"/api/admin/emby/libraries/{lib_id}",
                json={"cms_area_bucket": "oumei"})
    assert res.status_code == 200, res.text
    db.refresh(lib)
    assert lib.cms_area_bucket == "oumei"
    res = c.put(f"/api/admin/emby/libraries/{lib_id}",
                json={"cms_area_bucket": None})
    assert res.status_code == 200, res.text
    db.refresh(lib)
    assert lib.cms_area_bucket is None          # 显式 null = 清空
    res = c.put(f"/api/admin/emby/libraries/{lib_id}",
                json={"cms_area_bucket": "xxx"})
    assert res.status_code == 400              # 非法桶改库也挡
