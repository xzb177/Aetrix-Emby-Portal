"""上游目录聚合的后台任务（管理端手动触发；2026-10-31 O-01 持久化）。

管理端 axios 全局超时 30 秒。按库聚合经常超过这个时间，请求被掐断后
前端只能看到「网络暂时连不上」。「触发」和「跑完」拆开：

- ``enqueue`` 只在任务表（``upstream_aggregate_tasks``）登记一行并
  立刻返回 ``task_id`` + ``queued``，执行线程随后认领执行；
- 执行走 ``run_manual_aggregation`` 的逐库事务口径
  （``commit_each_library=True``）：一库跑完即提交该库映射，撞
  映射冲突只回滚该库；进度经 ``on_event`` 回调逐库写回任务行；
- ``get_task`` 按任务 id 读任务行。跑完时 ``result`` 是聚合摘要
  （libraries_discovered / fetched / paired / written /
  failed_libraries 等都在），**不带全量 pairs**（明细走映射表
  分页查，见设计稿 P-12）；失败时 ``error`` 带原 HTTP 状态、短码
  和中文 detail，不吞掉。

为什么落库（O-01，解 P-01/P-02/P-12）：此前任务只活在 api 进程
内存里，部署重启 api 即整轮蒸发、状态无处可查（2026-10-31 实证：
15:33 手动聚合被 15:47 的部署重启冲掉，约 14 分钟工作全损）。
现在任务行在 DB 里：重启后终态可查、进行中任务靠心跳判活——
``running`` 且心跳过期（SystemConfig
``upstream_aggregate_heartbeat_stale_sec``，默认 120 秒）的任务
在入队清扫时标 ``interrupted``，可经 ``resume_task`` 续跑（跳过
已完成的库）。执行面统一到 worker 是 O-03：本模块的 enqueue 暂
仍在 api 进程内起线程执行，认领语义（queued → running 的条件
更新）已按跨进程口径写，O-03 只换认领方、不换任务表。

苹果 CMS 不进这里；调度器（O-03 前）仍直接调聚合入口。
"""

from __future__ import annotations

import json
import logging
import threading
import uuid
from datetime import datetime, timedelta
from typing import Callable, Optional

from sqlalchemy import func
from sqlalchemy.exc import IntegrityError

from backend.emby_server import models as em
from backend.emby_server import upstream_aggregate_run as uar
from backend.emby_server import upstream_sources as ups

logger = logging.getLogger(__name__)

#: 任务状态集（与 models.UPSTREAM_AGGREGATE_TASK_STATUSES 对齐）。
_TERMINAL = ("succeeded", "failed", "interrupted")
_ACTIVE = ("queued", "running")

#: 心跳过期阈值（秒）SystemConfig 键：running 任务心跳超此值判
#: 已消亡、标 interrupted。默认 120（设计稿 O-01），钳 [30, 3600]。
KEY_HEARTBEAT_STALE_SEC = "upstream_aggregate_heartbeat_stale_sec"
HEARTBEAT_STALE_DEFAULT = 120

#: 终态任务保留天数 SystemConfig 键：超期行在入队时顺手清掉。
#: 默认 30（设计稿 O-01），钳 [1, 365]。
KEY_TASK_RETENTION_DAYS = "upstream_aggregate_task_retention_days"
TASK_RETENTION_DEFAULT = 30

_lock = threading.Lock()

#: 测试注入：不设则用 ``SessionLocal`` / ``run_manual_aggregation`` / 真线程。
_session_factory: Optional[Callable] = None
_aggregate_fn: Optional[Callable] = None


def reset_for_tests() -> None:
    """清空测试注入（单测隔离用，生产不调）。任务行在 DB 里，
    随测试引擎一起消亡，不在这里删。"""
    global _session_factory, _aggregate_fn
    _session_factory = None
    _aggregate_fn = None


# ---------------------------------------------------------------------------
# 会话与配置
# ---------------------------------------------------------------------------

def _open_session():
    factory = _session_factory
    if factory is None:
        from backend.database import SessionLocal
        factory = SessionLocal
    return factory()


def _rollback(db) -> None:
    try:
        db.rollback()
    except Exception:
        logger.exception("上游聚合任务回滚失败")


def _close(db) -> None:
    try:
        db.close()
    except Exception:
        logger.exception("上游聚合任务关闭会话失败")


def heartbeat_stale_sec(db) -> int:
    """心跳过期阈值（秒）：SystemConfig > 默认 120，钳 [30, 3600]。"""
    configured = uar._system_config_int(db, KEY_HEARTBEAT_STALE_SEC)
    if configured is not None:
        return max(30, min(3600, configured))
    return HEARTBEAT_STALE_DEFAULT


def task_retention_days(db) -> int:
    """终态任务保留天数：SystemConfig > 默认 30，钳 [1, 365]。"""
    configured = uar._system_config_int(db, KEY_TASK_RETENTION_DAYS)
    if configured is not None:
        return max(1, min(365, configured))
    return TASK_RETENTION_DEFAULT


# ---------------------------------------------------------------------------
# 任务行读写
# ---------------------------------------------------------------------------

def _get_row(db, task_id: str):
    return (db.query(em.UpstreamAggregateTask)
            .filter(em.UpstreamAggregateTask.task_id == task_id)
            .first())


def _active_row_for(db, source_id: int):
    """某源当前进行中的任务行（queued/running，取最新一条）。"""
    return (db.query(em.UpstreamAggregateTask)
            .filter(em.UpstreamAggregateTask.source_id == source_id,
                    em.UpstreamAggregateTask.status.in_(_ACTIVE))
            .order_by(em.UpstreamAggregateTask.id.desc())
            .first())


def _iso(value) -> Optional[str]:
    return value.isoformat() if value is not None else None


def _view_from_row(row) -> dict:
    """任务行 → 接口视图。进行中不带结果；失败不带结果（同旧口径），
    另带进度计数与时间戳（O-01 落库后这些字段重启可查）。"""
    status = row.status
    result = None
    if status == "succeeded" and row.result_json:
        try:
            result = json.loads(row.result_json)
        except ValueError:
            result = None
    error = None
    if status == "failed" and row.error_json:
        try:
            error = json.loads(row.error_json)
        except ValueError:
            error = None
    return {
        "task_id": row.task_id,
        "source_id": row.source_id,
        "status": status,
        "result": result,
        "error": error,
        "trigger": row.trigger,
        "resume_of": row.resume_of,
        "libraries_total": row.libraries_total or 0,
        "libraries_done": row.libraries_done or 0,
        "current_library_id": row.current_library_id,
        "current_library_name": row.current_library_name,
        "fetched": row.fetched or 0,
        "paired": row.paired or 0,
        "written": row.written or 0,
        "created_at": _iso(row.created_at),
        "started_at": _iso(row.started_at),
        "finished_at": _iso(row.finished_at),
        "heartbeat_at": _iso(row.heartbeat_at),
    }


def mark_stale_interrupted(db, *, now: Optional[datetime] = None) -> int:
    """把心跳过期的 running 任务标 interrupted（只 flush，调用方提交）。

    执行进程消亡后任务行会永远停在 running；心跳（入队时与逐库
    进度时更新）超阈值即判已消亡。标完可 resume 续跑。
    """
    now = now or datetime.now()
    threshold = now - timedelta(seconds=heartbeat_stale_sec(db))
    rows = (db.query(em.UpstreamAggregateTask)
            .filter(em.UpstreamAggregateTask.status == "running",
                    func.coalesce(em.UpstreamAggregateTask.heartbeat_at,
                                  em.UpstreamAggregateTask.created_at)
                    < threshold)
            .all())
    for row in rows:
        row.status = "interrupted"
        row.finished_at = now
        logger.warning("上游聚合任务心跳过期标中断 task_id=%s source_id=%s",
                       row.task_id, row.source_id)
    if rows:
        db.flush()
    return len(rows)


def cleanup_terminal_tasks(db, *, now: Optional[datetime] = None) -> int:
    """删超保留期的终态任务行（只 flush，调用方提交）。"""
    now = now or datetime.now()
    cutoff = now - timedelta(days=task_retention_days(db))
    deleted = (db.query(em.UpstreamAggregateTask)
               .filter(em.UpstreamAggregateTask.status.in_(_TERMINAL),
                       func.coalesce(em.UpstreamAggregateTask.finished_at,
                                     em.UpstreamAggregateTask.created_at)
                       < cutoff)
               .delete(synchronize_session=False))
    return int(deleted or 0)


def _create_row(db, *, source_id: int, page_size: int, trigger: str,
                resume_of: Optional[str] = None,
                skip_library_ids=None):
    now = datetime.now()
    row = em.UpstreamAggregateTask(
        task_id=uuid.uuid4().hex,
        source_id=source_id,
        trigger=trigger,
        status="queued",
        page_size=page_size,
        resume_of=resume_of,
        skip_libraries_json=(json.dumps(list(skip_library_ids))
                             if skip_library_ids else None),
        created_at=now,
        heartbeat_at=now,
    )
    db.add(row)
    db.flush()
    return row


# ---------------------------------------------------------------------------
# 入队 / 查询 / 续跑
# ---------------------------------------------------------------------------

def enqueue(source_id: int, page_size: int, *, trigger: str = "manual") -> dict:
    """登记任务行并启动执行，立刻返回当前视图（新任务恒为 queued）。

    同一源已有 queued/running 时不另起一轮，返回那条任务的当前
    视图（可能已是 running）。入队前顺手做两件维护：心跳过期的
    running 标 interrupted、超保留期的终态行清掉。
    """
    with _lock:
        db = _open_session()
        try:
            mark_stale_interrupted(db)
            cleanup_terminal_tasks(db)
            existing = _active_row_for(db, source_id)
            if existing is not None:
                db.commit()
                view = _view_from_row(existing)
                logger.info(
                    "上游聚合任务复用进行中的任务 task_id=%s source_id=%s "
                    "status=%s", view["task_id"], source_id, view["status"])
                return view
            row = _create_row(db, source_id=source_id,
                              page_size=page_size, trigger=trigger)
            db.commit()
            view = _view_from_row(row)
            task_id = row.task_id
        finally:
            _close(db)
    logger.info("上游聚合任务入队 task_id=%s source_id=%s trigger=%s",
                task_id, source_id, trigger)
    _submit(task_id)
    return view


def resume_task(source_id: int, task_id: str) -> Optional[dict]:
    """续跑一条 interrupted / failed 任务：新建任务行跳过已完成库。

    任务不存在 / 不属于该源 / 状态不在可续跑集 → None（同源已有
    进行中任务时返回那条的视图，与 enqueue 同口径）。
    """
    with _lock:
        db = _open_session()
        try:
            mark_stale_interrupted(db)
            db.commit()
            row = _get_row(db, task_id)
            if row is None or row.source_id != source_id:
                return None
            existing = _active_row_for(db, source_id)
            if existing is not None:
                view = _view_from_row(existing)
                logger.info(
                    "上游聚合续跑复用进行中的任务 task_id=%s source_id=%s",
                    view["task_id"], source_id)
                return view
            if row.status not in ("interrupted", "failed"):
                return None
            done_ids: list = []
            if row.libraries_json:
                try:
                    for lib in json.loads(row.libraries_json):
                        if (isinstance(lib, dict)
                                and lib.get("status") == "ok"
                                and lib.get("library_id") is not None):
                            done_ids.append(str(lib["library_id"]))
                except ValueError:
                    done_ids = []
            new_row = _create_row(
                db, source_id=source_id, page_size=row.page_size,
                trigger="manual", resume_of=row.task_id,
                skip_library_ids=done_ids)
            db.commit()
            view = _view_from_row(new_row)
            new_task_id = new_row.task_id
        finally:
            _close(db)
    logger.info("上游聚合任务续跑 task_id=%s resume_of=%s source_id=%s "
                "skip_libraries=%s", new_task_id, task_id, source_id,
                len(done_ids))
    _submit(new_task_id)
    return view


def get_task(source_id: int, task_id: str) -> Optional[dict]:
    """按源 + 任务 id 读任务行视图。对不上（没有、或属于别的源）返回 None。"""
    db = _open_session()
    try:
        row = _get_row(db, task_id)
        if row is None or row.source_id != source_id:
            return None
        return _view_from_row(row)
    finally:
        _close(db)


def _submit(task_id: str) -> None:
    threading.Thread(
        target=execute_task, args=(task_id,), daemon=True,
        name=f"upstream-agg-{task_id[:8]}",
    ).start()


# ---------------------------------------------------------------------------
# 执行
# ---------------------------------------------------------------------------

def _make_progress_handler(task_id: str) -> Callable:
    """聚合进度回调：把逐库进度写回任务行（独立短会话逐事件提交）。

    独立会话的用意：进度是观测面，不能搭执行会话的事务——执行
    会话回滚单库时进度不许陪葬；同一库先 done 后 failed（如提交
    期冲突）时按 library_id 替换明细，最终以最后一次事件为准。
    计数口径与逐库事务的结果口径一致：fetched 计全部已处理库，
    paired/written 只计 status=ok 的库。
    """

    def handle(event: dict) -> None:
        etype = event.get("type")
        if etype not in ("libraries_discovered", "library_done",
                        "library_failed"):
            return
        db = _open_session()
        try:
            row = _get_row(db, task_id)
            if row is None:
                return
            if etype == "libraries_discovered":
                total = event.get("total")
                if total is None:
                    total = len(event.get("libraries") or [])
                row.libraries_total = int(total or 0)
            else:
                summary = event.get("summary") or {}
                libs = []
                if row.libraries_json:
                    try:
                        libs = json.loads(row.libraries_json)
                    except ValueError:
                        libs = []
                libs = [x for x in libs
                        if not (isinstance(x, dict)
                                and x.get("library_id")
                                == summary.get("library_id"))]
                libs.append(summary)
                row.libraries_json = json.dumps(libs, ensure_ascii=False)
                row.libraries_done = sum(
                    1 for x in libs
                    if isinstance(x, dict) and x.get("status") == "ok")
                row.fetched = sum(
                    int(x.get("fetched") or 0) for x in libs
                    if isinstance(x, dict))
                row.paired = sum(
                    int(x.get("paired") or 0) for x in libs
                    if isinstance(x, dict) and x.get("status") == "ok")
                row.written = sum(
                    int(x.get("written") or 0) for x in libs
                    if isinstance(x, dict) and x.get("status") == "ok")
                row.current_library_id = summary.get("library_id")
                row.current_library_name = summary.get("library_name")
            row.heartbeat_at = datetime.now()
            db.commit()
        except Exception:
            _rollback(db)
            logger.exception("上游聚合任务进度写入失败 task_id=%s type=%s",
                             task_id, etype)
        finally:
            _close(db)

    return handle


def _finish(task_id: str, *, status: str, result: Optional[dict] = None,
            error: Optional[dict] = None) -> None:
    """写终态（独立会话 + 条件更新）：只许从 queued/running 落终态，
    已被标 interrupted 的不覆盖（它的成果以已提交库为准）。"""
    db = _open_session()
    try:
        values: dict = {"status": status,
                        "finished_at": datetime.now(),
                        "heartbeat_at": datetime.now()}
        if result is not None:
            summary = {k: v for k, v in result.items() if k != "pairs"}
            values["result_json"] = json.dumps(summary, ensure_ascii=False)
            values["fetched"] = int(result.get("fetched") or 0)
            values["paired"] = int(result.get("paired") or 0)
            values["written"] = int(result.get("written") or 0)
            values["libraries_total"] = int(
                result.get("libraries_discovered") or 0)
            libraries = result.get("libraries") or []
            values["libraries_done"] = sum(
                1 for x in libraries
                if isinstance(x, dict) and x.get("status") == "ok")
            values["libraries_json"] = json.dumps(
                libraries, ensure_ascii=False)
            values["unpaired_reasons_json"] = json.dumps(
                result.get("unpaired_reasons") or {}, ensure_ascii=False)
        if error is not None:
            values["error_json"] = json.dumps(error, ensure_ascii=False)
        updated = (db.query(em.UpstreamAggregateTask)
                   .filter(em.UpstreamAggregateTask.task_id == task_id,
                           em.UpstreamAggregateTask.status.in_(_ACTIVE))
                   .update(values, synchronize_session=False))
        db.commit()
        if not updated:
            logger.warning("上游聚合任务写终态时状态已变化 task_id=%s "
                           "status=%s", task_id, status)
    except Exception:
        _rollback(db)
        logger.exception("上游聚合任务写终态失败 task_id=%s", task_id)
    finally:
        _close(db)


def execute_task(task_id: str) -> None:
    """跑一条已入队的任务。认领是条件更新（queued → running）：
    重复进入 / 已被认领直接返回（跨进程口径，O-03 的 worker 认领
    走同一路径）。

    会话是执行方自己开的，不用请求上的 Session（请求返回后那个
    会话就关了）。聚合走逐库事务：``run_manual_aggregation`` 只在
    逐库边界 commit，任务级失败（凭据/未启用等发生在首库之前，
    或整轮异常）在这里落 failed：已知业务错误照原状态码与文案
    记到任务上；其余异常记日志后落成 500，不把异常原文回给前端。
    """
    db = _open_session()
    try:
        now = datetime.now()
        claimed = (db.query(em.UpstreamAggregateTask)
                   .filter(em.UpstreamAggregateTask.task_id == task_id,
                           em.UpstreamAggregateTask.status == "queued")
                   .update({"status": "running", "started_at": now,
                            "heartbeat_at": now},
                           synchronize_session=False))
        db.commit()
        if not claimed:
            return
        row = _get_row(db, task_id)
        source_id = row.source_id
        page_size = row.page_size
        resume_of = row.resume_of
        skip_ids = []
        if row.skip_libraries_json:
            try:
                skip_ids = [str(x) for x in
                            json.loads(row.skip_libraries_json)]
            except ValueError:
                skip_ids = []
    finally:
        _close(db)

    db = _open_session()
    try:
        source = ups.get_source(db, source_id)
        if source is None:
            _finish(task_id, status="failed", error={
                "status": 404, "code": "source_not_found",
                "detail": "上游源不存在",
            })
            logger.info("上游聚合任务失败 task_id=%s source_id=%s code=%s",
                        task_id, source_id, "source_not_found")
            return
        prior: dict = {}
        if resume_of:
            prev = _get_row(db, resume_of)
            if prev is not None and prev.libraries_json:
                try:
                    for lib in json.loads(prev.libraries_json):
                        if (isinstance(lib, dict)
                                and lib.get("status") == "ok"
                                and lib.get("library_id") is not None):
                            prior[str(lib["library_id"])] = lib
                except ValueError:
                    prior = {}
        fn = _aggregate_fn or uar.run_manual_aggregation
        result = fn(db, source, page_size=page_size,
                    on_event=_make_progress_handler(task_id),
                    commit_each_library=True,
                    skip_library_ids=skip_ids,
                    prior_summaries=prior)
        if not isinstance(result, dict):
            _rollback(db)
            _finish(task_id, status="failed", error={
                "status": 500, "code": "aggregate_failed",
                "detail": "聚合失败，请稍后重试",
            })
            logger.error("上游聚合任务返回了非 dict task_id=%s source_id=%s",
                         task_id, source_id)
            return
        db.commit()
        _finish(task_id, status="succeeded", result=result)
        logger.info(
            "上游聚合任务完成 task_id=%s source_id=%s fetched=%s "
            "paired=%s written=%s failed_libraries=%s",
            task_id, source_id, result.get("fetched"), result.get("paired"),
            result.get("written"), len(result.get("failed_libraries") or []))
    except uar.UpstreamAggregateRunError as exc:
        _rollback(db)
        _finish(task_id, status="failed", error={
            "status": exc.status, "code": exc.code, "detail": exc.message,
        })
        logger.info("上游聚合任务失败 task_id=%s source_id=%s code=%s status=%s",
                    task_id, source_id, exc.code, exc.status)
    except IntegrityError:
        _rollback(db)
        _finish(task_id, status="failed", error={
            "status": 409, "code": "mapping_conflict",
            "detail": "映射写入冲突，请重试",
        })
        logger.exception("上游聚合任务映射冲突 task_id=%s source_id=%s",
                         task_id, source_id)
    except Exception:
        _rollback(db)
        _finish(task_id, status="failed", error={
            "status": 500, "code": "aggregate_failed",
            "detail": "聚合失败，请稍后重试",
        })
        logger.exception("上游聚合任务异常 task_id=%s source_id=%s",
                         task_id, source_id)
    finally:
        _close(db)
