"""按源维度的目录扫描快照（任务清单第 11 项：扫描目录指纹）

钉住的行为：
- 快照判定：first / changed（指纹对不上，或项数对不上）/ due / not_due；
  只有指纹与项数都对上、且未到期，才算 not_due（整段跳过）。
- 源解析：挂载目录按同 mount_id 最长前缀匹配；本机目录按空 mount_id
  最长前缀匹配；解析不到回 None（调用方走旧 ScanDirState 路径）。
- 写回语义：first/changed/due 推进指纹、项数、last_scan_at、
  last_refresh_at、next_due_at；not_due 只推进 last_scan_at，
  next_due_at 与 last_refresh_at 保持原值（不许越推越远）。
- 源行重插认领：管理端保存库删掉源行重插后，同库同路径的旧快照被
  认领到新源行，不另起一行、不丢快照。
- 与缺失宽限（批一 #6）的顺序不许反：快照判定为 changed 只影响处理
  深度，不直接删除任何条目；清理仍走 missing_count 宽限（默认 3 轮
  才下架），本轮缺失只累计计数。
"""

import os
import tempfile
import uuid
from datetime import datetime, timedelta
from types import SimpleNamespace

os.environ.setdefault("DATABASE_TYPE", "sqlite")
os.environ.setdefault("REDIS_ENABLED", "false")
_fd, _tmppath = tempfile.mkstemp(suffix=".db")
os.close(_fd)
os.environ["DATABASE_URL"] = f"sqlite:///{_tmppath}"
from backend import database as _dbmod
from sqlalchemy import create_engine as _ce
from sqlalchemy.orm import sessionmaker as _sm
_dbmod.engine = _ce(os.environ["DATABASE_URL"])
_dbmod.configure_session_local(_sm(bind=_dbmod.engine))

import pytest

from backend.database import SessionLocal, init_db
from backend.emby_server import models as em
from backend.emby_server import scan_snapshots
from backend.emby_server import scanner

init_db()


@pytest.fixture()
def db():
    s = SessionLocal()
    try:
        yield s
    finally:
        s.close()


def _guid():
    return uuid.uuid4().hex


def _ctx(snap=None, stats=None):
    if snap is None:
        snap = SimpleNamespace(collection_type="movies", scrape_policy="smart",
                               targeted=False, force_full=False)
    return scanner._ScanContext(snap=snap, lib_id=1, stats=stats if stats is not None else {})


def _lib(db):
    lib = em.Library(guid=_guid(), name="t", collection_type="movies")
    db.add(lib)
    db.flush()
    return lib


def _source(db, lib_id, mount_id, sub_path):
    row = em.LibrarySource(library_id=lib_id, mount_id=mount_id, sub_path=sub_path)
    db.add(row)
    db.flush()
    return row


# ==================== 快照判定 ====================

def test_snapshot_decision_matrix():
    now = datetime.now()
    future = now + timedelta(hours=1)
    past = now - timedelta(hours=1)
    assert scan_snapshots.decide_snapshot(None, "v2:a", 3, now) == "first"
    assert scan_snapshots.decide_snapshot(("v2:a", 3, future), "v2:b", 3, now) == "changed"
    # 项数对不上也算 changed：指纹再像也不能跳过
    assert scan_snapshots.decide_snapshot(("v2:a", 3, future), "v2:a", 4, now) == "changed"
    assert scan_snapshots.decide_snapshot(("v2:a", 3, future), "v2:a", 3, now) == "not_due"
    assert scan_snapshots.decide_snapshot(("v2:a", 3, past), "v2:a", 3, now) == "due"
    assert scan_snapshots.decide_snapshot(("v2:a", 3, None), "v2:a", 3, now) == "due"
    # 项数拿不到时退回指纹单一路（不因为 count=None 误判 changed）
    assert scan_snapshots.decide_snapshot(("v2:a", 3, future), "v2:a", None, now) == "not_due"


def test_dir_decision_uses_snapshot_count(monkeypatch):
    monkeypatch.setenv("SCAN_DIR_DUE_SCHEDULING", "1")
    future = datetime.now() + timedelta(hours=1)
    # 快照 3 元组：项数对不上 → changed（旧 2 元组行为逐字保留）
    assert scanner._dir_decision(_ctx(), "/s1", "v2:a", ("v2:a", future, 3),
                                 entry_count=4) == "changed"
    assert scanner._dir_decision(_ctx(), "/s2", "v2:a", ("v2:a", future, 3),
                                 entry_count=3) == "not_due"
    assert scanner._dir_decision(_ctx(), "/s3", "v2:a", ("v2:a", future)) == "not_due"


# ==================== 源解析 ====================

def test_resolve_source_id_longest_prefix():
    rows = [(1, 3, "/"), (2, 3, "/A"), (3, None, "/data")]
    assert scan_snapshots.resolve_source_id(rows, "mount://3/A/B") == 2
    assert scan_snapshots.resolve_source_id(rows, "mount://3/C") == 1
    assert scan_snapshots.resolve_source_id(rows, "mount://9/X") is None
    assert scan_snapshots.resolve_source_id(rows, "/data/lib/f") == 3
    assert scan_snapshots.resolve_source_id(rows, "/other/f") is None
    # 按段匹配：/A 不许匹配 /AB
    assert scan_snapshots.resolve_source_id(rows, "mount://3/AB") == 1
    assert scan_snapshots.resolve_source_id(rows, "mount://xx/A") is None


# ==================== 写回语义与认领 ====================

def test_store_snapshot_semantics(db):
    lib = _lib(db)
    src = _source(db, lib.id, 3, "/")
    ctx = _ctx()
    ctx.lib_id = lib.id
    now = datetime.now()
    due = now + timedelta(days=1)
    # first：全推进
    ctx.dirty_snapshots["mount://3/A"] = (src.id, "v2:a", 3, "first", due)
    scanner._store_snapshots(db, ctx)
    db.commit()
    row = db.query(em.DirectoryScanSnapshot).filter_by(
        library_id=lib.id, directory_path="mount://3/A").one()
    assert row.listing_fingerprint == "v2:a" and row.entry_count == 3
    assert row.last_refresh_at is not None and row.next_due_at == due
    assert row.library_source_id == src.id
    # not_due：只推进 last_scan_at，排期与刷新时刻保持原值
    ctx.dirty_snapshots["mount://3/A"] = (src.id, "v2:a", 3, "not_due", due)
    scanner._store_snapshots(db, ctx)
    db.commit()
    row = db.query(em.DirectoryScanSnapshot).filter_by(
        library_id=lib.id, directory_path="mount://3/A").one()
    assert row.next_due_at == due and row.last_refresh_at is not None
    # changed：全推进（指纹与项数都换）
    due2 = due + timedelta(days=1)
    ctx.dirty_snapshots["mount://3/A"] = (src.id, "v2:b", 4, "changed", due2)
    scanner._store_snapshots(db, ctx)
    db.commit()
    row = db.query(em.DirectoryScanSnapshot).filter_by(
        library_id=lib.id, directory_path="mount://3/A").one()
    assert row.listing_fingerprint == "v2:b" and row.entry_count == 4
    assert row.next_due_at == due2


def test_snapshot_adopted_after_source_rows_recreated(db):
    lib = _lib(db)
    src = _source(db, lib.id, 3, "/A")
    now = datetime.now()
    due = now + timedelta(days=1)
    scan_snapshots.store_snapshots(
        db, lib.id, {"mount://3/A": (src.id, "v2:a", 2, "first", due)}, now)
    db.commit()
    # 管理端保存库：删掉源行重插（源 id 变了，路径不变）
    db.query(em.LibrarySource).filter_by(library_id=lib.id).delete()
    db.flush()
    src2 = _source(db, lib.id, 3, "/A")
    db.commit()
    # 下一轮写回同一路径：旧快照被认领到新源行，不另起一行
    scan_snapshots.store_snapshots(
        db, lib.id, {"mount://3/A": (src2.id, "v2:a", 2, "not_due", due)}, now)
    db.commit()
    rows = db.query(em.DirectoryScanSnapshot).filter_by(
        library_id=lib.id, directory_path="mount://3/A").all()
    assert len(rows) == 1 and rows[0].library_source_id == src2.id


def test_load_snapshot_states_prefers_snapshot_over_legacy(db):
    lib = _lib(db)
    src = _source(db, lib.id, 3, "/")
    now = datetime.now()
    due = now + timedelta(days=1)
    scan_snapshots.store_snapshots(
        db, lib.id, {"mount://3/A": (src.id, "v2:snap", 5, "first", due)}, now)
    db.commit()
    ctx = _ctx()
    ctx.lib_id = lib.id
    scanner._load_snapshot_states(db, ctx, ["mount://3/A", "mount://9/B"])
    assert ctx.snapshot_resolved == {"mount://3/A": src.id}
    assert ctx.snapshot_states["mount://3/A"] == ("v2:snap", due, 5)
    assert "mount://9/B" not in ctx.snapshot_resolved


# ==================== 缺失宽限：changed 不直接删 ====================

def test_changed_snapshot_does_not_delete_items(db, monkeypatch):
    monkeypatch.setattr(scanner, "MISSING_GRACE_SCANS", 3)
    lib = _lib(db)
    item_seen = em.MediaItem(guid=_guid(), library_id=lib.id, item_type="movie",
                             name="seen", file_path="/data/seen.mkv")
    item_gone = em.MediaItem(guid=_guid(), library_id=lib.id, item_type="movie",
                             name="gone", file_path="/data/gone.mkv")
    db.add_all([item_seen, item_gone])
    db.commit()
    # 快照判定 changed（目录清单变了），但清理仍走宽限：本轮只累计计数
    assert scan_snapshots.decide_snapshot(("v2:a", 2, datetime.now() + timedelta(days=1)),
                                          "v2:b", 1, datetime.now()) == "changed"
    removed = scanner._remove_missing_items(db, lib, {item_seen.guid})
    db.commit()
    assert removed == 0
    gone = db.query(em.MediaItem).filter_by(id=item_gone.id).one()
    assert gone.missing_count == 1 and gone.deleted_at is None
    seen = db.query(em.MediaItem).filter_by(id=item_seen.id).one()
    assert seen.missing_count == 0
