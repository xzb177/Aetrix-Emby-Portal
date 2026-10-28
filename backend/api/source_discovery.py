"""找源路由（后端能力，不挂前端）。

总闸 ``SOURCE_DISCOVERY_ENABLED`` 默认关。关时本组接口惰性 404
（路由级依赖先于鉴权，未登录也不会先露 401，与商店同款做法）。
开时：
- ``GET /search``：要登录；按标题 / TMDB / IMDb 查候选，只读不写，
  任何模式下搜索都不建转存任务。
- ``POST /confirm``：要登录；确认后复用既有转存链路
  （``transfer_variant.create_user_job``）落到用户绑定盘。
  绑定 / 转存总闸 / 24h 节流 / provider 判定一律走既有规则，
  建不了按原因诚实回 failed，不假装入队。
- 本组接口不做会员门槛（简报未要求）：搜索只读本地库、无外部
  成本；确认受绑定与转存总闸双重约束。会员口径如后续要加，
  另起一单，不要顺手塞进本文件。
- 日志只记数量与 id，不记查询文本、标题与用户隐私。
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from backend import models
from backend.api.user import get_current_user
from backend.database import get_db
from backend.emby_server import transfer_variant as tv
from backend.emby_server.source_discovery import config, orchestrator
from backend.emby_server.source_discovery.contract import (
    ALREADY_QUEUED_MESSAGE,
    DISABLED_DETAIL,
    MASTER_OFF_MESSAGE,
    NO_BINDING_MESSAGE,
    NO_CANDIDATE_MESSAGE,
    NO_DRIVE_MESSAGE,
    NO_QUERY_MESSAGE,
    NO_SOURCE_FID_MESSAGE,
    QUEUED_MESSAGE,
    SOURCE_DISCOVERY_TRIGGER,
    THROTTLED_MESSAGE,
    TRIPLE_REQUIRED_MESSAGE,
    UNKNOWN_PROVIDER_MESSAGE,
    CandidateOut,
    ConfirmIn,
    ConfirmOut,
    SearchResponse,
)
from backend.emby_server.source_discovery.types import SearchQuery

logger = logging.getLogger(__name__)


def require_discovery_enabled() -> None:
    """总闸。关时 404，避免未开启的路径先被鉴权依赖回答成 401。"""
    if not config.discovery_enabled():
        raise HTTPException(status_code=404, detail=DISABLED_DETAIL)


router = APIRouter(
    prefix="/api/user/source-discovery",
    tags=["用户端·找源"],
    # 插在端点依赖之前：总闸关时先 404，鉴权依赖不会先回答 401。
    dependencies=[Depends(require_discovery_enabled)],
)


@router.get("/search", response_model=SearchResponse)
def search_sources(
    title: str = "",
    tmdb_id: str = "",
    imdb_id: str = "",
    user: models.WebUser = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> SearchResponse:
    """查候选：只读，绝不建任务。"""
    query = SearchQuery(title=title, tmdb_id=tmdb_id, imdb_id=imdb_id)
    if query.is_empty():
        raise HTTPException(status_code=400, detail=NO_QUERY_MESSAGE)
    candidates, used = orchestrator.search_candidates(db, query)
    return SearchResponse(
        candidates=[CandidateOut(**c.to_dict()) for c in candidates],
        total=len(candidates),
        adapters=used,
        auto_transfer_enabled=config.auto_transfer_enabled(),
    )


def _failed(
    local_item_id: int | None,
    source_id: int | None,
    upstream_item_id: str | None,
    message: str,
) -> ConfirmOut:
    return ConfirmOut(
        state="failed",
        local_item_id=local_item_id,
        source_id=source_id,
        upstream_item_id=upstream_item_id,
        message=message,
    )


@router.post("/confirm", response_model=ConfirmOut)
def confirm_source(
    body: ConfirmIn,
    user: models.WebUser = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> ConfirmOut:
    """确认候选并建用户盘转存任务（复用既有链路，不新写转存）。

    默认模式必须指定候选三元组；自动模式开时可只给 tmdb_id，
    由服务端按置信度自动挑最高候选。两种模式下确认前都会重查
    映射与来源行，对不上的一律视为候选不存在。
    """
    auto = config.auto_transfer_enabled()
    triple = (
        body.local_item_id is not None
        and body.source_id is not None
        and (body.upstream_item_id or "").strip() != ""
    )
    if triple:
        resolved = orchestrator.resolve_candidate(
            db,
            body.local_item_id,  # type: ignore[arg-type]
            body.source_id,  # type: ignore[arg-type]
            (body.upstream_item_id or "").strip(),
        )
        local_item_id = body.local_item_id
        source_id = body.source_id
        upstream_item_id = (body.upstream_item_id or "").strip()
    elif auto and (body.tmdb_id or "").strip():
        candidates, _used = orchestrator.search_candidates(
            db, SearchQuery(tmdb_id=(body.tmdb_id or "").strip())
        )
        if not candidates:
            raise HTTPException(status_code=404, detail=NO_CANDIDATE_MESSAGE)
        top = candidates[0]
        resolved = orchestrator.resolve_candidate(
            db, top.local_item_id, top.source_id or 0,
            top.upstream_item_id or "",
        )
        local_item_id = top.local_item_id
        source_id = top.source_id
        upstream_item_id = top.upstream_item_id
    else:
        raise HTTPException(status_code=400, detail=TRIPLE_REQUIRED_MESSAGE)
    if resolved is None:
        raise HTTPException(status_code=404, detail=NO_CANDIDATE_MESSAGE)
    item, _mapping, _source = resolved
    provider = tv._item_provider(db, item)
    if provider is None:
        return _failed(local_item_id, source_id, upstream_item_id,
                       UNKNOWN_PROVIDER_MESSAGE)
    has_binding = (
        tv.has_active_drive_binding(db, user.id)
        if provider == "drive"
        else tv.has_active_pan115_binding(db, user.id)
    )
    if not has_binding:
        return _failed(local_item_id, source_id, upstream_item_id,
                       NO_BINDING_MESSAGE)
    if not tv.master_enabled():
        return _failed(local_item_id, source_id, upstream_item_id,
                       MASTER_OFF_MESSAGE)
    try:
        job = tv.create_user_job(
            db, item, user.id, provider, trigger=SOURCE_DISCOVERY_TRIGGER
        )
    except tv.TransferError as exc:
        if exc.kind == "throttled":
            existing = tv._latest_job(db, item.id, "user", str(user.id))
            if existing is not None:
                return ConfirmOut(
                    state="already_queued",
                    job_id=existing.id,
                    local_item_id=local_item_id,
                    source_id=source_id,
                    upstream_item_id=upstream_item_id,
                    message=ALREADY_QUEUED_MESSAGE,
                )
            return _failed(local_item_id, source_id, upstream_item_id,
                           THROTTLED_MESSAGE)
        if exc.kind == "no_source_fid":
            message = NO_SOURCE_FID_MESSAGE
        elif exc.kind == "no_drive":
            message = NO_DRIVE_MESSAGE
        elif exc.kind == "master_off":
            message = MASTER_OFF_MESSAGE
        else:
            message = exc.message or "转存任务创建失败"
        return _failed(local_item_id, source_id, upstream_item_id, message)
    logger.info(
        "找源确认已建任务 job_id=%s item_id=%s source_id=%s",
        job.id, local_item_id, source_id,
    )
    return ConfirmOut(
        state="queued",
        job_id=job.id,
        local_item_id=local_item_id,
        source_id=source_id,
        upstream_item_id=upstream_item_id,
        message=QUEUED_MESSAGE,
    )
