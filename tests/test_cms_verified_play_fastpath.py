# -*- coding: utf-8 -*-
"""CMS 已验活地址快路回归（起播解析慢治理）。

生产实证（2026-11-05，emby_play_start_events 72 小时 + 容器内
组件实测）：
- 成功起播 resolve_ms p50 2864ms / p95 10657ms，大头是每播必做的
  同步探活——探活单跳直达 CDN，但 CDN 对服务器机房位置的首字节
  耗时实测 1.9~9.7 秒，detail 缓存省不掉这一步；
- 46% 的播放是同一条目 30 秒内的复播（450/970），复播照样全额
  再探一遍（复播 avg 3159ms）；
- 地域 403 族在缓存路径还会每播触发「淘汰 + 回源重取 detail」。

钉死口径：
- 同一映射（quality_mapping_id）刚下发过的地址，在窗口
  （APPLECMS_VERIFIED_PLAY_TTL_SEC，默认 300 秒）内复播、且该
  地址仍在本份 detail 的候选内 → 免探活直接下发同一个地址；
- 窗口关闭（TTL=0）/ 地址已不在候选 / 无映射 id（显式上游口）
  → 逐字回现行逐候选探活，行为不变；
- 下发地址集合、403 软放行、候选顺序口径一概不变：快路只省
  「刚验过的同一地址再验一遍」的等待，不换线、不跳过首次验活。
"""
from types import SimpleNamespace

import pytest

from backend.database import CacheManager
from backend.emby_server import upstream_playback as up
from backend.emby_server import upstream_sources as us

BASE = "https://cms.example.com"
TOTAL = "UPSTREAM_PLAYBACK_ENABLED"

def _detail(vod_id):
    return {"code": 1, "page": 1, "pagecount": 1, "list": [{
        "vod_id": vod_id, "vod_play_from": "组一$$$组二",
        "vod_play_url":
            "第01集$https://cdn.example.com/v1e1.m3u8"
            "$$$第01集$https://cdn.example.com/v2e1.m3u8"}]}


DETAIL = _detail(4242)

ADDR1 = "https://cdn.example.com/v1e1.m3u8"
ADDR2 = "https://cdn.example.com/v2e1.m3u8"


def _source(source_id=21):
    return SimpleNamespace(
        id=source_id, source_type="applecms", base_url=BASE, enabled=True,
        user_agent=None, referer=None, extra_headers_json=None)


@pytest.fixture(autouse=True)
def _clean_verified(monkeypatch):
    monkeypatch.setattr(up, "_capture_cms_quality_async",
                        lambda *a, **k: None)
    yield
    for mid in (91001, 91002, 91003):
        CacheManager.delete(up.cms_verified_play_key(mid))
    for vod in ("4242", "4343", "4444", "4545", "4646", "4747", "4848"):
        CacheManager.delete(up.cms_detail_cache_key(21, vod))


def _env_on(monkeypatch, detail_cache="1"):
    monkeypatch.setenv(TOTAL, "1")
    monkeypatch.setenv("APPLECMS_ENABLED", "1")
    monkeypatch.setenv("APPLECMS_DETAIL_CACHE_ENABLED", detail_cache)
    monkeypatch.delenv("APPLECMS_TIER_GATE", raising=False)
    monkeypatch.delenv("APPLECMS_PARSE_LINES_ENABLED", raising=False)
    monkeypatch.delenv("APPLECMS_VERIFIED_PLAY_TTL_SEC", raising=False)


def _counting_get(payload):
    calls = []

    def http_get(url, headers, timeout):
        calls.append(url)
        return 200, payload
    return http_get, calls


def _counting_probe(status_by_marker):
    calls = []

    def probe(url, headers, timeout):
        calls.append(url)
        for marker, status in status_by_marker.items():
            if marker in url:
                return status, None
        return 206, None
    return probe, calls


def _resolve(monkeypatch, http_get, http_probe, mapping_id=91001,
             vod="4242"):
    monkeypatch.setattr(us, "get_source", lambda db, sid: _source())
    return up.resolve_upstream_stream(
        None, 21, vod, episode_index=1,
        http_get=http_get, http_probe=http_probe,
        quality_mapping_id=mapping_id)


def test_second_resolve_within_window_skips_probe(monkeypatch):
    """同映射复播：第二发免探活（探活计数不变）、下发同一地址。"""
    _env_on(monkeypatch)
    http_get, _get_calls = _counting_get(DETAIL)
    probe, probe_calls = _counting_probe({})
    url1 = _resolve(monkeypatch, http_get, probe)
    assert url1 == ADDR1
    assert probe_calls == [ADDR1]
    url2 = _resolve(monkeypatch, http_get, probe)
    assert url2 == ADDR1
    assert probe_calls == [ADDR1]  # 第二发没有再探


def test_fastpath_skips_evict_refetch_dance_on_403_host(monkeypatch):
    """403 软放行下发过的地址：复播不再触发缓存淘汰 + 回源重取。"""
    _env_on(monkeypatch)
    http_get, get_calls = _counting_get(_detail(4343))
    probe, probe_calls = _counting_probe({"v1": 403, "v2": 403})
    url1 = _resolve(monkeypatch, http_get, probe, vod="4343")
    assert url1 == ADDR1  # 现取路径 403 软放行
    assert len(get_calls) == 1
    url2 = _resolve(monkeypatch, http_get, probe, vod="4343")
    assert url2 == ADDR1
    # 没有第二发 detail 网络请求，也没有第二轮探活
    assert len(get_calls) == 1
    assert probe_calls == [ADDR1]


def test_window_disabled_by_env_zero(monkeypatch):
    """TTL=0 关闭快路：复播照常探活（今日行为）。"""
    _env_on(monkeypatch)
    monkeypatch.setenv("APPLECMS_VERIFIED_PLAY_TTL_SEC", "0")
    http_get, _ = _counting_get(_detail(4444))
    probe, probe_calls = _counting_probe({})
    assert _resolve(monkeypatch, http_get, probe, vod="4444") == ADDR1
    assert _resolve(monkeypatch, http_get, probe, vod="4444") == ADDR1
    assert probe_calls == [ADDR1, ADDR1]


def test_verified_address_gone_from_candidates_falls_back(monkeypatch):
    """已验地址已不在本份 detail 候选内 → 回现行探活，不发旧地址。"""
    _env_on(monkeypatch, detail_cache="0")
    http_get, _ = _counting_get(_detail(4545))
    probe, probe_calls = _counting_probe({})
    assert _resolve(monkeypatch, http_get, probe, vod="4545") == ADDR1
    # detail 变了：组一地址换成新地址
    changed = {"code": 1, "page": 1, "pagecount": 1, "list": [{
        "vod_id": 4545, "vod_play_from": "组一$$$组二",
        "vod_play_url":
            "第01集$https://cdn.example.com/v1new.m3u8"
            "$$$第01集$https://cdn.example.com/v2e1.m3u8"}]}
    http_get2, _ = _counting_get(changed)
    url = _resolve(monkeypatch, http_get2, probe, vod="4545")
    assert url == "https://cdn.example.com/v1new.m3u8"
    assert probe_calls[-1] == "https://cdn.example.com/v1new.m3u8"


def test_no_mapping_id_never_fastpaths(monkeypatch):
    """显式上游口（无 quality_mapping_id）：不记快路、每发照常探活。"""
    _env_on(monkeypatch)
    http_get, _ = _counting_get(_detail(4646))
    probe, probe_calls = _counting_probe({})
    monkeypatch.setattr(us, "get_source", lambda db, sid: _source())
    for _ in range(2):
        url = up.resolve_upstream_stream(
            None, 21, "4646", episode_index=1,
            http_get=http_get, http_probe=probe)
        assert url == ADDR1
    assert probe_calls == [ADDR1, ADDR1]


def test_verified_not_shared_across_mappings(monkeypatch):
    """快路按映射隔离：别的映射复播不许蹭本映射的已验记录。"""
    _env_on(monkeypatch)
    http_get, _ = _counting_get(_detail(4747))
    probe, probe_calls = _counting_probe({})
    assert _resolve(monkeypatch, http_get, probe,
                    mapping_id=91002, vod="4747") == ADDR1
    assert _resolve(monkeypatch, http_get, probe,
                    mapping_id=91003, vod="4747") == ADDR1
    assert probe_calls == [ADDR1, ADDR1]


def test_winner_candidate_is_the_verified_one(monkeypatch):
    """首候选死、次候选赢时，快路记的是赢家地址，复播直发赢家。"""
    _env_on(monkeypatch)
    http_get, _ = _counting_get(_detail(4848))
    probe, probe_calls = _counting_probe({"v1": 404})
    assert _resolve(monkeypatch, http_get, probe, vod="4848") == ADDR2
    assert probe_calls == [ADDR1, ADDR2]
    assert _resolve(monkeypatch, http_get, probe, vod="4848") == ADDR2
    assert probe_calls == [ADDR1, ADDR2]  # 复播零探活
