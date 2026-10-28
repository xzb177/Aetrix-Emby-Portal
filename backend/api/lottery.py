"""群抽奖公开 API：公平核验（无需登录，纯只读）+ 等级治理 Step 3 用户端。

Step 3 用户端（需 JWT 登录，仅 free realm 生效）：
- POST /proposals：Lv.2+ 提交活动提案（只写提案行，不建轮次）。
- GET /proposals/mine：查自己的提案。
- POST /rounds/direct：Lv.5+ 直接发起活动（建轮次 + 写直接发起记录行）。

请求体与管理端 RoundCreateRequest 同形，但本地自写模型、不从 welfare_admin
导入（公开路由不依赖管理端模块）。服务端逻辑在 backend/lottery_governance.py。
"""
from __future__ import annotations

import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from backend import lottery as lottery_core
from backend import lottery_governance, models
from backend.api.user import get_current_user
from backend.database import get_db

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/lottery", tags=["群抽奖"])


def _lottery():
    """惰性导入 lottery 模块（G1 契约，并行任务尚未合入时返回 None）。"""
    try:
        from backend import lottery

        return lottery
    except ImportError:
        return None


@router.get("/rounds/{round_id}/fairness")
def round_fairness(round_id: int, db: Session = Depends(get_db)) -> dict:
    """公开公平核验：返回某轮抽奖的种子哈希/种子/算法/参与者/中奖者。

    无需登录，纯只读。开奖前仅暴露 seed_hash，开奖后才暴露 seed（由 lottery.verify_round 控制）。
    """
    lot = _lottery()
    if lot is None:
        raise HTTPException(status_code=503, detail="抽奖功能暂未开启")
    try:
        data = lot.verify_round(db, round_id)
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        logger.warning("verify_round failed for round %s: %s", round_id, exc)
        raise HTTPException(status_code=404, detail="抽奖轮次不存在")
    if not isinstance(data, dict):
        raise HTTPException(status_code=500, detail="核验数据异常")
    return data


# ==================== 等级治理 Step 3：用户端 ====================


class GovernancePrizeItem(BaseModel):
    name: str = ""
    type: str = Field(..., description="days/points/whitelist")
    value: int = Field(0, ge=0)
    quantity: int = Field(1, ge=1)


class GovernanceRoundRequest(BaseModel):
    """与管理端 RoundCreateRequest 同形（本地自写，不跨模块导入）。

    chat_id 为空时沿用管理端规则：允许群恰好一个才自动解析，否则 400。
    """

    title: str
    chat_id: Optional[int] = None
    prizes: list[GovernancePrizeItem]
    draw_at: Optional[str] = None
    max_participants: Optional[int] = None
    lottery_type: str = "button"
    password_keyword: Optional[str] = None


def _gov_error(exc: lottery_governance.LotteryGovernanceError) -> HTTPException:
    return HTTPException(status_code=exc.status_code, detail=exc.detail)


@router.post("/proposals")
def submit_proposal(
    req: GovernanceRoundRequest,
    current_user: models.WebUser = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    """Lv.2+ 提交活动提案：只写提案行，不建轮次、不产生经验。

    管理员批准后才建轮次并走正常开奖流程；拒绝则不产生任何东西。
    """
    try:
        proposal = lottery_governance.submit_proposal(
            db, current_user.id, req.model_dump()
        )
    except lottery_governance.LotteryGovernanceError as exc:
        raise _gov_error(exc)
    return lottery_governance.proposal_to_dict(proposal)


@router.get("/proposals/mine")
def list_my_proposals(
    status: Optional[str] = Query(None, description="pending/approved/rejected"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    current_user: models.WebUser = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    """查自己的活动提案（分页，默认倒序）。"""
    total, items = lottery_governance.list_proposals(
        db, user_id=current_user.id, status=status, page=page, page_size=page_size
    )
    return {"total": total, "items": items}


@router.post("/rounds/direct")
def direct_launch_round(
    req: GovernanceRoundRequest,
    current_user: models.WebUser = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    """Lv.5+ 直接发起活动：不经审批建轮次，并写直接发起记录行（事后审计）。

    返回体与管理端创建端点同形（含 seed_hash 承诺与 notify 结果）；
    滥用时管理员可用现有取消端点取消轮次。
    """
    try:
        round_obj, _record = lottery_governance.direct_launch(
            db, current_user.id, req.model_dump()
        )
    except lottery_governance.LotteryGovernanceError as exc:
        raise _gov_error(exc)
    try:
        notify = lottery_core.notify_new_round(db, round_obj.id)
    except Exception:  # noqa: BLE001
        notify = {"ok": False}
    return {
        "id": round_obj.id,
        "title": round_obj.title,
        "chat_id": round_obj.chat_id,
        "status": round_obj.status,
        "lottery_type": round_obj.lottery_type,
        "password_keyword": round_obj.password_keyword,
        "seed_hash": round_obj.seed_hash,
        "notify": notify,
    }
