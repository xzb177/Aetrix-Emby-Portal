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

本模块不探活、不拼播放地址（那是 25-3/25-4 的活）；例外是两个手动触发口：
苹果 CMS 手动同步触发口（只调生产触发入口，不自写同步逻辑），以及
25-2 的手动聚合触发口（POST /upstream-sources/{id}/aggregate）。
聚合经常超过管理端 axios 的 30 秒，POST 只做入队并立刻返回任务 id，
真正的拉目录与配对由 worker 认领循环在 worker 进程内执行
（O-03，见 emby_server/upstream_aggregate_jobs），进度用
GET /upstream-sources/{id}/aggregate/{task_id} 查。
管理端不直接发上游请求。触发是手动动作，不接自动调度、不接定时任务。
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
from backend.emby_server import applecms_production as acp
from backend.emby_server import models as em
from backend.emby_server import upstream_sources as ups
from backend.emby_server import upstream_aggregate_jobs as uaj
from backend.emby_server import upstream_aggregate_run as uar
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
        description="emby / jellyfin / applecms（苹果 CMS 与 Emby 并列）")
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
    # 独有资源进库开关（按源字段，默认开）：absent = 不动；翻转时
    # 联动该源托管行隐藏/恢复（见 upstream_sources.update_source）。
    import_enabled: Optional[bool] = None


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
            note=data.get("note"), note_set=("note" in data),
            import_enabled=data.get("import_enabled"))
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


@admin_emby_router.post("/upstream-sources/{source_id}/sync-applecms")
def sync_applecms_upstream_source(
    source_id: int,
    staff: base_models.WebUser = Depends(require_staff),
    db: Session = Depends(get_db),
):
    """管理端手动触发一次苹果 CMS 同步（后续四件之 ②，仅管理员）。

    只接受苹果 CMS 型源（其它类型 400，本口不替 Emby/Jellyfin
    源跑同步）；同步逻辑复用生产触发入口
    ``applecms_production.production_trigger_applecms_sync`` 的单次
    有界流程，本路由不另起一套同步实现、不读播放地址。两闸未同
    开时触发入口返回 disabled 结果，本路由映射为 409（不是成功），
    调用方凭此知道本轮根本没跑。响应只含计数与健康，不含任何
    播放地址与未配对明细（未配对展示消费走展示层函数，不走本口）。
    """
    source = _get_source_or_404(db, source_id)
    if getattr(source, "source_type", None) != "applecms":
        raise HTTPException(
            status_code=400,
            detail="本接口只对苹果 CMS 型上游源触发同步")
    result = acp.production_trigger_applecms_sync(db, source_id)
    if result.health == "disabled":
        raise HTTPException(
            status_code=409,
            detail="苹果 CMS 同步未开启（APPLECMS_ENABLED 与 "
                   "UPSTREAM_SYNC_ENABLED 两闸须同开）")
    return {
        "source_id": result.source_id,
        "scanned": result.scanned,
        "mapped": result.mapped,
        "unmatched_count": len(result.unmatched),
        "hidden": result.hidden,
        "errors": result.errors,
        "health": result.health,
    }


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


class UpstreamAggregateRequest(BaseModel):
    page_size: Optional[int] = Field(
        default=None, ge=10, le=500,
        description="Emby Items 单页大小（10–500；不传 = 用配置缺省"
                    "（SystemConfig upstream_aggregate_default_page_size，"
                    "出厂 500），与调度路径同口径（O-05）")


@admin_emby_router.post(
    "/upstream-sources/{source_id}/verify-playback-samples")
def verify_upstream_playback_samples(
    source_id: int,
    staff: base_models.WebUser = Depends(require_staff),
    db: Session = Depends(get_db),
):
    """O-09 第 3 层一次性验证（手动触发，绝不常驻）：对该源映射按
    evidence 分层抽样，先过第 2 层存在性核验，通过的样本再调
    resolve_upstream_stream 核 302 地址，失败按错误码计数回报。

    每次调用都会让通过样本在上游产生真实起播探活（成本 = 样本量
    级，百条以内）；播放总闸 UPSTREAM_PLAYBACK_ENABLED 关着时
    resolve 按 upstream_playback_disabled 失败、照实计入 failed，
    不假装能播。全程只读映射：不删不改任何映射行。响应不出上游
    播放地址原文，只回计数。
    """
    source = _get_source_or_404(db, source_id)
    from backend.emby_server import upstream_mapping_patrol as ump
    return ump.verify_playback_samples(db, source_id=source.id)


@admin_emby_router.post("/upstream-sources/{source_id}/aggregate")
def aggregate_upstream_source(
    source_id: int,
    body: Optional[UpstreamAggregateRequest] = None,
    staff: base_models.WebUser = Depends(require_staff),
    db: Session = Depends(get_db),
):
    """手动触发某源的目录聚合，立刻返回任务，不阻塞到跑完。

    worker 认领循环再走原来的按库流程：取上游库清单 → 逐库拉目录 →
    聚合 → 配对 → 写路径开关开时写映射。单库失败仍只进
    failed_libraries，不毁已写映射。写映射的提交在认领执行线程里完成
    （record_mapping 只 flush，逐库提交）。

    不发上游请求就能判定的错误仍在本接口立刻返回，映射与以前相同：
    查无此源 404；源未启用 / applecms 型 400。
    要打到上游才知道的错误（凭据失效 409、额度 429、上游不可用 502、
    映射写入冲突 409）记在任务上，由查询口带回，不在这里空等。
    响应不出凭据原文、不出密封值、不出上游播放地址。
    """
    source = _get_source_or_404(db, source_id)
    try:
        uar.ensure_aggregatable(source)
    except uar.UpstreamAggregateRunError as exc:
        raise HTTPException(status_code=exc.status, detail=exc.message) from exc
    if body is not None and body.page_size is not None:
        page_size = body.page_size
    else:
        page_size = uar.configured_default_page_size(db)
    return uaj.enqueue(source.id, page_size)


@admin_emby_router.get(
    "/upstream-sources/{source_id}/aggregate/{task_id}")
def get_upstream_aggregate_task(
    source_id: int,
    task_id: str,
    staff: base_models.WebUser = Depends(require_staff),
):
    """查一条聚合任务（O-01 起读任务表，进程重启后终态仍可查）。

    完成时 result 是聚合摘要（与原先同步口同形，但不带全量
    pairs——明细走映射列表查，见设计稿 P-12），另带进度计数
    （libraries_total / libraries_done / fetched / paired /
    written）与时间戳。失败时 HTTP 仍是 200，``error.status`` /
    ``error.code`` / ``error.detail`` 沿用同步口的状态码和中文
    文案（凭据 409、上游不可用 502、额度 429、映射冲突 409 等），
    方便轮询区分「任务还在跑」和「这次聚合失败了」。任务不存在、
    或任务不属于这个源，返回 404。
    """
    view = uaj.get_task(source_id, task_id)
    if view is None:
        raise HTTPException(status_code=404, detail="聚合任务不存在或已过期")
    return view


@admin_emby_router.post(
    "/upstream-sources/{source_id}/aggregate/{task_id}/resume")
def resume_upstream_aggregate_task(
    source_id: int,
    task_id: str,
    staff: base_models.WebUser = Depends(require_staff),
    db: Session = Depends(get_db),
):
    """续跑一条被打断 / 失败的聚合任务（O-01）。

    新建一条任务，跳过原任务里已完成的库（映射已逐库落库，不
    重复拉取），未完成的库重跑。原任务不存在 / 不属于该源 /
    不在可续跑状态（interrupted、failed 以外），返回 404；
    同源已有进行中任务时返回那条任务的视图（与触发口同口径）。
    """
    source = _get_source_or_404(db, source_id)
    view = uaj.resume_task(source.id, task_id)
    if view is None:
        raise HTTPException(status_code=404,
                            detail="聚合任务不存在或不可续跑")
    return view


class UpstreamDetailLineConfigIn(BaseModel):
    """详情页上游线路开关写入体（U-02）。"""

    enabled: bool


@admin_emby_router.get("/upstream-detail-line-config")
def get_upstream_detail_line_config(
    staff: base_models.WebUser = Depends(require_staff),
    db: Session = Depends(get_db),
):
    """读详情页上游线路开关（SystemConfig，无行＝开，见 upstream_playback）。"""
    from backend.emby_server import upstream_playback as _up

    return {"enabled": _up.detail_line_enabled(db)}


@admin_emby_router.put("/upstream-detail-line-config")
def put_upstream_detail_line_config(
    payload: UpstreamDetailLineConfigIn,
    staff: base_models.WebUser = Depends(require_staff),
    db: Session = Depends(get_db),
):
    """写详情页上游线路开关（走 config_service 统一保存通道）。

    关时详情/PlaybackInfo 来源列表不再出现上游线路、取流口 404；
    开时恢复展示。审计由 /api/admin/emby/* 中间件统一记录。
    """
    from backend import config_service
    from backend.emby_server import upstream_playback as _up

    config_service.save_config(
        db, {_up.KEY_DETAIL_LINE_ENABLED: payload.enabled},
        schema="upstream_aggregate")
    db.commit()
    return {"enabled": _up.detail_line_enabled(db)}


class UpstreamFetchConcurrencyConfigIn(BaseModel):
    """聚合拉取并发写入体（2026-11-01 加速）。"""

    concurrency: int


@admin_emby_router.get("/upstream-fetch-concurrency-config")
def get_upstream_fetch_concurrency_config(
    staff: base_models.WebUser = Depends(require_staff),
    db: Session = Depends(get_db),
):
    """读聚合拉取并发（SystemConfig，无行＝默认 4，钳 [1, 16]）。"""
    return {"concurrency": uar.configured_fetch_concurrency(db)}


@admin_emby_router.put("/upstream-fetch-concurrency-config")
def put_upstream_fetch_concurrency_config(
    payload: UpstreamFetchConcurrencyConfigIn,
    staff: base_models.WebUser = Depends(require_staff),
    db: Session = Depends(get_db),
):
    """写聚合拉取并发（走 config_service 统一保存通道）。

    范围 [1, 16] 由 upstream_aggregate 家族 schema 校验，越界抛
    ConfigValueError（main.py 统一转 400）。并发只改逐库拉取的
    请求时序；上游不稳时调回 1 即回串行旧行为。审计由
    /api/admin/emby/* 中间件统一记录。
    """
    from backend import config_service

    config_service.save_config(
        db, {uar.KEY_FETCH_CONCURRENCY: payload.concurrency},
        schema="upstream_aggregate")
    db.commit()
    return {"concurrency": uar.configured_fetch_concurrency(db)}
