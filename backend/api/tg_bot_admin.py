"""管理端 TG Bot 状态（P0 可见性）：poller 心跳与最后一次轮询错误。

只读接口：读 SystemConfig 里的心跳与报错键，不触轮询、不改配置。
鉴权与管理端一致：get_current_admin + JWT。
"""
from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends
from sqlalchemy import func
from sqlalchemy.orm import Session

from backend import models as web_models
from backend.api.admin_core import get_current_admin
from backend.database import get_db
from backend.integrations import store
from backend.tg_bot import poller

tg_bot_admin_router = APIRouter(prefix="/api/admin", tags=["管理后台·TG Bot"])


def _heartbeat_age_seconds(heartbeat: str) -> int | None:
    """心跳时间戳距今秒数；空/不可解析返回 None（前端据此显示无心跳）。"""
    if not heartbeat:
        return None
    try:
        last = datetime.fromisoformat(str(heartbeat))
    except (TypeError, ValueError):
        return None
    return max(0, int((datetime.now() - last).total_seconds()))


def bot_status(db: Session) -> dict:
    """Bot 运行状态（纯函数，供接口与测试复用）。"""
    heartbeat = store.read_value(db, poller.HEARTBEAT_KEY, "") or ""
    last_error = store.read_value(db, poller.LAST_ERROR_KEY, "") or ""
    bound_users = (
        db.query(func.count(web_models.WebUser.id))
        .filter(web_models.WebUser.telegram_id.isnot(None))
        .scalar()
    )
    return {
        "poller_active": poller.is_active(db),
        "last_heartbeat": heartbeat,
        "heartbeat_age_seconds": _heartbeat_age_seconds(heartbeat),
        "last_error": last_error,
        "bound_users": int(bound_users or 0),
    }


@tg_bot_admin_router.get("/tg-bot/status")
def get_tg_bot_status(
    db: Session = Depends(get_db),
    current_admin=Depends(get_current_admin),
):
    return bot_status(db)
