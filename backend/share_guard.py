"""防共享两件套（v2.43.0）：跨城市行为轨迹 + 同播检测

「账号共享」有两种最常见的形态，长得不一样但根子一样——**一个账号被不止一个人在用**：
有人把号借出去，有人买了站外的共享会员。于是这里做两件事：

1. **跨城市行为轨迹**：登录 / 开始播放时把「IP → 城市」记下来。同一账号在
   **很短的时间窗口内**出现在不同城市，物理上几乎不可能（跨省高铁也要几小时），
   判定为异常。
2. **同播检测**：同一账号同时有多路播放会话，判定为异常。

**每一档都由管理员显式选择，缺省 = 关闭（``off``）**：

- ``off``：什么都不做——不判定、不写库、不通知；
- ``record``：只记录（写 ``share_guard_events``），后台能查；
- ``alert``：记录 + 通知管理员（站内信）；
- ``enforce``：记录 + 通知 + **处置**（跨城市 = 停用账号；同播 = 拦下多余会话）。

``enforce`` 永远不会因为「升级到新版本」而自动生效，只能由管理员显式选择。
这是防共享这类功能的分寸：判错一次就是误伤正常用户（出差、换网络、公司家庭
双宽带），而漏判一次只是少抓一个共享号。

城市来自既有能力「IP 与地理位置」（``backend/integrations/geoip.py``）：
**没配提供方就查不到城市**。查不到时一律按「不知道」处理，既不写轨迹也不判定
异常——绝不把「未知」当成「换了城市」（否则未配置地理库的所有用户都会被误判）。

配置全部落在 ``SystemConfig``（EM 与 EA 共用同一个库，见 ``playback_policy.py``
的口径），读配置走 ``integrations.store`` 的短 TTL 热读；默认值只在本模块定义
一次，``config_self_heal`` 引用 ``DEFAULTS`` 补缺失行。

接入点只有两处，都在热路径上（登录、播放进度上报），所以每一步都先看开关：
- 登录：``authlog.record_event`` 落日志之后（门户登录 / 客户端登录各一处）；
- 播放：``compat_routes._upsert_session`` 新建会话时。
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Optional

from sqlalchemy.orm import Session

from backend import models
from backend.integrations import store

logger = logging.getLogger(__name__)

# ==================== 配置键与默认值（默认值只在这里定义一次） ====================

CONFIG_TRAVEL_ACTION = "share_travel_action"
CONFIG_TRAVEL_WINDOW = "share_travel_window_minutes"
CONFIG_CONCURRENT_ACTION = "share_concurrent_action"
CONFIG_CONCURRENT_LIMIT = "share_concurrent_limit"
CONFIG_RETENTION_DAYS = "share_guard_retention_days"

#: 四档处置。``off`` 是缺省，升级上来的老部署行为与升级前完全一致。
ACTIONS = ("off", "record", "alert", "enforce")

#: 出厂默认（引用式：``config_self_heal`` 直接拿这份去补行）
DEFAULTS: dict[str, str] = {
    CONFIG_TRAVEL_ACTION: "off",
    CONFIG_TRAVEL_WINDOW: "30",
    CONFIG_CONCURRENT_ACTION: "off",
    CONFIG_CONCURRENT_LIMIT: "2",
    CONFIG_RETENTION_DAYS: "90",
}

#: 时间窗口的合法区间（分钟）。下限 1：窗口 = 0 会变成「上一次不管隔多久都算」，
#: 那不是防共享，那是随机报警。
WINDOW_MIN, WINDOW_MAX = 1, 1440
#: 同时播放数上限的合法区间。下限 1：0 会变成「一路都拦」。
LIMIT_MIN, LIMIT_MAX = 1, 20

KIND_TRAVEL = "travel"
KIND_CONCURRENT = "concurrent"

KIND_LABELS = {
    KIND_TRAVEL: "跨城市轨迹",
    KIND_CONCURRENT: "同播检测",
}

ACTION_LABELS = {
    "off": "关闭",
    "record": "只记录",
    "alert": "记录并告警",
    "enforce": "记录、告警并处置",
}

#: geoip 拿不到（或返回这种占位）时不做任何判定
_UNKNOWN_REGIONS = {"", "未知", "unknown", "unassigned", "n/a", "-"}

#: 同一个人同一类异常的告警节流（分钟）。
#:
#: 「处置」档拦下多余会话时**不会**停用账号，所以一个狂刷会话的客户端可以反复触发
#: 判定 —— 事件行每次都要记（那是事实），但站内信不该把管理员的邮箱刷爆。
#: 这是进程内的软限制：多进程部署下每个进程各有一份，节流窗口可能略松，
#: 但只会多出几条提醒，不会漏掉任何一次判定。
_NOTIFY_THROTTLE_MINUTES = 10
_notified_at: dict[tuple, datetime] = {}


def _should_notify(user_id: int, kind: str, now: datetime) -> bool:
    key = (int(user_id or 0), kind)
    last = _notified_at.get(key)
    if last is not None and now - last < timedelta(minutes=_NOTIFY_THROTTLE_MINUTES):
        return False
    if len(_notified_at) > 1024:  # 顺手清掉过期条目，避免字典无限长
        cutoff = now - timedelta(minutes=_NOTIFY_THROTTLE_MINUTES)
        for old_key, stamp in list(_notified_at.items()):
            if stamp < cutoff:
                _notified_at.pop(old_key, None)
    _notified_at[key] = now
    return True


# ==================== 配置读取 ====================


def _raw(db: Session, key: str) -> str:
    """统一热读（与 ``playback_policy`` 同一套）：短 TTL 缓存，保存时失效"""
    return (store.get_value(db, key, DEFAULTS.get(key, "")) or "").strip()


def _action(db: Session, key: str) -> str:
    value = _raw(db, key).lower()
    return value if value in ACTIONS else "off"


def _int(db: Session, key: str, low: int, high: int) -> int:
    try:
        number = int(float(_raw(db, key) or DEFAULTS[key]))
    except (TypeError, ValueError):
        number = int(DEFAULTS[key])
    return max(low, min(high, number))


def travel_action(db: Session) -> str:
    """跨城市轨迹的处置档位"""
    return _action(db, CONFIG_TRAVEL_ACTION)


def travel_window_minutes(db: Session) -> int:
    """城市切换判定的时间窗口（分钟）：窗口内换城市才算异常"""
    return _int(db, CONFIG_TRAVEL_WINDOW, WINDOW_MIN, WINDOW_MAX)


def concurrent_action(db: Session) -> str:
    """同播检测的处置档位"""
    return _action(db, CONFIG_CONCURRENT_ACTION)


def concurrent_limit(db: Session) -> int:
    """同时播放数上限（超出即判定为同播）"""
    return _int(db, CONFIG_CONCURRENT_LIMIT, LIMIT_MIN, LIMIT_MAX)


def retention_days(db: Session) -> int:
    """防共享事件保留天数（0 = 不自动清理）"""
    return _int(db, CONFIG_RETENTION_DAYS, 0, 3650)


def _geo_ready(db: Session) -> bool:
    try:
        from backend.integrations import geoip

        return geoip.provider(db) != "none"
    except Exception:  # noqa: BLE001 — 能力模块读不到就当作没配
        return False


def policy_payload(db: Session) -> dict:
    """当前策略（后台读一份，前端不维护默认值）"""
    return {
        "travel_action": travel_action(db),
        "travel_window_minutes": travel_window_minutes(db),
        "concurrent_action": concurrent_action(db),
        "concurrent_limit": concurrent_limit(db),
        "retention_days": retention_days(db),
        "actions": list(ACTIONS),
        "action_labels": dict(ACTION_LABELS),
        # 城市能不能查出来：没配地理能力时，跨城市检测只能「不判定」
        "geo_ready": _geo_ready(db),
    }


def write_policy(db: Session, values: dict) -> dict:
    """写回策略（只认白名单里的键；非法值保持原值不动，不存半个坏配置）"""
    applied: dict = {}
    for key, value in (values or {}).items():
        if key not in DEFAULTS:
            continue
        if key in (CONFIG_TRAVEL_ACTION, CONFIG_CONCURRENT_ACTION):
            text = str(value or "").strip().lower()
            if text not in ACTIONS:
                continue
        else:
            if key == CONFIG_TRAVEL_WINDOW:
                low, high = WINDOW_MIN, WINDOW_MAX
            elif key == CONFIG_CONCURRENT_LIMIT:
                low, high = LIMIT_MIN, LIMIT_MAX
            else:
                low, high = 0, 3650
            try:
                number = int(float(str(value).strip() or "0"))
            except (TypeError, ValueError):
                continue
            text = str(max(low, min(high, number)))
        row = db.query(models.SystemConfig).filter(models.SystemConfig.key == key).first()
        if row:
            row.value = text
        else:
            db.add(models.SystemConfig(key=key, value=text))
        applied[key] = text
    store.invalidate()  # 写时失效：同一进程内保存即生效
    return applied


# ==================== 城市（复用既有能力，查不到就是查不到） ====================


def city_of(db: Session, ip: Optional[str]) -> str:
    """IP → 城市串。查不到 / 未配置地理能力时返回空串（= 不知道，不是「别处」）。

    走 ``geoip`` 自己的缓存，所以每条登录/播放不会多打一次外部接口；
    任何异常都吞掉——归属地是可选的锦上添花，不能让它把登录拖挂。
    """
    value = (ip or "").strip()
    if not value:
        return ""
    try:
        from backend.integrations import geoip

        region = (geoip.region_of(db, value) or "").strip()
    except Exception:  # noqa: BLE001
        return ""
    return "" if region.lower() in _UNKNOWN_REGIONS else region


# ==================== 内部：写一行 + 通知管理员 ====================


def _add_event(
    db: Session,
    *,
    user: models.WebUser,
    kind: str,
    action: str,
    ip: Optional[str],
    region: str,
    prev_region: Optional[str] = None,
    sessions: int = 0,
    detail: str,
    is_baseline: bool = False,
    commit: bool = True,
) -> models.ShareGuardEvent:
    row = models.ShareGuardEvent(
        user_id=user.id,
        username=(user.username or "")[:64] or None,
        kind=kind,
        action=action,
        is_baseline=bool(is_baseline),
        ip=(ip or "")[:64] or None,
        region=(region or "")[:100] or None,
        prev_region=(prev_region or "")[:100] or None,
        sessions=int(sessions or 0),
        detail=(detail or "")[:255] or None,
    )
    db.add(row)
    if commit:
        db.commit()
    return row


def _notify_staff(db: Session, title: str, content: str, *, commit: bool = True) -> int:
    """给每位启用中的管理员写一条站内信，返回条数

    这里刻意**同步**落库，而不是调 ``notifications.notify_staff_users``：判定发生在
    登录与播放上报这两条热路径上（同步 def，跑在线程池里），而
    ``notify_staff_users`` 是 async——在同步上下文里调它要么开新事件循环（已经
    在一个里面了），要么把整条登录路径改成 async。同步落库 + 不做 WebSocket
    实时推送是这里更稳的取舍：站内信照样能第一时间在后台看到。
    """
    staff = (
        db.query(models.WebUser)
        .filter(
            models.WebUser.is_staff == True,  # noqa: E712
            models.WebUser.is_active == True,  # noqa: E712
        )
        .all()
    )
    for person in staff:
        db.add(
            models.StationMessage(
                from_user_id=None,
                to_user_id=person.id,
                title=title[:200],
                content=content,
                message_type="share_guard",
                related_id=None,
                is_read=False,
            )
        )
    if staff and commit:
        db.commit()
    return len(staff)


def _disable_user(db: Session, user: models.WebUser, *, detail: str, commit: bool = True) -> None:
    """停用账号 + 吊销其已签发的客户端令牌（只有 ``enforce`` 才走这里）"""
    user.is_active = False
    try:
        from backend.emby_server import models as em

        db.query(em.EmbyApiToken).filter(
            em.EmbyApiToken.user_id == user.id
        ).update({em.EmbyApiToken.is_revoked: True}, synchronize_session=False)
    except Exception as exc:  # noqa: BLE001 — 吊销失败不该让停用半途而废
        logger.warning("停用账号时吊销 Emby 令牌失败 user=%s: %s", user.id, exc)
    # 停用也进登录日志：这是「为什么这个号突然登不上」的答案
    try:
        from backend.authlog import record_event

        record_event(
            db, username=user.username, user_id=user.id, success=False,
            reason="share_guard", detail=detail[:255], commit=False,
        )
    except Exception:  # noqa: BLE001
        logger.warning("防共享停用账号时写安全日志失败 user=%s", user.id)
    if commit:
        db.commit()


# ==================== 1. 跨城市行为轨迹 ====================


def _latest_travel(db: Session, user_id: int) -> Optional[models.ShareGuardEvent]:
    return (
        db.query(models.ShareGuardEvent)
        .filter(
            models.ShareGuardEvent.user_id == user_id,
            models.ShareGuardEvent.kind == KIND_TRAVEL,
        )
        .order_by(models.ShareGuardEvent.created_at.desc())
        .first()
    )


def note_activity(
    db: Session,
    user: models.WebUser,
    ip: Optional[str],
    *,
    commit: bool = True,
) -> Optional[dict]:
    """登录 / 开始播放时记一笔轨迹并判定是否跨城市。

    返回 ``None`` = 没发生异常；异常时返回
    ``{"kind", "action", "region", "prev_region", "detail", "blocked"}``，
    ``blocked=True`` 表示账号已被停用，调用方应拒绝本次请求。
    """
    action = travel_action(db)
    # 开关与管理员豁免先判：未开启时这一步只有一次热读缓存，不会碰地理库
    if action == "off" or getattr(user, "is_staff", False):
        return None

    region = city_of(db, ip)
    if not region:
        # 没配地理能力 / 查不到：不知道在哪，就不判定
        return None

    window = travel_window_minutes(db)
    now = datetime.now()
    previous = _latest_travel(db, user.id)
    prev_region = (previous.region or "") if previous else ""
    last_seen = previous.created_at if previous else None
    in_window = bool(last_seen and last_seen >= now - timedelta(minutes=window))

    # 没异常（首次出现 / 同一城市 / 早于窗口）：只把基线往前推
    if not in_window or prev_region == region:
        # 同一城市且还在窗口内 → 节流：客户端每 10 秒上报一次，不节流表会写满
        if previous is not None and prev_region == region and in_window:
            return None
        _add_event(
            db, user=user, kind=KIND_TRAVEL, action=action, ip=ip, region=region,
            detail=f"当前所在：{region}", is_baseline=True, commit=commit,
        )
        return None

    # 窗口内换了城市 = 异常
    gap = int((now - last_seen).total_seconds() // 60)
    detail = (
        f"{window} 分钟内出现在两个城市：{prev_region} → {region}"
        f"（间隔 {gap} 分钟，IP {ip or '—'}）"
    )
    _add_event(
        db, user=user, kind=KIND_TRAVEL, action=action, ip=ip, region=region,
        prev_region=prev_region, detail=detail, commit=commit,
    )
    if action in ("alert", "enforce") and _should_notify(user.id, KIND_TRAVEL, now):
        _notify_staff(
            db,
            f"防共享：账号「{user.username}」疑似跨城市使用",
            f"{detail}。处置档位：{ACTION_LABELS[action]}。"
            f"可在后台「用户与账号 → 防共享」调整。",
            commit=commit,
        )
    if action == "enforce":
        _disable_user(db, user, detail=detail, commit=commit)
    return {"kind": KIND_TRAVEL, "action": action, "region": region,
            "prev_region": prev_region, "detail": detail, "blocked": action == "enforce"}


# ==================== 2. 同播检测 ====================


def _other_live_sessions(db: Session, user_id: int, exclude_key: str) -> list:
    from backend.emby_server import models as em

    return (
        db.query(em.PlaybackSession)
        .filter(
            em.PlaybackSession.user_id == user_id,
            em.PlaybackSession.ended_at.is_(None),
            em.PlaybackSession.session_key != exclude_key,
        )
        .order_by(em.PlaybackSession.start_time.asc())
        .all()
    )


def active_session_count(db: Session, user_id: int, *, exclude_key: Optional[str] = None) -> int:
    """该账号当前未结束的播放会话数（``exclude_key`` 那条不算在内）"""
    from backend.emby_server import models as em

    query = db.query(em.PlaybackSession).filter(
        em.PlaybackSession.user_id == user_id,
        em.PlaybackSession.ended_at.is_(None),
    )
    if exclude_key:
        query = query.filter(em.PlaybackSession.session_key != exclude_key)
    return int(query.count() or 0)


def note_session(
    db: Session,
    user: models.WebUser,
    session_key: str,
    ip: Optional[str],
    *,
    commit: bool = True,
) -> Optional[dict]:
    """新建播放会话时判定是否同播。

    返回 ``None`` = 没异常；异常时 ``blocked=True`` 表示应拦下这条会话
    （调用方不要写库，直接拒绝）。
    """
    action = concurrent_action(db)
    if action == "off" or getattr(user, "is_staff", False):
        return None

    # 串行化同一账号的准入：「数活会话 → 判上限」与调用方紧接着的
    # 「建会话行 → commit」必须是原子的，否则并发两路新建会话会同时看到
    # others < limit 而双双放行（check-then-act）。锁由本事务持有到提交/回滚；
    # 懒导入：顶层引 economy 有循环导入风险（本文件已有 geoip 等懒导入先例）。
    from backend.api.economy import lock_user_row
    lock_user_row(db, user.id)

    limit = concurrent_limit(db)
    others = _other_live_sessions(db, user.id, session_key)
    # 上限 N = 允许 N 路并发。第 N+1 路才是同播，所以 others >= limit 才算超
    if len(others) < limit:
        return None

    region = city_of(db, ip)
    first = others[0]
    where = " ".join(
        str(p) for p in (
            first.client_name or "", first.device_name or "", first.remote_addr or "",
        ) if p
    ) or "另一台设备"
    detail = (
        f"同时播放 {len(others) + 1} 路（上限 {limit}）；"
        f"已有一条来自 {where} 的会话（{region or '归属地未知'}）"
    )
    _add_event(
        db, user=user, kind=KIND_CONCURRENT, action=action, ip=ip, region=region,
        sessions=len(others) + 1, detail=detail, commit=commit,
    )
    if action in ("alert", "enforce") and _should_notify(user.id, KIND_CONCURRENT, datetime.now()):
        _notify_staff(
            db,
            f"防共享：账号「{user.username}」疑似同时多人观看",
            f"{detail}。处置档位：{ACTION_LABELS[action]}。"
            f"可在后台「用户与账号 → 防共享」调整。",
            commit=commit,
        )
    return {"kind": KIND_CONCURRENT, "action": action, "region": region,
            "sessions": len(others) + 1, "detail": detail, "blocked": action == "enforce"}


# ==================== 查询与清理 ====================


def purge_old(db: Session, days: Optional[int] = None) -> int:
    """按保留天数清理（``days=0`` = 不清理）。返回删除条数"""
    if days is None:
        days = retention_days(db)
    if days <= 0:
        return 0
    cutoff = datetime.now() - timedelta(days=days)
    deleted = (
        db.query(models.ShareGuardEvent)
        .filter(models.ShareGuardEvent.created_at < cutoff)
        .delete(synchronize_session=False)
    )
    db.commit()
    return int(deleted or 0)


def event_dto(row: models.ShareGuardEvent) -> dict:
    return {
        "id": row.id,
        "user_id": row.user_id,
        "username": row.username or "",
        "kind": row.kind,
        "kind_label": KIND_LABELS.get(row.kind, row.kind),
        "action": row.action,
        "action_label": ACTION_LABELS.get(row.action, row.action),
        "is_baseline": bool(row.is_baseline),
        "ip": row.ip or "",
        "region": row.region or "",
        "prev_region": row.prev_region or "",
        "sessions": row.sessions or 0,
        "detail": row.detail or "",
        "created_at": row.created_at.isoformat() if row.created_at else None,
    }


__all__ = [
    "ACTIONS", "ACTION_LABELS", "KIND_LABELS", "DEFAULTS",
    "KIND_TRAVEL", "KIND_CONCURRENT",
    "CONFIG_TRAVEL_ACTION", "CONFIG_TRAVEL_WINDOW",
    "CONFIG_CONCURRENT_ACTION", "CONFIG_CONCURRENT_LIMIT", "CONFIG_RETENTION_DAYS",
    "travel_action", "travel_window_minutes", "concurrent_action", "concurrent_limit",
    "retention_days", "policy_payload", "write_policy", "city_of",
    "note_activity", "note_session", "active_session_count", "purge_old", "event_dto",
]