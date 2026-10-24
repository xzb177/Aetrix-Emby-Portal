"""管理后台 · 上游源 CRUD（FMBY 学习 #25 25-1：只做源管理）。

路由挂在 admin_emby_router 上（/api/admin/emby/upstream-sources/*）：
鉴权走 require_staff（只认 Authorization 头 JWT，与媒体库管理同口径），
成功写操作的审计由 emby_server/audit.py 中间件统一记录（/api/admin/emby/*
全覆盖），本模块不另写审计行。

源行的全部写操作只经 emby_server/upstream_sources（魂 6：状态集中维护），
本模块只做「请求形态 → 模块调用 → 错误映射」三件事：
- UpstreamSourceError → 400；DuplicateSourceName → 409；查无此源 → 404；
- 响应一律走 source_payload：永远不出凭据原文、不出密封值，只出 has_secret；
- 新建恒为关闭（创建接口不接受 enabled 入参），启用/停用是单独端点。

本模块不探活、不拉目录、不拼播放地址（那是 25-2/25-3/25-4 的活）。
"""

from __future__ import annotations

import json
import logging
from typing import Optional

from fastapi import Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from backend import models as base_models
from backend.database import get_db
from backend.emby_server import models as em
from backend.emby_server import upstream_sources as ups
from backend.emby_server.portal import admin_emby_router, require_staff

logger = logging.getLogger(__name__)


def _get_source_or_404(db: Session, source_id: int) -> em.UpstreamSource:
    source = ups.get_source(db, source_id)
    if source is None:
        raise HTTPException(status_code=404, detail="上游源不存在")
    return source


def _commit_or_409(db: Session, name: str) -> None:
    """提交；唯一冲突（并发建同名）映射 409，其余 DB 错响亮抛。"""
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(
            status_code=409, detail=f"上游源名称已存在：{name!r}") from exc


@admin_emby_router.get("/upstream-sources")
def list_upstream_sources(
    staff: base_models.WebUser = Depends(require_staff),
    db: Session = Depends(get_db),
):
    """上游源列表（id 升序；源是管理员手工建的，数量级是「台」，不分页）。"""
    return {"items": [ups.source_payload(s) for s in ups.list_sources(db)]}


@admin_emby_router.get("/upstream-sources/{source_id}")
def get_upstream_source(
    source_id: int,
    staff: base_models.WebUser = Depends(require_staff),
    db: Session = Depends(get_db),
):
    """上游源详情（不出凭据原文、不出密封值，只出 has_secret）。"""
    return ups.source_payload(_get_source_or_404(db, source_id))


class UpstreamSourceCreateRequest(BaseModel):
    name: str = Field(description="源名称（管理端辨识用，全站唯一）")
    source_type: str = Field(
        description="emby / jellyfin（苹果 CMS 型已后置，传它直接被拒）")
    base_url: str = Field(description="上游基址（只许 http/https 公网地址）")
    auth_method: str = Field(default="password",
                              description="password（实测形态）/ api_key")
    username: Optional[str] = Field(
        default=None, description="password 形态必填；api_key 形态不填")
    secret: Optional[str] = Field(
        default=None,
        description="凭据原文（密码或 api_key）：只存密封值，响应永不回显；"
                    "不传 = 不配凭据（匿名访问）")
    extra_headers: Optional[dict] = Field(
        default=None, description="附加请求头（键值均为字符串的 JSON 对象）")
    user_agent: Optional[str] = None
    referer: Optional[str] = None
    note: Optional[str] = None


@admin_emby_router.post("/upstream-sources")
def create_upstream_source(
    req: UpstreamSourceCreateRequest,
    staff: base_models.WebUser = Depends(require_staff),
    db: Session = Depends(get_db),
):
    """新建上游源（新建恒为关闭；启用走单独的 enable 端点）。"""
    try:
        row = ups.create_source(
            db, name=req.name, source_type=req.source_type,
            base_url=req.base_url, auth_method=req.auth_method,
            username=req.username, secret=req.secret,
            extra_headers_json=(json.dumps(req.extra_headers, ensure_ascii=False)
                                if req.extra_headers is not None else None),
            user_agent=req.user_agent, referer=req.referer, note=req.note)
    except ups.DuplicateSourceName as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ups.UpstreamSourceError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    _commit_or_409(db, req.name)
    return ups.source_payload(row)


class UpstreamSourceUpdateRequest(BaseModel):
    name: Optional[str] = None
    source_type: Optional[str] = None
    base_url: Optional[str] = None
    auth_method: Optional[str] = None
    username: Optional[str] = None
    # 凭据三态：字段 absent = 不动；传非空 = 替换；传 "" = 清掉（改匿名）。
    secret: Optional[str] = None
    # 附加请求头三态：absent = 不动；传对象 = 替换；传 null = 清掉。
    extra_headers: Optional[dict] = None
    user_agent: Optional[str] = None
    referer: Optional[str] = None
    note: Optional[str] = None


@admin_emby_router.put("/upstream-sources/{source_id}")
def update_upstream_source(
    source_id: int,
    req: UpstreamSourceUpdateRequest,
    staff: base_models.WebUser = Depends(require_staff),
    db: Session = Depends(get_db),
):
    """编辑上游源（只改 body 里出现的字段；enabled 不在这里改）。"""
    source = _get_source_or_404(db, source_id)
    data = req.dict(exclude_unset=True)
    try:
        row = ups.update_source(
            db, source,
            name=data.get("name"),
            source_type=data.get("source_type"),
            base_url=data.get("base_url"),
            auth_method=data.get("auth_method"),
            username=data.get("username"),
            secret=data.get("secret"), secret_set=("secret" in data),
            extra_headers_json=(json.dumps(data["extra_headers"],
                                           ensure_ascii=False)
                                if data.get("extra_headers") is not None
                                else None),
            extra_headers_set=("extra_headers" in data),
            user_agent=data.get("user_agent"),
            user_agent_set=("user_agent" in data),
            referer=data.get("referer"),
            referer_set=("referer" in data),
            note=data.get("note"), note_set=("note" in data))
    except ups.DuplicateSourceName as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ups.UpstreamSourceError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    _commit_or_409(db, row.name)
    return ups.source_payload(row)


@admin_emby_router.post("/upstream-sources/{source_id}/enable")
def enable_upstream_source(
    source_id: int,
    staff: base_models.WebUser = Depends(require_staff),
    db: Session = Depends(get_db),
):
    """启用上游源（单独动作；启用本身不探活，健康状态仍是占位值）。"""
    row = ups.enable_source(db, _get_source_or_404(db, source_id))
    db.commit()
    return ups.source_payload(row)


@admin_emby_router.post("/upstream-sources/{source_id}/disable")
def disable_upstream_source(
    source_id: int,
    staff: base_models.WebUser = Depends(require_staff),
    db: Session = Depends(get_db),
):
    """停用上游源（25-2/25-4 只许读 enabled 的源，停用即断聚合与播放）。"""
    row = ups.disable_source(db, _get_source_or_404(db, source_id))
    db.commit()
    return ups.source_payload(row)


@admin_emby_router.delete("/upstream-sources/{source_id}")
def delete_upstream_source(
    source_id: int,
    staff: base_models.WebUser = Depends(require_staff),
    db: Session = Depends(get_db),
):
    """删除上游源（只删本行；25-2/25-3 落地后删源联动须复核本函数）。"""
    ups.delete_source(db, _get_source_or_404(db, source_id))
    db.commit()
    return {"success": True}
