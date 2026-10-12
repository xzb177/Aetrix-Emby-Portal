"""进程内滑动窗口限流器

用于保护登录/注册/认证等端点免遭暴力破解。
单实例部署下足够；如需多实例共享限流状态，可将存储替换为 Redis。
"""
from __future__ import annotations

import ipaddress
import os
import threading
import time
from collections import defaultdict, deque
from typing import Optional

# P2（完美审查 A-P2-3）：X-Real-IP 合法性校验复用 domain_guard 的 is_valid_ip，
# 横切能力只许一套，不另起。
from backend.domain_guard import is_valid_ip


class SlidingWindowLimiter:
    """滑动窗口计数限流：window 秒内最多 max_events 次"""

    # 超过这个 key 数就先做一次清理（清理是 O(keys)，别每次请求都做）
    _PURGE_THRESHOLD = 5000

    def __init__(self) -> None:
        self._hits: dict[str, deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()
        self._max_window = 0.0

    def check(self, key: str, max_events: int, window_seconds: float) -> tuple[bool, int]:
        """返回 (是否放行, 剩余需等待秒数)"""
        now = time.monotonic()
        with self._lock:
            if window_seconds > self._max_window:
                self._max_window = window_seconds
            q = self._hits[key]
            cutoff = now - window_seconds
            while q and q[0] < cutoff:
                q.popleft()
            if len(q) >= max_events:
                retry_after = int(window_seconds - (now - q[0])) + 1
                return False, max(retry_after, 1)
            q.append(now)
            # 防止 key 无限增长：超阈值时清掉「已经不可能再命中」的桶。
            # 不能只看空队列——某个 IP 打过一次之后再没出现，队列里那条时间戳会永远留着。
            if len(self._hits) > self._PURGE_THRESHOLD:
                self._purge(now)
            return True, 0

    def _purge(self, now: float) -> int:
        """丢掉所有已过期的桶（调用方持锁）

        判定用**见过的最大窗口**：比任何还在生效的窗口都老的桶，留着也不会有用。
        """
        span = self._max_window or 60.0
        dead = [
            k for k, v in self._hits.items()
            if not v or v[-1] < now - span
        ]
        for k in dead:
            self._hits.pop(k, None)
        return len(dead)

    def reset(self, key: str) -> None:
        with self._lock:
            self._hits.pop(key, None)


_limiter = SlidingWindowLimiter()


def check_rate_limit(key: str, max_events: int, window_seconds: float) -> tuple[bool, int]:
    """模块级便捷入口"""
    return _limiter.check(key, max_events, window_seconds)


def _trusted_proxies() -> list:
    """从环境变量 TRUSTED_PROXIES 解析可信代理列表。

    格式：逗号分隔的 IP 或 CIDR，如 "127.0.0.1,172.18.0.0/16"
    默认信任本地回环（127.0.0.0/8, ::1）。
    """
    raw = os.getenv("TRUSTED_PROXIES", "").strip()
    nets = []
    # 默认始终信任回环地址
    for default in ("127.0.0.0/8", "::1/128"):
        try:
            nets.append(ipaddress.ip_network(default))
        except ValueError:
            pass
    if raw:
        for part in raw.split(","):
            part = part.strip()
            if not part:
                continue
            try:
                # 单个 IP 自动转为 /32 或 /128
                if "/" not in part:
                    nets.append(ipaddress.ip_network(part + ("/128" if ":" in part else "/32")))
                else:
                    nets.append(ipaddress.ip_network(part))
            except ValueError:
                # 配置写错了就忽略，不让服务起不来
                continue
    return nets


def _is_trusted_proxy(ip: str) -> bool:
    """直连 IP 是否在可信代理列表里"""
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return False
    for net in _trusted_proxies():
        try:
            if addr in net:
                return True
        except TypeError:
            # IPv4 vs IPv6 不匹配就跳过
            continue
    return False


def get_client_ip(request) -> str:
    """获取真实客户端 IP（防伪造）。

    只有当直连方是可信代理（TRUSTED_PROXIES）时，才信任
    X-Forwarded-For / X-Real-IP 头；否则直接用 request.client.host。

    原理：XFF 和 X-Real-IP 都是客户端可伪造的请求头。只有当请求
    确实经过我们自己的反代（nginx）时，这些头才可信——而判断依据
    是 TCP 直连的对端 IP 是否在可信代理列表里（这个伪造不了）。

    可信代理场景下的优先级：
    1. X-Real-IP：nginx 按 $remote_addr 硬写
    2. X-Forwarded-For 最后一段：最近一跳代理追加的
    3. 直连 IP
    """
    if request is None:
        return "unknown"
    direct_ip = request.client.host if request.client else "unknown"

    # 直连方不是可信代理：所有转发头都不可信，直接用直连 IP
    if not _is_trusted_proxy(direct_ip):
        return direct_ip

    # 可信代理：X-Real-IP 优先（nginx 硬写，客户端伪造的会被覆盖）
    real = (request.headers.get("x-real-ip") or "").strip()
    # P2 修复（完美审查 A-P2-3）：必须是合法 IP 才采信——回环直连者可任意伪造
    # X-Real-IP 绕限流（每次换桶）并污染 LoginLog.ip。非法值回落到 XFF/直连 IP。
    if real and is_valid_ip(real):
        return real
    # 其次取 XFF 最后一段（最近一跳追加的才是真实客户端）
    fwd = request.headers.get("x-forwarded-for")
    if fwd:
        hops = [part.strip() for part in fwd.split(",") if part.strip()]
        if hops:
            return hops[-1]
    return direct_ip


def client_ip(request) -> str:
    """提取客户端 IP（只能信任我们自己的反代重新写入的头）

    安全：此值同时用于登录/注册/核销的限流键与登录日志，之前取
    `X-Forwarded-For` 的**第一段**——而仓库里的 Nginx 用的是
    `$proxy_add_x_forwarded_for`（把客户端自带的头追加在前面），
    于是任何人只要每次伪造一个 `X-Forwarded-For: 1.2.3.4` 就能换一个限流桶，
    暴力破解与刷接口形同不设限。现在的优先级：

    1. `X-Real-IP`：Nginx 按 `$remote_addr` 硬写，客户端无法伪造
    2. `X-Forwarded-For` 的**最后一段**：由最近一跳代理追加，才是真实客户端
    3. 直连时的 `request.client.host`

    注意：已升级为可信代理校验版本（get_client_ip），保留此函数名做兼容。
    """
    return get_client_ip(request)
