"""订阅到期提醒（续费通知）

要解决的问题：**会员到期前没有任何提醒**。`AdminEvent.SUBSCRIPTION_EXPIRED` 这个事件类型
早就定义好了，却从来没有发送点；续期只能靠用户自己想起来。对会员制站点来说，
「快到期了」是转化率最高的一次触达，缺了它等于白白流失续费。

这一模块负责三件事：

1. **判定**：扫出「生效中且即将到期」的订阅，按 ``expiry_reminder_days``（默认 7/3/1）
   分成档位；再加一档 ``expired``，在订阅刚过期时通知一次。
2. **去重**：每个（订阅 × 档位）只发一次，靠 ``subscription_reminders`` 表里的唯一约束。
   提醒任务每小时跑一次也不会重复打扰——这是「稳定运行」的基本要求。
3. **投递**：走站内信 + WebSocket 实时推送（``notify_admin_event``），
   与后台手工授予/续期的通知是同一条链路，用户端消息中心里能直接看到。

口径与 `backend/subscriptions.py` 一致：会员状态以 ``end_date`` 现算，不依赖
``UserSubscription.status``（它不会随时间自动翻转）。这里同样**不改 status**——
翻转 status 会让「曾经买过」的历史语义丢失，而所有判定本来就看 end_date。

开关：
- ``expiry_reminder_enabled``（布尔，默认 true）关掉后整个任务不动任何数据；
- ``expiry_reminder_days``（字符串，默认 ``7,3,1``）自定义提前几天提醒。

调度：EM（面板）启动时 ``start_reminder_scheduler()`` 起一个 daemon 线程，
按 ``REMINDER_INTERVAL``（默认 3600 秒）执行 ``run_expiry_reminders()``。
EA 不跑这个任务——通知与业务配置都在 EM 侧。
"""
from __future__ import annotations

import logging
import threading
from datetime import datetime, timedelta
from typing import Optional

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from backend import models, realms
from backend.emby_server.env_util import env_int

logger = logging.getLogger(__name__)

CONFIG_ENABLED = "expiry_reminder_enabled"
CONFIG_DAYS = "expiry_reminder_days"

# 默认在到期前 7 / 3 / 1 天提醒（运营可直接改配置，不必改代码）
DEFAULT_THRESHOLDS = (7, 3, 1)
# 提醒任务周期（秒）。默认一小时一次：档位是按「天」算的，跑得再密也不会多发一条
REMINDER_INTERVAL = env_int("REMINDER_INTERVAL", 3600, 300)

# 消息类型：与 AdminEvent 的 ``subscription.*`` 前缀一致，用户端按此归类
MESSAGE_TYPE = "subscription"


def _config_value(db: Session, key: str) -> Optional[str]:
    row = db.query(models.SystemConfig).filter(models.SystemConfig.key == key).first()
    return row.value if row else None


def reminders_enabled(db: Session) -> bool:
    """是否开启到期提醒（缺省开启：没有配置时按开启处理）"""
    raw = _config_value(db, CONFIG_ENABLED)
    if raw is None or str(raw).strip() == "":
        return True
    return str(raw).strip().lower() == "true"


def parse_thresholds(db: Session) -> list[int]:
    """解析提醒档位（天），返回**从大到小**的列表

    非法输入不抛错：整段配置写错时退回默认档位，而不是让后台任务整体挂掉。
    """
    raw = _config_value(db, CONFIG_DAYS)
    text = (raw or "").strip()
    if not text:
        return list(DEFAULT_THRESHOLDS)
    days: list[int] = []
    for part in text.replace("，", ",").split(","):
        part = part.strip()
        if not part:
            continue
        try:
            value = int(part)
        except ValueError:
            continue
        if 1 <= value <= 365 and value not in days:
            days.append(value)
    return sorted(days, reverse=True) or list(DEFAULT_THRESHOLDS)


_SCAN_BATCH = 500  # 键集分页扫描的每页订阅数


def _sent_kinds_batch(db: Session, subscription_ids: list[int]) -> dict[int, set[str]]:
    """批量取回这批订阅已发送过的 kind，返回 ``{subscription_id: {kind, ...}}``。

    替代逐条调用的 `_sent_kinds`，避免 N+1 查询；没发过任何提醒的订阅不会出现在
    返回值里，调用方需用 ``sent_map.get(sub_id, set())`` 兜底。
    """
    if not subscription_ids:
        return {}
    rows = db.query(
        models.SubscriptionReminder.subscription_id,
        models.SubscriptionReminder.kind,
    ).filter(
        models.SubscriptionReminder.subscription_id.in_(subscription_ids)
    ).all()
    sent_map: dict[int, set[str]] = {}
    for sub_id, kind in rows:
        sent_map.setdefault(sub_id, set()).add(kind)
    return sent_map


def _record(db: Session, item: dict, kind: str) -> bool:
    """登记一条发送记录；已被别的进程/上一轮记过则返回 False

    唯一约束是**并发安全**的那一层：EM 多实例或任务恰好重叠时，只有一个能写进去。
    """
    db.add(models.SubscriptionReminder(
        subscription_id=item["subscription_id"],
        user_id=item["user_id"],
        realm_id=item.get("realm_id"),
        kind=kind,
        sent_at=datetime.now(),
    ))
    try:
        db.commit()
        return True
    except IntegrityError:
        db.rollback()
        return False


def _retract(db: Session, subscription_id: int, kind: str) -> None:
    """撤掉本轮占用的去重占位（发送失败时调用）。

    ``_record`` 先占位是防并发双发的必要代价（多实例/任务重叠时靠唯一约束只发一次），
    但发送真失败时必须把这行撤掉：否则「失败」被永久记成「已发送」，
    下一轮 ``collect_due`` 看到 kind 已在 sent 里就再也不会补发，一条续费提醒就此丢失。
    唯一约束保证这行就是本轮自己占的位，删它不会误删别人的记录。
    """
    db.query(models.SubscriptionReminder).filter(
        models.SubscriptionReminder.subscription_id == subscription_id,
        models.SubscriptionReminder.kind == kind,
    ).delete(synchronize_session=False)  # 行是刚 commit 的，不在 session 缓存里纠缠
    db.commit()


def _site_url(db: Session) -> str:
    return (_config_value(db, "site_url") or "").strip().rstrip("/")


def collect_due(db: Session, now: Optional[datetime] = None) -> dict:
    """算出「这一轮该发什么」，不改任何数据（干跑 / 面板预览都用它）

    返回 ``{"reminders": [...], "expired": [...]}``，元素都带好文案所需的字段。
    """
    now = now or datetime.now()
    thresholds = parse_thresholds(db)
    horizon = now + timedelta(days=max(thresholds))

    realm_names = {r.id: r.name for r in realms.list_realms(db)}

    reminders: list[dict] = []
    expired: list[dict] = []
    last_id = 0
    # 键集分页：按 id 升序一页页扫，避免一次性把全表订阅读进内存
    while True:
        page = db.query(models.UserSubscription).filter(
            models.UserSubscription.id > last_id,
            models.UserSubscription.status == "active",
            models.UserSubscription.end_date != None,  # noqa: E711 — 允许 end_date 缺失的老行不参与
        ).order_by(models.UserSubscription.id.asc()).limit(_SCAN_BATCH).all()
        if not page:
            break
        last_id = page[-1].id

        # 本页已发送 kind 一次取回，循环里不再逐条查库
        sent_map = _sent_kinds_batch(db, [s.id for s in page])
        plan_ids = [s.plan_id for s in page if s.plan_id]
        plan_names = {
            p.id: p.name for p in db.query(models.SubscriptionPlan).filter(
                models.SubscriptionPlan.id.in_(plan_ids)
            ).all()
        } if plan_ids else {}

        for sub in page:
            end = sub.end_date
            if end is None:
                continue
            sent = sent_map.get(sub.id, set())
            # days_left 按「整天」算：今天 23:59 到期就是 0 天，与用户端 `days_left` 口径一致
            seconds_left = (end - now).total_seconds()
            days_left = int(seconds_left // 86400) if seconds_left > 0 else -1
            info = {
                "subscription_id": sub.id,
                "user_id": sub.user_id,
                "realm_id": sub.realm_id,
                "realm_name": realm_names.get(sub.realm_id, "") if sub.realm_id else "",
                "plan_name": plan_names.get(sub.plan_id) or "会员",
                "end_date": end,
            }

            if seconds_left <= 0:
                # 刚过期：只发一次「已到期」，之后不再打扰（续费靠用户自己或运营触达）
                if "expired" not in sent:
                    expired.append({**info, "kind": "expired", "days_left": 0})
                continue

            if end > horizon:
                continue
            # 取**最贴近**的那一档（升序里第一个 >= days_left）。
            # 不能取最宽松的那一档：还剩 2 天的订阅如果走 7 天档，7 天档就用掉了，
            # 真正该发的「3 天」永远不会发（而且文案会说“还有 6 天”）。
            tightest = next((t for t in sorted(thresholds) if days_left <= t), None)
            if tightest is not None and f"{tightest}d" not in sent:
                reminders.append({**info, "kind": f"{tightest}d",
                                  "threshold": tightest, "days_left": days_left})
    return {"reminders": reminders, "expired": expired, "thresholds": thresholds}


def _reminder_text(db: Session, item: dict) -> tuple[str, str]:
    """组装标题与正文（含续费引导）"""
    days = item["days_left"]
    realm_part = f"（{item['realm_name']}）" if item.get("realm_name") else ""
    end_text = item["end_date"].strftime("%Y-%m-%d")
    url = _site_url(db)
    tail = f"\n续费入口：{url}/wallet" if url else "\n请前往「钱包」页续费，以免播放中断。"

    if days <= 0:
        title = f"⚠️ 会员已到期{realm_part}"
        content = (
            f"你的「{item['plan_name']}」已于 {end_text} 到期。\n"
            "到期后非会员将无法播放，第三方播放器上的账号也会一并受限。" + tail
        )
    else:
        title = f"⏳ 会员还有 {days} 天到期{realm_part}"
        content = (
            f"你的「{item['plan_name']}」将于 {end_text} 到期（剩余 {days} 天）。\n"
            "提前续费不会浪费剩余时长，新时长会在当前到期日之后叠加。" + tail
        )
    return title, content


async def run_expiry_reminders(db: Session, now: Optional[datetime] = None,
                               dry_run: bool = False) -> dict:
    """执行一轮到期提醒，返回统计结果

    幂等：同一（订阅 × 档位）只会成功发送一次；重复调用返回 ``sent=0``。
    """
    result = {
        "enabled": reminders_enabled(db),
        "dry_run": dry_run,
        "thresholds": [],
        "reminded": 0,
        "expired_notified": 0,
        "skipped": 0,
        "failed": 0,
        "due": [],
    }
    if not result["enabled"]:
        return result

    due = collect_due(db, now=now)
    result["thresholds"] = due["thresholds"]
    result["due"] = [
        {"user_id": i["user_id"], "subscription_id": i["subscription_id"],
         "kind": i["kind"], "days_left": i["days_left"]}
        for i in due["reminders"] + due["expired"]
    ]
    if dry_run:
        return result

    from backend.notifications import notify_admin_event, AdminEvent

    for item in due["reminders"] + due["expired"]:
        is_expired = item["kind"] == "expired"
        if not _record(db, item, item["kind"]):
            result["skipped"] += 1
            continue
        title, content = _reminder_text(db, item)
        try:
            sent_result = await notify_admin_event(
                event_type=AdminEvent.SUBSCRIPTION_EXPIRED if is_expired
                else AdminEvent.SUBSCRIPTION_REMINDER,
                user_id=item["user_id"],
                title=title,
                content=content,
                related_id=item["subscription_id"],
            ) or {}
        except Exception as exc:  # noqa: BLE001 — 一条失败不能带走整轮
            logger.warning("到期提醒发送失败（订阅 %s）: %s", item["subscription_id"], exc)
            _retract(db, item["subscription_id"], item["kind"])  # 撤掉占位，下一轮还能补发
            result["failed"] += 1
            continue
        # 模式 B —— 静默失败：所有渠道都返回 False（InAppChannel.send 内部吞掉异常，从不抛），
        # 消息其实没写进站内信表，不能记成「已发送」。部分渠道成功即算成功，不撤记录。
        if not any(sent_result.values()):
            logger.warning("到期提醒全部渠道发送失败（订阅 %s）: %s", item["subscription_id"], sent_result)
            _retract(db, item["subscription_id"], item["kind"])  # 同上，撤掉占位等下一轮重试
            result["failed"] += 1
            continue
        if is_expired:
            result["expired_notified"] += 1
        else:
            result["reminded"] += 1

    if result["reminded"] or result["expired_notified"] or result["skipped"]:
        logger.info(
            "到期提醒完成: 提醒 %s 条 / 到期通知 %s 条 / 跳过 %s 条",
            result["reminded"], result["expired_notified"], result["skipped"],
        )
    return result


def reminder_status(db: Session, now: Optional[datetime] = None) -> dict:
    """面板口径：开关、档位、当前待发数量与最近发送记录"""
    now = now or datetime.now()
    enabled = reminders_enabled(db)
    due = collect_due(db, now=now) if enabled else {"reminders": [], "expired": [], "thresholds": parse_thresholds(db)}
    recent = db.query(models.SubscriptionReminder).order_by(
        models.SubscriptionReminder.sent_at.desc()
    ).limit(10).all()
    user_names = {
        u.id: u.username for u in db.query(models.WebUser).filter(
            models.WebUser.id.in_([r.user_id for r in recent])
        ).all()
    } if recent else {}
    return {
        "enabled": enabled,
        "thresholds": due["thresholds"],
        "interval_seconds": REMINDER_INTERVAL,
        "pending_reminders": len(due["reminders"]),
        "pending_expired": len(due["expired"]),
        "total_sent": db.query(models.SubscriptionReminder).count(),
        "recent": [
            {
                "id": r.id,
                "username": user_names.get(r.user_id, f"用户 {r.user_id}"),
                "kind": r.kind,
                "sent_at": r.sent_at.isoformat() if r.sent_at else None,
            }
            for r in recent
        ],
    }


# ==================== 后台调度 ====================

_SCHEDULER_STARTED = False
_SCHEDULER_LOCK = threading.Lock()


def start_reminder_scheduler(interval_seconds: Optional[int] = None) -> bool:
    """启动到期提醒线程（同一进程只启动一次；daemon，随进程退出）

    只在 EM（面板）上启动：提醒依赖面板的通知链路与业务配置，
    EA 是出流单元，不该自己发通知（多台 EA 会重复打扰用户）。
    """
    global _SCHEDULER_STARTED
    interval = REMINDER_INTERVAL if interval_seconds is None else max(30, interval_seconds)
    with _SCHEDULER_LOCK:
        if _SCHEDULER_STARTED:
            return False
        _SCHEDULER_STARTED = True

    def _loop() -> None:
        import asyncio

        from backend import coupons
        from backend.database import SessionLocal

        # 复用同一个 Event：每轮 new 一个 Event().wait() 没有意义
        wake = threading.Event()
        while True:
            wake.wait(interval)
            db = SessionLocal()
            try:
                summary = asyncio.run(run_expiry_reminders(db))
                if summary["reminded"] or summary["expired_notified"]:
                    logger.info(
                        "到期提醒周期完成: 提醒 %s / 到期 %s",
                        summary["reminded"], summary["expired_notified"],
                    )
                # 优惠券的预订额度不能被「点了下单但没付款」的订单永远占着：
                # 超时未支付的预订在这里自动关单并还额度（口径见 coupons.sweep_stale_reservations）
                swept = coupons.sweep_stale_reservations(db)
                if swept["scanned"]:
                    logger.info(
                        "优惠券预订清理: 扫描 %s / 自动关单 %s / 补记已用 %s / 释放 %s",
                        swept["scanned"], swept["closed_orders"],
                        swept["consumed"], swept["released"],
                    )
                db.commit()
            except Exception as exc:  # noqa: BLE001 — 提醒线程绝不能因一次失败退出
                logger.warning("到期提醒周期异常: %s", exc)
                db.rollback()
            finally:
                db.close()

    threading.Thread(target=_loop, daemon=True, name="subscription-reminders").start()
    logger.info("订阅到期提醒已启动（每 %s 秒检查一次）", interval)
    return True


__all__ = [
    "CONFIG_DAYS",
    "CONFIG_ENABLED",
    "DEFAULT_THRESHOLDS",
    "MESSAGE_TYPE",
    "REMINDER_INTERVAL",
    "collect_due",
    "parse_thresholds",
    "reminder_status",
    "reminders_enabled",
    "run_expiry_reminders",
    "start_reminder_scheduler",
]
