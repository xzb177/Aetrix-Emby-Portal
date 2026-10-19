"""订阅有效性判定与付费墙（全站单一事实来源）

订阅行（UserSubscription.status）不会随时间自动翻转为 expired，
因此「是否会员」统一按 end_date 现算，避免各入口口径不一致。

**一个服一个订阅**：多服部署下，判定必须带上服（``realm_id``）——
甲服的白票不应该能在乙服的 EA 上播放。传入方式：

- 显式参数（EM 管理端 / 用户端按具体服查询时）；
- 否则用**本进程的服**（EA 的进程级解析器，启动时按节点身份安装，见
  ``emby_server/nodes.py`` 与 ``emby_api/main.py``）；
- 都没有时用**当前服**（EM 面板上的门户请求；解析不到服时才退回「不做服过滤」）。

**接入方式（v2.7.0 公益服）**：一个服可以是

- ``paid``（付费服，缺省）：按下面的付费墙判定；
- ``free``（公益服）：**免费开放**，不需要订阅就能看（会员与否不影响播放），
  但默认禁止下载（可用该服的下载策略覆盖）。

做任何与「能不能看」相关的判断都走 ``can_play`` / ``ensure_playback_allowed``，
不要在业务代码里自己写 ``has_active_subscription``——否则公益服会到处漏出付费墙。

付费墙开关：
- SystemConfig["subscription_required"]（布尔，默认 true）是否要求有效订阅才能播放
- SystemConfig["subscription_gate_message"]（字符串，可选）自定义拦截提示文案
"""
from __future__ import annotations

import logging
import uuid
from datetime import datetime, timedelta
from decimal import Decimal, ROUND_HALF_UP
from typing import Callable, Optional

from fastapi import HTTPException
from sqlalchemy import or_
from sqlalchemy.orm import Session, joinedload

from backend import models, realms, vitality as _vitality

logger = logging.getLogger(__name__)

# 进程级「本进程服务哪个服」的解析器（EA 启动时装上）
_realm_resolver: Optional[Callable[[], Optional[int]]] = None


def set_process_realm_resolver(resolver: Optional[Callable[[], Optional[int]]]) -> None:
    """由 EA 启动流程装上一个「本进程的服」解析器（EM 不装，保持跨服口径）"""
    global _realm_resolver
    _realm_resolver = resolver


def process_realm_id() -> Optional[int]:
    """本进程的服（没装解析器或解析不到时返回 None = 不做服过滤）"""
    if _realm_resolver is None:
        return None
    try:
        return _realm_resolver()
    except Exception as exc:  # noqa: BLE001 — 解析失败不能把播放整体拦住
        logger.warning("解析本进程的服失败（按不过滤处理）: %s", exc)
        return None


def resolve_realm_id(realm_id: Optional[int] = None) -> Optional[int]:
    return realm_id if realm_id is not None else process_realm_id()


def _scoped_realm_id(db: Session, realm_id: Optional[int] = None) -> Optional[int]:
    """把「没指定服」解析成具体服：本进程的服 → 面板当前服

    EM（面板）上的用户端请求没有进程级服，此时按面板当前服判定，
    否则公益服/付费墙会因“解析不到服”而退回跨服口径。
    """
    target = resolve_realm_id(realm_id)
    if target is not None:
        return target
    try:
        return realms.active_realm_id(db)
    except Exception as exc:  # noqa: BLE001 — 解析不到服时退回旧口径（不阻断播放）
        logger.warning("解析当前服失败（按不过滤处理）: %s", exc)
        return None


def realm_access_mode(db: Session, realm_id: Optional[int] = None) -> str:
    """该服（默认本进程/当前服）的接入方式：paid / free"""
    return realms.access_mode_of(db, _scoped_realm_id(db, realm_id))


def is_free_realm(db: Session, realm_id: Optional[int] = None) -> bool:
    """这个服是不是公益服（免费开放，不需要订阅）"""
    return realm_access_mode(db, realm_id) == realms.ACCESS_FREE


def realm_access_note(db: Session, realm_id: Optional[int] = None) -> str:
    """该服的规则文案（公益服没写时给缺省提示；付费服为空串）"""
    return realms.access_note_of(db, _scoped_realm_id(db, realm_id))


def view_grant(db: Session, user: models.WebUser,
               realm_id: Optional[int] = None) -> tuple[bool, Optional[models.EmbyViewUnlock]]:
    """用户有没有权限查看某服的 Emby 账号 / 线路（**全站单一事实来源**）

    规则：付费服 = 有效订阅即权限；公益服 = 花积分解锁即权限（``EmbyViewUnlock``）。
    判定一律按 ``end_date`` / ``expires_at`` **现算**，不信行上的 status 字段
    （它不会随时间自动翻转，见本模块开头）。

    ``backend/emby_server/portal.py`` 的账号卡与服卡片都调这里——以前那段逻辑
    私有地写在 portal 里，管理端想回答「这个人到底被授权了什么」只能复制一份，
    两份迟早会漂。
    """
    target = _scoped_realm_id(db, realm_id)
    if realms.is_free_realm(db, target):
        now = datetime.now()
        unlock = (
            db.query(models.EmbyViewUnlock)
            .filter(
                models.EmbyViewUnlock.user_id == user.id,
                models.EmbyViewUnlock.realm_id == target,
                or_(models.EmbyViewUnlock.expires_at.is_(None),
                    models.EmbyViewUnlock.expires_at > now),
            )
            .order_by(models.EmbyViewUnlock.unlocked_at.desc())
            .first()
        )
        return (unlock is not None), unlock
    return has_active_subscription(db, user.id, target), None  # r49: 用 _scoped 口径（此前 resolve_realm_id；现有调用方下等价）

CONFIG_REQUIRED = "subscription_required"
CONFIG_MESSAGE = "subscription_gate_message"
CONFIG_ALLOW_DOWNLOAD = "allow_download"

DEFAULT_GATE_MESSAGE = "需要有效的会员订阅才能播放，请先开通会员"
DOWNLOAD_GATE_MESSAGE = "当前站点已关闭下载，仅支持在线观看"
FREE_REALM_DOWNLOAD_MESSAGE = realms.DEFAULT_FREE_DOWNLOAD_NOTE


def has_active_subscription(db: Session, user_id: int, realm_id: Optional[int] = None) -> bool:
    """是否持有「生效中且未到期」的订阅（可按服判定）"""
    target = resolve_realm_id(realm_id)
    query = db.query(models.UserSubscription).filter(
        models.UserSubscription.user_id == user_id,
        models.UserSubscription.status == "active",
        models.UserSubscription.end_date > datetime.now(),
    )
    if target is not None:
        query = query.filter(models.UserSubscription.realm_id == target)
    return query.first() is not None


def active_subscription(db: Session, user_id: int, realm_id: Optional[int] = None):
    """取一份生效中的订阅（按服；用于展示用户到底买的是哪个服）"""
    target = resolve_realm_id(realm_id)
    query = db.query(models.UserSubscription).filter(
        models.UserSubscription.user_id == user_id,
        models.UserSubscription.status == "active",
        models.UserSubscription.end_date > datetime.now(),
    )
    if target is not None:
        query = query.filter(models.UserSubscription.realm_id == target)
    return query.order_by(models.UserSubscription.end_date.desc()).first()


def _config_value(db: Session, key: str) -> Optional[str]:
    row = db.query(models.SystemConfig).filter(models.SystemConfig.key == key).first()
    return row.value if row else None


def subscription_required(db: Session) -> bool:
    """付费墙是否开启（缺省开启：项目默认按会员制运营）"""
    raw = _config_value(db, CONFIG_REQUIRED)
    if raw is None or str(raw).strip() == "":
        return True
    return str(raw).strip().lower() == "true"


def realm_requires_subscription(db: Session, realm_id: Optional[int] = None) -> bool:
    """这个服是否要会员才能看：公益服永远不要（即使全局付费墙开着）"""
    if is_free_realm(db, realm_id):
        return False
    return subscription_required(db)


def gate_message(db: Session) -> str:
    raw = _config_value(db, CONFIG_MESSAGE)
    text = (raw or "").strip()
    return text or DEFAULT_GATE_MESSAGE


def can_play(db: Session, user: models.WebUser, realm_id: Optional[int] = None) -> bool:
    """是否放行播放：管理员始终放行；公益服免费开放；付费墙关闭时全员放行

    订阅判定保持原有口径（显式服 / 本进程的服，不拿面板当前服去做回退），
    所以付费服的行为与升级前完全一致；公益服是新增的一层判断。
    """
    if getattr(user, "is_staff", False):
        return True
    target = _scoped_realm_id(db, realm_id)
    if realms.access_mode_of(db, target) == realms.ACCESS_FREE:
        # 公益服：免费开放，不是「白票」——这是这个服自己选的运营方式
        return True
    if not subscription_required(db):
        return True
    return has_active_subscription(db, user.id, resolve_realm_id(realm_id))


def ensure_playback_allowed(db: Session, user: models.WebUser,
                           realm_id: Optional[int] = None) -> None:
    """播放前校验：未订阅时抛 403（前端据此展示付费墙引导）

    ``realm_id`` 留空时按**本进程的服**判定（EA 上就是这台机器所属的服）。
    """
    if not can_play(db, user, realm_id):
        raise HTTPException(status_code=403, detail=gate_message(db))
    # C1 活力值：公益服用户活力值低于阈值时限制观影（管理员/非公益用户不受影响）
    _vitality.ensure_can_play(db, user)


def download_allowed(db: Session, realm_id: Optional[int] = None) -> bool:
    """本站点是否允许下载（缺省允许；关闭后第三方播放器触发的下载一并拦下）

    两层：全局开关（``allow_download`` 配置）与**该服的下载策略**——
    公益服没单独配策略时默认禁止下载（公益服靠免费开放引流，不能让整库被拖走）；
    付费服没单独配策略时跟随全局开关（与升级前完全一致）。
    """
    raw = _config_value(db, CONFIG_ALLOW_DOWNLOAD)
    global_allowed = True if raw is None or str(raw).strip() == "" else str(raw).strip().lower() == "true"
    if not global_allowed:
        return False
    target = _scoped_realm_id(db, realm_id)
    policy = realms.download_policy_of(db, target)
    if policy is not None:
        return bool(policy)
    return not realms.is_free_realm(db, target)


def ensure_download_allowed(db: Session, user: models.WebUser,
                            realm_id: Optional[int] = None,
                            request=None) -> None:
    """下载前校验：站点/该服关闭下载时抛 403（管理员依然放行，便于排查）

    ``request`` 带上下载策略网关（``DownloadGuardMiddleware``）已经判过的结论时直接复用：
    下载类请求「中间件判一遍、路由再判一遍」查的是同一件事（全局开关 + 该服策略），
    网关判过就不必重查。不传 ``request`` 或网关没给出结论时行为与以前完全一致。
    """
    state = getattr(request, "state", None) if request is not None else None
    if realm_id is None and state is not None and getattr(state, "download_allowed_by_guard", False):
        return
    if getattr(user, "is_staff", False):
        return
    if download_allowed(db, realm_id):
        return
    raise HTTPException(status_code=403, detail=download_gate_message(db, realm_id))


def download_gate_message(db: Session, realm_id: Optional[int] = None) -> str:
    """下载被拦时的提示：公益服说清「只提供在线观看」，其余沿用站点文案"""
    target = _scoped_realm_id(db, realm_id)
    if realms.is_free_realm(db, target) and realms.download_policy_of(db, target) is None:
        return FREE_REALM_DOWNLOAD_MESSAGE
    return DOWNLOAD_GATE_MESSAGE


# ==================== 自动续订（r48-⑬） ====================

# 触发窗口：到期前 24 小时内。任务书给了两个口径二选一
# （到期前 24h / 到期后 24h 宽限），这里选「到期前 24h」：
# 订阅仍是 active，顺延即 end_date += duration_days，无需翻转 status，
# 也不跟「到期提醒 expired 档」抢语义；部署重启漏掉一轮导致已过期的，
# 走既有的到期提醒通道（expired 档），不静默。
AUTO_RENEW_WINDOW_HOURS = 24

# 续订失败提醒的去重档位：复用 subscription_reminders 的唯一约束
# （与 7d/3d/1d/expired 同表不同 kind，互不干扰；续订成功后
# _grant_subscription 会清掉该订阅的全部提醒记录，下一周期可重新提醒）。
RENEW_FAILED_KIND = "renew_failed"


def _auto_renew_cost(db: Session, user: models.WebUser,
                     plan: models.SubscriptionPlan) -> dict:
    """自动续订的扣款口径：套餐积分价先过会员等级折扣（与⑪手动积分购买同口径）。

    优惠券 / 兑换码折扣权益是消耗品，需要用户意图，不在无人值守的自动续订里
    自动消耗——手动购买时仍可正常使用（见 _points_subscription_price 注释）。
    公益服（access_mode='free'）不打折，与人民币路径一致。
    """
    from backend import member_level as _ml

    list_points = int(plan.points_price)
    member_discount_pct = 0
    member_discount_points = 0
    _realm = getattr(plan, "realm", None)
    _is_paid_plan = not (_realm is not None
                         and getattr(_realm, "access_mode", "paid") == "free")
    if _is_paid_plan:
        member_discount_pct = _ml.get_user_discount_pct(db, user)
        if member_discount_pct > 0:
            member_discount_points = int(
                (Decimal(list_points) * Decimal(member_discount_pct)
                 / Decimal(100)).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
            )
    paid_points = list_points - member_discount_points
    return {
        "list_points": list_points,
        "member_discount_pct": member_discount_pct,
        "member_discount_points": member_discount_points,
        "paid_points": max(0, paid_points),
    }


def auto_renew_subscriptions(db: Session, now: Optional[datetime] = None) -> dict:
    """自动续订一轮：扫描 auto_renew=true 且 24 小时内到期的订阅，用积分余额
    按当前档位续订一个周期。

    调用方（janitor 定时链）每轮调一次。本函数只做落库并返回统计；
    失败通知的 payload 在 ``notices`` 里，调用方用既有订阅到期提醒通道
    （站内信）发出——通知失败不影响已提交的续订。

    幂等：续订成功后 end_date 推出 24h 窗口，重复执行不会双续；扣款走
    ``_spend_points`` 的原子条件 UPDATE，并发双跑也不会把余额扣成负数；
    每行先 ``with_for_update`` 再复核窗口，第二个并发进程会看到已顺延的
    end_date 而跳过。
    """
    now = now or datetime.now()
    window_end = now + timedelta(hours=AUTO_RENEW_WINDOW_HOURS)
    result: dict = {"renewed": 0, "failed": 0, "skipped": 0, "errors": 0,
                    "notices": []}

    candidates = (
        db.query(models.UserSubscription.id)
        .filter(
            models.UserSubscription.status == "active",
            models.UserSubscription.auto_renew == True,  # noqa: E712
            models.UserSubscription.end_date > now,
            models.UserSubscription.end_date <= window_end,
        )
        .order_by(models.UserSubscription.end_date.asc())
        .all()
    )
    for (sub_id,) in candidates:
        try:
            outcome = _auto_renew_one(db, sub_id, now)
        except Exception as exc:  # noqa: BLE001 — 一条失败不能带走整轮
            db.rollback()
            logger.warning("自动续订处理失败（订阅 %s）: %s", sub_id, exc)
            result["errors"] += 1
            continue
        if outcome == "renewed":
            result["renewed"] += 1
        elif outcome == "failed":
            result["failed"] += 1
        elif outcome == "skipped":
            result["skipped"] += 1
        elif isinstance(outcome, dict) and outcome.get("notice"):
            result["failed"] += 1
            result["notices"].append(outcome["notice"])
    return result


def _auto_renew_one(db: Session, sub_id: int, now: datetime):
    """处理一条订阅。返回 "renewed" / "failed" / "skipped"，
    或 {"notice": {...}}（余额不足：已落去重记录，调用方发通知）。"""
    from backend.api.economy import (
        InsufficientPoints,
        _grant_subscription,
        _spend_points,
    )
    from backend import reminders as _reminders

    sub = (
        db.query(models.UserSubscription)
        .options(joinedload(models.UserSubscription.plan),
                 joinedload(models.UserSubscription.user))
        .filter(models.UserSubscription.id == sub_id)
        .with_for_update()
        .first()
    )
    if sub is None:
        db.rollback()
        return "skipped"
    # 锁内复核：并发的另一轮可能已经续过，end_date 已推出窗口
    window_end = now + timedelta(hours=AUTO_RENEW_WINDOW_HOURS)
    if not (sub.status == "active" and sub.auto_renew
            and sub.end_date and now < sub.end_date <= window_end):
        db.rollback()
        return "skipped"
    user = sub.user
    plan = sub.plan
    if user is None or not getattr(user, "is_active", True):
        db.rollback()
        logger.info("自动续订跳过（订阅 %s）：用户不存在或已禁用", sub_id)
        return "skipped"
    if plan is None or not plan.is_active or plan.points_price is None:
        # 套餐下架 / 未配积分价：人民币自动扣款未开通，无从续订；只记数不打扰
        # （用户在开启开关时已做过 points_price 校验，这里是兜底）。
        db.rollback()
        logger.info("自动续订跳过（订阅 %s）：套餐不可用或无积分价", sub_id)
        return "skipped"

    cost = _auto_renew_cost(db, user, plan)
    paid_points = cost["paid_points"]
    if paid_points <= 0:
        db.rollback()
        logger.warning("自动续订跳过（订阅 %s）：积分价配置错误", sub_id)
        return "skipped"

    ref_id = f"auto_renew:{sub.id}:{now.strftime('%Y%m%d%H%M')}"
    try:
        _spend_points(
            db, user, paid_points, "subscription_auto_renew",
            f"自动续订 - {plan.name}（原价 {cost['list_points']}，"
            f"会员折扣 -{cost['member_discount_points']}，实付 {paid_points}）",
            ref_id,
        )
    except InsufficientPoints:
        db.rollback()
        # 余额不足：不续订。落一条去重记录（同一订阅只提醒一次），
        # 调用方走订阅到期提醒通道（站内信）通知用户。
        recorded = _reminders._record(
            db,
            {"subscription_id": sub.id, "user_id": user.id,
             "realm_id": sub.realm_id},
            RENEW_FAILED_KIND,
        )
        db.commit()
        if not recorded:
            return "failed"  # 已提醒过，不再打扰
        balance = int(getattr(user, "points", 0) or 0)
        return {"notice": {
            "user_id": user.id,
            "subscription_id": sub.id,
            "title": "⚡ 自动续订失败：能量不足",
            "content": (
                f"你的「{plan.name}」将于 {sub.end_date.strftime('%Y-%m-%d %H:%M')} 到期，"
                f"自动续订需要 {paid_points} 能量，当前余额 {balance}，余额不足未续订。"
                f"请及时补充能量，或关闭自动续订改手动续费。"
            ),
        }}

    # 扣款成功：顺延（_grant_subscription 会清掉该订阅的全部提醒记录，
    # 包括之前可能落过的 renew_failed，下一周期可重新提醒）。
    subscription = _grant_subscription(
        db, user, plan, plan.duration_days, "auto_renew", ref_id,
        realm_id=sub.realm_id,
    )
    order_id = f"AR{int(now.timestamp() * 1000)}{uuid.uuid4().hex[:6].upper()}"
    order = models.SubscriptionOrder(
        order_id=order_id, user_id=user.id,
        plan_id=plan.id, item_name=plan.name,
        # amount 保持人民币口径（沿用既有收入统计/退款/对账口径）；
        # 积分口径的原价/优惠/实付记在积分台账描述里，不混进人民币列。
        amount=plan.price, payment_method="auto_renew", status="paid",
        paid_at=now, subscription_id=subscription.id,
        days_granted=plan.duration_days,
    )
    db.add(order)
    db.commit()
    logger.info("自动续订成功（订阅 %s）：用户 %s 续 %s，实付 %s 能量",
                sub.id, user.id, plan.name, paid_points)
    return "renewed"


__all__ = [
    "CONFIG_REQUIRED",
    "CONFIG_MESSAGE",
    "CONFIG_ALLOW_DOWNLOAD",
    "DEFAULT_GATE_MESSAGE",
    "DOWNLOAD_GATE_MESSAGE",
    "FREE_REALM_DOWNLOAD_MESSAGE",
    "active_subscription",
    "has_active_subscription",
    "is_free_realm",
    "process_realm_id",
    "realm_access_mode",
    "realm_access_note",
    "realm_requires_subscription",
    "resolve_realm_id",
    "set_process_realm_resolver",
    "subscription_required",
    "gate_message",
    "can_play",
    "ensure_playback_allowed",
    "view_grant",
    "download_allowed",
    "download_gate_message",
    "ensure_download_allowed",
    "AUTO_RENEW_WINDOW_HOURS",
    "RENEW_FAILED_KIND",
    "auto_renew_subscriptions",
]
