"""上游目录聚合的进程内后台任务（管理端手动触发）。

管理端 axios 全局超时 30 秒。按库聚合经常超过这个时间，请求被掐断后
前端只能看到「网络暂时连不上」，聚合其实还在服务端跑，或者已经被
客户端放弃。这里把「触发」和「跑完」拆开：

- ``enqueue`` 只登记一条任务并立刻返回 ``task_id`` + ``queued``；
- 后台线程再调 ``run_manual_aggregation``（语义不变：按库拉目录、
  配对、开关开时写映射，单库失败隔离）；
- ``get_task`` 按任务 id 查状态。跑完时 ``result`` 就是
  ``run_manual_aggregation`` 的返回（libraries_discovered / fetched /
  paired / written / failed_libraries 等都在）；失败时 ``error`` 带上
  原来的 HTTP 状态、短码和中文 detail，不吞掉。

任务表在进程内存里。``serve.py`` 是单进程（``workers=1``），和封面
重生成、内存会话同一前提：轮询打到别的进程会 404。进程重启后进行中
的任务消失，已写映射不受影响（写路径仍是 record_mapping 的 upsert）。
苹果 CMS 不进这里，调度器也不走这张表，仍直接调手动聚合入口。
"""

from __future__ import annotations

import logging
import threading
import time
import uuid
from typing import Callable, Optional

from sqlalchemy.exc import IntegrityError

from backend.emby_server import upstream_aggregate_run as uar
from backend.emby_server import upstream_sources as ups

logger = logging.getLogger(__name__)

#: 终态任务保留上限。进行中的不淘汰；超了只丢最老的已结束任务。
_MAX_TASKS = 200

_TERMINAL = ("succeeded", "failed")
_ACTIVE = ("queued", "running")

_lock = threading.Lock()
_tasks: dict[str, dict] = {}

#: 测试注入：不设则用 ``SessionLocal`` / ``run_manual_aggregation`` / 真线程。
_session_factory: Optional[Callable] = None
_aggregate_fn: Optional[Callable] = None


def reset_for_tests() -> None:
    """清空任务表和测试注入（单测隔离用，生产不调）。"""
    global _session_factory, _aggregate_fn
    with _lock:
        _tasks.clear()
    _session_factory = None
    _aggregate_fn = None


def _new_task(source_id: int, page_size: int) -> dict:
    return {
        "task_id": uuid.uuid4().hex,
        "source_id": source_id,
        "page_size": page_size,
        "status": "queued",
        "created_at": time.monotonic(),
        "result": None,
        "error": None,
    }


def _view(task: dict) -> dict:
    """给接口的副本。进行中不带结果；失败不带结果。"""
    status = task["status"]
    return {
        "task_id": task["task_id"],
        "source_id": task["source_id"],
        "status": status,
        "result": task["result"] if status == "succeeded" else None,
        "error": dict(task["error"]) if status == "failed" and task["error"] else None,
    }


def _active_for(source_id: int) -> Optional[dict]:
    for task in _tasks.values():
        if task["source_id"] == source_id and task["status"] in _ACTIVE:
            return task
    return None


def _evict_locked() -> None:
    if len(_tasks) <= _MAX_TASKS:
        return
    terminal = [t for t in _tasks.values() if t["status"] in _TERMINAL]
    terminal.sort(key=lambda t: t["created_at"])
    overflow = len(_tasks) - _MAX_TASKS
    for task in terminal[:overflow]:
        _tasks.pop(task["task_id"], None)


def enqueue(source_id: int, page_size: int) -> dict:
    """登记任务并启动后台线程，立刻返回当前视图（新任务恒为 queued）。

    同一源已有 queued/running 时不另起一轮，返回那条任务的当前视图
    （可能已是 running）。避免两个线程同时写同一源的映射。
    """
    with _lock:
        existing = _active_for(source_id)
        if existing is not None:
            logger.info("上游聚合任务复用进行中的任务 task_id=%s source_id=%s "
                        "status=%s", existing["task_id"], source_id,
                        existing["status"])
            return _view(existing)
        task = _new_task(source_id, page_size)
        _tasks[task["task_id"]] = task
        _evict_locked()
        view = _view(task)
        task_id = task["task_id"]
    logger.info("上游聚合任务入队 task_id=%s source_id=%s", task_id, source_id)
    _submit(task_id)
    return view


def _submit(task_id: str) -> None:
    threading.Thread(
        target=execute_task, args=(task_id,), daemon=True,
        name=f"upstream-agg-{task_id[:8]}",
    ).start()


def get_task(source_id: int, task_id: str) -> Optional[dict]:
    """按源 + 任务 id 取视图。对不上（没有、或属于别的源）返回 None。"""
    with _lock:
        task = _tasks.get(task_id)
        if task is None or task["source_id"] != source_id:
            return None
        return _view(task)


def _mark(task_id: str, *, status: str, result=None, error=None) -> None:
    with _lock:
        task = _tasks.get(task_id)
        if task is None:
            return
        task["status"] = status
        task["result"] = result
        task["error"] = error


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


def execute_task(task_id: str) -> None:
    """跑一条已入队的任务。只处理仍是 queued 的；重复进入直接返回。

    会话是线程自己开的，不用请求上的 Session（请求返回后那个会话就关了）。
    ``run_manual_aggregation`` 只 flush，提交在这里。已知业务错误
    （``UpstreamAggregateRunError``、映射唯一冲突）照原状态码和文案
    记到任务上；其余异常记日志后落成 500，不把异常原文回给前端。
    """
    with _lock:
        task = _tasks.get(task_id)
        if task is None or task["status"] != "queued":
            return
        task["status"] = "running"
        source_id = task["source_id"]
        page_size = task["page_size"]
    try:
        db = _open_session()
    except Exception:
        logger.exception("上游聚合任务开会话失败 task_id=%s source_id=%s",
                         task_id, source_id)
        _mark(task_id, status="failed", error={
            "status": 500, "code": "aggregate_failed",
            "detail": "聚合失败，请稍后重试",
        })
        return
    try:
        source = ups.get_source(db, source_id)
        if source is None:
            _mark(task_id, status="failed", error={
                "status": 404, "code": "source_not_found",
                "detail": "上游源不存在",
            })
            logger.info("上游聚合任务失败 task_id=%s source_id=%s code=%s",
                        task_id, source_id, "source_not_found")
            return
        fn = _aggregate_fn or uar.run_manual_aggregation
        result = fn(db, source, page_size=page_size)
        if not isinstance(result, dict):
            _rollback(db)
            _mark(task_id, status="failed", error={
                "status": 500, "code": "aggregate_failed",
                "detail": "聚合失败，请稍后重试",
            })
            logger.error("上游聚合任务返回了非 dict task_id=%s source_id=%s",
                         task_id, source_id)
            return
        db.commit()
        _mark(task_id, status="succeeded", result=result)
        logger.info(
            "上游聚合任务完成 task_id=%s source_id=%s fetched=%s "
            "paired=%s written=%s failed_libraries=%s",
            task_id, source_id, result.get("fetched"), result.get("paired"),
            result.get("written"), len(result.get("failed_libraries") or []))
    except uar.UpstreamAggregateRunError as exc:
        _rollback(db)
        _mark(task_id, status="failed", error={
            "status": exc.status, "code": exc.code, "detail": exc.message,
        })
        logger.info("上游聚合任务失败 task_id=%s source_id=%s code=%s status=%s",
                    task_id, source_id, exc.code, exc.status)
    except IntegrityError:
        _rollback(db)
        _mark(task_id, status="failed", error={
            "status": 409, "code": "mapping_conflict",
            "detail": "映射写入冲突，请重试",
        })
        logger.exception("上游聚合任务映射冲突 task_id=%s source_id=%s",
                         task_id, source_id)
    except Exception:
        _rollback(db)
        _mark(task_id, status="failed", error={
            "status": 500, "code": "aggregate_failed",
            "detail": "聚合失败，请稍后重试",
        })
        logger.exception("上游聚合任务异常 task_id=%s source_id=%s",
                         task_id, source_id)
    finally:
        _close(db)
