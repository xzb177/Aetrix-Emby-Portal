"""Drive SA 中继（2026-10-23 额度应急）。

公开下载池是全网共用额度、爆池时整库播不了；中继改走服务端
SA 凭据从 Drive API 取流转发：file_id 不出服务器、字节走 SA
各自的额度。本文件锁三件事：

- playback_strategy.drive_relay_enabled：默认关、显式开才开、
  读失败回默认关；
- streaming.serve_drive_relay_async：地址由 drive_auth 构造
  （alt=media）、凭据走 get_token_for_drive 且只进服务端请求头；
  上游 403 轮换凭据重试一次；无凭据 503；
- video_stream 的改道接线由 test_drive_direct_play 一侧的既有
  用例保证关时不变（中继默认关）。
"""
import asyncio
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from backend.emby_server import playback_strategy as _strategy
from backend.emby_server import streaming


def _run(coro):
    return asyncio.run(coro)


# ---------- 开关 ----------

def _stub_store(monkeypatch, values):
    def fake_get(db, key, default=None):
        return values.get(key, default)

    monkeypatch.setattr(_strategy.store, "get_value", fake_get)


def test_drive_relay_enabled_defaults_off(monkeypatch):
    _stub_store(monkeypatch, {})
    assert _strategy.drive_relay_enabled(object()) is False


def test_drive_relay_enabled_explicit_on(monkeypatch):
    _stub_store(monkeypatch, {_strategy.KEY_DRIVE_RELAY: "true"})
    assert _strategy.drive_relay_enabled(object()) is True
    _stub_store(monkeypatch, {_strategy.KEY_DRIVE_RELAY: "0"})
    assert _strategy.drive_relay_enabled(object()) is False


def test_drive_relay_enabled_read_failure_defaults_off(monkeypatch):
    def boom(db, key, default=None):
        raise RuntimeError("db down")

    monkeypatch.setattr(_strategy.store, "get_value", boom)
    assert _strategy.drive_relay_enabled(object()) is False


# ---------- 中继包装 ----------

def _fake_request():
    return SimpleNamespace(headers={})


def _patch_relay(monkeypatch, behavior):
    """behavior(attempt_no, headers) -> 返回值或抛 HTTPException。"""
    calls = []

    async def fake_mount(url, request, headers=None, media_type="video/mp4", pt=None):
        calls.append({"url": url, "headers": dict(headers or {})})
        return behavior(len(calls), headers)

    monkeypatch.setattr(streaming, "serve_mount_relay_async", fake_mount)
    return calls


def _patch_token(monkeypatch, tokens):
    calls = []
    import backend.emby_server.drive_changes as _dc

    def fake_token(drive_id, force_refresh=False):
        calls.append({"drive_id": drive_id, "force": force_refresh})
        return tokens[min(len(calls) - 1, len(tokens) - 1)]

    monkeypatch.setattr(_dc, "get_token_for_drive", fake_token)
    return calls


def test_relay_uses_api_url_and_bearer(monkeypatch):
    calls = _patch_relay(monkeypatch, lambda n, h: "RESP")
    tokens = _patch_token(monkeypatch, ["TOK1"])
    out = _run(streaming.serve_drive_relay_async(
        _fake_request(), "DRIVE9", "FID123"))
    assert out == "RESP"
    assert len(calls) == 1
    assert "alt=media" in calls[0]["url"]
    assert "FID123" in calls[0]["url"]  # fid 只进服务端取流地址
    assert calls[0]["headers"]["Authorization"] == "Bearer TOK1"
    assert tokens == [{"drive_id": "DRIVE9", "force": False}]


def test_relay_403_rotates_token_and_retries(monkeypatch):
    def behavior(n, headers):
        if n == 1:
            raise HTTPException(status_code=403, detail="quota")
        return "RESP2"

    calls = _patch_relay(monkeypatch, behavior)
    tokens = _patch_token(monkeypatch, ["TOK1", "TOK2"])
    out = _run(streaming.serve_drive_relay_async(
        _fake_request(), "DRIVE9", "FID123"))
    assert out == "RESP2"
    assert [c["headers"]["Authorization"] for c in calls] == [
        "Bearer TOK1", "Bearer TOK2"]
    assert [t["force"] for t in tokens] == [False, True]


def test_relay_second_403_propagates(monkeypatch):
    def behavior(n, headers):
        raise HTTPException(status_code=403, detail="quota")

    _patch_relay(monkeypatch, behavior)
    _patch_token(monkeypatch, ["TOK1", "TOK2"])
    with pytest.raises(HTTPException) as ei:
        _run(streaming.serve_drive_relay_async(
            _fake_request(), "DRIVE9", "FID123"))
    assert ei.value.status_code == 403


def test_relay_non_403_no_retry(monkeypatch):
    def behavior(n, headers):
        raise HTTPException(status_code=502, detail="bad gateway")

    calls = _patch_relay(monkeypatch, behavior)
    _patch_token(monkeypatch, ["TOK1"])
    with pytest.raises(HTTPException) as ei:
        _run(streaming.serve_drive_relay_async(
            _fake_request(), "DRIVE9", "FID123"))
    assert ei.value.status_code == 502
    assert len(calls) == 1


def test_relay_no_token_503(monkeypatch):
    calls = _patch_relay(monkeypatch, lambda n, h: "RESP")
    _patch_token(monkeypatch, [None])
    with pytest.raises(HTTPException) as ei:
        _run(streaming.serve_drive_relay_async(
            _fake_request(), "DRIVE9", "FID123"))
    assert ei.value.status_code == 503
    assert calls == []
