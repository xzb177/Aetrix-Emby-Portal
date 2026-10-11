"""回归测试：同步版 serve_remote 兜底分支漏关流式响应（PR #584 的同步版漏网之鱼）。

背景：
- 异步版 serve_remote_async 的 except Exception 兜底分支已修为「先关 resp 再关 client」；
- 同步版 serve_remote 此前只关 client：当重定向 Location 畸形（如 http://[::1，
  urllib 解析抛 ValueError: Invalid IPv6 URL，MountError 继承自 RuntimeError 抓不住）时，
  会落到兜底分支——HTTP 502 正常抛出，但已打开的流式响应从未关闭，造成慢泄漏。
本文件断言同步版与异步版行为一致：抛 502 且 resp.close_calls >= 1。

全部离线：URL 使用公网 IP 字面量，不做真实 DNS / 网络访问。
"""

from __future__ import annotations

import inspect
from typing import Any

import httpx
import pytest
from fastapi import HTTPException
from starlette.requests import Request

from backend.emby_server import streaming

_MEDIA_URL = "https://93.184.216.34/v.mp4"


class _FakeResponse:
    """最小假响应：统计 close 次数，headers 用 httpx.Headers 保证大小写不敏感。"""

    def __init__(self, status_code: int = 200, headers: dict[str, str] | None = None) -> None:
        self.status_code = status_code
        self.headers = httpx.Headers(headers or {})
        self.close_calls = 0

    def close(self) -> None:
        self.close_calls += 1

    def iter_bytes(self, chunk_size: int | None = None):  # pragma: no cover - 兜底用
        yield b""


class _FakeClient:
    """按脚本依次返回响应或抛异常的假 httpx.Client。"""

    def __init__(self, script: list[Any]) -> None:
        self._script = list(script)
        self.sent: list[Any] = []
        self.close_calls = 0

    def build_request(self, method: str, url: Any, **kwargs: Any) -> httpx.Request:
        """部分实现会先 build_request 再 send，这里返回真实 httpx.Request 兜底。"""
        return httpx.Request(method, url, headers=kwargs.get("headers"))

    def send(self, request: Any, **kwargs: Any) -> Any:
        """按脚本返回响应；脚本项为异常实例时直接抛出。"""
        self.sent.append(request)
        item = self._script.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item

    def close(self) -> None:
        self.close_calls += 1


def _request() -> Request:
    """构造最小 Starlette Request（与 test_relay_redirect_leak.py 保持一致）。"""
    scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": "/emby/videos/1/stream",
        "raw_path": b"/emby/videos/1/stream",
        "query_string": b"",
        "headers": [(b"host", b"testserver")],
        "client": ("127.0.0.1", 12345),
        "server": ("testserver", 80),
    }
    return Request(scope)


def _call_serve_remote(url: str, request: Request):
    """按形参名自适应调用同步版 serve_remote，兼容 (url, request) 等参数顺序。"""
    values: dict[str, Any] = {}
    for name, param in inspect.signature(streaming.serve_remote).parameters.items():
        if name in ("url", "media_url", "src"):
            values[name] = url
        elif name in ("request", "req"):
            values[name] = request
        elif param.default is inspect.Parameter.empty:
            raise AssertionError(f"serve_remote 出现未覆盖的必填形参：{name}")
    return streaming.serve_remote(**values)


def test_sync_serve_remote_closes_stream_on_malformed_location(monkeypatch):
    """用例 1：302 + 畸形 Location → 502，且已打开的流式响应必须被关闭（修复前 close_calls == 0）。"""
    resp = _FakeResponse(status_code=302, headers={"location": "http://[::1"})
    client = _FakeClient([resp])
    monkeypatch.setattr(httpx, "Client", lambda *args, **kwargs: client)

    with pytest.raises(HTTPException) as exc_info:
        _call_serve_remote(_MEDIA_URL, _request())

    assert exc_info.value.status_code == 502
    assert resp.close_calls >= 1, "同步版兜底分支漏关 resp，流式响应泄漏"
    assert client.close_calls >= 1


def test_sync_serve_remote_mount_error_still_closes_response(monkeypatch):
    """用例 2（对照组）：私网 Location 走 MountError 分支 → 403，且响应已关闭（既有行为不变）。"""
    resp = _FakeResponse(status_code=302, headers={"location": "http://127.0.0.1/secret"})
    client = _FakeClient([resp])
    monkeypatch.setattr(httpx, "Client", lambda *args, **kwargs: client)

    with pytest.raises(HTTPException) as exc_info:
        _call_serve_remote(_MEDIA_URL, _request())

    assert exc_info.value.status_code == 403
    assert resp.close_calls >= 1
