# -*- coding: utf-8 -*-
"""上游探活与 availability_status（#25 25-3，源级）。

全部离线：HTTP 与熔断一律用替身；Redis 用内存替身；时间用注入的 now。
开关与阈值走 monkeypatch env，不依赖真实配置。
"""
import os

os.environ.setdefault("DATABASE_TYPE", "sqlite")
os.environ.setdefault("REDIS_ENABLED", "false")

import pytest

from backend.emby_server import upstream_health as uh


class FakeBreaker:
    def __init__(self, allowed=True):
        self.allowed = allowed
        self.successes = []
        self.failures = []

    def allow(self, key):
        return self.allowed

    def record_success(self, key):
        self.successes.append(key)

    def record_failure(self, key, status=None, error=""):
        self.failures.append((key, status, error))


class FakeRedis:
    def __init__(self):
        self.data = {}

    def get(self, key):
        return self.data.get(key)

    def set(self, key, value):
        self.data[key] = value


def _enable(monkeypatch):
    monkeypatch.setenv("UPSTREAM_HEALTH_PROBE_ENABLED", "1")


def _rec():
    return uh.SourceHealthRecord(source_key="s1")


# --- 状态机：FMBY 实测规则 ---

def test_no_record_means_active():
    assert uh.is_source_available(None) is True
    assert uh.is_line_available(None) is True


def test_success_resets_count_and_hidden():
    rec = _rec()
    rec.availability_state = uh.STATE_UNAVAILABLE
    rec.consecutive_unavailable_failures = 5
    rec.hidden_at = 100.0
    uh.apply_source_result(rec, uh.ProbeOutcome(uh.OUTCOME_SUCCESS), threshold=3, now=200.0)
    assert rec.availability_state == uh.STATE_ACTIVE
    assert rec.consecutive_unavailable_failures == 0
    assert rec.hidden_at is None
    assert rec.last_success_at == 200.0
    # 该用例的心跳是 200.0（1970 纪元）；按过期语义须以同一时刻判断，
    # 否则以真实 now 判断会因超 7 天未探活而判过期（属新语义，非回归）。
    assert uh.is_source_available(rec, now=200.0) is True


def test_failure_counts_but_does_not_flip_below_threshold():
    rec = _rec()
    uh.apply_source_result(rec, uh.ProbeOutcome(uh.OUTCOME_FAILURE, "unreachable"), threshold=3, now=10.0)
    uh.apply_source_result(rec, uh.ProbeOutcome(uh.OUTCOME_FAILURE, "unreachable"), threshold=3, now=20.0)
    assert rec.consecutive_unavailable_failures == 2
    assert rec.availability_state == uh.STATE_ACTIVE
    assert rec.hidden_at is None


def test_failure_at_threshold_flips_and_marks_hidden_once():
    rec = _rec()
    for i in range(3):
        uh.apply_source_result(rec, uh.ProbeOutcome(uh.OUTCOME_FAILURE, "server_error"), threshold=3, now=10.0 + i)
    assert rec.availability_state == uh.STATE_UNAVAILABLE
    assert rec.hidden_at == 12.0
    uh.apply_source_result(rec, uh.ProbeOutcome(uh.OUTCOME_FAILURE, "server_error"), threshold=3, now=99.0)
    assert rec.hidden_at == 12.0  # 继续失败不刷新隐藏起点
    assert uh.is_source_available(rec) is False


def test_unknown_never_overwrites_known_state():
    rec = _rec()
    rec.availability_state = uh.STATE_UNAVAILABLE
    rec.consecutive_unavailable_failures = 3
    rec.hidden_at = 50.0
    rec.last_failure_kind = "server_error"
    uh.apply_source_result(rec, uh.ProbeOutcome(uh.OUTCOME_UNKNOWN), threshold=3, now=60.0)
    assert rec.availability_state == uh.STATE_UNAVAILABLE
    assert rec.consecutive_unavailable_failures == 3
    assert rec.hidden_at == 50.0
    assert rec.last_failure_kind == "server_error"
    assert rec.last_checked_at == 60.0


def test_line_failure_needs_confirmation():
    rec = uh.LineHealthRecord(line_key="l1")
    uh.apply_line_result(rec, uh.ProbeOutcome(uh.OUTCOME_FAILURE, "playback_unavailable"),
                         confirmed=False, now=10.0)
    assert rec.availability_status == uh.LINE_AVAILABLE  # 未确认不摘线
    uh.apply_line_result(rec, uh.ProbeOutcome(uh.OUTCOME_FAILURE, "playback_unavailable"),
                         confirmed=True, now=20.0)
    assert rec.availability_status == uh.LINE_UNAVAILABLE
    uh.apply_line_result(rec, uh.ProbeOutcome(uh.OUTCOME_UNKNOWN), confirmed=True, now=30.0)
    assert rec.availability_status == uh.LINE_UNAVAILABLE  # Unknown 不覆盖
    uh.apply_line_result(rec, uh.ProbeOutcome(uh.OUTCOME_SUCCESS), confirmed=True, now=40.0)
    assert rec.availability_status == uh.LINE_AVAILABLE


def test_classify_line_probe_system_ok_but_playback_dead_is_failure():
    # B3 验收第 4 条：System/Info 活着、PlaybackInfo 500 → 线路必须判失败。
    out = uh.classify_line_probe(True, 500, None)
    assert out.kind == uh.OUTCOME_FAILURE
    assert out.failure_kind == "playback_unavailable"
    assert uh.classify_line_probe(True, 200, 206).kind == uh.OUTCOME_SUCCESS
    assert uh.classify_line_probe(False, 500, None).kind == uh.OUTCOME_FAILURE
    assert uh.classify_line_probe(True, None, None).kind == uh.OUTCOME_UNKNOWN


def test_classify_handshake_shape_unknown():
    assert uh.classify_handshake(200, {"ServerName": "x", "Id": "1"}).kind == uh.OUTCOME_SUCCESS
    assert uh.classify_handshake(200, {"weird": True}).kind == uh.OUTCOME_UNKNOWN
    assert uh.classify_handshake(200, None).kind == uh.OUTCOME_UNKNOWN
    assert uh.classify_handshake(401, None).failure_kind == "unauthorized"
    assert uh.classify_handshake(429, None).failure_kind == "rate_limited"
    assert uh.classify_handshake(500, None).failure_kind == "server_error"
    assert uh.classify_handshake(None, None).failure_kind == "unreachable"


def test_backoff_only_after_flip_and_capped():
    rec = _rec()
    rec.availability_state = uh.STATE_UNAVAILABLE
    rec.consecutive_unavailable_failures = 3
    assert uh.next_probe_due(rec, interval_sec=60.0, backoff_max_sec=600.0, threshold=3, now=1000.0) == 1060.0
    rec.consecutive_unavailable_failures = 13  # 2**10 * 60 远超上限
    assert uh.next_probe_due(rec, interval_sec=60.0, backoff_max_sec=600.0, threshold=3, now=1000.0) == 1600.0
    rec2 = _rec()  # 计数中不退避
    rec2.consecutive_unavailable_failures = 2
    assert uh.next_probe_due(rec2, interval_sec=60.0, backoff_max_sec=600.0, threshold=3, now=1000.0) == 1060.0


# --- 工人：握手、熔断、退避、SSRF、默认关 ---

def test_probe_disabled_does_nothing(monkeypatch):
    monkeypatch.setenv("UPSTREAM_HEALTH_PROBE_ENABLED", "0")
    store = uh.InMemoryHealthStore()
    calls = []
    out = uh.probe_sources_once(
        [uh.UpstreamSource(key="s1", kind="emby", base_url="http://127.0.0.1:8096")],
        store, http_get=lambda *a, **k: calls.append(a) or (200, {"Id": "1"}))
    assert out == [] and calls == [] and store.get_source("s1") is None


def test_probe_success_with_auth_proves_credentials(monkeypatch):
    _enable(monkeypatch)
    monkeypatch.setattr(uh, "provider_breaker", FakeBreaker())
    store = uh.InMemoryHealthStore()
    seen = []

    def fake_http(url, headers, timeout):
        seen.append((url, headers))
        if url.endswith("/System/Info/Public"):
            return 200, {"Id": "1", "ServerName": "up"}
        return 200, {"Id": "1"}

    src = uh.UpstreamSource(key="s1", kind="emby", base_url="http://example.com:8096/", api_key="SECRET-KEY")
    res = uh.probe_source(src, store, http_get=fake_http, now=100.0, threshold=3)
    assert res["outcome"] == "success"
    assert store.get_source("s1").availability_state == uh.STATE_ACTIVE
    assert all("SECRET-KEY" not in url for url, _ in seen)  # 凭据绝不进 URL
    assert any("Token=" in h.get("X-Emby-Authorization", "") for _, h in seen)


def test_probe_flips_after_threshold_and_skips_when_not_due(monkeypatch):
    _enable(monkeypatch)
    monkeypatch.setattr(uh, "provider_breaker", FakeBreaker())
    store = uh.InMemoryHealthStore()
    src = uh.UpstreamSource(key="s1", kind="jellyfin", base_url="http://example.com:8096")
    calls = []

    def dead(url, headers, timeout):
        calls.append(url)
        return None, None  # 断连 → unreachable

    for i in range(3):
        uh.probe_source(src, store, http_get=dead, now=100.0 + i * 1000.0, threshold=3,
                        interval_sec=60.0, backoff_max_sec=600.0)
    rec = store.get_source("s1")
    assert rec.availability_state == uh.STATE_UNAVAILABLE
    assert rec.hidden_at is not None
    n = len(calls)
    # 未到退避后的下次探活时刻：不发请求、不写状态。
    res = uh.probe_source(src, store, http_get=dead, now=rec.next_probe_at - 1,
                          threshold=3, interval_sec=60.0, backoff_max_sec=600.0)
    assert res["outcome"] == "skipped" and len(calls) == n


def test_probe_unknown_shape_does_not_flip(monkeypatch):
    _enable(monkeypatch)
    monkeypatch.setattr(uh, "provider_breaker", FakeBreaker())
    store = uh.InMemoryHealthStore()
    src = uh.UpstreamSource(key="s1", kind="emby", base_url="http://example.com:8096")
    rec = uh.SourceHealthRecord(source_key="s1", availability_state=uh.STATE_ACTIVE,
                                consecutive_unavailable_failures=2)
    store.set_source(rec)
    res = uh.probe_source(src, store,
                          http_get=lambda *a, **k: (200, {"weird": True}),
                          now=500.0, threshold=3)
    assert res["outcome"] == "unknown"
    after = store.get_source("s1")
    assert after.availability_state == uh.STATE_ACTIVE
    assert after.consecutive_unavailable_failures == 2


def test_breaker_open_skips_without_request(monkeypatch):
    _enable(monkeypatch)
    monkeypatch.setattr(uh, "provider_breaker", FakeBreaker(allowed=False))
    store = uh.InMemoryHealthStore()
    calls = []
    res = uh.probe_source(
        uh.UpstreamSource(key="s1", kind="emby", base_url="http://127.0.0.1:8096"),
        store, http_get=lambda *a, **k: calls.append(1) or (200, {"Id": "1"}),
        now=100.0, threshold=3)
    assert res["outcome"] == "skipped" and calls == []


def test_ssrf_userinfo_url_never_fetched(monkeypatch):
    _enable(monkeypatch)
    monkeypatch.setattr(uh, "provider_breaker", FakeBreaker())
    store = uh.InMemoryHealthStore()
    calls = []
    res = uh.probe_source(
        uh.UpstreamSource(key="s1", kind="emby", base_url="http://user:pass@127.0.0.1:8096"),
        store, http_get=lambda *a, **k: calls.append(1) or (200, {"Id": "1"}),
        now=100.0, threshold=3)
    assert calls == [] and res["outcome"] == "unknown"
    with pytest.raises(ValueError):
        uh.validate_base_url("ftp://127.0.0.1:8096")


def test_redis_store_roundtrip_and_failure_fail_open():
    store = uh.RedisHealthStore(client=FakeRedis())
    rec = uh.SourceHealthRecord(source_key="s1", availability_state=uh.STATE_UNAVAILABLE,
                                consecutive_unavailable_failures=3, hidden_at=42.0)
    store.set_source(rec)
    back = store.get_source("s1")
    assert back.availability_state == uh.STATE_UNAVAILABLE
    assert back.consecutive_unavailable_failures == 3
    assert back.hidden_at == 42.0
    assert "SECRET" not in FakeRedis().data.get("x", "")

    class BrokenRedis:
        def get(self, key):
            raise ConnectionError("down")

        def set(self, key, value):
            raise ConnectionError("down")

    broken = uh.RedisHealthStore(client=BrokenRedis())
    assert broken.get_source("s1") is None  # 读失败按无记录计，不抛
    broken.set_source(rec)  # 写失败不抛
