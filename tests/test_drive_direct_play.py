# -*- coding: utf-8 -*-
"""Drive 直链播放（最简模式，2026-10-16 用户拍板学 raisemby）。

- 开关开（默认）+ 条目有 drive_file_id → video_stream 直接 302 到谷歌
  usercontent 直链（Location 含 file_id），不经过对象存储直链/中继。
- 开关关 → 走原路径（对象存储直链判定；不命中则 410）。
- 非 Drive 条目（无 drive_file_id，如本地 .strm 里一条普通 http 直链）→
  不受影响，不命中则 410；但 mount:// 路径的远程挂载条目（115 等）→
  走专用中继 serve_mount_relay_async（2026-10-17 用户拍板）。
- 302 响应带卫生头：Cache-Control: no-store（直链个人化/短时，不可缓存复用）、
  Referrer-Policy: no-referrer。

全部离线：db/user/鉴权全部桩掉，不做真实网络与数据库请求。
"""
import os

os.environ.setdefault("DATABASE_TYPE", "sqlite")
os.environ.setdefault("REDIS_ENABLED", "false")

import asyncio
from types import SimpleNamespace

import pytest
from starlette.requests import Request

from backend.emby_server import api
from backend.emby_server import playback_strategy as _strategy
from backend.emby_server.mounts import PlayTarget

FID = "1eXUpWksxfCCrIZUye8lo24JGXyr_2ZQO"


def _request():
    scope = {"type": "http", "method": "GET", "path": "/Videos/item/stream",
             "raw_path": b"/Videos/item/stream", "query_string": b"",
             "headers": [], "scheme": "http", "server": ("testserver", 80),
             "client": ("testclient", 50000), "root_path": ""}
    return Request(scope)


def _stub_common(monkeypatch, item, target):
    monkeypatch.setattr(api, "_require_item", lambda db, item_id: item)
    monkeypatch.setattr(api, "_library_scope", lambda db_, user_: None)
    monkeypatch.setattr(api, "ensure_playback_allowed", lambda db, user: None)
    # 播放门禁与付费墙同口径桩掉（本文件测直链分支，不测门禁本身；
    # 门禁行为见 tests/test_playback_gate.py）。
    monkeypatch.setattr(
        "backend.emby_server.playback_gate.ensure_binding_playback_allowed",
        lambda db, u, r=None: None)
    monkeypatch.setattr(api, "_play_target", lambda db, it: target)
    # 通用中继已删（2026-10-17 用户拍板全删）：serve_remote_async 不复存在；
    # 未命中直链时 video_stream 直接 410——mount:// 的远程挂载条目除外，
    # 它们走专用中继 serve_mount_relay_async（见 test_mount_relay.py）。


def _stub_switch(monkeypatch, value):
    """把开关读数桩成指定值；value=None 时走真实 reader（假 db 下 fail-safe 回默认开）。"""
    if value is None:
        return
    real = _strategy.store.get_value

    def fake_get(db, key, default="", **kwargs):
        if key == _strategy.KEY_DRIVE_DIRECT_PLAY:
            return value
        return real(db, key, default, **kwargs)

    monkeypatch.setattr(_strategy.store, "get_value", fake_get)


def _drive_item():
    return SimpleNamespace(container="mkv", drive_file_id=FID)


def _drive_target():
    return PlayTarget("url",
                      f"https://drive.google.com/uc?export=download&id={FID}&confirm=t",
                      {"Authorization": "Bearer <redacted>"})


def test_switch_on_drive_item_302_to_usercontent(monkeypatch):
    """开关开（默认）+ Drive 条目 → 302 到 usercontent 直链，不走中继。"""
    _stub_common(monkeypatch, _drive_item(), _drive_target())
    _stub_switch(monkeypatch, None)  # 真实 reader：假 db 读失败 → fail-safe 默认开

    resp = asyncio.run(api.video_stream("item", _request(), object(), object()))

    assert resp.status_code == 302
    loc = resp.headers["location"]
    assert "drive.usercontent.google.com" in loc
    assert f"id={FID}" in loc
    assert not hasattr(api, "serve_remote_async"), "中继代理已随中继删除"
    assert resp.headers.get("cache-control") == "no-cache, no-store"
    assert resp.headers.get("referrer-policy") == "no-referrer"


def test_switch_off_drive_item_falls_back_to_relay(monkeypatch):
    """开关关 → Drive 条目走原路径（这里桩掉对象存储直链；中继已删，不命中则 410）。"""
    _stub_common(monkeypatch, _drive_item(), _drive_target())
    _stub_switch(monkeypatch, "false")
    monkeypatch.setattr(_strategy, "maybe_direct_url", lambda db, fid: None)

    with pytest.raises(Exception) as exc_info:
        asyncio.run(api.video_stream("item", _request(), object(), object()))

    assert getattr(exc_info.value, "status_code", None) == 410, \
        "开关关且无直链命中时，中继已删，应 410 而不是走中继"


def test_switch_on_non_drive_item_unaffected(monkeypatch):
    """非 Drive、非挂载条目（本地 .strm 里一条普通 http 直链）→ 开关开也不 302，
    无直链命中时仍 410（mount:// 条目走专用中继，见 test_mount_relay.py）。"""
    item = SimpleNamespace(container="mkv", drive_file_id="",
                           file_path="/strm/Movies/x.strm")
    target = PlayTarget("url", "https://115.example.com/direct/abc.mkv", {})
    _stub_common(monkeypatch, item, target)
    _stub_switch(monkeypatch, "true")

    with pytest.raises(Exception) as exc_info:
        asyncio.run(api.video_stream("item", _request(), object(), object()))

    assert getattr(exc_info.value, "status_code", None) == 410, \
        "非 Drive 非挂载条目无直链命中时，应 410"


def test_drive_direct_play_enabled_defaults_on(monkeypatch):
    """开关默认值：未配置时开；显式关时关；非法 db 下 fail-safe 开。"""
    assert _strategy.drive_direct_play_enabled(object()) is True
    _stub_switch(monkeypatch, "false")
    assert _strategy.drive_direct_play_enabled(object()) is False
    _stub_switch(monkeypatch, "0")
    assert _strategy.drive_direct_play_enabled(object()) is False
