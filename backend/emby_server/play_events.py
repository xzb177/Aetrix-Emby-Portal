"""起播事件打点（FMBY 学习批一 #2）

每次起播在调度层记一条事件——用的哪家源、解析耗时、成败与错误类型，
落库后管理端可聚合回答「起播慢是慢在哪家网盘、失败是哪类错误」。
学的是 FMBY 的 ``playback_start_events``（provider_type / startup_latency_ms
/ result / error_kind）：起播观测独立于播放会话与进度，单独成表。

对应魂 6（状态集中维护）：起播事件只有一个落点（本模块的队列与事件表），
播放各出口只许调 ``note_play_start``，不许各自写库或另建统计。
对应魂 3（调度与数据面分离）：打点只挂在调度层（api.py 的 video_stream
快路径与 _play_target 解析收口），数据面（streaming.py 中继取流本体）
一行不碰。

**铁律：打点不许拖慢起播。**
- 热路径只做一次 ``queue.put_nowait``（微秒级），不等 DB；
- 落库由后台守护线程批量做（凑满一批或到间隔上限才写），独立会话；
- 队列满、写库失败、模块被关——一律静默丢弃并计数，绝不向上抛。
  这正是 line_stats 当年不落库的顾虑（「落库要每次播放写一行，是热
  路径」），本模块用「队列 + 批量 + 失败静默」把这条顾虑解掉，而不是
  在播放路径上同步写库。

开关：环境变量 ``PLAY_START_EVENTS_ENABLED=0`` 关闭（默认开启），
import 时读一次，热路径不查 env（与 play_timing 同口径）。

已知边界（本期不做，别当 bug）：
- 事件粒度是「调度尝试」：快路径（Drive 中继 / 302）各一条、回落解析
  一条；回落分支最终分发到 mount 中继还是本机文件不另记 method 细分。
- 解析前的拒绝（条目不可见 404、未订阅拦截）没进调度层，不记。
- 聚合读口是 ``aggregate_play_start_events`` 查询函数；管理端调用方
  是 ``play_start_admin.py`` 的 GET /api/admin/playback/play-start-stats
 （FMBY #2 闭环接线，只读，不进播放热路径）。
"""
from __future__ import annotations

import logging
import os
import queue
import threading
import time
from datetime import datetime, timedelta
from typing import Optional

from sqlalchemy import case, func

from backend.emby_server import models as em

logger = logging.getLogger(__name__)

# import 时读一次（与 play_timing 同口径）：热路径不查 env
_ENABLED = os.getenv("PLAY_START_EVENTS_ENABLED", "1") not in (
    "0", "false", "False", "")

#: 队列上限：满了就丢新事件（起播高峰宁可少记，不许反压播放）
QUEUE_MAX = 2048
#: 后台落库的批量与间隔：凑满 BATCH 条或等满 INTERVAL 秒就写一批
BATCH = 50
FLUSH_INTERVAL_SEC = 1.0

# 模块级直接初始化，不许懒初始化（同 _pan115_url_cache 的口径）；
# 后台线程可以懒启动（第一次 note 时），队列与计数不行。
_QUEUE: "queue.Queue[dict]" = queue.Queue(maxsize=QUEUE_MAX)
_STATS = {"noted": 0, "written": 0, "dropped": 0, "write_errors": 0}
_STATS_LOCK = threading.Lock()
_THREAD: Optional[threading.Thread] = None
_THREAD_LOCK = threading.Lock()
_STOP = threading.Event()


def is_enabled() -> bool:
    """打点是否开启（供测试/外部查询）。"""
    return _ENABLED


def _bump(name: str, n: int = 1) -> None:
    with _STATS_LOCK:
        _STATS[name] += n


def play_event_stats() -> dict:
    """打点流水计数（健康检查 / 测试用）：noted 入队数、written 落库数、
    dropped 丢弃数（关开关/队列满/写失败）、write_errors 写库失败批次数。"""
    with _STATS_LOCK:
        return {**_STATS, "queued": _QUEUE.qsize(),
                "enabled": _ENABLED}


def note_play_start(*, provider: str = "unknown", mount_id=None,
                    user_id=None, item_id=None, method: Optional[str] = None,
                    variant: Optional[str] = None,
                    result: str = "ok", error_kind: Optional[str] = None,
                    resolve_ms: Optional[int] = None,
                    total_ms: Optional[int] = None) -> None:
    """记一条起播事件（只入队，永不抛、永不阻塞）。

    任何异常（队列满、关开关、参数怪异）都只体现为计数里的 dropped，
    调用方（播放入口）不需要、也不应该为打点写 try/except 兜底逻辑
    之外的任何东西。
    """
    if not _ENABLED:
        _bump("dropped")
        return
    try:
        event = {
            "provider": (provider or "unknown")[:30],
            "mount_id": mount_id,
            "user_id": user_id,
            "item_id": item_id,
            "method": (method or None) and method[:30],
            # 变体维度（FMBY 批二 #11 PR-2）：事件带上实际走的变体名，
            # 老调用方不传时为 None，与升级前逐字一致；聚合读口仍按
            # provider × mount_id 分组，variant 只作筛选维度，不改分组键。
            "variant": (variant or None) and variant[:30],
            "result": "error" if result == "error" else "ok",
            "error_kind": (error_kind or None) and error_kind[:40],
            "resolve_ms": None if resolve_ms is None else max(0, int(resolve_ms)),
            "total_ms": None if total_ms is None else max(0, int(total_ms)),
        }
        _QUEUE.put_nowait(event)
        _bump("noted")
        _ensure_writer_thread()
    except queue.Full:
        _bump("dropped")
    except Exception:  # noqa: BLE001 — 打点永不抛，见模块 docstring 铁律
        _bump("dropped")


def _ensure_writer_thread() -> None:
    """第一次记事件时懒启动后台落库线程（daemon，随进程退出）。"""
    global _THREAD
    if _THREAD is not None and _THREAD.is_alive():
        return
    with _THREAD_LOCK:
        if _THREAD is not None and _THREAD.is_alive():
            return
        _STOP.clear()
        _THREAD = threading.Thread(
            target=_writer_loop, name="play-events-writer", daemon=True)
        _THREAD.start()


def _writer_loop() -> None:
    """后台落库循环：等满间隔后把队列里攒的事件批量写完。"""
    while not _STOP.is_set():
        _STOP.wait(FLUSH_INTERVAL_SEC)
        try:
            flush_pending()
        except Exception:  # noqa: BLE001 — flush_pending 本应全吞，这里是双保险
            logger.debug("起播事件落库循环异常", exc_info=True)


def _write_batch(events: list[dict]) -> None:
    """一批事件写库（独立会话）。失败整批丢弃并计数，绝不向上抛。"""
    if not events:
        return
    from backend.database import SessionLocal

    db = None
    try:
        db = SessionLocal()
        db.add_all([em.PlayStartEvent(**event) for event in events])
        db.commit()
        _bump("written", len(events))
    except Exception:  # noqa: BLE001 — 写失败静默丢弃（铁律），只留计数与一条 debug
        if db is not None:
            try:
                db.rollback()
            except Exception:  # noqa: BLE001
                pass
        _bump("write_errors")
        _bump("dropped", len(events))
        logger.debug("起播事件批量落库失败，丢弃 %d 条", len(events),
                     exc_info=True)
    finally:
        if db is not None:
            try:
                db.close()
            except Exception:  # noqa: BLE001
                pass


def flush_pending() -> int:
    """把队列里当前攒着的事件全部落库，返回写出条数（后台线程与测试共用）。

    测试可不开后台线程、直接调它做同步断言；生产由 _writer_loop 周期调。
    """
    drained: list[dict] = []
    while True:
        try:
            drained.append(_QUEUE.get_nowait())
        except queue.Empty:
            break
    for i in range(0, len(drained), BATCH):
        _write_batch(drained[i:i + BATCH])
    return len(drained)


def reset_for_tests() -> None:
    """清空队列与计数、停掉后台线程（测试隔离用，照 #1203 熔断 reset 先例）。"""
    global _THREAD
    _STOP.set()
    thread = _THREAD
    _THREAD = None
    if thread is not None and thread.is_alive():
        thread.join(timeout=2.0)
    while True:
        try:
            _QUEUE.get_nowait()
        except queue.Empty:
            break
    with _STATS_LOCK:
        for name in _STATS:
            _STATS[name] = 0
    _STOP.clear()


def aggregate_play_start_events(db, hours: int = 24) -> dict:
    """管理端聚合读口：近 ``hours`` 小时起播事件按源、按错误类型汇总。

    回答两个问题：①哪家源起播多、失败多、解析慢（by_provider，挂载按
    mount_id 拆行并补挂载名/类型）；②失败都是哪类错误（by_error_kind）。
    只读聚合，不写库；调用方是低频管理面，不进播放热路径。
    """
    since = datetime.now() - timedelta(hours=max(1, int(hours)))
    error_count = func.sum(
        case((em.PlayStartEvent.result == "error", 1), else_=0))
    rows = (
        db.query(
            em.PlayStartEvent.provider,
            em.PlayStartEvent.mount_id,
            func.count(em.PlayStartEvent.id),
            error_count,
            func.avg(em.PlayStartEvent.resolve_ms),
            func.max(em.PlayStartEvent.resolve_ms),
        )
        .filter(em.PlayStartEvent.created_at >= since)
        .group_by(em.PlayStartEvent.provider, em.PlayStartEvent.mount_id)
        .all()
    )
    mount_ids = [row[1] for row in rows if row[1] is not None]
    mounts = {}
    if mount_ids:
        for mount in db.query(em.StorageMount).filter(
                em.StorageMount.id.in_(mount_ids)).all():
            mounts[mount.id] = mount
    by_provider = []
    for provider, mount_id, total, errors, avg_ms, max_ms in rows:
        mount = mounts.get(mount_id) if mount_id is not None else None
        by_provider.append({
            "provider": provider,
            "mount_id": mount_id,
            "mount_name": getattr(mount, "name", None),
            "mount_type": getattr(mount, "mount_type", None),
            "total": int(total or 0),
            "errors": int(errors or 0),
            "avg_resolve_ms": None if avg_ms is None else round(float(avg_ms), 1),
            "max_resolve_ms": max_ms,
        })
    by_provider.sort(key=lambda row: -row["total"])
    error_rows = (
        db.query(em.PlayStartEvent.error_kind, func.count(em.PlayStartEvent.id))
        .filter(em.PlayStartEvent.created_at >= since,
                em.PlayStartEvent.result == "error")
        .group_by(em.PlayStartEvent.error_kind)
        .all()
    )
    return {
        "window_hours": max(1, int(hours)),
        "by_provider": by_provider,
        "by_error_kind": [
            {"error_kind": kind or "unknown", "total": int(total or 0)}
            for kind, total in sorted(error_rows, key=lambda row: -row[1])
        ],
    }
