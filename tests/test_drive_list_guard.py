"""Drive files.list 字段守卫（L62 兜底）：挂载 3 etag 中断回归测试。

2026-10-25：#1247 新加 etag 字段，致共享盘根列举回 400、5 个库扫描
1 秒即 partial；#1258 只删 etag 修复。本文件钉住两点：
- 列目录字段白名单不含 etag（解析 files(...) 内字段令牌逐个比对，
  不做子串匹配，避免误伤其它字段名）；
- 共享盘根金丝雀探活只发一页只读列举，字段问题在盘根立刻暴露，
  不写 Drive、不落缓存。
不碰网络、不碰数据库：drive_changes 的入口在本文件里被替换成假实现。
"""
import os

# 与 test_drive_mount.py 同款：必须在 import backend 之前钉住 sqlite，
# 否则本文件若先于其他测试被收集，backend.database 会按默认 PG 建引擎，
# 连累后面靠 setdefault 切 sqlite 的测试文件（进程级状态污染）。
os.environ.setdefault("DATABASE_TYPE", "sqlite")
os.environ.setdefault("REDIS_ENABLED", "false")

from types import SimpleNamespace

import pytest

from backend.emby_server import drive_changes
from backend.emby_server import mount_drive  # noqa: F401 — 导入即注册 drive 提供者
from backend.emby_server import mounts as mnt
from backend.emby_server.mount_drive import DriveMount
from backend.emby_server.mounts import MountError

DRIVE_ID = "0AERUVu4kfWEfUk9PVA"
CALLS: list[dict] = []


def _fake_api_get(url, token, params):
    CALLS.append(dict(params))
    return {"files": [{"id": "F1", "name": "根目录电影.mkv",
                       "mimeType": "video/x-matroska"}]}


def _fake_token(drive_id, force_refresh=False):
    return "tok-test"


@pytest.fixture(autouse=True)
def _fake_drive(monkeypatch):
    CALLS.clear()
    mnt.invalidate_list_cache()
    monkeypatch.setattr(drive_changes, "_api_get", _fake_api_get)
    monkeypatch.setattr(drive_changes, "get_token_for_drive", _fake_token)
    yield
    mnt.invalidate_list_cache()


def _mount(path=f"drive:{DRIVE_ID}", config=None):
    return SimpleNamespace(
        id=99, name="gdrive-generic", mount_type="drive",
        path=path, config=config if config is not None else {},
    )


def _fields_tokens():
    raw = mount_drive._LIST_FIELDS
    inner = raw.split("files(", 1)[1].rsplit(")", 1)[0]
    return [t.strip() for t in inner.split(",") if t.strip()]


def test_list_fields_exclude_etag():
    tokens = _fields_tokens()
    assert "etag" not in tokens
    # 白名单逐项钉死：以后加字段必须同步改本断言，不许悄悄加回 etag。
    assert tokens == ["id", "name", "mimeType", "size", "modifiedTime", "trashed",
                      "md5Checksum", "headRevisionId", "version"]


def test_list_page_sends_whitelisted_fields():
    provider = DriveMount(_mount())
    provider._list_page(provider.root_id, None)
    assert len(CALLS) == 1
    assert CALLS[0]["fields"] == mount_drive._LIST_FIELDS
    assert CALLS[0]["q"] == f"'{DRIVE_ID}' in parents and trashed = false"
    assert CALLS[0]["driveId"] == DRIVE_ID
    assert CALLS[0]["corpora"] == "drive"


def test_canary_probe_root_ok_single_read():
    provider = DriveMount(_mount())
    result = provider.canary_probe_root()
    assert result["ok"] is True
    assert result["fields"] == mount_drive._LIST_FIELDS
    assert result["root_entries"] == 1
    assert len(CALLS) == 1  # 只发一页，不翻页、不走 list_dir 缓存


def test_canary_probe_root_failure_returns_not_raise(monkeypatch):
    def _boom(url, token, params):
        raise MountError("Drive 列举失败: 400")

    monkeypatch.setattr(drive_changes, "_api_get", _boom)
    provider = DriveMount(_mount())
    result = provider.canary_probe_root()
    assert result["ok"] is False
    assert "400" in result["error"]
    assert result["fields"] == mount_drive._LIST_FIELDS
