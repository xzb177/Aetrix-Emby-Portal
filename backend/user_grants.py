"""用户授权资源卡片（Phase 4）：「这个人到底被授权了什么」

## 一张卡片回答三个问题

管理员在用户 360° 抽屉里翻到一个用户，真正想知道的只有三件事：

| 问题 | 口径（单一事实来源） |
|---|---|
| 这个服**有什么资源** | ``realms.stats``（媒体库 / 条目 / 挂载 / 节点） |
| 他在这个服**被授权到什么程度** | 付费服看订阅、公益服看积分解锁 → ``subscriptions.view_grant`` |
| 授权**能做什么** | ``can_play`` / ``view_grant`` / ``download_allowed``（``backend/subscriptions.py``） |

所以这里**一个服一张卡**，而不是把「订阅记录」再抄一遍表格：订阅记录回答
「他买过什么」，授权卡片回答「他现在能用什么、够不够用、什么时候失效」。

## 三条容易写错的地方

1. **状态一律现算**。``UserSubscription.status`` 不会随时间自动翻成 expired
   （这是全站的老口径，见 ``backend/subscriptions.py`` 开头），所以卡片上的
   「生效中 / 即将到期 / 已过期」全部按 ``end_date`` 与当下比较得出。
2. **判定必须带服**。多服部署下同一个用户在每个服各有一份互不影响的订阅
   （``realm_id``），拿「最近一条订阅」当成通用结论就是错的。
3. **公益服不需要订阅**。free 服 ``can_play`` 恒为真，但**查看 Emby 账号/线路**
   仍要花积分解锁（``EmbyViewUnlock``）——这两件事不一样，卡片分开写。

## 不做的事

不给这套卡片加写操作（发放 / 撤销授权仍在用户详情与服管理里做），也不改判定口径。
它是一层**只读视图**：所有数字都来自既有函数，不重算、不缓存、不落库。
"""
from __future__ import annotations

import logging
import math
from datetime import datetime
from typing import Optional

from sqlalchemy.orm import Session

from backend import devices, models, realms, subscriptions

logger = logging.getLogger(__name__)

#: 剩余天数 ≤ 该值即「即将到期」（与服概览 / 订阅总览的 7 天口径一致）
EXPIRING_DAYS = 7

#: 授权来源（前端按 grant 选文案与图标，按 state 选配色）
GRANT_SUBSCRIPTION = "subscription"   # 付费服：有效订阅
GRANT_EXPIRED = "expired"             # 付费服：有过订阅但已到期 / 已取消
GRANT_NONE = "none"                   # 付费服：从来没有订阅
GRANT_UNLOCK = "unlock"               # 公益服：已花积分解锁查看权限
GRANT_FREE_OPEN = "free_open"         # 公益服：没解锁（能看，账号卡不下发地址）

GRANT_LABELS = {
    GRANT_SUBSCRIPTION: "订阅生效中",
    GRANT_EXPIRED: "订阅已到期",
    GRANT_NONE: "未授权",
    GRANT_UNLOCK: "积分解锁中",
    GRANT_FREE_OPEN: "公益服免费开放",
}

#: 退款撤销（orders_admin 关单时会把订阅翻成 cancelled）——与「自然到期」分开说，
#: 否则管理员会以为用户自己没用完就到期了。
GRANT_LABEL_CANCELLED = "订阅已取消"


def _days_left(end_date: Optional[datetime], now: datetime) -> int:
    """剩余天数（**向上取整**）

    向下取整会把「还剩 5 小时」算成 0 天，而那恰恰是最该提醒的一种。
    向上取整后它显示 1 天，并且与订阅总览的「7 天内到期」筛选
    （``end_date <= now + 7 天``，见 ``admin_economy`` / ``realms``）完全对齐：
    ``ceil(剩余秒数 / 86400) <= 7`` 等价于「到期时间不超过 7 天后」。
    """
    if not end_date:
        return 0
    seconds = (end_date - now).total_seconds()
    if seconds <= 0:
        return 0
    return math.ceil(seconds / 86400)


def _latest_subscription(db: Session, user_id: int, realm_id: int
                         ) -> Optional[models.UserSubscription]:
    """该用户在某个服上最近的一条订阅（不管有没有过期）。

    用来在「已到期」的卡片上仍然说得出「他买的什么套餐、什么时候到期的」——
    只查生效中的行会让过期卡片变成一张没有上下文的空卡。
    """
    return (
        db.query(models.UserSubscription)
        .filter(
            models.UserSubscription.user_id == user_id,
            models.UserSubscription.realm_id == realm_id,
        )
        .order_by(models.UserSubscription.end_date.desc())
        .first()
    )


def _subscription_payload(sub: Optional[models.UserSubscription],
                          now: datetime) -> dict:
    """订阅行 → 卡片上的订阅块（``None`` 表示这个服从来没有订阅）"""
    if sub is None:
        return {
            "subscription_id": None,
            "plan_name": "",
            "start_date": None,
            "end_date": None,
            "days_left": 0,
            "auto_renew": False,
            "cancelled": False,
        }
    return {
        "subscription_id": sub.id,
        "plan_name": sub.plan.name if sub.plan else f"套餐 #{sub.plan_id}",
        "start_date": sub.start_date.isoformat() if sub.start_date else None,
        "end_date": sub.end_date.isoformat() if sub.end_date else None,
        "days_left": _days_left(sub.end_date, now),
        "auto_renew": bool(sub.auto_renew),
        "cancelled": sub.status == "cancelled",
    }


def _resources(db: Session, realm_id: int) -> dict:
    """这个服提供的资源（媒体库 / 条目 / 挂载 / 出流节点）。

    直接复用 ``realms.stats``——服概览页与这里必须是同一套数字，
    各自数一遍迟早会差。

    读失败时**返回 None 而不是 0**：这个服明明有 3 个库却因为一次统计异常
    显示成 0，比显示「—」危险得多（管理员会以为内容被删了）。
    """
    try:
        stats = realms.stats(db, realm_id)
    except Exception:  # noqa: BLE001 — 一个服的统计读不到，不该拖垮整张抽屉
        logger.warning("读取服 %s 的资源统计失败（显示为未知）", realm_id, exc_info=True)
        return {"libraries": None, "enabled_libraries": None, "items": None,
                "mounts": None, "nodes": None, "nodes_online": None}
    return {
        "libraries": int(stats.get("libraries") or 0),
        "enabled_libraries": int(stats.get("enabled_libraries") or 0),
        "items": int(stats.get("items") or 0),
        "mounts": int(stats.get("mounts") or 0),
        "nodes": int(stats.get("nodes") or 0),        "nodes_online": int(stats.get("nodes_online") or 0),
    }


def _grant_of(db: Session, user: models.WebUser, realm: models.ServerRealm,
              now: datetime) -> tuple[str, dict]:
    """这个服上的授权来源 + 随附的说明块（订阅 / 解锁记录）"""
    free = realms.is_free_realm(db, realm.id)
    if free:
        granted, unlock = subscriptions.view_grant(db, user, realm.id)
        unlock_payload = {
            "unlocked": bool(granted),
            "unlocked_at": unlock.unlocked_at.isoformat() if unlock and unlock.unlocked_at else None,
            "expires_at": unlock.expires_at.isoformat() if unlock and unlock.expires_at else None,
            "points_spent": int(unlock.points_spent) if unlock else 0,
        }
        return (GRANT_UNLOCK if granted else GRANT_FREE_OPEN), {"unlock": unlock_payload}

    sub = _latest_subscription(db, user.id, realm.id)
    if sub is not None and sub.status == "active" and sub.end_date and sub.end_date > now:
        return GRANT_SUBSCRIPTION, {"subscription": _subscription_payload(sub, now)}
    payload = {"subscription": _subscription_payload(sub, now)}
    return (GRANT_EXPIRED if sub is not None else GRANT_NONE), payload


def _expiringSoon(grant: str, detail: dict) -> bool:
    """是否「即将到期」（≤ EXPIRING_DAYS 天）。已过期 / 未授权不算。"""
    if grant != GRANT_SUBSCRIPTION:
        return False
    sub = detail.get("subscription") or {}
    days = int(sub.get("days_left") or 0)
    return 0 < days <= EXPIRING_DAYS


def realm_card(db: Session, user: models.WebUser,
               realm: models.ServerRealm, now: datetime) -> dict:
    """一个服 = 一张授权卡片"""
    grant, detail = _grant_of(db, user, realm, now)
    sub = detail.get("subscription") or {}
    free = realms.is_free_realm(db, realm.id)
    can_play = subscriptions.can_play(db, user, realm.id)
    view_granted, _unlock = subscriptions.view_grant(db, user, realm.id)
    expiring = _expiringSoon(grant, detail)

    if expiring:
        state = "warn"
    elif grant in (GRANT_SUBSCRIPTION, GRANT_UNLOCK, GRANT_FREE_OPEN):
        state = "ok"
    else:
        state = "off"

    return {
        "realm_id": realm.id,
        "realm_name": realm.name,
        "slug": realm.slug,
        "is_default": realm.id == realms.legacy_realm_id(db),
        "is_active": bool(realm.is_active),
        "access_mode": realms.normalize_access_mode(realm.access_mode),
        "is_free": free,
        "access_note": realms.access_note_of(db, realm.id),
        # ---- 授权 ----
        "grant": grant,
        "grant_label": (
            GRANT_LABEL_CANCELLED
            if grant == GRANT_EXPIRED and sub.get("cancelled")
            else GRANT_LABELS[grant]
        ),
        "state": state,
        "expiring_soon": expiring,
        # ---- 授权能做什么 ----
        "can_play": bool(can_play),
        "view_granted": bool(view_granted),
        "download_allowed": bool(subscriptions.download_allowed(db, realm.id)),
        # ---- 这个服有什么 ----
        "resources": _resources(db, realm.id),
        **detail,
    }


def summary(db: Session, user: models.WebUser, cards: list[dict]) -> dict:
    """抽屉顶部那几行：能看几个服 / 设备用了几台"""
    try:
        limit: Optional[int] = devices.device_limit(db)
        used: Optional[int] = len(devices.active_devices(db, user.id))
    except Exception:  # noqa: BLE001 — 设备数读不到不该让整张抽屉失败
        logger.warning("读取用户 %s 的设备数失败（显示为未知）", user.id, exc_info=True)
        limit, used = None, None
    return {
        "realms_total": len(cards),
        "realms_playable": len([c for c in cards if c["can_play"]]),
        "realms_expiring": len([c for c in cards if c["expiring_soon"]]),
        "realms_expired": len([c for c in cards if c["grant"] == GRANT_EXPIRED]),
        "devices_used": used,
        "device_limit": limit,
        "play_line": "relay",
        "play_line_label": "302 直链",
        "scope_note": "授权与到期按当下现算（订阅行的 status 字段不会随时间自动过期）；"
                      "停用的服同样列出来并标注，它不改变播放判定，只是提醒管理员该清理了。",
    }


def cards(db: Session, user: models.WebUser) -> dict:
    """一个用户的完整授权卡片集（管理端一次拿全，前端不拼）"""
    now = datetime.now()
    rows = [
        realm_card(db, user, realm, now)
        for realm in realms.list_realms(db, include_disabled=True)
    ]
    # 能在看的服排前面，其余按服自身的排序（sort_order / id）——管理员先看到有问题的
    rows.sort(key=lambda c: (not c["can_play"], not c["is_active"], c["realm_id"]))
    return {
        "user": {
            "id": user.id,
            "username": user.username,
            "is_staff": bool(user.is_staff),
            "is_active": bool(user.is_active),
        },
        "summary": summary(db, user, rows),
        "cards": rows,
    }


__all__ = [
    "EXPIRING_DAYS",
    "GRANT_EXPIRED",
    "GRANT_FREE_OPEN",
    "GRANT_LABEL_CANCELLED",
    "GRANT_LABELS",
    "GRANT_NONE",
    "GRANT_SUBSCRIPTION",
    "GRANT_UNLOCK",
    "cards",
    "realm_card",
    "summary",
]
