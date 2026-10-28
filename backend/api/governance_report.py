"""等级公益服活动治理 Step 2：举报＋复核＋终审队列（用户端／管理端路由）。

用户端：举报求片、查自己的举报、投复核票（仅 Lv.3 及以上）。
管理端：终审队列（默认 escalated）、终审受理／驳回、停权列表／手动停权／撤销。
鉴权：用户端走 get_current_user，管理端走 get_current_admin（集中判角色）。
服务层错误 GovernanceError 直接转 HTTPException。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from backend import governance_report as gov
from backend.api.admin_core import get_current_admin
from backend.api.user import get_current_user
from backend.database import get_db

router = APIRouter(prefix="/api/user/governance", tags=["用户端·治理举报"])
admin_router = APIRouter(prefix="/api/admin/governance", tags=["管理端·治理举报"])


def _http(err: gov.GovernanceError) -> HTTPException:
    return HTTPException(status_code=err.status_code, detail=err.detail)


class ReportIn(BaseModel):
    target_id: int = Field(..., description="求片条目 id（v1 仅求片）")
    reason: str = Field(..., description="spam / duplicate / violation")
    detail: str | None = None


class ReviewVoteIn(BaseModel):
    decision: str = Field(..., description="agree / disagree")


class DecideIn(BaseModel):
    decision: str = Field(..., description="accepted / rejected")
    note: str | None = None


class SuspendIn(BaseModel):
    user_id: int
    days: int = 30
    note: str | None = None


@router.post("/reports", status_code=201)
def create_report(body: ReportIn, db=Depends(get_db), user=Depends(get_current_user)):
    """举报一条求片（同一用户对同一条目终身只能举报 1 次）。"""
    try:
        return gov.create_report(db, user, body.target_id, body.reason, body.detail)
    except gov.GovernanceError as err:
        raise _http(err) from None


@router.get("/reports")
def list_my_reports(
    db=Depends(get_db),
    user=Depends(get_current_user),
    limit: int = Query(100, ge=1, le=200),
):
    """自己的举报列表（最新在前，含复核统计）。"""
    return gov.list_my_reports(db, user, limit)


@router.get("/reports/{report_id}")
def get_my_report(
    report_id: int, db=Depends(get_db), user=Depends(get_current_user)
):
    """举报详情（仅举报人可看；终审结果随行返回）。"""
    try:
        return gov.get_report_detail(db, user, report_id, is_admin=False)
    except gov.GovernanceError as err:
        raise _http(err) from None


@router.post("/reports/{report_id}/review-votes")
def cast_review_vote(
    report_id: int,
    body: ReviewVoteIn,
    db=Depends(get_db),
    user=Depends(get_current_user),
):
    """投复核票（Lv.3 及以上；quorum≥3 人且同意权重≥2/3 时升级终审队列）。"""
    try:
        return gov.cast_review_vote(db, user, report_id, body.decision)
    except gov.GovernanceError as err:
        raise _http(err) from None


@admin_router.get("/reports/queue")
def admin_report_queue(
    db=Depends(get_db),
    admin=Depends(get_current_admin),
    status: str | None = Query(None, description="默认只列终审队列 escalated"),
    limit: int = Query(100, ge=1, le=200),
):
    """管理端举报列表：默认终审队列（escalated），传 status 可筛其它状态。"""
    return gov.admin_list_reports(db, status, limit)


@admin_router.get("/reports/{report_id}")
def admin_get_report(
    report_id: int, db=Depends(get_db), admin=Depends(get_current_admin)
):
    """管理端举报详情（含全部复核票）。"""
    try:
        return gov.get_report_detail(db, admin, report_id, is_admin=True)
    except gov.GovernanceError as err:
        raise _http(err) from None


@admin_router.post("/reports/{report_id}/decide")
def admin_decide_report(
    report_id: int,
    body: DecideIn,
    db=Depends(get_db),
    admin=Depends(get_current_admin),
):
    """管理员终审：受理 accepted / 驳回 rejected。受理不自动处理求片本身；
    驳回计入举报人终审驳回计数，达标自动停权（随响应返回 auto_suspension）。"""
    try:
        return gov.admin_decide(db, admin, report_id, body.decision, body.note)
    except gov.GovernanceError as err:
        raise _http(err) from None


@admin_router.get("/suspensions")
def admin_list_suspensions(
    db=Depends(get_db),
    admin=Depends(get_current_admin),
    user_id: int | None = Query(None),
    active_only: bool = Query(False),
    limit: int = Query(100, ge=1, le=200),
):
    """管理端停权列表（最新在前）。"""
    return gov.admin_list_suspensions(db, user_id, active_only, limit)


@admin_router.post("/suspensions", status_code=201)
def admin_create_suspension(
    body: SuspendIn, db=Depends(get_db), admin=Depends(get_current_admin)
):
    """管理员手动停权：冻结治理特权（举报／复核），不扣经验、不降级。"""
    try:
        return gov.admin_suspend(db, admin, body.user_id, body.days, body.note)
    except gov.GovernanceError as err:
        raise _http(err) from None


@admin_router.post("/suspensions/{suspension_id}/revoke")
def admin_revoke_suspension(
    suspension_id: int, db=Depends(get_db), admin=Depends(get_current_admin)
):
    """管理员撤销停权（仅生效中的可撤销）。"""
    try:
        return gov.admin_unsuspend(db, admin, suspension_id)
    except gov.GovernanceError as err:
        raise _http(err) from None
