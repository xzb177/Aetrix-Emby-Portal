# -*- coding: utf-8 -*-
"""库源健康过滤的试点范围机制（FMBY 挂载学习第 5 步，挂载-9）。

全部离线：sqlite 内存库；试点清单走 monkeypatch env。
测试里的库 id 一律是假 id（解析单测用 111/222 之类）或本测试
现建库的自增 id，代码与测试里不出现任何真实业务库的 id。
"""

from datetime import datetime, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend import models as base_models
from backend.emby_server import models as em
from backend.emby_server import library_source_health_pilot as pilot
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


def _mount(db, name):
    mount = em.StorageMount(name=name, mount_type="local", path="/tmp")
    db.add(mount)
    db.commit()
    return mount


def _hidden_source(db, lib, mount):
    row = em.LibrarySource(library_id=lib.id, mount_id=mount.id, sub_path="/")
    db.add(row)
    db.commit()
    db.add(em.LibrarySourceHealth(
        library_source_id=row.id, availability_state="Unavailable",
        consecutive_unavailable_failures=3,
        hidden_at=datetime.now() - timedelta(minutes=5)))
    db.commit()
    return row


# ---------- 清单解析：只认正整数，非法段丢弃 ----------

def test_parse_comma_list(monkeypatch):
    monkeypatch.setenv(pilot.PILOT_LIBRARY_IDS_ENV, "111, 222,111")
    assert pilot.pilot_library_ids() == frozenset({111, 222})


def test_parse_ignores_blank_and_invalid(monkeypatch):
    monkeypatch.setenv(pilot.PILOT_LIBRARY_IDS_ENV, " , abc, -5, 0, 333, ")
    assert pilot.pilot_library_ids() == frozenset({333})


def test_parse_empty_means_inactive(monkeypatch):
    monkeypatch.delenv(pilot.PILOT_LIBRARY_IDS_ENV, raising=False)
    assert pilot.pilot_library_ids() == frozenset()
    assert pilot.pilot_scope_active() is False
    monkeypatch.setenv(pilot.PILOT_LIBRARY_IDS_ENV, "   ")
    assert pilot.pilot_scope_active() is False


# ---------- 范围判定：未激活全放行，已激活只放清单 ----------

def test_scope_inactive_allows_all(monkeypatch):
    monkeypatch.delenv(pilot.PILOT_LIBRARY_IDS_ENV, raising=False)
    assert pilot.in_pilot_scope(111) is True
    assert pilot.in_pilot_scope(999001) is True


def test_scope_active_only_allows_listed(monkeypatch):
    monkeypatch.setenv(pilot.PILOT_LIBRARY_IDS_ENV, "111,222")
    assert pilot.in_pilot_scope(111) is True
    assert pilot.in_pilot_scope(333) is False


# ---------- 扫描过滤接线：试点外不滤、试点内照滤 ----------

def test_scan_filter_skipped_outside_pilot(db, monkeypatch):
    lib = _lib(db, "lib-pilot-out")
    mount = _mount(db, "m-pilot-out")
    _hidden_source(db, lib, mount)
    monkeypatch.setenv("LIBRARY_SOURCE_HEALTH_ENABLED", "1")
    # 清单里是与本库无关的假 id：本库不在试点内，不应被健康过滤。
    monkeypatch.setenv(pilot.PILOT_LIBRARY_IDS_ENV, "999001")
    sources, failed = ms.read_library_sources(db, lib)
    assert len(sources) == 1
    assert not any("健康" in (f.get("reason") or "") for f in failed)


def test_scan_filter_applies_inside_pilot(db, monkeypatch):
    lib = _lib(db, "lib-pilot-in")
    mount = _mount(db, "m-pilot-in")
    _hidden_source(db, lib, mount)
    monkeypatch.setenv("LIBRARY_SOURCE_HEALTH_ENABLED", "1")
    monkeypatch.setenv(pilot.PILOT_LIBRARY_IDS_ENV, str(lib.id))
    sources, failed = ms.read_library_sources(db, lib)
    assert sources == []
    assert any("健康" in (f.get("reason") or "") for f in failed)


def test_scan_filter_applies_when_pilot_inactive(db, monkeypatch):
    lib = _lib(db, "lib-pilot-none")
    mount = _mount(db, "m-pilot-none")
    _hidden_source(db, lib, mount)
    monkeypatch.setenv("LIBRARY_SOURCE_HEALTH_ENABLED", "1")
    monkeypatch.delenv(pilot.PILOT_LIBRARY_IDS_ENV, raising=False)
    sources, failed = ms.read_library_sources(db, lib)
    assert sources == []
    assert any("健康" in (f.get("reason") or "") for f in failed)
