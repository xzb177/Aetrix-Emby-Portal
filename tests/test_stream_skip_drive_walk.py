# -*- coding: utf-8 -*-
"""video_stream P0（2026-10-18）：drive 直链 302 跳过 _play_target 的 Drive 路径步行。

背景：步行结果在 302 分支根本没用（URL 由 drive_file_id 纯拼串），
却在 run_db 线程里做 1.3–4.7s 的 Drive API 调用并全程持有 DB 会话——
50 并发时打满连接池（25+25），新会话 30s 超时，全线排队约 90s。
修法：存在性校验通过则直接 302；存在性改走一次 files.get 便宜判定，
跑在独立执行器（不占 run_db 线程、不持有 DB 会话）。

- 快路径：drive 条目（mount:// 前缀 + 启用的 drive 挂载 + 开关开）→
  不调用 _play_target/步行，一次 files.get 通过后 302 到 usercontent 直链。
- files.get 404 → HTTP 404；超时/5xx/无凭据/401 → HTTP 503。
- 非 drive 条目 / 开关关 / 挂载解析失败 → 原路径（_play_target 照调）。
- 存在性校验跑在独立执行器（线程名前缀 drive-exists），不占 run_db 线程。
全部离线：httpx.get、get_token_for_drive 全部桩掉，不做真实网络与数据库请求。
"""
import os

os.environ.setdefault("DATABASE_TYPE", "sqlite")
os.environ.setdefault("REDIS_ENABLED", "false")

import asyncio
import threading
from types import SimpleNamespace

import pytest
from starlette.requests import Request

from backend.emby_server import api
from backend.emby_server import playback_strategy as _strategy

FID = "1eXUpWksxfCCrIZUye8lo24JGXyr_2ZQO"
SHARED_DRIVE_ID = "0AERUVu4kfWEfUk9PVA"


def _request():
    scope = {"type": "http", "method": "GET", "path": "/Videos/item/stream",
             "raw_path": b"/Videos/item/stream", "query_string": b"",
             "headers": [], "scheme": "http", "server": ("testserver", 80),
             "client": ("testclient", 50000), "root_path": ""}
    return Request(scope)


def _drive_item():
    return SimpleNamespace(container="mkv", drive_file_id=FID,
                           file_path="mount://3/MoviePilot/x.mkv", library=None)


class _FakeQuery:
    def __init__(self, mount):
        self._mount = mount

    def filter(self, *args, **kwargs):
        return self

    def first(self):
        return self._mount


class _FakeDB:
    """只够 _drive_mount_id_for_item 用的最小 db 桩。"""

    def __init__(self, mount):
        self._mount = mount

    def query(self, model):
        return _FakeQuery(self._mount)


def _drive_mount(path="drive:" + SHARED_DRIVE_ID, enabled=True,
                 mount_type="drive", mount_id=3):
    return SimpleNamespace(id=mount_id, path=path, is_enabled=enabled,
                           mount_type=mount_type, config="{}")


def _stub_common(monkeypatch, item):
    monkeypatch.setattr(api, "_require_item", lambda db, item_id: item)
    monkeypatch.setattr(api, "_library_scope", lambda db_, user_: None)
    monkeypatch.setattr(api, "ensure_playback_allowed", lambda db, user: None)


def _stub_switch(monkeypatch, value):
    """把开关读数桩成指定值。"""
    real = _strategy.store.get_value

    def fake_get(db, key, default="", **kwargs):
        if key == _strategy.KEY_DRIVE_DIRECT_PLAY:
            return value
        return real(db, key, default, **kwargs)

    monkeypatch.setattr(_strategy.store, "get_value", fake_get)


def _stub_http(monkeypatch, status_code=200, exc=None, token="tok"):
    """桩掉凭据层与 httpx；返回 (thread_names, token_calls)。"""
    from backend.emby_server import drive_changes
    thread_names = []
    token_calls = []

    def fake_get_token(drive_id, force_refresh=False):
        token_calls.append(drive_id)
        return token

    def fake_http_get(url, params=None, headers=None, timeout=None):
        thread_names.append(threading.current_thread().name)
        if exc is not None:
            raise exc
        assert params and params.get("supportsAllDrives") == "true", \
            "files.get 必须带 supportsAllDrives=true"
        assert timeout is not None and timeout <= 3.0, \
            "存在性校验超时必须 ≤3s"
        return SimpleNamespace(status_code=status_code)

    monkeypatch.setattr(drive_changes, "get_token_for_drive", fake_get_token)
    monkeypatch.setattr(api.httpx, "get", fake_http_get)
    return thread_names, token_calls


def test_fast_path_skips_walk_and_302s(monkeypatch):
    """快路径：drive 条目 + 开关开 → 不走 _play_target/步行，302 到直链。"""
    item = _drive_item()
    db = _FakeDB(_drive_mount())
    _stub_common(monkeypatch, item)
    _stub_switch(monkeypatch, "true")
    walk_calls = []
    monkeypatch.setattr(
        api, "_play_target",
        lambda db_, it: (_ for _ in ()).throw(
            AssertionError("快路径不许调用 _play_target")))
    from backend.emby_server import mounts as mount_lib
    monkeypatch.setattr(
        mount_lib, "resolve_play_target",
        lambda *a, **k: walk_calls.append(1) or (_ for _ in ()).throw(
            AssertionError("快路径不许做路径步行")))
    thread_names, token_calls = _stub_http(monkeypatch, status_code=200)

    resp = asyncio.run(api.video_stream("item", _request(), object(), db))

    assert resp.status_code == 302
    loc = resp.headers["location"]
    assert "drive.usercontent.google.com" in loc
    assert f"id={FID}" in loc
    assert walk_calls == [], "路径步行必须被跳过"
    assert token_calls == [SHARED_DRIVE_ID], "凭据应按挂载的盘取"
    assert thread_names and all(
        n.startswith("drive-exists") for n in thread_names), \
        f"存在性校验必须跑在独立执行器里，实际线程：{thread_names}"


def test_files_get_404_returns_404(monkeypatch):
    """files.get 404 → 该请求 404（文件已删/不可见）。"""
    item = _drive_item()
    _stub_common(monkeypatch, item)
    _stub_switch(monkeypatch, "true")
    _stub_http(monkeypatch, status_code=404)

    with pytest.raises(Exception) as exc_info:
        asyncio.run(api.video_stream("item", _request(), object(),
                                     _FakeDB(_drive_mount())))

    assert getattr(exc_info.value, "status_code", None) == 404


def test_files_get_500_returns_503(monkeypatch):
    """files.get 5xx → 503（瞬时故障不再误读成「文件没了」的 404）。"""
    item = _drive_item()
    _stub_common(monkeypatch, item)
    _stub_switch(monkeypatch, "true")
    _stub_http(monkeypatch, status_code=500)

    with pytest.raises(Exception) as exc_info:
        asyncio.run(api.video_stream("item", _request(), object(),
                                     _FakeDB(_drive_mount())))

    assert getattr(exc_info.value, "status_code", None) == 503


def test_files_get_401_returns_503_and_invalidates_token(monkeypatch):
    """files.get 401 → 503，且坏 token 按值失效（后续请求重新换取）。"""
    item = _drive_item()
    _stub_common(monkeypatch, item)
    _stub_switch(monkeypatch, "true")
    _stub_http(monkeypatch, status_code=401)

    with pytest.raises(Exception) as exc_info:
        asyncio.run(api.video_stream("item", _request(), object(),
                                     _FakeDB(_drive_mount())))

    assert getattr(exc_info.value, "status_code", None) == 503


def test_files_get_timeout_returns_503(monkeypatch):
    """files.get 超时 → 503。"""
    import httpx as _httpx
    item = _drive_item()
    _stub_common(monkeypatch, item)
    _stub_switch(monkeypatch, "true")
    _stub_http(monkeypatch, exc=_httpx.TimeoutException("timeout"))

    with pytest.raises(Exception) as exc_info:
        asyncio.run(api.video_stream("item", _request(), object(),
                                     _FakeDB(_drive_mount())))

    assert getattr(exc_info.value, "status_code", None) == 503


def test_no_token_returns_503(monkeypatch):
    """取不到凭据 → 503。"""
    item = _drive_item()
    _stub_common(monkeypatch, item)
    _stub_switch(monkeypatch, "true")
    _stub_http(monkeypatch, token=None)

    with pytest.raises(Exception) as exc_info:
        asyncio.run(api.video_stream("item", _request(), object(),
                                     _FakeDB(_drive_mount())))

    assert getattr(exc_info.value, "status_code", None) == 503


def test_non_drive_item_uses_old_path(monkeypatch):
    """非 drive 条目 → 原路径（_play_target 照调），行为不变。"""
    item = SimpleNamespace(container="mkv", drive_file_id="",
                           file_path="/strm/Movies/x.strm")
    _stub_common(monkeypatch, item)
    _stub_switch(monkeypatch, "true")
    calls = []
    monkeypatch.setattr(api, "_play_target",
                        lambda db_, it: calls.append(1) or SimpleNamespace(
                            kind="url", value="https://115.example.com/x",
                            headers={}))
    monkeypatch.setattr(_strategy, "maybe_direct_url", lambda db, fid: None)

    with pytest.raises(Exception) as exc_info:
        asyncio.run(api.video_stream("item", _request(), object(), object()))

    assert calls == [1], "非 drive 条目必须走原 _play_target"
    assert getattr(exc_info.value, "status_code", None) == 410


def test_switch_off_uses_old_path(monkeypatch):
    """开关关 → 原路径（_play_target 照调）。"""
    # 无 file_path（与 test_drive_direct_play.py 的既有口径一致），
    # 避免走到 mount:// 专用中继分支，保持断言聚焦在 _play_target 调用上。
    item = SimpleNamespace(container="mkv", drive_file_id=FID)
    _stub_common(monkeypatch, item)
    _stub_switch(monkeypatch, "false")
    calls = []
    monkeypatch.setattr(api, "_play_target",
                        lambda db_, it: calls.append(1) or SimpleNamespace(
                            kind="url", value="https://115.example.com/x",
                            headers={}))
    monkeypatch.setattr(_strategy, "maybe_direct_url", lambda db, fid: None)

    with pytest.raises(Exception) as exc_info:
        asyncio.run(api.video_stream("item", _request(), object(), object()))

    assert calls == [1], "开关关时必须走原 _play_target"
    assert getattr(exc_info.value, "status_code", None) == 410


def test_mount_deleted_falls_back_to_old_path(monkeypatch):
    """挂载已删/停用 → 回落原路径（不 404 误伤，由 _play_target 按旧语义处理）。"""
    item = _drive_item()
    _stub_common(monkeypatch, item)
    _stub_switch(monkeypatch, "true")
    calls = []
    # 真实语义：挂载已删时 _play_target 经 resolve_play_target 抛 404
    #（load_mount 取不到挂载 → MountError → 404）。
    from fastapi import HTTPException as _HTTPException

    def _fake_play_target(db_, it):
        calls.append(1)
        raise _HTTPException(status_code=404, detail="挂载 #3 不存在")

    monkeypatch.setattr(api, "_play_target", _fake_play_target)

    with pytest.raises(Exception) as exc_info:
        asyncio.run(api.video_stream("item", _request(), object(),
                                     _FakeDB(None)))

    assert calls == [1], "挂载解析失败必须回落原路径"
    assert getattr(exc_info.value, "status_code", None) == 404


def test_drive_id_resolution(monkeypatch):
    """_drive_mount_id_for_item 的挂载解析口径。"""
    item = _drive_item()
    assert api._drive_mount_id_for_item(
        _FakeDB(_drive_mount()), item) == SHARED_DRIVE_ID
    assert api._drive_mount_id_for_item(_FakeDB(None), item) is None
    assert api._drive_mount_id_for_item(
        _FakeDB(_drive_mount(enabled=False)), item) is None
    assert api._drive_mount_id_for_item(
        _FakeDB(_drive_mount(mount_type="115")), item) is None
    no_mount_item = SimpleNamespace(container="mkv", drive_file_id=FID,
                                    file_path="/media/x.mkv", library=None)
    assert api._drive_mount_id_for_item(
        _FakeDB(_drive_mount()), no_mount_item) is None
    strm_item = SimpleNamespace(container="mkv", drive_file_id=FID,
                                file_path="mount://3/MoviePilot/x.strm",
                                library=None)
    assert api._drive_mount_id_for_item(
        _FakeDB(_drive_mount()), strm_item) is None, \
        ".strm 不进快路径（走内容解析）"


def test_personal_drive_uses_oauth_token_source(monkeypatch):
    """个人盘挂载：凭据按盘取（myDrive:<remote> 走 OAuth），不硬编码共享盘。"""
    item = _drive_item()
    _stub_common(monkeypatch, item)
    _stub_switch(monkeypatch, "true")
    thread_names, token_calls = _stub_http(monkeypatch, status_code=200)

    resp = asyncio.run(api.video_stream(
        "item", _request(), object(),
        _FakeDB(_drive_mount(path="drive:myDrive:paul_emby"))))

    assert resp.status_code == 302
    assert token_calls == ["myDrive:paul_emby"], \
        f"个人盘必须按 myDrive:<remote> 取 OAuth 凭据，实际：{token_calls}"
