"""DNS pinning：堵住重定向校验的 DNS TOCTOU（A-P2-2）。

威胁模型
--------
``reject_private_url`` 在校验重定向目标时做一次 DNS 解析（``socket.getaddrinfo``），
而 httpx 在实际建连时又会做一次。攻击者若控制目标域名的 DNS，就可以在校验时
返回公网 IP、建连时返回 ``169.254.169.254``/内网 IP（DNS rebinding），从而绕过
「重定向目标一律拦内网」的 SSRF 防护。

修复
----
校验时解析一次，把 IP **pin 住**；建连时直接连 pin 住的 IP，不再二次解析。
pin 只发生在 TCP 层换对端 IP，httpcore 的 TLS SNI / 证书校验仍用 URL 主机名，
所以 HTTPS 不会坏。

事故教训（fail-open 必须保留）
------------------------------
``_resolves_private`` 在 DNS 解析失败时 fail-open，是因为曾经 fail-closed 把
「DNS 抖动/离线/临时故障」当成「内网地址」拒掉，连公开 CDN 域名都播不了
（生产事故）。本模块延续该教训：**解析失败 → 不 pin、照常放行**（请求会自然
失败为「源站不可达」），绝不重演误拦。

总开关 ``EMBY_DNS_PINNING``（默认开）
--------------------------------------
关掉即回到修复前的行为（校验与建连各做一次 DNS），用于紧急回滚，无需重新部署
（读环境变量，实时生效）。
"""
from __future__ import annotations

import os
import threading
import time
from typing import Callable, Dict, List, Optional

import httpx

# lookup 协议：lookup(host) -> pin 住的 IP 列表，或 None（无 pin，走正常 DNS）
Lookup = Callable[[str], Optional[List[str]]]


def dns_pinning_enabled() -> bool:
    """DNS pinning 总开关（``EMBY_DNS_PINNING``，默认开）。"""
    return os.getenv("EMBY_DNS_PINNING", "true").strip().lower() in {
        "1", "true", "yes", "on",
    }


def _norm_host(host: str) -> str:
    return host.lower().rstrip(".")


class PinningCache:
    """线程安全的 TTL pin 缓存：host -> [ip, ...]。

    给 async 共享 client 用：多个请求共享 pin 表，TTL（默认 300 秒）封顶陈旧度，
    条目数封顶防内存膨胀。每次经 ``reject_private_url`` 校验通过的重定向目标
    都会刷新自己的 pin，所以正常流量下 pin 一直是新鲜的。
    """

    def __init__(self, ttl: float = 300.0, max_entries: int = 4096) -> None:
        self._ttl = ttl
        self._max = max_entries
        self._lock = threading.Lock()
        self._data: Dict[str, tuple[List[str], float]] = {}

    def lookup(self, host: str) -> Optional[List[str]]:
        key = _norm_host(host)
        now = time.monotonic()
        with self._lock:
            item = self._data.get(key)
            if item is None:
                return None
            ips, expires_at = item
            if expires_at <= now:
                self._data.pop(key, None)
                return None
            return ips

    def store(self, host: str, ips: List[str]) -> None:
        if not ips:
            return
        key = _norm_host(host)
        with self._lock:
            if len(self._data) >= self._max and key not in self._data:
                # 淘汰最早插入的（dict 按插入序）；TTL 本来就会清过期项，
                # 这里只是内存上限的兜底。
                self._data.pop(next(iter(self._data)))
            self._data[key] = (list(ips), time.monotonic() + self._ttl)

    def clear(self) -> None:
        with self._lock:
            self._data.clear()

    def __len__(self) -> int:  # 测试用
        with self._lock:
            return len(self._data)


def _try_pinned_ips(connect, ips: List[str], host: str, port: int,
                    timeout, local_address, socket_options):
    """按顺序试 pin 住的 IP（简易 Happy Eyeballs）：全失败则抛最后一个异常。"""
    last_exc: Optional[BaseException] = None
    for ip in ips:
        try:
            return connect(ip, port, timeout, local_address, socket_options)
        except Exception as exc:  # noqa: BLE001 — 换下一个 IP 继续试
            last_exc = exc
    assert last_exc is not None
    raise last_exc


class PinningNetworkBackend:
    """同步版 httpcore network_backend 装饰器。

    ``lookup`` 命中时用 pin 住的 IP 建连，未命中时委托原 backend（正常 DNS）。
    只换 TCP 对端，SNI/证书校验不受影响。
    """

    def __init__(self, lookup: Lookup, delegate=None) -> None:
        self._lookup = lookup
        self._delegate = delegate

    def _pinned(self, host: str) -> Optional[List[str]]:
        try:
            return self._lookup(_norm_host(host))
        except Exception:
            # pin 表自身故障绝不能拖累请求：退回正常 DNS（fail-safe）
            return None

    def connect_tcp(self, host: str, port: int, timeout=None,
                    local_address=None, socket_options=None):
        ips = self._pinned(host)
        if not ips:
            return self._delegate.connect_tcp(
                host, port, timeout, local_address, socket_options)
        return _try_pinned_ips(
            self._delegate.connect_tcp, ips, host, port,
            timeout, local_address, socket_options)

    def connect_unix_socket(self, path: str, timeout=None, socket_options=None):
        return self._delegate.connect_unix_socket(path, timeout, socket_options)

    def sleep(self, seconds: float) -> None:
        self._delegate.sleep(seconds)


class AsyncPinningNetworkBackend:
    """异步版 httpcore network_backend 装饰器（语义同同步版）。"""

    def __init__(self, lookup: Lookup, delegate=None) -> None:
        self._lookup = lookup
        self._delegate = delegate

    def _pinned(self, host: str) -> Optional[List[str]]:
        try:
            return self._lookup(_norm_host(host))
        except Exception:
            return None

    async def connect_tcp(self, host: str, port: int, timeout=None,
                          local_address=None, socket_options=None):
        ips = self._pinned(host)
        if not ips:
            return await self._delegate.connect_tcp(
                host, port, timeout, local_address, socket_options)
        last_exc: Optional[BaseException] = None
        for ip in ips:
            try:
                return await self._delegate.connect_tcp(
                    ip, port, timeout, local_address, socket_options)
            except Exception as exc:  # noqa: BLE001 — 换下一个 IP 继续试
                last_exc = exc
        assert last_exc is not None
        raise last_exc

    async def connect_unix_socket(self, path: str, timeout=None, socket_options=None):
        return await self._delegate.connect_unix_socket(path, timeout, socket_options)

    async def sleep(self, seconds: float) -> None:
        await self._delegate.sleep(seconds)


def _inject_pinning(pool, backend) -> None:
    """把 pin 版 backend（已构造好的实例）塞进已建好的 httpcore pool。

    连接是懒建的，此时替换安全。pool 原来的 backend 成为 pin backend 的委托
    （delegate），未命中 pin 时行为与之前完全一致。

    找不到 ``_network_backend`` 说明 httpx/httpcore 版本变了——大声失败，
    让 CI 抓住，而不是静默失去 TOCTOU 防护。
    """
    old = getattr(pool, "_network_backend", None)
    if old is None:
        raise RuntimeError(
            "httpcore pool 没有 _network_backend 属性："
            "httpx/httpcore 版本可能已变更，DNS pinning 无法注入"
        )
    backend._delegate = old
    pool._network_backend = backend


class PinnedHTTPTransport(httpx.HTTPTransport):
    """带 DNS pinning 的同步 transport（给 ``httpx.Client(transport=...)`` 用）。

    ``lookup`` 是共享引用（``dict.get`` 这类 bound method 或
    ``PinningCache.lookup``）：校验通过后往 pin 表里写，transport 实时生效，
    无需重建 client。
    总开关 ``EMBY_DNS_PINNING`` 在每次建连时检查，关掉即回退到正常 DNS。
    """

    def __init__(self, lookup: Lookup, **kwargs) -> None:
        super().__init__(**kwargs)

        def _lookup(host: str) -> Optional[List[str]]:
            if not dns_pinning_enabled():
                return None
            return lookup(host)

        _inject_pinning(
            self._pool, PinningNetworkBackend(_lookup))


class PinnedAsyncHTTPTransport(httpx.AsyncHTTPTransport):
    """带 DNS pinning 的异步 transport（语义同同步版）。"""

    def __init__(self, lookup: Lookup, **kwargs) -> None:
        super().__init__(**kwargs)

        def _lookup(host: str) -> Optional[List[str]]:
            if not dns_pinning_enabled():
                return None
            return lookup(host)

        _inject_pinning(
            self._pool, AsyncPinningNetworkBackend(_lookup))
