"""播放响应的「绝不 302」护栏。

## 背景

曾经有两处会**把客户端 302 到上游**：``video_stream`` 的 ``?direct=true``
分支（配合 ``can_redirect_direct``），以及下载 / 拉文件两个端点对「无凭据直链」
的重定向。Google Drive 的真直链已确认不可行（302 带不过 Authorization 头，
token 放 URL 会被限流），这些分支连同 ``can_redirect_direct`` 一起删掉了。

现在**任何来源都只走服务端代理转发**，所以这里改成反向断言：即便显式带上
``?direct=true``，响应也必须是代理结果而不是 302。
"""
import asyncio
from types import SimpleNamespace

import pytest
from starlette.requests import Request

from backend.emby_server import api
from backend.emby_server.mounts import PlayTarget


def _request(query: str = ""):
    scope = {"type": "http", "method": "GET", "path": "/Videos/item/stream",
             "raw_path": b"/Videos/item/stream", "query_string": query.encode(),
             "headers": [], "scheme": "http", "server": ("testserver", 80),
             "client": ("testclient", 50000), "root_path": ""}
    return Request(scope)


def _stub_gates(monkeypatch):
    """本文件只测「代理 / 不代理」这一层：付费墙与客户端策略各自有自己的用例，

    这里都按放行处理（否则每个用例都要造一个真数据库会话）。
    """
    monkeypatch.setattr(api, "ensure_playback_allowed", lambda db, user: None)
    monkeypatch.setattr(api.playback_policy, "ensure_client_allowed", lambda db, user, ua: None)


def _stub_stream(monkeypatch, target, proxied):
    monkeypatch.setattr(api, "_require_item", lambda db, item_id: SimpleNamespace(container="mp4"))
    # 假 db/user 读不到可见范围；S12 起读失败 fail-closed，这里显式按「不过滤」桩掉
    monkeypatch.setattr(api, "_library_scope", lambda db_, user_: None)
    _stub_gates(monkeypatch)
    monkeypatch.setattr(api, "_play_target", lambda db, item: target)

    async def fake_proxy(url, request, headers, media_type, cache_control=None, **kwargs):
        proxied.append(url)
        return object()

    monkeypatch.setattr(api, "serve_remote_async", fake_proxy)


@pytest.mark.parametrize("query", ["", "direct=true", "direct=TRUE"])
def test_video_stream_never_redirects_the_client(monkeypatch, query):
    """公开直链也一样走代理：显式 ``?direct=true`` 也不再 302。

    曾经带凭据的目标会拒绝重定向、公开直链却会 302；那条分叉随 Google Drive
    真直链一起下线，现在没有任何目标会被重定向。
    """
    target = PlayTarget("url", "https://cdn.example/movie.mkv", {"User-Agent": "server"})
    proxied: list = []
    _stub_stream(monkeypatch, target, proxied)

    response = asyncio.run(api.video_stream("item", _request(query), object(), object()))

    assert proxied == [target.value], "必须由本服务代理转发"
    assert not hasattr(response, "status_code"), f"响应不该是 302：{response!r}"


def test_video_stream_proxies_with_range_headers(monkeypatch):
    """代理转发是分片响应：带上可缓存头（CDN 边缘缓存用），兼容既有实现。"""
    target = PlayTarget("url", "https://cdn.example/movie.mkv", {"User-Agent": "server"})
    monkeypatch.setattr(api, "_require_item", lambda db, item_id: SimpleNamespace(container="mp4"))
    # 假 db/user 读不到可见范围；S12 起读失败 fail-closed，这里显式按「不过滤」桩掉
    monkeypatch.setattr(api, "_library_scope", lambda db_, user_: None)
    _stub_gates(monkeypatch)
    monkeypatch.setattr(api, "_play_target", lambda db, item: target)
    proxy = object()

    async def fake_proxy(url, request, headers, media_type, cache_control=None, **kwargs):
        assert (url, headers, media_type) == (target.value, target.headers, "video/mp4")
        from backend.emby_server import cdn
        assert cache_control == cdn.SEGMENT_CACHE_HEADER
        return proxy

    monkeypatch.setattr(api, "serve_remote_async", fake_proxy)
    response = asyncio.run(api.video_stream("item", _request(), object(), object()))
    assert response is proxy