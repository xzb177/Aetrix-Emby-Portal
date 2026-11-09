# -*- coding: utf-8 -*-
"""个人盘起播取用事件落库（goal-38 审计第一步）。

病根：起播取用侧的关键动作此前只有日志或干脆没痕——绑定门禁
403 在抛错前不落任何表；副本选路（resolve_play_file_id）纯读
不记、命中/未命中只能靠门禁决策行间接推断；阶段 6 换线失败的
终局 409/重抛没有任何事件行。双盘审计台账（
GET /api/admin/audit/events）要能按人/盘/片回答「谁在什么时候
取用被拒、副本没命中、换链失败」，故单独立表
``drive_play_access_events`` 落这三类动作。

本模块是这三类事件的唯一落点：

- api.playback_info / api.video_stream 的绑定门禁 403 出口：
  ``gate_reject``（result=denied，reason=门禁 detail 原文）；
- api.video_stream 的副本选路出口：``copy_resolve``
  （result=hit/miss，detail 带 variant_id / owner_scope）；
- api.video_stream 的阶段 6 换线失败终局：``switch_failed``
  （result=error）。

**铁律：落库不许拖垮播放。** note_play_access 用独立会话写，
任何失败一律静默丢弃并计数返回 False，绝不向上抛、绝不改变
调用方的返回值/异常。挂点只观察、只记录——播放判定逻辑一行
不依赖本模块。

限频：起播是热路径，同一 (user_id, item_id, action, result,
reason) 在 ``THROTTLE_SECONDS`` 内重复触发只记一行（进程内
字典，重启即清；多进程下每进程各一份，台账侧可接受）。节流
命中的不写库、只计数。

保留期：maintenance.janitor_tick 周期调 purge_expired_events
清理过期行。保留天数 SystemConfig
``play_access_events_retention_days`` 可配，默认 30，钳 [1, 365]。
"""
from __future__ import annotations

import json
import logging
import threading
import time
from datetime import datetime, timedelta
from typing import Optional

from backend.database import SessionLocal
from backend.emby_server import models as em

logger = logging.getLogger(__name__)

#: 限频窗口（秒）：同一键在此窗口内只落一行。
THROTTLE_SECONDS = 10.0

#: 限频字典上限：超过即整体清空（防异常流量把字典撑爆）。
_THROTTLE_MAX_KEYS = 8192

#: 保留期配置键与默认值（天）
RETENTION_KEY = "play_access_events_retention_days"
RETENTION_DEFAULT_DAYS = 30

_lock = threading.Lock()
_last_written: dict = {}
_stats = {"noted": 0, "written": 0, "write_errors": 0, "throttled": 0}


def _bump(key: str) -> None:
    with _lock:
        _stats[key] = _stats.get(key, 0) + 1


def stats() -> dict:
    with _lock:
        return dict(_stats)


def _throttled(key) -> bool:
    """该键是否在限频窗口内已记过一行（记过返回 True 并计数）。"""
    now = time.monotonic()
    with _lock:
        if len(_last_written) > _THROTTLE_MAX_KEYS:
            _last_written.clear()
        last = _last_written.get(key)
        if last is not None and (now - last) < THROTTLE_SECONDS:
            _stats["throttled"] = _stats.get("throttled", 0) + 1
            return True
        _last_written[key] = now
        return False


def _trim(value, limit: int):
    if value is None:
        return None
    text = str(value)
    return text[:limit] if len(text) > limit else text


def _resolve_binding_id(db, user_id) -> Optional[int]:
    """尽力解析该用户当前 active 的 Drive 绑定行 id（台账按盘筛
    需要）；查不到/异常一律 None，不许因解析失败丢整行。"""
    if user_id is None:
        return None
    try:
        row = (
            db.query(em.UserStorageBinding.id)
            .filter(
                em.UserStorageBinding.user_id == user_id,
                em.UserStorageBinding.provider == "drive",
                em.UserStorageBinding.status == "active",
            )
            .order_by(em.UserStorageBinding.id)
            .first()
        )
        return int(row[0]) if row is not None else None
    except Exception:  # noqa: BLE001 — 解析失败不丢整行
        return None


def note_play_access(user_id=None, binding_id=None, item_id=None,
                     entry: str = "", action: str = "", result: str = "",
                     reason=None, detail=None) -> bool:
    """落一行起播取用事件。成功 True；限频命中/任何失败静默
    丢弃返回 False，绝不抛（见模块 docstring 铁律）。

    独立会话：不借调用方的事务——调用方回滚不连带记录，
    记录失败也不污染调用方事务。
    """
    _bump("noted")
    key = (user_id, item_id, action, result,
           _trim(reason, 200) if reason is not None else None)
    if _throttled(key):
        return False
    try:
        detail_json = None
        if detail is not None:
            detail_json = json.dumps(detail, ensure_ascii=False,
                                     default=str)
        row = em.DrivePlayAccessEvent(
            user_id=user_id,
            binding_id=binding_id,
            item_id=item_id,
            entry=_trim(entry, 20) or "",
            action=_trim(action, 40) or "",
            result=_trim(result, 40) or "",
            reason=_trim(reason, 200),
            detail=detail_json,
        )
    except Exception:  # noqa: BLE001 — 组装失败同样静默丢弃
        _bump("write_errors")
        logger.warning("起播取用事件组装失败 action=%s", action,
                       exc_info=True)
        return False
    db = None
    try:
        db = SessionLocal()
        if row.binding_id is None:
            row.binding_id = _resolve_binding_id(db, row.user_id)
        db.add(row)
        db.commit()
    except Exception:  # noqa: BLE001 — 写失败静默丢弃，不挡播放
        _bump("write_errors")
        logger.warning("起播取用事件写入失败 action=%s", action,
                       exc_info=True)
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


def _config_int(db, key: str) -> Optional[int]:
    """SystemConfig 整数热读：无行/非法/异常一律 None（回退默认）。

    与 transfer_decisions._config_int 同口径。
    """
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
    """保留天数：SystemConfig > 默认 30，钳 [1, 365]。"""
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
    deleted = (db.query(em.DrivePlayAccessEvent)
               .filter(em.DrivePlayAccessEvent.created_at < cutoff)
               .delete(synchronize_session=False))
    db.commit()
    return int(deleted or 0)
