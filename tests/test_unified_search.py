# -*- coding: utf-8 -*-
"""跨服统一搜索（第二批第 1 单）测试。

只测纯合并 + 上游现查的注入式传输，不发真实 HTTP、不碰数据库、
不写库、不触发聚合。本地 Python 3.12 + pyshim 口径，全量以
GitHub CI（Python 3.11）为准。
"""

import os

os.environ.setdefault("DATABASE_TYPE", "sqlite")
os.environ.setdefault("REDIS_ENABLED", "false")

from backend.emby_server import unified_search as us
from backend.emby_server import upstream_aggregation as agg


def _record(source, item_id, name="肖申克的救赎", tmdb="278", year=1994,
            item_type="Movie"):
    return agg.parse_emby_item(
        {"Id": item_id, "Name": name, "Type": item_type,
         "ProductionYear": year, "ProviderIds": {"Tmdb": tmdb}},
        source_id=source)


def _local(key, name="肖申克的救赎"):
    return us.LocalSearchEntry(aggregate_key=key, name=name,
                               item_type="movie", production_year=1994)


def test_local_first_and_cross_source_dedup_keeps_also_on():
    local = [_local("tmdb:278")]
    per_source = {
        "1": [_record("1", "a", tmdb="278")],
        "2": [_record("2", "b", tmdb="278"),
              _record("2", "c", name="阿甘正传", tmdb="329", year=1994)],
    }
    merged = us.merge_search_results(local, per_source, "肖申克")
    # 本地在前；同键上游不另起一行，只追加 also_on_sources。
    assert merged.results[0].origin == "local"
    assert merged.results[0].also_on_sources == ("1", "2")
    # “阿甘正传”不在查询词命中范围内（词是“肖申克”），不应出现；
    # 换词验证上游在后、按 aggregate_key 排序。
    merged2 = us.merge_search_results(
        [], {"2": [_record("2", "c", name="阿甘正传", tmdb="329", year=1994),
                   _record("2", "b", name="肖申克的救赎", tmdb="278")]},
        "阿甘")
    assert [r.aggregate_key for r in merged2.results] == ["tmdb:329"]
    assert merged2.results[0].origin == "upstream:2"


def test_upstream_only_sorted_by_aggregate_key_not_input_order():
    per_source = {
        "1": [_record("1", "z", name="肖申克外传", tmdb="999", year=1994),
              _record("1", "a", name="肖申克的救赎", tmdb="278", year=1994)],
    }
    merged = us.merge_search_results([], per_source, "肖申克")
    assert [r.aggregate_key for r in merged.results] == ["tmdb:278", "tmdb:999"]


def test_single_source_failure_does_not_collapse_whole_search():
    per_source = {
        "1": Exception("boom"),
        "2": [_record("2", "b", tmdb="278")],
        "3": "applecms 型源不走 Emby 搜索口",
    }
    merged = us.merge_search_results([], per_source, "肖申克")
    assert [r.aggregate_key for r in merged.results] == ["tmdb:278"]
    by_id = {s.source_id: s for s in merged.sources}
    assert by_id["1"].status == "unavailable"
    assert by_id["2"].status == "ok" and by_id["2"].count == 1
    assert by_id["3"].status == "unavailable"
    body = us.to_response_body(merged, limit=10)
    assert body["total"] == 1
    assert body["sources"][0]["error"] is not None


def test_source_filter_keeps_only_requested_source_and_marks_rest():
    per_source = {
        "1": [_record("1", "a", tmdb="278")],
        "2": [_record("2", "b", tmdb="278")],
    }
    merged = us.merge_search_results(
        [], per_source, "肖申克", source_filter={"2"})
    assert merged.results[0].source == "2"
    assert merged.results[0].also_on_sources == ()
    by_id = {s.source_id: s for s in merged.sources}
    assert by_id["1"].status == "not_requested"
    assert by_id["2"].status == "ok"


def test_empty_query_returns_nothing_not_whole_catalog():
    merged = us.merge_search_results(
        [_local("tmdb:278")],
        {"1": [_record("1", "a", tmdb="278")]}, "   ")
    assert merged.results == ()
    assert merged.query == ""


def test_switch_off_by_default(monkeypatch):
    monkeypatch.delenv("UNIFIED_SEARCH_ENABLED", raising=False)
    assert us.unified_search_enabled() is False
    monkeypatch.setenv("UNIFIED_SEARCH_ENABLED", "1")
    assert us.unified_search_enabled() is True


def test_search_upstream_source_maps_403_frontend_blocked():
    class Source:
        id = 1
        base_url = "https://example.invalid/emby"
        source_type = "emby"
        user_agent = None
        referer = None
        extra_headers_json = "{}"

    def fake_get(url, headers, timeout):
        assert "SearchTerm" in url
        return 403, None  # nginx HTML 被传输层吞成 None → 前门拦截

    def fake_resolve(source, http_post=None):
        return None  # 匿名

    import backend.emby_server.upstream_aggregate_run as run
    # 直接替换模块级解析函数后复原（只 patch 模块属性，不 patch 实例）。
    orig_resolve = run._resolve_api_key
    run._resolve_api_key = fake_resolve
    try:
        records, error = us.search_upstream_source(
            Source(), "肖申克", http_get=fake_get)
    finally:
        run._resolve_api_key = orig_resolve
    assert records == []
    assert error is not None and error.startswith("upstream_frontend_blocked")
    assert "api_key" not in error


def test_search_upstream_source_200_parses_and_filters_by_term():
    class Source:
        id = 7
        base_url = "https://example.invalid/emby"
        source_type = "emby"
        user_agent = None
        referer = None
        extra_headers_json = "{}"

    def fake_get(url, headers, timeout):
        return 200, {"Items": [
            {"Id": "a", "Name": "肖申克的救赎", "Type": "Movie",
             "ProductionYear": 1994, "ProviderIds": {"Tmdb": "278"}},
            {"Id": "b", "Name": "阿甘正传", "Type": "Movie",
             "ProductionYear": 1994, "ProviderIds": {"Tmdb": "329"}},
        ], "TotalRecordCount": 2}

    import backend.emby_server.upstream_aggregate_run as run
    orig_resolve = run._resolve_api_key
    run._resolve_api_key = lambda source, http_post=None: None
    try:
        records, error = us.search_upstream_source(
            Source(), "肖申克", http_get=fake_get)
    finally:
        run._resolve_api_key = orig_resolve
    assert error is None
    assert [r.upstream_item_id for r in records] == ["a"]


# ---- R4 审查修复回归 ----

def test_exception_error_is_fixed_code_not_raw_text():
    # 模块头凭据纪律：异常原文可能含带 api_key 的上游 URL，绝不进 error 体。
    merged = us.merge_search_results(
        [], {"1": Exception("https://upstream/x?api_key=SECRET123")}, "肖申克")
    body = us.to_response_body(merged, limit=10)
    assert body["sources"][0]["status"] == "unavailable"
    assert body["sources"][0]["error"] == "upstream_unavailable"
    assert "SECRET123" not in str(body)


def test_none_placeholder_source_marks_not_requested():
    # 调用方约定：被滤源以 None 占位进 per_source（不发 HTTP），merge 须
    # 在 inspect 值之前先标 not_requested。
    merged = us.merge_search_results(
        [], {"1": None, "2": [_record("2", "b", tmdb="278")]},
        "肖申克", source_filter={"2"})
    by_id = {s.source_id: s for s in merged.sources}
    assert by_id["1"].status == "not_requested"
    assert by_id["2"].status == "ok"


def test_local_key_prefers_production_year_over_scan_year():
    # 本地键年份须与上游 production_year 同口径；scan_year 优先会让
    # 无外部身份的同一作品在两边算出不同的标题+年份键，去重 miss。
    key = us.local_aggregate_key(
        name="无身份片", item_type="movie",
        production_year=2021, scan_year=2020,
        tmdb_id=None, imdb_id=None,
        season_number=None, episode_number=None)
    upstream = agg.parse_emby_item(
        {"Id": "u", "Name": "无身份片", "Type": "Movie",
         "ProductionYear": 2021, "ProviderIds": {}},
        source_id="1")
    assert key == agg.aggregate_key_for(upstream)
    merged = us.merge_search_results(
        [us.LocalSearchEntry(aggregate_key=key, name="无身份片",
                             item_type="movie", production_year=2021)],
        {"1": [upstream]}, "无身份片")
    assert len(merged.results) == 1
    assert merged.results[0].origin == "local"
    assert merged.results[0].also_on_sources == ("1",)


def test_local_key_falls_back_to_scan_year_when_no_production_year():
    key = us.local_aggregate_key(
        name="无身份片", item_type="movie",
        production_year=None, scan_year=2020,
        tmdb_id=None, imdb_id=None,
        season_number=None, episode_number=None)
    assert key == "title:无身份片:2020"


def test_route_filtered_source_reported_not_requested(monkeypatch):
    # 端到端（路由层）：被滤源须出现在 sources 里标 not_requested，
    # 而不是被调用方 continue 掉直接消失。
    monkeypatch.setenv("UNIFIED_SEARCH_ENABLED", "1")
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from sqlalchemy import create_engine as _ce
    from sqlalchemy.orm import sessionmaker as _sm
    from sqlalchemy.pool import StaticPool as _SP
    from backend.emby_server import models as _em
    from backend.emby_server import search_api as _api
    from backend.emby_server.auth import get_emby_user as _geu
    from backend.database import get_db as _gdb
    from backend import library_scope as _ls

    engine = _ce("sqlite://", connect_args={"check_same_thread": False},
                 poolclass=_SP)
    _em.MediaItem.__table__.create(engine, checkfirst=True)
    _em.UpstreamSource.__table__.create(engine, checkfirst=True)
    db = _sm(bind=engine)()
    db.add(_em.MediaItem(id=1, guid="00000000000000000000000000000001",
                         library_id=1, item_type="movie", name="肖申克的救赎",
                         production_year=1994, sort_name="肖申克的救赎",
                         is_hidden=False))
    db.add(_em.UpstreamSource(id=1, name="src-apple", source_type="applecms",
                              base_url="https://example.invalid",
                              auth_method="password", username="u",
                              enabled=True))
    db.add(_em.UpstreamSource(id=2, name="src-emby", source_type="emby",
                              base_url="https://example.invalid",
                              auth_method="password", username="u",
                              enabled=True))
    db.commit()
    monkeypatch.setattr(_ls, "effective_ids_safe", lambda d, u: {1})
    monkeypatch.setattr(
        us, "search_upstream_source",
        lambda source, term: ([_record("2", "b", tmdb="278")], None))
    app = FastAPI()
    app.include_router(_api.search_router)
    app.dependency_overrides[_geu] = lambda: type(
        "U", (), {"id": 1, "is_staff": False})()
    app.dependency_overrides[_gdb] = lambda: db
    client = TestClient(app)
    r = client.get("/emby/UnifiedSearch",
                   params={"SearchTerm": "肖申克", "source": "2"})
    assert r.status_code == 200, r.text
    body = r.json()
    by_id = {s["source_id"]: s for s in body["sources"]}
    assert by_id["1"]["status"] == "not_requested"
    assert by_id["2"]["status"] == "ok"
