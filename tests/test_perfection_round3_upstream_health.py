# -*- coding: utf-8 -*-
"""后端完美审查第三轮第 1 路：#1378 过期翻译 + #1382 unknown 名额归还。

- #1378：PG 翻译的过期降级只限 Active。过期 Unavailable 是“已知已死”，
  须保持 unhealthy，不能被抹成 unknown（与 effective_availability_state
  只降级 Active 的口径一致；修前 sync_health_to_pg 对任何过期记录都
  降级，过期死源在 PG 里丢了 unhealthy 结论）。
- #1382 续：unknown 结论（200 形态不对、握手异常）同样结束了一次探针，
  必须归还半开名额；修前名额占到 120 秒自愈窗口，下次到期探活被
  breaker_open 挡掉（invalid_base_url 出口第二轮已修，unknown 出口漏了）。
全部离线：Redis 用内存替身，DB 用 sqlite 内存库，熔断状态进程全局，
fixture 前后 reset_all。
"""

import os

os.environ.setdefault("DATABASE_TYPE", "sqlite")
os.environ.setdefault("REDIS_ENABLED", "false")

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend import models as base_models
from backend.emby_server import models as em
from backend.emby_server import upstream_health as uh
from backend.emby_server import upstream_health_persist as uhp
from backend.emby_server.metasources import breaker

NOW = 1_800_000_000.0
WEEK = 7 * 24 * 3600
STALE = NOW - WEEK - 1.0


@pytest.fixture()
def db_factory():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False},
                           poolclass=StaticPool)
    base_models.Base.metadata.create_all(engine)
    em.Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)


@pytest.fixture()
def db(db_factory):
    session = db_factory()
    yield session
    session.close()


@pytest.fixture(autouse=True)
def _breaker_isolation(monkeypatch):
    monkeypatch.setenv("UPSTREAM_HEALTH_PROBE_ENABLED", "1")
    monkeypatch.setenv("UPSTREAM_HEALTH_PG_SYNC_ENABLED", "1")
    monkeypatch.setenv("PROVIDER_BREAKER_ENABLED", "1")
    monkeypatch.setenv("PROVIDER_BREAKER_THRESHOLD", "3")
    monkeypatch.setenv("PROVIDER_BREAKER_COOLDOWN_SEC", "60")
    monkeypatch.setenv("PROVIDER_BREAKER_FAST_SEC", "0")  # 冷却立即到期 → 半开
    breaker.reset_all()
    yield
    breaker.reset_all()


def _row(db, name, health_status):
    row = em.UpstreamSource(name=name, source_type="emby",
                            base_url="https://x.example.com",
                            auth_method="password", username="a",
                            credential_scope="source",
                            health_status=health_status)
    db.add(row)
    db.flush()
    return row


def _trip_half_open(key):
    breaker.record_failure(f"upstream:{key}", status=429, error="HTTP 429")


def _good_http(url, headers, timeout):
    return 200, {"Id": "live-server", "ServerName": "live"}


# ---------- #1378：过期降级只限 Active ----------

def test_sync_stale_unavailable_stays_unhealthy(db):
    row = _row(db, "过期死源", health_status="unhealthy")
    store = uh.InMemoryHealthStore()
    store.set_source(uh.SourceHealthRecord(source_key=str(row.id),
                                           availability_state=uh.STATE_UNAVAILABLE,
                                           last_checked_at=STALE))
    result = uhp.sync_health_to_pg(db, store, now=NOW)
    assert result["updated"] == 0 and result["errors"] == 0
    db.refresh(row)
    assert row.health_status == "unhealthy"


def test_sync_stale_unavailable_from_healthy_becomes_unhealthy(db):
    row = _row(db, "过期死源2", health_status="healthy")
    store = uh.InMemoryHealthStore()
    store.set_source(uh.SourceHealthRecord(source_key=str(row.id),
                                           availability_state=uh.STATE_UNAVAILABLE,
                                           last_checked_at=STALE))
    result = uhp.sync_health_to_pg(db, store, now=NOW)
    assert result["updated"] == 1 and result["errors"] == 0
    db.refresh(row)
    assert row.health_status == "unhealthy"


# ---------- #1382 续：unknown 结论归还半开名额 ----------

def test_unknown_classify_releases_half_open_slot():
    store = uh.InMemoryHealthStore()
    _trip_half_open("u1")
    first = uh.probe_source(
        uh.UpstreamSource(key="u1", kind="emby", base_url="https://x.example.com"),
        store, http_get=lambda url, headers, timeout: (200, {"foo": 1}),
        now=1000.0, interval_sec=1.0)
    assert first["outcome"] == uh.OUTCOME_UNKNOWN
    second = uh.probe_source(
        uh.UpstreamSource(key="u1", kind="emby", base_url="https://x.example.com"),
        store, http_get=_good_http, now=1002.0, interval_sec=1.0)
    # 修前：第二次 allow() 因名额仍在飞而回 False → skipped / breaker_open。
    assert second["outcome"] == "success"


def test_http_exception_unknown_releases_half_open_slot():
    store = uh.InMemoryHealthStore()
    _trip_half_open("u2")

    def _boom(url, headers, timeout):
        raise RuntimeError("boom")

    first = uh.probe_source(
        uh.UpstreamSource(key="u2", kind="emby", base_url="https://x.example.com"),
        store, http_get=_boom, now=1000.0, interval_sec=1.0)
    assert first["outcome"] == uh.OUTCOME_UNKNOWN
    second = uh.probe_source(
        uh.UpstreamSource(key="u2", kind="emby", base_url="https://x.example.com"),
        store, http_get=_good_http, now=1002.0, interval_sec=1.0)
    assert second["outcome"] == "success"
