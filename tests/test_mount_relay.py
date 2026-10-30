# -*- coding: utf-8 -*-
"""远程挂载专用中继（2026-10-17 用户拍板：给 115 留一条专用中继，其他照删）。

覆盖：
1. video_stream 分支（P1 回归的复现与修复验证）：
   - mount:// 条目（kind="url"、无 drive_file_id）→ 走 serve_mount_relay_async，
     不再 410（修复前这条用例会 410，即复现）。
   - 非挂载的 url 条目（本地 .strm 里一条普通 http 直链）→ 仍 410。
   - Drive 条目（有 drive_file_id，即使 file_path 是 mount://）→ 仍优先 302。
2. serve_mount_relay_async 本体（本地测试服务器，真 HTTP，不碰外网）：
   - Range 透传：上游 206 → 中继回 206 + Content-Range 透回。
   - Accept-Ranges 兜底：上游不发时中继必须补 ``Accept-Ranges: bytes``
     （2026-10-30 公网丢头事故的后端契约；丢头点在反代层，见 PR 说明）。
   - 凭据头转发：Cookie/UA 发给上游；X-Pickcode（内部头）不转发。
   - 上游 404 → 抛 404（不是 410），上游 5xx → 抛 502。
   - 重定向到内网 IP 被拒绝（SSRF 守卫保留）：抛 403。

全部离线：db/user/鉴权全部桩掉；网络只连 127.0.0.1 测试服务器。
"""
import os

os.environ.setdefault("DATABASE_TYPE", "sqlite")
os.environ.setdefault("REDIS_ENABLED", "false")

import asyncio
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from starlette.requests import Request
from starlette.responses import Response

from backend.emby_server import api
from backend.emby_server import playback_strategy as _strategy
from backend.emby_server import streaming as _streaming
from backend.emby_server.mounts import PlayTarget

FID = "1eXUpWksxfCCrIZUye8lo24JGXyr_2ZQO"
BODY = b"0123456789" * 100  # 1000 字节，供 Range 测试


class _RangeHandler(BaseHTTPRequestHandler):
    """支持 Range 的最小测试源站；记录收到的请求头供断言。"""

    received = []  # 类级：记录 (path, headers-dict)
    mode = "ok"    # ok | notfound | error | redirect_private

    def _record(self):
        type(self).received.append(
            (self.path, {k.lower(): v for k, v in self.headers.items()}))

    def _send_body(self, status, body, extra=None):
        self.send_response(status)
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        self._record()
        mode = type(self).mode
        if mode == "notfound":
            self._send_body(404, b"nope")
            return
        if mode == "error":
            self._send_body(500, b"boom")
            return
        if mode == "redirect_private":
            self.send_response(302)
            self.send_header("Location", "http://127.0.0.1:9/gone")
            self.end_headers()
            return
        if mode == "no_accept":
            # 上游不发 Accept-Ranges（模拟部分源站/公开链的实际行为）：
            # 中继必须自行补上（serve_mount_relay_async 的 setdefault），
            # 否则 206 到播放器手里缺头、iOS 系渐进播放兼容差。
            range_h = self.headers.get("Range")
            if range_h and range_h.startswith("bytes="):
                spec = range_h[len("bytes="):].split("-")
                try:
                    start = int(spec[0]) if spec[0] else 0
                except ValueError:
                    start = 0
                end = int(spec[1]) if len(spec) > 1 and spec[1] else len(BODY) - 1
                end = min(end, len(BODY) - 1)
                part = BODY[start:end + 1]
                self._send_body(206, part, {
                    "Content-Range": f"bytes {start}-{end}/{len(BODY)}",
                    "Content-Type": "video/mp4",
                })
                return
            self._send_body(200, BODY, {"Content-Type": "video/mp4"})
            return
        # ok：支持 Range
        range_h = self.headers.get("Range")
        if range_h and range_h.startswith("bytes="):
            spec = range_h[len("bytes="):].split("-")
            try:
                start = int(spec[0]) if spec[0] else 0
            except ValueError:
                start = 0
            end = int(spec[1]) if len(spec) > 1 and spec[1] else len(BODY) - 1
            end = min(end, len(BODY) - 1)
            part = BODY[start:end + 1]
            self._send_body(206, part, {
                "Content-Range": f"bytes {start}-{end}/{len(BODY)}",
                "Accept-Ranges": "bytes",
                "Content-Type": "video/mp4",
            })
            return
        self._send_body(200, BODY, {
            "Accept-Ranges": "bytes", "Content-Type": "video/mp4"})

    def log_message(self, *args):
        pass


@pytest.fixture()
def upstream():
    """起一个本地测试源站，yield 基地址；用完关闭。"""
    _RangeHandler.received = []
    _RangeHandler.mode = "ok"
    srv = HTTPServer(("127.0.0.1", 0), _RangeHandler)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield f"http://127.0.0.1:{srv.server_port}"
    srv.shutdown()
    srv.server_close()


def _request(range_header=None):
    headers = []
    if range_header:
        headers = [(b"range", range_header.encode())]
    scope = {"type": "http", "method": "GET", "path": "/Videos/item/stream",
             "raw_path": b"/Videos/item/stream", "query_string": b"",
             "headers": headers, "scheme": "http", "server": ("testserver", 80),
             "client": ("testclient", 50000), "root_path": ""}
    return Request(scope)


def _stub_common(monkeypatch, item, target):
    monkeypatch.setattr(api, "_require_visible_item", lambda db, user, item_id: item)
    monkeypatch.setattr(api, "ensure_playback_allowed", lambda db, user: None)
    # 播放门禁与付费墙同口径桩掉（本文件测中继分支，不测门禁本身；
    # 门禁行为见 tests/test_playback_gate.py）。
    monkeypatch.setattr(
        "backend.emby_server.playback_gate.ensure_binding_playback_allowed",
        lambda db, u, r=None: None)
    monkeypatch.setattr(api.play_sign, "check_referer", lambda *a, **k: None)
    monkeypatch.setattr(api, "_play_target", lambda db, it: target)
    monkeypatch.setattr(_strategy, "maybe_direct_url", lambda db, fid: None)


async def _call_and_drain(coro):
    """一次 event loop 里完成「调中继 + 读完流」，返回 (status, headers, body)。"""
    resp = await coro
    chunks = []
    async for c in resp.body_iterator:
        chunks.append(c)
    return resp.status_code, dict(resp.headers), b"".join(chunks)


# ---------------- 1. video_stream 分支 ----------------

def test_mount_item_goes_to_mount_relay_not_410(monkeypatch):
    """P1 回归修复：mount:// 条目 kind=url → 专用中继，不再 410。"""
    item = SimpleNamespace(container="mkv", drive_file_id="",
                           file_path="mount://3/Movies/a.mkv")
    target = PlayTarget("url", "https://115.example.com/d/abc",
                        {"User-Agent": "115-ua", "Cookie": "c=1",
                         "X-Pickcode": "pc123"})
    _stub_common(monkeypatch, item, target)

    calls = {}

    async def fake_relay(url, request, headers, media_type, pt=None, sniff_quota_page=False):
        calls.update(url=url, headers=headers, media_type=media_type)
        return Response(b"fake-bytes", media_type="video/mp4")

    monkeypatch.setattr(_streaming, "serve_mount_relay_async", fake_relay)

    resp = asyncio.run(api.video_stream("item", _request(), object(), object()))

    assert resp.status_code == 200
    assert calls["url"] == "https://115.example.com/d/abc"
    assert calls["headers"]["Cookie"] == "c=1"
    assert calls["media_type"] == "video/mkv"


def test_non_mount_url_item_still_410(monkeypatch):
    """非挂载的 url 条目（本地 .strm 里一条普通 http 直链）→ 仍 410。"""
    item = SimpleNamespace(container="mkv", drive_file_id="",
                           file_path="/strm/Movies/x.strm")
    target = PlayTarget("url", "https://cdn.example.com/x.mp4", {})
    _stub_common(monkeypatch, item, target)

    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(api.video_stream("item", _request(), object(), object()))
    assert exc_info.value.status_code == 410


def test_drive_item_with_fid_still_302_first(monkeypatch):
    """Drive 条目（有 drive_file_id）→ 302 优先，不走挂载中继。

    2026-10-18 P0：drive 直链 302 走快路径（存在性校验后直接 302，
    跳过 _play_target 的 Drive 路径步行）。此处把存在性校验桩成通过，
    断言的仍是"302 优先、不进中继"这一条语义。
    """
    item = SimpleNamespace(container="mkv", drive_file_id=FID,
                           file_path="mount://3/Movies/a.mkv")
    target = PlayTarget("url", f"https://drive.google.com/uc?id={FID}", {})
    _stub_common(monkeypatch, item, target)
    monkeypatch.setattr(_strategy, "drive_direct_play_enabled", lambda db: True)

    async def _exists_ok(db, it, fid, force_recheck=False):
        assert fid == FID
        return True

    monkeypatch.setattr(api, "_ensure_drive_file_playable", _exists_ok)

    resp = asyncio.run(api.video_stream("item", _request(), object(), object()))
    assert resp.status_code == 302
    assert "drive.usercontent.google.com" in resp.headers["location"]


def test_mount_relay_mark_exists():
    """新中继符号存在且是 Drive 中继之外的独立实现。"""
    assert callable(_streaming.serve_mount_relay_async)
    assert not hasattr(_streaming, "serve_remote_async"), \
        "已删的通用中继不许回来"


# ---------------- 2. serve_mount_relay_async 本体 ----------------

def test_relay_range_passthrough_206(upstream):
    """Range 透传：客户端要 bytes=0-99 → 上游 206 → 中继回 206 + Content-Range。"""
    req = _request(range_header="bytes=0-99")
    status, headers, body = asyncio.run(_call_and_drain(
        _streaming.serve_mount_relay_async(
            upstream + "/movie.mkv", req, {"User-Agent": "t-ua"}, "video/mp4")))
    assert status == 206
    assert body == BODY[0:100]
    assert headers.get("content-range") == f"bytes 0-99/{len(BODY)}"
    assert headers.get("accept-ranges") == "bytes"
    # 上游确实收到了 Range 头
    _paths = [p for p, h in _RangeHandler.received]
    assert _paths and _RangeHandler.received[0][1].get("range") == "bytes=0-99"


def test_relay_accept_ranges_set_when_upstream_omits_it_206(upstream):
    """回归兜底（2026-10-30）：上游 206 不带 Accept-Ranges 时，中继响应
    必须仍带 ``Accept-Ranges: bytes``。

    生产事故：公网中继 206 丢 Accept-Ranges（逐跳定位丢在 VPS nginx 的
    proxy_cache 继承，后端直连带头）。后端这一层是契约的源头——上游
    （谷歌公开链/挂载源）发不发不可控，中继 setdefault 补齐不许删，
    本用例钉住它。"""
    _RangeHandler.mode = "no_accept"
    req = _request(range_header="bytes=0-99")
    status, headers, body = asyncio.run(_call_and_drain(
        _streaming.serve_mount_relay_async(
            upstream + "/movie.mkv", req, {"User-Agent": "t-ua"}, "video/mp4")))
    assert status == 206
    assert body == BODY[0:100]
    assert headers.get("content-range") == f"bytes 0-99/{len(BODY)}"
    assert headers.get("accept-ranges") == "bytes"


def test_relay_accept_ranges_set_when_upstream_omits_it_200(upstream):
    """整段 200 同口径：上游不发 Accept-Ranges，中继也必须补上。"""
    _RangeHandler.mode = "no_accept"
    req = _request()
    status, headers, body = asyncio.run(_call_and_drain(
        _streaming.serve_mount_relay_async(
            upstream + "/movie.mkv", req, {"User-Agent": "t-ua"}, "video/mp4")))
    assert status == 200
    assert body == BODY
    assert headers.get("accept-ranges") == "bytes"


def test_relay_forwards_credentials_strips_pickcode(upstream):
    """凭据头转发给上游；X-Pickcode 是内部头，不许转发。"""
    req = _request()
    status, headers, body = asyncio.run(_call_and_drain(
        _streaming.serve_mount_relay_async(
            upstream + "/movie.mkv", req,
            {"User-Agent": "t-ua", "Cookie": "sess=abc", "X-Pickcode": "pc123"},
            "video/mp4")))
    assert status == 200
    assert body == BODY
    got = _RangeHandler.received[-1][1]
    assert got.get("cookie") == "sess=abc"
    assert got.get("user-agent") == "t-ua"
    assert "x-pickcode" not in got


def test_relay_upstream_404_maps_to_404(upstream):
    """上游 404 → 抛 404（不是 410；410 只留给 Drive 死链语义，已删）。"""
    _RangeHandler.mode = "notfound"
    req = _request()
    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(_streaming.serve_mount_relay_async(
            upstream + "/gone.mkv", req, {}, "video/mp4"))
    assert exc_info.value.status_code == 404


def test_relay_upstream_500_maps_to_502(upstream):
    """上游 5xx → 抛 502。"""
    _RangeHandler.mode = "error"
    req = _request()
    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(_streaming.serve_mount_relay_async(
            upstream + "/boom.mkv", req, {}, "video/mp4"))
    assert exc_info.value.status_code == 502


def test_relay_rejects_private_redirect_target(upstream):
    """SSRF 守卫保留：重定向到内网 IP → 403 拒绝。"""
    _RangeHandler.mode = "redirect_private"
    req = _request()
    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(_streaming.serve_mount_relay_async(
            upstream + "/redir", req, {}, "video/mp4"))
    assert exc_info.value.status_code == 403
