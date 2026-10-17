"""
管理后台 · 订单关单与退款

补上一个此前完全不存在的运营口子：订单只能「标记已支付」，**不能关、也不能退**。
线下多转了一笔、用户点错套餐、支付回调把用户搞重复了——后台当时一点办法都没有，
只能去改数据库。这里把两件事做成一等公民：

- **关单**：把未支付的订单作废（`pending` → `closed`）。不碰任何权益，用户也没付钱，
  所以不打扰用户（只留审计）。
- **退款**：把已支付的订单冲正（`paid` → `refunded`），并且**按账本精确回滚**：

  1. 充值订单：按 `PointsLog.ref_id = recharge:{order_id}` 找到当时发的积分并扣回；
     同时撤回邀请人拿到的那笔返利（`ref_id = rebate:{order_id}`）——返利也是这笔订单产生的，
     只退买家不退返利，就等于让站点为一次退款付两遍钱。
  2. 订阅订单：按履约时记下的 `subscription_id` / `days_granted` **精确回滚天数**
     （老订单没有这两个字段时，才退回“该用户在该服的生效订阅”这一近似），
     回滚后已过期则把订阅置为 `cancelled`——这是真实撤销，与自然到期不是一回事。

积分的通用原则：**不悄悄把余额扣成负数**。卖家余额不够时默认拒绝退款并回报当前余额，
由管理员显式选择 `allow_negative=true` 才扣（客服场景下有时候就是要先退钱后追账）。

老库自动补列（`refunded_at` / `refund_reason` / `closed_at` / `subscription_id` / `days_granted`），
没有这些列的库不会因为升级而报错。

v2.10.0：关单 / 退款同时**释放优惠券预订的额度**（`coupons.release`）。
券的额度是预订制的（下单就占），如果订单作废/退了而不还额度，码会被白白烧掉。

单独成文件：`backend/api/admin.py` 已经很大，订单相关的新端点集中在这里维护。
鉴权口径完全一致（复用 `get_current_admin` + JWT + `_audit`）。
"""
import logging
from datetime import datetime, timedelta
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel
from sqlalchemy import func
from sqlalchemy.orm import Session

from backend import coupons, models
from backend.api.admin_core import _audit, get_current_admin
from backend.database import get_db
from backend.db_retry import commit_with_retry

logger = logging.getLogger(__name__)

admin_orders_router = APIRouter(prefix="/api/admin", tags=["管理后台·订单"])

# 订单状态：pending 待支付 / paid 已支付 / refunded 已退款 / closed 已关闭
CLOSEABLE_STATUSES = ("pending",)
REFUNDABLE_STATUSES = ("paid",)


class RefundRequest(BaseModel):
    reason: str = ""
    # 是否回滚权益（积分 / 会员天数）。缺省回滚：退款不退权益 = 白送。
    revoke_entitlement: bool = True
    # 余额不够时是否允许扣成负数（客服先退钱后追账的场景）
    allow_negative: bool = False
    # 是否同时撤回邀请人的返利
    reverse_rebate: bool = True


def _find_order(db: Session, order_id: str):
    """按订单号找订单（充值优先，最后回退订阅）"""
    recharge = db.query(models.RechargeOrder).filter(
        models.RechargeOrder.order_id == order_id).first()
    if recharge:
        return recharge, "recharge"
    subscription = db.query(models.SubscriptionOrder).filter(
        models.SubscriptionOrder.order_id == order_id).first()
    if subscription:
        return subscription, "subscription"
    return None, None


def _ledger_entry(db: Session, user_id: int, ref_id: str, type_: str):
    """按账本引用找那笔发放记录（退款要按当时实际发的数冲正，而不是按订单金额重算）"""
    return db.query(models.PointsLog).filter(
        models.PointsLog.user_id == user_id,
        models.PointsLog.ref_id == ref_id,
        models.PointsLog.type == type_,
    ).order_by(models.PointsLog.id.asc()).first()


def _reverse_points(db: Session, user: models.WebUser, amount: int, description: str,
                    ref_id: str, allow_negative: bool) -> int:
    """从用户身上扣回 amount 积分；余额不够且不允许负数时抛 400（并把余额告诉管理员）"""
    from backend.api.economy import _add_points

    amount = abs(int(amount or 0))
    if amount <= 0:
        return 0
    balance = int(user.points or 0)
    if not allow_negative and balance < amount:
        raise HTTPException(
            status_code=400,
            detail=(f"{user.username} 当前余额 {balance} 积分，不足以冲正 {amount} 积分。"
                    "确认要退款请勾选「允许余额为负」（会先退款后追账）。"),
        )
    _add_points(db, user, -amount, "refund", description, ref_id)
    return amount


def _rollback_subscription(db: Session, order: models.SubscriptionOrder,
                           plan: Optional[models.SubscriptionPlan], now: datetime):
    """回滚这笔订单开出的会员天数，返回 (订阅, 回滚天数, 是否已撤销)"""
    days = int(order.days_granted or 0)
    # 履约时没记天数（老订单）：退回套餐当前时长，这是唯一可用的近似
    if days <= 0 and plan is not None:
        days = int(plan.duration_days or 0)

    subscription = None
    if order.subscription_id:
        subscription = db.query(models.UserSubscription).filter(
            models.UserSubscription.id == order.subscription_id).first()
    if subscription is None:
        # 老订单没有 subscription_id：退回到「该用户在该服生效中的订阅」
        target_realm = plan.realm_id if plan is not None else None
        q = db.query(models.UserSubscription).filter(
            models.UserSubscription.user_id == order.user_id,
            models.UserSubscription.end_date > now,
        )
        if target_realm is not None:
            q = q.filter(models.UserSubscription.realm_id == target_realm)
        subscription = q.order_by(models.UserSubscription.end_date.desc()).first()
    if subscription is None or days <= 0:
        return subscription, 0, False

    subscription.end_date = subscription.end_date - timedelta(days=days)
    cancelled = False
    if subscription.end_date <= now:
        # 真实撤销（不是自然到期）：这里应当翻状态，避免"退了款还显示生效中"
        subscription.status = "cancelled"
        cancelled = True
    subscription.updated_at = now
    return subscription, days, cancelled


@admin_orders_router.post("/economy/orders/{order_id}/close")
def close_order(
    order_id: str,
    current_admin: models.WebUser = Depends(get_current_admin),
    db: Session = Depends(get_db),
):
    """关闭未支付订单（作废）：不碰权益，也不打扰用户（他并没有付钱）"""
    order, kind = _find_order(db, order_id)
    if order is None:
        raise HTTPException(status_code=404, detail="订单不存在")
    if order.status not in CLOSEABLE_STATUSES:
        raise HTTPException(
            status_code=400,
            detail=f"只有待支付订单可以关闭（当前状态：{order.status}）；已支付订单请用退款。",
        )

    order.status = "closed"
    order.closed_at = datetime.now()
    # 优惠券用的是预订制额度：订单作废了，把占用的次数还回去（不还就等于把码烧了）
    coupons.release(db, order.coupon_usage_id)
    # 兑换码折扣权益同样是「下单即占」：订单作废后还回未用（与上面优惠券同一道理，
    # 否则用户一次没付成的下单会永久烧掉这张权益）
    _restore_discount_credit(db, order_id)
    db.commit()

    _audit(db, current_admin, "economy_close_order", "order", None,
           {"order_id": order_id, "kind": kind})
    db.commit()
    return {"success": True, "order_id": order_id, "kind": kind, "status": "closed"}


@admin_orders_router.post("/economy/orders/{order_id}/refund")
async def refund_order(
    order_id: str,
    request: RefundRequest,
    current_admin: models.WebUser = Depends(get_current_admin),
    db: Session = Depends(get_db),
):
    """退款：冲正订单并按账本回滚权益（充值扣回积分 / 订阅回滚天数）

    查单 / 原子占位 / 冲正 / 审计整段是同步 SQLAlchemy，下放线程池执行；
    通知要走 WebSocket，必须留在事件循环上 await。
    """
    from backend.api.economy import _add_points  # noqa: F401 — 保持与履约同一套账本写入
    from backend.notifications import AdminEvent, notify_admin_event

    admin_id = current_admin.id

    def _refund() -> dict:
        order, kind = _find_order(db, order_id)
        if order is None:
            raise HTTPException(status_code=404, detail="订单不存在")
        if order.status == "refunded":
            raise HTTPException(status_code=400, detail="该订单已经退款，不能重复退款")
        if order.status not in REFUNDABLE_STATUSES:
            raise HTTPException(
                status_code=400,
                detail=f"只有已支付订单可以退款（当前状态：{order.status}）；未支付订单请用关闭。",
            )

        now = datetime.now()
        user = db.query(models.WebUser).filter(models.WebUser.id == order.user_id).first()
        if user is None:
            raise HTTPException(status_code=404, detail="订单用户不存在")

        result = {
            "success": True, "order_id": order_id, "kind": kind,
            "status": "refunded", "revoked_points": 0, "revoked_days": 0,
            "revoked_xp": 0,
            "rebate_reversed": 0, "cancelled": False, "coupon_released": False,
        }

        # 原子占位：并发点两次「退款」时只有一个请求能拿到这一单。
        # 上面两句状态检查与这里之间隔着整段冲正（扣回积分 / 回滚会员天数，都**不是幂等**的）：
        # 读-改-写会让两个请求都读到 paid，于是各扣一次（双重回滚）。
        # 条件 UPDATE 的 rowcount 就是判据（与 economy._claim_order 同一套写法）——
        # 抢不到的那一边什么都不做，直接告诉前端「已处理」。
        model = models.RechargeOrder if kind == "recharge" else models.SubscriptionOrder
        claimed = (
            db.query(model)
            .filter(model.id == order.id, model.status.in_(tuple(REFUNDABLE_STATUSES)))
            .update({"status": "refunded", "refunded_at": now}, synchronize_session=False)
        )
        if not claimed:
            db.rollback()
            logger.info("退款请求被并发的那一次抢先（订单 %s，%s）", order_id, kind)
            return {**result, "already_processed": True,
                    "message": "该订单已处理（并发退款只有一次生效）"}

        if request.revoke_entitlement:
            if kind == "recharge":
                entry = _ledger_entry(db, user.id, f"recharge:{order_id}", "recharge")
                granted = int(entry.amount) if entry else int(order.amount or 0)
                result["revoked_points"] = _reverse_points(
                    db, user, granted,
                    f"订单退款冲正（订单 {order_id}）", f"refund:{order_id}",
                    request.allow_negative,
                )
                if request.reverse_rebate:
                    result["rebate_reversed"] = _reverse_rebate(db, order_id, request.allow_negative)
            else:
                plan = db.query(models.SubscriptionPlan).filter(
                    models.SubscriptionPlan.id == order.plan_id).first()
                subscription, days, cancelled = _rollback_subscription(db, order, plan, now)
                result["revoked_days"] = days
                result["cancelled"] = cancelled
                if subscription is not None and days > 0:
                    result["subscription_end"] = (
                        subscription.end_date.isoformat() if subscription.end_date else None
                    )
                # P1 修复（审查）：积分购买的订阅订单，退款时只回滚了天数、
                # 当初扣的积分没退（用户积分与会员两失）。按账本查实际扣款退回。
                if (order.payment_method or "") == "points":
                    from backend.api.economy import _add_points
                    spent = _ledger_entry(db, user.id, f"subscription_points:{order_id}",
                                          "subscription_buy")
                    refund_pts = abs(int(spent.amount)) if spent else 0
                    if refund_pts > 0:
                        _add_points(db, user, refund_pts, "refund",
                                    f"订阅退款退回积分（订单 {order_id}）",
                                    f"refund:{order_id}")
                        result["revoked_points"] = -refund_pts

            # 履约时按实付发的会员经验也要撤回（两种 kind 都发），否则「买→退」
            # 可以刷经验升等级。revoke_entitlement=False（只退钱不撤权益）时不撤。
            result["revoked_xp"] = _reverse_xp(db, user, order_id, kind)

        # 状态与时间已由上面那次条件 UPDATE 原子写入，这里让当前事务里的 ORM 对象跟上
        order.status = "refunded"
        order.refunded_at = now
        order.refund_reason = (request.reason or "").strip()[:255]
        if kind == "recharge":
            order.refunded_points = result["revoked_points"]
        # 退款同样释放优惠券额度（用户没付钱买成这笔，次数不该算用掉）
        coupons.release(db, order.coupon_usage_id)
        result["coupon_released"] = bool(order.coupon_usage_id)
        # 兑换码折扣权益同样还回未用（与优惠券同一道理；是否过期由下次选用时判定）
        result["credit_restored"] = _restore_discount_credit(db, order_id)
        commit_with_retry(db, label="订单退款")

        _audit(db, admin_id, "economy_refund_order", "order", None,
               {"order_id": order_id, "kind": kind, "reason": order.refund_reason,
                "revoked_points": result["revoked_points"],
                "revoked_days": result["revoked_days"],
                "revoked_xp": result["revoked_xp"],
                "rebate_reversed": result["rebate_reversed"],
                "coupon_released": result.get("coupon_released", False),
                "allow_negative": request.allow_negative})
        db.commit()
        logger.info("订单退款完成: %s kind=%s %s", order_id, kind, result)

        # 通知要用到的都取成纯值：上面提交过，回读 ORM 属性 = 一次隐式回查
        detail_bits = []
        if result["revoked_points"]:
            detail_bits.append(f"已扣回 {result['revoked_points']} 积分")
        if result["revoked_days"]:
            detail_bits.append(f"已回滚 {result['revoked_days']} 天会员")
        result["_notify"] = {"user_id": user.id, "reason": order.refund_reason or "",
                             "details": detail_bits}
        return result

    info = await run_in_threadpool(_refund)
    notify_ctx = info.pop("_notify", None)
    if notify_ctx is None:
        return info      # 并发抢不到的那一次：订单已被处理，不重复通知

    # 先提交再通知：履约事务未提交时另开会话写站内信会撞 SQLite 写锁，通知会被静默丢掉
    reason_text = f"\n原因：{notify_ctx['reason']}" if notify_ctx["reason"] else ""
    try:
        await notify_admin_event(
            event_type=AdminEvent.ORDER_REFUNDED,
            user_id=notify_ctx["user_id"],
            title="↩️ 订单已退款",
            content=(f"订单 {order_id} 已退款。"
                     + ("；".join(notify_ctx["details"]) + "。" if notify_ctx["details"] else "")
                     + reason_text),
            related_id=None,
        )
    except Exception as exc:  # noqa: BLE001 — 通知失败不影响已完成的退款
        logger.warning("退款通知发送失败（订单 %s）: %s", order_id, exc)

    return info


def _restore_discount_credit(db: Session, order_id: str) -> bool:
    """把这笔订单占用的兑换码折扣权益还回未用（关单/退款时调用）

    兑换码折扣权益是「下单即占」（``create_payment_order`` 里条件 UPDATE 置 used），
    与优惠券预订同一道理：订单没付成（关单）或钱退了（退款），权益不该被烧掉。
    是否过期由下次选用时按 ``expires_at`` 判定，这里只管还。
    """
    restored = (
        db.query(models.ExchangeDiscountCredit)
        .filter(
            models.ExchangeDiscountCredit.used_order_id == order_id,
            models.ExchangeDiscountCredit.status == "used",
        )
        .update({"status": "unused", "used_order_id": None},
                synchronize_session=False)
    )
    return bool(restored)


def _reverse_xp(db: Session, user: models.WebUser, order_id: str, kind: str) -> int:
    """撤回这笔订单履约时发放的会员经验（按账本精确找，不重算）

    充值/订阅履约成功时会按实付金额 1 元 = 1 经验发放（``member_level.add_xp``，
    ref_id = ``recharge:{order_id}`` / ``subscription:{order_id}``）。退款只回滚
    积分/天数而不撤经验，用户就能「买→退→买→退」刷经验升等级、永久拿折扣特权。
    经验钳制在 0，不扣成负数；等级按阈值重算，够格降级就降。
    """
    from backend import member_level as _ml

    prefix = "recharge" if kind == "recharge" else "subscription"
    granted = (
        db.query(func.coalesce(func.sum(models.MemberXpLog.xp_delta), 0))
        .filter(
            models.MemberXpLog.user_id == user.id,
            models.MemberXpLog.ref_id == f"{prefix}:{order_id}",
            models.MemberXpLog.xp_delta > 0,
        )
        .scalar()
    ) or 0
    already = (
        db.query(func.coalesce(func.sum(-models.MemberXpLog.xp_delta), 0))
        .filter(
            models.MemberXpLog.user_id == user.id,
            models.MemberXpLog.ref_id == f"refund:{order_id}",
            models.MemberXpLog.xp_delta < 0,
        )
        .scalar()
    ) or 0
    to_reverse = int(granted) - int(already)
    if to_reverse <= 0:
        return 0
    _ml.add_xp(db, user, -to_reverse, "refund", f"refund:{order_id}")
    logger.info("订单 %s 撤回会员经验 %d", order_id, to_reverse)
    return to_reverse


def _reverse_rebate(db: Session, order_id: str, allow_negative: bool) -> int:
    """撤回这笔充值带给邀请人的返利（按账本引用精确找，不重算比例）"""
    entry = db.query(models.PointsLog).filter(
        models.PointsLog.ref_id == f"rebate:{order_id}",
        models.PointsLog.type == "rebate",
    ).order_by(models.PointsLog.id.asc()).first()
    if entry is None:
        return 0
    inviter = db.query(models.WebUser).filter(models.WebUser.id == entry.user_id).first()
    if inviter is None:
        return 0
    try:
        return _reverse_points(
            db, inviter, int(entry.amount),
            f"订单退款撤回返利（订单 {order_id}）", f"rebate-reversal:{order_id}",
            allow_negative,
        )
    except HTTPException as exc:
        # 邀请人余额不够：不因为"退不了返利"就整单退不了款——主退款照做，返利欠着
        logger.warning("撤回返利失败（订单 %s）: %s", order_id, exc.detail)
        return 0


@admin_orders_router.get("/economy/refunds")
def list_recent_refunds(
    limit: int = 20,
    current_admin: models.WebUser = Depends(get_current_admin),
    db: Session = Depends(get_db),
):
    """最近的退款 / 关单记录（带原因）——对账与客服复盘用

    订单列表里只有状态标签，说不清「为什么退的」；这里把原因和时间一并给出。
    """
    limit = max(1, min(int(limit or 20), 100))
    rows: list[tuple[object, str]] = []
    for model, kind in ((models.RechargeOrder, "recharge"),
                        (models.SubscriptionOrder, "subscription")):
        for o in db.query(model).filter(
            model.status.in_(("refunded", "closed"))
        ).order_by(model.created_at.desc()).limit(limit).all():
            rows.append((o, kind))

    # 订单表不带用户名，这里补上——否则对账要一条条去查用户
    user_ids = {o.user_id for o, _ in rows}
    users = {u.id: u.username for u in db.query(models.WebUser).filter(
        models.WebUser.id.in_(user_ids)).all()} if user_ids else {}

    out = [
        {
            "order_id": o.order_id,
            "kind": kind,
            "status": o.status,
            "username": users.get(o.user_id, f"用户 {o.user_id}"),
            "reason": o.refund_reason or "",
            "at": (o.refunded_at or o.closed_at).isoformat()
                  if (o.refunded_at or o.closed_at) else None,
        }
        for o, kind in rows
    ]
    out.sort(key=lambda x: x["at"] or "", reverse=True)
    return {"records": out[:limit]}


__all__ = ["admin_orders_router"]
