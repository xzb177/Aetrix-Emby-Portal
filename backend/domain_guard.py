"""域名强制 + Cloudflare 真实 IP：流节点隐藏源站 IP 的通用能力。

背景（分离架构）：流节点部署在高宽带大盘机器上，公网入口走 Cloudflare
（橙云）代理隐藏真实 IP，用户只能用域名访问。本模块提供两块开箱即用的能力：

1. ``DomainGuardMiddleware`` —— 强制域名访问：
   - 管理后台「流媒体加速」开关打开时，自动用配置的加速域名做白名单
     （DB 配置，进程内 60 秒 TTL 缓存，最长 60 秒生效）；
   - 环境变量 ``ENFORCE_DOMAIN``（逗号分隔，如 ``stream.example.com``）是运维
     手动覆盖：DB 开关未打开时才看它；都没设置时**完全透传**，单机部署行为一字不变；
   - 设置后，Host 头不在白名单的请求一律 403（防直接用 IP 绕过 CF 打源站）；
   - 例外：``/api/health`` 允许来自内网 IP（RFC1918 / 本机）的健康检查，
     主服务的节点健康检查走内网 IP 时不被误杀。

2. ``CloudflareIPMiddleware`` —— 取真实用户 IP：
   - 「流媒体加速」开关打开时自动启用；否则看环境变量 ``TRUST_CF_IP=true``
     （默认关闭，不信任任何代理头）；
   - **只有 TCP 直连对端是可信代理时才读代理头**（安全修复 H4）：Cloudflare 官方网段
     （内置，可用 ``CLOUDFLARE_IP_RANGES`` 覆盖），以及与客户端公开地址同一份点名名单
     （``peer_honors_forwarded_headers``：本机回环、``TRUSTED_PROXIES``、
     ``FORWARDED_ALLOW_IPS`` 里的精确 IP / CIDR）。
     ``FORWARDED_ALLOW_IPS=*`` **不**让任意对端改写客户端 IP（限流、审计会一起中招）；
     那个通配只留给公开 URL 的 scheme/host 推断，与 uvicorn 对齐。见到 ``*`` 记一次 WARNING。
     直连源站的请求带 ``CF-Connecting-IP: 127.0.0.1`` 不再生效；
   - 优先 ``CF-Connecting-IP``，回退 ``X-Forwarded-For`` **最后一段**（最近一跳代理追加的，
     首段是客户端可伪造的）；
   - 还原出的地址是回环 / 未指定地址时拒绝（防止借此冒充本机绕过 /metrics 等）；
   - 只接受合法 IP 格式，非法的直接忽略（不让伪造头污染日志/限流）；
   - 把解析出的 IP 写回 ``scope["client"]``，下游的限流/日志拿到的就是真人 IP。

两个都是**纯 ASGI 中间件**（直接实现 ``__call__(scope, receive, send)``），
绝不使用 BaseHTTPMiddleware —— 后者会缓冲整个响应体，大文件流式播放会被它
拖死（2026-10-07 PR #388 的教训）。只碰 ``http.response.start`` 的头 / 直接
短路返回，不读、不改、不缓冲 body。
"""

from __future__ import annotations

import ipaddress
import logging
import os
from typing import Awaitable, Callable, Optional

logger = logging.getLogger(__name__)

# 环境变量
ENFORCE_DOMAIN_ENV = "ENFORCE_DOMAIN"
TRUST_CF_IP_ENV = "TRUST_CF_IP"

# 健康检查路径：内网来源放行，不受域名强制约束
HEALTH_PATHS = ("/api/health",)


# ---- 管理后台「流媒体加速」开关（DB 配置，store 统一 60 秒 TTL 缓存） ----
# 横切能力只许一套：带缓存的配置热读统一走 backend.integrations.store，
# 这里不另起一套 TTL。

def _accel_db_config() -> tuple[Optional[set[str]], Optional[bool]]:
    """读流媒体加速开关的 DB 配置。

    返回 ``(allowed_hosts, trust_cf)``；开关未打开或 DB 不可用时返回
    ``(None, None)``，调用方回退到环境变量。
    """
    try:
        from backend.database import SessionLocal
        from backend.integrations import store

        db = SessionLocal()
        try:
            from backend.emby_server.stream_accel import CONFIG_DOMAIN, CONFIG_ENABLED
            vals = store.get_values(
                db,
                [CONFIG_ENABLED, CONFIG_DOMAIN],
                {CONFIG_ENABLED: "false", CONFIG_DOMAIN: ""},
                ttl=60.0,
            )
        finally:
            db.close()
    except Exception:
        logger.debug("流媒体加速 DB 配置读取失败，回退环境变量", exc_info=True)
        return None, None
    enabled = (vals.get(CONFIG_ENABLED) or "").strip().lower() in (
        "1", "true", "yes", "on",
    )
    domain = (vals.get(CONFIG_DOMAIN) or "").strip().lower()
    if enabled and domain:
        hosts = {h.strip().lower() for h in domain.split(",") if h.strip()}
        if hosts:
            return hosts, True
    return None, None


def _get_allowed_hosts() -> Optional[set[str]]:
    allowed, _ = _accel_db_config()
    if allowed:
        return allowed
    raw = (os.environ.get(ENFORCE_DOMAIN_ENV) or "").strip()
    if not raw:
        return None
    hosts = {h.strip().lower() for h in raw.split(",") if h.strip()}
    return hosts or None


def _trust_cf_ip() -> bool:
    _, trust_cf = _accel_db_config()
    if trust_cf:
        return True
    return (os.environ.get(TRUST_CF_IP_ENV) or "").strip().lower() in (
        "1", "true", "yes", "on",
    )


def _host_of(headers: dict[bytes, bytes]) -> str:
    for k, v in headers.items():
        if k.lower() == b"host":
            try:
                text = v.decode("latin-1").strip().lower()
            except Exception:
                return ""
            # 去掉端口：stream.example.com:8001 -> stream.example.com
            if text.startswith("["):  # IPv6 字面量 [::1]:8001
                end = text.find("]")
                return text[: end + 1] if end != -1 else text
            return text.split(":")[0]
    return ""


def _client_ip(scope: dict) -> str:
    client = scope.get("client")
    if isinstance(client, (list, tuple)) and client:
        return str(client[0])
    return ""


def _is_private_ip(ip: str) -> bool:
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return False
    return addr.is_private or addr.is_loopback


def is_valid_ip(ip: str) -> bool:
    """公开工具：校验字符串是否为合法 IPv4/IPv6 地址。

    R18-P3：原为私有 _valid_ip，被 ratelimit 跨模块引用；公开化以正名。
    """
    try:
        ipaddress.ip_address(ip)
        return True
    except ValueError:
        return False


class DomainGuardMiddleware:
    """强制域名访问的纯 ASGI 中间件（见模块文档）。"""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope.get("type") not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return
        allowed = _get_allowed_hosts()
        if not allowed:
            await self.app(scope, receive, send)
            return

        path = scope.get("path") or ""
        # 健康检查 + 内网来源：放行（主服务节点健康检查走内网 IP）
        if path in HEALTH_PATHS and _is_private_ip(_client_ip(scope)):
            await self.app(scope, receive, send)
            return

        headers = dict(scope.get("headers") or [])
        host = _host_of(headers)
        if host not in allowed:
            logger.warning("域名守卫拦截非域名访问: host=%r path=%s", host, path)
            body = b'{"error":"forbidden","reason":"direct ip access denied"}'
            await send({
                "type": "http.response.start",
                "status": 403,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(body)).encode()),
                ],
            })
            await send({"type": "http.response.body", "body": body})
            return
        await self.app(scope, receive, send)


# Cloudflare 官方公布的回源网段（https://www.cloudflare.com/ips-v4 / ips-v6，2026-10 取）
CLOUDFLARE_IP_RANGES_ENV = "CLOUDFLARE_IP_RANGES"
DEFAULT_CLOUDFLARE_RANGES = (
    "173.245.48.0/20", "103.21.244.0/22", "103.22.200.0/22", "103.31.4.0/22",
    "141.101.64.0/18", "108.162.192.0/18", "190.93.240.0/20", "188.114.96.0/20",
    "197.234.240.0/22", "198.41.128.0/17", "162.158.0.0/15", "104.16.0.0/13",
    "104.24.0.0/14", "172.64.0.0/13", "131.0.72.0/22",
    "2400:cb00::/32", "2606:4700::/32", "2803:f800::/32", "2405:b500::/32",
    "2405:8100::/32", "2a06:98c0::/29", "2c0f:f248::/32",
)


def _cloudflare_networks() -> list:
    raw = (os.environ.get(CLOUDFLARE_IP_RANGES_ENV) or "").strip()
    parts = [p.strip() for p in raw.split(",") if p.strip()] if raw else list(DEFAULT_CLOUDFLARE_RANGES)
    nets = []
    for part in parts:
        try:
            nets.append(ipaddress.ip_network(part, strict=False))
        except ValueError:
            continue
    return nets


# ``*`` 只警告一次：热路径每个请求都会读这份名单。
_wildcard_warned = False


def _forwarded_allow_ips() -> list[str]:
    raw = os.getenv("FORWARDED_ALLOW_IPS", "")
    parts = [part.strip() for part in raw.split(",") if part.strip()]
    _note_forwarded_wildcard(parts)
    return parts


def _note_forwarded_wildcard(parts: list[str]) -> None:
    """``*`` 不会让域名守卫信任所有对端；提醒一次，避免运维以为和 uvicorn 完全同一语义。"""
    global _wildcard_warned
    if _wildcard_warned or "*" not in parts:
        return
    _wildcard_warned = True
    logger.warning(
        "FORWARDED_ALLOW_IPS 含 *：域名守卫不会因此采信任意对端的转发头"
        "（客户端 IP 只认显式 IP/CIDR、TRUSTED_PROXIES 与 Cloudflare 网段）。"
        "未设置 EMBY_PUBLIC_URL 时，公开 URL 的 scheme/host 仍把 * 视为全信任。"
    )


def forwarded_allow_wildcard() -> bool:
    """部署方是否写了 ``FORWARDED_ALLOW_IPS=*``。

    只给公开 URL 的 scheme/host 推断用。域名守卫改写 ``scope["client"]`` 时必须忽略它，
    否则任意直连方都能伪造客户端 IP。
    """
    return "*" in _forwarded_allow_ips()


def _ip_in_cidr_list(ip: str, parts: list[str]) -> bool:
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return False
    for part in parts:
        if "/" not in part:
            continue
        try:
            if addr in ipaddress.ip_network(part, strict=False):
                return True
        except (ValueError, TypeError):
            continue
    return False


def peer_honors_forwarded_headers(ip: str) -> bool:
    """直连 IP 是否在点名名单里，因而允许采信转发头。

    域名守卫（改写客户端 IP）与公开 URL（拼 scheme/host）共用这一份点名名单，不含 ``*``：

    - ``TRUSTED_PROXIES``（含默认回环）：与限流同一份名单，支持 CIDR。
    - ``FORWARDED_ALLOW_IPS`` 里的精确 IP 与 CIDR。

    ``*`` **不是**「信任所有对端」。把它算进去的话，任何一个直连方都能用
    ``X-Forwarded-For`` 改写 ``scope["client"]``。公开 URL 若要跟随 uvicorn
    把显式 ``*`` 当成全信任，走 :func:`forwarded_allow_wildcard`，不要改这里。
    """
    if not ip:
        return False
    allows = [part for part in _forwarded_allow_ips() if part != "*"]
    if ip in allows:
        return True
    if _ip_in_cidr_list(ip, allows):
        return True
    from backend.ratelimit import _is_trusted_proxy

    return _is_trusted_proxy(ip)


def _peer_trusted(peer: str) -> bool:
    """TCP 直连对端是否可信：Cloudflare 网段，或 ``peer_honors_forwarded_headers``。"""
    try:
        addr = ipaddress.ip_address(peer)
    except ValueError:
        return False
    for net in _cloudflare_networks():
        if addr.version == net.version and addr in net:
            return True
    return peer_honors_forwarded_headers(peer)


def _peer_is_cloudflare(peer: str) -> bool:
    """对端是否在 Cloudflare 官方网段内。

    P2 修复（审查）：CF-Connecting-IP 只能采信 CF 官方网段发来的。
    此前 _peer_trusted 把回环/自配代理也算可信，攻击者经本机 nginx 伪造
    CF-Connecting-IP 即可任意冒充客户端 IP（限流绕过、审计投毒）。
    """
    try:
        addr = ipaddress.ip_address(peer)
    except ValueError:
        return False
    for net in _cloudflare_networks():
        if addr.version == net.version and addr in net:
            return True
    return False


def _spoofable_target(ip: str) -> bool:
    """还原出来的「真实 IP」是回环 / 未指定地址：不可能是真实公网用户，拒绝"""
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return True
    return addr.is_loopback or addr.is_unspecified


class CloudflareIPMiddleware:
    """CF 真实 IP 还原的纯 ASGI 中间件（见模块文档）。"""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope.get("type") in ("http", "websocket") and _trust_cf_ip():
            client = scope.get("client")
            peer = client[0] if isinstance(client, (list, tuple)) and client else ""
            if not _peer_trusted(str(peer or "")):
                # H4：直连源站（不经 CF / 本机反代）的请求，代理头一律不信
                await self.app(scope, receive, send)
                return
            headers = {k.lower(): v for k, v in (scope.get("headers") or [])}
            real_ip = ""
            # P2 修复：CF-Connecting-IP 仅在对端 ∈ CF 官方网段时采信；
            # 回环/自配代理来的走 X-Forwarded-For 末段（防伪造）。
            if _peer_is_cloudflare(str(peer or "")):
                cf_ip = headers.get(b"cf-connecting-ip")
                if cf_ip:
                    try:
                        real_ip = cf_ip.decode("latin-1").strip()
                    except Exception:
                        real_ip = ""
            if not real_ip:
                xff = headers.get(b"x-forwarded-for")
                if xff:
                    try:
                        hops = [h.strip() for h in xff.decode("latin-1").split(",") if h.strip()]
                        real_ip = hops[-1] if hops else ""
                    except Exception:
                        real_ip = ""
            if real_ip and is_valid_ip(real_ip) and not _spoofable_target(real_ip):
                port = client[1] if isinstance(client, (list, tuple)) and len(client) > 1 else 0
                scope["client"] = (real_ip, port)
                # 给下游留个标记：这个 IP 是代理头还原的
                scope.setdefault("state", {})["real_ip_from_proxy"] = real_ip
            elif real_ip:
                logger.warning("忽略非法 / 可疑代理 IP 头: %r", real_ip[:64])
        await self.app(scope, receive, send)
