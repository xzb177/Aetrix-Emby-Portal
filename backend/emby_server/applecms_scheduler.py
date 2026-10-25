# -*- coding: utf-8 -*-
"""苹果 CMS 定时调度（后续四件之 ①）。

定位（与 #1263 的分工）：
- 本模块只做「到点触发」：按间隔调用一次苹果 CMS 同步（单次有界
  逻辑仍在 ``applecms_sync.sync_source`` 里，本模块不另起一套同步
  实现、不读播放地址、不碰 video_stream 选路）。
- 两闸仍是总门：``APPLECMS_ENABLED`` 与 ``UPSTREAM_SYNC_ENABLED``
  任一关，调度工人就不启动（``start_applecms_scheduler`` 返回
  ``False``）；两闸在运行中被关掉，下一轮 ``run_scheduled_sync``
  经生产触发入口同样返回 disabled 结果，不做任何写操作。
- 间隔走 env，不 hardcode 业务值：``APPLECMS_SYNC_INTERVAL_SEC``
  （默认 21600 秒 = 6 小时；非法/缺失回落默认；小于 60 秒钳制
  到 60 秒，防止误配过短间隔反复刷上游）。
- 启动接线：worker.py 里只加本模块的一段启动块（与变体探活、
  健康同步的接线行互不相干，各自独立）；关时不启动、不发请求，
  全站行为逐字不变。
- 单实例：worker 进程本身有 Redis 单实例锁，调度线程只由 worker
  进程起；幂等：已有存活线程时 ``start_*`` 直接返回 True，不双跑。
"""

from __future__ import annotations

import logging
import os
import threading
from typing import Callable, Iterable, Optional

logger = logging.getLogger(__name__)

#: 定时同步间隔 env 键。
SYNC_INTERVAL_ENV = "APPLECMS_SYNC_INTERVAL_SEC"

#: 默认间隔（秒）：目录同步是低频活，6 小时一轮已够；要更密由
#: 部署方在 env 里显式改，不由代码替部署方定业务节奏。
DEFAULT_SYNC_INTERVAL_SEC = 21600.0

#: 间隔下限（秒）：低于此值钳制，防止误配把上游刷爆。
MIN_SYNC_INTERVAL_SEC = 60.0

_scheduler_thread: threading.Thread | None = None
_scheduler_lock = threading.Lock()


def sync_interval_sec(explicit: Optional[float] = None) -> float:
    """读同步间隔（秒）：显式值优先，否则读 env，非法回落默认。"""
    if explicit is not None:
        try:
            value = float(explicit)
        except (TypeError, ValueError):
            return DEFAULT_SYNC_INTERVAL_SEC
        if value != value:  # NaN
            return DEFAULT_SYNC_INTERVAL_SEC
        return max(MIN_SYNC_INTERVAL_SEC, value)
    try:
        value = float(os.getenv(SYNC_INTERVAL_ENV,
                                str(DEFAULT_SYNC_INTERVAL_SEC)) or
                      str(DEFAULT_SYNC_INTERVAL_SEC))
    except (TypeError, ValueError):
        return DEFAULT_SYNC_INTERVAL_SEC
    if value != value:
        return DEFAULT_SYNC_INTERVAL_SEC
    return max(MIN_SYNC_INTERVAL_SEC, value)


def scheduler_enabled() -> bool:
    """调度是否可启动：复用同步工人的两闸，不另立开关。"""
    from backend.emby_server import applecms_sync as acs

    return acs.gates_enabled()


def run_scheduled_sync(*, session_factory=None, trigger_fn=None,
                       list_sources_fn=None) -> list:
    """跑一轮定时同步，返回每源的结果列表（单源异常隔离到该源）。

    依赖一律可注入（测试不碰真实 DB）：
    - ``trigger_fn(db, source_id)``：默认走生产触发入口
      ``applecms_production.production_trigger_applecms_sync``
      （成功一次 commit 在入口内完成）；
    - ``list_sources_fn()``：默认列出已启用的苹果 CMS 源；
    - ``session_factory``：默认 ``backend.database.SessionLocal``，
      惰性 import（本模块顶层不碰 DB，单测 import 不建库）。
    每源一个独立 session：单源失败不污染其它源的事务。
    """
    if trigger_fn is None or list_sources_fn is None:
        from backend.database import SessionLocal
        from backend.emby_server import applecms_production as acp
        from backend.emby_server import upstream_sources as ups

        if session_factory is None:
            session_factory = SessionLocal

        def _default_list_sources():
            db = session_factory()
            try:
                return [(s.id) for s in ups.list_sources(db)
                        if getattr(s, "source_type", None) == "applecms"
                        and getattr(s, "enabled", False)]
            finally:
                db.close()

        if list_sources_fn is None:
            list_sources_fn = _default_list_sources
        if trigger_fn is None:
            def _default_trigger(db, source_id):
                return acp.production_trigger_applecms_sync(db, source_id)
            trigger_fn = _default_trigger
    results = []
    try:
        source_ids = list(list_sources_fn() or [])
    except Exception:
        logger.exception("applecms 定时同步列源失败")
        return results
    for source_id in source_ids:
        db = session_factory() if session_factory is not None else None
        try:
            results.append(trigger_fn(db, source_id))
        except Exception:
            # 单源异常只记日志、不中断整轮；生产触发入口本身已
            # rollback，本处再兜底一次以防注入的 trigger 未回滚。
            logger.exception("applecms 定时同步单源失败 source_id=%s",
                             source_id)
            try:
                if db is not None:
                    db.rollback()
            except Exception:
                pass
        finally:
            try:
                if db is not None:
                    db.close()
            except Exception:
                pass
    return results


def run_scheduler_loop(stop_event=None, *, run_once=None,
                       interval_sec: Optional[float] = None) -> None:
    """调度循环：先跑一轮，再按间隔等，停表置位即退出。

    ``run_once`` 未传时走 :func:`run_scheduled_sync`（生产路径）；
    等待用 ``stop_event.wait(interval)``（无 stop_event 时用
    ``time.sleep``），使停机不用等满一个间隔。
    单轮异常已在 ``run_scheduled_sync`` 内隔离；注入的 ``run_once``
    抛异常时本循环记日志后继续，不让调度线程悄悄死掉。
    """
    import time

    interval = sync_interval_sec(interval_sec)
    do_run = run_once if run_once is not None else run_scheduled_sync
    while True:
        if stop_event is not None and stop_event.is_set():
            return
        try:
            do_run()
        except Exception:
            logger.exception("applecms 定时同步整轮失败（下一轮继续）")
        if stop_event is not None:
            if stop_event.wait(interval):
                return
        else:
            time.sleep(interval)


def start_applecms_scheduler(stop_event=None, *, run_once=None,
                             interval_sec: Optional[float] = None) -> bool:
    """启动定时调度线程（worker.py 调用，照探活工人的写法）。

    两闸关时返回 False、不启动；幂等：已有存活线程时直接返回 True。
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
            daemon=True, name="applecms-scheduler",
        )
        thread.start()
        _scheduler_thread = thread
        return True
