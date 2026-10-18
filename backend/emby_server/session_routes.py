"""播放会话端点：补鉴权 + 会话键归一化（在协议路由注册前替换旧实现）

`backend/emby_server/api.py` 里的历史实现在会话端点上有三处问题：

1. ``GET /Sessions``（含 ``/emby/Sessions``）**没有任何鉴权**，返回全站所有未结束会话：
   用户名 / 设备 / 客户端 / IP / 正在播放的条目；
2. ``DELETE /Sessions/{key}``（含 ``/emby/Sessions/{key}``）**同样没有鉴权**；
3. 客户端没带 ``PlaySessionId`` 时，会话键回退成 ``{user.id}-{item.guid[:16]}``，而
   ``item.guid`` 就是客户端可见的条目 id：会话键可以被公开算出来。

转码已删除（2026-10-17）：结束会话只标记结束时间，不再有转码进程可停。

本模块沿用 ``mount_routes.py`` 的做法——在 ``include_router`` **之前**把这几条路由换成
带鉴权的实现（``install_session_routes``），并把上报入口的会话键归一化：

- **列表**：普通用户只能看到自己的会话，``is_staff`` 可看全站；
- **结束**：只能结束自己的会话（``session.user_id == user.id``），``is_staff`` 可停任意；
- **会话键**：客户端自带的 ``PlaySessionId`` 只有满足 ``^[A-Za-z0-9_.:-]{1,64}$`` 且没有被
  别的用户占用时才复用；其余按「用户 | 设备 | 条目」做 HMAC-SHA256（key=SECRET_KEY）
  派生稳定键（同一设备同一条目的多次上报复用同一行，不再每报新建一行；不同设备键不同；
  无服务端密钥不可预测，也不会改写别人的会话）。

上报路由（``Sessions/Playing`` / ``Progress`` / ``Stopped``）保留 ``api.py`` 里的原处理函数：
本模块只把 body 里的 ``PlaySessionId`` 换成归一化后的可信会话键，播放进度、已看状态等
业务逻辑不重复实现（``Request.json()`` 的结果被 starlette 缓存，就地改写即可生效）。
"""
from __future__ import annotations

import hashlib
import hmac
import inspect
import logging
import re
from datetime import datetime

from fastapi import Depends, HTTPException, Request
from sqlalchemy.orm import Session

from backend import admin_roles, models
from backend.database import get_db
from backend.emby_server import api as emby_api
from backend.emby_server import compat_routes
from backend.emby_server import models as em
from backend.emby_server.auth import get_emby_user, parse_emby_authorization
from backend.security import SECRET_KEY

logger = logging.getLogger(__name__)

# 会话键允许的字符（会进 URL，必须排除 / 空格等会破坏路由的字符）
SESSION_KEY_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,64}$")

# 被替换的旧路由（api.py 里不带鉴权的实现）
LIST_PATHS = ("/emby/Sessions", "/Sessions")
STOP_PATHS = ("/emby/Sessions/{session_key}", "/Sessions/{session_key}")
# 上报路由：路径 → compat_routes.py 里的处理函数名（只改 body，不改逻辑）
REPORT_PATHS = {
    "/emby/Sessions/Playing": "session_playing",
    "/Sessions/Playing": "session_playing",
    "/emby/Sessions/Playing/Progress": "session_progress",
    "/Sessions/Playing/Progress": "session_progress",
    "/emby/Sessions/Playing/Stopped": "session_stopped",
    "/Sessions/Playing/Stopped": "session_stopped",
}
# 需要从路由表里摘掉的旧端点（端点名用于精确识别，避免误伤同名路径的新实现）
SUPERSEDED_ENDPOINTS = ("get_sessions", "stop_session") + tuple(set(REPORT_PATHS.values()))


def _is_staff(user: models.WebUser) -> bool:
    return bool(getattr(user, "is_staff", False))


def _device_id_of(request: Request) -> str:
    """从请求头取设备 id（口径与 ``auth.issue_token`` 一致）"""
    device_id = ""
    try:
        parsed = parse_emby_authorization(request.headers.get("X-Emby-Authorization") or "")
        if parsed:
            device_id = str(parsed.get("DeviceId") or "")
    except Exception:  # noqa: BLE001 — 取不到设备 id 就回退，不影响会话键派生
        device_id = ""
    if not device_id:
        try:
            device_id = str(request.headers.get("X-Device-Id") or "")
        except Exception:  # noqa: BLE001
            device_id = ""
    device_id = device_id.strip()[:100]
    return device_id or "unknown-device"


def resolve_session_key(db: Session, user: models.WebUser, body: dict,
                        device_id: str = "") -> str:
    """
    解析并返回本次播放上报应当使用的会话键（PlaybackSession.session_key）。

    策略：
    1. 客户端自带合法且未被占用的 PlaySessionId 时直接复用（原样返回），保持与客户端/上游
       播放会话的对应关系；若该键已被其它用户占用，则忽略并降级到第 2 步。
    2. 否则按 “用户 | 设备 | 条目” 做 HMAC-SHA256（key = SECRET_KEY）派生稳定键：
       - 同一设备、同一条目的多次上报（Playing / Progress / Stopped）会落到同一行，
         不再每报新建一行，避免播放会话表被同一段播放刷爆；
       - 不同设备派生出的键不同，因此同播检测（多设备并发播放）不受影响；
       - 没有服务端密钥 SECRET_KEY 的一方无法预测该键，避免客户端伪造/碰撞。
    """
    raw = str(body.get("PlaySessionId") or "").strip()
    if raw and SESSION_KEY_RE.match(raw):
        owner = (
            db.query(em.PlaybackSession.user_id)
            .filter(em.PlaybackSession.session_key == raw)
            .first()
        )
        if owner is None or owner[0] == user.id:
            return raw
        logger.warning("忽略被其它用户占用的 PlaySessionId: %s", raw)

    item_ref = str(body.get("ItemId") or "").strip()
    dev = (device_id or "").strip() or "unknown-device"
    msg = f"playback-session-v1|{user.id}|{dev}|{item_ref}"
    digest = hmac.new(
        SECRET_KEY.encode("utf-8"), msg.encode("utf-8"), hashlib.sha256
    ).hexdigest()[:32]
    return f"s{digest}"


# ==================== 新实现 ====================


def get_sessions_scoped(
    request: Request,
    user: models.WebUser = Depends(get_emby_user),
    db: Session = Depends(get_db),
):
    """在线会话列表：普通用户只看自己的，管理员（is_staff）看全站

    旧实现没有任何鉴权，任何人拿到的是全站会话（用户名 / 设备 / 客户端 / IP / 在看的条目）。
    """
    query = db.query(em.PlaybackSession).filter(em.PlaybackSession.ended_at.is_(None))
    if not _is_staff(user):
        query = query.filter(em.PlaybackSession.user_id == user.id)
    sessions = query.order_by(em.PlaybackSession.last_update_at.desc()).all()

    if not sessions:
        return []
    # 会话列表会被客户端周期性轮询：一次性取回关联条目与用户，
    # 避免「每条会话各查两次」的 N+1（N 台设备同时播放就是 2N 次查询）
    item_ids = {session.item_id for session in sessions}
    user_ids = {session.user_id for session in sessions}
    items = {
        item.id: item
        for item in db.query(em.MediaItem).filter(em.MediaItem.id.in_(item_ids)).all()
    }
    owners = {
        user.id: user
        for user in db.query(models.WebUser).filter(models.WebUser.id.in_(user_ids)).all()
    }
    return [
        emby_api._now_playing_dto(session, items[session.item_id], owners[session.user_id])
        for session in sessions
        if session.item_id in items and session.user_id in owners
    ]


def stop_session_checked(
    session_key: str,
    request: Request,
    user: models.WebUser = Depends(get_emby_user),
    db: Session = Depends(get_db),
):
    """结束播放会话：只能结束自己的，管理员（is_staff）可结束任意会话

    旧实现既没有鉴权，会话键又能从「用户 id + 条目 id」直接算出来。

    角色（v2.26.0）：管理员身份之上的只读角色**踢不掉别人**——否则「只读审计」能
    通过 Emby 接口绕道写（踢人下线、顺带停转码），后台那层角色就等于没做。
    停自己的会话不受影响（客户端自己的行为）。
    """
    session = (
        db.query(em.PlaybackSession)
        .filter(em.PlaybackSession.session_key == session_key)
        .first()
    )
    if session is None:
        raise HTTPException(status_code=404, detail="播放会话不存在")
    if session.user_id != user.id:
        if not _is_staff(user):
            raise HTTPException(status_code=403, detail="只能结束自己的播放会话")
        admin_roles.ensure_admin_allowed(request, user)

    # 转码已删除（2026-10-17）：结束会话只标记，会话上报的 PlayMethod 恒为直连。
    session.ended_at = datetime.now()
    db.commit()
    return {"success": True}


def _install_report_routes(router, originals: dict) -> int:
    """上报路由：改 body 里的会话键后交给 api.py 的原处理函数

    不改业务逻辑，只保证「会话键不可预测（无 PlaySessionId 时按
    用户|设备|条目 HMAC 派生稳定键）、且不会指向别人的会话」。
    """
    installed = 0
    for path, endpoint_name in REPORT_PATHS.items():
        original = originals[endpoint_name]

        async def report(
            request: Request,
            user: models.WebUser = Depends(get_emby_user),
            db: Session = Depends(get_db),
            _original=original,
        ):
            from backend.emby_server.async_db import run_db
            body = await request.json()
            if isinstance(body, dict):
                # 就地改写：starlette 会缓存 Request.json() 的结果，
                # 原处理函数随后读到的就是归一化后的 body。
                # P0（2026-09-29）：sync DB helper 经 run_db 扔线程池，不卡事件循环。
                # 设备 id 是纯同步的头解析，在 run_db 之前取好传进去。
                body["PlaySessionId"] = await run_db(
                    resolve_session_key, db, user, body, _device_id_of(request))
            # 原处理函数是同步 def（拆分后跑在线程池）时不 await，直接取返回值
            result = _original(request, user, db)
            if inspect.isawaitable(result):
                result = await result
            return result

        router.post(path)(report)
        installed += 1
    return installed


def install_session_routes(router) -> dict:
    """替换会话端点实现（必须在 ``app.include_router(emby_router)`` **之前**调用）

    返回替换统计，供启动日志与冒烟测试断言。
    """
    kept = []
    superseded = 0
    for route in router.routes:
        path = getattr(route, "path", "")
        endpoint = getattr(getattr(route, "endpoint", None), "__name__", "")
        is_old_list = path in LIST_PATHS and endpoint == "get_sessions"
        is_old_stop = path in STOP_PATHS and endpoint == "stop_session"
        is_old_report = REPORT_PATHS.get(path) == endpoint
        if is_old_list or is_old_stop or is_old_report:
            superseded += 1
            continue
        kept.append(route)
    if superseded:
        router.routes = kept

    for path in LIST_PATHS:
        router.get(path)(get_sessions_scoped)
    for path in STOP_PATHS:
        router.delete(path)(stop_session_checked)
    report_routes = _install_report_routes(
        router, {name: getattr(compat_routes, name) for name in set(REPORT_PATHS.values())}
    )

    logger.info(
        "会话端点：已替换 %d 条旧路由（未鉴权列表/结束 + 可预测会话键），注册 %d 条带鉴权上报路由",
        superseded,
        report_routes,
    )
    return {"superseded": superseded, "report_routes": report_routes}


__all__ = [
    "install_session_routes",
    "resolve_session_key",
    "get_sessions_scoped",
    "stop_session_checked",
    "LIST_PATHS",
    "STOP_PATHS",
    "REPORT_PATHS",
    "SESSION_KEY_RE",
]
