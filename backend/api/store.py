"""想看商店骨架路由。

总闸 ``STORE_ENABLED`` 默认关。关时本组接口惰性 404（路由级依赖先于鉴权，
未登录也不会先露 401）。开时：

- ``GET /catalog``、``GET /items/{tmdb_id}`` 访客只读（目录是空列表；
  详情只回 tmdb_id 与库内是否已有条目，不拉 TMDB）。
- ``POST /want``、``GET /wants`` 要登录，并且要有效会员订阅
  （``subscriptions.has_active_subscription``）。
- 想看的 R0 判定：``tmdb_id`` 对得上未下架的 ``emby_items`` 才算库内。
  对不上返回 ``missing_source``。对得上返回 501，不调用转存入队。

播放、多账号池、预热、R1 自动求片、R2 索引都不在本文件。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from backend import models, subscriptions
from backend.api.user import get_current_user
from backend.database import get_db
from backend.emby_server import models as em
from backend.emby_server.store_contract import (
    MISSING_SOURCE_MESSAGE,
    STORE_DISABLED_DETAIL,
    STORE_MEMBERSHIP_REQUIRED_DETAIL,
    TRANSFER_NOT_WIRED_DETAIL,
    CatalogResponse,
    ItemResponse,
    WantIn,
    WantListResponse,
    WantRecord,
    store_enabled,
)

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

    下架行不算。优先电影 / 剧集主条，没有再退到任意未下架行
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
    """会员想看。未映射 → missing_source；已映射 → 501，不入队。"""
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
    # 故意不调用 transfer_variant.create_user_job。大实现另排期。
    raise HTTPException(status_code=501, detail=TRANSFER_NOT_WIRED_DETAIL)


@router.get("/wants", response_model=WantListResponse)
def get_wants(
    user: models.WebUser = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> WantListResponse:
    """会员的想看清单。骨架不落库，固定空列表。"""
    require_store_member(db, user)
    return WantListResponse(items=[])
