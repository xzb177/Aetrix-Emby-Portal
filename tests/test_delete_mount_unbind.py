"""删挂载解绑不完整（360 审计 A-022）回归：delete_mount 必须同步清掉
paths 里的 mount:// 引用、mount_ids 列以及新表来源行，否则扫描会继续
信任死源并记 failed、保存会 400、计数会误报。
"""

import os

os.environ.setdefault("DATABASE_TYPE", "sqlite")
os.environ.setdefault("REDIS_ENABLED", "false")

from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend import models as base_models
from backend.emby_server import models as em
from backend.emby_server import mount_sources as ms
from backend.emby_server import portal_mount_routes as pmr


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


def _lib(db, guid, paths="", mount_ids=""):
    lib = em.Library(guid=guid, name=guid, paths=paths, mount_ids=mount_ids)
    db.add(lib)
    db.commit()
    return lib


def _mount(db, name, mount_type="local", path=""):
    mount = em.StorageMount(name=name, mount_type=mount_type, path=path,
                            config="{}", is_enabled=True)
    db.add(mount)
    db.commit()
    db.refresh(mount)
    return mount


def _row(db, lib, mount_id, sub_path, enabled=True):
    row = em.LibrarySource(library_id=lib.id, mount_id=mount_id,
                           sub_path=sub_path, is_enabled=enabled)
    db.add(row)
    db.commit()
    return row


def _rows_for(db, lib, mount_id):
    return db.query(em.LibrarySource).filter(
        em.LibrarySource.library_id == lib.id,
        em.LibrarySource.mount_id == mount_id).all()


def test_paths_only_library_unbound_and_rows_removed(db):
    """paths-only 的库：旧代码完全漏网（计数 0、paths 原样保留）。"""
    m = _mount(db, "m5")
    lib = _lib(db, "lib-paths-only",
               paths=f"mount://{m.id}/电影", mount_ids="")
    ms.write_library_sources(db, lib, [f"mount://{m.id}/电影"], [])
    db.commit()
    db.refresh(lib)
    assert _rows_for(db, lib, m.id), "前置：新表应有该挂载行"
    out = pmr.delete_mount(m.id, staff=SimpleNamespace(), db=db)
    assert out == {"success": True, "unbound_libraries": 1}
    db.refresh(lib)
    assert f"mount://{m.id}/" not in (lib.paths or "")
    assert _rows_for(db, lib, m.id) == []


def test_both_columns_library_cleared(db):
    """paths + mount_ids 双列并存的库：两列都要清，新表行也要删。"""
    m = _mount(db, "m5")
    lib = _lib(db, "lib-both",
               paths=f"mount://{m.id}/电影,/keep", mount_ids=str(m.id))
    _row(db, lib, m.id, "/电影")
    out = pmr.delete_mount(m.id, staff=SimpleNamespace(), db=db)
    assert out["unbound_libraries"] == 1
    db.refresh(lib)
    assert f"mount://{m.id}/" not in (lib.paths or "")
    assert "/keep" in (lib.paths or "")
    assert lib.mount_ids == ""
    assert _rows_for(db, lib, m.id) == []


def test_unbound_count_counts_all_forms_and_leaves_other_mount(db):
    """计数：mount_ids-only、paths-only、双列并存各计 1，其他挂载不动。"""
    m5 = _mount(db, "m5")
    m6 = _mount(db, "m6")
    lib_a = _lib(db, "lib-ids-only", paths="", mount_ids=str(m5.id))
    lib_b = _lib(db, "lib-paths-only",
                 paths=f"mount://{m5.id}/电影", mount_ids="")
    ms.write_library_sources(db, lib_b, [f"mount://{m5.id}/电影"], [])
    db.commit()
    lib_c = _lib(db, "lib-both",
                 paths=f"mount://{m5.id}/电影", mount_ids=str(m5.id))
    lib_other = _lib(db, "lib-other",
                     paths=f"mount://{m6.id}/剧集", mount_ids="")
    out = pmr.delete_mount(m5.id, staff=SimpleNamespace(), db=db)
    assert out["unbound_libraries"] == 3
    db.refresh(lib_a)
    db.refresh(lib_other)
    assert lib_a.mount_ids == ""
    assert f"mount://{m6.id}/" in (lib_other.paths or "")


def test_row_only_library_unbound_and_row_removed(db):
    """新表-only 的库：旧列完全没提该挂载、只有新表有行，也要计数并清行。"""
    m = _mount(db, "m5")
    lib = _lib(db, "lib-row-only", paths="", mount_ids="")
    _row(db, lib, m.id, "/电影")
    out = pmr.delete_mount(m.id, staff=SimpleNamespace(), db=db)
    assert out["unbound_libraries"] == 1
    assert _rows_for(db, lib, m.id) == []


def test_after_delete_source_resolution_has_no_dead_mount_failed(db, tmp_path):
    """删挂载后该库的来源解析不再出现死挂载 failed（清理不再被挡住）。"""
    real = tmp_path / "real"
    real.mkdir()
    m = _mount(db, "m5")
    lib = _lib(db, "lib-scan",
               paths=f"{real},mount://{m.id}/电影", mount_ids="")
    ms.write_library_sources(db, lib, [str(real), f"mount://{m.id}/电影"], [])
    db.commit()
    out = pmr.delete_mount(m.id, staff=SimpleNamespace(), db=db)
    assert out["unbound_libraries"] == 1
    db.refresh(lib)
    sources, failed = ms.read_library_sources(db, lib)
    assert all(f"mount://{m.id}" not in (f.get("label") or "") for f in failed)
    assert all("不存在" not in (f.get("reason") or "") for f in failed)
    assert any(s.kind == "local" for s in sources)
