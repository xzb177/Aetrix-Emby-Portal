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
