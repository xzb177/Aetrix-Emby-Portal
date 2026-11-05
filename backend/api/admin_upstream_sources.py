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


@admin_emby_router.get("/upstream-sources/ad-skip/detections/{mapping_id}")
def get_ad_skip_detection(
    mapping_id: int,
    staff: base_models.WebUser = Depends(require_staff),
    db: Session = Depends(get_db),
):
    """片头印制广告检测结果只读查询（广告剪除试点，2026-11-03）。

    按映射 id 查 cms_ad_skip 的现行结论：状态/跳过秒数/置信度/
    证据帧路径；无记录回 404。管理页面暂未接（试点期先给接口
    口，页面列后续项）。
    """
    from backend.emby_server import cms_ad_skip as adskip

    row = adskip.get_detection(db, mapping_id)
    if row is None:
        raise HTTPException(status_code=404, detail="该映射尚无检测记录")
    return adskip.detection_payload(row)


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
    """删除上游源并联动清理（PA-038：托管行软删＋映射／关系／
    生命周期／绑定／非 running 任务残留行硬删，见 ups.delete_source）。"""
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


class UpstreamExclusiveLineConfigIn(BaseModel):
    """白名单专属线路配置写入体（2026-11-04；2026-11-05 加 pilot 字段）。

    source_ids＝专属名单全量替换（管理页列表增删后回写整表）；
    pilot_base_url 缺省（None）＝基址不动，非 None 时写入
    （空串＝清掉基址、专属线整体惰性）。
    pilot_entry_enabled / pilot_entry_secret 同口径：缺省＝不动，
    secret 非 None 时写入（空串＝清除密钥、pilot 入口永不认定）。
    """

    source_ids: list[int] = Field(default_factory=list)
    pilot_base_url: Optional[str] = None
    pilot_entry_enabled: Optional[bool] = None
    pilot_entry_secret: Optional[str] = None


def _exclusive_line_config_view(db: Session) -> dict:
    """专属线路配置读视图（默认线路的派生视图，旧口兼容门面）。

    名单回 id 列表；基址与入口密钥只回「已配/未配」——两者都含
    口令/密钥，原文不回浏览器（页面只需状态＋重设入口）；入口
    开关回当前生效值。真源是默认线路记录（见 _line_view 的新口）。
    """
    from backend.emby_server import upstream_playback as _up

    line = _up.default_line(db)
    return {
        "source_ids": sorted(line["legacy_source_ids"]) if line else [],
        "pilot_configured": bool(line and line["stream_base_url"]),
        "pilot_entry_enabled": bool(line and line["enabled"]),
        "pilot_entry_configured": bool(line and line["entry_secret"]),
    }


@admin_emby_router.get("/upstream-exclusive-line-config")
def get_upstream_exclusive_line_config(
    staff: base_models.WebUser = Depends(require_staff),
    db: Session = Depends(get_db),
):
    """读白名单专属线路配置（默认线路视图，旧口兼容门面）。"""
    return _exclusive_line_config_view(db)


@admin_emby_router.put("/upstream-exclusive-line-config")
def put_upstream_exclusive_line_config(
    payload: UpstreamExclusiveLineConfigIn,
    staff: base_models.WebUser = Depends(require_staff),
    db: Session = Depends(get_db),
):
    """写白名单专属线路配置（旧口兼容门面，落到默认线路记录）。

    能力化后配置真源是线路记录集：本口把 source_ids → 默认线路
    的 legacy 名单、pilot_base_url → 取流基址、pilot_entry_* →
    启用开关/入口密钥，经 save_exclusive_lines 统一落库（保存即
    失效配置缓存，热读立刻按新值执法）。无线路时按迁移线路的
    形状新建默认线路。管理端新界面走 /exclusive-lines 系列口，
    本口只作存量兼容。审计由 /api/admin/emby/* 中间件统一记录。
    """
    from backend.emby_server import upstream_playback as _up

    lines = _up.exclusive_lines(db)
    # 必须改 lines 列表里的那个 dict 本身（default_line 是另一次
    # 解析出的副本，改它不会落到待保存的列表上）。
    line = None
    for cand in lines:
        if cand["id"] == _up.MIGRATED_LINE_ID:
            line = cand
            break
    if line is None and lines:
        line = lines[0]
    if line is None:
        line = {
            "id": _up.MIGRATED_LINE_ID, "name": _up.MIGRATED_LINE_NAME,
            "entry_url": "", "stream_base_url": "", "entry_secret": "",
            "audience": _up.AUDIENCE_WHITELIST, "scope_mode": "all",
            "scope_source_ids": [], "legacy_source_ids": [],
            "enabled": False,
        }
        lines = [line]
    line["legacy_source_ids"] = _up._coerce_id_list(payload.source_ids)
    if payload.pilot_base_url is not None:
        line["stream_base_url"] = payload.pilot_base_url
        if line["stream_base_url"]:
            origin = _up._origin_of(line["stream_base_url"])
            if origin:
                line["entry_url"] = origin
    if payload.pilot_entry_enabled is not None:
        line["enabled"] = payload.pilot_entry_enabled
    if payload.pilot_entry_secret is not None:
        line["entry_secret"] = payload.pilot_entry_secret
    _up.save_exclusive_lines(db, lines)
    db.commit()
    return _exclusive_line_config_view(db)


# ---------------------------------------------------------------------------
# 专属线路通用能力（2026-11-05）：线路记录 CRUD（管理端统一入口）
#
# 一条线路＝名称/入口地址/取流基址/入口密钥/准入人群/适用范围/
# 启用开关/存量旧地址名单。取流基址与入口密钥含口令/密钥：所有
# 读口只回「已配/未配」，原文永不回浏览器；写入走
# upstream_playback.save_exclusive_lines 统一通道（config_service
# 校验＋缓存失效），保存即热读生效。审计由 /api/admin/emby/* 中
# 间件统一记录。
# ---------------------------------------------------------------------------


def _line_view(line: dict) -> dict:
    """线路读视图：基址与密钥只回已配状态，原文不回显。"""
    return {
        "id": line["id"],
        "name": line["name"],
        "entry_url": line["entry_url"],
        "audience": line["audience"],
        "scope_mode": line["scope_mode"],
        "scope_source_ids": list(line["scope_source_ids"]),
        "legacy_source_ids": list(line["legacy_source_ids"]),
        "enabled": bool(line["enabled"]),
        "stream_configured": bool(line["stream_base_url"]),
        "secret_configured": bool(line["entry_secret"]),
    }


class ExclusiveLineIn(BaseModel):
    """新建线路写入体（基址/密钥创建时可一并给，后续可改）。"""

    name: str
    entry_url: str = ""
    stream_base_url: Optional[str] = None
    entry_secret: Optional[str] = None
    audience: str = "whitelist"
    scope_mode: str = "all"
    scope_source_ids: list[int] = Field(default_factory=list)
    legacy_source_ids: list[int] = Field(default_factory=list)
    enabled: bool = True


class ExclusiveLineUpdateIn(BaseModel):
    """编辑线路写入体：None＝不动；基址/密钥空串＝清除。"""

    name: Optional[str] = None
    entry_url: Optional[str] = None
    stream_base_url: Optional[str] = None
    entry_secret: Optional[str] = None
    audience: Optional[str] = None
    scope_mode: Optional[str] = None
    scope_source_ids: Optional[list[int]] = None
    legacy_source_ids: Optional[list[int]] = None
    enabled: Optional[bool] = None


class ExclusiveLineResetSecretIn(BaseModel):
    """重置入口密钥写入体：新值由管理员提供（须同步改反代侧注入
    值）；响应只回已配状态，原文不回显（与全线口径一致）。"""

    secret: str


def _get_line_or_404(db: Session, line_id: str):
    from backend.emby_server import upstream_playback as _up

    lines = _up.exclusive_lines(db)
    for line in lines:
        if line["id"] == line_id:
            return lines, line
    raise HTTPException(status_code=404, detail="专属线路不存在")


@admin_emby_router.get("/exclusive-lines")
def list_exclusive_lines(
    staff: base_models.WebUser = Depends(require_staff),
    db: Session = Depends(get_db),
):
    """线路列表（含迁移视图：旧四键未落库时第一条即迁移线路）。"""
    from backend.emby_server import upstream_playback as _up

    return {"lines": [_line_view(l) for l in _up.exclusive_lines(db)]}


@admin_emby_router.post("/exclusive-lines")
def create_exclusive_line(
    payload: ExclusiveLineIn,
    staff: base_models.WebUser = Depends(require_staff),
    db: Session = Depends(get_db),
):
    """新建一条线路（id 服务端生成）；保存即入口认定/选线热生效。"""
    import uuid as _uuid

    from backend.emby_server import upstream_playback as _up

    lines = _up.exclusive_lines(db)
    line = {
        "id": _uuid.uuid4().hex[:12],
        "name": payload.name,
        "entry_url": payload.entry_url,
        "stream_base_url": payload.stream_base_url or "",
        "entry_secret": payload.entry_secret or "",
        "audience": payload.audience,
        "scope_mode": payload.scope_mode,
        "scope_source_ids": payload.scope_source_ids,
        "legacy_source_ids": payload.legacy_source_ids,
        "enabled": payload.enabled,
    }
    lines.append(line)
    saved = _up.save_exclusive_lines(db, lines)
    db.commit()
    created = next(l for l in saved if l["id"] == line["id"])
    return {"line": _line_view(created)}


@admin_emby_router.put("/exclusive-lines/{line_id}")
def update_exclusive_line(
    line_id: str,
    payload: ExclusiveLineUpdateIn,
    staff: base_models.WebUser = Depends(require_staff),
    db: Session = Depends(get_db),
):
    """编辑线路：None 字段不动；基址/密钥传空串＝清除该项。"""
    from backend.emby_server import upstream_playback as _up

    lines, line = _get_line_or_404(db, line_id)
    for field in ("name", "entry_url", "stream_base_url", "entry_secret",
                  "audience", "scope_mode", "scope_source_ids",
                  "legacy_source_ids", "enabled"):
        value = getattr(payload, field)
        if value is not None:
            line[field] = value
    saved = _up.save_exclusive_lines(db, lines)
    db.commit()
    updated = next(l for l in saved if l["id"] == line_id)
    return {"line": _line_view(updated)}


@admin_emby_router.post("/exclusive-lines/{line_id}/reset-secret")
def reset_exclusive_line_secret(
    line_id: str,
    payload: ExclusiveLineResetSecretIn,
    staff: base_models.WebUser = Depends(require_staff),
    db: Session = Depends(get_db),
):
    """重置线路入口密钥：只回已配状态，新旧值都不回显。

    重置后旧密钥命中的请求立刻按普通入口处理；反代侧注入值须
    由管理员同步改成同值，线路才重新被认定。
    """
    from backend.emby_server import upstream_playback as _up

    if not payload.secret or not payload.secret.strip():
        raise HTTPException(status_code=400, detail="密钥不能为空")
    lines, line = _get_line_or_404(db, line_id)
    line["entry_secret"] = payload.secret
    _up.save_exclusive_lines(db, lines)
    db.commit()
    return {"secret_configured": True}


@admin_emby_router.delete("/exclusive-lines/{line_id}")
def delete_exclusive_line(
    line_id: str,
    staff: base_models.WebUser = Depends(require_staff),
    db: Session = Depends(get_db),
):
    """删除线路：删后该线入口不再被认定、用户端不再展示。"""
    from backend.emby_server import upstream_playback as _up

    lines, _line = _get_line_or_404(db, line_id)
    lines = [l for l in lines if l["id"] != line_id]
    _up.save_exclusive_lines(db, lines)
    db.commit()
    return {"deleted": True}


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
