"""管理端 TG Bot 状态（P0 可见性）：poller 心跳与最后一次轮询错误。

只读接口：读 SystemConfig 里的心跳与报错键，不触轮询、不改配置。
鉴权与管理端一致：get_current_admin + JWT。
"""
from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from backend.api.admin_core import get_current_admin
from backend.database import get_db
from backend.integrations import store
from backend.tg_bot import poller

tg_bot_admin_router = APIRouter(prefix="/api/admin", tags=["管理后台·TG Bot"])


def bot_status(db: Session) -> dict:
    """Bot 运行状态（纯函数，供接口与测试复用）。"""
    heartbeat = store.read_value(db, poller.HEARTBEAT_KEY, "") or ""
    last_error = store.read_value(db, poller.LAST_ERROR_KEY, "") or ""
    return {
        "poller_active": poller.is_active(db),
        "last_heartbeat": heartbeat,
        "last_error": last_error,
    }


@tg_bot_admin_router.get("/tg-bot/status")
def get_tg_bot_status(
    db: Session = Depends(get_db),
    current_admin=Depends(get_current_admin),
):
    return bot_status(db)
