# -*- coding: utf-8 -*-
"""A-095 回归：FUSE 挂载点 -> rclone remote 映射只许一份定义。

审计 R30-06 §A-095：drive_changes.py 的 _library_drive_prefixes 与
_library_fs_prefixes 各写死一份字面 fuse_map（同一部署值两份定义，
改一处漏一处即口径漂移）；且挂载点不在表内的 FUSE 路径库静默产空、
永不触发扫描，无任何日志。

定稿口径（本文件锁定）：
- 映射单一来源 `_fuse_remote_map(db)`：优先从 StorageMount 行派生
  （rclone 挂载行 config 声明 fuse_mount_point），派生不出回落模块级
  单一历史常量，派生值覆盖同名常量（部署数据说了算）；
- 表内既有两条历史映射行为逐字不变；
- 未知挂载点记 warning（带库 id 与路径），不许静默产空。
"""
import os

os.environ.setdefault("DATABASE_TYPE", "sqlite")
os.environ.setdefault("REDIS_ENABLED", "false")

import json
import logging
import uuid
from pathlib import Path
from unittest import mock

from backend.database import SessionLocal, init_db
from backend.emby_server import models as em
from backend.emby_server import drive_changes

init_db()

_DRIVES = {"DRIVE123": ["MP"], "DRIVE999": ["paul_emby"]}


def _guid():
    return uuid.uuid4().hex


def _make_lib(db, paths):
    lib = em.Library(guid=_guid(), name="a095_%s" % _guid()[:8],
                     collection_type="movies", paths=paths)
    db.add(lib)
    db.commit()
    db.refresh(lib)
    return lib


def _make_mount(db, path, config):
    m = em.StorageMount(name="a095_%s" % _guid()[:8], mount_type="rclone",
                        path=path, config=json.dumps(config),
                        is_enabled=True)
    db.add(m)
    db.commit()
    db.refresh(m)
    return m


def _cleanup(db, *objs):
    for obj in objs:
        if obj is not None:
            db.delete(obj)
    db.commit()


def _prefixes_for(db, lib_id):
    return [row for row in drive_changes._library_drive_prefixes(db)
            if row[0] == lib_id]


# ---------------------------------------------------------------------------
# 静态：字面映射只许一份定义
# ---------------------------------------------------------------------------

def test_fuse_map_literal_defined_once():
    src = Path(drive_changes.__file__).read_text(encoding="utf-8")
    # 历史映射的每个条目字面在生产文件里只能出现一次（单一常量定义处）
    assert src.count('"/mnt/mp": "MP:"') == 1
    assert src.count('"/mnt/paul": "paul_emby:"') == 1
    # 两处前缀推导共用同一个映射来源，不许再各写一份
    assert src.count("_fuse_remote_map(db)") >= 2


# ---------------------------------------------------------------------------
# 行为不变：表内既有两条历史映射照常工作（两函数口径一致）
# ---------------------------------------------------------------------------

def test_legacy_mp_path_maps_in_both_functions():
    db = SessionLocal()
    lib = _make_lib(db, "/mnt/mp/nastool/剧集")
    try:
        with mock.patch.object(drive_changes, "discover_drives",
                               return_value=_DRIVES):
            assert (lib.id, "DRIVE123", "/nastool/剧集") in \
                _prefixes_for(db, lib.id)
            fs = drive_changes._library_fs_prefixes(db)
        assert fs.get((lib.id, "/nastool/剧集")) == "/mnt/mp/nastool/剧集"
    finally:
        _cleanup(db, lib)
        db.close()


def test_legacy_paul_path_maps_in_both_functions():
    db = SessionLocal()
    lib = _make_lib(db, "/mnt/paul/video/电影")
    try:
        with mock.patch.object(drive_changes, "discover_drives",
                               return_value=_DRIVES):
            assert (lib.id, "DRIVE999", "/video/电影") in \
                _prefixes_for(db, lib.id)
            fs = drive_changes._library_fs_prefixes(db)
        assert fs.get((lib.id, "/video/电影")) == "/mnt/paul/video/电影"
    finally:
        _cleanup(db, lib)
        db.close()


# ---------------------------------------------------------------------------
# 未知挂载点：记 warning，不许静默产空
# ---------------------------------------------------------------------------

def test_unknown_fuse_prefix_warns_not_silent(caplog):
    db = SessionLocal()
    uniq = "/mnt/unk%s" % _guid()[:8]
    lib = _make_lib(db, uniq + "/剧集")
    try:
        with mock.patch.object(drive_changes, "discover_drives",
                               return_value=_DRIVES):
            with caplog.at_level(logging.WARNING,
                                 logger="backend.emby_server.drive_changes"):
                assert _prefixes_for(db, lib.id) == []
                fs = drive_changes._library_fs_prefixes(db)
        assert (lib.id, "/剧集") not in fs
        hits = [r for r in caplog.records
                if r.name == "backend.emby_server.drive_changes"
                and r.levelno >= logging.WARNING
                and uniq in r.getMessage()]
        assert hits, "未知 FUSE 挂载点必须记 warning，不许静默产空"
    finally:
        _cleanup(db, lib)
        db.close()


# ---------------------------------------------------------------------------
# 派生：映射从 StorageMount 行来（数据驱动），且覆盖同名历史常量
# ---------------------------------------------------------------------------

def test_fuse_map_derived_from_storage_mount_row():
    db = SessionLocal()
    lib = _make_lib(db, "/mnt/newr/电影")
    mount = _make_mount(db, "rclone:NEWR:/media",
                        {"fuse_mount_point": "/mnt/newr"})
    try:
        drives = dict(_DRIVES)
        drives["DRIVE777"] = ["NEWR"]
        with mock.patch.object(drive_changes, "discover_drives",
                               return_value=drives):
            assert (lib.id, "DRIVE777", "/电影") in _prefixes_for(db, lib.id)
            fs = drive_changes._library_fs_prefixes(db)
        assert fs.get((lib.id, "/电影")) == "/mnt/newr/电影"
    finally:
        _cleanup(db, lib, mount)
        db.close()


def test_derived_mapping_overrides_legacy_constant():
    db = SessionLocal()
    lib = _make_lib(db, "/mnt/mp/nastool/剧集")
    mount = _make_mount(db, "rclone:OTHER:/x",
                        {"fuse_mount_point": "/mnt/mp"})
    try:
        drives = dict(_DRIVES)
        drives["DRIVE555"] = ["OTHER"]
        with mock.patch.object(drive_changes, "discover_drives",
                               return_value=drives):
            got = _prefixes_for(db, lib.id)
        # 部署数据（挂载行声明）说了算：走 OTHER -> DRIVE555，
        # 不再走历史常量的 MP -> DRIVE123
        assert (lib.id, "DRIVE555", "/nastool/剧集") in got
        assert (lib.id, "DRIVE123", "/nastool/剧集") not in got
    finally:
        _cleanup(db, lib, mount)
        db.close()
