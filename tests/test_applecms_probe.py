# -*- coding: utf-8 -*-
"""苹果 CMS 轻探活（FMBY #25 下一批唯一施工项）。

全部离线：HTTP 一律用桩替身；健康存储用内存替身；时间用注入的 now；
开关走 monkeypatch env，不依赖真实配置。所有验证均为 mock：
真实闭环待用户给 1 个试点 CMS 端点后才做，本文件不编造端点。
"""
import os

os.environ.setdefault("DATABASE_TYPE", "sqlite")
os.environ.setdefault("REDIS_ENABLED", "false")

import pytest

from backend.emby_server import applecms_probe as acp
from backend.emby_server import upstream_health as uh

NOW = 1_800_000_000.0


def _src(**kw):
    base = {"key": "7", "base_url": "http://cms.example.com"}
    base.update(kw)
    return acp.AppleCMSSource(**base)


def _stub(status, payload, calls=None):
    def http_get(url, headers, timeout):
        if calls is not None:
            calls.append((url, dict(headers), timeout))
        return status, payload
    return http_get


def _enable(monkeypatch):
    monkeypatch.setenv("APPLECMS_PROBE_ENABLED", "1")


def test_success_first_page_only(monkeypatch):
    _enable(monkeypatch)
    store = uh.InMemoryHealthStore()
    calls = []
    result = acp.probe_applecms_source(
        _src(), store,
        http_get=_stub(200, {"code": 1, "pagecount": 5,
                             "list": [{"vod_id": 1}]}, calls),
        now=NOW, interval_sec=0.0)
    assert result["outcome"] == "success"
    assert result["state_after"] == uh.STATE_ACTIVE
    assert len(calls) == 1  # 只取首页，不翻页
    assert "ac=list" in calls[0][0] and "pg=1" in calls[0][0]
    record = store.get_source("7")
    assert record.availability_state == uh.STATE_ACTIVE
    assert record.consecutive_unavailable_failures == 0
    assert record.last_success_at == NOW


def test_string_code_1_counts_as_success(monkeypatch):
    _enable(monkeypatch)
    store = uh.InMemoryHealthStore()
    result = acp.probe_applecms_source(
        _src(), store, http_get=_stub(200, {"code": "1", "list": []}),
        now=NOW, interval_sec=0.0)
    assert result["outcome"] == "success"


def test_trailing_slash_base_url_builds_clean_url(monkeypatch):
    _enable(monkeypatch)
    store = uh.InMemoryHealthStore()
    calls = []
    acp.probe_applecms_source(
        _src(base_url="http://cms.example.com/"), store,
        http_get=_stub(200, {"code": 1, "list": []}, calls),
        now=NOW, interval_sec=0.0)
    assert "cms.example.com/api.php/provide/vod/?ac=list" in calls[0][0]


def test_bad_code_is_probe_failed_below_threshold(monkeypatch):
    _enable(monkeypatch)
    store = uh.InMemoryHealthStore()
    result = acp.probe_applecms_source(
        _src(), store, http_get=_stub(200, {"code": 0, "list": []}),
        now=NOW, interval_sec=0.0)
    assert result["outcome"] == "failure"
    assert result["failure_kind"] == acp.FAILURE_PROBE_FAILED
    record = store.get_source("7")
    assert record.consecutive_unavailable_failures == 1
    assert record.availability_state == uh.STATE_ACTIVE  # 未到阈值不翻转


def test_three_failures_flip_to_unavailable(monkeypatch):
    _enable(monkeypatch)
    store = uh.InMemoryHealthStore()
    src = _src()
    stub = _stub(200, {"code": 0, "list": []})
    for _ in range(3):
        result = acp.probe_applecms_source(
            src, store, http_get=stub, now=NOW, threshold=3, interval_sec=0.0)
    assert result["state_after"] == uh.STATE_UNAVAILABLE
    record = store.get_source("7")
    assert record.consecutive_unavailable_failures == 3
    assert record.hidden_at == NOW


def test_success_resets_after_failures(monkeypatch):
    _enable(monkeypatch)
    store = uh.InMemoryHealthStore()
    src = _src()
    bad = _stub(200, {"code": 0, "list": []})
    for _ in range(2):
        acp.probe_applecms_source(src, store, http_get=bad, now=NOW,
                                  interval_sec=0.0)
    result = acp.probe_applecms_source(
        src, store, http_get=_stub(200, {"code": 1, "list": []}),
        now=NOW, interval_sec=0.0)
    assert result["outcome"] == "success"
    record = store.get_source("7")
    assert record.consecutive_unavailable_failures == 0
    assert record.hidden_at is None


def test_unauthorized_is_auth_error(monkeypatch):
    _enable(monkeypatch)
    store = uh.InMemoryHealthStore()
    for status in (401, 403):
        result = acp.probe_applecms_source(
            _src(), store, http_get=_stub(status, {"code": 0}),
            now=NOW, interval_sec=0.0)
        assert result["failure_kind"] == acp.FAILURE_AUTH_ERROR


def test_network_failure_is_probe_failed(monkeypatch):
    _enable(monkeypatch)
    store = uh.InMemoryHealthStore()
    result = acp.probe_applecms_source(
        _src(), store, http_get=_stub(None, None), now=NOW, interval_sec=0.0)
    assert result["outcome"] == "failure"
    assert result["failure_kind"] == acp.FAILURE_PROBE_FAILED

    def raising(url, headers, timeout):
        raise RuntimeError("boom")

    result = acp.probe_applecms_source(
        _src(), store, http_get=raising, now=NOW, interval_sec=0.0)
    assert result["failure_kind"] == acp.FAILURE_PROBE_FAILED


def test_http_429_is_probe_failed_not_its_own_kind(monkeypatch):
    # 失败定性只写 probe_failed / auth_error 两类，429 不另起一类。
    _enable(monkeypatch)
    store = uh.InMemoryHealthStore()
    result = acp.probe_applecms_source(
        _src(), store, http_get=_stub(429, None), now=NOW, interval_sec=0.0)
    assert result["outcome"] == "failure"
    assert result["failure_kind"] == acp.FAILURE_PROBE_FAILED


def test_disabled_switch_sends_no_request(monkeypatch):
    monkeypatch.delenv("APPLECMS_PROBE_ENABLED", raising=False)
    store = uh.InMemoryHealthStore()
    calls = []
    result = acp.probe_applecms_source(
        _src(), store, http_get=_stub(200, {"code": 1, "list": []}, calls),
        now=NOW)
    assert result["outcome"] == "skipped"
    assert result["skipped_reason"] == "disabled"
    assert calls == []
    assert store.get_source("7") is None
    assert acp.probe_applecms_sources_once(
        [_src()], store, http_get=_stub(200, {"code": 1, "list": []}, calls),
        now=NOW) == []


def test_invalid_base_url_is_unknown_not_failure(monkeypatch):
    _enable(monkeypatch)
    store = uh.InMemoryHealthStore()
    calls = []
    result = acp.probe_applecms_source(
        _src(base_url="ftp://cms.example.com"), store,
        http_get=_stub(200, {"code": 1, "list": []}, calls),
        now=NOW, interval_sec=0.0)
    assert result["outcome"] == "unknown"
    assert calls == []
    record = store.get_source("7")
    assert record.availability_state == uh.STATE_ACTIVE  # 未覆盖已知
    assert record.last_checked_at == NOW


def test_not_due_skips_without_request(monkeypatch):
    _enable(monkeypatch)
    store = uh.InMemoryHealthStore()
    src = _src()
    acp.probe_applecms_source(
        src, store, http_get=_stub(200, {"code": 1, "list": []}),
        now=NOW, interval_sec=60.0)
    calls = []
    result = acp.probe_applecms_source(
        src, store, http_get=_stub(200, {"code": 1, "list": []}, calls),
        now=NOW + 10)
    assert result["outcome"] == "skipped"
    assert result["skipped_reason"] == "not_due"
    assert calls == []


def test_source_disabled_skips(monkeypatch):
    _enable(monkeypatch)
    store = uh.InMemoryHealthStore()
    calls = []
    result = acp.probe_applecms_source(
        _src(enabled=False), store,
        http_get=_stub(200, {"code": 1, "list": []}, calls), now=NOW)
    assert result["outcome"] == "skipped"
    assert calls == []
    assert store.get_source("7") is None


def test_headers_passthrough(monkeypatch):
    _enable(monkeypatch)
    store = uh.InMemoryHealthStore()
    calls = []
    src = _src(headers={"User-Agent": "TestAgent/1.0",
                        "Referer": "http://ref.example/"})
    acp.probe_applecms_source(
        src, store, http_get=_stub(200, {"code": 1, "list": []}, calls),
        now=NOW, interval_sec=0.0)
    assert calls[0][1]["User-Agent"] == "TestAgent/1.0"
    assert calls[0][1]["Referer"] == "http://ref.example/"


def test_source_headers_building():
    class Row:
        user_agent = "UA/2.0"
        referer = "http://r.example/"
        extra_headers_json = '{"X-Custom": "yes", "n": 1}'

    headers = acp.source_headers(Row())
    assert headers["User-Agent"] == "UA/2.0"
    assert headers["Referer"] == "http://r.example/"
    assert headers["X-Custom"] == "yes"
    assert "n" not in headers  # 非字符串值丢弃

    class BadRow:
        user_agent = None
        referer = None
        extra_headers_json = "{not json"

    assert acp.source_headers(BadRow()) == {"Accept": "*/*"}


def test_daemon_starts_and_stays_idle_when_disabled(monkeypatch):
    monkeypatch.delenv("APPLECMS_PROBE_ENABLED", raising=False)
    store = uh.InMemoryHealthStore()
    thread = acp.start_applecms_probe_daemon(lambda: [_src()], store)
    assert thread.daemon is True
    assert thread.is_alive()
