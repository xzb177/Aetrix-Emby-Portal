# -*- coding: utf-8 -*-
"""求片中心 API 路由（公益服模块3-娱乐板块）

.. deprecated::
    求片已并入统一求片流程（backend/media_seek.py + /api/media-seek）。
    本模块所有端点均标记 deprecated，仅保留兼容，不再演进。

用户端（前缀 /api/requests）：
- POST /            提交求片
- GET  /            我的求片列表
- GET  /quota       本月额度

管理端影子口已下线（批 3）：原 GET /welfare/requests 及 approve/reject/done
四口由 backend/api/welfare_admin.py 先注册、实际生效，本模块的重复注册
从未被匹配，现予删除；求片审核以 /api/media-seek 为准。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from backend import models
from backend import welfare_requests as req_mod
from backend.api.user import get_current_user
from backend.database import get_db

router = APIRouter(prefix="/api/requests", tags=["娱乐-求片"])


class SubmitRequest(BaseModel):
    tmdb_id: str = Field(..., description="TMDB ID")
    media_type: str = Field("movie", description="movie 或 tv")
    title: str = Field(..., description="影片标题")


class ReviewNote(BaseModel):
    admin_note: str = Field("", description="审核备注")


@router.post("", deprecated=True)
def submit(
    req: SubmitRequest,
    current_user: models.WebUser = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """提交求片"""
    try:
        r = req_mod.submit_request(db, current_user, req.tmdb_id, req.media_type, req.title)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return req_mod._to_dict(r)


@router.get("", deprecated=True)
def my_list(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    current_user: models.WebUser = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """我的求片列表"""
    return req_mod.list_my_requests(db, current_user, page, page_size)


@router.get("/quota", deprecated=True)
def quota(
    current_user: models.WebUser = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """本月求片额度"""
    return req_mod.get_monthly_quota(db, current_user)
