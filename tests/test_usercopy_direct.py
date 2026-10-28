# -*- coding: utf-8 -*-
"""用户副本免凭证直链（2026-10-28）：SenPlayer 不发 RequiredHttpHeaders
时 alt=media 必 403，故用户 Drive 副本改走免凭证 usercontent 地址 + 空头。

钉死：
- 工人侧授权限幂等：add_anyone_reader 遇 409（已有相同权限）视为成功，
  不再记 orphan；其它非 2xx 仍抛异常。
- 选路判定只认用户 Drive 副本行（owner_scope=user 且 variant_drive_id
  以 userdrive: 开头）；115 副本、服主副本、None 一律走旧链路。
- _apply_usercopy_direct：授权限成功才换 usercontent 地址 + 空头；授权限
  失败、缺 fid、非 dict 源一律返回 False 且不动源（调用方回落旧链路）；
  内部异常永不外抛。
- ensure_user_copy_public：只用本人 Drive 绑定令牌；无绑定、令牌空、
  授权限失败、内部异常一律 False，绝不用于共享源文件（调用方只传
  用户副本行）。

全部离线：Drive 调用与令牌口一律 monkeypatch，不碰真实谷歌。
"""
import os
from types import SimpleNamespace

os.environ.setdefault("DATABASE_TYPE", "sqlite")
os.environ.setdefault("REDIS_ENABLED", "false")

import pytest

from backend.emby_server import api
from backend.emby_server import drive_oauth
from backend.emby_server import drive_share
from backend.emby_server import transfer_variant as tv

FID = "1qTzAAmIdaS7YdZo638tBldrrQBekmy-I"


class _Resp:
    def __init__(self, status_code):
        self.status_code = status_code


def _client_with_status(monkeypatch, status_code):
    client = tv.DriveApiClient()
    monkeypatch.setattr(
        client, "_request",
        lambda method, url, drive_id, **kwargs: _Resp(status_code))
    return client


def test_add_anyone_reader_409_is_success(monkeypatch):
    client = _client_with_status(monkeypatch, 409)
    client.add_anyone_reader("myDrive:410", FID)


def test_add_anyone_reader_201_is_success(monkeypatch):
    client = _client_with_status(monkeypatch, 201)
    client.add_anyone_reader("myDrive:410", FID)


def test_add_anyone_reader_other_failure_still_raises(monkeypatch):
    client = _client_with_status(monkeypatch, 403)
    with pytest.raises(Exception, match="补 anyone 权限失败"):
        client.add_anyone_reader("myDrive:410", FID)


def test_is_user_drive_copy_variant():
    user_drive = SimpleNamespace(
        owner_scope="user", variant_drive_id="userdrive:410")
    assert api._is_user_drive_copy_variant(user_drive) is True
    assert api._is_user_drive_copy_variant(None) is False
    source_row = SimpleNamespace(
        owner_scope="source", variant_drive_id="mount:3")
    assert api._is_user_drive_copy_variant(source_row) is False
    pan115_row = SimpleNamespace(
        owner_scope="user", variant_drive_id="pan115:user-410")
    assert api._is_user_drive_copy_variant(pan115_row) is False
    no_drive_row = SimpleNamespace(owner_scope="user", variant_drive_id=None)
    assert api._is_user_drive_copy_variant(no_drive_row) is False


def _user():
    return SimpleNamespace(id=410)


def test_apply_usercopy_direct_success(monkeypatch):
    monkeypatch.setattr(
        tv, "ensure_user_copy_public", lambda db, uid, fid: True)
    src = {"DirectPlayUrl": "https://old/stream",
           "DirectStreamUrl": "https://old/stream",
           "RequiredHttpHeaders": {"Authorization": "Bearer old"}}
    assert api._apply_usercopy_direct(None, _user(), src, FID) is True
    assert src["DirectPlayUrl"] == (
        "https://drive.usercontent.google.com/download?id=" + FID
        + "&export=download&confirm=t")
    assert src["DirectStreamUrl"] == src["DirectPlayUrl"]
    assert src["RequiredHttpHeaders"] == {}


def test_apply_usercopy_direct_ensure_fail_keeps_old(monkeypatch):
    monkeypatch.setattr(
        tv, "ensure_user_copy_public", lambda db, uid, fid: False)
    src = {"DirectPlayUrl": "https://old/stream",
           "DirectStreamUrl": "https://old/stream",
           "RequiredHttpHeaders": {"Authorization": "Bearer old"}}
    assert api._apply_usercopy_direct(None, _user(), src, FID) is False
    assert src["DirectPlayUrl"] == "https://old/stream"
    assert src["RequiredHttpHeaders"] == {"Authorization": "Bearer old"}


def test_apply_usercopy_direct_missing_fid_or_bad_source(monkeypatch):
    assert api._apply_usercopy_direct(None, _user(), {}, "") is False
    assert api._apply_usercopy_direct(None, _user(), None, FID) is False
    assert api._apply_usercopy_direct(None, _user(), "not-a-dict", FID) is False


def test_apply_usercopy_direct_never_raises(monkeypatch):
    def _boom(db, uid, fid):
        raise RuntimeError("boom")
    monkeypatch.setattr(tv, "ensure_user_copy_public", _boom)
    src = {"DirectPlayUrl": "https://old/stream"}
    assert api._apply_usercopy_direct(None, _user(), src, FID) is False
    assert src["DirectPlayUrl"] == "https://old/stream"


def test_ensure_user_copy_public_ok_and_already(monkeypatch):
    fake_binding = SimpleNamespace(id=15)
    monkeypatch.setattr(
        tv, "_active_binding", lambda db, uid, provider: fake_binding)
    monkeypatch.setattr(
        drive_oauth, "get_binding_drive_access_token",
        lambda db, binding: "tok-user")
    for status in ("ok", "already"):
        monkeypatch.setattr(
            drive_share, "ensure_shared", lambda fid, tok: status)
        assert tv.ensure_user_copy_public(None, 410, FID) is True


def test_ensure_user_copy_public_fail_paths(monkeypatch):
    # 无绑定
    monkeypatch.setattr(tv, "_active_binding", lambda db, uid, p: None)
    assert tv.ensure_user_copy_public(None, 410, FID) is False
    # 令牌空
    fake_binding = SimpleNamespace(id=15)
    monkeypatch.setattr(
        tv, "_active_binding", lambda db, uid, p: fake_binding)
    monkeypatch.setattr(
        drive_oauth, "get_binding_drive_access_token",
        lambda db, binding: "")
    assert tv.ensure_user_copy_public(None, 410, FID) is False
    # 授权限失败 / 内部异常
    monkeypatch.setattr(
        drive_oauth, "get_binding_drive_access_token",
        lambda db, binding: "tok-user")
    monkeypatch.setattr(
        drive_share, "ensure_shared", lambda fid, tok: "failed")
    assert tv.ensure_user_copy_public(None, 410, FID) is False
    monkeypatch.setattr(
        drive_share, "ensure_shared", lambda fid, tok: (_ for _ in ()).throw(
            RuntimeError("boom")))
    assert tv.ensure_user_copy_public(None, 410, FID) is False
    # 缺 fid / 缺用户
    assert tv.ensure_user_copy_public(None, 410, "") is False
    assert tv.ensure_user_copy_public(None, None, FID) is False


def test_ensure_user_copy_public_uses_own_binding_only(monkeypatch):
    seen = {}

    def _binding(db, uid, provider):
        seen["uid"] = uid
        seen["provider"] = provider
        return SimpleNamespace(id=15)

    def _token(db, binding):
        seen["binding_id"] = binding.id
        return "tok-user"

    def _share(fid, tok):
        seen["fid"] = fid
        seen["tok"] = tok
        return "ok"

    monkeypatch.setattr(tv, "_active_binding", _binding)
    monkeypatch.setattr(
        drive_oauth, "get_binding_drive_access_token", _token)
    monkeypatch.setattr(drive_share, "ensure_shared", _share)
    assert tv.ensure_user_copy_public(None, 410, FID) is True
    assert seen == {"uid": 410, "provider": "drive", "binding_id": 15,
                    "fid": FID, "tok": "tok-user"}
