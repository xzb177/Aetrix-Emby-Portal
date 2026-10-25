# -*- coding: utf-8 -*-
"""库源健康落库（FMBY 挂载学习第 5 步）。

全部离线：sqlite 内存库；开关与阈值走 monkeypatch env；
探针扇出直接调函数，不跑真实体检。
"""

from datetime import datetime, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend import models as base_models
from backend.emby_server import models as em
from backend.emby_server import library_source_health as lsh
from backend.emby_server import mount_sources as ms


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


def _lib(db, guid):
    lib = em.Library(guid=guid, name=guid)
    db.add(lib)
    db.commit()
    return lib


def _mount(db, name, mount_type="local", path="/tmp"):
    mount = em.StorageMount(name=name, mount_type=mount_type, path=path)
    db.add(mount)
    db.commit()
    return mount


def _source(db, lib, mount, sub_path="/"):
    row = em.LibrarySource(library_id=lib.id, mount_id=mount.id, sub_path=sub_path)
    db.add(row)
    db.commit()
    return row


def _health_row(db, source_id):
    return db.query(em.LibrarySourceHealth).filter(
        em.LibrarySourceHealth.library_source_id == source_id).first()


# ---------- 读口：无记录默认可用 ----------

def test_no_row_means_available(db):
    assert lsh.is_source_available(db, 999999) is True


# ---------- 状态机：成功复位 ----------

def test_success_resets_count_and_hidden(db):
    lib = _lib(db, "lib-ok")
    mount = _mount(db, "m-ok")
    src = _source(db, lib, mount)
    t0 = datetime.now() - timedelta(hours=1)
    db.add(em.LibrarySourceHealth(
        library_source_id=src.id, availability_state="Unavailable",
        consecutive_unavailable_failures=3, last_failure_kind="probe_failed",
        hidden_at=t0))
    db.commit()
    lsh.record_source_result(db, src.id, True)
    row = _health_row(db, src.id)
    assert row.availability_state == "Active"
    assert row.consecutive_unavailable_failures == 0
    assert row.hidden_at is None
    assert row.last_success_at is not None


# ---------- 状态机：达阈值才隐藏 ----------

def test_failure_hides_at_threshold(db):
    lib = _lib(db, "lib-hide")
    mount = _mount(db, "m-hide")
    src = _source(db, lib, mount)
    lsh.record_source_result(db, src.id, False, "probe_failed", threshold=3)
    lsh.record_source_result(db, src.id, False, "probe_failed", threshold=3)
    row = _health_row(db, src.id)
    assert row.availability_state == "Active"
    assert row.consecutive_unavailable_failures == 2
    assert row.hidden_at is None
    lsh.record_source_result(db, src.id, False, "probe_failed", threshold=3)
    row = _health_row(db, src.id)
    assert row.availability_state == "Unavailable"
    assert row.consecutive_unavailable_failures == 3
    assert row.hidden_at is not None
    assert row.last_failure_kind == "probe_failed"
    assert lsh.is_source_available(db, src.id) is False


# ---------- 状态机：Unknown 只推 last_checked ----------

def test_unknown_only_moves_checked(db):
    lib = _lib(db, "lib-unk")
    mount = _mount(db, "m-unk")
    src = _source(db, lib, mount)
    t0 = datetime.now() - timedelta(hours=1)
    t1 = datetime.now()
    db.add(em.LibrarySourceHealth(
        library_source_id=src.id, availability_state="Active",
        consecutive_unavailable_failures=2, last_checked_at=t0))
    db.commit()
    lsh.record_source_result(db, src.id, None, now=t1)
    row = _health_row(db, src.id)
    assert row.availability_state == "Active"
    assert row.consecutive_unavailable_failures == 2
    assert row.hidden_at is None
    assert row.last_checked_at == t1


# ---------- 状态机：hidden_at 首次翻转后不刷新 ----------

def test_hidden_at_not_refreshed(db):
    lib = _lib(db, "lib-hidden")
    mount = _mount(db, "m-hidden")
    src = _source(db, lib, mount)
    t0 = datetime.now() - timedelta(hours=1)
    db.add(em.LibrarySourceHealth(
        library_source_id=src.id, availability_state="Unavailable",
        consecutive_unavailable_failures=3, hidden_at=t0))
    db.commit()
    lsh.record_source_result(db, src.id, False, "probe_failed", threshold=3)
    row = _health_row(db, src.id)
    assert row.hidden_at == t0
    assert row.consecutive_unavailable_failures == 4


# ---------- 扇出：同挂载多源行同写，他挂载不动 ----------

def test_fanout_updates_only_same_mount(db):
    lib = _lib(db, "lib-fan")
    mount_a = _mount(db, "m-fan-a")
    mount_b = _mount(db, "m-fan-b")
    src_a1 = _source(db, lib, mount_a, "/")
    src_a2 = _source(db, lib, mount_a, "/sub")
    src_b = _source(db, lib, mount_b, "/")
    assert lsh.sync_mount_probe_to_source_health(db, mount_a.id, False, "probe_failed") == 2
    assert _health_row(db, src_a1.id).consecutive_unavailable_failures == 1
    assert _health_row(db, src_a2.id).consecutive_unavailable_failures == 1
    assert _health_row(db, src_b.id) is None
    assert lsh.sync_mount_probe_to_source_health(db, mount_a.id, None) == 0


# ---------- 开关与阈值默认值 ----------

def test_env_defaults(monkeypatch):
    monkeypatch.delenv("LIBRARY_SOURCE_HEALTH_ENABLED", raising=False)
    monkeypatch.delenv("LIBRARY_SOURCE_HEALTH_HIDE_AFTER", raising=False)
    assert lsh.health_enabled() is False
    assert lsh.hide_after() == 3
    monkeypatch.setenv("LIBRARY_SOURCE_HEALTH_HIDE_AFTER", "5")
    assert lsh.hide_after() == 5


# ---------- 扫描过滤：开关开时隐藏源不进扫描、记 failed ----------

def test_scan_filter_hides_unhealthy_source(db, monkeypatch):
    lib = _lib(db, "lib-scan")
    mount = _mount(db, "m-scan")
    src = _source(db, lib, mount, "/")
    db.add(em.LibrarySourceHealth(
        library_source_id=src.id, availability_state="Unavailable",
        consecutive_unavailable_failures=3,
        hidden_at=datetime.now() - timedelta(minutes=5)))
    db.commit()
    monkeypatch.setenv("LIBRARY_SOURCE_HEALTH_ENABLED", "1")
    sources, failed = ms.read_library_sources(db, lib)
    assert sources == []
    assert any("健康" in (f.get("reason") or "") for f in failed)


def test_scan_filter_off_keeps_source(db, monkeypatch):
    lib = _lib(db, "lib-scan-off")
    mount = _mount(db, "m-scan-off")
    src = _source(db, lib, mount, "/")
    db.add(em.LibrarySourceHealth(
        library_source_id=src.id, availability_state="Unavailable",
        consecutive_unavailable_failures=3,
        hidden_at=datetime.now() - timedelta(minutes=5)))
    db.commit()
    monkeypatch.delenv("LIBRARY_SOURCE_HEALTH_ENABLED", raising=False)
    sources, failed = ms.read_library_sources(db, lib)
    assert len(sources) == 1
    assert not any("健康" in (f.get("reason") or "") for f in failed)
