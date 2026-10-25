# -*- coding: utf-8 -*-
"""苹果 CMS 抓取模块测试（全离线：http_get 一律用桩，不真实联网）。

夹具用占位域名（example.com），不是任何真实资源站地址。
"""
import json
import os

import pytest

from backend.emby_server import applecms_fetcher as acf

FIXTURES = os.path.join(os.path.dirname(__file__), "fixtures")
BASE = "https://cms.example.com"


def _load(name):
    with open(os.path.join(FIXTURES, name), encoding="utf-8") as fh:
        return json.load(fh)


def _stub(payload, captured=None, status=200):
    def http_get(url, headers, timeout):
        if captured is not None:
            captured.append(url)
        return status, payload
    return http_get


def _movie_vod():
    return _load("applecms_list_movie.json")["list"][0]


def _series_vod():
    return _load("applecms_list_series.json")["list"][0]


# ---------------- fetch_page ----------------

def test_fetch_page_list_movie():
    captured = []
    payload = acf.fetch_page(BASE, ac="list", pg=1,
                             http_get=_stub(_load("applecms_list_movie.json"),
                                            captured))
    assert payload["code"] == 1
    assert payload["list"][0]["vod_id"] == 101
    assert "ac=list" in captured[0] and "pg=1" in captured[0]
    assert captured[0].startswith(BASE + "/api.php/provide/vod/")


def test_fetch_page_invalid_ac_rejected():
    with pytest.raises(ValueError):
        acf.fetch_page(BASE, ac="bogus",
                       http_get=_stub(_load("applecms_list_movie.json")))


def test_fetch_page_code_not_one_distinguished():
    with pytest.raises(acf.AppleCMSError) as exc:
        acf.fetch_page(BASE, http_get=_stub(
            _load("applecms_code_error.json")))
    assert exc.value.kind == "bad_code"
    assert exc.value.cms_code == 0


def test_fetch_page_429_distinguished():
    with pytest.raises(acf.AppleCMSError) as exc:
        acf.fetch_page(BASE, http_get=_stub({}, status=429))
    assert exc.value.kind == "rate_limited"
    assert exc.value.http_status == 429


def test_fetch_page_401_403_distinguished():
    for status in (401, 403):
        with pytest.raises(acf.AppleCMSError) as exc:
            acf.fetch_page(BASE, http_get=_stub({}, status=status))
        assert exc.value.kind == "unauthorized"
        assert exc.value.http_status == status


def test_fetch_page_unreachable_distinguished():
    def boom(url, headers, timeout):
        raise ConnectionError("down")

    with pytest.raises(acf.AppleCMSError) as exc:
        acf.fetch_page(BASE, http_get=boom)
    assert exc.value.kind == "network"
    with pytest.raises(acf.AppleCMSError) as exc:
        acf.fetch_page(BASE, http_get=_stub(None, status=None))
    assert exc.value.kind == "network"


def test_fetch_page_non_dict_body():
    with pytest.raises(acf.AppleCMSError) as exc:
        acf.fetch_page(BASE, http_get=_stub(["not", "a", "dict"]))
    assert exc.value.kind == "bad_response"


def test_fetch_page_error_carries_no_url():
    with pytest.raises(acf.AppleCMSError) as exc:
        acf.fetch_page(BASE, http_get=_stub(
            _load("applecms_code_error.json")))
    assert BASE not in str(exc.value)
    assert "cdn.example.com" not in str(exc.value)


# ---------------- iter_pages / fetch_detail ----------------

def test_iter_pages_stops_at_pagecount():
    pages = {
        1: {"code": 1, "page": 1, "pagecount": 2, "limit": 1,
            "list": [{"vod_id": 1}]},
        2: {"code": 1, "page": 2, "pagecount": 2, "limit": 1,
            "list": [{"vod_id": 2}]},
        3: {"code": 1, "page": 3, "pagecount": 2, "limit": 1,
            "list": [{"vod_id": 3}]},
    }
    seen_pages = []

    def http_get(url, headers, timeout):
        import urllib.parse
        pg = int(urllib.parse.parse_qs(
            urllib.parse.urlparse(url).query)["pg"][0])
        seen_pages.append(pg)
        return 200, pages[pg]

    items = list(acf.iter_pages(BASE, http_get=http_get))
    assert [item["vod_id"] for item in items] == [1, 2]
    assert seen_pages == [1, 2]


def test_iter_pages_stops_on_short_page():
    pages = {
        1: {"code": 1, "page": 1, "pagecount": 9, "limit": 20,
            "list": [{"vod_id": 1}]},
    }

    def http_get(url, headers, timeout):
        return 200, pages[1]

    assert [item["vod_id"] for item in
            acf.iter_pages(BASE, http_get=http_get)] == [1]


def test_fetch_detail_series_and_missing():
    detail = acf.fetch_detail(
        BASE, 202,
        http_get=_stub(_load("applecms_detail_series.json")))
    assert detail["vod_id"] == 202
    empty = {"code": 1, "page": 1, "pagecount": 0, "list": []}
    assert acf.fetch_detail(BASE, 999, http_get=_stub(empty)) is None
    with pytest.raises(ValueError):
        acf.fetch_detail(BASE, "  ", http_get=_stub(empty))


# ---------------- parse / classify / pick / episode ----------------

def test_parse_play_groups_movie_and_series():
    groups = acf.parse_play_groups(_movie_vod()["vod_play_from"],
                                   _movie_vod()["vod_play_url"])
    assert len(groups) == 1
    assert groups[0]["name"] == "线路A"
    assert groups[0]["episodes"] == [
        {"title": "正片", "address": "https://cdn.example.com/movie.m3u8"}]

    groups = acf.parse_play_groups(_series_vod()["vod_play_from"],
                                   _series_vod()["vod_play_url"])
    assert [g["name"] for g in groups] == ["直链线路", "解析线路"]
    assert len(groups[0]["episodes"]) == 2
    assert groups[0]["episodes"][1]["address"] == \
        "https://cdn.example.com/ep2.mp4?token=x"
    assert groups[1]["episodes"][0]["address"] == "playtoken202"


def test_parse_play_groups_from_empty_falls_back():
    vod = _load("applecms_from_empty.json")["list"][0]
    groups = acf.parse_play_groups(vod["vod_play_from"],
                                   vod["vod_play_url"])
    assert [g["name"] for g in groups] == ["线路1", "线路2"]
    groups = acf.parse_play_groups(None, None)
    assert groups == []


def test_classify_url_ignores_query_and_case():
    assert acf.classify_url("https://cdn.example.com/a.m3u8?x=1") == "direct"
    assert acf.classify_url("http://cdn.example.com/A.MP4") == "direct"
    assert acf.classify_url("https://cdn.example.com/a.ts") == "parse"
    assert acf.classify_url("playtoken202") == "parse"
    assert acf.classify_url("") == "unknown"
    assert acf.classify_url(None) == "unknown"


def test_pick_direct_group_first_all_direct():
    groups = acf.parse_play_groups(_series_vod()["vod_play_from"],
                                   _series_vod()["vod_play_url"])
    direct = acf.pick_direct_group(groups)
    assert direct["name"] == "直链线路"
    parse_only = [groups[1]]
    assert acf.pick_direct_group(parse_only) is None
    mixed = [{"name": "混合", "episodes": [
        {"title": "1", "address": "https://cdn.example.com/a.mp4"},
        {"title": "2", "address": "sometoken"}]}]
    assert acf.pick_direct_group(mixed) is None


def test_episode_address_one_based():
    groups = acf.parse_play_groups(_series_vod()["vod_play_from"],
                                   _series_vod()["vod_play_url"])
    assert acf.episode_address(groups[0], 1) == \
        "https://cdn.example.com/ep1.mp4"
    assert acf.episode_address(groups[0], 2) == \
        "https://cdn.example.com/ep2.mp4?token=x"
    assert acf.episode_address(groups[0], 0) is None
    assert acf.episode_address(groups[0], 3) is None
    assert acf.episode_address(groups[0], "nope") is None


# ---------------- resolve_play ----------------

def test_resolve_play_direct_and_out_of_range():
    vod = _series_vod()
    assert acf.resolve_play(vod, 2) == (
        "ok", "直链线路", "https://cdn.example.com/ep2.mp4?token=x")
    assert acf.resolve_play(vod, 3) == ("unplayable", "episode_out_of_range")
    assert acf.resolve_play(vod, 0) == ("unplayable", "episode_out_of_range")


def test_resolve_play_parse_only_reasons():
    vod = {"vod_play_from": "解析线路",
           "vod_play_url": "第01集$playtoken202"}
    assert acf.resolve_play(vod, 1) == ("unplayable", "parse_not_enabled")
    assert acf.resolve_play(vod, 1, parse_lines_enabled=True) == (
        "unplayable", "parse_rule_missing")


def test_resolve_play_no_direct_group():
    assert acf.resolve_play({}, 1) == ("unplayable", "no_direct_group")
    assert acf.resolve_play({"vod_play_from": "", "vod_play_url": ""},
                            1) == ("unplayable", "no_direct_group")
    assert acf.resolve_play(None, 1) == ("unplayable", "no_direct_group")
