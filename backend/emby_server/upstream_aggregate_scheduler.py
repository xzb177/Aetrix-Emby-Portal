# -*- coding: utf-8 -*-
"""上游聚合自动调度骨架（准备线，开关默认关）。

定位（与 #1352 系手动聚合的分工）：
- 手动聚合仍是唯一已验证的入口：管理端
  ``POST /api/admin/emby/upstream-sources/{id}/aggregate`` 入队后，
  后台线程调 ``upstream_aggregate_run.run_manual_aggregation``
  （按库拉目录 → 聚合 → 配对 → 开关开时写映射，单库失败隔离）。
  本调度不走那张任务表，仍在调度线程里直接调同一入口。
- 本模块只做「到点触发」：按间隔对已启用的 Emby / Jellyfin 型源
  逐个调用同一手动聚合入口，不另起一套聚合实现、不读播放地址、
  不碰 video_stream 选路、不改手动聚合的任何语义。
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
- 每源一个独立 session：单源异常（凭据失效、额度、上游不可用、
  其他异常）只记该源 failed、rollback 后继续下一源，不毁已写映射。
- 每轮、每源结果均经 ``logger`` 输出（轮次开始/结束、每源 ok /
  failed / skipped 与计数），调度日志可查；返回值同样带逐源明细，
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


def run_scheduled_aggregation(*, session_factory=None, aggregate_fn=None,
                              list_sources_fn=None) -> list:
    """跑一轮定时聚合，返回每源的结果列表（单源异常隔离到该源）。

    依赖一律可注入（测试不碰真实 DB、不发真实请求）：
    - ``list_sources_fn()``：默认列出已启用的 Emby / Jellyfin 型源行；
    - ``aggregate_fn(db, source)``：默认走生产手动聚合入口
      （``run_manual_aggregation`` + 调用方 commit；见
      ``_default_aggregate``），返回聚合结果 dict；
    - ``session_factory``：默认 ``backend.database.SessionLocal``，
      惰性 import（本模块顶层不碰 DB，单测 import 不建库）。

    每源结果形态：``{"source_id", "status", ...}``，status 为
    ``ok`` / ``failed`` / ``skipped`` 之一；failed 带 ``code`` /
    ``error``，skipped 带 ``reason``。列源本身失败时返回空列表并
    记日志，不抛错（调度线程不因此死掉）。
    """
    if aggregate_fn is None or list_sources_fn is None:
        from backend.database import SessionLocal
        from backend.emby_server import upstream_aggregate_run as uar
        from backend.emby_server import upstream_sources as ups

        if session_factory is None:
            session_factory = SessionLocal

        def _default_list_sources():
            db = session_factory()
            try:
                return [s for s in ups.list_sources(db)
                        if getattr(s, "source_type", "") != "applecms"
                        and getattr(s, "enabled", False)]
            finally:
                db.close()

        if list_sources_fn is None:
            list_sources_fn = _default_list_sources

        if aggregate_fn is None:
            def _default_aggregate(db, source):
                result = uar.run_manual_aggregation(db, source)
                db.commit()
                return result

            aggregate_fn = _default_aggregate

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
        # 单源隔离（完美审查第四轮）：开会话与取结果都在 try 内——
        # 开会话失败若在 try 外，本轮直接抛掉、已跑完的源结果一并丢失；
        # 非 dict 返回若按 ok 记，调度日志会谎称成功（fetched/written 全 None）。
        db = None
        try:
            db = session_factory() if session_factory is not None else None
            outcome = aggregate_fn(db, source)
            if not isinstance(outcome, dict):
                raise TypeError("聚合返回非 dict，无法记调度结果")
            fetched = outcome.get("fetched")
            written = outcome.get("written")
            logger.info("上游聚合定时调度单源完成 source_id=%s fetched=%s "
                        "written=%s", source_id, fetched, written)
            results.append({"source_id": source_id, "status": "ok",
                            "fetched": fetched, "written": written})
        except Exception as exc:  # noqa: BLE001 - 单源隔离：任何异常都不毁整轮
            code = getattr(exc, "code", None) or exc.__class__.__name__
            logger.exception("上游聚合定时调度单源失败 source_id=%s code=%s",
                             source_id, code)
            try:
                if db is not None:
                    db.rollback()
            except Exception:
                pass
            results.append({"source_id": source_id, "status": "failed",
                            "code": code, "error": str(exc)})
        finally:
            try:
                if db is not None:
                    db.close()
            except Exception:
                pass
    ok_count = sum(1 for r in results if r["status"] == "ok")
    failed_count = sum(1 for r in results if r["status"] == "failed")
    logger.info("上游聚合定时调度本轮结束 sources=%s ok=%s failed=%s",
                len(results), ok_count, failed_count)
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
