"""A-P2-2 DNS pinning 测试：DNS rebinding TOCTOU 防护。

测三件事：
1. 攻击模拟：校验时 DNS 返回公网 IP、建连时返回内网 IP → 建连必须走 pin 住的
   公网 IP（旧代码会连到内网 IP，即 SSRF 绕过成功）。
2. 事故教训不重演：DNS 解析失败时 fail-open（放行、不 pin），绝不误拦。
3. 工程质量：pin 缓存 TTL/上限、IP 故障转移、总开关、transport 注入。
"""
import asyncio
import socket
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import httpx
import pytest

from backend.emby_server import playback_security as pb
from backend.emby_server.dns_pinning import (
    AsyncPinningNetworkBackend,
    PinnedAsyncHTTPTransport,
    PinnedHTTPTransport,
    PinningCache,
    PinningNetworkBackend,
    _norm_host,
    dns_pinning_enabled,
)
from backend.emby_server.mounts import MountError

PUBLIC_IP = "93.184.216.34"
PRIVATE_IP = "169.254.169.254"


def _addrinfo(ip):
    """伪造 getaddrinfo 返回：[(family, type, proto, canon, (ip, port))]"""
    return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, 0))]


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    monkeypatch.delenv("EMBY_DNS_PINNING", raising=False)
    monkeypatch.delenv("EMBY_MEDIA_URL_ALLOWLIST", raising=False)
    monkeypatch.delenv("EMBY_BLOCK_PRIVATE_MEDIA_URLS", raising=False)


class _RecordingDelegate:
    """记录建连目标的假 backend（不同真实建连）。"""

    def __init__(self, fail_on=()):
        self.connected_to = []
        self.fail_on = set(fail_on)

    def connect_tcp(self, host, port, timeout=None, local_address=None, socket_options=None):
        self.connected_to.append(host)
        if host in self.fail_on:
            raise OSError(f"connection refused: {host}")
        return object()

    def connect_unix_socket(self, path, timeout=None, socket_options=None):
        raise NotImplementedError

    def sleep(self, seconds):
        pass


# ==================== 1. 攻击模拟：DNS rebinding ====================

def test_rebinding_attack_blocked_by_pinning(monkeypatch):
    """核心用例：攻击者控制 DNS，校验时给公网 IP、建连时给内网 IP。

    旧行为：httpx 建连时二次解析 → 连到 169.254.169.254（SSRF 绕过成功）。
    新行为：建连走 pin 住的公网 IP。
    """
    calls = {"n": 0}

    def fake_getaddrinfo(host, port, *args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            return _addrinfo(PUBLIC_IP)   # 校验时：公网 IP，通过检查
        return _addrinfo(PRIVATE_IP)      # 建连时：攻击者翻转为内网 IP

    monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)

    url, pinning = pb.validate_url_pinned("http://evil.example/x.mkv", reject_private=True)
    assert url == "http://evil.example/x.mkv"
    assert pinning == {"evil.example": [PUBLIC_IP]}

    delegate = _RecordingDelegate()
    backend = PinningNetworkBackend(pinning.get, delegate=delegate)
    backend.connect_tcp("evil.example", 80)

    # 关键断言：连的是 pin 住的公网 IP，而不是翻转后的内网 IP
    assert delegate.connected_to == [PUBLIC_IP]


def test_rebinding_old_behavior_would_hit_private(monkeypatch):
    """对照：不用 pin（旧代码路径）时，二次解析确实会拿到内网 IP。

    证明上面的攻击模拟是有效的（不是 mock 写错了）。
    """
    calls = {"n": 0}

    def fake_getaddrinfo(host, port, *args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            return _addrinfo(PUBLIC_IP)
        return _addrinfo(PRIVATE_IP)

    monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)

    # 旧代码：校验解析一次 …
    assert pb._resolves_private("evil.example") is False
    # … 建连时再解析一次，拿到的是内网 IP
    second = socket.getaddrinfo("evil.example", None, type=socket.SOCK_STREAM)
    assert second[0][4][0] == PRIVATE_IP


# ==================== 2. 事故教训：DNS 失败 fail-open ====================

def test_dns_failure_fail_open_not_pinned(monkeypatch):
    """DNS 解析失败 → 放行、不 pin（曾经 fail-closed 误拦公开 CDN 的生产事故）。"""
    def boom(host, port, *args, **kwargs):
        raise OSError("DNS 抖动")

    monkeypatch.setattr(socket, "getaddrinfo", boom)

    url, pinning = pb.validate_url_pinned("https://cdn.example.com/x.mkv", reject_private=True)
    assert url == "https://cdn.example.com/x.mkv"
    assert pinning == {}
    # 旧口径函数行为不变
    assert pb.reject_private_url("https://cdn.example.com/x.mkv")


def test_private_ip_still_blocked(monkeypatch):
    """解析到内网 IP → 照样拦（fail-open 只针对「解析失败」，不针对「解析出内网」）。"""
    monkeypatch.setattr(
        socket, "getaddrinfo", lambda h, p, *a, **k: _addrinfo(PRIVATE_IP))
    with pytest.raises(MountError):
        pb.validate_url_pinned("http://evil.example/x.mkv", reject_private=True)


def test_allowlist_still_honored(monkeypatch):
    """allowlist 是运维显式决定：命中则不校验不 pin，直接放行。"""
    monkeypatch.setenv("EMBY_MEDIA_URL_ALLOWLIST", "nas.example")
    monkeypatch.setattr(
        socket, "getaddrinfo", lambda h, p, *a, **k: _addrinfo("192.168.1.10"))
    url, pinning = pb.validate_url_pinned("http://nas.example/x.mkv", reject_private=True)
    assert url == "http://nas.example/x.mkv"
    assert pinning == {}


# ==================== 3. backend 行为 ====================

def test_unpinned_host_uses_normal_dns():
    """pin 表里没有的 host → 走原 backend（行为与修之前一致）。"""
    delegate = _RecordingDelegate()
    backend = PinningNetworkBackend({}.get, delegate=delegate)
    backend.connect_tcp("other.example", 80)
    assert delegate.connected_to == ["other.example"]


def test_pinned_ip_failover():
    """pin 的第一个 IP 连不上 → 试第二个（简易 Happy Eyeballs）。"""
    delegate = _RecordingDelegate(fail_on={"10.0.0.1"})
    backend = PinningNetworkBackend(
        {"cdn.example": ["10.0.0.1", "10.0.0.2"]}.get, delegate=delegate)
    backend.connect_tcp("cdn.example", 443)
    assert delegate.connected_to == ["10.0.0.1", "10.0.0.2"]


def test_pinned_all_fail_raises_last():
    """pin 的 IP 全连不上 → 抛最后一个异常（fail-closed 在建连层是安全的）。"""
    delegate = _RecordingDelegate(fail_on={"10.0.0.1", "10.0.0.2"})
    backend = PinningNetworkBackend(
        {"cdn.example": ["10.0.0.1", "10.0.0.2"]}.get, delegate=delegate)
    with pytest.raises(OSError):
        backend.connect_tcp("cdn.example", 443)


def test_lookup_failure_failsafe():
    """pin 表自身故障 → 退回正常 DNS，绝不拖累请求（fail-safe）。"""
    def boom(host):
        raise RuntimeError("pin table corrupted")

    delegate = _RecordingDelegate()
    backend = PinningNetworkBackend(boom, delegate=delegate)
    backend.connect_tcp("any.example", 80)
    assert delegate.connected_to == ["any.example"]


def test_host_normalization():
    """大小写/尾点规范化：EVIL.EXAMPLE. 与 evil.example 同一个 pin。"""
    delegate = _RecordingDelegate()
    backend = PinningNetworkBackend({"evil.example": [PUBLIC_IP]}.get, delegate=delegate)
    backend.connect_tcp("EVIL.EXAMPLE.", 80)
    assert delegate.connected_to == [PUBLIC_IP]
    assert _norm_host("EVIL.EXAMPLE.") == "evil.example"


# ==================== 4. PinningCache ====================

def test_pinning_cache_ttl():
    cache = PinningCache(ttl=0.05)
    cache.store("a.example", [PUBLIC_IP])
    assert cache.lookup("a.example") == [PUBLIC_IP]
    import time
    time.sleep(0.08)
    assert cache.lookup("a.example") is None


def test_pinning_cache_bounded():
    cache = PinningCache(ttl=300.0, max_entries=3)
    for i in range(5):
        cache.store(f"h{i}.example", [PUBLIC_IP])
    assert len(cache) <= 3


def test_pinning_cache_empty_store_ignored():
    cache = PinningCache()
    cache.store("a.example", [])
    assert len(cache) == 0


# ==================== 5. transport 注入 ====================

def test_pinned_http_transport_injects_backend():
    t = PinnedHTTPTransport({"a.example": [PUBLIC_IP]}.get, trust_env=False)
    backend = t._pool._network_backend
    assert isinstance(backend, PinningNetworkBackend)


def test_pinned_async_http_transport_injects_backend():
    t = PinnedAsyncHTTPTransport({"a.example": [PUBLIC_IP]}.get, trust_env=False)
    backend = t._pool._network_backend
    assert isinstance(backend, AsyncPinningNetworkBackend)


def test_kill_switch_disables_pinning(monkeypatch):
    """EMBY_DNS_PINNING=0 → 建连回退到正常 DNS（紧急回滚，无需重新部署）。"""
    monkeypatch.setenv("EMBY_DNS_PINNING", "0")
    assert dns_pinning_enabled() is False

    delegate = _RecordingDelegate()
    t = PinnedHTTPTransport({"evil.example": [PUBLIC_IP]}.get, trust_env=False)
    # 把 recording delegate 换进 pin backend 的委托链
    t._pool._network_backend._delegate = delegate
    t._pool._network_backend.connect_tcp("evil.example", 80)
    # 开关关闭 → 无视 pin，走正常解析（delegate 收到的是原 hostname）
    assert delegate.connected_to == ["evil.example"]


def test_kill_switch_default_on():
    assert dns_pinning_enabled() is True


# ==================== 6. 端到端：真实 httpx+httpcore 走 pin 建连 ====================

class _Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        body = b"ok"
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


def test_end_to_end_pinned_request_real_http():
    """本地起真 HTTP 服务：fake 主机名 pin 到 127.0.0.1，走完整 httpx 栈拿到 200。

    证明 pin 在真实的 httpcore 建连路径上生效（不是 mock 自嗨）。
    """
    server = HTTPServer(("127.0.0.1", 0), _Handler)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        transport = PinnedHTTPTransport(
            {"fake-host.invalid": ["127.0.0.1"]}.get, trust_env=False)
        with httpx.Client(transport=transport, timeout=5.0) as client:
            resp = client.get(f"http://fake-host.invalid:{port}/")
            assert resp.status_code == 200
            assert resp.text == "ok"
    finally:
        server.shutdown()


def test_end_to_end_async_pinned_request_real_http():
    """异步版端到端。"""
    server = HTTPServer(("127.0.0.1", 0), _Handler)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    async def go():
        transport = PinnedAsyncHTTPTransport(
            {"fake-host.invalid": ["127.0.0.1"]}.get, trust_env=False)
        async with httpx.AsyncClient(transport=transport, timeout=5.0) as client:
            resp = await client.get(f"http://fake-host.invalid:{port}/")
            assert resp.status_code == 200
            assert resp.text == "ok"

    try:
        asyncio.run(go())
    finally:
        server.shutdown()
