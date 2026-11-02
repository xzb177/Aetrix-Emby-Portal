"""远程挂载中继：客户端不读也不关的连接必须让出上游连接。

播放器（Infuse / AVPlayer 系）拖动或重开时会弃用旧连接但不 close：
TCP 一直挂着、接收窗口填满后就不再读。中继这边 ``iter_mount`` 停在
``yield`` 上等 ASGI send 排空，上游 115/网盘连接和共享池名额
（``_mount_relay_client`` 池上限 32）被一直占住，直到客户端退出播放。
几路这样的「僵尸连接」就能把池占满，后续所有播放 PoolTimeout → 503。

这里用真 uvicorn + 真 socket 复现：客户端读一小段后停读（不关连接），
断言超过 ``MOUNT_RELAY_STALL_TIMEOUT_SEC`` 后上游连接被关闭。
"""
import os
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
import uvicorn
from fastapi import FastAPI, Request

from backend.emby_server import streaming as _streaming

TOTAL = 512 * 1024 * 1024  # 远大于各级 socket 缓冲，保证客户端停读后上游写会阻塞
PIECE = b"x" * (256 * 1024)


class _EndlessHandler(BaseHTTPRequestHandler):
    """一直往外写的上游；记录当前仍在写的连接数。"""

    active = 0
    lock = threading.Lock()

    def do_GET(self):
        with type(self).lock:
            type(self).active += 1
        try:
            self.send_response(200)
            self.send_header("Content-Type", "video/mp4")
            self.send_header("Content-Length", str(TOTAL))
            self.end_headers()
            sent = 0
            while sent < TOTAL:
                self.wfile.write(PIECE)
                sent += len(PIECE)
        except OSError:
            pass  # 中继关了上游连接
        finally:
            with type(self).lock:
                type(self).active -= 1

    def log_message(self, *args):
        pass


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture()
def relay_stack(monkeypatch):
    monkeypatch.setattr(_streaming, "_mount_relay_stall_timeout", lambda: 1.0)
    _EndlessHandler.active = 0
    upstream = ThreadingHTTPServer(("127.0.0.1", 0), _EndlessHandler)
    threading.Thread(target=upstream.serve_forever, daemon=True).start()
    up_url = f"http://127.0.0.1:{upstream.server_address[1]}/movie.mkv"

    app = FastAPI()

    @app.get("/relay")
    async def relay(request: Request):
        return await _streaming.serve_mount_relay_async(
            up_url, request, {"User-Agent": "t-ua"}, "video/mp4")

    port = _free_port()
    server = uvicorn.Server(uvicorn.Config(
        app, host="127.0.0.1", port=port, log_level="warning", lifespan="off"))
    t = threading.Thread(target=server.run, daemon=True)
    t.start()
    deadline = time.time() + 10
    while not server.started and time.time() < deadline:
        time.sleep(0.05)
    yield port
    server.should_exit = True
    t.join(timeout=5)
    upstream.shutdown()


def _wait(pred, timeout):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if pred():
            return True
        time.sleep(0.1)
    return pred()


def _open_and_stall(port: int) -> socket.socket:
    sock = socket.socket()
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 64 * 1024)
    sock.connect(("127.0.0.1", port))
    sock.sendall(b"GET /relay HTTP/1.1\r\nHost: t\r\n\r\n")
    got = 0
    while got < 2 * 1024 * 1024:
        chunk = sock.recv(65536)
        assert chunk, "relay closed before streaming"
        got += len(chunk)
    return sock  # 之后不再读，也不关——模拟被播放器弃用的连接


def test_stalled_client_releases_upstream(relay_stack):
    sock = _open_and_stall(relay_stack)
    try:
        assert _wait(lambda: _EndlessHandler.active == 1, 5)
        # 停读超过阈值（1s）后，上游连接应被中继关掉
        assert _wait(lambda: _EndlessHandler.active == 0, 15), \
            "stalled client kept the upstream connection open"
    finally:
        sock.close()


def test_reading_client_is_not_cut(relay_stack):
    """正常读取（哪怕慢）的客户端不受影响：持续读 3 倍阈值时长仍在流。"""
    sock = socket.socket()
    sock.connect(("127.0.0.1", relay_stack))
    sock.sendall(b"GET /relay HTTP/1.1\r\nHost: t\r\n\r\n")
    try:
        end = time.time() + 3.5
        while time.time() < end:
            assert sock.recv(65536), "reading client was cut off"
            time.sleep(0.05)
        assert _EndlessHandler.active == 1
    finally:
        sock.close()
