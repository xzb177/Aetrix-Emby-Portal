# -*- coding: utf-8 -*-
"""上游聚合自动调度骨架（准备线，开关默认关）。

定位（2026-10-31 优化 O-03 起，与手动聚合共用任务表）：
- 手动聚合与本调度是同一条链的两个触发口：管理端
  ``POST /api/admin/emby/upstream-sources/{id}/aggregate`` 与
  本调度都只往任务表（``upstream_aggregate_tasks``）入队，
  执行统一归 worker 的认领循环（``upstream_aggregate_jobs``
  的 claim_and_execute_once → run_manual_aggregation 逐库事务）。
- 本模块只做「到点入队」：按间隔对已启用的 Emby / Jellyfin 型源
  逐个入队（trigger=scheduled），不自己执行聚合、不读播放地址、
  不碰 video_stream 选路。O-03 前本调度在调度线程里直接调聚合
  入口（与手动两条路、跨进程无互斥，见设计稿 P-04），已废止。
- 同源互斥：入队前先查该源进行中任务，有则本轮跳过并记
  skipped（reason=task_active）；手动与调度从此不会同源并发。
- 苹果 CMS 型源不进本调度（它走自己的 ``applecms_scheduler`` 同步）；
  未启用源不进本调度（手动聚合本身也会 400，本处先跳过并记 skipped）。

开关与间隔（全部进 env.example，中文注释，默认关）：
- ``UPSTREAM_AGGREGATE_SCHEDULE_ENABLED``：``1`` 才启动，默认 ``0``；
  关时 ``start_upstream_aggregate_scheduler`` 返回 ``False``、不启动
  线程、不发任何上游请求，全站行为逐字不变。
- ``UPSTREAM_AGGREGATE_SCHEDULE_INTERVAL_SEC``：默认 21600 秒（6 小时，
  与苹果 CMS 同步同频的低频起步值）；非法/缺失回落默认；低于 60 秒
  钳制到 60 秒，防止误配过短间隔反复刷上游。

单源失败隔离与调度日志：
- 入队是轻动作：单源入队异常只记该源 failed、继续下一源，
  不毁整轮；执行期的失败落在任务行上（status/error），调度
  本轮结果只表达「入队结果」：queued / skipped / failed。
- 每轮、每源结果均经 ``logger`` 输出（轮次开始/结束、每源
  结果与计数），调度日志可查；返回值同样带逐源明细，
  供调用方与测试断言。

启动接线：``backend/worker.py`` 里只加本模块的一段启动块（与苹果 CMS
调度、探活工人互不相干，各自独立）；关时不启动。单实例：已有存活
线程时 ``start_*`` 直接返回 True，不双跑。
"""

from __future__ import annotations

import logging
import os
import threading
from typing import Callable, Optional

logger = logging.getLogger(__name__)

#: 调度总开关 env 键（默认 0 = 关）。
SCHEDULE_ENABLED_ENV = "UPSTREAM_AGGREGATE_SCHEDULE_ENABLED"

#: 调度间隔 env 键（秒）。
SCHEDULE_INTERVAL_ENV = "UPSTREAM_AGGREGATE_SCHEDULE_INTERVAL_SEC"

#: 默认间隔（秒）：目录聚合是低频活，6 小时一轮已够；要更密由
#: 部署方在 env 里显式改，不由代码替部署方定业务节奏。
DEFAULT_SCHEDULE_INTERVAL_SEC = 21600.0

#: 间隔下限（秒）：低于此值钳制，防止误配把上游刷爆。
MIN_SCHEDULE_INTERVAL_SEC = 60.0

_scheduler_thread: threading.Thread | None = None
_scheduler_lock = threading.Lock()


def scheduler_enabled() -> bool:
    """调度是否可启动：开关显式为 1 才真，其余一律关。"""
    return (os.getenv(SCHEDULE_ENABLED_ENV, "0") or "0").strip() == "1"


def schedule_interval_sec(explicit: Optional[float] = None) -> float:
    """读调度间隔（秒）：显式值优先，否则读 env，非法回落默认。"""
    if explicit is not None:
        try:
            value = float(explicit)
        except (TypeError, ValueError):
            return DEFAULT_SCHEDULE_INTERVAL_SEC
        if value != value:  # NaN
            return DEFAULT_SCHEDULE_INTERVAL_SEC
        return max(MIN_SCHEDULE_INTERVAL_SEC, value)
    try:
        value = float(os.getenv(SCHEDULE_INTERVAL_ENV,
                                str(DEFAULT_SCHEDULE_INTERVAL_SEC)) or
                      str(DEFAULT_SCHEDULE_INTERVAL_SEC))
    except (TypeError, ValueError):
        return DEFAULT_SCHEDULE_INTERVAL_SEC
    if value != value:
        return DEFAULT_SCHEDULE_INTERVAL_SEC
    return max(MIN_SCHEDULE_INTERVAL_SEC, value)


def run_scheduled_aggregation(*, session_factory=None, enqueue_fn=None,
                              list_sources_fn=None, active_fn=None) -> list:
    """跑一轮定时调度：对已启用的 Emby / Jellyfin 型源逐个入队。

    只入队不执行（O-03）：执行归 worker 认领循环。依赖一律可注入
    （测试不碰真实 DB、不发真实请求）：
    - ``list_sources_fn()``：默认列出已启用的 Emby / Jellyfin 型源行；
    - ``active_fn(source_id)``：默认查任务表该源进行中任务
      （``upstream_aggregate_jobs.active_task_for``）；
    - ``enqueue_fn(source_id)``：默认解析配置缺省页大小后走
      ``upstream_aggregate_jobs.enqueue(..., trigger="scheduled")``，
      返回任务视图 dict；
    - ``session_factory``：默认 ``backend.database.SessionLocal``，
      惰性 import（本模块顶层不碰 DB，单测 import 不建库）。

    每源结果形态：``{"source_id", "status", ...}``，status 为
    ``queued``（带 task_id）/ ``failed``（带 code / error）/
    ``skipped``（带 reason：source_disabled /
    source_type_not_supported / task_active）之一。列源本身失败
    时返回空列表并记日志，不抛错（调度线程不因此死掉）。
    """
    if enqueue_fn is None or list_sources_fn is None or active_fn is None:
        from backend.database import SessionLocal
        from backend.emby_server import upstream_aggregate_jobs as uaj
        from backend.emby_server import upstream_aggregate_run as uar
        from backend.emby_server import upstream_sources as ups

        if session_factory is None:
            session_factory = SessionLocal

        if list_sources_fn is None:
            def _default_list_sources():
                db = session_factory()
                try:
                    return [s for s in ups.list_sources(db)
                            if getattr(s, "source_type", "") != "applecms"
                            and getattr(s, "enabled", False)]
                finally:
                    db.close()

            list_sources_fn = _default_list_sources

        if active_fn is None:
            active_fn = uaj.active_task_for

        if enqueue_fn is None:
            def _default_enqueue(source_id):
                db = session_factory()
                try:
                    page_size = uar.configured_default_page_size(db)
                finally:
                    db.close()
                return uaj.enqueue(source_id, page_size,
                                   trigger="scheduled")

            enqueue_fn = _default_enqueue

    try:
        sources = list(list_sources_fn() or [])
    except Exception:
        logger.exception("上游聚合定时调度列源失败")
        return []
    logger.info("上游聚合定时调度本轮开始 sources=%s", len(sources))
    results = []
    for source in sources:
        source_id = getattr(source, "id", None)
        if not getattr(source, "enabled", False):
            logger.info("上游聚合定时调度跳过未启用源 source_id=%s", source_id)
            results.append({"source_id": source_id, "status": "skipped",
                            "reason": "source_disabled"})
            continue
        if getattr(source, "source_type", "") == "applecms":
            logger.info("上游聚合定时调度跳过苹果 CMS 源 source_id=%s", source_id)
            results.append({"source_id": source_id, "status": "skipped",
                            "reason": "source_type_not_supported"})
            continue
        try:
            if active_fn(source_id) is not None:
                logger.info("上游聚合定时调度跳过进行中源 source_id=%s",
                            source_id)
                results.append({"source_id": source_id, "status": "skipped",
                                "reason": "task_active"})
                continue
            view = enqueue_fn(source_id)
            if not isinstance(view, dict) or not view.get("task_id"):
                raise TypeError("入队返回非任务视图，无法记调度结果")
            logger.info("上游聚合定时调度单源入队 source_id=%s task_id=%s",
                        source_id, view["task_id"])
            results.append({"source_id": source_id, "status": "queued",
                            "task_id": view["task_id"]})
        except Exception as exc:  # noqa: BLE001 - 单源隔离：入队异常不毁整轮
            code = getattr(exc, "code", None) or exc.__class__.__name__
            logger.exception("上游聚合定时调度单源入队失败 source_id=%s "
                             "code=%s", source_id, code)
            results.append({"source_id": source_id, "status": "failed",
                            "code": code, "error": str(exc)})
    queued_count = sum(1 for r in results if r["status"] == "queued")
    skipped_count = sum(1 for r in results if r["status"] == "skipped")
    failed_count = sum(1 for r in results if r["status"] == "failed")
    logger.info("上游聚合定时调度本轮结束 sources=%s queued=%s "
                "skipped=%s failed=%s",
                len(results), queued_count, skipped_count, failed_count)
    return results


def run_scheduler_loop(stop_event=None, *, run_once=None,
                       interval_sec: Optional[float] = None) -> None:
    """调度循环：先跑一轮，再按间隔等，停表置位即退出。

    ``run_once`` 未传时走 :func:`run_scheduled_aggregation`（生产路径）；
    等待用 ``stop_event.wait(interval)``（无 stop_event 时用
    ``time.sleep``），使停机不用等满一个间隔。
    单轮异常已在 ``run_scheduled_aggregation`` 内隔离；注入的 ``run_once``
    抛异常时本循环记日志后继续，不让调度线程悄悄死掉。
    """
    import time

    interval = schedule_interval_sec(interval_sec)
    do_run = run_once if run_once is not None else run_scheduled_aggregation
    while True:
        if stop_event is not None and stop_event.is_set():
            return
        try:
            do_run()
        except Exception:
            logger.exception("上游聚合定时调度整轮失败（下一轮继续）")
        if stop_event is not None:
            if stop_event.wait(interval):
                return
        else:
            time.sleep(interval)


def start_upstream_aggregate_scheduler(stop_event=None, *, run_once=None,
                                       interval_sec: Optional[float] = None) -> bool:
    """启动定时调度线程（worker.py 调用，照苹果 CMS 调度工人的写法）。

    开关关时返回 False、不启动；幂等：已有存活线程时直接返回 True。
    """
    global _scheduler_thread
    if not scheduler_enabled():
        return False
    with _scheduler_lock:
        alive = _scheduler_thread
        if alive is not None and alive.is_alive():
            return True
        thread = threading.Thread(
            target=run_scheduler_loop,
            args=(stop_event,),
            kwargs={"run_once": run_once, "interval_sec": interval_sec},
            daemon=True, name="upstream-aggregate-scheduler",
        )
        thread.start()
        _scheduler_thread = thread
        return True
