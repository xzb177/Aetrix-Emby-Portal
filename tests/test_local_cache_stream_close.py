"""回归测试：local_cache._open_remote_stream 异常路径漏关流式响应。

背景
----
``streaming.serve_remote`` 的同类泄漏已在 #584 / #587 修复（中转代理兜底异常分支
漏关流式响应，慢泄漏共享连接池）。本次审计发现
``local_cache._open_remote_stream`` 存在同口径的漏网之鱼：

1. ``for ... else`` 重定向超限分支：循环里最后一跳 3xx 响应仍处于打开状态，
   ``except _RemoteError`` 只关了 client；
2. 兜底 ``except Exception`` 分支：``Location`` 畸形导致 ``_next_redirect`` 抛错时，
   已经打开的 3xx 流式响应未被关闭，同样只关了 client。

两个用例全部离线执行：URL 只使用公网 IP 字面量，不触发真实 DNS 解析与网络访问。
"""
from __future__ import annotations

import httpx
import pytest

from backend.emby_server.local_cache import _RemoteError, _open_remote_stream
from backend.emby_server.streaming import MAX_REDIRECTS

# 公网 IP 字面量：避免任何真实 DNS 解析
_TARGET = "https://93.184.216.34/v.mp4"


class _FakeResponse:
    """记录 ``close`` 次数的流式响应替身。"""

    def __init__(
        self,
        status_code: int = 200,
        headers: dict | None = None,
        body: bytes = b"",
    ) -> None:
        self.status_code = status_code
        self.headers = httpx.Headers(headers or {})
        self._body = body
        self.close_calls = 0

    def close(self) -> None:
        self.close_calls += 1

    def iter_bytes(self, *args, **kwargs):
        """成功路径兜底：本测试只覆盖异常路径，实际不会被消费。"""
        yield self._body


class _FakeClient:
    """按脚本依次返回响应 / 抛异常的 ``httpx.Client`` 替身。"""

    def __init__(self, script: list) -> None:
        self._script = list(script)
        self.close_calls = 0

    def build_request(self, method, url, headers=None, **kwargs):
        # 返回真实 httpx.Request，保证 URL 拼接 / 请求头处理等逻辑可用
        return httpx.Request(method, url, headers=headers, **kwargs)

    def send(self, request, **kwargs):
        item = self._script.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item

    def close(self) -> None:
        self.close_calls += 1


@pytest.fixture
def install_client(monkeypatch):
    """把 ``httpx.Client`` 替换成脚本化的假客户端。"""

    def _install(script):
        client = _FakeClient(script)
        monkeypatch.setattr(httpx, "Client", lambda *args, **kwargs: client)
        return client

    return _install


def test_fallback_exception_closes_stream(install_client):
    """兜底 except 分支：Location 畸形时也必须关掉已打开的流式响应。"""
    resp = _FakeResponse(302, {"location": "http://[::1"})
    client = install_client([resp])

    with pytest.raises(_RemoteError) as excinfo:
        _open_remote_stream(_TARGET, {}, 0)

    assert "源站不可达" in str(excinfo.value)
    # 修复前：兜底分支只关 client，resp.close_calls == 0
    assert resp.close_calls >= 1
    assert client.close_calls >= 1


def test_redirect_limit_closes_last_stream(install_client):
    """for...else 超限分支：最后一跳 3xx 响应也必须被关闭。"""
    responses = [
        _FakeResponse(302, {"location": f"https://93.184.216.34/next{i}"})
        for i in range(MAX_REDIRECTS + 1)
    ]
    client = install_client(responses)

    with pytest.raises(_RemoteError) as excinfo:
        _open_remote_stream(_TARGET, {}, 0)

    assert "源站重定向次数过多" in str(excinfo.value)
    # 非最后一跳会被循环内正常 resp.close()，只有最后一跳泄漏
    assert responses[-1].close_calls >= 1
    assert client.close_calls >= 1
