# -*- coding: utf-8 -*-
"""谷歌 Drive API 报错原因落库（2026-11-08 盘点治本）。

病根：Drive 报错的 reason（userRateLimitExceeded /
downloadQuotaExceeded / rateLimitExceeded 等）在代码多处被
解析用于分类判定（drive_changes 的 403 配额识别、drive_share
的退避判定、池熔断计数），但算完即弃、全站没有任何一张表
落下来——撞限时只知道「撞了几次」，分不清撞的是哪种限额，
后续削减调用（沉淀预算/探测复用等）改完也无从验收。

本模块是 reason 的唯一落点（照 transfer_decisions 的单落点
纪律，不许第二处另建统计）：

- 中央出口接线（403/429 响应统一经 note_drive_response）：
  drive_changes._api_get、transfer_variant.DriveApiClient
  （含 UserDriveApiClient 继承与 _RawTokenClient）、
  drive_share.ensure_shared、byos_scan 的 Drive 列举口。
  streaming 中继数据面与 first_play 的 usercontent 探测不在
  此列（前者是字节面、错误体多非标准 JSON；后者不是 Drive
  API 本体），需要时再按同口径扩。
- **铁律：落库不许拖垮主请求。** note_* 用独立会话写，
  任何失败（DB 挂、解析炸）一律静默丢弃并计数返回 False，
  绝不向上抛、绝不改变主请求的返回值/异常/重试行为。
- 账号标识只存不可逆短哈希（sha256 前 12 位）：SA/用户令牌、
  drive_id、绑定令牌一律先哈希再落，邮箱/token 原文绝不进表。

保留期：maintenance.janitor_tick 周期调 purge_expired_events
清理过期行。保留天数 SystemConfig
``drive_api_error_events_retention_days`` 可配，默认 14，
钳 [1, 365]。

管理端只读面：error_summary 供
GET /api/admin/tasks/drive-error-summary（admin_tasks），
近 N 天按 reason / 天计数。
"""
from __future__ import annotations

import hashlib
import inspect
import logging
import threading
from datetime import datetime, timedelta
from typing import Optional

from sqlalchemy import func

from backend.database import SessionLocal
from backend.emby_server import models as em

logger = logging.getLogger(__name__)

#: 只落这两个状态：403（配额类藏在里面）与 429（限流本体）。
#: 401/404/5xx 各有既有处置与日志口径，不进本表。
RECORDED_STATUS_CODES = frozenset({403, 429})

#: 保留期配置键与默认值（天）
RETENTION_KEY = "drive_api_error_events_retention_days"
RETENTION_DEFAULT_DAYS = 14

#: 调用栈推断时跳过的模块（中央出口自身与本模块）：要的是
#: 「谁在调中央出口」，不是出口本身。
_CALLER_SKIP_MODULES = frozenset({
    "backend.emby_server.drive_error_events",
    "backend.emby_server.drive_changes",
    "backend.emby_server.transfer_variant",
    "backend.emby_server.drive_share",
    "backend.emby_server.byos_scan",
})

_STATS = {"noted": 0, "written": 0, "write_errors": 0}
_STATS_LOCK = threading.Lock()


def error_stats() -> dict:
    """落库流水计数（测试/观测用）：noted 尝试数、written
    落库数、write_errors 静默丢弃数。"""
    with _STATS_LOCK:
        return dict(_STATS)


def _bump(name: str) -> None:
    with _STATS_LOCK:
        _STATS[name] += 1


def _trim(value, limit: int) -> str:
    if value is None:
        return ""
    text = str(value)
    return text[:limit] if len(text) > limit else text


def account_hash_for(identifier) -> str:
    """账号标识 → 不可逆短哈希（sha256 前 12 位）。

    入参可以是令牌原文、drive_id 等任何账号标识字符串；
    只落哈希，原文绝不进表。空标识回空串。
    """
    if not identifier:
        return ""
    return hashlib.sha256(
        str(identifier).encode("utf-8")).hexdigest()[:12]


def parse_error_reason(resp) -> tuple[str, str]:
    """从谷歌错误响应体解析 (reason, domain)：取 errors[0]。

    解析失败（非 JSON / 无 errors / 结构异常）一律回 ("", "")，
    调用方照常落一行（reason 空串在聚合里就是「未解析」桶），
    绝不因解析失败丢掉这次撞限事件。resp 只需有 .json()。
    """
    try:
        body = resp.json()
    except Exception:  # noqa: BLE001 — 解析失败按未解析处理
        return "", ""
    try:
        if not isinstance(body, dict):
            return "", ""
        errors = (body.get("error") or {}).get("errors") or []
        if not errors or not isinstance(errors[0], dict):
            return "", ""
        first = errors[0]
        return (str(first.get("reason") or ""),
                str(first.get("domain") or ""))
    except Exception:  # noqa: BLE001
        return "", ""


def infer_caller() -> Optional[str]:
    """从调用栈推断来源模块短名：第一个不在跳过集里的
    backend.* 帧。推不到（测试直调、脚本直调）返回 None，
    调用方用各自的出口标签兜底。"""
    try:
        for frame_info in inspect.stack(0)[1:]:
            module = frame_info.frame.f_globals.get("__name__", "")
            if not module.startswith("backend."):
                continue
            if module in _CALLER_SKIP_MODULES:
                continue
            return module.rsplit(".", 1)[-1]
    except Exception:  # noqa: BLE001 — 推断失败不许影响落库
        return None
    return None


def caller_label(default: str) -> str:
    """调用方标签：栈推断优先，推不到用中央出口自己的标签。"""
    return infer_caller() or default


def note_drive_error(*, status_code: int, reason: str = "",
                     domain: str = "", caller: str = "unknown",
                     account: str = "") -> bool:
    """落一行 Drive 报错事件。成功 True；任何失败静默丢弃
    返回 False，绝不抛（见模块 docstring 铁律）。

    account 传账号标识原文（令牌/drive_id 等），本函数哈希
    后才落库；已是哈希也无妨（调用方一般传原文）。
    独立会话：不借调用方事务，落库失败不污染主流程。
    """
    _bump("noted")
    try:
        row = em.DriveApiErrorEvent(
            status_code=int(status_code),
            reason=_trim(reason, 80),
            domain=_trim(domain, 80),
            caller=_trim(caller, 80) or "unknown",
            account_hash=account_hash_for(account),
        )
    except Exception:  # noqa: BLE001 — 组装失败同样静默丢弃
        _bump("write_errors")
        logger.warning("Drive 报错事件组装失败 status=%s",
                       status_code, exc_info=True)
        return False
    db = None
    try:
        db = SessionLocal()
        db.add(row)
        db.commit()
    except Exception:  # noqa: BLE001 — 写失败静默丢弃，不挡主请求
        _bump("write_errors")
        logger.warning("Drive 报错事件写入失败 status=%s reason=%s",
                       status_code, reason, exc_info=True)
        if db is not None:
            try:
                db.rollback()
            except Exception:  # noqa: BLE001
                pass
        return False
    finally:
        if db is not None:
            try:
                db.close()
            except Exception:  # noqa: BLE001
                pass
    _bump("written")
    return True


def note_drive_response(resp, *, caller: str,
                        account: str = "") -> bool:
    """中央出口统一入口：响应是 403/429 才落库，其余直接
    返回 False 不记。resp 只需有 .status_code 与 .json()。

    本函数自己也绝不抛——解析/落库的异常都在 note_drive_error
    与 parse_error_reason 内吞掉，这里再兜一层，保证接线点
    可以无条件调用。
    """
    try:
        if int(getattr(resp, "status_code", 0) or 0) \
                not in RECORDED_STATUS_CODES:
            return False
        reason, domain = parse_error_reason(resp)
        return note_drive_error(
            status_code=int(resp.status_code), reason=reason,
            domain=domain, caller=caller, account=account)
    except Exception:  # noqa: BLE001 — 观测点永不影响主请求
        _bump("write_errors")
        logger.warning("Drive 报错事件记录失败", exc_info=True)
        return False


# ---------------------------------------------------------------------------
# 保留期与只读面
# ---------------------------------------------------------------------------

def _config_int(db, key: str) -> Optional[int]:
    """SystemConfig 整数热读：无行/非法/异常一律 None（回退
    默认）。与 transfer_decisions._config_int 同口径。"""
    if db is None:
        return None
    try:
        from backend.integrations import store
        raw = store.get_value(db, key, "")
    except Exception:  # noqa: BLE001
        return None
    try:
        value = int(str(raw).strip())
    except (TypeError, ValueError):
        return None
    return value if value > 0 else None


def retention_days(db) -> int:
    """保留天数：SystemConfig > 默认 14，钳 [1, 365]。"""
    configured = _config_int(db, RETENTION_KEY)
    if configured is not None:
        return max(1, min(365, configured))
    return RETENTION_DEFAULT_DAYS


def purge_expired_events(db, *, now: Optional[datetime] = None) -> int:
    """删超保留期的事件行（本函数提交），返回删除行数。

    由 maintenance.janitor_tick 周期调用；失败由调用方兜底，
    本函数不吞异常（与 janitor 各分项同口径）。
    """
    now = now or datetime.now()
    cutoff = now - timedelta(days=retention_days(db))
    deleted = (db.query(em.DriveApiErrorEvent)
               .filter(em.DriveApiErrorEvent.created_at < cutoff)
               .delete(synchronize_session=False))
    db.commit()
    return int(deleted or 0)


def error_summary(db, *, days: int = 7) -> dict:
    """近 N 天报错聚合（管理端只读面）：按 reason 总计 +
    按（天, reason）明细。天口径 = created_at 的本地日期。

    days 钳 [1, 90]。reason 空串原样返回（聚合里即「未解析」
    桶），前端/调用方自行标注。
    """
    days = max(1, min(90, int(days or 7)))
    since = datetime.now() - timedelta(days=days)
    base = db.query(em.DriveApiErrorEvent).filter(
        em.DriveApiErrorEvent.created_at >= since)
    total = int(base.count())
    by_reason = [
        {"reason": reason or "", "count": int(count)}
        for reason, count in (
            base.with_entities(em.DriveApiErrorEvent.reason,
                               func.count(em.DriveApiErrorEvent.id))
            .group_by(em.DriveApiErrorEvent.reason)
            .order_by(func.count(em.DriveApiErrorEvent.id).desc())
            .all())
    ]
    day_col = func.date(em.DriveApiErrorEvent.created_at)
    by_day_reason = [
        {"day": str(day), "reason": reason or "", "count": int(count)}
        for day, reason, count in (
            base.with_entities(
                day_col, em.DriveApiErrorEvent.reason,
                func.count(em.DriveApiErrorEvent.id))
            .group_by(day_col, em.DriveApiErrorEvent.reason)
            .order_by(day_col, em.DriveApiErrorEvent.reason)
            .all())
    ]
    return {"days": days, "total": total,
            "by_reason": by_reason, "by_day_reason": by_day_reason}
