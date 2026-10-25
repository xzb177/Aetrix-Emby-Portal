# -*- coding: utf-8 -*-
"""播放按库源健康表过滤（FMBY 挂载学习第 5 步，播放侧）。

全部离线：sqlite 内存库；健康总闸与试点清单走 monkeypatch env。
只测本模块的映射与判定，不起 FastAPI、不调 video_stream；
api.py 三处接线（变体清单 / 变体解析 / 取流门）以 CI 为准。
"""

from datetime import datetime, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend import models as base_models
from backend.emby_server import models as em
from backend.emby_server import playback_source_health as psh


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


def _setup(db, sub_path="/Movies"):
    lib = em.Library(guid="lib-psh", name="lib-psh")
    db.add(lib)
    db.commit()
    mount = em.StorageMount(name="m-psh", mount_type="local", path="/tmp")
    db.add(mount)
    db.commit()
    src = em.LibrarySource(library_id=lib.id, mount_id=mount.id, sub_path=sub_path)
    db.add(src)
    db.commit()
    item = em.MediaItem(guid="item-psh", library_id=lib.id, item_type="movie",
                        name="item-psh",
                        file_path=f"mount://{mount.id}{sub_path}/a.mkv")
    db.add(item)
    db.commit()
    return lib, mount, src, item


def _hide(db, src):
    db.add(em.LibrarySourceHealth(
        library_source_id=src.id, availability_state="Unavailable",
        consecutive_unavailable_failures=3,
        hidden_at=datetime.now() - timedelta(minutes=5)))
    db.commit()


def _on(monkeypatch):
    monkeypatch.setenv("LIBRARY_SOURCE_HEALTH_ENABLED", "1")


# ---------- 总闸关 / 无记录默认可用 ----------

def test_gate_off_always_available(db, monkeypatch):
    _lib, _m, src, item = _setup(db)
    _hide(db, src)
    monkeypatch.delenv("LIBRARY_SOURCE_HEALTH_ENABLED", raising=False)
    assert psh.item_source_available(db, item) is True


def test_no_health_row_means_available(db, monkeypatch):
    _lib, _m, _src, item = _setup(db)
    _on(monkeypatch)
    assert psh.item_source_available(db, item) is True


def test_local_path_means_available(db, monkeypatch):
    lib, _m, src, _item = _setup(db)
    _hide(db, src)
    local = em.MediaItem(guid="item-local", library_id=lib.id,
                         item_type="movie", name="local",
                         file_path="/tmp/a.mkv")
    db.add(local)
    db.commit()
    _on(monkeypatch)
    assert psh.item_source_available(db, local) is True


# ---------- 隐藏源不可播；成功复位后可播 ----------

def test_hidden_source_not_available(db, monkeypatch):
    _lib, _m, src, item = _setup(db)
    _hide(db, src)
    _on(monkeypatch)
    assert psh.item_source_available(db, item) is False


def test_active_row_means_available(db, monkeypatch):
    _lib, _m, src, item = _setup(db)
    db.add(em.LibrarySourceHealth(
        library_source_id=src.id, availability_state="Active",
        consecutive_unavailable_failures=0,
        last_success_at=datetime.now()))
    db.commit()
    _on(monkeypatch)
    assert psh.item_source_available(db, item) is True


# ---------- 子目录前缀：子目录行优先于根行；无匹配不猜 ----------

def test_subpath_prefix_prefers_subdir_row(db, monkeypatch):
    lib, mount, _sub, item = _setup(db, "/Movies")
    root = em.LibrarySource(library_id=lib.id, mount_id=mount.id, sub_path="/")
    db.add(root)
    db.commit()
    _hide(db, root)  # 根行隐藏，子目录行无健康记录 → 条目仍可播
    _on(monkeypatch)
    assert psh.item_source_available(db, item) is True


def test_no_prefix_match_means_available(db, monkeypatch):
    lib, mount, _sub, _item = _setup(db, "/Movies")
    other = em.MediaItem(guid="item-other", library_id=lib.id,
                         item_type="movie", name="other",
                         file_path=f"mount://{mount.id}/Other/b.mkv")
    db.add(other)
    db.commit()
    _hide(db, _sub)
    _on(monkeypatch)
    assert psh.item_source_available(db, other) is True


# ---------- 试点范围：试点外不过滤 ----------

def test_outside_pilot_not_filtered(db, monkeypatch):
    _lib, _m, src, item = _setup(db)
    _hide(db, src)
    _on(monkeypatch)
    monkeypatch.setenv("LIBRARY_SOURCE_HEALTH_PILOT_LIBRARY_IDS", "999001")
    assert psh.item_source_available(db, item) is True


def test_inside_pilot_filtered(db, monkeypatch):
    lib, _m, src, item = _setup(db)
    _hide(db, src)
    _on(monkeypatch)
    monkeypatch.setenv("LIBRARY_SOURCE_HEALTH_PILOT_LIBRARY_IDS", str(lib.id))
    assert psh.item_source_available(db, item) is False


# ---------- 变体层过滤函数 ----------

def test_filter_variants():
    variants = ["a", "b"]
    assert psh.filter_variants(variants, True) == ["a", "b"]
    assert psh.filter_variants(variants, False) == []
