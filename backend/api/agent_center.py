"""用户端 · 代理中心 API（只有被管理员指定的代理能用）。

未被指定为代理的用户：/me 返回 is_agent=false（前端据此不显示入口），
其余接口一律 403——没指定的人完全无感，这是功能语义的一部分。
数据隔离：所有查询都按「当前用户的代理行」过滤，代理只能看自己的
余额 / 流水 / 卡码 / 销售，越权读他人数据在查询层就不存在入口。
金额与归属口径见 backend/agents.py。
"""
import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from backend import agents as agent_service
from backend import codes, models
from backend.api.admin_ops import code_dto
from backend.api.agents_admin import _log_dto, _purchase_dto, _sale_dto
from backend.api.pagination import pagination_params
from backend.api.user import get_current_user
from backend.database import get_db

logger = logging.getLogger(__name__)

agent_center_router = APIRouter(prefix="/api/user/agent", tags=["用户端·代理中心"])


def _require_agent(db: Session, user: models.WebUser) -> models.Agent:
    agent = agent_service.get_active_agent(db, user.id)
    if agent is None:
        raise HTTPException(status_code=403, detail="你不是代理")
    return agent


def _agent_profile(agent: models.Agent) -> dict:
    level = agent.level
    return {
        "id": agent.id,
        "level_id": agent.level_id,
        "level_name": level.name if level else "",
        "discount_rate": float(level.discount_rate) if level else 0,
        "balance": float(agent_service.money(agent.balance or 0)),
        "is_active": bool(agent.is_active),
    }


# ==================== 身份 ====================

@agent_center_router.get("/me")
def agent_me(
    current_user: models.WebUser = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """当前用户是不是代理：前端「我的」据此决定要不要显示代理中心入口。"""
    agent = agent_service.get_active_agent(db, current_user.id)
    if agent is None:
        return {"is_agent": False, "agent": None}
    return {"is_agent": True, "agent": _agent_profile(agent)}


# ==================== 进货 ====================

@agent_center_router.get("/products")
def list_products(
    current_user: models.WebUser = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """可进货商品 = 在售订阅套餐，带本等级的进货单价。"""
    agent = _require_agent(db, current_user)
    level = agent.level
    rows = db.query(models.SubscriptionPlan).filter(
        models.SubscriptionPlan.is_active.is_(True)
    ).order_by(models.SubscriptionPlan.sort_order,
               models.SubscriptionPlan.id).all()
    products = []
    for plan in rows:
        face = agent_service.money(plan.price)
        if face <= 0:
            continue
        products.append({
            "plan_id": plan.id,
            "name": plan.name,
            "description": plan.description or "",
            "duration_days": plan.duration_days,
            "realm_id": plan.realm_id,
            "face_value": float(face),
            "unit_cost": float(agent_service.discounted_unit_price(
                face, level.discount_rate)),
            "discount_rate": float(level.discount_rate),
        })
    return {"products": products}


class PurchaseRequest(BaseModel):
    plan_id: int
    code_type: int = Field(default=codes.CODE_TYPE_REGISTER)
    quantity: int = Field(default=1, ge=1, le=agent_service.MAX_PURCHASE_QUANTITY)
    note: str = ""


@agent_center_router.post("/purchase")
def purchase(
    request: PurchaseRequest,
    current_user: models.WebUser = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """进货：扣余额 + 生成卡码，同一事务，失败整单回滚。"""
    agent = _require_agent(db, current_user)
    plan = db.query(models.SubscriptionPlan).filter(
        models.SubscriptionPlan.id == request.plan_id).first()
    if not plan:
        raise HTTPException(status_code=404, detail="套餐不存在")
    try:
        purchase_row, created = agent_service.purchase_codes(
            db, agent, plan, code_type=request.code_type,
            quantity=request.quantity, note=request.note)
    except agent_service.AgentError as exc:
        db.rollback()
        raise HTTPException(status_code=400, detail=str(exc))
    db.commit()
    return {
        "success": True,
        "purchase": _purchase_dto(purchase_row),
        "codes": [
            {"id": c.id, "code": c.code,
             "days_text": codes.format_days(c.days),
             "expires_at": c.expires_at.isoformat() if c.expires_at else None}
            for c in created
        ],
    }


# ==================== 我的数据 ====================

@agent_center_router.get("/codes")
def my_codes(
    limit: int = 50,
    offset: int = 0,
    state: Optional[str] = None,
    current_user: models.WebUser = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    agent = _require_agent(db, current_user)
    limit, offset = pagination_params(limit, offset)
    query = db.query(models.RegistrationCode).filter(
        models.RegistrationCode.agent_id == agent.id).order_by(
        models.RegistrationCode.id.desc())
    total = query.count()
    rows = query.offset(offset).limit(limit).all()
    items = [code_dto(db, r) for r in rows]
    if state:
        items = [i for i in items if i["state"] == state]
    return {"total": total, "codes": items}


@agent_center_router.get("/ledger")
def my_ledger(
    limit: int = 50,
    offset: int = 0,
    current_user: models.WebUser = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    agent = _require_agent(db, current_user)
    limit, offset = pagination_params(limit, offset)
    query = db.query(models.AgentBalanceLog).filter(
        models.AgentBalanceLog.agent_id == agent.id).order_by(
        models.AgentBalanceLog.id.desc())
    total = query.count()
    rows = query.offset(offset).limit(limit).all()
    return {"total": total, "logs": [_log_dto(r) for r in rows]}


@agent_center_router.get("/purchases")
def my_purchases(
    limit: int = 50,
    offset: int = 0,
    current_user: models.WebUser = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    agent = _require_agent(db, current_user)
    limit, offset = pagination_params(limit, offset)
    query = db.query(models.AgentPurchase).filter(
        models.AgentPurchase.agent_id == agent.id).order_by(
        models.AgentPurchase.id.desc())
    total = query.count()
    rows = query.offset(offset).limit(limit).all()
    return {"total": total, "purchases": [_purchase_dto(r) for r in rows]}


@agent_center_router.get("/sales")
def my_sales(
    limit: int = 50,
    offset: int = 0,
    current_user: models.WebUser = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    agent = _require_agent(db, current_user)
    limit, offset = pagination_params(limit, offset)
    query = db.query(models.AgentSale).filter(
        models.AgentSale.agent_id == agent.id).order_by(
        models.AgentSale.id.desc())
    total = query.count()
    rows = query.offset(offset).limit(limit).all()
    return {"total": total, "sales": [_sale_dto(db, r) for r in rows]}


@agent_center_router.get("/stats")
def my_stats(
    current_user: models.WebUser = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """我的销售统计：汇总 + 近 30 天按日 + 近 12 个月按月。"""
    agent = _require_agent(db, current_user)
    summary = agent_service.agent_summary(db, agent.id)
    buckets = agent_service.sales_buckets(db, agent.id)
    return {"summary": summary, "daily": buckets["daily"],
            "monthly": buckets["monthly"]}
