"""代理分销服务层（2026-11-06 用户亲点完整版：分级 / 折扣 / 进货 / 销售统计）。

口径（钉死，不许漂移）：

- **等级是数据不是代码**：等级名、进货折扣率、排序、启停全在
  ``agent_levels`` 表里由管理员维护，本模块不写死任何等级与折扣。
- **代理身份靠指定**：``agents`` 表里有行且 ``is_active`` 才算代理；
  没有被指定的用户完全无感（用户端入口不出现、行为零变化）。
- **余额是独立账本**：进货余额只走 ``agents.balance`` +
  ``agent_balance_logs``（只增不改），不与积分钱包
  （``web_users.points`` / ``points_logs``）混账。余额来源 v1 只有
  管理员手工增减（带备注、留痕）。
- **进货商品 = 现有订阅套餐**：面值 = 套餐人民币价 ``SubscriptionPlan.price``；
  进货单价 = 面值 × 等级折扣率，**按分四舍五入**（``ROUND_HALF_UP``
  到 2 位小数，与全站金额列 ``Numeric(10, 2)`` 的口径一致，见
  :func:`discounted_unit_price`）。生成的码就是现有卡码
  （``RegistrationCode``，``source='agent'``，天数/归属服取套餐），
  核销仍走 ``codes.redeem_code`` 单口。
- **账目安全**：余额扣减走条件 UPDATE（``balance >= amount`` 才扣，
  与 ``codes.claim_code`` 的原子占位同一套写法），并发下扣不动就
  整单拒绝，绝不扣成负数；扣款、进货单、卡码、流水在同一事务里，
  调用方统一提交，中间崩溃一起回滚。
- **销售归属**：代理码核销成功时由 ``codes.redeem_code`` 在同一事务里
  调 :func:`record_sale` 落 ``agent_sales``；金额取进货单快照，
  之后改套餐价/等级折扣不影响历史口径。
"""
import logging
from datetime import datetime, timedelta
from decimal import Decimal, ROUND_HALF_UP
from typing import Optional

from sqlalchemy import func
from sqlalchemy.orm import Session

from backend import codes, models, realms

logger = logging.getLogger(__name__)

CENT = Decimal("0.01")
# 代理可进货的卡码类型：注册码 / 续期码。白名单码是身份特权、不作商品。
PURCHASABLE_CODE_TYPES = (codes.CODE_TYPE_REGISTER, codes.CODE_TYPE_RENEW)
MAX_PURCHASE_QUANTITY = 100


class AgentError(Exception):
    """业务拒绝（余额不足 / 代理或等级已停用 / 商品不可进货等），API 层转 400。"""


class InsufficientBalance(AgentError):
    """余额不足：整单拒绝，不生成任何码、不留进货流水。"""


def money(value) -> Decimal:
    """金额归一到分（ROUND_HALF_UP 2 位小数），与全站金额列口径一致。"""
    return Decimal(str(value)).quantize(CENT, rounding=ROUND_HALF_UP)


def discounted_unit_price(face_value, discount_rate) -> Decimal:
    """进货单价 = 面值 × 等级折扣率，按分四舍五入。

    例：面值 33.33、五折 → 16.665 → 16.67（HALF_UP，不是银行家舍入）。
    """
    return money(Decimal(str(face_value)) * Decimal(str(discount_rate)))


# ==================== 查询 ====================

def get_agent(db: Session, agent_id: int) -> Optional[models.Agent]:
    return db.query(models.Agent).filter(models.Agent.id == agent_id).first()


def get_agent_by_user(db: Session, user_id: int) -> Optional[models.Agent]:
    return db.query(models.Agent).filter(
        models.Agent.user_id == user_id).first()


def get_active_agent(db: Session, user_id: int) -> Optional[models.Agent]:
    """当前生效的代理身份；没指定或已被取消都返回 None（用户无感）。"""
    agent = get_agent_by_user(db, user_id)
    if agent is None or not agent.is_active:
        return None
    return agent


# ==================== 余额账本 ====================

def _fresh_balance(db: Session, agent_id: int) -> Decimal:
    db.expire_all()
    row = db.query(models.Agent.balance).filter(
        models.Agent.id == agent_id).first()
    return money(row[0]) if row else Decimal("0")


def adjust_balance(db: Session, agent: models.Agent, amount,
                   note: str = "", operator_id: Optional[int] = None
                   ) -> models.AgentBalanceLog:
    """管理员手工增减余额（带备注留痕）。不提交，调用方统一提交。

    扣减走条件 UPDATE：余额不足以扣时抛 InsufficientBalance，余额绝不为负。
    """
    amount = money(amount)
    if amount == 0:
        raise AgentError("调整金额不能为 0")
    if amount > 0:
        updated = (
            db.query(models.Agent)
            .filter(models.Agent.id == agent.id)
            .update({models.Agent.balance:
                     models.Agent.balance + amount},
                    synchronize_session=False)
        )
    else:
        updated = (
            db.query(models.Agent)
            .filter(models.Agent.id == agent.id,
                    models.Agent.balance >= -amount)
            .update({models.Agent.balance:
                     models.Agent.balance + amount},
                    synchronize_session=False)
        )
    if not updated:
        raise InsufficientBalance("代理余额不足，无法扣减")
    balance_after = _fresh_balance(db, agent.id)
    log = models.AgentBalanceLog(
        agent_id=agent.id, kind="admin_adjust", amount=amount,
        balance_after=balance_after, note=note or None,
        operator_id=operator_id)
    db.add(log)
    db.flush()
    return log


def _debit_for_purchase(db: Session, agent: models.Agent, total: Decimal) -> None:
    """进货扣款：条件 UPDATE 原子扣减，扣不动抛 InsufficientBalance。"""
    updated = (
        db.query(models.Agent)
        .filter(models.Agent.id == agent.id,
                models.Agent.balance >= total)
        .update({models.Agent.balance: models.Agent.balance - total},
                synchronize_session=False)
    )
    if not updated:
        raise InsufficientBalance(
            f"进货余额不足：本次进货需 {total}，请联系管理员补充余额")


# ==================== 进货发码 ====================

def purchase_codes(db: Session, agent: models.Agent,
                   plan: models.SubscriptionPlan, code_type: int,
                   quantity: int, note: str = ""):
    """代理进货：按本等级折扣价扣余额，生成一批现有卡码。不提交。

    返回 (AgentPurchase, [RegistrationCode])。任何一步失败都抛
    AgentError/InsufficientBalance，由调用方回滚整单——不会出现
    「扣了钱没码」或「有码没扣钱」。
    """
    if not agent.is_active:
        raise AgentError("代理身份已取消，不能进货")
    level = agent.level
    if level is None or not level.is_active:
        raise AgentError("代理等级已停用，不能进货，请联系管理员")
    if not plan.is_active:
        raise AgentError("该套餐已停售，不能进货")
    if code_type not in PURCHASABLE_CODE_TYPES:
        raise AgentError("代理只能进货注册码或续期码")
    if quantity < 1 or quantity > MAX_PURCHASE_QUANTITY:
        raise AgentError(f"单次进货数量须在 1-{MAX_PURCHASE_QUANTITY} 之间")

    face_value = money(plan.price)
    if face_value <= 0:
        raise AgentError("该套餐未定价，不能进货")
    unit_cost = discounted_unit_price(face_value, level.discount_rate)
    total_cost = money(unit_cost * quantity)

    target_realm = realms.claim(db, plan.realm_id)
    if not realms.get_realm(db, target_realm):
        raise AgentError(f"套餐归属服不存在: #{target_realm}")

    _debit_for_purchase(db, agent, total_cost)

    purchase = models.AgentPurchase(
        agent_id=agent.id, plan_id=plan.id, plan_name=plan.name,
        realm_id=target_realm, code_type=code_type, quantity=quantity,
        face_value=face_value, discount_rate=level.discount_rate,
        unit_cost=unit_cost, total_cost=total_cost)
    db.add(purchase)
    db.flush()

    days = plan.duration_days
    created = []
    for index in range(1, quantity + 1):
        raw = ""
        for _attempt in range(20):
            raw = codes.render_code(code_type=code_type, days=days, index=index)
            if not db.query(models.RegistrationCode).filter(
                models.RegistrationCode.code == raw
            ).first():
                break
        else:
            raise AgentError("卡码生成冲突，请重试")
        code = models.RegistrationCode(
            code=raw, max_uses=1, use_count=0, is_active=True,
            note=note or None, expires_at=None,
            created_by=agent.user_id, code_type=code_type, days=days,
            source="agent", realm_id=target_realm,
            reward_type="subscription",
            agent_id=agent.id, agent_purchase_id=purchase.id)
        db.add(code)
        created.append(code)
    db.flush()
    for c in created:
        db.refresh(c)

    balance_after = _fresh_balance(db, agent.id)
    db.add(models.AgentBalanceLog(
        agent_id=agent.id, kind="purchase", amount=-total_cost,
        balance_after=balance_after,
        note=f"进货 {plan.name} × {quantity}",
        operator_id=agent.user_id, ref_id=purchase.id))
    db.flush()
    return purchase, created


# ==================== 销售归属 ====================

def record_sale(db: Session, code: models.RegistrationCode,
                buyer_user_id: int) -> Optional[models.AgentSale]:
    """代理码核销成功时落销售归属（由 codes.redeem_code 在核销事务里调用）。

    非代理码直接返回 None。金额取进货单快照；进货单缺失（不应发生）
    时按 0 记并留日志，不许拖垮核销主流程之外的其它路径。
    """
    agent_id = getattr(code, "agent_id", None)
    if not agent_id:
        return None
    purchase = None
    if getattr(code, "agent_purchase_id", None):
        purchase = db.query(models.AgentPurchase).filter(
            models.AgentPurchase.id == code.agent_purchase_id).first()
    if purchase is None:
        logger.warning(
            "代理码 %s 核销时找不到进货单（agent_id=%s），销售归属按 0 记",
            code.code, agent_id)
    sale = models.AgentSale(
        agent_id=agent_id, code_id=code.id,
        purchase_id=purchase.id if purchase else None,
        buyer_user_id=buyer_user_id,
        face_value=purchase.face_value if purchase else Decimal("0"),
        unit_cost=purchase.unit_cost if purchase else Decimal("0"))
    db.add(sale)
    db.flush()
    return sale


# ==================== 统计 ====================

def agent_summary(db: Session, agent_id: int) -> dict:
    """单个代理的进货/销售汇总（管理端列表与代理中心共用）。"""
    purchase = db.query(
        func.coalesce(func.sum(models.AgentPurchase.quantity), 0),
        func.coalesce(func.sum(models.AgentPurchase.total_cost), 0),
    ).filter(models.AgentPurchase.agent_id == agent_id).first()
    sales = db.query(
        func.count(models.AgentSale.id),
        func.coalesce(func.sum(models.AgentSale.face_value), 0),
        func.coalesce(func.sum(models.AgentSale.unit_cost), 0),
    ).filter(models.AgentSale.agent_id == agent_id).first()
    return {
        "purchase_quantity": int(purchase[0] or 0),
        "purchase_cost_total": float(money(purchase[1] or 0)),
        "sales_count": int(sales[0] or 0),
        "sales_face_total": float(money(sales[1] or 0)),
        "sales_cost_total": float(money(sales[2] or 0)),
    }


def sales_buckets(db: Session, agent_id: Optional[int] = None) -> dict:
    """销售按日/月聚合：近 30 天按日、近 12 个月按月，空档补 0。

    销量量级小，全量取行后在 Python 里分桶，SQLite/PG 口径一致。
    agent_id=None 为全站（管理端全局统计）。
    """
    query = db.query(models.AgentSale.created_at, models.AgentSale.face_value)
    if agent_id is not None:
        query = query.filter(models.AgentSale.agent_id == agent_id)
    rows = query.all()

    now = datetime.now()
    daily = {}
    for i in range(29, -1, -1):
        day = (now - timedelta(days=i)).strftime("%Y-%m-%d")
        daily[day] = {"period": day, "count": 0, "face_total": Decimal("0")}
    monthly = {}
    year, month = now.year, now.month
    for _ in range(12):
        key = f"{year:04d}-{month:02d}"
        monthly[key] = {"period": key, "count": 0, "face_total": Decimal("0")}
        month -= 1
        if month == 0:
            year, month = year - 1, 12

    for created_at, face_value in rows:
        if created_at is None:
            continue
        day_key = created_at.strftime("%Y-%m-%d")
        if day_key in daily:
            daily[day_key]["count"] += 1
            daily[day_key]["face_total"] += Decimal(str(face_value or 0))
        month_key = created_at.strftime("%Y-%m")
        if month_key in monthly:
            monthly[month_key]["count"] += 1
            monthly[month_key]["face_total"] += Decimal(str(face_value or 0))

    def _out(buckets):
        return [
            {"period": b["period"], "count": b["count"],
             "face_total": float(money(b["face_total"]))}
            for b in buckets.values()
        ]

    return {"daily": _out(daily), "monthly": _out(monthly)}
