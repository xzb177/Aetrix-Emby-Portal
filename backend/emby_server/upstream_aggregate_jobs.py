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
已完成的库）。

执行面统一到 worker（O-03，解 P-04）：api 只入队（写任务行）不
执行；worker 进程的认领循环（``claim_queued_tasks`` /
``start_aggregate_worker``，worker.py 接线）按 FIFO 认领 queued
任务并在 worker 进程内执行。认领语义（queued → running 的条件
更新）按跨进程口径写，且认领时同源串行：同一 source_id 已有
running 任务时，后入队的同源任务暂不认领、留在 queued 等下一
轮——手动（api）与调度（worker）从此走同一张表、同一认领口径，
跨进程的同源双跑被任务表本身挡住。调度口（A-120）同样只入队：
``enqueue_scheduled``，进行中复用即调度轮的去重语义。

审计 A-117 / A-118（2026-10-31，随 #1620 先行落地；O-03 整合后
机制保留、位置随执行面移到认领侧）：
- A-117：``_submit`` 曾裸调 ``Thread.start()``，启动失败时异常穿
  出、行永卡 queued；且无 queued 清扫、去重又把 queued 计入进行
  中，致死行永复用。现：认领提交走 ``_submit`` 兜底（启动失败记
  failed、不抛）、入队维护清扫超龄 queued 行（600 秒无认领即
  failed）、复用到的 queued 行由认领循环在轮询间隔内接手（认领
  是条件更新，重复进入无害）。
- A-118：心跳曾只在库边界刷新，长库窗内无心跳、跑超 120 秒即被
  误判死亡、二次触发致双跑。现：执行侧每窗调轻量心跳回调（只刷
  heartbeat，不碰执行事务）；``mark_stale_interrupted`` 跳过本进
  程仍有活线程的任务（跨进程死亡仍由心跳兜底）。

苹果 CMS 不进这里。
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

#: worker 认领轮询间隔（秒）SystemConfig 键：认领循环每轮扫一次
#: queued 任务行。默认 5（设计稿 O-03），钳 [1, 60]；键已在
#: config_schema 的 upstream_aggregate 家族登记，本模块是唯一读取方。
KEY_CLAIM_POLL_SEC = "upstream_aggregate_claim_poll_sec"
CLAIM_POLL_DEFAULT = 5

#: worker 认领循环单轮提交上限：防止某轮 queued 积压时一次起太多线程。
CLAIM_BATCH_LIMIT = 50

_lock = threading.Lock()

#: 本进程活执行线程登记（A-118）：task_id → 执行线程。认领成功后
#: 登记、执行结束（任何出口）后注销；``mark_stale_interrupted``
#: 跳过登记在册的任务，避免“同进程活任务被自己人判死”。跨进程
#: 的任务不在此表，仍由心跳过期兜底。
_live_lock = threading.Lock()
_LIVE: dict = {}

#: queued 超龄阈值（秒，A-117）：入队后这么久仍无人认领即判启动
#: 失败。认领由 worker 循环轮询（默认 5 秒），600 秒是给 worker
#: 停摆 / 部署重启的宽限，不是正常排队等待。纯模块常量，不新增
#: 配置键（与 heartbeat 阈值分开：后者量的是 running 任务的
#: 存活，前者量的是 queued 行的认领）。
QUEUED_STALE_DEFAULT = 600

#: worker 认领循环线程（start_aggregate_worker 幂等登记，照调度器写法）。
_claim_thread: Optional[threading.Thread] = None
_claim_lock = threading.Lock()

#: 测试注入：不设则用 ``SessionLocal`` / ``run_manual_aggregation`` / 真线程。
_session_factory: Optional[Callable] = None
_aggregate_fn: Optional[Callable] = None


def reset_for_tests() -> None:
    """清空测试注入（单测隔离用，生产不调）。任务行在 DB 里，
    随测试引擎一起消亡，不在这里删。活线程登记一并清掉（测试
    内起的执行线程随用例结束，不许把登记带到下一个用例）。"""
    global _session_factory, _aggregate_fn
    _session_factory = None
    _aggregate_fn = None
    with _live_lock:
        _LIVE.clear()


def _register_live(task_id: str) -> None:
    with _live_lock:
        _LIVE[task_id] = threading.current_thread()


def _unregister_live(task_id: str) -> None:
    with _live_lock:
        _LIVE.pop(task_id, None)


def _live_task_ids() -> set:
    with _live_lock:
        return set(_LIVE)


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


def claim_poll_sec(db) -> int:
    """worker 认领轮询间隔（秒）：SystemConfig > 默认 5，钳 [1, 60]。"""
    configured = uar._system_config_int(db, KEY_CLAIM_POLL_SEC)
    if configured is not None:
        return max(1, min(60, configured))
    return CLAIM_POLL_DEFAULT


# ---------------------------------------------------------------------------
# 轮次种类决策（定时增量拉取）：上次成功 / 上次全量成功 / kind
# ---------------------------------------------------------------------------

def last_succeeded_finished_at(db, source_id: int) -> Optional[datetime]:
    """某源最近一轮成功（任意 kind）的结束时刻；无成功记录给 None。

    调度启动新鲜度门用它判「刚成功过不久」（只查本地任务表，
    不发任何上游请求）。
    """
    return (db.query(func.max(em.UpstreamAggregateTask.finished_at))
            .filter(em.UpstreamAggregateTask.source_id == source_id,
                    em.UpstreamAggregateTask.status == "succeeded")
            .scalar())


def last_full_succeeded_finished_at(db,
                                    source_id: int) -> Optional[datetime]:
    """某源最近一轮**全量**成功的结束时刻；无则 None。

    kind 记在任务 result_json 里（增量轮为 ``incremental``）；
    本功能上线前的历史行没有 kind 字段——它们本来就是全量，
    按全量计。从新到旧扫最近若干条成功行即可（全量兜底间隔
    以天计，成功行保留期 30 天，扫 200 条绰绰有余）。
    """
    rows = (db.query(em.UpstreamAggregateTask)
            .filter(em.UpstreamAggregateTask.source_id == source_id,
                    em.UpstreamAggregateTask.status == "succeeded")
            .order_by(em.UpstreamAggregateTask.finished_at.desc())
            .limit(200)
            .all())
    for row in rows:
        kind = None
        if row.result_json:
            try:
                parsed = json.loads(row.result_json)
            except ValueError:
                parsed = None
            if isinstance(parsed, dict):
                kind = parsed.get("kind")
        if kind != uar.RUN_KIND_INCREMENTAL:
            return row.finished_at
    return None


def decide_scheduled_run_kind(db, source_id: int) -> str:
    """定时轮 kind 决策（手动轮不走这里、恒全量）：

    - 增量开关关（env 回退）→ full；
    - 该源尚无任何水位线（存量首次）→ full（跑完建线）；
    - 从未有过全量成功、或距上次全量成功已满全量兜底间隔
      → full；
    - 其余 → incremental。
    """
    if not uar.incremental_enabled():
        return uar.RUN_KIND_FULL
    if not uar.has_watermarks(db, source_id):
        return uar.RUN_KIND_FULL
    last_full = last_full_succeeded_finished_at(db, source_id)
    if last_full is None:
        return uar.RUN_KIND_FULL
    if datetime.now() - last_full >= timedelta(
            seconds=uar.full_interval_sec()):
        return uar.RUN_KIND_FULL
    return uar.RUN_KIND_INCREMENTAL


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

    执行进程消亡后任务行会永远停在 running；心跳（认领时、逐库
    进度时、逐窗心跳时更新）超阈值即判已消亡。标完可 resume 续跑。
    本进程仍有活线程的任务跳过（A-118）：心跳冻住不等于线程已死
    （长库窗内无进度事件时心跳本就停在认领时刻），同进程活任务
    被自己人判死是双跑的源头；跨进程死亡仍由心跳兜底。
    """
    now = now or datetime.now()
    threshold = now - timedelta(seconds=heartbeat_stale_sec(db))
    live = _live_task_ids()
    rows = (db.query(em.UpstreamAggregateTask)
            .filter(em.UpstreamAggregateTask.status == "running",
                    func.coalesce(em.UpstreamAggregateTask.heartbeat_at,
                                  em.UpstreamAggregateTask.created_at)
                    < threshold)
            .all())
    marked = 0
    for row in rows:
        if row.task_id in live:
            logger.info("上游聚合任务心跳过期但本进程活线程在跑，跳过 "
                        "task_id=%s source_id=%s",
                        row.task_id, row.source_id)
            continue
        row.status = "interrupted"
        row.finished_at = now
        marked += 1
        logger.warning("上游聚合任务心跳过期标中断 task_id=%s source_id=%s",
                       row.task_id, row.source_id)
    if marked:
        db.flush()
    return marked


def mark_stale_queued_failed(db, *, now: Optional[datetime] = None) -> int:
    """把超龄无人认领的 queued 任务标 failed（只 flush，调用方提交）。

    A-117：线程启动失败（或入队提交后、认领前进程被部署重启冲掉）
    时行永停 queued；认领是入队时立刻发生的，超
    ``QUEUED_STALE_DEFAULT`` 秒仍 queued 即判启动失败。标 failed
    后可 resume 续跑（failed 在可续跑集里），且去重不再复用死行。
    """
    now = now or datetime.now()
    threshold = now - timedelta(seconds=QUEUED_STALE_DEFAULT)
    rows = (db.query(em.UpstreamAggregateTask)
            .filter(em.UpstreamAggregateTask.status == "queued",
                    func.coalesce(em.UpstreamAggregateTask.heartbeat_at,
                                  em.UpstreamAggregateTask.created_at)
                    < threshold)
            .all())
    for row in rows:
        row.status = "failed"
        row.finished_at = now
        row.error_json = json.dumps(
            {"status": 500, "code": "queued_stale",
             "detail": "任务入队后长时间无人认领，已标记失败，请重新触发"},
            ensure_ascii=False)
        logger.warning("上游聚合任务 queued 超龄标失败 task_id=%s source_id=%s",
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

def _enqueue_locked(db, *, source_id: int, page_size: int, trigger: str,
                    resume_of: Optional[str] = None,
                    skip_library_ids=None):
    """入队核心（调用方须持 _lock、带开着的会话）：先做入队维护
    （心跳过期标 interrupted、超龄 queued 标 failed、超保留期终态
    行清掉），再查重——同源已有 queued/running 时不另起一轮，返
    回那条的视图与 created=False；否则新建 queued 行，返回视图
    与 created=True。是否执行不由本函数决定：worker 认领循环认领
    queued 行（O-03）。
    """
    mark_stale_interrupted(db)
    mark_stale_queued_failed(db)
    cleanup_terminal_tasks(db)
    existing = _active_row_for(db, source_id)
    if existing is not None:
        db.commit()
        view = _view_from_row(existing)
        logger.info(
            "上游聚合任务复用进行中的任务 task_id=%s source_id=%s "
            "status=%s", view["task_id"], source_id, view["status"])
        return view, False
    row = _create_row(db, source_id=source_id, page_size=page_size,
                      trigger=trigger, resume_of=resume_of,
                      skip_library_ids=skip_library_ids)
    db.commit()
    return _view_from_row(row), True

def enqueue(source_id: int, page_size: int, *, trigger: str = "manual") -> dict:
    """登记任务行，立刻返回当前视图（新任务恒为 queued）。

    O-03：本函数只入队，不在调用进程（api）启动执行——执行由
    worker 认领循环认领 queued 行。同一源已有 queued/running 时
    不另起一轮，返回那条任务的当前视图（可能已是 running）。入队
    前顺手做三件维护：心跳过期的 running 标 interrupted、超龄
    queued 标 failed、超保留期的终态行清掉（维护在
    ``_enqueue_locked`` 内）。
    """
    with _lock:
        db = _open_session()
        try:
            view, _ = _enqueue_locked(db, source_id=source_id,
                                      page_size=page_size, trigger=trigger)
        finally:
            _close(db)
    logger.info("上游聚合任务入队 task_id=%s source_id=%s trigger=%s "
                "（worker 认领执行）",
                view["task_id"], source_id, trigger)
    return view


def enqueue_scheduled(source_id: int) -> dict:
    """调度口入队（A-120）：与手动口同一张表、同一去重语义。

    返回 ``{"task_id", "status", "trigger", "created", "view"}``：
    created=True 是本轮新入队（调度轮的任务行，worker 认领执行）；
    created=False 是复用了同源进行中的任务——调度轮不启动第二路
    执行，这正是期望的去重行为，调用方（调度器）据此记 skipped。
    页大小与手动口同口径（SystemConfig 缺省页大小）。
    """
    db = _open_session()
    try:
        page_size = uar.configured_default_page_size(db)
    finally:
        _close(db)
    with _lock:
        db = _open_session()
        try:
            view, created = _enqueue_locked(
                db, source_id=source_id, page_size=page_size,
                trigger="scheduled")
        finally:
            _close(db)
    logger.info("上游聚合调度任务入队 task_id=%s source_id=%s created=%s",
                view["task_id"], source_id, created)
    return {"task_id": view["task_id"], "status": view["status"],
            "trigger": view["trigger"], "created": created, "view": view}


def resume_task(source_id: int, task_id: str) -> Optional[dict]:
    """续跑一条 interrupted / failed 任务：新建任务行跳过已完成库。

    任务不存在 / 不属于该源 / 状态不在可续跑集 → None（同源已有
    进行中任务时返回那条的视图，与 enqueue 同口径）。O-03：续跑
    同样只入队不执行，worker 认领循环认领新行。
    """
    with _lock:
        db = _open_session()
        try:
            mark_stale_interrupted(db)
            mark_stale_queued_failed(db)
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
            else:
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
                logger.info("上游聚合任务续跑 task_id=%s resume_of=%s "
                            "source_id=%s skip_libraries=%s",
                            new_task_id, task_id, source_id, len(done_ids))
        finally:
            _close(db)
    logger.info("上游聚合任务续跑 task_id=%s resume_of=%s source_id=%s "
                "skip_libraries=%s（worker 认领执行）",
                new_task_id, task_id, source_id, len(done_ids))
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


def _mark_failed(task_id: str, *, code: str, detail: str) -> None:
    """把行记 failed（独立会话 + 条件更新）：只许从 queued 落
    failed，已被认领的不覆盖（启动失败时本就没有执行线程，但
    重提认领可能正好撞上另一个认领成功的线程，条件更新兜底）。"""
    db = _open_session()
    try:
        updated = (db.query(em.UpstreamAggregateTask)
                   .filter(em.UpstreamAggregateTask.task_id == task_id,
                           em.UpstreamAggregateTask.status == "queued")
                   .update({"status": "failed",
                            "finished_at": datetime.now(),
                            "heartbeat_at": datetime.now(),
                            "error_json": json.dumps(
                                {"status": 500, "code": code,
                                 "detail": detail}, ensure_ascii=False)},
                           synchronize_session=False))
        db.commit()
        if not updated:
            logger.warning("上游聚合任务启动失败但行已不在 queued "
                           "task_id=%s", task_id)
    except Exception:
        _rollback(db)
        logger.exception("上游聚合任务启动失败记 failed 失败 task_id=%s",
                         task_id)
    finally:
        _close(db)


def _submit(task_id: str) -> bool:
    """启动执行线程。start() 失败（线程耗尽等）时不抛异常（A-117，
    照 A-050 扫描队列的口径）：行已提交为 queued，异常穿出只会让
    管理端回 500、行却永卡 queued；改为记 failed 并返回 False，
    认领方（``claim_queued_tasks``）据此不计入已提交。成功返回
    True。"""
    try:
        threading.Thread(
            target=execute_task, args=(task_id,), daemon=True,
            name=f"upstream-agg-{task_id[:8]}",
        ).start()
    except Exception:
        logger.exception("上游聚合任务启动执行线程失败 task_id=%s", task_id)
        _mark_failed(task_id, code="task_start_failed",
                     detail="执行线程启动失败，请重新触发")
        return False
    return True


# ---------------------------------------------------------------------------
# worker 认领循环（O-03）
# ---------------------------------------------------------------------------

def claim_queued_tasks(*, limit: int = CLAIM_BATCH_LIMIT) -> list:
    """认领一轮 queued 任务（worker 进程调用）：按 created_at/id FIFO
    取行，对每条调 ``_submit`` 起执行线程。同源只提交最早的一条：
    后入队的同源行留待下一轮（FIFO＋同源串行的语义在认领侧钉死，
    不靠线程调度谁先跑到认领门——两条同源行同轮齐发时，后行线程
    可能抢先认领，先行反而滞留 queued）。认领本身是
    ``execute_task`` 内的条件更新（queued → running），重复提交
    无害：已被认领的线程直接返回。返回本轮已提交的 task_id 列表。

    兼容 #1620 前后的 ``_submit`` 语义：旧版返回 None、启动失败
    抛异常（此处吞并记日志，行留 queued 等下轮）；#1620 版返回
    bool、启动失败时行已记 failed（返回 False 的不计入已提交）。
    """
    db = _open_session()
    try:
        rows = (db.query(em.UpstreamAggregateTask)
                .filter(em.UpstreamAggregateTask.status == "queued")
                .order_by(em.UpstreamAggregateTask.created_at.asc(),
                          em.UpstreamAggregateTask.id.asc())
                .limit(limit)
                .all())
        task_ids = []
        seen_sources = set()
        for row in rows:
            if row.source_id in seen_sources:
                continue
            seen_sources.add(row.source_id)
            task_ids.append(row.task_id)
    finally:
        _close(db)
    submitted = []
    for task_id in task_ids:
        try:
            ok = _submit(task_id)
        except Exception:
            logger.exception("上游聚合认领提交执行线程失败 task_id=%s",
                             task_id)
            continue
        if ok is not False:
            submitted.append(task_id)
    if submitted:
        logger.info("上游聚合认领循环提交任务 task_ids=%s", submitted)
    return submitted


def run_claim_loop(stop_event=None, *, claim_fn=None,
                   interval_sec: Optional[float] = None) -> None:
    """worker 认领循环：每轮认领一次，再按间隔等，停表置位即退出。

    ``claim_fn`` 未传时走 :func:`claim_queued_tasks`（生产路径）；
    间隔优先用显式值，否则读 SystemConfig
    ``upstream_aggregate_claim_poll_sec``（默认 5 秒，读失败回落 5）。
    单轮异常已在认领内隔离；注入的 ``claim_fn`` 抛异常时本循环
    记日志后继续，不让认领线程悄悄死掉。
    """
    import time

    while True:
        if stop_event is not None and stop_event.is_set():
            return
        try:
            if claim_fn is not None:
                claim_fn()
            else:
                claim_queued_tasks()
        except Exception:
            logger.exception("上游聚合认领循环单轮失败（下一轮继续）")
        if interval_sec is not None:
            interval = max(1.0, min(60.0, float(interval_sec)))
        else:
            db = _open_session()
            try:
                interval = float(claim_poll_sec(db))
            except Exception:
                interval = float(CLAIM_POLL_DEFAULT)
            finally:
                _close(db)
        if stop_event is not None:
            if stop_event.wait(interval):
                return
        else:
            time.sleep(interval)


def start_aggregate_worker(stop_event=None, *, claim_fn=None,
                           interval_sec: Optional[float] = None) -> bool:
    """启动 worker 认领循环线程（worker.py 调用，照调度器工人写法）。

    无开关门：worker 进程跑起来认领循环就必须在，否则入队的任务
    无人执行。幂等：已有存活线程时直接返回 True，不双跑。
    """
    global _claim_thread
    with _claim_lock:
        alive = _claim_thread
        if alive is not None and alive.is_alive():
            return True
        thread = threading.Thread(
            target=run_claim_loop,
            args=(stop_event,),
            kwargs={"claim_fn": claim_fn, "interval_sec": interval_sec},
            daemon=True, name="upstream-aggregate-worker",
        )
        thread.start()
        _claim_thread = thread
        return True


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


def _make_heartbeat_handler(task_id: str) -> Callable:
    """窗级心跳回调（A-118）：只刷新任务行 heartbeat，不碰执行事务。

    与进度回调分离：进度是逐库观测面（带明细与计数），心跳只是
    存活信号，每窗一次。条件更新（仅 running 态）：已被他进程标
    interrupted 的任务不许被残留线程的心跳复活。心跳写失败只记
    日志，绝不打断聚合。
    """

    def handle(event: dict) -> None:
        db = _open_session()
        try:
            (db.query(em.UpstreamAggregateTask)
             .filter(em.UpstreamAggregateTask.task_id == task_id,
                     em.UpstreamAggregateTask.status == "running")
             .update({"heartbeat_at": datetime.now()},
                     synchronize_session=False))
            db.commit()
        except Exception:
            _rollback(db)
            logger.exception("上游聚合任务心跳写入失败 task_id=%s", task_id)
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
    重复进入 / 已被认领直接返回；同源已有 running 任务时本次认领
    同样返回、行留在 queued 等认领循环下一轮（跨进程口径，O-03
    的同源串行就靠这条认领条件，手动与调度不再双写映射）。

    会话是执行方自己开的，不用请求上的 Session（请求返回后那个
    会话就关了）。聚合走逐库事务：``run_manual_aggregation`` 只在
    逐库边界 commit，任务级失败（凭据/未启用等发生在首库之前，
    或整轮异常）在这里落 failed：已知业务错误照原状态码与文案
    记到任务上；其余异常记日志后落成 500，不把异常原文回给前端。
    """
    db = _open_session()
    try:
        head = _get_row(db, task_id)
        if head is None or head.status != "queued":
            return
        source_id = head.source_id
        now = datetime.now()
        other_running = (db.query(em.UpstreamAggregateTask.id)
                         .filter(em.UpstreamAggregateTask.source_id
                                 == source_id,
                                 em.UpstreamAggregateTask.task_id != task_id,
                                 em.UpstreamAggregateTask.status == "running"))
        claimed = (db.query(em.UpstreamAggregateTask)
                   .filter(em.UpstreamAggregateTask.task_id == task_id,
                           em.UpstreamAggregateTask.status == "queued",
                           ~other_running.exists())
                   .update({"status": "running", "started_at": now,
                            "heartbeat_at": now},
                           synchronize_session=False))
        db.commit()
        if not claimed:
            return
        row = _get_row(db, task_id)
        page_size = row.page_size
        trigger = row.trigger
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
    _register_live(task_id)
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
        # 轮次种类：只有定时口（trigger=scheduled）才可能增量，
        # 且要过 kind 决策（存量首次/全量兜底/回退开关都在决策
        # 里）；手动与续跑（trigger=manual）恒全量、语义不变。
        run_kind = uar.RUN_KIND_FULL
        if trigger == "scheduled":
            run_kind = decide_scheduled_run_kind(db, source_id)
        result = fn(db, source, page_size=page_size,
                    on_event=_make_progress_handler(task_id),
                    on_heartbeat=_make_heartbeat_handler(task_id),
                    commit_each_library=True,
                    skip_library_ids=skip_ids,
                    prior_summaries=prior,
                    run_kind=run_kind)
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
            "上游聚合任务完成 task_id=%s source_id=%s kind=%s fetched=%s "
            "paired=%s written=%s failed_libraries=%s",
            task_id, source_id, result.get("kind"), result.get("fetched"),
            result.get("paired"), result.get("written"),
            len(result.get("failed_libraries") or []))
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
        _unregister_live(task_id)
        _close(db)
