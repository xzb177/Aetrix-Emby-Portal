"""管理端条目图人工覆盖（媒体库学习 L2）。

写侧不受 ITEM_ARTWORK_OVERRIDE_ENABLED 开关影响（可先配好再开开关）；
读侧是否生效由开关决定（见 item_artwork）。覆盖只接受 http(s) URL。

鉴权：路由级 require_staff，与 library_cover 同口径。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy.orm import Session

from backend.database import get_db
from backend.emby_server import item_artwork
from backend.emby_server import models as em
from backend.emby_server.portal import require_staff

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
    try:
        row = item_artwork.set_override(
            db, payload.item_id, payload.kind, payload.image_url,
            created_by=getattr(admin, "id", None),
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    return {
        "ok": True, "id": row.id, "item_id": row.item_id,
        "kind": row.kind, "image_url": row.image_url,
    }


@router.delete("/artwork-overrides/{item_id}/{kind}")
def delete_override(
    item_id: int,
    kind: str,
    db: Session = Depends(get_db),
):
    """取消覆盖：无此行 → 404。"""
    if not item_artwork.cancel_override(db, item_id, kind):
        raise HTTPException(status_code=404, detail="没有这条覆盖")
    return {"ok": True, "item_id": item_id, "kind": kind}
