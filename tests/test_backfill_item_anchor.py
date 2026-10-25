"""存量身份锚回填（断旧路前置）：推导规则与只填空幂等。

覆盖：
- source_id 推导：精确子目录、最长前缀胜出、根 "/" 全匹配、
  库不一致不取别库行、全停用行不上报为可映射、非 mount:// 路径、
  尾斜杠 sub_path 归一化；
- provider_file_id 推导：Drive/rclone 取 drive_file_id、115 不推导、
  空值留空、未知类型不推导；
- 回填：dry-run 不写库、apply 只填空不覆盖、重跑全跳过、
  软删除/合并行排除、非 mount:// 行不动。
"""

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend import models as base_models
from backend.emby_server import models as em
from scripts.backfill_item_anchor import backfill_item_anchor, derive_anchor


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


ROWS = [
    {"id": 10, "library_id": 1, "mount_id": 3, "sub_path": "/", "is_enabled": True},
    {"id": 11, "library_id": 1, "mount_id": 3, "sub_path": "/Movies", "is_enabled": True},
    {"id": 12, "library_id": 2, "mount_id": 3, "sub_path": "/Series", "is_enabled": True},
    {"id": 13, "library_id": 1, "mount_id": 3, "sub_path": "/Old", "is_enabled": False},
]
TYPES = {3: "drive"}


def test_derive_longest_prefix_wins():
    got = derive_anchor("mount://3/Movies/a.mkv", 1, "fid-1", ROWS, TYPES)
    assert got["source_id"] == 11 and got["matched_row"]["sub_path"] == "/Movies"
    assert got["provider_file_id"] == "fid-1"


def test_derive_root_matches_all():
    got = derive_anchor("mount://3/Other/b.mkv", 1, "fid-2", ROWS, TYPES)
    assert got["source_id"] == 10


def test_derive_library_mismatch_not_taken():
    got = derive_anchor("mount://3/Series/s01e01.mkv", 1, "fid-3", ROWS, TYPES)
    # /Series 行属于 2 库，1 库条目不取它，只剩根行可用。
    assert got["source_id"] == 10
    got2 = derive_anchor("mount://3/Series/s01e01.mkv", 9, "fid-3", ROWS, TYPES)
    assert got2["source_id"] is None and got2["source_reason"] == "library_mismatch"


def test_derive_disabled_only():
    got = derive_anchor("mount://3/Old/x.mkv", 1, "fid-4", ROWS, TYPES)
    # /Old 行停用，根行仍可用 → 取根行，不算停用。
    assert got["source_id"] == 10
    rows = [dict(r, is_enabled=False) if r["id"] == 10 else r for r in ROWS]
    got2 = derive_anchor("mount://3/Other/y.mkv", 1, "fid-4", rows, TYPES)
    assert got2["source_id"] is None and got2["source_reason"] == "source_disabled"


def test_derive_no_source_row_and_bad_paths():
    assert derive_anchor("mount://9/x.mkv", 1, "f", ROWS, TYPES)["source_reason"] == "no_source_row"
    assert derive_anchor("/local/x.mkv", 1, "f", ROWS, TYPES)["source_reason"] == "not_mount_path"
    assert derive_anchor("mount://abc/x.mkv", 1, "f", ROWS, TYPES)["source_reason"] == "not_mount_path"
    assert derive_anchor(None, 1, "f", ROWS, TYPES)["source_reason"] == "not_mount_path"


def test_derive_trailing_slash_subpath():
    rows = [{"id": 20, "library_id": 1, "mount_id": 3,
             "sub_path": "/Movies/", "is_enabled": True}]
    got = derive_anchor("mount://3/Movies/a.mkv", 1, "f", rows, TYPES)
    assert got["source_id"] == 20
    got2 = derive_anchor("mount://3/MoviesExtra/a.mkv", 1, "f", rows, TYPES)
    assert got2["source_id"] is None  # 目录边界：MoviesExtra 不是 Movies 之下


def test_derive_fid_rules():
    assert derive_anchor("mount://3/a.mkv", 1, "fid-9", ROWS, {3: "rclone"})["provider_file_id"] == "fid-9"
    got = derive_anchor("mount://3/a.mkv", 1, "fid-9", ROWS, {3: "115"})
    assert got["provider_file_id"] is None and got["fid_reason"] == "fid_115_unresolved"
    got = derive_anchor("mount://3/a.mkv", 1, "", ROWS, TYPES)
    assert got["provider_file_id"] is None and got["fid_reason"] == "fid_empty"
    got = derive_anchor("mount://3/a.mkv", 1, "fid-9", ROWS, {3: "weird"})
    assert got["provider_file_id"] is None and got["fid_reason"] == "fid_type_unknown"


def _lib(db, guid):
    lib = em.Library(guid=guid, name=guid)
    db.add(lib)
    db.commit()
    return lib


def _mount(db, name, mount_type="drive"):
    mount = em.StorageMount(name=name, mount_type=mount_type, path="drive:x",
                            is_enabled=True)
    db.add(mount)
    db.commit()
    return mount


def _source(db, lib_id, mount_id, sub_path, enabled=True):
    row = em.LibrarySource(library_id=lib_id, mount_id=mount_id,
                           sub_path=sub_path, is_enabled=enabled)
    db.add(row)
    db.commit()
    return row


def _item(db, lib_id, path, fid=None, source_id=None, provider_file_id=None,
          item_type="movie", deleted=False, merged=None):
    item = em.MediaItem(guid=f"g-{lib_id}-{path}", library_id=lib_id,
                        item_type=item_type, name="t", file_path=path,
                        drive_file_id=fid, source_id=source_id,
                        provider_file_id=provider_file_id)
    if deleted:
        item.deleted_at = __import__("datetime").datetime(2025, 1, 1)
    if merged is not None:
        item.merged_into_id = merged
    db.add(item)
    db.commit()
    return item


def test_backfill_dry_run_writes_nothing(db):
    lib = _lib(db, "lib-bf")
    mount = _mount(db, "m3")
    src = _source(db, lib.id, mount.id, "/")
    item = _item(db, lib.id, f"mount://{mount.id}/a.mkv", fid="fid-a")
    stats = backfill_item_anchor(db, dry_run=True)
    assert stats["scanned"] == 1 and stats["both_filled"] == 1
    db.refresh(item)
    assert item.source_id is None and item.provider_file_id is None


def test_backfill_apply_fill_empty_only_and_idempotent(db):
    lib = _lib(db, "lib-bf2")
    mount = _mount(db, "m3")
    src = _source(db, lib.id, mount.id, "/")
    keep = _item(db, lib.id, f"mount://{mount.id}/keep.mkv", fid="fid-k",
                 source_id=999)
    partial = _item(db, lib.id, f"mount://{mount.id}/p.mkv", fid="fid-p",
                    provider_file_id="old-fid")
    full = _item(db, lib.id, f"mount://{mount.id}/f.mkv", fid="fid-f",
                 source_id=src.id, provider_file_id="fid-f")
    stats = backfill_item_anchor(db, dry_run=False)
    assert stats["both_filled"] == 0 and stats["fid_only"] == 1
    assert stats["source_only"] == 1  # keep 已有 source 只补 fid；partial 已有 fid 只补 source
    db.refresh(keep)
    db.refresh(partial)
    assert keep.source_id == 999 and keep.provider_file_id == "fid-k"
    assert partial.source_id == src.id and partial.provider_file_id == "old-fid"
    stats2 = backfill_item_anchor(db, dry_run=False)
    assert stats2["skipped_complete"] == 3 and stats2["unmapped"] == 0


def test_backfill_excludes_deleted_merged_and_local(db):
    lib = _lib(db, "lib-bf3")
    mount = _mount(db, "m3")
    _source(db, lib.id, mount.id, "/")
    _item(db, lib.id, f"mount://{mount.id}/gone.mkv", fid="fid-g", deleted=True)
    _item(db, lib.id, f"mount://{mount.id}/dup.mkv", fid="fid-d", merged=1)
    _item(db, lib.id, "/local/x.mkv", fid="fid-l")
    stats = backfill_item_anchor(db, dry_run=True)
    assert stats["scanned"] == 0


def test_backfill_structural_row_gets_source_only(db):
    lib = _lib(db, "lib-bf4")
    mount = _mount(db, "m3")
    src = _source(db, lib.id, mount.id, "/Series")
    series = _item(db, lib.id, f"mount://{mount.id}/Series", item_type="series")
    stats = backfill_item_anchor(db, dry_run=False)
    assert stats["source_only"] == 1
    db.refresh(series)
    assert series.source_id == src.id and series.provider_file_id is None
