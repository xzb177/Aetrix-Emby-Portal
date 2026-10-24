"""低置信度识别人工审查队列（FMBY 学习批二 #15）。

学的是 FMBY 的识别兜底：识别拿不准（低置信度）不硬认、不丢弃，进人工
审查队列等管理员处置。对应魂 1（身份与证据先行）：入队时把当时的候选与
分数做成证据快照随行保存，管理员处置时看的就是识别当刻的证据；对应
魂 6（状态集中维护）：队列状态只经本模块流转（pending → bound /
ignored / requeued），任何出口不许私自改审查行。

现状（本模块接入前）：TMDB ``search()`` 只返回过置信线的命中；搜过但
无一过线时 enrich 在 ``_enrich_apply`` 里把条目标终态
``metadata_source='none'``，被拒的候选当场丢弃——管理员事后想人工认片
手里什么都不剩。本模块开启后，终态化的同一事务里把这类条目入队。

三纪律（FMBY 在这条链上栽过真 bug：入队写的 reason_code 不在 DB CHECK
约束允许集里，整条「识别→审核」链静默断裂）：
① 枚举与约束同源：reason/status 允许值只在 ``models`` 的
   ``IDENTIFY_REVIEW_REASONS`` / ``IDENTIFY_REVIEW_STATUSES`` 定义一处，
   DB 的 CheckConstraint SQL 由同一元组拼出，本模块的校验也只读它。
② 写队列失败响亮报错：``enqueue_review`` 失败抛 ``ReviewEnqueueError``
   （先打 ERROR 日志带堆栈），绝不静默吞、绝不伪装成识别完成——调用方
   （enrich 写库阶段）会因此整条回滚走失败/重试路，而不是终态 done。
③ 整链端到端：tests/test_identify_review_queue.py 造低置信条目走通
   「识别收尾入队 → 管理列表可见 → 处置（绑定/忽略/重跑）→ 条目状态
   正确变化」全链。

总开关：env ``IDENTIFY_REVIEW_QUEUE_ENABLED``（默认 0=关，关 = enrich
行为与现行逐字一致），模块加载时读一次（与 IDENTITY_SHELF_GATE 同口径）。
"""
from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Optional

from sqlalchemy.exc import SQLAlchemyError

from backend.emby_server import models as em
from backend.emby_server.env_util import env_int

logger = logging.getLogger(__name__)

#: 审查队列总开关（默认关）。启动时读一次，热路径不碰 env。
IDENTIFY_REVIEW_QUEUE_ENABLED = env_int("IDENTIFY_REVIEW_QUEUE_ENABLED", 0, 0, 1)

#: 重跑识别的队列优先级（与 enrich_worker.retry_unmatched 的捞回口径同值：
#: 高于默认 0、低于 repair 100，让人工点名重跑的条目排在队首附近）。
REQUEUE_PRIORITY = 50


def queue_enabled() -> bool:
    """审查队列是否开启（全仓读开关的唯一入口；测试 patch 本模块常量）。"""
    return IDENTIFY_REVIEW_QUEUE_ENABLED > 0


class ReviewEnqueueError(RuntimeError):
    """入队失败（纪律②：响亮报错，绝不静默吞、绝不伪装成识别完成）。"""


def enqueue_review(db, item, reason: str, evidence: Optional[dict]):
    """把条目入队（或刷新其既有 pending 行的证据）。调用方事务内执行，
    本函数只 flush 不 commit。

    幂等：同一条目已有 pending 行时更新证据快照与条目快照，不新增行
    （DB 层另有部分唯一索引 uq_identify_review_one_pending 兜底）。

    失败语义：reason 不在允许集 → ValueError（编程错误，响亮）；
    DB 写失败 → 打 ERROR 日志（带堆栈）后抛 ReviewEnqueueError。
    """
    if reason not in em.IDENTIFY_REVIEW_REASONS:
        raise ValueError(
            f"审查入队 reason={reason!r} 不在允许集 "
            f"{em.IDENTIFY_REVIEW_REASONS}（唯一定义点在 models，"
            "新增取值先改那里，约束与校验同源）")
    try:
        task = (db.query(em.IdentifyReviewTask)
                .filter(em.IdentifyReviewTask.item_id == item.id,
                        em.IdentifyReviewTask.status == "pending")
                .first())
        evidence_text = json.dumps(evidence or {}, ensure_ascii=False)
        if task is None:
            task = em.IdentifyReviewTask(
                item_id=item.id,
                item_name=getattr(item, "name", None),
                item_type=getattr(item, "item_type", None),
                item_year=getattr(item, "production_year", None),
                reason=reason,
                status="pending",
                evidence_json=evidence_text,
            )
            db.add(task)
        else:
            task.item_name = getattr(item, "name", None)
            task.item_type = getattr(item, "item_type", None)
            task.item_year = getattr(item, "production_year", None)
            task.reason = reason
            task.evidence_json = evidence_text
            task.updated_at = datetime.now()
        db.flush()
    except SQLAlchemyError as exc:
        logger.error("识别审查入队失败（不静默、不伪装完成）item_id=%s "
                     "reason=%s: %s", getattr(item, "id", "?"), reason, exc,
                     exc_info=True)
        raise ReviewEnqueueError(
            f"识别审查入队失败 item_id={getattr(item, 'id', '?')}: {exc}"
        ) from exc
    logger.info("识别审查入队 id=%s item_id=%s name=%r reason=%s",
                task.id, item.id, getattr(item, "name", ""), reason)
    return task


def maybe_enqueue_terminal_none(db, item, tmdb_client) -> bool:
    """识别出口接入点：条目在 enrich 收尾被判「TMDB 搜过无高置信命中」
    （终态 none）时调用。开关关 / 非剧非电影 / TMDB 一个候选都没返回
    （真没有、不是拿不准）时不入队，返回 False。

    证据快照来自 ``tmdb_client.scored_candidates``（原始候选 + 各自的
    置信分数，被拒的 tier=None）——管理员处置时看到的是识别当刻的候选。
    证据抓取本身失败（瞬态）只记 warning 并跳过入队：条目照旧终态 none，
    不把「抓证据失败」伪装成「没有候选」之外的任何结论。
    入队写失败不在这里吞——enqueue_review 的 ReviewEnqueueError 直接
    上抛，enrich 写库阶段整体回滚走失败/重试（纪律②）。
    """
    if not queue_enabled():
        return False
    kind = {"series": "series", "movie": "movie"}.get(
        getattr(item, "item_type", "") or "")
    if not kind:
        return False
    try:
        candidates = tmdb_client.scored_candidates(
            item.name, getattr(item, "production_year", None), kind)
    except Exception as exc:  # noqa: BLE001 — 证据抓取失败不许拖垮识别收尾
        logger.warning("识别审查证据抓取失败，跳过入队 item_id=%s: %s",
                       getattr(item, "id", "?"), exc)
        return False
    if not candidates:
        return False
    evidence = {
        "query": item.name,
        "kind": kind,
        "searched_year": getattr(item, "production_year", None),
        "captured_at": datetime.now().isoformat(timespec="seconds"),
        "candidates": candidates,
    }
    enqueue_review(db, item, "no_confident_match", evidence)
    return True


def list_reviews(db, status: Optional[str] = None, page: int = 1,
                 page_size: int = 20):
    """审查队列分页列表（管理端用）。status 非法值直接 ValueError。"""
    if status is not None and status not in em.IDENTIFY_REVIEW_STATUSES:
        raise ValueError(f"status={status!r} 不在允许集 "
                         f"{em.IDENTIFY_REVIEW_STATUSES}")
    q = db.query(em.IdentifyReviewTask)
    if status:
        q = q.filter(em.IdentifyReviewTask.status == status)
    total = q.count()
    rows = (q.order_by(em.IdentifyReviewTask.created_at.desc(),
                       em.IdentifyReviewTask.id.desc())
            .offset(max(0, page - 1) * page_size)
            .limit(page_size)
            .all())
    return rows, total


def get_review(db, task_id: int):
    """按 id 取一行审查任务（不存在返回 None）。"""
    return (db.query(em.IdentifyReviewTask)
            .filter(em.IdentifyReviewTask.id == task_id)
            .first())


#: 允许的处置结果（pending 之外的终态）；流转只许 pending → 其一。
RESOLUTIONS = ("bound", "ignored", "requeued")


def mark_resolved(db, task, status: str, user_id,
                  tmdb_id: Optional[str] = None):
    """处置落账：只许从 pending 流转到 bound / ignored / requeued 之一。

    非 pending 重复处置、非法目标状态都抛 ValueError（调用方转 409/400），
    不许静默覆盖既有处置结果——谁处置的、何时、以什么结果，全留痕。
    只 flush 不 commit（调用方事务）。
    """
    if status not in RESOLUTIONS:
        raise ValueError(f"处置状态 {status!r} 不在允许集 {RESOLUTIONS}")
    if task.status != "pending":
        raise ValueError(
            f"审查任务 {task.id} 已是 {task.status}，不能再次处置")
    task.status = status
    task.resolved_by = user_id
    task.resolved_at = datetime.now()
    task.updated_at = datetime.now()
    if tmdb_id is not None:
        task.resolved_tmdb_id = str(tmdb_id)
    db.flush()
    return task


def requeue_item(db, item) -> None:
    """把条目重新排入补全队列（处置「重跑识别」）。

    口径与 enrich_worker.retry_unmatched 的单条版一致：pending + 清
    attempts/退避 + priority=REQUEUE_PRIORITY；同时失效该条目的 TMDB
    阴性搜索缓存（不清的话重跑直接命中旧缓存、一个请求都不发，重跑成
    摆设——retry_unmatched 第 7 批的教训）。缓存失效失败不挡重跑本身
    （与 retry_unmatched 同口径，只记 debug）。
    """
    item.enrich_status = "pending"
    item.enrich_attempts = 0
    item.enrich_next_retry_at = None
    item.enrich_priority = REQUEUE_PRIORITY
    db.flush()
    try:
        from backend.emby_server import tmdb_cache
        from backend.emby_server.tmdb import preferred_language
        tmdb_cache.invalidate_search(
            item.name, getattr(item, "production_year", None),
            item.item_type, preferred_language())
    except Exception as exc:  # noqa: BLE001 — 缓存清理失败不影响重跑入队
        logger.debug("重跑识别：清理 TMDB 搜索缓存失败: %s", exc)
