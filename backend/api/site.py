"""站点信息（公开端点）

前端在启动时读一次这里，决定顶栏站名 / 登录页标题 / 浏览器标题 / 主题色。
**不需要鉴权**：这些信息本来就要显示给未登录的访客看（登录页），其中也不含任何凭据——
私钥、API Key 之类一律不出现在这里。

值来自「站点与品牌」能力（后台「系统设置」里填），非法值由能力模块自己回落——
主题色的十六进制校验与 Logo 的协议校验都在 ``backend/integrations/branding.py``。
"""
from __future__ import annotations

import logging
import threading
import time

from fastapi import APIRouter, Depends, Request
from sqlalchemy import and_, func, or_
from sqlalchemy.orm import Session

from backend import realms
from backend.database import get_db
from backend.emby_server.models import MediaItem
from backend.emby_server.portal import resolve_emby_base_url
from backend.integrations import branding as branding_capability
from backend.tg_bind import get_bot_username

logger = logging.getLogger(__name__)

site_router = APIRouter(prefix="/api/site", tags=["站点信息"])


@site_router.get("/branding")
def get_branding(db: Session = Depends(get_db)):
    """站点名称 / Logo / 主题色 / SEO（公开）"""
    return branding_capability.public_info(db)


@site_router.get("/tg-login-entry")
def get_tg_login_entry(db: Session = Depends(get_db)) -> dict:
    """登录页「用 Telegram 登录」入口所需的机器人公开用户名（公开）

    登录页据此决定是否渲染 TG 登录入口：点入口跳到机器人私聊，
    在主菜单点「一键免密进入控制面板」即可免密登录。这里只暴露
    机器人的公开用户名（本来就印在机器人主页上，任何人可见），
    不含 token 等任何凭据。未配置机器人时返回空串，前端不渲染入口；
    读取异常同样回落空串——公开端点绝不能 500 拖垮登录页。
    """
    try:
        bot_username = get_bot_username(db)
    except Exception:
        logger.warning("获取 Telegram 机器人用户名失败，登录入口将不展示", exc_info=True)
        bot_username = ""
    return {"bot_username": bot_username or ""}


# 登录页海报拼贴（公开）：服务端缓存 10 分钟，海报不常变
_LOGIN_POSTERS_TTL = 600
# 失败负缓存：DB 抖动期间留 30 秒窗口，避免每个请求都重打全表随机查询
_LOGIN_POSTERS_FAIL_TTL = 30
# at 初值用 None 表示"从未刷新"：monotonic() 起点是系统启动时刻，
# 用 0.0 会在开机不足 TTL 时误判缓存新鲜，导致登录页持续空白
_login_posters_cache: dict = {"at": None, "items": [], "emby_base_url": ""}
# single-flight 锁：TTL 到期瞬间只放一个线程去刷，其余直接返回旧缓存
_login_posters_lock = threading.Lock()


@site_router.get("/login-posters")
def get_login_posters(db: Session = Depends(get_db), request: Request = None):
    """登录页背景海报拼贴用到的条目（公开）

    只返回 32 位 guid + 片名：图片 URL 由前端用已公开的
    ``/emby/Items/{id}/Images/Primary`` 端点拼装，不暴露文件路径、
    播放地址或任何用户数据。异常时返回空列表，前端回退静态背景。

    ``emby_base_url`` 与 ``GET /api/user/emby/server`` 的 ``base_url``
    同源（``resolve_emby_base_url``）：登录页未登录拿不到 EA 地址，
    前端据此拼出 EA 上的海报直链，避免相对路径被反代到错误的后端。
    """
    now = time.monotonic()
    at = _login_posters_cache["at"]
    # 命中缓存（含失败后的负缓存窗口）直接返回
    if at is not None:
        ttl = _LOGIN_POSTERS_TTL if _login_posters_cache["items"] else _LOGIN_POSTERS_FAIL_TTL
        if now - at < ttl:
            return {
                "items": _login_posters_cache["items"],
                "emby_base_url": _login_posters_cache["emby_base_url"],
            }
    # single-flight：拿不到锁说明已有线程在刷，直接返回旧缓存
    # （哪怕已过期，旧海报也比并发打爆 DB 好）
    if not _login_posters_lock.acquire(blocking=False):
        return {
            "items": _login_posters_cache["items"],
            "emby_base_url": _login_posters_cache["emby_base_url"],
        }
    try:
        # 拿锁后二次检查：等待期间可能已被别的线程刷过
        now = time.monotonic()
        at = _login_posters_cache["at"]
        if at is not None:
            ttl = _LOGIN_POSTERS_TTL if _login_posters_cache["items"] else _LOGIN_POSTERS_FAIL_TTL
            if now - at < ttl:
                return {
                    "items": _login_posters_cache["items"],
                    "emby_base_url": _login_posters_cache["emby_base_url"],
                }
        try:
            rows = (
                db.query(MediaItem.guid, MediaItem.name)
                .filter(
                    MediaItem.deleted_at.is_(None),
                    MediaItem.item_type.in_(("movie", "series")),
                    or_(
                        and_(
                            MediaItem.poster_path.isnot(None),
                            MediaItem.poster_path != "",
                        ),
                        and_(
                            MediaItem.primary_image_url.isnot(None),
                            MediaItem.primary_image_url != "",
                        ),
                    ),
                )
                .order_by(func.random())
                .limit(36)
                .all()
            )
            items = [{"id": r.guid, "name": r.name} for r in rows]
            # EA 地址：与 /api/user/emby/server 的 base_url 同一配置源（不硬编码）
            # 与查询同处 try：active_realm_id / resolve_emby_base_url 都会读 DB、都可能抛异常，
            # 必须一并兜底，否则前端拿不到降级响应
            emby_base_url = resolve_emby_base_url(db, realms.active_realm_id(db), request) or ""
        except Exception:
            logger.exception("login-posters 查询失败")
            # 失败也推进 at（保留旧 items）：负缓存 30 秒，避免 DB 抖动期故障放大
            _login_posters_cache["at"] = now
            return {
                "items": _login_posters_cache["items"],
                "emby_base_url": _login_posters_cache["emby_base_url"],
            }
        _login_posters_cache["at"] = now
        _login_posters_cache["items"] = items
        _login_posters_cache["emby_base_url"] = emby_base_url
        return {"items": items, "emby_base_url": emby_base_url}
    finally:
        _login_posters_lock.release()


__all__ = ["site_router"]
