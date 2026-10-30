# -*- coding: utf-8 -*-
"""起播 / 拖动提速回归（2026-10-30）：中继与 quota 探测的连接复用。

生产证据（2026-10-30 15:12 用户真机录屏 + EA 日志，item 688899）：
稳态 PlaybackInfo 808ms 几乎全是一发新鲜 quota 裁决 probe；每次
stream / 拖动首字节 1.6–2.2s ≈ probe（约 0.75s）+ 中继 alt=media
新建连接取首字节（约 0.87s）。VPS 实测到谷歌单次 TCP+TLS 握手约
0.11–0.16s，而 probe 两腿与中继此前都是每请求新建 httpx 客户端、
关客户端连 transport 一起关，握手每发重付。

本文件钉住新口径（只动连接层，不动任何裁决/顺序/出法）：
1. 中继客户端同一 event loop 内共享同一实例；serve 完成后不关它。
2. probe 两腿共享同一同步客户端；对同一源站的多次探测只建一条
   连接（本地 keep-alive 源站数连接数）。
判定新鲜度（#1495）、出流顺序（副本优先/门禁/fast302/挂载分支）
一律不归本文件管，见 test_usercopy_* / test_mount_serving_order。
"""
import os

os.environ.setdefault("DATABASE_TYPE", "sqlite")
os.environ.setdefault("REDIS_ENABLED", "false")

import asyncio
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace

from backend.emby_server import streaming
from backend.emby_server import transfer_variant as tv


# ---------------- 中继（async） ----------------

def test_mount_relay_client_shared_within_loop():
    """同一 loop 内两次取中继客户端必须是同一实例（连接池共享）。"""

    async def _two():
        return streaming._mount_relay_client(), streaming._mount_relay_client()

    c1, c2 = asyncio.run(_two())
    assert c1 is c2


class _FakeResp:
    def __init__(self, chunks):
        self._chunks = list(chunks)
        self.status_code = 206
        self.headers = {"content-type": "video/mp4",
                        "content-range": "bytes 0-9/100",
                        "accept-ranges": "bytes"}
        self.closed = False

    async def aiter_bytes(self, _n):
        for chunk in self._chunks:
            yield chunk

    async def aclose(self):
        self.closed = True


class _FakeClient:
    def __init__(self):
        self.closed = False

    async def aclose(self):
        self.closed = True


def test_serve_mount_relay_does_not_close_client(monkeypatch):
    """serve 完成后只关响应、不关（共享）客户端，连接才归还得到池里。"""
    resp = _FakeResp([b"0123456789"])
    client = _FakeClient()

    async def _follow(c, url, forward, silent=False):
        return resp, url, 0

    monkeypatch.setattr(streaming, "_follow_redirects", _follow)
    monkeypatch.setattr(streaming, "_mount_relay_client", lambda: client)
    request = SimpleNamespace(headers={})

    async def _serve_and_collect():
        response = await streaming.serve_mount_relay_async(
            "https://example.com/file", request)
        return b"".join([c async for c in response.body_iterator])

    body = asyncio.run(_serve_and_collect())
    assert body == b"0123456789"
    assert resp.closed
    assert not client.closed


# ---------------- probe（sync） ----------------

class _CountingHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    conns = []

    def setup(self):
        super().setup()
        type(self).conns.append(self.connection)

    def do_GET(self):
        body = b"x" * 512
        self.send_response(206)
        self.send_header("Content-Type", "video/mp4")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


def test_probe_legs_reuse_one_connection():
    """两腿 + 重复探测打同一源站，只许建一条连接（keep-alive 复用）。"""
    _CountingHandler.conns = []
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _CountingHandler)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    try:
        url = f"http://127.0.0.1:{srv.server_port}/f"
        s1, _c1, h1 = tv._usercopy_probe_http(url)
        s2, _c2, h2 = tv._usercopy_probe_http_get(url)
        s3, _c3, h3 = tv._usercopy_probe_http(url)
        assert (s1, s2, s3) == (206, 206, 206)
        assert len(h1) == 512 and len(h2) == 512 and len(h3) == 512
        assert len(_CountingHandler.conns) == 1, (
            f"期望 1 条连接复用，实际 {len(_CountingHandler.conns)} 条")
    finally:
        srv.shutdown()
        srv.server_close()


def test_shared_probe_client_is_singleton():
    assert tv._shared_probe_client() is tv._shared_probe_client()
