"""客户端可见的绝对 URL 基址（播放 / 字幕 / 图片 / 导入 scheme / System/Info）。

反代在本进程前面终结 TLS 时，uvicorn 默认不采信非回环对端的 ``X-Forwarded-Proto``
（``FORWARDED_ALLOW_IPS`` 未设置时只信 ``127.0.0.1``）。Docker 网关（常见
``172.18.0.1``）发来的请求因此仍是 ``http``，``request.base_url`` 会把
``http://源站域名或源站 IP`` 写进 PlaybackInfo。SenPlayer 不跟随 80 端口的 301，
``/stream`` 根本到不了本服务；Host 里的源站地址也会泄漏给客户端。

所有交给客户端的绝对地址都走 ``client_base_url``，只此一套：

1. ``EMBY_PUBLIC_URL`` 已设且是 http(s) → 用它，不看 Host、不看转发头。
2. 否则仅当 TCP 直连方在可信范围时，才采信 ``X-Forwarded-Proto`` /
   ``X-Forwarded-Host``。可信范围是 ``TRUSTED_PROXIES``、
   ``FORWARDED_ALLOW_IPS`` 里的精确 IP / CIDR，以及部署方显式写的 ``*``。
   ``*`` 只在这一步生效（与 uvicorn 对齐，只影响下发给客户端的 scheme/host）；
   域名守卫改写客户端 IP 时忽略它，两边共用 ``peer_honors_forwarded_headers``
   做点名判断，不把通配塞进那份名单。
3. 否则 ``request.base_url``。
"""
from __future__ import annotations

import logging
import os
from urllib.parse import urlsplit, urlunsplit

from starlette.requests import Request

logger = logging.getLogger(__name__)

_HTTP = ("http://", "https://")


def configured_public_url() -> str:
    """部署方写明的对外地址。不是 http(s) 就当没配（避免把残缺字符串下发给播放器）。"""
    raw = os.getenv("EMBY_PUBLIC_URL", "").strip().rstrip("/")
    if not raw.startswith(_HTTP):
        return ""
    return _without_default_port(raw)


def client_base_url(request: Request | None) -> str:
    """拼给客户端的基址（无尾斜杠）。没有请求、也没有公开地址时返回空串。"""
    public = configured_public_url()
    if public:
        return public
    if request is None:
        return ""
    if _forwarded_headers_trusted(request):
        rebuilt = _from_forwarded(request)
        if rebuilt:
            return rebuilt
    return _raw_request_base(request)


def _raw_request_base(request: Request) -> str:
    try:
        base = str(request.base_url).rstrip("/")
    except Exception:  # noqa: BLE001 — 拿不到地址就当没推断出来
        return ""
    if not base.startswith(_HTTP):
        return ""
    return _without_default_port(base)


def _from_forwarded(request: Request) -> str:
    """可信反代上的对外 scheme + host。没有转发头时返回空串，交给 request.base_url。"""
    try:
        headers = request.headers
    except Exception:  # noqa: BLE001
        return ""
    proto_raw = (headers.get("x-forwarded-proto") or "").split(",")[0].strip().lower()
    host_raw = (headers.get("x-forwarded-host") or "").split(",")[0].strip()
    if not proto_raw and not host_raw:
        return ""
    proto = proto_raw if proto_raw in ("http", "https") else ""
    host = host_raw if _host_ok(host_raw) else ""
    if not proto:
        try:
            proto = (request.url.scheme or "").lower()
        except Exception:  # noqa: BLE001
            proto = ""
        if proto not in ("http", "https"):
            proto = "http"
    if not host:
        host = (headers.get("host") or "").split(",")[0].strip()
        if not _host_ok(host):
            return ""
    host = _strip_default_port_host(proto, host)
    return f"{proto}://{host}"


def _forwarded_headers_trusted(request: Request) -> bool:
    """转发头只在「直连方是我们自己的反代」时可信。

    ``CloudflareIPMiddleware`` 会在确认对端可信之后把 ``scope["client"]`` 改写成
    真实用户 IP。改写后的 IP 不能再拿来判断这次转发头可不可信，所以认它留下的标记。
    """
    if _proxy_restored_real_ip(request):
        return True
    return _peer_in_forwarded_allow_range(_peer_host(request))


def _proxy_restored_real_ip(request: Request) -> bool:
    try:
        state = request.scope.get("state") or {}
    except Exception:  # noqa: BLE001
        return False
    return bool(state.get("real_ip_from_proxy"))


def _peer_host(request: Request) -> str:
    try:
        client = request.client
    except Exception:  # noqa: BLE001
        return ""
    if client is None:
        return ""
    host = getattr(client, "host", None)
    if host:
        return str(host)
    if isinstance(client, (tuple, list)) and client:
        return str(client[0])
    return ""


def _peer_in_forwarded_allow_range(ip: str) -> bool:
    """直连 IP 是否允许采信转发头来拼公开 URL。

    点名名单与域名守卫相同（``peer_honors_forwarded_headers``，不含 ``*``）。
    ``FORWARDED_ALLOW_IPS=*`` 只在这里额外放行：部署方与 uvicorn 对齐的显式开关，
    只改下发给客户端的 scheme/host，不改写 ``scope["client"]``。
    没有对端地址时，即使写了 ``*`` 也不采信。
    """
    if not ip:
        return False
    from backend.domain_guard import forwarded_allow_wildcard, peer_honors_forwarded_headers

    if forwarded_allow_wildcard():
        return True
    return peer_honors_forwarded_headers(ip)


def warn_public_url_on_startup() -> None:
    """校验 ``EMBY_PUBLIC_URL``。不合规只记 WARNING，绝不因此拒绝启动。

    未设置不告警。已设置时必须以 ``https://`` 开头，并且在域名守卫开着
    （``_get_allowed_hosts()`` 非空）时，主机落在那份允许名单里。
    """
    try:
        raw = os.getenv("EMBY_PUBLIC_URL", "").strip()
        if not raw:
            return
        reasons: list[str] = []
        if not raw.lower().startswith("https://"):
            reasons.append("必须以 https:// 开头")
        host = _public_url_hostname(raw)
        try:
            from backend import domain_guard

            allowed = domain_guard._get_allowed_hosts()
        except Exception:
            logger.warning("EMBY_PUBLIC_URL 域名守卫校验失败，服务继续启动", exc_info=True)
            allowed = None
        if allowed is not None and host not in allowed:
            reasons.append("主机不在域名守卫白名单内")
        if reasons:
            shown = raw if len(raw) <= 200 else raw[:200] + "…"
            logger.warning(
                "EMBY_PUBLIC_URL 不合规（%s），继续启动: %s",
                "；".join(reasons),
                shown,
            )
    except Exception:
        logger.warning("EMBY_PUBLIC_URL 启动校验失败，服务继续启动", exc_info=True)


def _public_url_hostname(raw: str) -> str:
    try:
        host = urlsplit(raw).hostname or ""
    except ValueError:
        return ""
    return host.strip().lower()


def _host_ok(host: str) -> bool:
    if not host or len(host) > 255:
        return False
    # 拒绝空格、路径、userinfo：转发头是客户端可伪造的，可信对端也不该带出坏 URL
    return not any(ch in host for ch in " \t\r\n/@\\?#")


def _strip_default_port_host(proto: str, host: str) -> str:
    if proto == "https" and host.endswith(":443"):
        return host[:-4]
    if proto == "http" and host.endswith(":80"):
        return host[:-3]
    return host


def _without_default_port(url: str) -> str:
    raw = (url or "").strip().rstrip("/")
    if not raw:
        return ""
    try:
        parts = urlsplit(raw)
        port = parts.port
    except ValueError:
        return raw
    if port is None:
        return raw
    if not (
        (parts.scheme == "https" and port == 443)
        or (parts.scheme == "http" and port == 80)
    ):
        return raw
    host = parts.hostname or ""
    if ":" in host:
        host = f"[{host}]"
    return urlunsplit((parts.scheme, host, parts.path or "", parts.query, parts.fragment)).rstrip("/")
