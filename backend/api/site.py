"""站点信息（公开端点）

前端在启动时读一次这里，决定顶栏站名 / 登录页标题 / 浏览器标题 / 主题色。
**不需要鉴权**：这些信息本来就要显示给未登录的访客看（登录页），其中也不含任何凭据——
私钥、API Key 之类一律不出现在这里。

值来自「站点与品牌」能力（后台「系统设置」里填），非法值由能力模块自己回落——
主题色的十六进制校验与 Logo 的协议校验都在 ``backend/integrations/branding.py``。
"""
from __future__ import annotations

import logging
import time

from fastapi import APIRouter, Depends, Request
from sqlalchemy import and_, func, or_
from sqlalchemy.orm import Session

from backend import realms
from backend.database import get_db
from backend.emby_server.models import MediaItem
from backend.emby_server.portal import resolve_emby_base_url
from backend.integrations import branding as branding_capability

logger = logging.getLogger(__name__)

site_router = APIRouter(prefix="/api/site", tags=["站点信息"])


@site_router.get("/branding")
def get_branding(db: Session = Depends(get_db)):
    """站点名称 / Logo / 主题色 / SEO（公开）"""
    return branding_capability.public_info(db)


# 登录页海报拼贴（公开）：服务端缓存 10 分钟，海报不常变
_LOGIN_POSTERS_TTL = 600
_login_posters_cache: dict = {"at": 0.0, "items": [], "emby_base_url": ""}


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
    if now - _login_posters_cache["at"] < _LOGIN_POSTERS_TTL:
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
    except Exception:
        logger.exception("login-posters 查询失败")
        return {"items": [], "emby_base_url": ""}
    # EA 地址：与 /api/user/emby/server 的 base_url 同一配置源（不硬编码）
    emby_base_url = resolve_emby_base_url(db, realms.active_realm_id(db), request) or ""
    _login_posters_cache["at"] = now
    _login_posters_cache["items"] = items
    _login_posters_cache["emby_base_url"] = emby_base_url
    return {"items": items, "emby_base_url": emby_base_url}


__all__ = ["site_router"]
