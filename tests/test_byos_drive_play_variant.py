# -*- coding: utf-8 -*-
"""BYOS Drive 播放变体（byos_drive_relay）。

钉住的口径：
- 清单：byos:// 条目 + 该用户 active 的 Drive 绑定 + 有 drive_file_id
  时，清单在主线之后追加 byos_drive_relay（第一条仍是主线）；
  无绑定 / 非 byos 条目 / 无 fid 时恒不追加，不虚列。
- 取流：客户端显式选 byos_drive_relay 时，video_stream 凭路径绑定
  行那个用户的 Drive 令牌中继（serve_drive_relay_async，drive_id
  为 byos:<绑定 id>），走该用户自己的额度；任何失败都明文报错，
  绝不静默回落共享 SA 池（SA 池一调就断言失败）。
- 令牌口径：钉住路径绑定行，不用“最早 active 绑定”
  （get_active_binding 是最早口径，多个 Drive 绑定时会播错盘）。

全部离线：Drive 令牌与中继取流全部桩掉，不碰真 Drive、不读真库。
video_stream 桩 harness 仿照 tests/test_byos_user_relay.py 同口径。
"""
import asyncio
import os
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from starlette.requests import Request
from starlette.responses import Response

os.environ.setdefault("DATABASE_TYPE", "sqlite")
os.environ.setdefault("REDIS_ENABLED", "false")

from backend.emby_server import api, play_variants
from backend.emby_server import playback_strategy as _strategy
from backend.emby_server.drive_oauth import DriveOAuthError


def _run(coro):
    return asyncio.run(coro)


# ---------- 清单层（纯函数，不碰 DB） ----------

def _drive_item():
    return SimpleNamespace(guid="g-byos", drive_file_id="FID7",
                           container="mkv",
                           file_path="byos://7/movie.mkv")


def test_list_variants_byos_drive_appended_after_mainline():
    names = [v.name for v in play_variants.list_variants(
        None, _drive_item(), user_drive_byos_available=True)]
    assert names[-1] == play_variants.BYOS_DRIVE_RELAY
    assert names[0] in (play_variants.DRIVE_RELAY, play_variants.DRIVE_DIRECT)
    assert [v.name for v in play_variants.list_variants(
        None, _drive_item())] == [
        v for v in names if v != play_variants.BYOS_DRIVE_RELAY]


def test_list_variants_byos_drive_not_leaked_to_local_or_no_fid():
    local = SimpleNamespace(guid="g-local", drive_file_id="",
                            file_path="/media/a.mkv")
    assert [v.name for v in play_variants.list_variants(
        None, local, user_drive_byos_available=True)] == [
        play_variants.LOCAL_FILE]
    no_fid = SimpleNamespace(guid="g-nofid", drive_file_id="",
                             file_path="byos://7/movie.mkv")
    assert play_variants.BYOS_DRIVE_RELAY not in [
        v.name for v in play_variants.list_variants(
            None, no_fid, user_drive_byos_available=True)]


def test_resolve_byos_drive_variant():
    assert play_variants.resolve_requested_variant(
        {"variant": "byos_drive_relay"},
        ["drive_relay", "byos_drive_relay"]) == ("byos_drive_relay", None)
    assert play_variants.resolve_requested_variant(
        {"variant": "byos_drive_relay"},
        ["drive_relay", "drive_direct"]) == (None, 409)
    variant = play_variants._VARIANTS[play_variants.BYOS_DRIVE_RELAY]
    assert variant.is_remote is False and variant.needs_relay is True


# ---------- 可用性 helper（FakeDB，不碰真库） ----------

class _FakeQuery:
    def __init__(self, row=None):
        self._row = row

    def filter(self, *args, **kwargs):
        return self

    def first(self):
        return self._row


class _FakeDB:
    def __init__(self, row=None):
        self._row = row

    def query(self, *args, **kwargs):
        return _FakeQuery(self._row)


def _binding(**over):
    row = {"id": 7, "user_id": 11, "provider": "drive",
           "status": "active"}
    row.update(over)
    return SimpleNamespace(**row)


def _user():
    return SimpleNamespace(id=11)


def _relay_on(monkeypatch):
    def fake_get(db, key, default="", **kwargs):
        if key == _strategy.KEY_DRIVE_RELAY:
            return "true"
        return default

    monkeypatch.setattr(_strategy.store, "get_value", fake_get)


def test_user_drive_byos_available_matrix(monkeypatch):
    _relay_on(monkeypatch)
    item = _drive_item()
    assert api._user_drive_byos_available(_FakeDB(_binding()), item, _user()) is True
    assert api._user_drive_byos_available(
        _FakeDB(_binding(user_id=99)), item, _user()) is False
    assert api._user_drive_byos_available(
        _FakeDB(_binding(status="expired")), item, _user()) is False
    assert api._user_drive_byos_available(
        _FakeDB(_binding(provider="pan115")), item, _user()) is False
    assert api._user_drive_byos_available(_FakeDB(None), item, _user()) is False
    non_byos = SimpleNamespace(guid="g-x", drive_file_id="FID7",
                               file_path="mount://3/x.mkv")
    assert api._user_drive_byos_available(
        _FakeDB(_binding()), non_byos, _user()) is False


# ---------- video_stream 显式变体取流 ----------

def _fake_request(query=b"variant=byos_drive_relay"):
    scope = {"type": "http", "method": "GET",
             "path": "/emby/Videos/item1/stream",
             "raw_path": b"/emby/Videos/item1/stream",
             "query_string": query, "headers": [], "scheme": "http",
             "server": ("testserver", 80),
             "client": ("testclient", 50000), "root_path": ""}
    return Request(scope)


def _setup_video_stream(monkeypatch, token_behavior=None):
    from backend.emby_server import drive_changes as _dc
    from backend.emby_server import drive_oauth as _do

    monkeypatch.setattr(api, "_require_visible_item",
                        lambda db, u, item_id: _drive_item())
    monkeypatch.setattr(api, "ensure_playback_allowed", lambda db, u: None)
    monkeypatch.setattr(api, "_enforce_path_access",
                        lambda db, u, it: None)
    monkeypatch.setattr(api, "_enforce_stream_limit",
                        lambda db, req, u, it, item_id: None)
    monkeypatch.setattr(api, "_observe_traffic", lambda resp: resp)
    monkeypatch.setattr(api.play_sign, "check_referer",
                        lambda request, db: None)

    relay_calls = []

    async def fake_relay(request, drive_id, file_id, media_type="video/mp4",
                         pt=None, access_token=None):
        relay_calls.append({"drive_id": drive_id, "file_id": file_id,
                            "media_type": media_type,
                            "access_token": access_token})
        return Response(content=b"x", media_type="video/mp4")

    monkeypatch.setattr(api, "serve_drive_relay_async", fake_relay)

    token_calls = []

    def fake_token(db, binding, http_post=None):
        token_calls.append(binding)
        if token_behavior is not None:
            return token_behavior(binding)
        return "USERTOK"

    monkeypatch.setattr(_do, "get_binding_drive_access_token", fake_token)

    sa_calls = []

    def fake_sa(drive_id, force_refresh=False):
        sa_calls.append(drive_id)
        raise AssertionError("byos 路上不许咨询共享 SA 池")

    monkeypatch.setattr(_dc, "get_token_for_drive", fake_sa)

    notes = []
    monkeypatch.setattr(api, "_note_play_start_safe",
                        lambda item_, **kwargs: notes.append(kwargs))

    def fake_get(db, key, default="", **kwargs):
        if key == _strategy.KEY_DRIVE_DIRECT_PLAY:
            return "true"
        if key == _strategy.KEY_DRIVE_RELAY:
            return "true"
        return default

    monkeypatch.setattr(_strategy.store, "get_value", fake_get)
    return relay_calls, token_calls, sa_calls, notes


def test_explicit_byos_drive_variant_uses_binding_token(monkeypatch):
    relay, token, sa, notes = _setup_video_stream(monkeypatch)
    resp = _run(api.video_stream(
        "item1", _fake_request(), _user(), _FakeDB(_binding())))
    assert resp.status_code == 200
    assert relay == [{"drive_id": "byos:7", "file_id": "FID7",
                      "media_type": "video/mkv",
                      "access_token": "USERTOK"}]
    assert len(token) == 1 and token[0].id == 7
    assert sa == []
    assert notes and notes[0]["variant"] == "byos_drive_relay"
    assert notes[0]["result"] == "ok"


def test_explicit_byos_drive_variant_other_user_403(monkeypatch):
    # 他人绑定在校验层就不可见：_available_variant_names 不含本变体，
    # resolve 按 409 挡下（不泄露他人绑定存在性，与 delete_binding
    # 的 not_found 口径一致）；分支内的 403 是校验与取流之间绑定
    # 归属变化时的纵深防御，不靠本用例触发。
    relay, token, sa, notes = _setup_video_stream(monkeypatch)
    with pytest.raises(HTTPException) as ei:
        _run(api.video_stream(
            "item1", _fake_request(), _user(), _FakeDB(_binding(user_id=99))))
    assert ei.value.status_code == 409
    assert relay == [] and token == [] and sa == []


def test_explicit_byos_drive_variant_expired_token_503(monkeypatch):
    def boom(binding):
        raise DriveOAuthError("expired", "登录已过期，请重新绑定。")

    relay, token, sa, notes = _setup_video_stream(monkeypatch, boom)
    with pytest.raises(HTTPException) as ei:
        _run(api.video_stream(
            "item1", _fake_request(), _user(), _FakeDB(_binding())))
    assert ei.value.status_code == 503
    assert "重新绑定" in ei.value.detail
    assert relay == [] and sa == []
