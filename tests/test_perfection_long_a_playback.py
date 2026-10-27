# -*- coding: utf-8 -*-
"""完美审查 A 批（播放链路）：first_play 真实路径回归 + play_sign 开关口径。

背景：既有 tests/test_first_play.py 只注入探针（_http / probe 桩），
真实 urllib 路径上 403/401 会被 urlopen 抛成 HTTPError、旧代码吞进
通用 except 报 status=0，轮换循环首轮即 break——“403 换 SA 最多
5 次”在真实路径上从未生效。本文件用本地真实 HTTP 服务器（不碰
谷歌、不碰真实 SA）钉死真实路径行为。
"""
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from backend.emby_server import first_play as fp
from backend.emby_server import play_sign
from backend.integrations import store


def _serve(handler_cls):
    srv = HTTPServer(("127.0.0.1", 0), handler_cls)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_address[1]}/f"


class _StatusByPathHandler(BaseHTTPRequestHandler):
    """按路径回固定状态：/403、/401、/429、/206（带 Content-Range）。"""

    def do_GET(self):
        mapping = {"/403": 403, "/401": 401, "/429": 429, "/206": 206}
        status = mapping.get(self.path, 500)
        body = b"x" if status == 206 else b""
        self.send_response(status)
        if status == 206:
            self.send_header("Content-Range", "bytes 0-0/1000")
            self.send_header("Content-Type", "video/x-matroska")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if body:
            self.wfile.write(body)

    def log_message(self, *args):  # noqa: ARG002
        pass


def test_probe_real_path_reports_http_error_status():
    srv, base = _serve(_StatusByPathHandler)
    try:
        assert fp.probe_alt_media(base.replace("/f", "/403"), "tok")["status"] == 403
        assert fp.probe_alt_media(base.replace("/f", "/401"), "tok")["status"] == 401
        assert fp.probe_alt_media(base.replace("/f", "/429"), "tok")["status"] == 429
        ok = fp.probe_alt_media(base.replace("/f", "/206"), "tok")
        assert ok["ok"] is True and ok["status"] == 206
        assert ok["content_range"] == "bytes 0-0/1000"
    finally:
        srv.shutdown()


class _RotateHandler(BaseHTTPRequestHandler):
    """tok0 → 403（坏 SA），tok1 → 206（好 SA），其余 → 403。"""

    def do_GET(self):
        if self.headers.get("Authorization") == "Bearer tok1":
            body = b"x"
            self.send_response(206)
            self.send_header("Content-Range", "bytes 0-0/1000")
            self.send_header("Content-Type", "video/x-matroska")
            self.send_header("Content-Length", "1")
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_response(403)
            self.send_header("Content-Length", "0")
            self.end_headers()

    def log_message(self, *args):  # noqa: ARG002
        pass


def test_resolve_real_path_rotates_on_403(monkeypatch):
    srv, url = _serve(_RotateHandler)
    try:
        monkeypatch.setattr(fp, "alt_media_url", lambda fid: url)
        seen = []

        def token_fn(attempt):
            seen.append(attempt)
            return f"tok{attempt}"

        result = fp.resolve_verified_alt_media("fid1", token_fn)
        assert result["ok"] is True
        assert result["token"] == "tok1"
        assert result["rotations"] == 1
        assert seen == [0, 1]
    finally:
        srv.shutdown()


def test_resolve_real_path_429_does_not_rotate_but_reports_429(monkeypatch):
    srv, base = _serve(_StatusByPathHandler)
    try:
        monkeypatch.setattr(
            fp, "alt_media_url", lambda fid: base.replace("/f", "/429"))
        seen = []

        def token_fn(attempt):
            seen.append(attempt)
            return "tok"

        result = fp.resolve_verified_alt_media("fid1", token_fn)
        assert result["ok"] is False
        assert seen == [0]  # 429 换 SA 无帮助，直接放弃
        assert result["last_status"] == 429  # 不再谎报成 0
    finally:
        srv.shutdown()


def test_hotlink_enabled_read_failure_stays_enabled(monkeypatch):
    """配置读失败回落默认启用、不弱化验签（与 auth.py 同口径注释一致）。

    旧 play_sign.hotlink_enabled 文档写“fail-open（放行）”，与代码
    （回落 HOTLINK_DEFAULT=True）及 auth.py 注释矛盾，本批只改文档、
    行为用本用例钉死：读失败 = 启用 = 验签不弱化。
    """

    def _boom(db, key):  # noqa: ARG002
        raise RuntimeError("config store down")

    monkeypatch.setattr(store, "get_value", _boom)
    assert play_sign.hotlink_enabled(db=object()) is True
