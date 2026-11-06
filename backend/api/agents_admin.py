"""管理后台 · 代理分销 API（等级 / 代理指定 / 余额 / 全量统计）

用户 2026-11-06 亲点完整版。语义与账目口径见 backend/agents.py：
等级是可配数据、代理靠指定、余额是独立账本（只增不改的流水）、
销售归属在核销事务里落账。鉴权与管理端完全一致：get_current_admin。
"""
import logging
from decimal import Decimal, InvalidOperation
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import func
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from backend import agents as agent_service
from backend import models
from backend.api.admin_core import _audit, get_current_admin
from backend.api.pagination import pagination_params
from backend.database import get_db
from backend.usernames import find_users_by_username_ci

logger = logging.getLogger(__name__)

agents_admin_router = APIRouter(prefix="/api/admin", tags=["管理后台·代理分销"])


# ==================== DTO ====================

def level_dto(db: Session, level: models.AgentLevel) -> dict:
    agent_count = db.query(func.count(models.Agent.id)).filter(
        models.Agent.level_id == level.id).scalar() or 0
    return {
        "id": level.id,
        "name": level.name,
        "discount_rate": float(level.discount_rate),
        "sort_order": level.sort_order or 0,
        "is_active": bool(level.is_active),
        "agent_count": int(agent_count),
        "created_at": level.created_at.isoformat() if level.created_at else None,
    }


def agent_dto(db: Session, agent: models.Agent) -> dict:
    user = agent.user
    level = agent.level
    dto = {
        "id": agent.id,
        "user_id": agent.user_id,
        "username": user.username if user else "",
        "level_id": agent.level_id,
        "level_name": level.name if level else "",
        "discount_rate": float(level.discount_rate) if level else 0,
        "balance": float(agent_service.money(agent.balance or 0)),
        "is_active": bool(agent.is_active),
        "note": agent.note or "",
        "created_at": agent.created_at.isoformat() if agent.created_at else None,
    }
    dto.update(agent_service.agent_summary(db, agent.id))
    return dto


def _get_level_or_404(db: Session, level_id: int) -> models.AgentLevel:
    level = db.query(models.AgentLevel).filter(
        models.AgentLevel.id == level_id).first()
    if not level:
        raise HTTPException(status_code=404, detail="代理等级不存在")
    return level


def _get_agent_or_404(db: Session, agent_id: int) -> models.Agent:
    agent = agent_service.get_agent(db, agent_id)
    if not agent:
        raise HTTPException(status_code=404, detail="代理不存在")
    return agent


def _parse_rate(raw) -> Decimal:
    try:
        rate = Decimal(str(raw))
    except (InvalidOperation, ValueError):
        raise HTTPException(status_code=400, detail="折扣率非法")
    if rate <= 0 or rate > 1:
        raise HTTPException(status_code=400, detail="折扣率须在 (0, 1] 之间（0.8 = 八折进货）")
    return rate


# ==================== 等级管理 ====================

@agents_admin_router.get("/agents/levels")
def list_levels(
    current_admin: models.WebUser = Depends(get_current_admin),
    db: Session = Depends(get_db),
):
    rows = db.query(models.AgentLevel).order_by(
        models.AgentLevel.sort_order, models.AgentLevel.id).all()
    return {"levels": [level_dto(db, r) for r in rows]}


class LevelCreateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=50)
    discount_rate: float
    sort_order: int = 0


@agents_admin_router.post("/agents/levels")
def create_level(
    request: LevelCreateRequest,
    current_admin: models.WebUser = Depends(get_current_admin),
    db: Session = Depends(get_db),
):
    name = request.name.strip()
    if not name:
        raise HTTPException(status_code=400, detail="等级名称不能为空")
    rate = _parse_rate(request.discount_rate)
    if db.query(models.AgentLevel).filter(
        models.AgentLevel.name == name
    ).first():
        raise HTTPException(status_code=400, detail=f"等级名称已存在：{name}")
    level = models.AgentLevel(
        name=name, discount_rate=rate, sort_order=request.sort_order,
        is_active=True)
    db.add(level)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(status_code=400, detail=f"等级名称已存在：{name}")
    db.refresh(level)
    _audit(db, current_admin, "create_agent_level", "agent_level", level.id,
           {"name": name, "discount_rate": str(rate)})
    db.commit()
    return {"success": True, "level": level_dto(db, level)}


class LevelUpdateRequest(BaseModel):
    name: Optional[str] = None
    discount_rate: Optional[float] = None
    sort_order: Optional[int] = None
    is_active: Optional[bool] = None


@agents_admin_router.patch("/agents/levels/{level_id}")
def update_level(
    level_id: int,
    request: LevelUpdateRequest,
    current_admin: models.WebUser = Depends(get_current_admin),
    db: Session = Depends(get_db),
):
    level = _get_level_or_404(db, level_id)
    if request.name is not None:
        name = request.name.strip()
        if not name:
            raise HTTPException(status_code=400, detail="等级名称不能为空")
        dup = db.query(models.AgentLevel).filter(
            models.AgentLevel.name == name,
            models.AgentLevel.id != level_id).first()
        if dup:
            raise HTTPException(status_code=400, detail=f"等级名称已存在：{name}")
        level.name = name
    if request.discount_rate is not None:
        level.discount_rate = _parse_rate(request.discount_rate)
    if request.sort_order is not None:
        level.sort_order = request.sort_order
    if request.is_active is not None:
        level.is_active = request.is_active
    db.commit()
    db.refresh(level)
    _audit(db, current_admin, "update_agent_level", "agent_level", level.id,
           {"name": level.name, "discount_rate": str(level.discount_rate),
            "is_active": bool(level.is_active)})
    db.commit()
    return {"success": True, "level": level_dto(db, level)}


@agents_admin_router.delete("/agents/levels/{level_id}")
def delete_level(
    level_id: int,
    current_admin: models.WebUser = Depends(get_current_admin),
    db: Session = Depends(get_db),
):
    """删除等级：有代理在用时拒绝（先改等级或停用等级），不许连带丢归属。"""
    level = _get_level_or_404(db, level_id)
    in_use = db.query(func.count(models.Agent.id)).filter(
        models.Agent.level_id == level_id).scalar() or 0
    if in_use:
        raise HTTPException(
            status_code=400,
            detail=f"该等级下还有 {in_use} 个代理，不能删除；可先改等级或停用")
    db.delete(level)
    db.commit()
    _audit(db, current_admin, "delete_agent_level", "agent_level", level_id,
           {"name": level.name})
    db.commit()
    return {"success": True}


# ==================== 代理管理 ====================

@agents_admin_router.get("/agents")
def list_agents(
    limit: int = 50,
    offset: int = 0,
    current_admin: models.WebUser = Depends(get_current_admin),
    db: Session = Depends(get_db),
):
    limit, offset = pagination_params(limit, offset)
    query = db.query(models.Agent).order_by(models.Agent.id.desc())
    total = query.count()
    rows = query.offset(offset).limit(limit).all()
    return {"total": total, "agents": [agent_dto(db, r) for r in rows]}


class AgentAssignRequest(BaseModel):
    user_id: Optional[int] = None
    username: str = ""
    level_id: int


@agents_admin_router.post("/agents")
def assign_agent(
    request: AgentAssignRequest,
    current_admin: models.WebUser = Depends(get_current_admin),
    db: Session = Depends(get_db),
):
    """指定代理：新建或复活已有记录（改等级 + 重新启用），余额历史保留。"""
    level = _get_level_or_404(db, request.level_id)
    user = None
    if request.user_id:
        user = db.query(models.WebUser).filter(
            models.WebUser.id == request.user_id).first()
        if not user:
            raise HTTPException(status_code=404, detail="用户不存在")
    else:
        username = (request.username or "").strip()
        if not username:
            raise HTTPException(status_code=400, detail="请提供 user_id 或 username")
        matches = find_users_by_username_ci(db, username)
        if not matches:
            raise HTTPException(status_code=404, detail=f"用户不存在：{username}")
        if len(matches) > 1:
            raise HTTPException(status_code=400, detail=f"用户名不唯一：{username}")
        user = matches[0]

    agent = agent_service.get_agent_by_user(db, user.id)
    if agent is None:
        agent = models.Agent(user_id=user.id, level_id=level.id,
                             balance=Decimal("0"), is_active=True)
        db.add(agent)
    else:
        agent.level_id = level.id
        agent.is_active = True
    db.commit()
    db.refresh(agent)
    _audit(db, current_admin, "assign_agent", "agent", agent.id,
           {"user_id": user.id, "username": user.username,
            "level_id": level.id})
    db.commit()
    return {"success": True, "agent": agent_dto(db, agent)}


class AgentUpdateRequest(BaseModel):
    level_id: Optional[int] = None
    is_active: Optional[bool] = None
    note: Optional[str] = None


@agents_admin_router.patch("/agents/{agent_id}")
def update_agent(
    agent_id: int,
    request: AgentUpdateRequest,
    current_admin: models.WebUser = Depends(get_current_admin),
    db: Session = Depends(get_db),
):
    """改等级 / 取消代理（is_active=false，账目保留）/ 改备注。"""
    agent = _get_agent_or_404(db, agent_id)
    if request.level_id is not None:
        _get_level_or_404(db, request.level_id)
        agent.level_id = request.level_id
    if request.is_active is not None:
        agent.is_active = request.is_active
    if request.note is not None:
        agent.note = request.note or None
    db.commit()
    db.refresh(agent)
    _audit(db, current_admin, "update_agent", "agent", agent.id,
           {"level_id": agent.level_id, "is_active": bool(agent.is_active)})
    db.commit()
    return {"success": True, "agent": agent_dto(db, agent)}


class BalanceAdjustRequest(BaseModel):
    amount: str  # 带符号金额（元），字符串入参避免浮点误差
    note: str = ""


@agents_admin_router.post("/agents/{agent_id}/balance")
def adjust_balance(
    agent_id: int,
    request: BalanceAdjustRequest,
    current_admin: models.WebUser = Depends(get_current_admin),
    db: Session = Depends(get_db),
):
    """管理员手工增减进货余额：走账本流水，扣减不许把余额扣成负数。"""
    agent = _get_agent_or_404(db, agent_id)
    try:
        amount = Decimal(str(request.amount))
    except (InvalidOperation, ValueError):
        raise HTTPException(status_code=400, detail="金额非法")
    try:
        log = agent_service.adjust_balance(
            db, agent, amount, note=request.note,
            operator_id=current_admin.id)
    except agent_service.AgentError as exc:
        db.rollback()
        raise HTTPException(status_code=400, detail=str(exc))
    db.commit()
    _audit(db, current_admin, "adjust_agent_balance", "agent", agent.id,
           {"amount": str(amount), "note": request.note})
    db.commit()
    db.refresh(agent)
    return {
        "success": True,
        "agent": agent_dto(db, agent),
        "log": {
            "id": log.id, "amount": float(log.amount),
            "balance_after": float(log.balance_after),
            "note": log.note or "",
            "created_at": log.created_at.isoformat() if log.created_at else None,
        },
    }


# ==================== 单代理流水 / 进货 / 销售 ====================

def _log_dto(log: models.AgentBalanceLog) -> dict:
    return {
        "id": log.id, "agent_id": log.agent_id, "kind": log.kind,
        "amount": float(log.amount),
        "balance_after": float(log.balance_after),
        "note": log.note or "", "ref_id": log.ref_id,
        "created_at": log.created_at.isoformat() if log.created_at else None,
    }


def _purchase_dto(p: models.AgentPurchase) -> dict:
    return {
        "id": p.id, "agent_id": p.agent_id, "plan_id": p.plan_id,
        "plan_name": p.plan_name or "", "realm_id": p.realm_id,
        "code_type": p.code_type, "quantity": p.quantity,
        "face_value": float(p.face_value),
        "discount_rate": float(p.discount_rate),
        "unit_cost": float(p.unit_cost),
        "total_cost": float(p.total_cost),
        "created_at": p.created_at.isoformat() if p.created_at else None,
    }


def _sale_dto(db: Session, sale: models.AgentSale) -> dict:
    buyer = db.query(models.WebUser.username).filter(
        models.WebUser.id == sale.buyer_user_id).first()
    code = db.query(models.RegistrationCode.code).filter(
        models.RegistrationCode.id == sale.code_id).first()
    return {
        "id": sale.id, "agent_id": sale.agent_id,
        "code_id": sale.code_id, "code": code[0] if code else "",
        "purchase_id": sale.purchase_id,
        "buyer_user_id": sale.buyer_user_id,
        "buyer_username": buyer[0] if buyer else "",
        "face_value": float(sale.face_value),
        "unit_cost": float(sale.unit_cost),
        "created_at": sale.created_at.isoformat() if sale.created_at else None,
    }


@agents_admin_router.get("/agents/{agent_id}/ledger")
def agent_ledger(
    agent_id: int,
    limit: int = 50,
    offset: int = 0,
    current_admin: models.WebUser = Depends(get_current_admin),
    db: Session = Depends(get_db),
):
    _get_agent_or_404(db, agent_id)
    limit, offset = pagination_params(limit, offset)
    query = db.query(models.AgentBalanceLog).filter(
        models.AgentBalanceLog.agent_id == agent_id).order_by(
        models.AgentBalanceLog.id.desc())
    total = query.count()
    rows = query.offset(offset).limit(limit).all()
    return {"total": total, "logs": [_log_dto(r) for r in rows]}


@agents_admin_router.get("/agents/{agent_id}/purchases")
def agent_purchases(
    agent_id: int,
    limit: int = 50,
    offset: int = 0,
    current_admin: models.WebUser = Depends(get_current_admin),
    db: Session = Depends(get_db),
):
    _get_agent_or_404(db, agent_id)
    limit, offset = pagination_params(limit, offset)
    query = db.query(models.AgentPurchase).filter(
        models.AgentPurchase.agent_id == agent_id).order_by(
        models.AgentPurchase.id.desc())
    total = query.count()
    rows = query.offset(offset).limit(limit).all()
    return {"total": total, "purchases": [_purchase_dto(r) for r in rows]}


@agents_admin_router.get("/agents/{agent_id}/sales")
def agent_sales(
    agent_id: int,
    limit: int = 50,
    offset: int = 0,
    current_admin: models.WebUser = Depends(get_current_admin),
    db: Session = Depends(get_db),
):
    _get_agent_or_404(db, agent_id)
    limit, offset = pagination_params(limit, offset)
    query = db.query(models.AgentSale).filter(
        models.AgentSale.agent_id == agent_id).order_by(
        models.AgentSale.id.desc())
    total = query.count()
    rows = query.offset(offset).limit(limit).all()
    return {"total": total, "sales": [_sale_dto(db, r) for r in rows]}


# ==================== 全局统计 ====================

@agents_admin_router.get("/agents/sales/all")
def all_sales(
    limit: int = 50,
    offset: int = 0,
    current_admin: models.WebUser = Depends(get_current_admin),
    db: Session = Depends(get_db),
):
    limit, offset = pagination_params(limit, offset)
    query = db.query(models.AgentSale).order_by(models.AgentSale.id.desc())
    total = query.count()
    rows = query.offset(offset).limit(limit).all()
    return {"total": total, "sales": [_sale_dto(db, r) for r in rows]}


@agents_admin_router.get("/agents/overview/stats")
def overview(
    current_admin: models.WebUser = Depends(get_current_admin),
    db: Session = Depends(get_db),
):
    """全局统计：代理数 / 进货 / 销售 / 余额池（管理端统计页）。"""
    agent_count = db.query(func.count(models.Agent.id)).filter(
        models.Agent.is_active.is_(True)).scalar() or 0
    purchase = db.query(
        func.coalesce(func.sum(models.AgentPurchase.quantity), 0),
        func.coalesce(func.sum(models.AgentPurchase.total_cost), 0),
    ).first()
    sales = db.query(
        func.count(models.AgentSale.id),
        func.coalesce(func.sum(models.AgentSale.face_value), 0),
        func.coalesce(func.sum(models.AgentSale.unit_cost), 0),
    ).first()
    topped = db.query(
        func.coalesce(func.sum(models.AgentBalanceLog.amount), 0),
    ).filter(
        models.AgentBalanceLog.kind == "admin_adjust",
        models.AgentBalanceLog.amount > 0).first()
    balance_pool = db.query(
        func.coalesce(func.sum(models.Agent.balance), 0)).first()
    buckets = agent_service.sales_buckets(db, None)
    return {
        "agent_count": int(agent_count),
        "purchase_quantity": int(purchase[0] or 0),
        "purchase_cost_total": float(agent_service.money(purchase[1] or 0)),
        "sales_count": int(sales[0] or 0),
        "sales_face_total": float(agent_service.money(sales[1] or 0)),
        "sales_cost_total": float(agent_service.money(sales[2] or 0)),
        "topped_up_total": float(agent_service.money(topped[0] or 0)),
        "balance_pool_total": float(agent_service.money(balance_pool[0] or 0)),
        "daily": buckets["daily"],
        "monthly": buckets["monthly"],
    }
