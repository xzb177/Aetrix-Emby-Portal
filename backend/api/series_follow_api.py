# -*- coding: utf-8 -*-
"""全自动追剧用户端接口（只读状态 + 显式订阅/取消，2026-10-27 首批第二单）。

挂在 /api/user/series-follows（用户 JWT 鉴权，get_current_user），
只操作调用方本人的订阅行；绝不对外发消息，状态只在站内可见。

开关 SERIES_FOLLOW_ENABLED 默认关：关时三口一律 404（不是空
列表，避免“关着却看着像没订阅”的误读）。订阅接口只建订阅行，
不建转存任务（建任务只由工人循环的扫集做）；状态接口只读，
不触发扫集、不建任务。
"""

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from backend import models
from backend.api.user import get_current_user
from backend.database import get_db
from backend.emby_server import series_follow as sf

router = APIRouter(prefix="/api/user/series-follows",
                   tags=["追剧订阅"])


def _require_enabled():
    if not sf.series_follow_enabled():
        raise HTTPException(status_code=404, detail="追剧订阅未启用")


class SeriesFollowRequest(BaseModel):
    series_item_id: int = Field(gt=0)


@router.get("")
def list_series_follows(
    current_user: models.WebUser = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """本人订阅与每集转存状态（只读）。"""
    _require_enabled()
    return sf.follow_status(db, current_user.id)


@router.post("")
def subscribe_series(
    request: SeriesFollowRequest,
    current_user: models.WebUser = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """显式订阅一部剧（opt-in；已订阅幂等）。"""
    _require_enabled()
    out = sf.subscribe(db, current_user.id, request.series_item_id)
    if not out["subscribed"]:
        if out["reason"] == "series_not_found":
            raise HTTPException(status_code=404, detail="剧集不存在")
        raise HTTPException(status_code=400, detail=out["reason"])
    return out


@router.delete("/{series_item_id}")
def unsubscribe_series(
    series_item_id: int,
    current_user: models.WebUser = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """取消订阅（只置 cancelled，不删行）。"""
    _require_enabled()
    return sf.unsubscribe(db, current_user.id, series_item_id)
