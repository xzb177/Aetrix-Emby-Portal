"""审计 A-053 回归：上游拉取 SSRF 纵深（解析复验 + 不跟随重定向）。

红测说明：本文件顶层只导旧模块（聚合/探活/播放），出站公共层
在用到的测试里才惰性导入——因此在旧代码上全文件跑红（行为类
测试断言失败、层相关测试 ImportError），修后全绿。

本地服务器只绑 127.0.0.1（内网），命中计数证明“请求根本没发出去”。
"""
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from backend.emby_server import upstream_aggregate_run as agg
from backend.emby_server import upstream_health as uh
from backend.emby_server import upstream_playback as up


def _serve(handler_cls):
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler_cls)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server


def _json_handler(hits):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _reply(self):
            hits.append(self.path)
            body = json.dumps({"ok": True}).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        do_GET = _reply
        do_POST = _reply

    return Handler


def _redirect_handler(hits, location):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            hits.append(self.path)
            self.send_response(302)
            self.send_header("Location", location)
            self.send_header("Content-Length", "0")
            self.end_headers()

    return Handler


def _port(server):
    return server.server_address[1]


@pytest.mark.parametrize("host", ["127.0.0.1", "0x7f.0.0.1", "2130706433"])
def test_get_to_loopback_forms_blocked_no_hit(host):
    """聚合 GET：字面回环与非十进制回环写法一律拦截、服务器 0 命中。"""
    hits = []
    server = _serve(_json_handler(hits))
    try:
        status, body = agg._default_http_get(
            f"http://{host}:{_port(server)}/Items",
            {"Accept": "application/json"},
            5,
        )
    finally:
        server.shutdown()
        server.server_close()
    assert (status, body) == (None, None)
    assert hits == []


def test_post_to_loopback_blocked_no_hit():
    """聚合认证 POST：回环地址拦截、服务器 0 命中。"""
    hits = []
    server = _serve(_json_handler(hits))
    try:
        status, body = agg._default_http_post(
            f"http://127.0.0.1:{_port(server)}/Users/AuthenticateByName",
            {"Username": "u", "Pw": "p"},
            {"Accept": "application/json"},
            5,
        )
    finally:
        server.shutdown()
        server.server_close()
    assert (status, body) == (None, None)
    assert hits == []


def test_health_get_to_loopback_blocked_no_hit():
    """探活 GET：回环地址拦截、服务器 0 命中。"""
    hits = []
    server = _serve(_json_handler(hits))
    try:
        status, body = uh._default_http_get(
            f"http://127.0.0.1:{_port(server)}/System/Info/Public",
            {"Accept": "application/json"},
            5,
        )
    finally:
        server.shutdown()
        server.server_close()
    assert (status, body) == (None, None)
    assert hits == []


def test_range_probe_to_loopback_blocked_no_hit():
    """播放 Range 探活：回环地址拦截、服务器 0 命中。"""
    hits = []
    server = _serve(_json_handler(hits))
    try:
        status, retry = up._default_http_range_probe(
            f"http://127.0.0.1:{_port(server)}/Videos/x/stream",
            {"Accept": "*/*"},
            5,
        )
    finally:
        server.shutdown()
        server.server_close()
    assert (status, retry) == (None, None)
    assert hits == []


def test_redirect_not_followed(monkeypatch):
    """重定向不跟随：初始地址放行后，302 目标（内网）0 命中、状态 302。

    初始校验用补丁放行本地 A（只为隔离测重定向行为本身）；
    302 目标不被请求是本测试的断言对象。
    """
    from backend.emby_server import upstream_http

    hits_b = []
    server_b = _serve(_json_handler(hits_b))
    hits_a = []
    try:
        location = f"http://127.0.0.1:{_port(server_b)}/System/Info/Public"
        server_a = _serve(_redirect_handler(hits_a, location))
        try:
            orig_verify = upstream_http.verify_outbound_url
            port_a = _port(server_a)

            def _allow_a_only(url):
                if url.startswith(f"http://127.0.0.1:{port_a}/"):
                    return None
                return orig_verify(url)

            monkeypatch.setattr(
                upstream_http, "verify_outbound_url", _allow_a_only
            )
            status, body = agg._default_http_get(
                f"http://127.0.0.1:{port_a}/Items",
                {"Accept": "application/json"},
                5,
            )
        finally:
            server_a.shutdown()
            server_a.server_close()
    finally:
        server_b.shutdown()
        server_b.server_close()
    assert status == 302
    assert hits_a != []
    assert hits_b == []


def test_positive_control_real_opener(monkeypatch):
    """正向对照：校验放行时真实 opener 能取到本地服务器 200。"""
    from backend.emby_server import upstream_http

    hits = []
    server = _serve(_json_handler(hits))
    try:
        monkeypatch.setattr(
            upstream_http, "verify_outbound_url", lambda url: None
        )
        status, body = agg._default_http_get(
            f"http://127.0.0.1:{_port(server)}/Items",
            {"Accept": "application/json"},
            5,
        )
    finally:
        server.shutdown()
        server.server_close()
    assert status == 200
    assert body == {"ok": True}
    assert hits != []


def test_validate_base_url_rejects_private_literals():
    """探活校验：字面私网 IP 拒绝，公网域名仍放行（校验本身不解析 DNS）。"""
    with pytest.raises(ValueError):
        uh.validate_base_url("http://127.0.0.1:8096")
    with pytest.raises(ValueError):
        uh.validate_base_url("http://10.0.0.5:8096")
    assert uh.validate_base_url("http://example.com:8096") == "http://example.com:8096"


def test_verify_outbound_url_units():
    """出站校验单测：字面 IP 直判、非法协议与 userinfo 拒绝、公网放行。"""
    from backend.emby_server.upstream_http import (
        OutboundBlockedError,
        is_public_ip,
        verify_outbound_url,
    )

    assert is_public_ip("8.8.8.8") is True
    assert is_public_ip("127.0.0.1") is False
    assert is_public_ip("10.0.0.5") is False
    assert is_public_ip("::ffff:127.0.0.1") is False  # IPv4 映射形式不许绕过
    assert is_public_ip("::1") is False
    for bad in (
        "http://127.0.0.1:8096/x",
        "http://10.0.0.5:8096/x",
        "http://[::1]:8096/x",
        "http://0x7f.0.0.1:80/x",
        "ftp://example.com/x",
        "http://user:pass@example.com/x",
    ):
        with pytest.raises(OutboundBlockedError):
            verify_outbound_url(bad)
    verify_outbound_url("http://93.184.216.34/x")  # 字面公网 IP，不走 DNS 即放行
