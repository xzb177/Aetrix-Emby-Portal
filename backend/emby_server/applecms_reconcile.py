# -*- coding: utf-8 -*-
"""苹果 CMS 月度全量对账（新媒体库类型一期·对账单）。

口径（2026-11-02 定案）：增量靠 provide ``h=24`` 窗口日跑（同步
单），删除与漂移靠月度全量对账收尾——

1. 先跑一轮**全量**同步（``sync_library(full=True)``，不带 h
   窗口），拿到每个源本轮在目录里实际见到的全部 vod_id
   （seen 在分类过滤之前记录：在不在源目录里，与本库收不收
   是两件事）；
2. 完整性红线：任一配置源本轮出错（拉取失败 / 源缺失 / 停用）
   或同步被 blocked，seen 集合不可信，整轮**不对账**——抖动
   绝不许误判成下架（``reconcile_hosted_lifecycle`` 的调用前提
   本就要求整轮可信）；
3. 完整跑完才对每源调既有 ``reconcile_hosted_lifecycle``：
   没见到的托管行走既有宽限计数（2 轮不见隐藏、累计 4 轮软删、
   重现恢复），本模块不另造生命周期语义。月度节奏下，一部片在
   源下架后约 2 个月隐藏、4 个月软删——克制优先，照既有机制。

调度：月度全量走独立开关 ``APPLECMS_LIBRARY_RECONCILE_ENABLED``
（默认关）+ 间隔 ``APPLECMS_LIBRARY_RECONCILE_INTERVAL_SEC``
（默认 2592000 = 30 天）。与增量调度分开两道闸：增量开着不等于
对账开着，防「顺手把删除对账也开了」。
"""

from __future__ import annotations

import logging
import os
import threading
import time
from typing import Dict, List, Optional

from sqlalchemy.orm import Session

from backend.emby_server import applecms_library as acl
from backend.emby_server import applecms_library_sync as als
from backend.emby_server import models as em
from backend.emby_server import upstream_import as uimp
from backend.emby_server import upstream_sources as us

logger = logging.getLogger(__name__)

#: 月度对账总开关 env 键（默认关，与增量同步开关分开）。
RECONCILE_ENABLED_ENV = "APPLECMS_LIBRARY_RECONCILE_ENABLED"
#: 对账间隔 env 键（秒），默认 30 天。
RECONCILE_INTERVAL_ENV = "APPLECMS_LIBRARY_RECONCILE_INTERVAL_SEC"
DEFAULT_RECONCILE_INTERVAL_SEC = 2592000.0

_scheduler_thread: Optional[threading.Thread] = None
_scheduler_lock = threading.Lock()


def reconcile_enabled() -> bool:
    return (os.environ.get(RECONCILE_ENABLED_ENV) or "").strip() == "1"


def reconcile_interval_sec(explicit: Optional[float] = None) -> float:
    if explicit is not None:
        return max(1.0, float(explicit))
    raw = (os.environ.get(RECONCILE_INTERVAL_ENV) or "").strip()
    try:
        value = float(raw) if raw else DEFAULT_RECONCILE_INTERVAL_SEC
    except ValueError:
        value = DEFAULT_RECONCILE_INTERVAL_SEC
    return max(3600.0, value)


def reconcile_library(db: Session, library, *, http_get=None,
                      sleep_fn=None) -> Dict[str, object]:
    """一个库的月度对账：全量同步 + 完整性校验 + 生命周期对账。"""
    out: Dict[str, object] = {
        "library_id": int(library.id), "reconciled": False,
        "reason": None, "sync": None, "lifecycle": {},
    }
    stats = als.sync_library(db, library, http_get=http_get,
                             full=True, sleep_fn=sleep_fn)
    out["sync"] = stats
    if stats.get("blocked"):
        out["reason"] = str(stats["blocked"])
        return out
    if stats.get("errors"):
        # 任一源不完整 = seen 不可信，整轮不对账（红线）。
        out["reason"] = "partial_sync"
        logger.warning(
            "applecms 对账跳过 library_id=%s errors=%s",
            library.id, stats["errors"])
        return out
    lifecycle: Dict[int, object] = {}
    for source_id, seen in (stats.get("seen_by_source") or {}).items():
        source = us.get_source(db, int(source_id))
        if source is None:
            continue
        lifecycle[int(source_id)] = uimp.reconcile_hosted_lifecycle(
            db, source, set(seen))
    db.commit()
    out["lifecycle"] = lifecycle
    out["reconciled"] = True
    return out


def run_reconcile_round(*, session_factory=None, http_get=None
                        ) -> List[dict]:
    """一轮对账调度：对全部已启用的 CMS 库逐个对账。开关关返回 []。"""
    if not reconcile_enabled():
        return []
    if session_factory is None:
        from backend.database import SessionLocal
        session_factory = SessionLocal
    results: List[dict] = []
    db = session_factory()
    try:
        libraries = (
            db.query(em.Library)
            .filter(em.Library.kind == acl.KIND_APPLECMS,
                    em.Library.is_enabled.is_(True))
            .all())
        library_ids = [lib.id for lib in libraries]
    finally:
        db.close()
    for library_id in library_ids:
        lib_db = session_factory()
        try:
            row = lib_db.get(em.Library, library_id)
            if row is None:
                continue
            results.append(reconcile_library(
                lib_db, row, http_get=http_get))
        except Exception:
            logger.exception("applecms 对账整库失败 library_id=%s",
                             library_id)
            try:
                lib_db.rollback()
            except Exception:
                pass
        finally:
            lib_db.close()
    return results


def run_scheduler_loop(stop_event=None, *, run_once=None,
                       interval_sec: Optional[float] = None) -> None:
    do_run = run_once if run_once is not None else run_reconcile_round
    interval = reconcile_interval_sec(interval_sec)
    while True:
        if stop_event is not None and stop_event.is_set():
            return
        try:
            do_run()
        except Exception:
            logger.exception("applecms 对账整轮失败（下一轮继续）")
        if stop_event is not None:
            if stop_event.wait(interval):
                return
        else:
            time.sleep(interval)


def start_reconcile_scheduler(stop_event=None) -> bool:
    """启动对账调度线程；开关关或已启动时返回 False。"""
    global _scheduler_thread
    if not reconcile_enabled():
        return False
    with _scheduler_lock:
        if _scheduler_thread is not None and _scheduler_thread.is_alive():
            return False
        thread = threading.Thread(
            target=run_scheduler_loop, args=(stop_event,),
            name="applecms-library-reconcile", daemon=True)
        thread.start()
        _scheduler_thread = thread
    return True
