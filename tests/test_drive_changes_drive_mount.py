# -*- coding: utf-8 -*-
"""Tests: _library_drive_prefixes 认 drive: 直挂路径（实时入库断裂回归）。

根因：库路径 mount://<id>/<子目录> 的挂载 path 为 drive:<drive_id> 时，
旧实现只解析 rclone: 前缀，remote=None → drive_id=None → 全部库
不产出前缀，map_changes_to_libraries 恒返回 {}，Drive Changes 自
2026-10-17 迁直挂以来从未触发入库扫描。

注：本文件用内存 fake DB（不连 sqlite），只覆盖前缀推导与映射的
纯逻辑；CI 的 sqlite 环境同样可跑。
"""
import os

os.environ.setdefault("DATABASE_TYPE", "sqlite")
os.environ.setdefault("REDIS_ENABLED", "false")

import types
import uuid
from unittest import mock

from backend.emby_server import models as em
from backend.emby_server import drive_changes

TEAM_ID = "0AERUVu4kfWEfUk9PVA"


class _Q:
    """极简 query stub：只支持 == 过滤 + all()/first()。"""

    def __init__(self, rows):
        self._rows = list(rows)

    def filter(self, crit):
        try:
            col = crit.left.key
            right = crit.right
            val = right.value if hasattr(right, "value") else right
            rows = [r for r in self._rows if getattr(r, col, None) == val]
        except Exception:  # noqa: BLE001 — 兜底：不过滤
            rows = self._rows
        return _Q(rows)

    def all(self):
        return list(self._rows)

    def first(self):
        return self._rows[0] if self._rows else None


class _FakeDB:
    def __init__(self, libs, mounts):
        self._libs = libs
        self._mounts = mounts

    def query(self, model):
        if model is em.Library:
            return _Q(self._libs)
        if model is em.StorageMount:
            return _Q(self._mounts)
        raise AssertionError("unexpected model %r" % (model,))


def _lib(lib_id, paths):
    return types.SimpleNamespace(id=lib_id, guid=uuid.uuid4().hex,
                                 name="dcl_%s" % lib_id, paths=paths,
                                 is_enabled=True, is_virtual=False)


def _mount(mount_id, path, config="{}"):
    return types.SimpleNamespace(id=mount_id, name="dcm_%s" % mount_id,
                                 mount_type="drive", path=path, config=config,
                                 is_enabled=True)


def _discover(ret):
    return mock.patch.object(drive_changes, "discover_drives",
                             return_value=ret)


def test_drive_mount_shared_prefix():
    """drive:<共享盘ID> → 前缀直接产出 drive_id 与子目录。"""
    # mount id 取自库路径 mount://3
    db = _FakeDB([_lib(11, "mount://3/nastool/剧集/国产剧")],
                 [_mount(3, "drive:%s" % TEAM_ID)])
    with _discover({TEAM_ID: ["MP"]}):
        got = drive_changes._library_drive_prefixes(db)
    assert (11, TEAM_ID, "/nastool/剧集/国产剧") in got, got


def test_drive_mount_personal_prefix():
    """drive:myDrive:<remote> → drive_id 取冒号后整体。"""
    db = _FakeDB([_lib(12, "mount://4/剧集")],
                 [_mount(4, "drive:myDrive:paul_emby")])
    with _discover({"myDrive:paul_emby": ["paul_emby"]}):
        got = drive_changes._library_drive_prefixes(db)
    assert (12, "myDrive:paul_emby", "/剧集") in got, got


def test_drive_mount_config_drive_id_override():
    """config.drive_id 显式值优先于路径里的盘 ID（与 DriveMount 约定一致）。"""
    db = _FakeDB([_lib(13, "mount://5/Movies")],
                 [_mount(5, "drive:WRONGID",
                         '{"drive_id": "OVERRIDE123"}')])
    with _discover({"OVERRIDE123": ["MP"]}):
        got = drive_changes._library_drive_prefixes(db)
    assert (13, "OVERRIDE123", "/Movies") in got, got
    assert all(did != "WRONGID" for _, did, _ in got)


def test_drive_mount_end_to_end_mapping():
    """端到端：该目录下新视频文件的 fake change → 映射出库 id 与 fs 前缀。"""
    db = _FakeDB([_lib(14, "mount://6/nastool/剧集/国产剧")],
                 [_mount(6, "drive:%s" % TEAM_ID)])
    change = {"fileId": "fid_new_1",
              "file": {"name": "S01E01.mkv", "mimeType": "video/x-matroska",
                       "parents": ["p1"], "trashed": False}}
    with _discover({TEAM_ID: ["MP"]}), \
         mock.patch.object(drive_changes, "_get_token",
                           return_value="tok"), \
         mock.patch.object(drive_changes, "drive_path_of",
                           return_value="/nastool/剧集/国产剧/某剧/S01E01.mkv"):
        got = drive_changes.map_changes_to_libraries(db, TEAM_ID, [change])
    assert got == {14: {"mount://6/nastool/剧集/国产剧/某剧"}}, got


def test_drive_mount_empty_path_skipped():
    """drive: 后面空 → 不产出前缀（不抛错）。"""
    db = _FakeDB([_lib(15, "mount://7/x")], [_mount(7, "drive:")])
    with _discover({}):
        got = drive_changes._library_drive_prefixes(db)
    assert not [t for t in got if t[0] == 15], got


def test_rclone_branch_unchanged():
    """rclone: 分支行为不变（回归守卫）。"""
    db = _FakeDB([_lib(16, "mount://8/y")], [_mount(8, "rclone:MP:/x")])
    with _discover({"D1": ["MP"]}):
        got = drive_changes._library_drive_prefixes(db)
    assert (16, "D1", "/y") in got, got
