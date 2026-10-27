# -*- coding: utf-8 -*-
"""后端完美审查第二轮第 1 项：上游健康过期（parent 2026-10-27 定口径）。

钉死三条，全部离线：Redis 用内存替身，DB 用 sqlite 内存库。
- 超 7 天未探活的 Active 记录在自动调度判断里等效 unknown
  （is_source_available 回 False），但存的状态本身不动。
- 每次探活结论都刷新 Redis 心跳（unknown 结论也不例外，只是不改状态）。
- PG 同步把 Redis 探活心跳镜像进 PG last_checked_at（状态不变时也写，
  不计 updated）；过期 Active 在 PG 翻译层降级为 unknown。
无新增 env 键：7 天阈值是常量 HEALTH_STALE_AFTER_SEC。
"""

import os
from datetime import datetime

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

NOW = 1_800_000_000.0
WEEK = 7 * 24 * 3600
STALE = NOW - WEEK - 1.0
BOUNDARY = NOW - WEEK
FRESH = NOW - 3600.0


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


def _row(db, name, health_status="unknown"):
    row = em.UpstreamSource(name=name, source_type="emby",
                            base_url="https://x.example.com",
                            auth_method="password", username="a",
                            credential_scope="source",
                            health_status=health_status)
    db.add(row)
    db.flush()
    return row


def _rec(state=uh.STATE_ACTIVE, checked=FRESH):
    return uh.SourceHealthRecord(source_key="s1",
                                 availability_state=state,
                                 last_checked_at=checked)


def _enable(monkeypatch):
    monkeypatch.setenv("UPSTREAM_HEALTH_PG_SYNC_ENABLED", "1")


# ---------- 自动调度判断：过期等效 unknown ----------

def test_stale_active_is_unavailable_for_auto_scheduling():
    assert uh.is_source_available(_rec(checked=STALE), now=NOW) is False
    assert uh.effective_availability_state(_rec(checked=STALE),
                                           now=NOW) == uh.OUTCOME_UNKNOWN
    # 等效而已：存的状态本身不动，下次探活成功即复位。
    rec = _rec(checked=STALE)
    uh.is_source_available(rec, now=NOW)
    assert rec.availability_state == uh.STATE_ACTIVE


def test_fresh_and_boundary_active_stay_available():
    assert uh.is_source_available(_rec(checked=FRESH), now=NOW) is True
    # 严格大于 7 天才过期：恰好 7 天整不过期。
    assert uh.is_source_available(_rec(checked=BOUNDARY), now=NOW) is True


def test_no_record_and_missing_heartbeat_stay_available():
    assert uh.is_source_available(None, now=NOW) is True
    assert uh.is_source_available(_rec(checked=None), now=NOW) is True


def test_unavailable_stays_unavailable_regardless_of_heartbeat():
    assert uh.is_source_available(
        _rec(state=uh.STATE_UNAVAILABLE, checked=FRESH), now=NOW) is False
    assert uh.is_source_available(
        _rec(state=uh.STATE_UNAVAILABLE, checked=STALE), now=NOW) is False


def test_unknown_outcome_advances_heartbeat_without_state_change():
    rec = _rec(state=uh.STATE_UNAVAILABLE, checked=10.0)
    rec.consecutive_unavailable_failures = 3
    rec.hidden_at = 50.0
    uh.apply_source_result(rec, uh.ProbeOutcome(uh.OUTCOME_UNKNOWN),
                           threshold=3, now=60.0)
    assert rec.availability_state == uh.STATE_UNAVAILABLE
    assert rec.consecutive_unavailable_failures == 3
    assert rec.hidden_at == 50.0
    assert rec.last_checked_at == 60.0


# ---------- PG 同步：过期降级 + 心跳镜像 ----------

def test_sync_downgrades_stale_active_to_unknown(db, monkeypatch):
    _enable(monkeypatch)
    row = _row(db, "源 A", health_status="healthy")
    store = uh.InMemoryHealthStore()
    store.set_source(uh.SourceHealthRecord(source_key=str(row.id),
                                           availability_state=uh.STATE_ACTIVE,
                                           last_checked_at=STALE))
    result = uhp.sync_health_to_pg(db, store, now=NOW)
    assert result["updated"] == 1 and result["errors"] == 0
    db.refresh(row)
    assert row.health_status == "unknown"


def test_sync_stale_active_already_unknown_is_unchanged(db, monkeypatch):
    _enable(monkeypatch)
    row = _row(db, "源 A", health_status="unknown")
    store = uh.InMemoryHealthStore()
    store.set_source(uh.SourceHealthRecord(source_key=str(row.id),
                                           availability_state=uh.STATE_ACTIVE,
                                           last_checked_at=STALE))
    result = uhp.sync_health_to_pg(db, store, now=NOW)
    assert result["unchanged"] == 1 and result["updated"] == 0
    db.refresh(row)
    assert row.health_status == "unknown"


def test_sync_mirrors_heartbeat_when_state_unchanged(db, monkeypatch):
    _enable(monkeypatch)
    row = _row(db, "源 A", health_status="healthy")
    row.last_checked_at = datetime(2020, 1, 1)
    db.flush()
    store = uh.InMemoryHealthStore()
    store.set_source(uh.SourceHealthRecord(source_key=str(row.id),
                                           availability_state=uh.STATE_ACTIVE,
                                           last_checked_at=FRESH))
    result = uhp.sync_health_to_pg(db, store, now=NOW)
    assert result["unchanged"] == 1 and result["updated"] == 0
    assert result["heartbeat_refreshed"] == 1
    db.refresh(row)
    assert row.health_status == "healthy"
    assert row.last_checked_at == datetime.fromtimestamp(FRESH)


def test_sync_state_change_row_also_mirrors_probe_time(db, monkeypatch):
    _enable(monkeypatch)
    row = _row(db, "源 A", health_status="healthy")
    store = uh.InMemoryHealthStore()
    store.set_source(uh.SourceHealthRecord(source_key=str(row.id),
                                           availability_state=uh.STATE_UNAVAILABLE,
                                           last_checked_at=FRESH))
    result = uhp.sync_health_to_pg(db, store, now=NOW)
    assert result["updated"] == 1 and result["errors"] == 0
    db.refresh(row)
    assert row.health_status == "unhealthy"
    assert row.last_checked_at == datetime.fromtimestamp(FRESH)


def test_sync_skips_heartbeat_copy_when_no_heartbeat(db, monkeypatch):
    _enable(monkeypatch)
    row = _row(db, "源 A", health_status="healthy")
    row.last_checked_at = datetime(2020, 1, 1)
    db.flush()
    store = uh.InMemoryHealthStore()
    store.set_source(uh.SourceHealthRecord(source_key=str(row.id),
                                           availability_state=uh.STATE_ACTIVE,
                                           last_checked_at=None))
    result = uhp.sync_health_to_pg(db, store, now=NOW)
    assert result["unchanged"] == 1 and result["heartbeat_refreshed"] == 0
    db.refresh(row)
    assert row.last_checked_at == datetime(2020, 1, 1)
