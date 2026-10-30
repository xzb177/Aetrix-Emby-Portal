# -*- coding: utf-8 -*-
"""公益服能量 API 路由（挂载到 backend/points.py 的 router）

端点（前缀 /api/points）：
- POST /signin          【已废弃】转发到 POST /api/user/economy/checkin
- POST /redeem          能量兑换公益天数 {days_option: 7 | 30}
- GET  /balance         能量概览（余额/今日获得/累计获得）
- GET  /welfare-status  福利资格状态（本人：是否公益用户 + 资格到期日）
- GET  /logs            能量明细（分页）
- POST /chat-award      发言奖励（内部调用，供聊天模块用）
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from backend import models
from backend import points as points_mod
from backend.api.user import get_current_user
from backend.database import get_db
from backend.tg_bind import require_tg_bound

router = APIRouter(prefix="/api/points", tags=["公益服-能量"])


class RedeemRequest(BaseModel):
    days_option: int = Field(..., description="兑换天数，仅支持 7 或 30")


@router.post("/signin", deprecated=True)
def welfare_signin(
    current_user: models.WebUser = Depends(get_current_user),
    db: Session = Depends(get_db),
    _tg: models.WebUser = Depends(require_tg_bound)
):
    """【已废弃 2026-10-09】公益服签到已统一到 POST /api/user/economy/checkin。

    此接口仅为兼容保留，转发到统一签到逻辑。新客户端请使用 /api/user/economy/checkin。
    """
    try:
        return points_mod.welfare_signin(db, current_user)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/redeem")
def redeem_welfare(
    req: RedeemRequest,
    current_user: models.WebUser = Depends(get_current_user),
    db: Session = Depends(get_db),
    _tg: models.WebUser = Depends(require_tg_bound)
):
    """能量兑换公益天数"""
    try:
        return points_mod.redeem_welfare(db, current_user, req.days_option)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.get("/welfare-status")
def welfare_status(
    current_user: models.WebUser = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """福利资格状态（本人）：是否公益用户 + 资格到期日，供续期入口首屏展示。"""
    return points_mod.get_welfare_status(db, current_user)


@router.get("/balance")
def points_balance(
    current_user: models.WebUser = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """能量概览"""
    return points_mod.get_points_summary(db, current_user)


@router.get("/logs")
def points_logs(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    current_user: models.WebUser = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """能量明细（分页）"""
    q = db.query(models.PointsLog).filter(
        models.PointsLog.user_id == current_user.id
    ).order_by(models.PointsLog.id.desc())
    total = q.count()
    items = q.offset((page - 1) * page_size).limit(page_size).all()
    return {
        "total": total,
        "page": page,
        "page_size": page_size,
        "items": [
            {
                "id": it.id,
                "amount": it.amount,
                "balance_after": it.balance_after,
                "type": it.type,
                "description": it.description,
                "created_at": it.created_at.isoformat() if it.created_at else None,
            }
            for it in items
        ],
    }


@router.post("/chat-award")
def chat_award(
    user_id: int = Query(..., description="被奖励用户 ID"),
    current_user: models.WebUser = Depends(get_current_user),
    db: Session = Depends(get_db),
    _tg: models.WebUser = Depends(require_tg_bound)
):
    """发言奖励（内部接口）

    P1 修复（审查）：此前任意登录用户可给任意 user_id 发奖励，
    零发言每天白嫖上限能量。现强制只能给自己发。
    """
    if user_id != current_user.id:
        raise HTTPException(status_code=403, detail="只能为自己领取发言奖励")
    awarded = points_mod.award_chat_points(db, user_id)
    return {"awarded": awarded}
