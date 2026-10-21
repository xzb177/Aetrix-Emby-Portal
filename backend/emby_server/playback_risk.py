"""播放风控可观测（2026-10-21）：Drive 存在性检查 / 302 / 403·429 计数 + 叠播看板。

为什么要它：Drive 播放链路是「EA 做一次 files.get 存在性检查 → 302 到谷歌
usercontent 直链」，字节不经本机。真正可能触发谷歌风控的是两件事：
① 我们这边的 Drive API 调用量（存在性检查、补开分享）；
② 同一文件被多人同时叠播（usercontent 对同 file_id 高并发 Range 更挑剔）。
本模块只做**计数与只读汇总**，不改任何播放判定。

计数口径：
- 进程内计数（重启清零）始终记；
- Redis 回写不在请求路径上做。``bump`` 只加内存和待刷增量，后台线程按
  ``FLUSH_INTERVAL_SEC`` 批量 HINCRBY 到 ``REDIS_KEY``（EA 单 worker，
  同步 Redis 会堵住之后每一个请求）。Redis 不可用或写入失败时请求不受影响：
  未启用则丢掉这批增量（内存合计还在），写入异常则把增量加回待刷队列。
  管理端读的是全进程合计；读不到时只能看到本进程数字，返回体 ``scope``
  字段如实标明，不谎报。
- 任何异常永不抛给调用方。
"""
from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timedelta

logger = logging.getLogger(__name__)

#: 计数项（固定白名单，前端按名展示）
COUNTER_NAMES = (
    "redirect_302",          # 播放链路发出的 302（Drive / 对象存储直链）
    "exists_cache_hit",      # 存在性检查命中正缓存（省掉一次 files.get）
    "exists_cache_miss",     # 未命中 → 真实打了一次 files.get
    "exists_404",            # files.get 404（文件已删/不可见）
    "exists_503",            # files.get 其他失败（超时/5xx/凭据/403/429）→ 503
    "drive_403",             # Drive API 返回 403（存在性检查 / 补开分享）
    "drive_429",             # Drive API 返回 429
)

REDIS_KEY = "aetrix:playback_risk:counters"
REDIS_STARTED_FIELD = "_started_at"

#: 「正在播」判定窗口：last_update_at 在此秒数内且未结束（播放器约 10s 上报一次进度）
ACTIVE_WINDOW_SEC = 120
#: 叠播风险阈值：同一条目同时在播人数 ≥ 此值即标红
STACK_THRESHOLD = 2

_counters = {name: 0 for name in COUNTER_NAMES}
#: 尚未刷到 Redis 的增量。请求路径只动这块内存。
_pending = {name: 0 for name in COUNTER_NAMES}
_lock = threading.Lock()
_started_at = time.time()
#: 后台批量回写间隔。不进 env：调它不改变计数口径，只改变跨进程可见的延迟。
FLUSH_INTERVAL_SEC = 2.0
_flusher_started = False
_flusher_lock = threading.Lock()


def _redis():
    try:
        from backend.database import redis_client

        return redis_client
    except Exception:  # noqa: BLE001
        return None


def bump(name: str, n: int = 1) -> None:
    """计数 +n。只动进程内内存，Redis 交给后台批量刷。永不抛异常。"""
    if name not in _counters or not n:
        return
    try:
        with _lock:
            _counters[name] += n
            _pending[name] += n
    except Exception:  # noqa: BLE001
        return
    try:
        _ensure_flusher()
    except Exception:  # noqa: BLE001
        pass


def _flush_once() -> None:
    """把待刷增量一次性 HINCRBY 出去。Redis 未启用则丢掉这批；失败则加回。

    调用方（后台线程或测试）之外不要在请求路径上调用。永不抛。
    """
    batch: dict[str, int] = {}
    with _lock:
        for name in COUNTER_NAMES:
            n = _pending[name]
            if n:
                batch[name] = n
                _pending[name] = 0
    if not batch:
        return
    sent: list[str] = []
    try:
        rc = _redis()
        if rc is None:
            return
        for name, n in batch.items():
            rc.hincrby(REDIS_KEY, name, n)
            sent.append(name)
        try:
            rc.hsetnx(REDIS_KEY, REDIS_STARTED_FIELD, int(time.time()))
        except Exception:  # noqa: BLE001 — 计数已写入，起始时间丢了下次再补
            logger.debug("playback_risk 写 started_at 失败", exc_info=True)
    except Exception:  # noqa: BLE001 — Redis 抖动只留在后台线程
        logger.debug("playback_risk Redis 批量回写失败", exc_info=True)
        with _lock:
            for name, n in batch.items():
                if name not in sent:
                    _pending[name] += n


def _flush_loop() -> None:
    while True:
        time.sleep(FLUSH_INTERVAL_SEC)
        try:
            _flush_once()
        except Exception:  # noqa: BLE001
            logger.debug("playback_risk 后台刷写异常", exc_info=True)


def _ensure_flusher() -> None:
    """第一次 bump 时拉起守护线程。请求路径到此为止，不再碰 Redis。"""
    global _flusher_started
    if _flusher_started:
        return
    with _flusher_lock:
        if _flusher_started:
            return
        threading.Thread(
            target=_flush_loop, name="playback-risk-flush", daemon=True,
        ).start()
        _flusher_started = True


def note_drive_status(code) -> None:
    """Drive API 响应码观测点：403/429 各计一次，其他码忽略。"""
    try:
        c = int(code)
    except Exception:  # noqa: BLE001
        return
    if c == 403:
        bump("drive_403")
    elif c == 429:
        bump("drive_429")


def process_counters() -> dict:
    with _lock:
        return dict(_counters)


def reset() -> None:
    """清零（单元测试隔离用；只清进程内，含尚未刷出的增量）。"""
    global _started_at
    with _lock:
        for k in _counters:
            _counters[k] = 0
            _pending[k] = 0
    _started_at = time.time()


def counters_snapshot() -> dict:
    """优先读 Redis 全进程合计；读不到回落本进程。"""
    rc = _redis()
    if rc is not None:
        try:
            raw = rc.hgetall(REDIS_KEY) or {}
            out = {name: int(raw.get(name) or 0) for name in COUNTER_NAMES}
            started = raw.get(REDIS_STARTED_FIELD)
            return {"scope": "all_processes", "counters": out,
                    "since": int(started) if started else None}
        except Exception:  # noqa: BLE001
            logger.debug("playback_risk 读 Redis 失败，回落本进程", exc_info=True)
    return {"scope": "process", "counters": process_counters(),
            "since": int(_started_at)}


def _hit_rate(c: dict):
    total = int(c.get("exists_cache_hit", 0)) + int(c.get("exists_cache_miss", 0))
    if total <= 0:
        return None
    return round(int(c.get("exists_cache_hit", 0)) / total, 4)


def active_sessions(db, now: datetime | None = None, top: int = 10) -> dict:
    """DB 口径：正在播的会话数 + 按条目聚合的同时在播人数（Top N）。"""
    from sqlalchemy import func

    from backend.emby_server import models as em

    now = now or datetime.now()
    cutoff = now - timedelta(seconds=ACTIVE_WINDOW_SEC)
    PS = em.PlaybackSession
    base = db.query(PS).filter(PS.ended_at.is_(None), PS.last_update_at >= cutoff)
    total = base.count()
    viewers = func.count(func.distinct(PS.user_id)).label("viewers")
    rows = (
        db.query(PS.item_id, viewers, func.count(PS.id).label("sessions"))
        .filter(PS.ended_at.is_(None), PS.last_update_at >= cutoff)
        .group_by(PS.item_id)
        .order_by(viewers.desc(), func.count(PS.id).desc())
        .limit(top)
        .all()
    )
    item_ids = [r[0] for r in rows if r[0] is not None]
    names = {}
    if item_ids:
        found = db.query(em.MediaItem).filter(em.MediaItem.id.in_(item_ids)).all()
        series_ids = {getattr(it, "series_id", None) for it in found} - {None}
        series_names = {}
        if series_ids:
            series_names = {
                sid: n for sid, n in db.query(em.MediaItem.id, em.MediaItem.name)
                .filter(em.MediaItem.id.in_(list(series_ids))).all()
            }
        for it in found:
            names[it.id] = {
                "name": getattr(it, "name", None),
                "series_name": series_names.get(getattr(it, "series_id", None)),
                "is_drive": bool((getattr(it, "drive_file_id", "") or "").strip()),
            }
    items = []
    for item_id, v, s in rows:
        meta = names.get(item_id, {})
        items.append({
            "item_id": item_id,
            "name": meta.get("name"),
            "series_name": meta.get("series_name"),
            "is_drive": meta.get("is_drive", False),
            "viewers": int(v or 0),
            "sessions": int(s or 0),
            "stacked": int(v or 0) >= STACK_THRESHOLD,
        })
    return {
        "active_sessions": int(total),
        "window_seconds": ACTIVE_WINDOW_SEC,
        "stack_threshold": STACK_THRESHOLD,
        "stacked_items": sum(1 for x in items if x["stacked"]),
        "top_items": items,
    }


def snapshot(db) -> dict:
    """管理端一次拿全：计数（全进程/本进程）+ 正在播 + 叠播 Top10 + 缓存 TTL。"""
    snap = counters_snapshot()
    c = snap["counters"]
    try:
        from backend.emby_server.api import drive_exists_cache_ttl

        ttl = drive_exists_cache_ttl()
    except Exception:  # noqa: BLE001
        ttl = None
    try:
        sessions = active_sessions(db)
    except Exception:  # noqa: BLE001 — 会话表读失败不影响计数展示
        logger.warning("playback_risk 读播放会话失败", exc_info=True)
        sessions = {"active_sessions": None, "window_seconds": ACTIVE_WINDOW_SEC,
                    "stack_threshold": STACK_THRESHOLD, "stacked_items": 0,
                    "top_items": [], "error": True}
    return {
        "scope": snap["scope"],
        "since": snap["since"],
        "counters": c,
        "exists_cache_hit_rate": _hit_rate(c),
        "exists_cache_ttl_sec": ttl,
        **sessions,
    }


__all__ = [
    "COUNTER_NAMES", "bump", "note_drive_status", "counters_snapshot",
    "process_counters", "reset", "active_sessions", "snapshot",
]
