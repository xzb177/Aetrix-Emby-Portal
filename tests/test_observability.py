# -*- coding: utf-8 -*-
"""可观测底座测试。

覆盖：
1. 开关默认关：关闭时 snapshot 返回 {"enabled": False}，record_request 零开销
2. 开关开时：各探针返回结构正确（status/message/metrics）
3. API 指标：record_request → api_stats 的 P50/P99/错误率计算正确
4. 路由归一化：数字 id/uuid 收敛，避免基数爆炸
5. 阈值可配置：环境变量覆盖默认值
6. 探针容错：单个探针异常不影响其他
"""
import os

os.environ.setdefault("DATABASE_TYPE", "sqlite")
os.environ.setdefault("REDIS_ENABLED", "false")
# 测试时打开开关
os.environ["OBSERVABILITY_ENABLED"] = "1"

import uuid
from unittest import mock

import pytest

from backend.database import SessionLocal, init_db
from backend.emby_server import observability as obs

init_db()


@pytest.fixture(autouse=True)
def _reset():
    obs.reset_metrics()
    yield
    obs.reset_metrics()


@pytest.fixture()
def db():
    s = SessionLocal()
    try:
        yield s
    finally:
        s.close()


def _guid():
    return uuid.uuid4().hex


# ---------------------------------------------------------------------------
# 开关
# ---------------------------------------------------------------------------

def test_disabled_by_default(monkeypatch):
    monkeypatch.delenv("OBSERVABILITY_ENABLED", raising=False)
    assert obs.enabled() is False
    # 关闭时 record_request 直接返回，不记录
    obs.record_request("/api/test", "GET", 200, 10.0)
    stats = obs.api_stats()
    assert stats["totals"]["requests"] == 0


def test_enabled_flag_variants(monkeypatch):
    for v in ("1", "true", "yes", "on"):
        monkeypatch.setenv("OBSERVABILITY_ENABLED", v)
        assert obs.enabled() is True
    for v in ("0", "false", "no", "off", ""):
        monkeypatch.setenv("OBSERVABILITY_ENABLED", v)
        assert obs.enabled() is False


def test_snapshot_disabled(monkeypatch, db):
    monkeypatch.delenv("OBSERVABILITY_ENABLED", raising=False)
    assert obs.snapshot(db) == {"enabled": False}


# ---------------------------------------------------------------------------
# 路由归一化
# ---------------------------------------------------------------------------

def test_normalize_path():
    assert obs._normalize_path("/api/items/123") == "/api/items/{id}"
    assert obs._normalize_path("/api/items/123/chapters/4") == "/api/items/{id}/chapters/{id}"
    u = _guid()
    assert obs._normalize_path(f"/api/files/{u}") == "/api/files/{uuid}"
    assert obs._normalize_path("/api/health") == "/api/health"


# ---------------------------------------------------------------------------
# API 指标
# ---------------------------------------------------------------------------

def test_record_and_p99():
    # 100 个样本：1..100ms
    for i in range(1, 101):
        obs.record_request("/api/items", "GET", 200, float(i))
    stats = obs.api_stats()
    assert stats["totals"]["requests"] == 100
    routes = stats["routes"]
    assert len(routes) == 1
    r = routes[0]
    assert r["route"] == "GET /api/items"
    assert r["count"] == 100
    assert r["p50_ms"] == pytest.approx(50.5, abs=1.0)
    assert r["p99_ms"] == pytest.approx(99.0, abs=1.0)
    assert r["error_ratio"] == 0.0


def test_error_ratio():
    for _ in range(8):
        obs.record_request("/api/x", "GET", 200, 5.0)
    for _ in range(2):
        obs.record_request("/api/x", "GET", 500, 5.0)
    stats = obs.api_stats()
    assert stats["totals"]["requests"] == 10
    assert stats["totals"]["errors"] == 2
    assert stats["routes"][0]["error_ratio"] == pytest.approx(0.2)
    assert stats["global_error_ratio"] == pytest.approx(0.2)


def test_login_playback_classification():
    obs.record_request("/api/auth/login", "POST", 200, 10.0)
    obs.record_request("/emby/Users/Authenticate", "POST", 200, 10.0)
    obs.record_request("/api/play/123", "GET", 200, 10.0)
    obs.record_request("/emby/videos/456/stream", "GET", 200, 10.0)
    obs.record_request("/api/health", "GET", 200, 10.0)
    totals = obs.api_stats()["totals"]
    assert totals["logins"] == 2
    assert totals["playbacks"] == 2
    assert totals["requests"] == 5


def test_record_never_raises(monkeypatch):
    # record_request 内部异常不能抛出来（主链路保护）
    monkeypatch.delenv("OBSERVABILITY_ENABLED", raising=False)
    monkeypatch.setenv("OBSERVABILITY_ENABLED", "1")
    # 传垃圾参数也不该抛
    obs.record_request(None, None, None, None)  # type: ignore


# ---------------------------------------------------------------------------
# 阈值可配置
# ---------------------------------------------------------------------------

def test_thresholds_configurable(monkeypatch):
    monkeypatch.setenv("OBS_PROBE_QUEUE_WARN", "9999")
    assert obs.THRESHOLDS["probe_queue_warn"]() == 9999
    monkeypatch.setenv("OBS_API_P99_MS_WARN", "1234.5")
    assert obs.THRESHOLDS["api_p99_ms_warn"]() == pytest.approx(1234.5)
    # 非法值回退默认
    monkeypatch.setenv("OBS_PROBE_QUEUE_WARN", "not_a_number")
    assert obs.THRESHOLDS["probe_queue_warn"]() == 1000


# ---------------------------------------------------------------------------
# 探针结构
# ---------------------------------------------------------------------------

def test_probe_subsystems_structure(db):
    subs = obs.probe_subsystems(db)
    expected_keys = {"database", "redis", "disk", "workers", "probe",
                     "enrich", "scan", "sa_quota", "api"}
    # 注 2026-10-17：strm 子系统已随 strm_gen 删除
    assert expected_keys <= set(subs.keys())
    for key, s in subs.items():
        assert s["status"] in ("ok", "warn", "down"), key
        assert "message" in s, key
        assert "metrics" in s, key


def test_snapshot_enabled(db):
    snap = obs.snapshot(db)
    assert snap["enabled"] is True
    assert snap["level"] in ("ok", "warn", "down")
    assert "subsystems" in snap
    assert "thresholds" in snap
    # 阈值快照包含全部键
    assert "probe_queue_warn" in snap["thresholds"]


def test_probe_never_raises_on_broken_db():
    # db 是 None 时各探针应降级为 warn 而不是抛异常
    subs = obs.probe_subsystems(None)
    for key, s in subs.items():
        assert s["status"] in ("ok", "warn", "down"), key


def test_api_probe_warn_on_high_error():
    for _ in range(10):
        obs.record_request("/api/y", "GET", 500, 5.0)
    s = obs._probe_api()
    assert s["status"] == "warn"
    assert "错误率" in s["message"]


def test_api_probe_warn_on_high_p99(monkeypatch):
    monkeypatch.setenv("OBS_API_P99_MS_WARN", "100")
    for _ in range(10):
        obs.record_request("/api/z", "GET", 200, 500.0)
    s = obs._probe_api()
    assert s["status"] == "warn"
    assert "P99" in s["message"]
