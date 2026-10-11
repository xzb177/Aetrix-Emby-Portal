"""网关级下载策略兜底

站点关闭下载（`allow_download=false`）时，仅在 `/Download` 单条路由上判断是不够的：
Emby 客户端还会走 `/Items/{id}/File` 这类等价路径。这里在 ASGI 层做统一兜底，
避免将来新增下载类路由时漏判造成绕过。

实现要点：
- 纯 ASGI 中间件，**不是** BaseHTTPMiddleware —— 后者会包裹响应体，
  可能影响本站的大文件流式播放（StreamingResponse / FileResponse）。
- 路径不匹配时原样透传（零开销），匹配时才查库判策略。
- 未认证 / 解析不到用户时放行，交回原路由返回 401，不改变原有错误语义。
- 管理员始终放行，与路由内 `ensure_download_allowed` 口径一致。
- `_verdict` 的同步 SQLAlchemy 判定经 `run_in_threadpool` 下放到线程池，绝不占用事件循环（口径同 access_guard）
"""
from __future__ import annotations

import logging

from fastapi import Request
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

logger = logging.getLogger(__name__)


def is_download_path(path: str) -> bool:
    """下载类路径判定（大小写不敏感，兼容尾斜杠）"""
    p = (path or "").rstrip("/").lower()
    return p.endswith("/download") or p.endswith("/file")


def _is_privileged(user) -> bool:
    for attr in ("is_staff", "is_admin", "is_superuser"):
        if getattr(user, attr, False):
            return True
    return False


class DownloadGuardMiddleware:
    """下载策略网关级兜底中间件"""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope.get("type") != "http" or not is_download_path(scope.get("path", "")):
            return await self.app(scope, receive, send)

        allowed_by_policy, blocked = await run_in_threadpool(self._verdict, scope)
        if allowed_by_policy:
            # 把「本站点允许下载」这个结论留给下游路由：路由里的 ensure_download_allowed
            # 直接复用，同一请求不再把「全局开关 + 该服策略」重查一遍。
            scope.setdefault("state", {})["download_allowed_by_guard"] = True
        if blocked is None:
            return await self.app(scope, receive, send)

        response = JSONResponse({"detail": blocked}, status_code=403)
        return await response(scope, receive, send)

    # ---- 内部 ----

    @staticmethod
    def _verdict(scope) -> tuple[bool, str | None]:
        """返回 (站点是否允许下载, 403 文案或 None)

        只有「真的判过策略且结论是允许」才回 True——导入失败、拿不到身份、判定异常
        这些「没结论」的情况一律回 False，交给下游路由自己再判（宁可多查一次，不能漏拦）。
        """
        try:
            from backend.database import SessionLocal
            from backend.emby_server.auth import resolve_request_user
            from backend.subscriptions import download_allowed, download_gate_message
        except Exception:  # pragma: no cover - 导入失败时不影响主流程
            logger.exception("下载策略兜底初始化失败，已放行")
            return False, None

        db = SessionLocal()
        try:
            if download_allowed(db):
                return True, None

            # 只读 header / query，不消费 body，原 receive 仍交给下游路由
            request = Request(scope, receive=None)
            user = resolve_request_user(db, request)
            if user is None:
                # 拿不到身份就交回原路由处理（通常是 401），不改错误语义
                return False, None

            if _is_privileged(user):
                return True, None
            # 文案按「本机这个服」取：公益服要说清只提供在线观看（见 subscriptions）
            return False, download_gate_message(db)
        except Exception:  # pragma: no cover
            logger.exception("下载策略兜底判定失败，已放行")
            return False, None
        finally:
            db.close()
