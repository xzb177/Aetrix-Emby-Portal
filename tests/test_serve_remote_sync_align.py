"""回归测试：同步版 serve_remote 与异步版 serve_remote_async 行为对齐。

此前只在异步版具备、同步版遗漏的两项行为：

1. 内部头 ``X-Pickcode`` 不得下发给源站（115 直链缓存失效用的内部标识）；
   ``_redirect_headers`` 只剥 ``authorization/cookie/proxy-authorization``，
   X-Pickcode 不在名单里，跨主机重定向时会继续外泄。
2. 115 直链过期（403/404）时清理直链缓存（``_invalidate_115_url``），
   客户端重试即可拿到新直链，而不是干等到 3 小时 TTL 到期。

全部离线：URL 使用公网 IP 字面量，不做真实 DNS / 网络访问；
用假 ``httpx.Client`` / 假 ``Response`` 替换真实实现（与
test_serve_remote_sync_leak.py 同一手法：直接 monkeypatch 全局 httpx.Client）。
"""
from __future__ import annotations

import asyncio

import httpx
import pytest
from fastapi import HTTPException
from starlette.requests import Request

from backend.emby_server import streaming

_MEDIA_URL = "http://203.0.113.10/media.mkv"


class _FakeResponse:
    """最小假响应：headers 用 httpx.Headers 保证大小写不敏感，统计 close 次数。"""

    def __init__(self, status_code: int = 200, headers: dict | None = None,
                 chunks: tuple = (b"ok",)):
        self.status_code = status_code
        self.headers = httpx.Headers(headers or {})
        self._chunks = list(chunks)
        self.close_calls = 0

    def iter_bytes(self, chunk_size=None):
        yield from self._chunks

    def close(self) -> None:
        self.close_calls += 1


class _FakeClient:
    """按脚本返回响应的假 httpx.Client，记录发出的请求。"""

    def __init__(self, responses: list):
        self._responses = list(responses)
        self.sent: list = []
        self.close_calls = 0

    def build_request(self, method: str, url, **kwargs) -> httpx.Request:
        return httpx.Request(method, url, headers=kwargs.get("headers"))

    def send(self, request, **kwargs):
        self.sent.append(request)
        return self._responses.pop(0)

    def close(self) -> None:
        self.close_calls += 1


def _request() -> Request:
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


def test_sync_pops_x_pickcode(monkeypatch):
    """内部头 X-Pickcode 不应下发给源站，其它头（如 User-Agent）保留。"""
    resp = _FakeResponse(200, headers={"content-length": "2"})
    client = _FakeClient([resp])
    monkeypatch.setattr(httpx, "Client", lambda *a, **k: client)

    result = streaming.serve_remote(
        _MEDIA_URL, _request(),
        {"X-Pickcode": "pc123", "User-Agent": "ua"},
        "video/x-matroska",
    )

    assert client.sent, "应当向源站发出请求"
    sent_headers = client.sent[0].headers
    assert "x-pickcode" not in sent_headers, "内部头 X-Pickcode 外泄给了源站"
    assert sent_headers.get("user-agent") == "ua"
    # 耗尽响应流，触发 iter_remote 的 finally（关 resp/client），避免资源告警。
    # 注：Starlette 1.x 的 StreamingResponse 会把同步生成器包成 async 生成器。
    async def _drain():
        async for _ in result.body_iterator:
            pass
    asyncio.run(_drain())
    assert resp.close_calls >= 1


def test_sync_invalidates_115_cache_on_403(monkeypatch):
    """403 且携带 X-Pickcode 时：清 115 直链缓存，并把 403 透传给客户端。"""
    client = _FakeClient([_FakeResponse(403, headers={})])
    monkeypatch.setattr(httpx, "Client", lambda *a, **k: client)

    calls: list = []
    monkeypatch.setattr(
        "backend.emby_server.mounts._invalidate_115_url",
        lambda pickcode: calls.append(pickcode),
    )

    with pytest.raises(HTTPException) as exc_info:
        streaming.serve_remote(
            _MEDIA_URL, _request(), {"X-Pickcode": "pc999"}, "video/x-matroska",
        )

    assert exc_info.value.status_code == 403
    assert calls == ["pc999"], "403 时应调用 _invalidate_115_url 清掉过期直链缓存"


def test_sync_no_invalidate_without_pickcode(monkeypatch):
    """404 但未携带 X-Pickcode 时：不应触碰 115 直链缓存。"""
    client = _FakeClient([_FakeResponse(404, headers={})])
    monkeypatch.setattr(httpx, "Client", lambda *a, **k: client)

    calls: list = []
    monkeypatch.setattr(
        "backend.emby_server.mounts._invalidate_115_url",
        lambda pickcode: calls.append(pickcode),
    )

    with pytest.raises(HTTPException) as exc_info:
        streaming.serve_remote(
            _MEDIA_URL, _request(), {"User-Agent": "ua"}, "video/x-matroska",
        )

    assert exc_info.value.status_code == 404
    assert calls == []
