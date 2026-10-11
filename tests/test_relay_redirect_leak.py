"""回归测试：serve_remote_async 的兜底异常分支必须关闭已打开的流式响应

背景：重定向循环里拿到流式响应后，若 ``_next_redirect`` 抛出非 ``MountError`` 的异常
（例如畸形 Location ``http://[::1`` 会让 urlsplit 抛 ``ValueError: Invalid IPv6 URL``），
会落到外层 ``except Exception`` 兜底。此时若不关闭 ``resp``，这条连接永远不会归还给
进程共享的 relay 连接池，反复命中会耗尽 ``RELAY_MAX_CONNECTIONS``，
导致后续所有中转播放 503。这里用假 client 复现三条异常路径。

测试全部离线：URL / Location 只用公网 IP 字面量或畸形 URL，不做真实 DNS。
"""

import asyncio

import httpx
import pytest
from starlette.exceptions import HTTPException
from starlette.requests import Request

from backend.emby_server import streaming

# 公网 IP 字面量：避免 _resolves_private 在测试里做真实 DNS
ORIGIN = "https://93.184.216.34/v.mp4"


def _request(headers=None):
    """构造最小可用的 Starlette Request（参考 tests/test_playback_redirect.py）。"""
    scope = {
        "type": "http",
        "method": "GET",
        "path": "/v.mp4",
        "raw_path": b"/v.mp4",
        "query_string": b"",
        "headers": [
            (k.lower().encode("latin-1"), v.encode("latin-1"))
            for k, v in (headers or {}).items()
        ],
    }
    return Request(scope)


class _FakeResponse:
    """假流式响应：只关心 aclose 被调了几次，用来断言连接是否归还。"""

    def __init__(self, status_code, headers=None):
        self.status_code = status_code
        self.headers = dict(headers or {})
        self.aclose_calls = 0

    async def aclose(self):
        self.aclose_calls += 1


class _FakeClient:
    """按脚本依次返回假响应 / 抛异常的假 relay client。"""

    def __init__(self, script):
        self._script = list(script)
        self.sent_urls = []

    def build_request(self, method, url, headers=None):
        return {"method": method, "url": url, "headers": headers}

    async def send(self, request, stream=True):
        assert stream is True, "serve_remote_async 必须用流式请求"
        self.sent_urls.append(request["url"])
        item = self._script.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item


def _install(monkeypatch, script):
    """把假 client 装进 streaming 模块，返回它以便断言。"""
    client = _FakeClient(script)
    monkeypatch.setattr(streaming, "get_relay_client", lambda: client)
    return client


def test_malformed_location_closes_response(monkeypatch):
    """畸形 Location（http://[::1）→ _next_redirect 抛 ValueError → 502 且响应已关闭。"""
    resp = _FakeResponse(302, {"location": "http://[::1"})
    _install(monkeypatch, [resp])

    async def _go():
        return await streaming.serve_remote_async(ORIGIN, _request())

    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(_go())

    assert exc_info.value.status_code == 502
    # 修复前这里是 0：连接被泄漏在共享池里
    assert resp.aclose_calls >= 1


def test_rejected_location_still_closes_response(monkeypatch):
    """私网 Location → MountError → 403（既有行为不变），且响应已关闭。"""
    resp = _FakeResponse(302, {"location": "http://127.0.0.1/secret"})
    _install(monkeypatch, [resp])

    async def _go():
        return await streaming.serve_remote_async(ORIGIN, _request())

    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(_go())

    assert exc_info.value.status_code == 403
    assert resp.aclose_calls >= 1


def test_pool_timeout_closes_prior_response(monkeypatch):
    """第二跳 send 抛 PoolTimeout → 503，且第一跳的响应已关闭归还池。"""
    first = _FakeResponse(302, {"location": ORIGIN})
    _install(monkeypatch, [first, httpx.PoolTimeout("pool full")])

    async def _go():
        return await streaming.serve_remote_async(ORIGIN, _request())

    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(_go())

    assert exc_info.value.status_code == 503
    # 注意：HTTPException 的 headers 是原样透传的普通 dict，键名大小写敏感
    assert exc_info.value.headers.get("Retry-After") == "2"
    assert first.aclose_calls >= 1
