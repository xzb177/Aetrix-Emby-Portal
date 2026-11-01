"""管理端条目图人工覆盖（媒体库学习 L2）。

写侧不受 ITEM_ARTWORK_OVERRIDE_ENABLED 开关影响（可先配好再开开关）；
读侧是否生效由开关决定（见 item_artwork）。覆盖只接受 http(s) URL。

鉴权：路由级 require_staff，与 library_cover 同口径。
"""

from __future__ import annotations

import json

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy.orm import Session

from backend.api.admin_core import _audit
from backend.database import get_db
from backend.emby_server import cover_mirror, item_artwork, library_covers
from backend.emby_server import models as em
from backend.emby_server.portal import require_staff
from backend.models import SystemConfig

router = APIRouter(
    prefix="/api/admin/media-library",
    tags=["admin-media-library"],
    dependencies=[Depends(require_staff)],
)


class OverrideIn(BaseModel):
    item_id: int
    kind: str
    image_url: str


@router.get("/artwork-overrides")
def list_overrides(
    limit: int = Query(100, ge=1, le=500),
    db: Session = Depends(get_db),
):
    """覆盖列表：只 join 活条目出数（条目已删的孤行不展示、不影响出图）。"""
    rows = (
        db.query(em.MediaItemArtworkOverride, em.MediaItem)
        .join(em.MediaItem, em.MediaItem.id == em.MediaItemArtworkOverride.item_id)
        .filter(em.MediaItem.deleted_at.is_(None), em.MediaItem.merged_into_id.is_(None))
        .order_by(em.MediaItemArtworkOverride.updated_at.desc())
        .limit(limit)
        .all()
    )
    return {
        "items": [
            {
                "id": row.id,
                "item_id": row.item_id,
                "item_name": item.name,
                "item_type": item.item_type,
                "kind": row.kind,
                "image_url": row.image_url,
                "created_at": row.created_at.isoformat() if row.created_at else None,
            }
            for row, item in rows
        ]
    }


@router.post("/artwork-overrides")
def set_override(
    payload: OverrideIn,
    admin=Depends(require_staff),
    db: Session = Depends(get_db),
):
    """新增或更新覆盖：条目不存在 → 404；kind/URL 非法 → 400。"""
    item = db.query(em.MediaItem).filter(em.MediaItem.id == payload.item_id).first()
    if item is None:
        raise HTTPException(status_code=404, detail="条目不存在")
    admin_id = getattr(admin, "id", None)  # 助手层会 commit，先取纯值防过期
    try:
        row = item_artwork.set_override(
            db, payload.item_id, payload.kind, payload.image_url,
            created_by=admin_id,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    if admin_id is not None:
        _audit(db, admin_id, "artwork_override_set", "media_item",
               payload.item_id, {"kind": payload.kind})
        db.commit()
    return {
        "ok": True, "id": row.id, "item_id": row.item_id,
        "kind": row.kind, "image_url": row.image_url,
    }


@router.delete("/artwork-overrides/{item_id}/{kind}")
def delete_override(
    item_id: int,
    kind: str,
    db: Session = Depends(get_db),
    admin=Depends(require_staff),
):
    """取消覆盖：无此行 → 404。"""
    if not item_artwork.cancel_override(db, item_id, kind):
        raise HTTPException(status_code=404, detail="没有这条覆盖")
    admin_id = getattr(admin, "id", None)
    if admin_id is not None:
        _audit(db, admin_id, "artwork_override_delete",
               "media_item", item_id, {"kind": kind})
        db.commit()
    return {"ok": True, "item_id": item_id, "kind": kind}


class CoverStrategyIn(BaseModel):
    order: list[str]


@router.get("/cover-strategy")
def get_cover_strategy(db: Session = Depends(get_db)):
    """库封面策略（管理端图片与顺序页）：生效顺序 + 各来源现状。

    env_order 非空时文件配置优先，管理端保存暂不生效（仍可先保存，
    与条目图覆盖「先配好再开开关」同口径）；borrow_enabled 为文件
    配置开关现状（只读，借代表一步仅在其开启时生效）。
    """
    return {
        "stored_order": library_covers.stored_strategy_order(db),
        "env_order": library_covers.env_strategy_order(),
        "effective_order": library_covers.effective_strategy_order(db),
        "borrow_enabled": library_covers.borrow_enabled(),
        "known_steps": list(library_covers._KNOWN_STEPS),
    }


@router.put("/cover-strategy")
def set_cover_strategy(
    payload: CoverStrategyIn,
    db: Session = Depends(get_db),
    admin=Depends(require_staff),
):
    """保存库封面策略顺序：未知步骤 → 400；空表 → 400（至少保留一步）；
    重复步骤按首次出现去重。存 system_configs，不新增 env 键；出图侧
    下次调用即生效（无缓存）。"""
    steps: list[str] = []
    for entry in payload.order:
        step = str(entry).strip()
        if step not in library_covers._KNOWN_STEPS:
            raise HTTPException(status_code=400, detail=f"未知封面步骤：{step}")
        if step not in steps:
            steps.append(step)
    if not steps:
        raise HTTPException(status_code=400, detail="至少保留一步封面策略")
    row = (
        db.query(SystemConfig)
        .filter(SystemConfig.key == library_covers.COVER_STRATEGY_KEY)
        .first()
    )
    if row is None:
        row = SystemConfig(
            key=library_covers.COVER_STRATEGY_KEY,
            value=json.dumps(steps),
            description="库封面策略顺序（管理端图片与顺序页保存）",
        )
        db.add(row)
    else:
        row.value = json.dumps(steps)
    admin_id = getattr(admin, "id", None)
    if admin_id is not None:
        _audit(db, admin_id, "cover_strategy_save", "system",
               None, {"steps": len(steps), "order": steps})
    db.commit()
    return {
        "ok": True,
        "order": steps,
        "effective_order": library_covers.effective_strategy_order(db),
    }


class CoverMirrorRetryIn(BaseModel):
    source_hash: str


@router.get("/cover-mirrors")
def cover_mirror_stats(db: Session = Depends(get_db)):
    """图床镜像只读可见性：各状态行数 + 镜像目录总字节。"""
    return cover_mirror.mirror_stats(db)


@router.post("/cover-mirrors/retry")
def cover_mirror_retry(
    payload: CoverMirrorRetryIn,
    db: Session = Depends(get_db),
    admin=Depends(require_staff),
):
    """图床镜像手动重推：无行 → 404；failed/dead → pending（计数清零）；
    进行中或已成功 → 409（不动）。"""
    result = cover_mirror.retry_mirror(db, (payload.source_hash or "").strip())
    if result is None:
        raise HTTPException(status_code=404, detail="没有这条镜像")
    if result is False:
        raise HTTPException(status_code=409, detail="该镜像不需重推（进行中或已成功）")
    admin_id = getattr(admin, "id", None)
    if admin_id is not None:
        _audit(db, admin_id, "cover_mirror_retry",
               "cover_mirror", None,
               {"source_hash": (payload.source_hash or "").strip()})
        db.commit()
    return {"ok": True, "source_hash": (payload.source_hash or "").strip()}
