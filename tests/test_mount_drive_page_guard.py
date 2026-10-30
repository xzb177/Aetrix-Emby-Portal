# -*- coding: utf-8 -*-
"""Tests for DriveMount._list_all pagination guard (audit R6 A-021)."""
import os

os.environ.setdefault("DATABASE_TYPE", "sqlite")
os.environ.setdefault("REDIS_ENABLED", "false")

from unittest import mock

from backend.database import init_db
from backend.emby_server import drive_changes, mount_drive
from backend.emby_server.mounts import MountError

init_db()


def _mount_with_stubbed_pages(page_fn):
    """DriveMount 实例（绕过 __init__），_pin_listing_cred 与 _list_page
    均用桩；page_fn(count) -> (body, token)。"""
    mount = object.__new__(mount_drive.DriveMount)
    calls = {"n": 0}

    def fake_list_page(folder_id, page_token, token=None, sa=None):
        calls["n"] += 1
        return page_fn(calls["n"])

    mount._pin_listing_cred = lambda: ("tok", None)
    mount._list_page = fake_list_page
    return mount, calls


def test_list_all_stuck_page_token_raises_mount_error():
    """A-021：_list_page 恒回同一 nextPageToken → 第二页即判定卡住，
    抛 MountError（旧代码无护栏，会一直翻页直到桩的 cap 页后回终页）。"""
    def page_fn(n):
        if n > 10:
            return {"files": [], "nextPageToken": None}, "tok"
        return {"files": [{"id": "f%d" % n}], "nextPageToken": "stuck"}, "tok"

    mount, calls = _mount_with_stubbed_pages(page_fn)
    try:
        mount._list_all("folder1")
    except MountError:
        pass
    else:
        raise AssertionError("卡住的 nextPageToken 必须停表抛 MountError")
    assert calls["n"] == 2, calls


def test_list_all_total_limit_stops():
    """A-021：总量上限先到先停（上限压到 2 条，每页 2 条，第一页即超，
    且还有下一页时必须抛错而非静默截断）。"""
    def page_fn(n):
        return {"files": [{"id": "a"}, {"id": "b"}],
                "nextPageToken": "tok-%d" % n}, "tok"

    mount, calls = _mount_with_stubbed_pages(page_fn)
    with mock.patch.object(drive_changes, "DRIVE_PAGE_GUARD_MAX_ITEMS", 2):
        try:
            mount._list_all("folder1")
        except MountError:
            pass
        else:
            raise AssertionError("条数达到上限必须停表抛 MountError")
    assert calls["n"] == 1, calls


def test_list_all_final_page_at_limit_returns_normally():
    """A-021 边界：恰好在上限页翻完（末页 nextPageToken 为空）不触发护栏，
    正常返回全部条目。"""
    def page_fn(n):
        if n == 1:
            return {"files": [{"id": "a"}],
                    "nextPageToken": "tok-1"}, "tok"
        return {"files": [{"id": "b"}], "nextPageToken": None}, "tok"

    mount, calls = _mount_with_stubbed_pages(page_fn)
    with mock.patch.object(drive_changes, "DRIVE_PAGE_GUARD_MAX_PAGES", 2):
        items = mount._list_all("folder1")
    assert [i["id"] for i in items] == ["a", "b"]
    assert calls["n"] == 2, calls
