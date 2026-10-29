# -*- coding: utf-8 -*-
"""共享盘 files.list 缺 includeItemsFromAllDrives 致 Path B 暂存目录 403。

生产实证（2026-10-29）：任务 422/439（源共享盘 0AERUVu4kfWEfUk9PVA）
的按名查失败体逐字为 Google 原话“The includeItemsFromAllDrives
parameter must be set to true when driveId is specified or corpora
contains drive or allDrives.”——此前 _list_params 的共享盘分支只带
driveId + corpora=drive，缺该参数，根枚举与按名查同 403，Path B
在源盘建暂存目录的第一步即死。本文件 mock 按谷歌真实语义：
files.list 带 driveId 但缺 includeItemsFromAllDrives 即 403，
带齐即 200；全部离线，不碰真实 Drive。
"""
import os

os.environ.setdefault("DATABASE_TYPE", "sqlite")
os.environ.setdefault("REDIS_ENABLED", "false")

from types import SimpleNamespace

import pytest

from backend.emby_server import transfer_variant as tv

SHARED = "0AERUVu4kfWEfUk9PVA"
GOOGLE_MSG = ("The includeItemsFromAllDrives parameter must be set to true "
              "when driveId is specified or corpora contains drive or "
              "allDrives.")


def _resp(status_code, files=None, text=""):
    return SimpleNamespace(status_code=status_code,
                           json=lambda: {"files": files or []},
                           text=text)


class _GoogleLike:
    """按谷歌真实语义的 files.list 桩：缺参即 403，带齐即 200。

    root_files / name_files：根枚举、按名查各自返回的目录行；
    root_403：根枚举是否额外回 403（模拟非缺参成因的 403，
    此时回落路径应接住）；create_id：POST 建目录返回的 id。
    """

    def __init__(self, root_files=None, name_files=None,
                 root_403=False, create_id="newfolder"):
        self.root_files = root_files
        self.name_files = name_files
        self.root_403 = root_403
        self.create_id = create_id
        self.calls = []

    def __call__(self, method, url, drive_id, **kwargs):
        # 注意：以实例整体 monkeypatch 到类属性时不会像普通函数
        # 那样绑定 self，调用方传进来的第一位就是 method。
        params = kwargs.get("params") or {}
        self.calls.append((method, params))
        if method == "GET":
            if params.get("driveId") and \
                    params.get("includeItemsFromAllDrives") != "true":
                return _resp(403, text='{"error": {"code": 403, "message": "%s"}}'
                             % GOOGLE_MSG)
            q = params.get("q", "")
            if "in parents" in q:
                if self.root_403:
                    return _resp(403, text='{"error": {"code": 403}}')
                return _resp(200, files=self.root_files)
            return _resp(200, files=self.name_files)
        if method == "POST":
            return SimpleNamespace(status_code=201,
                                   json=lambda: {"id": self.create_id},
                                   text="")
        raise AssertionError("unexpected drive call: %s" % method)


def test_shared_list_params_include_flag():
    params = tv.DriveApiClient._list_params(SHARED)
    assert params["driveId"] == SHARED
    assert params["corpora"] == "drive"
    assert params["includeItemsFromAllDrives"] == "true"
    personal = tv.DriveApiClient._list_params("myDrive:665")
    assert personal == {"spaces": "drive"}


def test_shared_list_old_params_403_reproduces_production(monkeypatch):
    # 旧参数（缺 flag）打谷歌语义桩必 403 且文案逐字等于生产失败体：
    # 证明桩 enforce 真实语义、旧代码必死，新参数是唯一变量。
    fake = _GoogleLike(root_files=[{"id": "d", "name": "aetrix-transfer"}])
    monkeypatch.setattr(tv.DriveApiClient, "_request", fake)
    old_params = {"q": "name='aetrix-transfer' and '%s' in parents and "
                       "mimeType='application/vnd.google-apps.folder' and "
                       "trashed=false" % SHARED,
                  "supportsAllDrives": "true", "fields": "files(id,name)",
                  "driveId": SHARED, "corpora": "drive"}
    resp = tv.DriveApiClient()._request(
        "GET", tv.FILES_URL, SHARED, params=old_params)
    assert resp.status_code == 403
    assert GOOGLE_MSG in resp.text


def test_ensure_folder_shared_root_hit(monkeypatch):
    fake = _GoogleLike(root_files=[{"id": "srcdir",
                                    "name": "aetrix-transfer"}])
    monkeypatch.setattr(tv.DriveApiClient, "_request", fake)
    assert tv.DriveApiClient().ensure_folder(SHARED, "aetrix-transfer") \
        == "srcdir"
    assert len(fake.calls) == 1 and fake.calls[0][0] == "GET"


def test_ensure_folder_shared_root_403_falls_back_to_name(monkeypatch):
    # 根枚举 403（非缺参成因）→ 按名查命中即复用，不走创建。
    fake = _GoogleLike(root_403=True,
                       name_files=[{"id": "appfolder",
                                    "name": "aetrix-transfer"}])
    monkeypatch.setattr(tv.DriveApiClient, "_request", fake)
    assert tv.DriveApiClient().ensure_folder(SHARED, "aetrix-transfer") \
        == "appfolder"
    assert [m for m, _ in fake.calls] == ["GET", "GET"]


def test_ensure_folder_shared_create_when_absent(monkeypatch):
    # 根枚举 403＋按名查 200 空＝目录确实不存在，才走创建
    # （200 空的根枚举则直落创建，不经过名查——见实现分支）。
    fake = _GoogleLike(root_403=True, name_files=[],
                       create_id="created-dir")
    monkeypatch.setattr(tv.DriveApiClient, "_request", fake)
    assert tv.DriveApiClient().ensure_folder(SHARED, "aetrix-transfer") \
        == "created-dir"
    assert [m for m, _ in fake.calls] == ["GET", "GET", "POST"]


def test_path_b_staging_first_step_succeeds(monkeypatch):
    # Path B 第一行 drive_api.ensure_folder(job.drive_id, dir) 的
    # 精确形状：源共享盘＋转存目录名，带齐参数后首查即 200。
    fake = _GoogleLike(root_files=[{"id": "staging-dir",
                                    "name": "aetrix-transfer"}])
    monkeypatch.setattr(tv.DriveApiClient, "_request", fake)
    assert tv.DriveApiClient().ensure_folder(
        SHARED, tv.transfer_dir_name()) == "staging-dir"
    params = fake.calls[0][1]
    assert params.get("includeItemsFromAllDrives") == "true"
