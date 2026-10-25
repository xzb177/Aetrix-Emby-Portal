"""管理后台 · 低置信识别人工审查队列（FMBY 学习批二 #15）

TMDB 真搜过、有候选但无一过置信线的条目（旧口径终态 none、候选丢弃），
开启队列（env IDENTIFY_REVIEW_QUEUE_ENABLED=1）后在 enrich 收尾入队
（emby_identify_review_tasks），管理员在这里逐条处置：
- 绑定到指定 TMDB：复用 admin_scrape.bind_tmdb_id 这唯一绑定实现
  （校验/补详情/锁定语义全同），不另写第二套；
- 忽略：条目保持现状（终态 none，日后可用「重试未匹配项」整体捞回）；
- 重跑识别：条目重新排入补全队列（口径同 retry_unmatched 单条版）。

路由挂在 admin_emby_router 上（/api/admin/emby/identify-review/*），
成功写操作的审计由 emby_server/audit.py 中间件统一记录。
队列状态流转只经 emby_server/identify_review（魂 6：状态集中维护）。
"""
from __future__ import annotations

import json
import logging
from typing import Optional

from fastapi import Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from backend import models as base_models
from backend.api.admin_scrape import TmdbBindRequest, bind_tmdb_id
from backend.database import get_db
from backend.emby_server import identify_review as irev
from backend.emby_server import models as em
from backend.emby_server.portal import admin_emby_router, require_staff

logger = logging.getLogger(__name__)


def _task_payload(db: Session, task: em.IdentifyReviewTask,
                  with_evidence: bool = False) -> dict:
    """审查行 + 条目当前状态（绑定处置要看条目现在有没有 tmdb_id）。"""
    item = (db.query(em.MediaItem)
            .filter(em.MediaItem.id == task.item_id).first())
    payload = {
        "id": task.id,
        "item_id": task.item_id,
        "item_name": task.item_name,
        "item_type": task.item_type,
        "item_year": task.item_year,
        "reason": task.reason,
        "status": task.status,
        "created_at": task.created_at.isoformat() if task.created_at else None,
        "updated_at": task.updated_at.isoformat() if task.updated_at else None,
        "resolved_by": task.resolved_by,
        "resolved_at": task.resolved_at.isoformat() if task.resolved_at else None,
        "resolved_tmdb_id": task.resolved_tmdb_id,
        "item_exists": item is not None,
        "item_tmdb_id": getattr(item, "tmdb_id", None) if item else None,
        "item_enrich_status": getattr(item, "enrich_status", None) if item else None,
        "item_metadata_source": (getattr(item, "metadata_source", None)
                                 if item else None),
    }
    if with_evidence:
        try:
            payload["evidence"] = json.loads(task.evidence_json or "{}")
        except (TypeError, ValueError):
            payload["evidence"] = {}
    return payload


def _get_task_or_404(db: Session, task_id: int) -> em.IdentifyReviewTask:
    task = irev.get_review(db, task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="审查任务不存在")
    return task


def _require_pending(task: em.IdentifyReviewTask) -> None:
    if task.status != "pending":
        raise HTTPException(
            status_code=409,
            detail=f"该审查任务已处置（{task.status}），不能重复操作")


@admin_emby_router.get("/identify-review/tasks")
def list_identify_review_tasks(
    status: Optional[str] = Query(default=None),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    staff: base_models.WebUser = Depends(require_staff),
    db: Session = Depends(get_db),
):
    """审查队列分页列表（默认全部状态、最新在前；status 可筛 pending 等）。"""
    try:
        rows, total = irev.list_reviews(db, status=status, page=page,
                                        page_size=page_size)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {
        "items": [_task_payload(db, t) for t in rows],
        "total": total,
        "page": page,
        "page_size": page_size,
    }


@admin_emby_router.get("/identify-review/tasks/{task_id}")
def get_identify_review_task(
    task_id: int,
    staff: base_models.WebUser = Depends(require_staff),
    db: Session = Depends(get_db),
):
    """审查任务详情（含入队时的证据快照：候选与分数）。"""
    task = _get_task_or_404(db, task_id)
    return _task_payload(db, task, with_evidence=True)


class ReviewBindRequest(BaseModel):
    tmdb_id: str = Field(description="管理员选定要绑定的 TMDB ID（或 IMDb tt 号）")


@admin_emby_router.post("/identify-review/tasks/{task_id}/bind")
def bind_identify_review_task(
    task_id: int,
    req: ReviewBindRequest,
    staff: base_models.WebUser = Depends(require_staff),
    db: Session = Depends(get_db),
):
    """处置：绑定到指定 TMDB。绑定本体走 bind_tmdb_id 唯一实现（先校验
    ID 存在、再补详情、并锁定为人工成果）；绑定成功后审查行落 bound。
    绑定失败（ID 不存在等）原样返回其错误，审查行保持 pending。
    """
    task = _get_task_or_404(db, task_id)
    _require_pending(task)
    tid = (req.tmdb_id or "").strip()
    if not tid:
        raise HTTPException(status_code=400, detail="tmdb_id 不能为空")
    try:
        result = bind_tmdb_id(
            item_id=task.item_id,
            req=TmdbBindRequest(tmdb_id=tid, verify=True),
            staff=staff, db=db)
    except HTTPException:
        db.rollback()
        raise
    # 绑定已由 bind_tmdb_id 提交；审查行落账是第二步，失败只响亮记日志
    # 不回滚绑定（条目已正确绑定是主事实，审查行留 pending 可再忽略）。
    try:
        task = _get_task_or_404(db, task_id)
        item = (db.query(em.MediaItem)
                .filter(em.MediaItem.id == task.item_id).first())
        irev.mark_resolved(db, task, "bound", staff.id,
                           tmdb_id=getattr(item, "tmdb_id", None) or tid)
        db.commit()
    except Exception as exc:  # noqa: BLE001 — 见上方注释，不回滚已成功的绑定
        db.rollback()
        logger.error("审查任务 %s 绑定成功但落账失败（行保持 pending，"
                     "可手动忽略）: %s", task_id, exc, exc_info=True)
    return {"success": True, "task": _task_payload(db, task, with_evidence=True),
            "bind": result}


@admin_emby_router.post("/identify-review/tasks/{task_id}/ignore")
def ignore_identify_review_task(
    task_id: int,
    staff: base_models.WebUser = Depends(require_staff),
    db: Session = Depends(get_db),
):
    """处置：忽略。条目保持终态 none 现状，审查行落 ignored 留痕。"""
    task = _get_task_or_404(db, task_id)
    _require_pending(task)
    try:
        irev.mark_resolved(db, task, "ignored", staff.id)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    db.commit()
    return {"success": True, "task": _task_payload(db, task)}


@admin_emby_router.post("/identify-review/tasks/{task_id}/rerun")
def rerun_identify_review_task(
    task_id: int,
    staff: base_models.WebUser = Depends(require_staff),
    db: Session = Depends(get_db),
):
    """处置：重跑识别。条目重新排入补全队列（含失效阴性搜索缓存），
    审查行落 requeued；重跑若再次低置信收尾，会刷新同一条 pending 行
    的证据（入队幂等），不会堆新行。
    """
    task = _get_task_or_404(db, task_id)
    _require_pending(task)
    item = (db.query(em.MediaItem)
            .filter(em.MediaItem.id == task.item_id).first())
    if item is None:
        raise HTTPException(status_code=404, detail="条目已不存在，无法重跑")
    irev.requeue_item(db, item)
    try:
        irev.mark_resolved(db, task, "requeued", staff.id)
    except ValueError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    db.commit()
    return {"success": True, "task": _task_payload(db, task)}
