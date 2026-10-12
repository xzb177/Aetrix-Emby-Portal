"""媒体与交付域的写操作审计（v2.30.0）

**问题**：后台的「操作日志」只覆盖 `/api/admin/*`（用户、订单、卡码、设置…）。
媒体与交付域的端点挂在 `/api/admin/emby/*` 上，走的是另一套鉴权依赖，**一条审计都不写**：
删掉一个十万条的媒体库、删掉已入库的条目、删掉 115 账号、停掉全站转码，
在「系统与审计 → 操作日志」里都查不到是谁干的，只能去翻服务日志。

**为什么做成中间件，而不是在每个端点里写一行 `_audit(...)`**：

- 端点很多（媒体库 / 条目 / 会话 / 转码 / 115 账号 / 存储来源 / 扫描队列…），
  逐个写一定会漏，漏的那个通常就是最危险的那个；
- 新增端点时要记得补审计，而「记得」不是一种机制；
- 中间件只看**已经发生的成功请求**（2xx/3xx），而端点里写审计是在做之前就记，
  失败/被拒的操作也会留下一行「已经做过」——审计里不该有这种记录。

三条实现口径：

1. **纯 ASGI 中间件**（与 `download_guard.py` 同一写法）：不是 `BaseHTTPMiddleware`，
   不包裹响应体，大文件流式播放不受影响；路径/方法不匹配时零开销透传。
2. **只记成功的写操作**：`POST/PUT/PATCH/DELETE` 且响应 < 400；身份从
   `Authorization: Bearer` 解析（面板 JWT），解析不出管理员就跳过——那种请求本来就会被
   鉴权依赖拒掉，审计里不该出现「匿名管理员」。
3. **记意图而不是记全文**：动作名由「方法 + 路径模板」映射而来（见 `ACTION_TABLE`），
   `details` 里只有方法、路径与路径参数。**绝不记请求体**：115 账号 Cookie、挂载凭据
   都在请求体里，抄进日志等于把密钥又存了一份。

4. **端点可自助富化**：中间件只知道「谁在什么时候动了哪个路径」，不知道业务细节
   （例如重置 scope、影响行数）。端点在成功后可以把细节挂到
   `request.state.audit_extra`（dict，只能由服务端代码设置），中间件会原样并进
   `details`（`method/path/admin_user_id/client_ip` 四个保留键不许覆盖）。
   这是通用机制：任何端点都能用，不用改中间件，也不会产生两条重复日志。
"""

from __future__ import annotations

import logging
import re

from backend import models

logger = logging.getLogger(__name__)

PREFIX = "/api/admin/emby"

# 只有这几种方法算写操作
WRITE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})

# (方法, 路径模板) → (动作名, 目标类型)
# 路径模板里 `{}` 匹配任意一段（id / guid / 会话键）。未列出的写端点回落到 emby_admin_write，
# 仍会留下「谁在什么时候动了哪个路径」，只是动作名不带语义。
ACTION_TABLE: dict[tuple[str, str], tuple[str, str]] = {
    ("POST", "/libraries"): ("emby_create_library", "library"),
    ("PUT", "/libraries/{}"): ("emby_update_library", "library"),
    ("DELETE", "/libraries/{}"): ("emby_delete_library", "library"),
    ("POST", "/libraries/{}/scan"): ("emby_scan_library", "library"),
    ("POST", "/libraries/{}/cover"): ("emby_upload_library_cover", "library"),
    ("DELETE", "/libraries/{}/cover"): ("emby_remove_library_cover", "library"),
    ("POST", "/libraries/virtual"): ("emby_generate_virtual_libraries", "library"),
    ("POST", "/libraries/repair/run"): ("emby_run_repair", "library"),
    ("DELETE", "/scan-queue/{}"): ("emby_scan_cancel", "library"),
    ("DELETE", "/items/{}"): ("emby_delete_item", "media_item"),
    ("DELETE", "/sessions/{}"): ("emby_stop_session", "playback_session"),
    ("POST", "/transcodes/stop-all"): ("emby_stop_all_transcodes", "transcode"),
    ("POST", "/scrape/probe/reset"): ("emby_probe_queue_reset", "probe"),
    ("POST", "/115/accounts"): ("emby_create_pan115", "pan115_account"),
    ("PUT", "/115/accounts/{}"): ("emby_update_pan115", "pan115_account"),
    ("DELETE", "/115/accounts/{}"): ("emby_delete_pan115", "pan115_account"),
    ("POST", "/115/accounts/{}/verify"): ("emby_verify_pan115", "pan115_account"),
    ("POST", "/mounts"): ("emby_create_mount", "storage_mount"),
    ("PUT", "/mounts/{}"): ("emby_update_mount", "storage_mount"),
    ("DELETE", "/mounts/{}"): ("emby_delete_mount", "storage_mount"),
}

# 已知端点里没写进上表的（或将来新增的）统一回落动作名
FALLBACK_ACTION = "emby_admin_write"

# 端点自助富化审计时不许覆盖的保留键（中间件的权威字段）
_RESERVED_DETAIL_KEYS = frozenset({"method", "path", "admin_user_id", "client_ip"})

_ITEM_ID_RE = re.compile(r"^(?:[0-9a-f]{16,}|[0-9a-f-]{20,})$", re.IGNORECASE)


def _literal_segments() -> frozenset[str]:
    """ACTION_TABLE 里出现过的**字面量**路径段（`{}` 不算）

    **为什么需要它**：全数字的段不一定是 id。`115` 就是一个字面量命名空间
    （115 网盘），按「是数字就当 id」的口径，`/115/accounts` 会被收敛成
    `/{}/accounts` —— 于是 ACTION_TABLE 里那 4 条 `/115/*` 全部变成永远匹配不上的
    **死条目**，删 / 改 115 账号在操作日志里只会看到一句没有语义的 `emby_admin_write`。

    同一个坑里还有 `/libraries/virtual` 里的 `virtual`，所以口径统一成：
    表里写过的字面量段，永远不当作 id。
    """
    out: set[str] = set()
    for _, template in ACTION_TABLE:
        for segment in template.strip("/").split("/"):
            if segment and segment != "{}":
                out.add(segment)
    return frozenset(out)


LITERAL_SEGMENTS = _literal_segments()


def normalize_path(path: str) -> str:
    """把请求路径收敛成「路径模板」：数字/guid/会话键都换成 `{}`

    只做形态判断（全数字 / 长十六进制 / 明显的随机键），不猜语义：
    猜错的后果是把 `/libraries/virtual` 里的 `virtual` 也换成 `{}`，
    那样「生成虚拟库」就会被记成「改了某个库」。所以这里只替换**看起来像 id**、
    且**没有在 ACTION_TABLE 里作为字面量出现过**的段（见 ``LITERAL_SEGMENTS``）。
    """
    tail = (path or "").split("?", 1)[0]
    if tail.startswith(PREFIX):
        tail = tail[len(PREFIX):]
    segments = [s for s in tail.split("/") if s]
    out: list[str] = []
    for index, segment in enumerate(segments):
        if segment in LITERAL_SEGMENTS:
            out.append(segment)
            continue
        if segment.isdigit() or _ITEM_ID_RE.match(segment):
            out.append("{}")
            continue
        # 会话键（``s`` + ``secrets.token_urlsafe(16)``，见 session_routes）：最后一段且足够长。
        # 字符集必须包含 ``-``、``_`` 与大写字母——token_urlsafe 用的是 base64url，
        # 只认小写字母与数字会让绝大多数真实会话键落进回落动作（「结束会话」就没有名字了）。
        if index == len(segments) - 1 and re.fullmatch(r"s[0-9A-Za-z_-]{8,}", segment):
            out.append("{}")
            continue
        out.append(segment)
    return "/" + "/".join(out)


def lookup(method: str, path: str) -> tuple[str, str]:
    """(方法, 路径) → (动作名, 目标类型)"""
    method = (method or "").upper()
    template = normalize_path(path)
    hit = ACTION_TABLE.get((method, template))
    if hit:
        return hit
    # 只差一个 **id 段**（例如 /libraries/{}/items）：退回上一级模板再试一次。
    # 只对 `{}` 放宽：把字面量段去掉会把子动作记成父动作的名字——
    # 例如 `/mounts/test`（测试一个还没保存的挂载）会变成「创建存储挂载」，
    # 运维在操作日志里会看到一条并不存在的「谁建了个挂载」。
    if template.endswith("/{}"):
        hit = ACTION_TABLE.get((method, template[: -len("/{}")]))
        if hit:
            return hit
    target_type = template.strip("/").split("/")[0] if template.strip("/") else ""
    return FALLBACK_ACTION, target_type or "emby"


def _admin_from_token(db, headers) -> models.WebUser | None:
    """解析 Bearer token 并确认是启用的管理员（解析不出返回 None）"""
    from backend.security import resolve_jwt_user_id

    raw = ""
    for key, value in headers or ():
        if key == b"authorization":
            raw = value.decode("latin-1", "ignore").strip()
            break
    if not raw.lower().startswith("bearer "):
        return None
    user_id = resolve_jwt_user_id(raw[7:].strip())
    if user_id is None:
        return None
    user = db.query(models.WebUser).filter(models.WebUser.id == user_id).first()
    if user is None or not user.is_staff or not user.is_active:
        return None
    return user


def record_write(action: str, target_type: str, path: str, details: dict) -> None:
    """在独立会话里落库（中间件不是请求依赖，不共用请求会话）"""
    from backend.database import SessionLocal

    with SessionLocal() as db:
        try:
            user_id = details.get("admin_user_id")
            db.add(models.AdminLog(
                admin_user_id=int(user_id) if user_id else None,
                action=action,
                target_type=target_type or None,
                target_id=None,
                details=details,
                ip_address=str(details.get("client_ip") or "") or None,
            ))
            db.commit()
        except Exception as exc:  # noqa: BLE001 — 审计失败不影响这次操作
            db.rollback()
            logger.warning("写操作审计落库失败（操作本身已成功）: %s", exc)


class AdminWriteAuditMiddleware:
    """把 `/api/admin/emby/*` 的成功写操作记进操作日志（admin_logs）"""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope.get("type") != "http":
            return await self.app(scope, receive, send)
        path = scope.get("path", "")
        method = (scope.get("method") or "").upper()
        if method not in WRITE_METHODS or not path.startswith(PREFIX):
            return await self.app(scope, receive, send)

        status = 500

        async def send_wrapper(message):
            nonlocal status
            if message["type"] == "http.response.start":
                status = int(message.get("status", 500))
            await send(message)

        await self.app(scope, receive, send_wrapper)

        # 被拒 / 失败的操作不记：审计里只该有「真的做了的事」
        if not (200 <= status < 400):
            return
        action, target_type = lookup(method, path)
        details = {
            "method": method,
            "path": normalize_path(path),
        }
        try:
            from backend.database import SessionLocal
            from backend.ratelimit import client_ip
            from starlette.requests import Request

            request = Request(scope, receive=None)
            details["client_ip"] = client_ip(request)
            with SessionLocal() as db:
                user = _admin_from_token(db, scope.get("headers"))
                if user is None:
                    # 解析不出管理员：这条请求本来就会被鉴权依赖拒掉（这里是 401/403），不记
                    return
                details["admin_user_id"] = user.id
        except Exception as exc:  # noqa: BLE001 — 解析失败不影响响应
            logger.debug("写操作审计身份解析失败: %s", exc)
            return

        # 端点自助富化：request.state.audit_extra（服务端代码设置，非客户端输入）
        # 里的业务细节（例如 scope、影响行数）并进 details，保留键不许覆盖。
        # 通用机制：任何端点都可以在成功后挂自己的细节，不用改中间件。
        extra = (scope.get("state") or {}).get("audit_extra")
        if isinstance(extra, dict):
            for k, v in extra.items():
                if k not in _RESERVED_DETAIL_KEYS:
                    details[k] = v

        record_write(action, target_type, path, details)
