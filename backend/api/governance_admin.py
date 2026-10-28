"""等级公益服活动治理 Step 4：管理端接口（停权工具＋审计日志只读）。

挂在既有 ``admin_router``（/api/admin 前缀）上，由 ``backend/api/__init__.py``
导入注册。写操作同时写两条审计：既有管理审计（``_audit``）与治理审计
日志（Lv.6 可见，``record_governance_audit``），同一事务一次提交。

范围：仅公益服生效（``subscriptions.is_free_realm``），付费服下一律 403；
审计日志只有读接口，没有写／改／删接口。
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Optional

from fastapi import Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from backend import governance, models
from backend.api.admin_core import admin_router, get_current_admin, _audit
from backend.database import get_db
from backend.subscriptions import is_free_realm


def _require_free_realm(db: Session) -> None:
    if not is_free_realm(db):
        raise HTTPException(status_code=403, detail="治理功能仅在公益服开放")


def _audit_row(row: models.GovernanceAuditLog, names: dict) -> dict:
    try:
        detail = json.loads(row.detail) if row.detail else {}
    except (ValueError, TypeError):
        detail = {"raw": row.detail}
    return {
        "id": row.id,
        "action": row.action,
        "actor_user_id": row.actor_user_id,
        "actor_name": names.get(row.actor_user_id) if row.actor_user_id else None,
        "target_user_id": row.target_user_id,
        "target_name": names.get(row.target_user_id) if row.target_user_id else None,
        "target_ref": row.target_ref,
        "detail": detail,
        "created_at": row.created_at.isoformat() if row.created_at else None,
    }


@admin_router.get("/governance/audit-logs")
def governance_audit_logs(
    limit: int = Query(50, ge=1, le=200),
    action: Optional[str] = None,
    actor_id: Optional[int] = None,
    target_id: Optional[int] = None,
    current_admin: models.WebUser = Depends(get_current_admin),
    db: Session = Depends(get_db),
):
    """治理审计日志只读查询（管理员全量）。"""
    _require_free_realm(db)
    rows = governance.list_audit_logs(
        db, limit=limit, action=action or None,
        actor_user_id=actor_id, target_user_id=target_id,
    )
    ids = {r.actor_user_id for r in rows if r.actor_user_id}
    ids |= {r.target_user_id for r in rows if r.target_user_id}
    names = (
        {u.id: u.username for u in db.query(models.WebUser).filter(models.WebUser.id.in_(ids)).all()}
        if ids else {}
    )
    return {"items": [_audit_row(r, names) for r in rows]}


@admin_router.get("/governance/freezes")
def governance_freezes(
    current_admin: models.WebUser = Depends(get_current_admin),
    db: Session = Depends(get_db),
):
    """当前生效中的治理停权列表。"""
    _require_free_realm(db)
    now = datetime.now()
    rows = (
        db.query(models.GovernanceFreeze)
        .filter(models.GovernanceFreeze.released_at.is_(None))
        .order_by(models.GovernanceFreeze.id.desc())
        .all()
    )
    active = [r for r in rows if r.expires_at is None or r.expires_at > now]
    ids = {r.user_id for r in active} | {r.created_by for r in active}
    names = (
        {u.id: u.username for u in db.query(models.WebUser).filter(models.WebUser.id.in_(ids)).all()}
        if ids else {}
    )
    return {"items": [
        {
            "id": r.id,
            "user_id": r.user_id,
            "username": names.get(r.user_id),
            "reason": r.reason,
            "expires_at": r.expires_at.isoformat() if r.expires_at else None,
            "created_by": r.created_by,
            "created_by_name": names.get(r.created_by),
            "created_at": r.created_at.isoformat() if r.created_at else None,
        }
        for r in active
    ]}


class FreezeRequest(BaseModel):
    user_id: int
    reason: str
    days: Optional[int] = Field(default=None, description="停权天数，空=不限期")


@admin_router.post("/governance/freeze")
def governance_freeze(
    req: FreezeRequest,
    current_admin: models.WebUser = Depends(get_current_admin),
    db: Session = Depends(get_db),
):
    """冻结指定用户的治理特权（不扣经验、不影响核心权益）。"""
    _require_free_realm(db)
    user = db.query(models.WebUser).filter(models.WebUser.id == req.user_id).first()
    if user is None:
        raise HTTPException(status_code=404, detail="用户不存在")
    if governance.active_freeze(db, req.user_id) is not None:
        raise HTTPException(status_code=409, detail="该用户治理特权已被冻结")
    try:
        row = governance.freeze_governance(
            db, user_id=req.user_id, reason=req.reason,
            days=req.days, admin_id=current_admin.id,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    _audit(db, current_admin.id, "governance_freeze", "user", req.user_id,
           {"reason": req.reason, "days": req.days})
    db.commit()
    return {"success": True, "freeze_id": row.id,
            "expires_at": row.expires_at.isoformat() if row.expires_at else None}


class FreezeReleaseRequest(BaseModel):
    user_id: int


@admin_router.post("/governance/freeze/release")
def governance_freeze_release(
    req: FreezeReleaseRequest,
    current_admin: models.WebUser = Depends(get_current_admin),
    db: Session = Depends(get_db),
):
    """解封指定用户的治理停权。"""
    _require_free_realm(db)
    if governance.active_freeze(db, req.user_id) is None:
        raise HTTPException(status_code=404, detail="该用户没有生效中的治理停权")
    row = governance.release_governance_freeze(
        db, user_id=req.user_id, admin_id=current_admin.id,
    )
    _audit(db, current_admin.id, "governance_freeze_release", "user", req.user_id,
           {"freeze_id": row.id})
    db.commit()
    return {"success": True, "freeze_id": row.id}


class DowngradeRequest(BaseModel):
    user_id: int
    xp: int = Field(gt=0, description="扣减经验数，须为正数")
    reason: str


@admin_router.post("/governance/downgrade")
def governance_downgrade(
    req: DowngradeRequest,
    current_admin: models.WebUser = Depends(get_current_admin),
    db: Session = Depends(get_db),
):
    """管理员手动降级：扣经验（钳制在 0）＋重算等级＋审计，理由必填。"""
    _require_free_realm(db)
    user = db.query(models.WebUser).filter(models.WebUser.id == req.user_id).first()
    if user is None:
        raise HTTPException(status_code=404, detail="用户不存在")
    try:
        result = governance.governance_downgrade(
            db, user_id=req.user_id, xp=req.xp,
            reason=req.reason, admin_id=current_admin.id,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    _audit(db, current_admin.id, "governance_downgrade", "user", req.user_id,
           {"xp": req.xp, "reason": req.reason})
    db.commit()
    return {"success": True, **result}
