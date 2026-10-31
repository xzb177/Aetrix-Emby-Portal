# -*- coding: utf-8 -*-
"""审计 A-119 回归：苹果 CMS 取数是 A-053 漏掉的第四路 SSRF。

红测说明：旧代码上本文件全红——``_default_cms_http_get`` 仍是裸
``urlopen``（跟随重定向、不复验解析）；修后全绿。本地服务器只绑
127.0.0.1（内网），命中计数证明“请求根本没发出去”。

三项覆盖（报告建议全做）：
①域名解析到私网时苹果 CMS 取数被拦；
②302 到 127.0.0.1 时不跟随；
③生产三链（同步／播放／探活）默认 http_get 下均走 safe 层。
"""
import inspect
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace

import pytest

from backend.emby_server import applecms_fetcher as acf

_CMS_OK = {"code": 1, "msg": "ok", "page": 1, "pagecount": 1, "limit": 20,
           "total": 1, "list": [{"vod_id": 101, "vod_name": "t",
                                 "vod_play_from": "g1",
                                 "vod_play_url": ("E01$"
                                                  "http://cdn.example.com/"
                                                  "a.m3u8")}]}


def _serve(handler_cls):
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler_cls)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def _port(server):
    return server.server_address[1]


def _json_handler(hits, payload):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            hits.append(self.path)
            body = json.dumps(payload).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

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


def test_fetch_page_to_loopback_literal_blocked_no_hit():
    """①字面回环：苹果 CMS 取数被拦、服务器 0 命中。"""
    hits = []
    server = _serve(_json_handler(hits, _CMS_OK))
    try:
        with pytest.raises(acf.AppleCMSError) as exc:
            acf.fetch_page(f"http://127.0.0.1:{_port(server)}")
    finally:
        server.shutdown()
        server.server_close()
    assert exc.value.kind == "network"
    assert hits == []


def test_fetch_page_to_localhost_blocked_no_hit():
    """①域名解析到私网：localhost 解析到回环，取数被拦、0 命中。"""
    hits = []
    server = _serve(_json_handler(hits, _CMS_OK))
    try:
        with pytest.raises(acf.AppleCMSError) as exc:
            acf.fetch_page(f"http://localhost:{_port(server)}")
    finally:
        server.shutdown()
        server.server_close()
    assert exc.value.kind == "network"
    assert hits == []


def test_redirect_to_loopback_not_followed(monkeypatch):
    """②302 到 127.0.0.1 时不跟随：内网目标 0 命中、状态 302。

    初始校验用补丁放行本地 A（只为隔离测重定向行为本身）；
    302 目标不被请求是本测试的断言对象。
    """
    from backend.emby_server import upstream_http

    hits_b = []
    server_b = _serve(_json_handler(hits_b, _CMS_OK))
    hits_a = []
    try:
        location = (f"http://127.0.0.1:{_port(server_b)}/api.php/"
                    "provide/vod/?ac=list&pg=1")
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
            status, _payload = acf._default_cms_http_get(
                f"http://127.0.0.1:{port_a}/api.php/provide/vod/"
                "?ac=list&pg=1",
                {}, 5)
        finally:
            server_a.shutdown()
            server_a.server_close()
    finally:
        server_b.shutdown()
        server_b.server_close()
    assert status == 302
    assert hits_a != []
    assert hits_b == []


def test_default_impl_walks_safe_layer():
    """③默认函数体引用断言：默认实现必须走 safe_urlopen。"""
    src = inspect.getsource(acf._default_cms_http_get)
    assert "safe_urlopen" in src


def _recording_stub(calls):
    def stub(url, headers, timeout):
        calls.append(url)
        return 200, _CMS_OK
    return stub


def test_sync_chain_defaults_walk_module_default(monkeypatch):
    """③同步链：生产适配器默认分支透传 None，落到模块默认实现。"""
    from backend.emby_server import applecms_production as acp

    calls = []
    monkeypatch.setattr(acf, "_default_cms_http_get",
                        _recording_stub(calls))
    source = SimpleNamespace(base_url="https://cms.example.com",
                             user_agent=None, referer=None,
                             extra_headers_json=None)
    page = acp.make_fetch_page()(source, "1", 1)
    assert page["code"] == 1
    detail = acp.make_fetch_detail()(source, 101)
    assert detail["vod_id"] == 101
    assert len(calls) == 2
    assert all("/api.php/provide/vod/" in url for url in calls)


def test_probe_chain_defaults_walk_module_default(monkeypatch):
    """③探活链：探活默认分支透传 None，落到模块默认实现。"""
    from backend.emby_server import applecms_probe as acp_probe
    from backend.emby_server import upstream_health as uh

    calls = []
    monkeypatch.setattr(acf, "_default_cms_http_get",
                        _recording_stub(calls))
    monkeypatch.setenv("APPLECMS_PROBE_ENABLED", "1")
    src = acp_probe.AppleCMSSource(key="9",
                                   base_url="https://cms.example.com",
                                   enabled=True, headers={})
    result = acp_probe.probe_applecms_source(src, uh.InMemoryHealthStore())
    assert result["outcome"] == "success"
    assert len(calls) == 1
    assert "/api.php/provide/vod/" in calls[0]


def test_playback_chain_defaults_walk_module_default(monkeypatch):
    """③播放链：播放分支默认分支透传 None，落到模块默认实现。"""
    from backend.emby_server import upstream_playback as up

    calls = []
    monkeypatch.setattr(acf, "_default_cms_http_get",
                        _recording_stub(calls))
    monkeypatch.setenv("APPLECMS_ENABLED", "1")
    source = SimpleNamespace(id=9, base_url="https://cms.example.com",
                             user_agent=None, referer=None,
                             extra_headers_json=None)
    address = up._resolve_applecms_stream(
        source, "101",
        http_probe=lambda url, headers, timeout: (200, None))
    assert address == "http://cdn.example.com/a.m3u8"
    assert len(calls) == 1
    assert "/api.php/provide/vod/" in calls[0]
