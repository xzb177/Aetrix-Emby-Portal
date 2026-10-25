"""管理端库设序（媒体库学习 L4）。

Library.sort_order：0 = 未设序。Views 只在任一库 sort_order 非零时
才排序，否则查询逐字不变（见 api.user_views）。已设序的在前（数值
越小越靠前），未设序的排在已设之后按 id 自然序；批量 PUT 一次写
全量顺序（管理端拖拽后整单保存）。

鉴权：路由级 require_staff，与 library_cover 同口径。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from backend.database import get_db
from backend.emby_server import models as em
from backend.emby_server.portal import require_staff

router = APIRouter(
    prefix="/api/admin/emby/libraries",
    tags=["admin-library-order"],
    dependencies=[Depends(require_staff)],
)


class OrderItem(BaseModel):
    library_id: int
    sort_order: int


class OrderIn(BaseModel):
    orders: list[OrderItem]


@router.get("/order")
def get_library_order(db: Session = Depends(get_db)):
    """当前库顺序（供管理端拖拽列表回显）。

    与 api.user_views 的 Views 顺序同一口径：已设序的在前（按
    sort_order 升序），未设序（0）的排在已设之后、按 id 自然序。
    """
    libs = db.query(em.Library).all()
    libs = sorted(
        libs,
        key=lambda lib: (0, lib.sort_order, lib.id) if (lib.sort_order or 0) != 0
        else (1, 0, lib.id),
    )
    return {
        "items": [
            {"library_id": lib.id, "name": lib.name, "sort_order": lib.sort_order or 0}
            for lib in libs
        ]
    }


@router.put("/order")
def set_library_order(
    payload: OrderIn,
    db: Session = Depends(get_db),
):
    """批量设序：orders 里出现未知库 → 400（整单不写）。"""
    ids = [o.library_id for o in payload.orders]
    known = {
        lib.id
        for lib in db.query(em.Library).filter(em.Library.id.in_(ids)).all()
    } if ids else set()
    unknown = [lid for lid in ids if lid not in known]
    if unknown:
        raise HTTPException(status_code=400, detail=f"未知媒体库：{unknown}")
    for order in payload.orders:
        db.query(em.Library).filter(em.Library.id == order.library_id).update(
            {"sort_order": order.sort_order}
        )
    db.commit()
    return {"ok": True, "updated": len(payload.orders)}
