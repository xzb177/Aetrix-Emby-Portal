# -*- coding: utf-8 -*-
"""后端完美审查第二轮第 7 项：熔断半开名额归还（parent 2026-10-27 定口径）。

钉死：probe_source 在 allow() 通过后走 invalid_base_url 出口时，必须调
release_probe 归还半开探针名额（breaker 文档要求）。修前该出口漏归还，
名额一直被占到 120 秒自愈窗口（breaker._PROBE_STALE_SEC），其间该源的
半开探测全被 breaker_open 挡掉。
操纵方式照 tests/test_provider_breaker.py 既有范式；熔断状态是进程全局，
本文件 fixture 前后 reset_all，不许漏给同进程其他文件。
全部离线：http 层用桩，DB 不碰（内存 store）。
"""

import os

os.environ.setdefault("DATABASE_TYPE", "sqlite")
os.environ.setdefault("REDIS_ENABLED", "false")

import pytest

from backend.emby_server import upstream_health as uh
from backend.emby_server.metasources import breaker


@pytest.fixture(autouse=True)
def _breaker_isolation(monkeypatch):
    monkeypatch.setenv("UPSTREAM_HEALTH_PROBE_ENABLED", "1")
    monkeypatch.setenv("PROVIDER_BREAKER_ENABLED", "1")
    monkeypatch.setenv("PROVIDER_BREAKER_THRESHOLD", "3")
    monkeypatch.setenv("PROVIDER_BREAKER_COOLDOWN_SEC", "60")
    monkeypatch.setenv("PROVIDER_BREAKER_FAST_SEC", "0")  # 冷却立即到期 → 半开
    breaker.reset_all()
    yield
    breaker.reset_all()


def _trip_half_open(key: str) -> None:
    """429 快开断 + 冷却 0 秒已到期：下一次 allow() 即半开放行（放一个探测）。"""
    breaker.record_failure(key, status=429, error="HTTP 429")


def test_invalid_base_url_releases_half_open_slot():
    store = uh.InMemoryHealthStore()
    bad = uh.UpstreamSource(key="rel", kind="emby", base_url="not-a-url")
    _trip_half_open("upstream:rel")

    first = uh.probe_source(bad, store, http_get=lambda url, headers, timeout: (200, {}),
                            now=1000.0, interval_sec=1.0)
    assert first["outcome"] == uh.OUTCOME_UNKNOWN
    assert first["skipped_reason"] == "invalid_base_url"
    assert first["state_before"] == first["state_after"] == uh.STATE_ACTIVE

    def _http_get(url, headers, timeout):
        assert url.endswith("/System/Info/Public")
        return 200, {"Id": "live-server", "ServerName": "live"}

    good = uh.UpstreamSource(key="rel", kind="emby",
                             base_url="https://live.example.com")
    second = uh.probe_source(good, store, http_get=_http_get,
                             now=1002.0, interval_sec=1.0)
    # 修前：第二次 allow() 因名额仍在飞而回 False → outcome skipped / breaker_open。
    assert second["outcome"] == "success"
    assert store.get_source("rel").availability_state == uh.STATE_ACTIVE


def test_invalid_base_url_with_breaker_disabled_behaves_as_before(monkeypatch):
    """熔断关时 allow 从未置名额，release_probe 是安全 no-op，旧行为逐字不变。"""
    monkeypatch.setenv("PROVIDER_BREAKER_ENABLED", "0")
    store = uh.InMemoryHealthStore()
    bad = uh.UpstreamSource(key="rel2", kind="emby", base_url="ftp://x.example.com")
    result = uh.probe_source(bad, store, http_get=lambda url, headers, timeout: (200, {}),
                             now=1000.0, interval_sec=1.0)
    assert result["outcome"] == uh.OUTCOME_UNKNOWN
    assert result["skipped_reason"] == "invalid_base_url"
    assert result["state_before"] == result["state_after"] == uh.STATE_ACTIVE
