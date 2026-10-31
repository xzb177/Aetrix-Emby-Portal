"""想看商店路由。

总闸 ``STORE_ENABLED`` 默认关。关时本组接口惰性 404（路由级依赖先于鉴权，
未登录也不会先露 401）。开时：

- ``GET /catalog``、``GET /items/{tmdb_id}`` 访客只读（目录是空列表；
  详情只回 tmdb_id 与库内是否已有条目，不拉 TMDB）。
- ``POST /want``、``GET /wants`` 要登录，并且要有效会员订阅
  （``subscriptions.has_active_subscription``）。
- 想看的 R0 判定：``tmdb_id`` 对得上未下架的 ``emby_items`` 才算库内。
  对不上返回 ``missing_source``，文案写明不能直接开播，绝不给即时开播错觉。
- 对得上时调 ``transfer_variant.create_user_job`` 建用户盘转存任务
  （trigger=``store_want``）；建不了时按原因诚实回 ``failed``，不假装入队。

播放、多账号池、预热、R1 自动求片、R2 索引都不在本文件。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from backend import models, subscriptions
from backend.api.user import get_current_user
from backend.database import get_db
from backend.emby_server import models as em
from backend.emby_server import transfer_variant as tv
from backend.emby_server.store_contract import (
    MISSING_SOURCE_MESSAGE,
    NO_BINDING_MESSAGE,
    NO_DRIVE_MESSAGE,
    NO_SOURCE_FID_MESSAGE,
    STORE_DISABLED_DETAIL,
    STORE_MEMBERSHIP_REQUIRED_DETAIL,
    TRANSFER_MASTER_OFF_MESSAGE,
    UNKNOWN_PROVIDER_MESSAGE,
    WANT_ALREADY_QUEUED_MESSAGE,
    WANT_FAILED_GENERIC_MESSAGE,
    WANT_QUEUED_MESSAGE,
    WANT_STATE_MESSAGES,
    CatalogResponse,
    ItemResponse,
    WantIn,
    WantListResponse,
    WantRecord,
    store_enabled,
)

#: 想看商店建的转存任务的 trigger（与播放触发、追剧触发分桶可查，
#: 但 24h 节流按归属桶算，不按 trigger 分——同条目同用户 24h 内已有
#: 任何任务行都不重复建）。
STORE_WANT_TRIGGER = "store_want"

def require_store_enabled() -> None:
    """总闸。关时 404，避免未开启的路径先被鉴权依赖回答成 401。"""
    if not store_enabled():
        raise HTTPException(status_code=404, detail=STORE_DISABLED_DETAIL)


router = APIRouter(
    prefix="/api/user/store",
    tags=["用户端·想看商店"],
    # 插在端点依赖之前：总闸关时先 404，鉴权依赖不会先回答 401。
    dependencies=[Depends(require_store_enabled)],
)


def require_store_member(db: Session, user: models.WebUser) -> None:
    """想看写入和想看清单要有效会员订阅。

    复用 ``subscriptions.has_active_subscription``（按 end_date 现算，
    全站会员口径）。不走 ``can_play`` / ``ensure_playback_allowed``：
    那条口在公益服或付费墙关闭时会放行播放，想看商店锁的是会员资格，
    能播放不等于能想看。
    """
    if subscriptions.has_active_subscription(db, user.id):
        return
    raise HTTPException(status_code=403, detail=STORE_MEMBERSHIP_REQUIRED_DETAIL)


def find_library_item(db: Session, tmdb_id: str) -> em.MediaItem | None:
    """R0：tmdb 是否映射到现有 emby_items。

    下架行与隐藏行不算（A-113：隐藏条目不得经在库判定探查）。
    优先电影 / 剧集主条，没有再退到任意未下架行
    （避免只剩一集时把「库里有」判丢）。不创建、不修改任何行。
    """
    key = (tmdb_id or "").strip()
    if not key:
        return None

    def visible():
        # 每次新开查询：Query.filter 在部分版本上会改自身，不能复用同一条。
        return db.query(em.MediaItem).filter(
            em.MediaItem.tmdb_id == key,
            em.MediaItem.deleted_at.is_(None),
            em.MediaItem.is_hidden == False,  # noqa: E712
        )

    primary = (
        visible()
        .filter(em.MediaItem.item_type.in_(("movie", "series")))
        .order_by(em.MediaItem.merged_into_id.isnot(None), em.MediaItem.id)
        .first()
    )
    if primary is not None:
        return primary
    return visible().order_by(em.MediaItem.id).first()


def _job_state(status: str) -> str:
    """转存任务行 status → 想看状态。未见过的 status 当已入队处理，
    绝不谎报失败。"""
    return {
        "pending": "wanted",
        "running": "transferring",
        "done": "ready",
        "failed": "failed",
    }.get(status or "", "wanted")


def _record_for_job(job, tmdb_id: str, in_library: bool,
                    library_item_id: int | None, message: str) -> WantRecord:
    state = _job_state(job.status)
    return WantRecord(
        tmdb_id=tmdb_id,
        state=state,
        in_library=in_library,
        library_item_id=library_item_id,
        job_id=job.id,
        message=message,
    )


def _latest_user_job(db: Session, item_id: int, user_id: int):
    """该条目该用户归属桶内最新的一行任务（或 None）。"""
    return tv._latest_job(db, item_id, "user", str(user_id))


@router.get("/catalog", response_model=CatalogResponse)
def get_catalog() -> CatalogResponse:
    """访客可浏览。骨架不接片源，固定空列表。"""
    return CatalogResponse(items=[], total=0)


@router.get("/items/{tmdb_id}", response_model=ItemResponse)
def get_item(
    tmdb_id: str,
    db: Session = Depends(get_db),
) -> ItemResponse:
    """访客可看详情骨架。只填 tmdb_id 与 in_library，不拉外部元数据。"""
    key = tmdb_id.strip()
    row = find_library_item(db, key)
    return ItemResponse(
        tmdb_id=key,
        in_library=row is not None,
        library_item_id=row.id if row is not None else None,
    )


@router.post("/want", response_model=WantRecord)
def post_want(
    body: WantIn,
    user: models.WebUser = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> WantRecord:
    """会员想看。未映射 → missing_source；已映射 → 真建用户盘转存任务。

    建不了时按原因诚实回 failed，不假装入队：
    - provider 判定不出（本地 / rclone / byos 等形状）→ 暂不支持；
    - 对应 provider 无生效绑定 → 先绑定再点（不建注定失败的单，
      与追剧扫建任务同款纪律）；
    - 转存总闸未开 / 115 无源 fid / Drive 取不到源盘 → 各自明说；
    - 24h 内同归属桶已有任务 → 不重复建，返回已有任务的状态。
    """
    require_store_member(db, user)
    key = body.tmdb_id.strip()
    if not key:
        raise HTTPException(status_code=400, detail="tmdb_id 不能为空")
    row = find_library_item(db, key)
    if row is None:
        return WantRecord(
            tmdb_id=key,
            state="missing_source",
            in_library=False,
            library_item_id=None,
            message=MISSING_SOURCE_MESSAGE,
        )
    provider = tv._item_provider(db, row)
    if provider is None:
        return WantRecord(
            tmdb_id=key,
            state="failed",
            in_library=True,
            library_item_id=row.id,
            message=UNKNOWN_PROVIDER_MESSAGE,
        )
    has_binding = (
        tv.has_active_drive_binding(db, user.id)
        if provider == "drive"
        else tv.has_active_pan115_binding(db, user.id)
    )
    if not has_binding:
        return WantRecord(
            tmdb_id=key,
            state="failed",
            in_library=True,
            library_item_id=row.id,
            message=NO_BINDING_MESSAGE,
        )
    if not tv.master_enabled():
        return WantRecord(
            tmdb_id=key,
            state="failed",
            in_library=True,
            library_item_id=row.id,
            message=TRANSFER_MASTER_OFF_MESSAGE,
        )
    try:
        job = tv.create_user_job(db, row, user.id, provider,
                                 trigger=STORE_WANT_TRIGGER)
    except tv.TransferError as exc:
        if exc.kind == "throttled":
            existing = _latest_user_job(db, row.id, user.id)
            if existing is not None:
                return _record_for_job(
                    existing, key, True, row.id, WANT_ALREADY_QUEUED_MESSAGE)
            return WantRecord(
                tmdb_id=key,
                state="wanted",
                in_library=True,
                library_item_id=row.id,
                message=WANT_ALREADY_QUEUED_MESSAGE,
            )
        message = {
            "master_off": TRANSFER_MASTER_OFF_MESSAGE,
            "no_source_fid": NO_SOURCE_FID_MESSAGE,
            "no_drive": NO_DRIVE_MESSAGE,
        }.get(exc.kind, WANT_FAILED_GENERIC_MESSAGE)
        return WantRecord(
            tmdb_id=key,
            state="failed",
            in_library=True,
            library_item_id=row.id,
            message=message,
        )
    return _record_for_job(job, key, True, row.id, WANT_QUEUED_MESSAGE)


@router.get("/wants", response_model=WantListResponse)
def get_wants(
    user: models.WebUser = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> WantListResponse:
    """会员的想看清单：本人 trigger=store_want 的转存任务行。

    按任务行 status 映射状态；条目行已没的任务跳过（认不出 tmdb，
    不编一条）。最多 100 条。"""
    require_store_member(db, user)
    jobs = (
        db.query(em.TransferJob)
        .filter(
            em.TransferJob.owner_scope == "user",
            em.TransferJob.owner_ref == str(user.id),
            em.TransferJob.trigger == STORE_WANT_TRIGGER,
        )
        .order_by(em.TransferJob.created_at.desc())
        .limit(100)
        .all()
    )
    items: list[WantRecord] = []
    for job in jobs:
        row = (
            db.query(em.MediaItem)
            .filter(em.MediaItem.id == job.item_id)
            .first()
        )
        if row is None or not row.tmdb_id:
            continue
        state = _job_state(job.status)
        items.append(WantRecord(
            tmdb_id=row.tmdb_id,
            state=state,
            in_library=row.deleted_at is None,
            library_item_id=row.id,
            job_id=job.id,
            message=WANT_STATE_MESSAGES[state],
        ))
    return WantListResponse(items=items)
