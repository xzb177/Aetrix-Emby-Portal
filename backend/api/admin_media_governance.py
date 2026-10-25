"""管理后台 · 可见性治理复核队列（L7）与库级扫描预设配置（L10）。

复用现有管理鉴权（require_staff）与 admin_emby_router，不另立开关
（开关只走 env/文件配置，2026-10-19 原则）。

复核动作的语义（硬约束，见 visibility_governance 模块头）：准入/
驳回只追加决策行与事件、留处置痕迹，**不改变任何露出结果**——露出
永远由案 A 判定函数现算。成功写操作的审计由 emby_server/audit.py
中间件统一记录（与 identify-review 同口径）。
"""
from __future__ import annotations

import json
import logging
from typing import Optional

from fastapi import Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from backend import models as base_models
from backend.database import get_db
from backend.emby_server import library_scan_presets as lsp
from backend.emby_server import models as em
from backend.emby_server import visibility_governance as vg
from backend.emby_server.portal import admin_emby_router, require_staff

logger = logging.getLogger(__name__)


def _task_payload(task: em.VisibilityGovernanceTask) -> dict:
    return {
        "id": task.id,
        "item_id": task.item_id,
        "item_name": task.item_name,
        "surface": task.surface,
        "status": task.status,
        "reason": task.reason,
        "decided_by": task.decided_by,
        "decided_at": task.decided_at.isoformat() if task.decided_at else None,
        "decision_note": task.decision_note,
        "created_at": task.created_at.isoformat() if task.created_at else None,
    }


def _get_task_or_404(db: Session, task_id: int) -> em.VisibilityGovernanceTask:
    task = vg.get_task(db, task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="治理任务不存在")
    return task


@admin_emby_router.get("/visibility-governance/tasks")
def list_visibility_tasks(
    status: Optional[str] = Query(default=None),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    staff: base_models.WebUser = Depends(require_staff),
    db: Session = Depends(get_db),
):
    """复核队列分页列表（默认全部状态、最新在前；status 可筛 pending 等）。"""
    try:
        rows, total = vg.list_tasks(db, status=status, page=page,
                                    page_size=page_size)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"items": [_task_payload(t) for t in rows], "total": total,
            "page": page, "page_size": page_size}


class ReviewRequest(BaseModel):
    note: str = Field(default="", description="处置备注（留痕）")


@admin_emby_router.post("/visibility-governance/tasks/{task_id}/approve")
def approve_visibility_task(
    task_id: int, body: ReviewRequest,
    staff: base_models.WebUser = Depends(require_staff),
    db: Session = Depends(get_db),
):
    """准入：任务 → approved，追加决策行与事件；不改变露出结果。"""
    _get_task_or_404(db, task_id)
    try:
        task = vg.review_task(db, task_id, True, actor=staff.id,
                              note=body.note)
        db.commit()
    except ValueError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return _task_payload(task)


@admin_emby_router.post("/visibility-governance/tasks/{task_id}/reject")
def reject_visibility_task(
    task_id: int, body: ReviewRequest,
    staff: base_models.WebUser = Depends(require_staff),
    db: Session = Depends(get_db),
):
    """驳回：任务 → rejected，追加决策行与事件；不改变露出结果。"""
    _get_task_or_404(db, task_id)
    try:
        task = vg.review_task(db, task_id, False, actor=staff.id,
                              note=body.note)
        db.commit()
    except ValueError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return _task_payload(task)


@admin_emby_router.get("/visibility-governance/events")
def list_visibility_events(
    item_id: Optional[int] = Query(default=None),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=100),
    staff: base_models.WebUser = Depends(require_staff),
    db: Session = Depends(get_db),
):
    """事件流水只读列表（最新在前；item_id 可筛某条目）。"""
    rows, total = vg.list_events(db, item_id=item_id, page=page,
                                 page_size=page_size)
    items = []
    for ev in rows:
        try:
            detail = json.loads(ev.detail_json or "{}")
        except (TypeError, ValueError):
            detail = {}
        items.append({"id": ev.id, "task_id": ev.task_id,
                      "item_id": ev.item_id, "event_type": ev.event_type,
                      "actor": ev.actor, "detail": detail,
                      "created_at": ev.created_at.isoformat()
                      if ev.created_at else None})
    return {"items": items, "total": total, "page": page,
            "page_size": page_size}


class ScanPresetRequest(BaseModel):
    preset: str = Field(description="解析口径：movies / tvshows / mixed（= 库 collection_type 取值）")
    extensions: str = Field(default="", description="逗号分隔的扩展名白名单，空=不限")
    path_filters: str = Field(default="", description="JSON 数组子串过滤，空=不限")


@admin_emby_router.get("/library-scan-presets/{library_id}")
def get_library_scan_preset(
    library_id: int,
    staff: base_models.WebUser = Depends(require_staff),
    db: Session = Depends(get_db),
):
    """某库的扫描预设策略（无行时返回 preset=null，意为走现行口径）。"""
    row = (db.query(em.LibraryScanPreset)
           .filter(em.LibraryScanPreset.library_id == library_id).first())
    if row is None:
        return {"library_id": library_id, "preset": None, "extensions": "",
                "path_filters": "[]"}
    return {"library_id": library_id, "preset": row.preset,
            "extensions": row.extensions or "",
            "path_filters": row.path_filters or "[]"}


@admin_emby_router.put("/library-scan-presets/{library_id}")
def put_library_scan_preset(
    library_id: int, body: ScanPresetRequest,
    staff: base_models.WebUser = Depends(require_staff),
    db: Session = Depends(get_db),
):
    """配置某库的扫描预设策略（一库一行，upsert；开关关时只存配置不生效）。"""
    try:
        row = lsp.upsert_policy(db, library_id, body.preset,
                                body.extensions, body.path_filters)
        db.commit()
    except ValueError as exc:
        db.rollback()
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"library_id": library_id, "preset": row.preset,
            "extensions": row.extensions or "",
            "path_filters": row.path_filters or "[]"}
