"""EA 纯 ASGI 中间件（中转零拷贝优化）

为什么不用 ``@app.middleware("http")``（BaseHTTPMiddleware）：
- BaseHTTPMiddleware 每个请求起一个 task group + 用内存对象流中转响应，
  响应体的**每一个分块**都要经过它的中继。播放时每秒上百个分块 × 若干个
  中间件 = 可观的上下文切换与拷贝。
- 纯 ASGI 中间件只在 ``http.response.start`` 上改头（或提前短路返回），
  body 分块原样透传给下游，**零额外拷贝、零 task group**。

三个中间件与原来的 ``@app.middleware("http")`` 版本行为逐项对齐，
注册顺序也与原来一致（见 emby_api/main.py 的 add_middleware 位置）。
"""
from __future__ import annotations

import logging
import os

from fastapi.responses import JSONResponse
from starlette.requests import Request

logger = logging.getLogger(__name__)


def _ensure_header(raw_headers: list, name: bytes, value: bytes) -> None:
    """raw header 列表上实现 setdefault 语义：已存在则不动。"""
    lname = name.lower()
    for k, _v in raw_headers:
        if k.lower() == lname:
            return
    raw_headers.append((name, value))


class SecurityHeadersMiddleware:
    """安全响应头：只改 http.response.start 的头，body 透传。"""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return

        # 原实现按 request.url.path 判断；ASGI 层直接用 scope["path"]，语义一致。
        path = scope.get("path", "")
        is_emby = path.startswith("/emby/")
        skip_no_store = False
        if is_emby:
            # video segments cacheable, skip forced no-store（与原实现一致，
            # 保持函数内懒导入，避免模块加载期循环导入）
            from backend.emby_server.cdn import is_segment_path

            skip_no_store = is_segment_path(path)

        async def send_with_headers(message):
            if message["type"] == "http.response.start":
                headers = message.setdefault("headers", [])
                _ensure_header(headers, b"x-content-type-options", b"nosniff")
                _ensure_header(headers, b"x-frame-options", b"DENY")
                _ensure_header(
                    headers,
                    b"referrer-policy",
                    b"strict-origin-when-cross-origin",
                )
                if is_emby and not skip_no_store:
                    _ensure_header(headers, b"cache-control", b"no-store")
            await send(message)

        await self.app(scope, receive, send_with_headers)


class DoubleEmbyPrefixMiddleware:
    """双重 /emby 前缀归一化（纯 ASGI，只改 scope 的 path，原样透传）。

    部分客户端自己会拼 /emby 前缀，用户在地址栏又填了带 /emby 的服务地址，
    请求就变成 /emby/emby/... 全部 404（2026-10-22 晚实测 10 次）。这不是
    客户端一家的错，服务端做一层容错最划算：在进任何业务中间件 / 路由前
    把重复的 /emby/ 前缀折叠成一层。只折叠紧邻的重复段，正常路径零影响。
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope.get("type") == "http":
            path = scope.get("path", "")
            collapsed = path
            while collapsed.startswith("/emby/emby/"):
                collapsed = "/emby/" + collapsed[len("/emby/emby/"):]
            if collapsed == "/emby/emby":
                collapsed = "/emby"
            if collapsed != path:
                scope["path"] = collapsed
                raw = scope.get("raw_path")
                if raw:
                    while raw.startswith(b"/emby/emby/"):
                        raw = b"/emby/" + raw[len(b"/emby/emby/"):]
                    if raw == b"/emby/emby":
                        raw = b"/emby"
                    scope["raw_path"] = raw
        await self.app(scope, receive, send)


class ForegroundActivityMiddleware:
    """前台活跃打点中间件——/emby/ 用户路径经过即记录，供 maintenance 重活让路；纯 ASGI，只读 scope[path]，不包 send，零拷贝零开销；任何异常永不影响主链路。"""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return
        path = scope.get("path", "")
        if path.startswith("/emby/"):
            try:
                from backend.emby_server import foreground_activity as _fa

                _fa.note()
            except Exception:
                pass
        await self.app(scope, receive, send)


class EaBodyLimitMiddleware:
    """请求体大小上限：只看请求头做廉价拒绝，放行时零开销（不包 send）。"""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return

        try:
            max_mb = float(os.getenv("MAX_REQUEST_BODY_MB", "10"))
        except ValueError:
            max_mb = 10
        max_bytes = int(max_mb * 1024 * 1024)

        clen = None
        for k, v in scope.get("headers", []):
            if k.lower() == b"content-length":
                clen = v
                break
        if clen:
            try:
                if int(clen) > max_bytes:
                    response = JSONResponse(
                        status_code=413,
                        content={"error": f"请求体过大，上限 {max_mb:g}MB"},
                    )
                    await response(scope, receive, send)
                    return
            except ValueError:
                pass
        await self.app(scope, receive, send)


# 限流规则表：收敛到 backend.ratelimit.API_RATE_LIMITS（横切能力只许一套），
# EM 与 EA 共用，改限额只改一处。保留本名做兼容。
from backend.ratelimit import API_RATE_LIMITS as _EA_RATE_LIMITS  # noqa: E402


def _ea_get_ip(request: Request) -> str:
    """真实客户端 IP（与原来同一口径：只有可信代理写进来的头才算数）。"""
    from backend.ratelimit import get_client_ip

    return get_client_ip(request)


def _ea_is_auth(request: Request) -> bool:
    """EA 限流档判定：是否认证用户。

    判定逻辑收敛到 backend.ratelimit.is_authenticated_request（横切能力只许一套）。
    旧实现只看 Authorization 头 / Emby token 在不在（``bool(auth or token)``），
    攻击者发任意伪造头就能拿到 600/分钟认证档；现纯验签，失败一律 False。
    保留本函数名做兼容。
    """
    from backend.ratelimit import is_authenticated_request

    return is_authenticated_request(request)


def _ea_check_limit(ip: str, path: str, authenticated: bool) -> tuple:
    for prefix, limit_anon, limit_auth in _EA_RATE_LIMITS:
        if path.startswith(prefix):
            limit = limit_auth if authenticated else limit_anon
            if limit == 0:
                return True, ""
            try:
                from backend import database as db
                import time

                window = int(time.time() // 60)
                # 区分认证/未认证的 key，避免互相影响
                auth_tag = "auth" if authenticated else "anon"
                key = f"ratelimit:ea:{ip}:{prefix}:{auth_tag}:{window}"
                # S4：Redis 正常走 Redis；运行期故障 / 熔断中改用进程内计数（不再每请求
                # 卡满 socket 超时）；未启用 Redis 时返回 None = 不限流（与升级前一致）
                count = db.rate_limit_incr(key, 70)  # 窗口 60 秒 + 10 秒缓冲
                if count is not None and count > limit:
                    return False, f"每分钟最多 {limit} 次"
            except Exception:
                return True, ""  # 异常时不限流（降级）
            return True, ""
    return True, ""


class EaRateLimitMiddleware:
    """EA 限流：命中规则才进 Redis 检查；放行时零开销（不包 send）。"""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return

        path = scope.get("path", "")
        # /api/ 与 /emby/ 都限流：后者覆盖 Emby 客户端登录（其它 /emby/ 路径无匹配规则则放行）
        if path.startswith(("/api/", "/emby/")):
            request = Request(scope)
            ip = _ea_get_ip(request)
            authenticated = _ea_is_auth(request)
            # redis-py 是同步客户端：下放线程池，避免在事件循环里等 Redis 往返
            # （与原实现一致，见原 ea_rate_limit_middleware 注释）。
            from starlette.concurrency import run_in_threadpool

            allowed, reason = await run_in_threadpool(
                _ea_check_limit, ip, path, authenticated
            )
            if not allowed:
                response = JSONResponse(
                    status_code=429,
                    content={"error": "请求太频繁，请稍后再试", "reason": reason},
                    headers={"Retry-After": "60"},
                )
                await response(scope, receive, send)
                return
        await self.app(scope, receive, send)
