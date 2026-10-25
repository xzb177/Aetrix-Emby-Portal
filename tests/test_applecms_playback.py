# -*- coding: utf-8 -*-
"""苹果 CMS 播放分支测试（全离线）。

- 源行用桩（SimpleNamespace）并 monkeypatch ``us.get_source``：
  源类型允许集目前只有 emby/jellyfin（模型 CHECK 约束同款），
  苹果 CMS 源行尚建不出来，分支代码与测试先用桩源就绪，
  等允许集放行即接通（见 upstream_playback 模块 docstring 缺口）。
- detail 抓取与探活一律用桩注入（http_get / http_probe），不联网；
  夹具地址均为 example.com 占位域名。
"""
import os
from types import SimpleNamespace

import pytest

from backend.emby_server import upstream_playback as up
from backend.emby_server import upstream_sources as us

BASE = "https://cms.example.com"
TOTAL = "UPSTREAM_PLAYBACK_ENABLED"

SERIES_DETAIL = {
    "code": 1, "page": 1, "pagecount": 1, "list": [{
        "vod_id": 202, "vod_play_from": "直链线路$$$解析线路",
        "vod_play_url": "第01集$https://cdn.example.com/ep1.mp4"
                      "#第02集$https://cdn.example.com/ep2.mp4?token=x"
                      "$$$第01集$playtoken202"}]}

PARSE_ONLY_DETAIL = {
    "code": 1, "page": 1, "pagecount": 1, "list": [{
        "vod_id": 203, "vod_play_from": "解析线路",
        "vod_play_url": "第01集$playtoken203"}]}

NO_PLAY_DETAIL = {
    "code": 1, "page": 1, "pagecount": 1, "list": [{
        "vod_id": 204, "vod_play_from": "", "vod_play_url": ""}]}


def _source(**kw):
    row = SimpleNamespace(
        id=7, source_type="applecms", base_url=BASE, enabled=True,
        user_agent=None, referer=None, extra_headers_json=None)
    for key, value in kw.items():
        setattr(row, key, value)
    return row


@pytest.fixture()
def cms_source(monkeypatch):
    row = _source()
    monkeypatch.setattr(us, "get_source", lambda db, sid: row)
    return row


def _detail_get(payload, captured=None):
    def http_get(url, headers, timeout):
        if captured is not None:
            captured.append(url)
        return 200, payload
    return http_get


def _probe_ok(captured=None):
    def probe(url, headers, timeout):
        if captured is not None:
            captured.append(url)
        return 206, None
    return probe


def _probe_status(status, retry_after=None):
    def probe(url, headers, timeout):
        return status, retry_after
    return probe


def _env_on(monkeypatch):
    monkeypatch.setenv(TOTAL, "1")
    monkeypatch.setenv("APPLECMS_ENABLED", "1")
    monkeypatch.delenv("APPLECMS_TIER_GATE", raising=False)
    monkeypatch.delenv("APPLECMS_PARSE_LINES_ENABLED", raising=False)


def test_switch_defaults_off(monkeypatch):
    for key in ("APPLECMS_ENABLED", "APPLECMS_PARSE_LINES_ENABLED",
                "APPLECMS_TIER_GATE", "UPSTREAM_SYNC_ENABLED"):
        monkeypatch.delenv(key, raising=False)
    assert up.applecms_enabled() is False
    assert up.applecms_parse_lines_enabled() is False
    assert up.applecms_tier_gate_enabled() is False
    assert up.applecms_sync_enabled() is False
    assert up.applecms_tier_configured() is False


def test_total_gate_off_blocks_applecms(monkeypatch, cms_source):
    monkeypatch.setenv(TOTAL, "0")
    monkeypatch.setenv("APPLECMS_ENABLED", "1")
    with pytest.raises(up.UpstreamPlaybackError) as exc:
        up.resolve_upstream_stream(None, 7, "202")
    assert (exc.value.status, exc.value.code) == (
        404, "upstream_playback_disabled")


def test_applecms_gate_off_matches_closed_behavior(monkeypatch, cms_source):
    monkeypatch.setenv(TOTAL, "1")
    monkeypatch.setenv("APPLECMS_ENABLED", "0")
    captured = []
    with pytest.raises(up.UpstreamPlaybackError) as exc:
        up.resolve_upstream_stream(
            None, 7, "202",
            http_get=_detail_get(SERIES_DETAIL, captured))
    assert (exc.value.status, exc.value.code) == (
        404, "upstream_playback_disabled")
    assert captured == []  # 关着就不发任何上游请求


def test_disabled_source_rejected_before_branch(monkeypatch):
    monkeypatch.setattr(us, "get_source",
                        lambda db, sid: _source(enabled=False))
    _env_on(monkeypatch)
    with pytest.raises(up.UpstreamPlaybackError) as exc:
        up.resolve_upstream_stream(None, 7, "202")
    assert (exc.value.status, exc.value.code) == (
        409, "upstream_source_disabled")


def test_direct_group_selected_and_episode_address(monkeypatch, cms_source):
    _env_on(monkeypatch)
    captured = []
    url = up.resolve_upstream_stream(
        None, 7, "202", episode_index=2,
        http_get=_detail_get(SERIES_DETAIL), http_probe=_probe_ok(captured))
    assert url == "https://cdn.example.com/ep2.mp4?token=x"
    assert captured == [url]
    assert "playtoken" not in url  # 解析组没被选中


def test_episode_defaults_to_first(monkeypatch, cms_source):
    """播放口形态：episode_index 未传时默认 1（播第 1 集）。

    HTTP 层（api.py upstream_stream 路由）目前未传 episode 参数，
    接线时由路由把 ?episode= 透传给本函数；未接线前一律播第 1 集。
    """
    _env_on(monkeypatch)
    url = up.resolve_upstream_stream(
        None, 7, "202",
        http_get=_detail_get(SERIES_DETAIL), http_probe=_probe_ok())
    assert url == "https://cdn.example.com/ep1.mp4"


def test_episode_out_of_range(monkeypatch, cms_source):
    _env_on(monkeypatch)
    with pytest.raises(up.UpstreamPlaybackError) as exc:
        up.resolve_upstream_stream(
            None, 7, "202", episode_index=9,
            http_get=_detail_get(SERIES_DETAIL), http_probe=_probe_ok())
    assert (exc.value.status, exc.value.code) == (
        404, "upstream_item_not_found")


def test_parse_only_unplayable_reasons(monkeypatch, cms_source):
    _env_on(monkeypatch)
    with pytest.raises(up.UpstreamPlaybackError) as exc:
        up.resolve_upstream_stream(
            None, 7, "203",
            http_get=_detail_get(PARSE_ONLY_DETAIL), http_probe=_probe_ok())
    assert exc.value.status == 502
    assert exc.value.code == "upstream_unavailable"
    assert "解析型线路未开启" in exc.value.message

    monkeypatch.setenv("APPLECMS_PARSE_LINES_ENABLED", "1")
    with pytest.raises(up.UpstreamPlaybackError) as exc:
        up.resolve_upstream_stream(
            None, 7, "203",
            http_get=_detail_get(PARSE_ONLY_DETAIL), http_probe=_probe_ok())
    assert exc.value.status == 502
    assert "本站无解析规则" in exc.value.message


def test_no_direct_group_unplayable(monkeypatch, cms_source):
    _env_on(monkeypatch)
    with pytest.raises(up.UpstreamPlaybackError) as exc:
        up.resolve_upstream_stream(
            None, 7, "204",
            http_get=_detail_get(NO_PLAY_DETAIL), http_probe=_probe_ok())
    assert (exc.value.status, exc.value.code) == (
        502, "upstream_unavailable")


def test_tier_gate_on_unconfigured_returns_403(monkeypatch, cms_source):
    """分级 403 复用结论：文件里唯一的 403 码是 upstream_forbidden，
    本批复用它（标准语义「上游拒绝」，此处 message 区分成本地
    分级未配置；若协调员要单独立码，换 _resolve_applecms_stream
    一处即可）。"""
    _env_on(monkeypatch)
    monkeypatch.setenv("APPLECMS_TIER_GATE", "1")
    captured = []
    with pytest.raises(up.UpstreamPlaybackError) as exc:
        up.resolve_upstream_stream(
            None, 7, "202",
            http_get=_detail_get(SERIES_DETAIL, captured),
            http_probe=_probe_ok())
    assert exc.value.status == 403
    assert exc.value.code == "upstream_forbidden"
    assert "分级映射未配置" in exc.value.message
    assert captured == []  # 门控在取数前就拦，不发上游请求


def test_tier_gate_off_skips_check(monkeypatch, cms_source):
    _env_on(monkeypatch)
    url = up.resolve_upstream_stream(
        None, 7, "202",
        http_get=_detail_get(SERIES_DETAIL), http_probe=_probe_ok())
    assert url == "https://cdn.example.com/ep1.mp4"


def test_cms_error_mapping(monkeypatch, cms_source):
    _env_on(monkeypatch)
    cases = [
        (lambda: _detail_get({"code": 0, "list": []}),
         502, "upstream_unavailable"),
        (lambda url, headers, timeout: (429, {}), 429,
         "upstream_quota_exhausted"),
        (lambda url, headers, timeout: (401, {}), 409,
         "upstream_credential_invalid"),
        (lambda url, headers, timeout: (403, {}), 403,
         "upstream_forbidden"),
        (lambda url, headers, timeout: (None, None), 502,
         "upstream_unavailable"),
    ]
    for http_get, status, code in cases:
        with pytest.raises(up.UpstreamPlaybackError) as exc:
            up.resolve_upstream_stream(
                None, 7, "202", http_get=http_get, http_probe=_probe_ok())
        assert (exc.value.status, exc.value.code) == (status, code)
    # detail 空列表 = vod 消失 → 404
    with pytest.raises(up.UpstreamPlaybackError) as exc:
        up.resolve_upstream_stream(
            None, 7, "999",
            http_get=_detail_get({"code": 1, "list": []}),
            http_probe=_probe_ok())
    assert (exc.value.status, exc.value.code) == (
        404, "upstream_item_not_found")


def test_probe_failure_classified(monkeypatch, cms_source):
    _env_on(monkeypatch)
    with pytest.raises(up.UpstreamPlaybackError) as exc:
        up.resolve_upstream_stream(
            None, 7, "202",
            http_get=_detail_get(SERIES_DETAIL),
            http_probe=_probe_status(404))
    assert (exc.value.status, exc.value.code) == (
        404, "upstream_item_not_found")
    with pytest.raises(up.UpstreamPlaybackError) as exc:
        up.resolve_upstream_stream(
            None, 7, "202",
            http_get=_detail_get(SERIES_DETAIL),
            http_probe=_probe_status(429, retry_after=60))
    assert exc.value.status == 429
    assert exc.value.retry_after == 60


def test_error_body_contains_no_playback_url(monkeypatch, cms_source):
    _env_on(monkeypatch)
    with pytest.raises(up.UpstreamPlaybackError) as exc:
        up.resolve_upstream_stream(
            None, 7, "203",
            http_get=_detail_get(PARSE_ONLY_DETAIL), http_probe=_probe_ok())
    body = exc.value.to_body()
    assert "cdn.example.com" not in str(body)
    assert "playtoken" not in str(body)
    assert set(body) <= {"code", "message", "source_id", "retry_after"}
