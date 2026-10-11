"""优惠券（购买时抵扣）的核心逻辑

与兑换码（`ExchangeCode`）的分工必须先说清，否则两套东西会搅在一起：

- **兑换码**：不花钱直接拿东西（核销即完成，不走支付网关）；
- **优惠券**：付费时打折/减钱，钱**仍然走支付网关**，只是单价变了。
  所以优惠券只影响「下单时的金额」，不影响履约、退款与财务流水的口径。

**额度是预订制的**，这是本模块最关键的一条：

1. 下单 → 占额度（``CouponUsage.status='reserved'``，`coupon_codes.use_count` 原子 +1）；
2. 支付成功履约 → ``consumed``；
3. 订单被关闭 / 退款 → ``released``，额度还回去。

只在付款成功时计数会让一串未支付订单同时绕过限额，付完款全都拿到折扣；
而「关单/退款不还额度」又会让码被白白烧掉。总额度用**条件 UPDATE** 抢占
（`use_count < max_uses` 才 +1），**单人限领**用条件 `INSERT ... SELECT` 抢占
（该人有效核销数 < ``per_user_limit`` 才写入），并发下单两处都超不了上限。

价格一律用 `Decimal` 算、最后保留两位：钱不能落进浮点数。
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta
from decimal import Decimal, ROUND_HALF_UP
from typing import Optional

from fastapi import HTTPException
from sqlalchemy import func, insert, literal, select
from sqlalchemy.orm import Session

from backend import models

logger = logging.getLogger(__name__)

CONFIG_ENABLED = "coupon_enabled"
# 预订多久没付款就自动关单并还额度（小时，0 = 不自动清理）
CONFIG_RESERVE_HOURS = "coupon_reserve_hours"
DEFAULT_RESERVE_HOURS = 24
# 一轮最多清理多少条：后台线程要能随时收工，不把大表锁住（与扫描/维护同一口径）
SWEEP_BATCH = 500

# 折扣类型
PERCENT = "percent"
FIXED = "fixed"

# 核销状态
RESERVED = "reserved"
CONSUMED = "consumed"
RELEASED = "released"
ACTIVE_STATUSES = (RESERVED, CONSUMED)


def _money(value) -> Decimal:
    """统一成两位小数的金额（钱不能落进浮点）"""
    if value is None:
        return Decimal("0.00")
    return Decimal(str(value)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def enabled(db: Session) -> bool:
    """优惠券功能是否开启（缺省开启：没配过就是开）"""
    row = db.query(models.SystemConfig).filter(
        models.SystemConfig.key == CONFIG_ENABLED).first()
    if row is None or row.value is None or str(row.value).strip() == "":
        return True
    return str(row.value).strip().lower() == "true"


def reserve_hours(db: Session) -> int:
    """预订自动清理的时限（小时）；未配置/写错时回落 24，写 0 表示不自动清理"""
    row = db.query(models.SystemConfig).filter(
        models.SystemConfig.key == CONFIG_RESERVE_HOURS).first()
    if row is None or row.value is None or str(row.value).strip() == "":
        return DEFAULT_RESERVE_HOURS
    try:
        return max(0, int(str(row.value).strip()))
    except (TypeError, ValueError):
        return DEFAULT_RESERVE_HOURS


def active_use_count(db: Session, coupon: models.CouponCode) -> int:
    """当前占用的额度（已预订 + 已消费），比 `use_count` 更权威：校验用它"""
    return db.query(models.CouponUsage).filter(
        models.CouponUsage.coupon_id == coupon.id,
        models.CouponUsage.status.in_(ACTIVE_STATUSES),
    ).count()


def user_use_count(db: Session, coupon_id: int, user_id: int) -> int:
    """这个用户在该券上已占用的额度（每人限用的判定依据）"""
    return db.query(models.CouponUsage).filter(
        models.CouponUsage.coupon_id == coupon_id,
        models.CouponUsage.user_id == user_id,
        models.CouponUsage.status.in_(ACTIVE_STATUSES),
    ).count()


def resolve_item(db: Session, kind: str, item_id: int) -> tuple[Decimal, Optional[int], str]:
    """把 (kind, item_id) 解析成 (原价, 适用服, 名称) —— 预览与下单必须同一套口径"""
    if kind == "subscription":
        plan = db.query(models.SubscriptionPlan).filter(
            models.SubscriptionPlan.id == item_id,
            models.SubscriptionPlan.is_active == True,  # noqa: E712
        ).first()
        if not plan:
            raise HTTPException(status_code=404, detail="套餐不存在")
        return _money(plan.price), plan.realm_id, plan.name
    if kind == "recharge":
        package = db.query(models.RechargePackage).filter(
            models.RechargePackage.id == item_id,
            models.RechargePackage.is_active == True,  # noqa: E712
        ).first()
        if not package:
            raise HTTPException(status_code=404, detail="充值套餐不存在")
        return _money(package.price), None, package.name
    raise HTTPException(status_code=400, detail="kind 必须是 recharge 或 subscription")


def _invalid(message: str) -> HTTPException:
    return HTTPException(status_code=400, detail=message)


def validate(db: Session, coupon: models.CouponCode, *, user: models.WebUser,
             kind: str, realm_id: Optional[int], amount: Decimal,
             now: Optional[datetime] = None) -> None:
    """校验这张券能不能用于这次购买，不能则抛 400 并说明原因（文案直接给用户看）"""
    now = now or datetime.now()

    if not coupon.is_active:
        raise _invalid("该优惠券已停用")
    if coupon.valid_from and now < coupon.valid_from:
        raise _invalid(f"该优惠券将于 {coupon.valid_from.strftime('%Y-%m-%d %H:%M')} 开始可用")
    if coupon.valid_until and now > coupon.valid_until:
        raise _invalid("该优惠券已过期")

    allowed_kinds = ("subscription", "recharge") if coupon.kind in ("all", None) else (coupon.kind,)
    if kind not in allowed_kinds:
        raise _invalid("该优惠券不适用于此商品" if coupon.kind != "all" else "券类型无效")

    if coupon.realm_id is not None:
        # 指定了服的券只用于该服的会员购买：充值是全站的，跨服会把折扣用错地方
        if kind != "subscription" or realm_id != coupon.realm_id:
            raise _invalid("该优惠券仅适用于指定服的会员购买")

    if coupon.min_amount and amount < _money(coupon.min_amount):
        raise _invalid(f"该券满 {_money(coupon.min_amount)} 元可用，当前金额不足")

    if coupon.per_user_limit and user_use_count(db, coupon.id, user.id) >= coupon.per_user_limit:
        raise _invalid(f"该券每人限用 {coupon.per_user_limit} 次，你已用完")

    used = active_use_count(db, coupon)
    if coupon.max_uses and used >= coupon.max_uses:
        raise _invalid("该优惠券已被领用完")


def compute(amount: Decimal, coupon: models.CouponCode) -> tuple[Decimal, Decimal]:
    """算出 (优惠金额, 实付金额)。金额一律 Decimal、两位小数，不低于 0。

    ``percent`` 的 ``value`` 是**实付百分比**（85 = 八五折、实付 85%），
    所以优惠额 = 原价 × (100 - value) / 100；不是直接把 value 当折扣额。
    """
    discount = Decimal("0.00")
    if coupon.discount_type == FIXED:
        discount = _money(coupon.value)
    else:
        percent = Decimal(str(max(0, min(100, int(coupon.value or 0)))))
        discount = _money(amount * (Decimal("100") - percent) / Decimal("100"))
        if coupon.max_discount and discount > _money(coupon.max_discount):
            discount = _money(coupon.max_discount)

    if discount > amount:
        discount = amount
    paid = _money(amount - discount)
    return _money(discount), paid


def quote(db: Session, *, user: models.WebUser, code: str, kind: str, item_id: int,
          now: Optional[datetime] = None) -> dict:
    """预览：给定优惠码与商品，返回能不能用、优惠多少、实付多少

    下单前调用（用户端展示「已优惠 ¥X」），下单时也走这里——两处口径必须一致，
    否则会出现「预览 8 折、实付全价」这类信任问题。
    """
    if not enabled(db):
        raise _invalid("优惠券功能未开启")
    text = (code or "").strip()
    if not text:
        raise _invalid("请填写优惠码")

    coupon = db.query(models.CouponCode).filter(
        models.CouponCode.code == text.strip().upper()).first()
    if coupon is None:
        raise _invalid("优惠码不存在")

    list_price, realm_id, item_name = resolve_item(db, kind, item_id)
    validate(db, coupon, user=user, kind=kind, realm_id=realm_id,
             amount=list_price, now=now)
    discount, paid = compute(list_price, coupon)
    return {
        "valid": True,
        "code": coupon.code,
        "coupon_id": coupon.id,
        "item_name": item_name,
        "list_price": float(list_price),
        "discount_amount": float(discount),
        "paid_amount": float(paid),
        "discount_type": coupon.discount_type,
        "value": coupon.value,
    }


def reserve(db: Session, *, coupon: models.CouponCode, user: models.WebUser,
            order_id: str, kind: str, list_price: Decimal,
            discount: Decimal, paid: Decimal) -> models.CouponUsage:
    """占额度：**总限额**与**单人限领**都用「条件写入」原子抢占，并发也超不了

    旧实现只把总限额做成了条件 UPDATE，单人限领仍是「先查计数再插入」：同一用户的
    两个请求同时走到这里都会读到「还没领满」，于是各领一张，绕过每人限领（P2 的 TOCTOU）。
    现在先按条件 ``INSERT ... SELECT`` 占单人名额（该人有效核销数 < per_user_limit 才写入，
    拿到 0 行就是被别人先一步领满），再按条件 UPDATE 占总额度。两步都在调用方的事务里，
    任一步抢不到就抛 400，整笔下单一起回滚——不会留下「占了额度却没订单」的半张核销记录。

    成功后返回核销记录；抢不到说明已被自己或别人用完。

    注意：下面的「条件 INSERT ... SELECT」单独看在 PostgreSQL READ COMMITTED 下
    **不是**原子的——两个并发事务会同时看到 count < limit 而都插入成功（已用
    tests/test_coupons_reserve_race.py 复现）。所以入口先对券行加
    ``with_for_update()`` 行锁，把同一张券的所有占位串行化，锁内的 count 才可信；
    锁也顺带让后面的总额度条件 UPDATE 不用跟别的占位事务抢行锁。
    （economy.py 里积分链尾 / 订阅发放的同类竞态也是这么修的。）
    """
    locked = (
        db.query(models.CouponCode)
        .filter(models.CouponCode.id == coupon.id)
        .with_for_update()
        .first()
    )
    if locked is None:
        raise _invalid("优惠码不存在")
    coupon = locked  # 后续都用锁住的这一行：限额/开关是锁内读到的最新值

    now = datetime.now()
    per_user_limit = int(coupon.per_user_limit or 0)
    if per_user_limit > 0:
        mine = (
            select(func.count())
            .select_from(models.CouponUsage)
            .where(
                models.CouponUsage.coupon_id == coupon.id,
                models.CouponUsage.user_id == user.id,
                models.CouponUsage.status.in_(ACTIVE_STATUSES),
            )
            .scalar_subquery()
        )
        values = {
            "coupon_id": coupon.id,
            "user_id": user.id,
            "order_id": order_id,
            "kind": kind,
            "status": RESERVED,
            "list_price": list_price,
            "discount_amount": discount,
            "paid_amount": paid,
            "created_at": now,
        }
        columns = list(values)
        claimed = db.execute(
            insert(models.CouponUsage).from_select(
                columns,
                select(*[literal(values[name]) for name in columns]).where(mine < per_user_limit),
            )
        ).rowcount
        if not claimed:
            raise _invalid(f"该券每人限用 {per_user_limit} 次，你已用完")
        usage = db.query(models.CouponUsage).filter(
            models.CouponUsage.order_id == order_id
        ).first()
        if usage is None:  # pragma: no cover - 插入成功却读不到：数据层异常，宁可 500
            raise HTTPException(status_code=500, detail="优惠券核销记录写入异常，请重试")
    else:
        usage = models.CouponUsage(
            coupon_id=coupon.id, user_id=user.id, order_id=order_id, kind=kind,
            status=RESERVED, list_price=list_price, discount_amount=discount,
            paid_amount=paid, created_at=now,
        )
        db.add(usage)
        db.flush()

    query = db.query(models.CouponCode).filter(models.CouponCode.id == coupon.id)
    if coupon.max_uses:
        query = query.filter(models.CouponCode.use_count < int(coupon.max_uses))
    updated = query.update({models.CouponCode.use_count:
                            models.CouponCode.use_count + 1}, synchronize_session=False)
    if not updated:
        raise _invalid("该优惠券已被领用完")
    return usage


def consume(db: Session, usage_id: Optional[int]) -> None:
    """支付成功履约：预订 → 已消费（额度不动，仍占用）"""
    if not usage_id:
        return
    db.query(models.CouponUsage).filter(
        models.CouponUsage.id == usage_id,
        models.CouponUsage.status == RESERVED,
    ).update({"status": CONSUMED, "closed_at": None}, synchronize_session=False)


def sweep_stale_reservations(db: Session, hours: Optional[int] = None) -> dict:
    """清理超时未支付的预订（额度不能被一张永远不付款的订单白占着）

    预订制的好处是能防超发，代价是**未支付订单会占着额度**。用户点一下「下单」
    却不去付款（或者支付页关了）时，额度不该永久减一。这里按对账口径收尾：

    - 订单仍是 `pending` → 一并关单（超时未支付自动作废，与手动关单同构），再还额度；
    - 订单其实已 `paid`（履约时漏了 consume 之类的历史脏数据）→ 补成 `consumed`；
    - 订单已经被关/被退（正常路径已经还过额度）→ 这里再还一次是幂等的，不会多还；
    - 订单不存在（人为删过数据）→ 还额度。

    只处理到期的预订，且限制单轮批量：后台线程每小时跑一轮，不抢前台锁。
    """
    hours = reserve_hours(db) if hours is None else max(0, int(hours))
    summary = {"enabled": bool(hours), "scanned": 0, "closed_orders": 0,
               "consumed": 0, "released": 0}
    if not hours:
        return summary

    cutoff = datetime.now() - timedelta(hours=hours)
    stale = db.query(models.CouponUsage).filter(
        models.CouponUsage.status == RESERVED,
        models.CouponUsage.created_at < cutoff,
    ).order_by(models.CouponUsage.id.asc()).limit(SWEEP_BATCH).all()

    now = datetime.now()
    for usage in stale:
        summary["scanned"] += 1
        order = db.query(models.RechargeOrder).filter(
            models.RechargeOrder.order_id == usage.order_id).first()
        if order is None:
            order = db.query(models.SubscriptionOrder).filter(
                models.SubscriptionOrder.order_id == usage.order_id).first()

        if order is None:
            release(db, usage.id)
            summary["released"] += 1
        elif order.status == "pending":
            # 条件 UPDATE：只有库里仍是 pending 才关单，避免覆盖并发支付回调刚写入的 paid
            closed = db.query(type(order)).filter(
                type(order).order_id == order.order_id,
                type(order).status == "pending",
            ).update({"status": "closed", "closed_at": now}, synchronize_session=False)
            if closed:
                release(db, usage.id)
                summary["closed_orders"] += 1
                summary["released"] += 1
            else:
                # 0 行说明状态已被并发改动（如支付回调刚置为 paid），刷新后按新状态收尾
                db.refresh(order)
                if order.status == "paid":
                    consume(db, usage.id)
                    summary["consumed"] += 1
                else:  # closed / refunded：正常路径已释放，再走一次是幂等的
                    release(db, usage.id)
                    summary["released"] += 1
        elif order.status == "paid":
            consume(db, usage.id)
            summary["consumed"] += 1
        else:  # refunded / closed：正常路径已释放，再走一次是幂等的
            release(db, usage.id)
            summary["released"] += 1

    if summary["scanned"]:
        db.flush()
    return summary


def release(db: Session, usage_id: Optional[int]) -> None:
    """订单关闭 / 退款：还额度（只还一次，重复调用不会多还）"""
    if not usage_id:
        return
    updated = db.query(models.CouponUsage).filter(
        models.CouponUsage.id == usage_id,
        models.CouponUsage.status.in_(ACTIVE_STATUSES),
    ).update({"status": RELEASED, "closed_at": datetime.now()}, synchronize_session=False)
    if not updated:
        return
    usage = db.query(models.CouponUsage).filter(models.CouponUsage.id == usage_id).first()
    if usage is None:
        return
    # 额度还回去，但不能掉到负数（老数据/手工改过时兜底）
    db.query(models.CouponCode).filter(
        models.CouponCode.id == usage.coupon_id,
        models.CouponCode.use_count > 0,
    ).update({models.CouponCode.use_count: models.CouponCode.use_count - 1},
             synchronize_session=False)


__all__ = [
    "ACTIVE_STATUSES", "CONFIG_ENABLED", "CONFIG_RESERVE_HOURS", "CONSUMED", "DEFAULT_RESERVE_HOURS",
    "FIXED", "PERCENT", "RELEASED", "RESERVED", "active_use_count", "compute", "consume",
    "enabled", "quote", "release", "reserve", "reserve_hours", "resolve_item",
    "sweep_stale_reservations", "user_use_count", "validate",
]
