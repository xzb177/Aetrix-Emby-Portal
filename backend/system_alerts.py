"""系统告警：后台指标越线时通知管理员（v2.56.x）

## 为什么要做

probe / enrich / scan 等后台 worker 的关键指标越线时，以前一个通知都不发，
管理员只能靠定时看监控发现。这是「可观测底座」的下半场：**看得见 + 喊得出来**。

## 设计原则

1. **总开关默认关**：``SYSTEM_ALERTS_ENABLED=1`` 或
   ``SystemConfig[system_alerts_enabled]=1`` 才工作；关闭时定时检查直接跳过。
2. **阈值/渠道/接收人/冷却全部可配**：规则存 ``AlertRule`` 表（event_type 非空的
   事件型规则），首次运行时按内置默认播种，之后管理员可在后台改——不写死业务数值。
3. **冷却去重**：只对"发送成功过"的告警去重——发送失败的不进入冷却，
   下轮继续重试（R16 P1-2：一次投递失败不再导致整个冷却期失声）。
4. **自动恢复**：指标回到阈值内，open 的告警自动标 resolved，告警列表不发霉。
5. **评估只读探针**：复用 ``observability.probe_subsystems``（聚合 COUNT，
   不打 Drive API，不抢主链路）。单个探针异常只跳过该事件，不影响其他。
6. **夜间模式不误报**：enrich 开了夜间模式时，白天零产出是预期的，
   ``enrich_stalled`` 在窗口外不触发。

## 事件类型

- ``probe_queue_high``：探测队列堆积（pending_ready >= 阈值）
- ``enrich_stalled``：补全停滞（有积压且长时间无产出）
- ``disk_full``：磁盘余量低于阈值
- ``sa_quota_low``：SA 熔断中或 SA 未配置
- ``scan_failed``：有库扫描失败
- ``api_error_high``：API 错误率超阈值
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import threading
from datetime import datetime, timedelta
from typing import Any, Optional
from backend.emby_server.env_util import env_float, env_int

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 事件类型常量
# ---------------------------------------------------------------------------

EVT_PROBE_QUEUE_HIGH = "probe_queue_high"
EVT_ENRICH_STALLED = "enrich_stalled"
EVT_DISK_FULL = "disk_full"
EVT_SA_QUOTA_LOW = "sa_quota_low"
EVT_SCAN_FAILED = "scan_failed"
EVT_API_ERROR_HIGH = "api_error_high"

ALL_EVENTS = (
    EVT_PROBE_QUEUE_HIGH,
    EVT_ENRICH_STALLED,
    EVT_DISK_FULL,
    EVT_SA_QUOTA_LOW,
    EVT_SCAN_FAILED,
    EVT_API_ERROR_HIGH,
)

# ---------------------------------------------------------------------------
# 开关与默认阈值（全部可配置：环境变量是出厂默认，DB 规则可覆盖）
# ---------------------------------------------------------------------------

MASTER_SWITCH_ENV = "SYSTEM_ALERTS_ENABLED"
MASTER_SWITCH_KEY = "system_alerts_enabled"
CHECK_INTERVAL_ENV = "ALERT_CHECK_INTERVAL_SEC"
DEFAULT_CHECK_INTERVAL_SEC = 600  # 10 分钟
# 检查间隔的 DB 配置键（R16 P1-1：后台可配，热更新，不再只读环境变量）
INTERVAL_KEY = "alert_check_interval_sec"


def is_truthy(v: Any) -> bool:
    return str(v or "").strip().lower() not in {"", "0", "false", "no", "off"}


def current_interval_sec(db=None) -> int:
    """当前生效的检查间隔（秒），优先级：DB 配置 > 环境变量 > 默认 600。

    钳制到 60~86400。读库失败时回落到环境变量，不挡主流程。
    """
    iv = env_int(CHECK_INTERVAL_ENV, DEFAULT_CHECK_INTERVAL_SEC)
    if db is not None:
        try:
            from backend import models
            row = db.query(models.SystemConfig).filter(
                models.SystemConfig.key == INTERVAL_KEY).first()
            if row is not None and str(row.value or "").strip():
                iv = int(float(row.value))
        except Exception:  # noqa: BLE001 — 读库失败按环境变量处理
            logger.debug("读取告警检查间隔失败，按环境变量处理", exc_info=True)
    return max(60, min(86400, iv))


def enabled(db=None) -> bool:
    """总开关：环境变量或 SystemConfig，任一开即开。默认关。"""
    if is_truthy(os.getenv(MASTER_SWITCH_ENV)):
        return True
    if db is not None:
        try:
            from backend import models
            row = db.query(models.SystemConfig).filter(
                models.SystemConfig.key == MASTER_SWITCH_KEY).first()
            if row is not None and is_truthy(row.value):
                return True
        except Exception:  # noqa: BLE001 — 读库失败按关处理，不挡主流程
            logger.debug("读取告警总开关失败，按关闭处理", exc_info=True)
    return False


# 内置默认规则：event_type → 默认配置（播种进 AlertRule 表，之后以 DB 为准）
DEFAULT_RULES: dict[str, dict[str, Any]] = {
    EVT_PROBE_QUEUE_HIGH: {
        "name": "探测队列堆积",
        "threshold": lambda: float(env_int("ALERT_PROBE_QUEUE_HIGH", 5000)),
        "severity": "warning",
        "channels": ["in_app", "telegram"],
        "cooldown_sec": 21600,
    },
    EVT_ENRICH_STALLED: {
        "name": "补全停滞",
        "threshold": lambda: float(env_int("ALERT_ENRICH_IDLE_SEC", 7200)),
        "severity": "warning",
        "channels": ["in_app", "telegram"],
        "cooldown_sec": 21600,
    },
    EVT_DISK_FULL: {
        "name": "磁盘空间不足",
        "threshold": lambda: env_float("ALERT_DISK_FREE_PCT", 10.0),
        "severity": "critical",
        "channels": ["in_app", "telegram"],
        "cooldown_sec": 86400,
    },
    EVT_SA_QUOTA_LOW: {
        "name": "SA 配额告警",
        "threshold": lambda: 0.0,  # 条件型：有熔断或 SA 为 0 即触发
        "severity": "warning",
        "channels": ["in_app", "telegram"],
        "cooldown_sec": 3600,
    },
    EVT_SCAN_FAILED: {
        "name": "扫描失败",
        "threshold": lambda: 0.0,  # 条件型：failed > 0 即触发
        "severity": "warning",
        "channels": ["in_app", "telegram"],
        "cooldown_sec": 21600,
    },
    EVT_API_ERROR_HIGH: {
        "name": "API 错误率过高",
        "threshold": lambda: env_float("ALERT_API_ERROR_RATIO", 0.05),
        "severity": "warning",
        "channels": ["in_app", "telegram"],
        "cooldown_sec": 3600,
    },
}

# enrich_stalled 的积压下限：积压太少时不算"停滞"（环境变量可配）
def _enrich_backlog_floor() -> int:
    return env_int("ALERT_ENRICH_BACKLOG_FLOOR", 1000)


# ---------------------------------------------------------------------------
# 规则播种与读取（DB 是唯一真相源）
# ---------------------------------------------------------------------------

def ensure_default_rules(db) -> int:
    """幂等播种：DB 里没有该 event_type 的规则时，按内置默认插入一条。

    返回新插入的条数。legacy 指标型规则（event_type 为空）不受影响。

    R16 P2-2：逐条 add+commit 并捕获 IntegrityError（`event_type` 有唯一约束），
    防"先查后插"竞态下两个进程同时插入重复规则。R16 P3-14：`notification_channels`
    是 JSON 列，直接写 list（不再 json.dumps 后再被 SQLAlchemy 包一层）。
    """
    from sqlalchemy.exc import IntegrityError

    from backend import models
    inserted = 0
    for evt, spec in DEFAULT_RULES.items():
        exists = db.query(models.AlertRule).filter(
            models.AlertRule.event_type == evt).first()
        if exists is not None:
            continue
        db.add(models.AlertRule(
            name=spec["name"],
            metric_name=f"event:{evt}",
            condition="gte",
            threshold=float(spec["threshold"]()),
            severity=spec["severity"],
            notification_channels=list(spec["channels"]),
            event_type=evt,
            recipients=json.dumps({"staff": True}),
            cooldown_sec=spec["cooldown_sec"],
            is_active=True,
        ))
        try:
            db.commit()
        except IntegrityError:
            # 并发下另一进程/线程刚好先插了 → 回滚后重查，有就算（0 插入）
            db.rollback()
            again = db.query(models.AlertRule).filter(
                models.AlertRule.event_type == evt).first()
            if again is None:
                raise
            continue
        inserted += 1
    if inserted:
        logger.info("系统告警默认规则已播种 %d 条", inserted)
    return inserted


def _get_rule(db, event_type: str):
    from backend import models
    return db.query(models.AlertRule).filter(
        models.AlertRule.event_type == event_type,
        models.AlertRule.is_active == True,  # noqa: E712
    ).first()


def _rule_channels(rule) -> list[str]:
    raw = getattr(rule, "notification_channels", None)
    if not raw:
        return ["in_app"]
    if isinstance(raw, list):
        return [str(c) for c in raw]
    try:
        parsed = json.loads(raw)
        return [str(c) for c in parsed] if isinstance(parsed, list) else ["in_app"]
    except Exception:  # noqa: BLE001
        return ["in_app"]


def _rule_recipients(rule) -> dict:
    raw = getattr(rule, "recipients", None)
    if not raw:
        return {"staff": True}
    if isinstance(raw, dict):
        return raw
    try:
        parsed = json.loads(raw)
        return parsed if isinstance(parsed, dict) else {"staff": True}
    except Exception:  # noqa: BLE001
        return {"staff": True}


def _staff_user_ids(db) -> list[int]:
    from backend import models
    rows = db.query(models.WebUser.id).filter(
        models.WebUser.is_staff == True,  # noqa: E712
        models.WebUser.is_active == True,  # noqa: E712
    ).all()
    return [r[0] for r in rows]


def _recipient_ids(db, rule) -> list[int]:
    spec = _rule_recipients(rule)
    if spec.get("staff"):
        return _staff_user_ids(db)
    ids = spec.get("user_ids") or []
    return [int(i) for i in ids if str(i).isdigit()]


# ---------------------------------------------------------------------------
# 统一告警入口
# ---------------------------------------------------------------------------

async def notify_alert(
    event_type: str,
    title: str,
    message: str,
    *,
    severity: Optional[str] = None,
    metrics: Optional[dict] = None,
    channels: Optional[list[str]] = None,
    cooldown_sec: Optional[int] = None,
    db=None,
    _ensure_rules: bool = True,
) -> bool:
    """系统告警统一入口。

    - 总开关关闭 → 直接返回 False（静默跳过）。
    - 冷却期内同一规则已有**发送成功**的 open/acknowledged 告警 → 去重跳过（防轰炸）；
      发送失败的不进入冷却，下轮继续重试（R16 P1-2）。
    - 渠道走现有三渠道 + webhook；接收人默认全部启用中的管理员。
    - 落库时记 notified_ok（是否至少一个渠道送达），管理员在面板能区分
      "已送达"与"发送失败"（R16 P1-2）。

    返回 True 表示至少一个渠道发送成功。
    """
    from backend import models
    from backend.database import SessionLocal

    own = db is None
    if own:
        db = SessionLocal()
    try:
        if not enabled(db):
            logger.debug("系统告警总开关关闭，跳过 %s", event_type)
            return False

        if _ensure_rules:  # R16 P3-8：check_all 轮首已播种时可跳过
            ensure_default_rules(db)
        rule = _get_rule(db, event_type)
        if rule is None:
            logger.warning("事件 %s 无可用规则，跳过", event_type)
            return False

        eff_severity = severity or rule.severity or "warning"
        eff_channels = channels or _rule_channels(rule)
        eff_cooldown = cooldown_sec if cooldown_sec is not None else (
            rule.cooldown_sec or 3600)

        # 冷却去重：只认发送成功过的记录（open/acknowledged）→ 跳过；
        # R16 P1-2：发送失败的不去重，下轮重试；
        # R16 P2-3：ack 确认后冷却期内也不再重复打扰。
        cutoff = datetime.now() - timedelta(seconds=max(60, int(eff_cooldown)))
        recent = db.query(models.Alert).filter(
            models.Alert.rule_id == rule.id,
            models.Alert.status.in_(["open", "acknowledged"]),
            models.Alert.notified_ok == True,  # noqa: E712
            models.Alert.created_at >= cutoff,
        ).first()
        if recent is not None:
            logger.info("告警 %s 在冷却期内已发送过（%s），去重跳过",
                        event_type, recent.created_at)
            return False

        recipients = _recipient_ids(db, rule)
        full_message = message
        if metrics:
            compact = ", ".join(f"{k}={v}" for k, v in list(metrics.items())[:8])
            full_message = f"{message}\n指标：{compact}"

        results: dict[str, bool] = {}
        sent_any = False

        # 站内信：复用现有 notify_staff_users（落库 + WebSocket 实时推送）
        if "in_app" in eff_channels and recipients:
            try:
                from backend import notifications as _ntf
                n = await _ntf.notify_staff_users(
                    db, title, full_message,
                    message_type=f"alert.{event_type}")
                results["in_app"] = n > 0
                sent_any = sent_any or n > 0
            except Exception as exc:  # noqa: BLE001
                logger.error("告警站内信发送失败: %s", exc)
                results["in_app"] = False

        # 邮件 / Telegram：逐接收人发送（未绑定渠道的用户会优雅跳过）
        svc = None
        for ch in ("email", "telegram"):
            if ch not in eff_channels or not recipients:
                continue
            try:
                from backend import notifications as _ntf
                svc = svc or _ntf.get_notification_service()
                if ch not in svc.channels:
                    results[ch] = False
                    continue
                ok_any = False
                for uid in recipients:
                    try:
                        r = await svc.send(
                            user_id=uid, title=title, content=full_message,
                            channels=[ch], message_type=f"alert.{event_type}",
                            db=db)
                        ok_any = ok_any or bool(r.get(ch))
                    except Exception:  # noqa: BLE001 — 单个接收人失败不影响其他
                        logger.warning("告警 %s 发送给用户 %s 失败",
                                       ch, uid, exc_info=True)
                results[ch] = ok_any
                sent_any = sent_any or ok_any
            except Exception as exc:  # noqa: BLE001
                logger.error("告警 %s 渠道发送失败: %s", ch, exc)
                results[ch] = False

        # webhook：广播到 URL（不是发给某用户），只发一次
        if "webhook" in eff_channels:
            try:
                from backend import notifications as _ntf
                svc = svc or _ntf.get_notification_service()
                ch_obj = svc.channels.get("webhook")
                if ch_obj is None or not getattr(ch_obj, "enabled", False):
                    results["webhook"] = False
                else:
                    ok = await ch_obj.send(
                        0, title, full_message,
                        data={"event_type": event_type,
                              "severity": eff_severity,
                              "metrics": metrics or {}})
                    results["webhook"] = ok
                    sent_any = sent_any or ok
            except Exception as exc:  # noqa: BLE001
                logger.error("告警 webhook 发送失败: %s", exc)
                results["webhook"] = False

        # 落告警记录（供后台查看 / 去重 / 自动恢复；notified_ok 区分送达与否）
        try:
            db.add(models.Alert(
                rule_id=rule.id,
                severity=eff_severity,
                title=title,
                message=full_message,
                status="open",
                notified_ok=sent_any,  # R16 P1-2：失败的不参与冷却去重
            ))
            db.commit()
        except Exception:  # noqa: BLE001 — 记录失败不影响已发出的通知
            db.rollback()
            logger.warning("告警记录落库失败", exc_info=True)

        logger.info("系统告警 %s 已触发（渠道 %s）", event_type, results)
        return sent_any
    finally:
        if own:
            try:
                db.close()
            except Exception:  # noqa: BLE001
                pass


def resolve_alerts(db, event_type: str) -> int:
    """指标恢复时，把该事件的 open/acknowledged 告警标为 resolved。

    R16 P2-3：acknowledged 的也要自动恢复，生命周期闭环（确认后条件持续
    不再打扰，条件恢复后自动 resolved）。返回处理条数。
    """
    from backend import models
    rule = _get_rule(db, event_type)
    if rule is None:
        return 0
    now = datetime.now()
    rows = db.query(models.Alert).filter(
        models.Alert.rule_id == rule.id,
        models.Alert.status.in_(["open", "acknowledged"]),
    ).all()
    for r in rows:
        r.status = "resolved"
        r.resolved_at = now
    if rows:
        db.commit()
        logger.info("系统告警 %s 自动恢复 %d 条", event_type, len(rows))
    return len(rows)


# ---------------------------------------------------------------------------
# 定时检查：评估探针 → 越线触发 / 恢复自动解决
# ---------------------------------------------------------------------------

def _enrich_night_paused(db) -> bool:
    """enrich 夜间模式且当前不在窗口内 → 白天零产出是预期的，不判停滞。"""
    try:
        # R16 P3-2：走 enrich_worker 的公开包装，不再跨模块调私有函数
        from backend.emby_server import enrich_worker
        cfg = enrich_worker.get_episode_night_config(db)
        return bool(cfg.get("enabled")) and not bool(cfg.get("in_window"))
    except Exception:  # noqa: BLE001 — 读不到配置按"不暂停"处理（宁可误报不漏报）
        return False


def _check_probe_queue(subs: dict, rule) -> Optional[tuple[str, str, dict]]:
    m = (subs.get("probe") or {}).get("metrics") or {}
    pending = int(m.get("pending_ready") or 0)
    thr = float(rule.threshold or 5000)
    if pending >= thr:
        return (f"探测队列堆积 {pending} 条（阈值 {thr:g}）",
                f"待探测 {pending} 条，排队重试 {m.get('retrying', 0)} 条，"
                f"预计 {m.get('eta_hours') or '?'} 小时消化。请检查 worker 是否正常运行。",
                {"pending_ready": pending, "retrying": m.get("retrying", 0),
                 "eta_hours": m.get("eta_hours")})
    return None


def _check_enrich_stalled(subs: dict, rule, db) -> Optional[tuple[str, str, dict]]:
    if _enrich_night_paused(db):
        return None  # 夜间模式的白天：零产出是预期的
    m = (subs.get("enrich") or {}).get("metrics") or {}
    backlog = int(m.get("backlog") or 0)
    idle_sec = m.get("idle_sec")
    idle_thr = float(rule.threshold or 7200)
    floor = _enrich_backlog_floor()
    if backlog >= floor and idle_sec is not None and float(idle_sec) >= idle_thr:
        hrs = float(idle_sec) / 3600
        return (f"补全停滞：{backlog} 条积压、{hrs:.1f} 小时无产出",
                f"补全队列积压 {backlog} 条，已 {hrs:.1f} 小时没有产出"
                f"（阈值 {idle_thr / 3600:g} 小时）。请检查 enrich worker。",
                {"backlog": backlog, "idle_sec": idle_sec,
                 "done_per_min": m.get("done_per_min", 0)})
    return None


def _check_disk(subs: dict, rule) -> Optional[tuple[str, str, dict]]:
    m = (subs.get("disk") or {}).get("metrics") or {}
    free_pct = m.get("disk_free_percent")
    if free_pct is None:
        return None
    # R16 P3-6：threshold=0 不再被 falsy 吞掉回落 10.0（只有 None 才回落）
    thr = 10.0 if rule.threshold is None else float(rule.threshold)
    if float(free_pct) < thr:
        return (f"磁盘空间不足：仅剩 {float(free_pct):.1f}%",
                f"磁盘余量 {float(free_pct):.1f}%（阈值 {thr:g}%）。请及时清理。",
                {"disk_free_percent": free_pct})
    return None


def _check_sa_quota(subs: dict, rule) -> Optional[tuple[str, str, dict]]:
    pm = (subs.get("probe") or {}).get("metrics") or {}
    sm = (subs.get("sa_quota") or {}).get("metrics") or {}
    breakers = pm.get("open_breakers") or []
    sa_count = sm.get("sa_count")
    if breakers:
        return (f"SA 熔断中：{len(breakers)} 个挂载",
                f"以下挂载的 SA 被熔断：{'、'.join(breakers)}。"
                f"可能触发了 Google 限流，请检查配额。",
                {"open_breakers": breakers, "sa_count": sa_count})
    if sa_count == 0:
        return ("未配置 SA 账号",
                "一个可用的 SA 账号都没有，Drive 相关功能将不可用。",
                {"sa_count": 0})
    return None


def _check_scan_failed(subs: dict, rule) -> Optional[tuple[str, str, dict]]:
    m = (subs.get("scan") or {}).get("metrics") or {}
    failed = int(m.get("failed") or 0)
    if failed > 0:
        names = "、".join(x.get("name", "?") for x in (m.get("last_failed") or [])[:3])
        return (f"{failed} 个库扫描失败" + (f"：{names}" if names else ""),
                f"{failed} 个媒体库扫描失败"
                + (f"（{names}）" if names else "")
                + "。请查看扫描日志排查。",
                {"failed": failed})
    return None


def _check_api_error(subs: dict, rule) -> Optional[tuple[str, str, dict]]:
    m = (subs.get("api") or {}).get("metrics") or {}
    ratio = m.get("global_error_ratio")
    if ratio is None:
        return None
    thr = float(rule.threshold if rule.threshold else 0.05)
    if float(ratio) >= thr:
        # R16 P2-5：探针返回了 global_p99_ms=None 时 f"{None:.0f}" 会抛
        # TypeError → 整个事件静默漏报；用 or 0 防御
        p99 = m.get("global_p99_ms") or 0
        return (f"API 错误率 {float(ratio):.1%}（阈值 {thr:.1%}）",
                f"API 错误率 {float(ratio):.1%}，P99 延迟 "
                f"{p99:.0f}ms。请检查后端日志。",
                {"global_error_ratio": ratio,
                 "global_p99_ms": m.get("global_p99_ms", 0)})
    return None


# 事件 → 检查函数（新增事件类型只加一行）
_CHECKERS = {
    EVT_PROBE_QUEUE_HIGH: _check_probe_queue,
    EVT_ENRICH_STALLED: _check_enrich_stalled,
    EVT_DISK_FULL: _check_disk,
    EVT_SA_QUOTA_LOW: _check_sa_quota,
    EVT_SCAN_FAILED: _check_scan_failed,
    EVT_API_ERROR_HIGH: _check_api_error,
}


def check_all(db=None) -> dict:
    """跑一轮告警检查：评估各事件 → 越线触发 / 恢复自动解决。

    返回 {"fired": [...], "resolved": [...], "skipped": [...]}。
    所有评估只读 observability 探针；单个事件异常不影响其他。
    """
    from backend.database import SessionLocal

    own = db is None
    if own:
        db = SessionLocal()
    result: dict[str, list] = {"fired": [], "resolved": [], "skipped": []}
    try:
        if not enabled(db):
            result["skipped"].append("disabled")
            return result

        ensure_default_rules(db)

        try:
            from backend.emby_server import observability
            subs = observability.probe_subsystems(db)
        except Exception as exc:  # noqa: BLE001 — 探针整体失败就整轮跳过
            logger.warning("告警检查：探针执行失败，整轮跳过: %s", exc)
            result["skipped"].append("probes_failed")
            return result

        # R16 P3-7：一轮只建一个事件循环复用到底（asyncio.run 不能嵌套，
        # 将来若有 async 端点直接调 check_all 会抛 RuntimeError）
        loop = asyncio.new_event_loop()
        try:
            for event_type, checker in _CHECKERS.items():
                try:
                    rule = _get_rule(db, event_type)
                    if rule is None:
                        result["skipped"].append(event_type)
                        continue
                    if event_type == EVT_ENRICH_STALLED:
                        hit = checker(subs, rule, db)
                    else:
                        hit = checker(subs, rule)
                    if hit:
                        title, message, metrics = hit
                        # R16 P3-8：轮首已播种，notify_alert 不再重复 ensure
                        fired = loop.run_until_complete(notify_alert(
                            event_type, title, message,
                            severity=rule.severity, metrics=metrics,
                            db=db, _ensure_rules=False))
                        result["fired" if fired else "skipped"].append(event_type)
                    else:
                        n = resolve_alerts(db, event_type)
                        if n:
                            result["resolved"].append(f"{event_type}:{n}")
                except Exception:  # noqa: BLE001 — 单个事件异常不影响其他事件
                    logger.warning("告警检查事件 %s 异常", event_type, exc_info=True)
                    result["skipped"].append(event_type)
        finally:
            loop.close()
        return result
    finally:
        if own:
            try:
                db.close()
            except Exception:  # noqa: BLE001
                pass


# ---------------------------------------------------------------------------
# 定时调度（daemon 线程，随进程退出；只在面板 EM 侧启动）
# ---------------------------------------------------------------------------

_SCHEDULER_STARTED = False
_SCHEDULER_LOCK = threading.Lock()
# R16 P3-13：模块级事件做 wait（不再每轮新建），将来可做优雅退出
_STOP_EVENT = threading.Event()


def start_alert_scheduler(interval_sec: Optional[int] = None) -> bool:
    """启动系统告警定时检查（同一进程只启动一次；daemon，随进程退出）。

    只在面板（EM）上启动：EA 是出流单元，不该自己发告警（多台 EA 会重复打扰）。
    总开关默认关：没开时线程空转等待，不打扰任何现有行为。

    R16 P1-1：`interval_sec` 显式传参仍优先；否则每轮检查前重读
    `current_interval_sec(db)`（DB 配置 > 环境变量 > 默认 600），后台改了
    间隔不用重启进程即生效（热更新）。R16 P0-1：worker.py 的 worker 进程
    也会调这里（生产拓扑下 main.py 的 `if not _is_api_role` 分支在 api
    进程被跳过，调度器原本永不启动）。
    """
    global _SCHEDULER_STARTED
    with _SCHEDULER_LOCK:
        if _SCHEDULER_STARTED:
            return False
        _SCHEDULER_STARTED = True

    fixed_interval = None
    if interval_sec is not None:
        fixed_interval = max(60, int(interval_sec))

    def _loop() -> None:
        from backend.database import SessionLocal
        while not _STOP_EVENT.is_set():
            # 热更新：每轮重读生效的检查间隔
            if fixed_interval is not None:
                interval = fixed_interval
            else:
                db_iv = SessionLocal()
                try:
                    interval = current_interval_sec(db_iv)
                finally:
                    try:
                        db_iv.close()
                    except Exception:  # noqa: BLE001
                        pass
            if _STOP_EVENT.wait(interval):
                break
            db = SessionLocal()
            try:
                summary = check_all(db)
                if summary["fired"] or summary["resolved"]:
                    logger.info("系统告警检查完成: 触发 %s / 恢复 %s",
                                summary["fired"], summary["resolved"])
            except Exception:  # noqa: BLE001 — 检查异常不杀线程
                logger.warning("系统告警检查异常", exc_info=True)
            finally:
                try:
                    db.close()
                except Exception:  # noqa: BLE001
                    pass

    t = threading.Thread(target=_loop, name="system-alerts", daemon=True)
    t.start()
    logger.info("系统告警定时检查已启动（总开关默认关；间隔热更新）")
    return True


__all__ = [
    "EVT_PROBE_QUEUE_HIGH", "EVT_ENRICH_STALLED", "EVT_DISK_FULL",
    "EVT_SA_QUOTA_LOW", "EVT_SCAN_FAILED", "EVT_API_ERROR_HIGH",
    "ALL_EVENTS", "DEFAULT_RULES",
    # r42：env_int 统一走 backend.emby_server.env_util（原本地实现已删）
    "is_truthy",
    "INTERVAL_KEY", "current_interval_sec",
    "enabled", "ensure_default_rules",
    "notify_alert", "resolve_alerts", "check_all",
    "start_alert_scheduler",
]
