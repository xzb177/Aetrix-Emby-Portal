# -*- coding: utf-8 -*-
"""上游健康落库（#25 清单第 14 项：Redis 状态 → PG health_status）。

钉死：翻译三条逐一、回填覆盖/不覆盖、无记录当 unknown、
开关关时零写 PG、单行失败不中断其余行、周期守护开关门控。
全部离线：Redis 用内存替身，DB 用 sqlite 内存库，开关走 monkeypatch。
"""
import os
import time
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


def _store_with(records):
    """records: {str(source_id): redis_state}，用真实 InMemoryHealthStore。"""
    store = uh.InMemoryHealthStore()
    for key, state in records.items():
        store.set_source(uh.SourceHealthRecord(source_key=key,
                                               availability_state=state))
    return store


def _enable(monkeypatch):
    monkeypatch.setenv("UPSTREAM_HEALTH_PG_SYNC_ENABLED", "1")


# ---------- 翻译表（唯一口径） ----------

@pytest.mark.parametrize("redis_state,pg_state", [
    (uh.STATE_ACTIVE, "healthy"),
    (uh.STATE_UNAVAILABLE, "unhealthy"),
    (None, "unknown"),
    ("BogusFutureState", "unknown"),
    (123, "unknown"),
])
def test_translate_redis_state(redis_state, pg_state):
    assert uhp.translate_redis_state(redis_state) == pg_state


def test_redis_key_is_str_source_id(db):
    row = _row(db, "源 A")
    assert uhp.redis_key_for_source(row) == str(row.id)


# ---------- 一次性同步 ----------

def test_sync_overwrites_stale_pg_value(db, monkeypatch):
    """回填覆盖：PG 里是 healthy，Redis 说 Unavailable → 写 unhealthy。"""
    _enable(monkeypatch)
    row = _row(db, "源 A", health_status="healthy")
    store = _store_with({str(row.id): uh.STATE_UNAVAILABLE})
    result = uhp.sync_health_to_pg(db, store)
    assert result["updated"] == 1 and result["errors"] == 0
    db.refresh(row)
    assert row.health_status == "unhealthy"


def test_sync_skips_unchanged_value(db, monkeypatch):
    """回填不覆盖（值已一致）：不动 updated_at，不计 updated。"""
    _enable(monkeypatch)
    row = _row(db, "源 A", health_status="healthy")
    row.updated_at = datetime(2020, 1, 1)
    db.flush()
    store = _store_with({str(row.id): uh.STATE_ACTIVE})
    result = uhp.sync_health_to_pg(db, store)
    assert result["unchanged"] == 1 and result["updated"] == 0
    db.refresh(row)
    assert row.health_status == "healthy"
    assert row.updated_at == datetime(2020, 1, 1)


def test_sync_no_record_means_unknown(db, monkeypatch):
    """无记录当 unknown：PG 里是 healthy、无 Redis 记录 → 写 unknown。"""
    _enable(monkeypatch)
    row = _row(db, "源 A", health_status="healthy")
    result = uhp.sync_health_to_pg(db, uh.InMemoryHealthStore())
    assert result["no_record"] == 1 and result["updated"] == 1
    db.refresh(row)
    assert row.health_status == "unknown"


def test_sync_no_record_already_unknown_unchanged(db, monkeypatch):
    _enable(monkeypatch)
    row = _row(db, "源 A", health_status="unknown")
    result = uhp.sync_health_to_pg(db, uh.InMemoryHealthStore())
    assert result["no_record"] == 1 and result["unchanged"] == 1
    db.refresh(row)
    assert row.health_status == "unknown"


def test_sync_disabled_writes_nothing(db, monkeypatch):
    """开关关时零写 PG：值明明对不上也不动。"""
    monkeypatch.setenv("UPSTREAM_HEALTH_PG_SYNC_ENABLED", "0")
    row = _row(db, "源 A", health_status="healthy")
    store = _store_with({str(row.id): uh.STATE_UNAVAILABLE})
    result = uhp.sync_health_to_pg(db, store)
    assert result["disabled"] is True
    db.refresh(row)
    assert row.health_status == "healthy"


def test_sync_store_error_skips_row_continues_others(db, monkeypatch):
    """单行 Redis 读失败只记 error、不中断其余行。"""
    _enable(monkeypatch)
    row1 = _row(db, "源 A", health_status="healthy")
    row2 = _row(db, "源 B", health_status="healthy")

    class FlakyStore(uh.InMemoryHealthStore):
        def get_source(self, key):
            if key == str(row2.id):
                raise RuntimeError("redis down for this key")
            return super().get_source(key)

    store = FlakyStore()
    store.set_source(uh.SourceHealthRecord(source_key=str(row1.id),
                                           availability_state=uh.STATE_UNAVAILABLE))
    result = uhp.sync_health_to_pg(db, store)
    assert result["updated"] == 1 and result["errors"] == 1
    db.refresh(row1)
    db.refresh(row2)
    assert row1.health_status == "unhealthy"
    assert row2.health_status == "healthy"


def test_sync_pg_write_error_warns_and_continues(db, monkeypatch):
    """单行 PG 写失败只记 error、不中断其余行。"""
    _enable(monkeypatch)
    row1 = _row(db, "源 A", health_status="healthy")
    row2 = _row(db, "源 B", health_status="healthy")
    real_set = uhp.ups.set_health_status

    def _flaky(db_, source, status):
        if source.id == row1.id:
            raise RuntimeError("pg down for this row")
        return real_set(db_, source, status)

    monkeypatch.setattr(uhp.ups, "set_health_status", _flaky)
    store = _store_with({str(row1.id): uh.STATE_UNAVAILABLE,
                         str(row2.id): uh.STATE_UNAVAILABLE})
    result = uhp.sync_health_to_pg(db, store)
    assert result["updated"] == 1 and result["errors"] == 1
    db.refresh(row2)
    assert row2.health_status == "unhealthy"


# ---------- 周期守护 ----------

def test_daemon_syncs_when_enabled(db_factory, monkeypatch):
    _enable(monkeypatch)
    session = db_factory()
    row = _row(session, "源 A", health_status="healthy")
    row_id = row.id
    session.commit()
    session.close()
    store = _store_with({str(row_id): uh.STATE_UNAVAILABLE})
    thread, stop = uhp.start_health_sync_daemon(
        session_factory=db_factory, store=store, interval_sec=0.05)
    try:
        deadline = time.time() + 5
        while time.time() < deadline:
            probe = db_factory()
            try:
                current = probe.query(em.UpstreamSource).filter(
                    em.UpstreamSource.id == row_id).first().health_status
            finally:
                probe.close()
            if current == "unhealthy":
                break
            time.sleep(0.05)
        assert current == "unhealthy"
    finally:
        stop.set()
        thread.join(timeout=5)


def test_daemon_does_nothing_when_disabled(db_factory, monkeypatch):
    monkeypatch.setenv("UPSTREAM_HEALTH_PG_SYNC_ENABLED", "0")
    session = db_factory()
    row = _row(session, "源 A", health_status="healthy")
    row_id = row.id
    session.commit()
    session.close()
    store = _store_with({str(row_id): uh.STATE_UNAVAILABLE})
    thread, stop = uhp.start_health_sync_daemon(
        session_factory=db_factory, store=store, interval_sec=0.05)
    try:
        time.sleep(0.3)
        probe = db_factory()
        try:
            current = probe.query(em.UpstreamSource).filter(
                em.UpstreamSource.id == row_id).first().health_status
        finally:
            probe.close()
        assert current == "healthy"
    finally:
        stop.set()
        thread.join(timeout=5)
