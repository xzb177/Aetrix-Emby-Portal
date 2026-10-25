"""门户端媒体库新口（媒体库学习 L1 / L5 / L6 / L9）。

- L1：GET /libraries/{id}/cover/{kind} 库封面五件套（poster / banner /
  backdrop / thumb / logo；logo 恒 null）。开关关时只回旧行为
  （cover_path 或 null），不 404——旧口径有值可回。
- L5：GET /libraries/{id}/items 游标分页（sort=title|year，next_cursor）。
  开关关时 404（新口，无旧口可保）；开时读投影、投影不可用回落现算。
- L6：GET /home/sections 首页分区（注册表驱动）。开关关时 404；
  开时按注册表顺序供数：continue_watching 复用门户继续观看供数
  （与 ContinueWatch 同形），recently_updated 按入库时间倒序。
  recently_updated 是货架面：身份门开时只出已绑定条目（案 A）。
- L9：POST /resume/{guid}/hide、/unhide 与 GET /resume/hidden。
  藏是按用户自己的观看记录（UserMediaData.hide_from_resume）置位；
  前端对剧集卡传单集 guid（播的是那集），对电影卡传电影 guid。

鉴权：门户 JWT 用户（get_current_user），与门户其它用户口同口径。
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import Response
from sqlalchemy.orm import Session

from backend.api.user import get_current_user
from backend.database import get_db
from backend.emby_server import models as em
from backend.emby_server import item_artwork, item_sort_projection, library_covers
from backend.emby_server.env_util import env_int
from backend.emby_server.identity_gate import gate_enabled, identity_clause

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/user/media-library", tags=["media-library"])

SECTIONS_ENABLED = env_int("HOME_SECTIONS_PROJECTION_ENABLED", 0, 0, 1)

_COVER_KINDS = ("poster", "banner", "backdrop", "thumb", "logo")

# 分区种子：key / 标题 / 供数来源说明 / 顺序。ensure_sections 只补缺行，
# 已有行（管理员改过标题/顺序/开关）不动。
_SEED_SECTIONS = (
    ("continue_watching", "继续观看", "resume", 0),
    ("recently_updated", "最近更新", "recently_added", 1),
)

_SVG_PLACEHOLDER = (
    '<svg xmlns="http://www.w3.org/2000/svg" width="800" height="450"'
    ' viewBox="0 0 800 450"><rect width="800" height="450" fill="#e8e4dc"/>'
    '<g fill="none" stroke="#9a948a" stroke-width="6">'
    '<rect x="310" y="150" width="180" height="150" rx="12"/>'
    '<circle cx="400" cy="225" r="42"/></g>'
    '<text x="400" y="350" font-family="sans-serif" font-size="28"'
    ' fill="#8a847a" text-anchor="middle">暂无封面</text></svg>'
).encode("utf-8")


def sections_enabled() -> bool:
    """首页分区口是否开启（测试 patch 本模块常量）。"""
    return SECTIONS_ENABLED == 1


def ensure_sections(db) -> list:
    """幂等补齐分区种子行（只在开关开着的读取路径调用，不写迁移）。"""
    existing = {row.key for row in db.query(em.HomeSection).all()}
    added = False
    for key, title, provider, position in _SEED_SECTIONS:
        if key not in existing:
            db.add(em.HomeSection(
                key=key, title=title, provider=provider,
                position=position, enabled=True,
            ))
            added = True
    if added:
        db.commit()
    return (
        db.query(em.HomeSection)
        .filter(em.HomeSection.enabled == True)  # noqa: E712
        .order_by(em.HomeSection.position.asc(), em.HomeSection.id.asc())
        .all()
    )


def portal_poster_url(item, db) -> str | None:
    """门户列表项海报地址：覆盖 / 自有图 → Images 路由（路由内按
    _image_chain 出图，覆盖经同一链路）；无图 → None。

    只对 movie / series 顶层条目用（无父级回退）；季的回退口径在
    api.season_poster_url，不在这里重复。
    """
    if item_artwork.override_enabled():
        if item_artwork.override_url(db, item.id, "Primary"):
            return f"/emby/Items/{item.guid}/Images/Primary"
    if getattr(item, "poster_path", None) or getattr(item, "primary_image_url", None):
        return f"/emby/Items/{item.guid}/Images/Primary"
    return None


def _serialize_cursor_item(item, db) -> dict:
    return {
        "id": item.id,
        "guid": item.guid,
        "name": item.name,
        "item_type": item.item_type,
        "production_year": item.production_year,
        "poster_url": portal_poster_url(item, db),
        "date_added": item.date_added.isoformat() if getattr(item, "date_added", None) else None,
    }


def _recently_updated_items(db, limit: int = 20) -> list[dict]:
    """最近更新：启用库的 movie / series，按入库时间倒序。

    货架面：身份门开时挂 identity_clause（案 A：只出已绑定条目）。
    """
    q = (
        db.query(em.MediaItem)
        .join(em.Library, em.Library.id == em.MediaItem.library_id)
        .filter(
            em.Library.is_enabled == True,  # noqa: E712
            em.MediaItem.item_type.in_(("movie", "series")),
            em.MediaItem.merged_into_id.is_(None),
        )
    )
    if gate_enabled():
        q = q.filter(identity_clause())
    rows = (
        q.order_by(em.MediaItem.date_added.desc().nullslast(), em.MediaItem.id.desc())
        .limit(limit)
        .all()
    )
    return [_serialize_cursor_item(item, db) for item in rows]


@router.get("/libraries/{library_id}/items")
def list_library_items_cursor(
    library_id: int,
    sort: str = Query("title"),
    cursor: str | None = Query(None),
    limit: int = Query(50, ge=1, le=100),
    user=Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """库内条目游标分页（L5）。开关关 → 404；游标非法 → 400。"""
    if not item_sort_projection.enabled():
        raise HTTPException(status_code=404, detail="游标分页未开启")
    lib = db.query(em.Library).filter(em.Library.id == library_id).first()
    if lib is None or not bool(getattr(lib, "is_enabled", False)):
        raise HTTPException(status_code=404, detail="媒体库不存在或未启用")
    if sort not in ("title", "year"):
        raise HTTPException(status_code=400, detail="sort 只接受 title / year")
    cursor_key, cursor_id = None, None
    if cursor:
        try:
            cursor_sort, cursor_key, cursor_id = item_sort_projection.decode_cursor(cursor)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e)) from e
        if cursor_sort != sort:
            raise HTTPException(status_code=400, detail="游标与 sort 不匹配")
    items, next_cursor = item_sort_projection.load_page(
        db, library_id, sort, cursor_key, cursor_id, limit,
    )
    return {
        "items": [_serialize_cursor_item(item, db) for item in items],
        "next_cursor": next_cursor,
    }


@router.get("/libraries/{library_id}/cover/{kind}")
def library_cover_kind(
    library_id: int,
    kind: str,
    user=Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """库封面五件套之一（L1）。kind 非法 → 400；库不存在 → 404。"""
    if kind not in _COVER_KINDS:
        raise HTTPException(status_code=400, detail="kind 只接受 poster / banner / backdrop / thumb / logo")
    lib = db.query(em.Library).filter(em.Library.id == library_id).first()
    if lib is None:
        raise HTTPException(status_code=404, detail="媒体库不存在")
    return library_covers.resolve_library_cover(db, lib, kind)


@router.get("/libraries/{library_id}/cover-placeholder.svg")
def library_cover_placeholder(
    library_id: int,
    user=Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """SVG 占位图（L1 策略的 svg 一步）：静态字节，不含库名，可缓存。"""
    lib = db.query(em.Library).filter(em.Library.id == library_id).first()
    if lib is None:
        raise HTTPException(status_code=404, detail="媒体库不存在")
    return Response(
        content=_SVG_PLACEHOLDER,
        media_type="image/svg+xml",
        headers={"Cache-Control": "public, max-age=86400"},
    )


@router.get("/home/sections")
def home_sections(
    user=Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """首页分区（L6）：按注册表顺序供数；开关关 → 404。"""
    if not sections_enabled():
        raise HTTPException(status_code=404, detail="首页分区未开启")
    sections = ensure_sections(db)
    # continue_watching 供数复用门户继续观看（与 ContinueWatch 同形）：
    # portal.py 体量大，延迟 import，避免本模块 import 时拖入扫描/挂载链。
    from backend.emby_server.portal import get_resume_list

    out = []
    for section in sections:
        if section.key == "continue_watching":
            items = get_resume_list(request_user=user, db=db, limit=12)["items"]
        elif section.key == "recently_updated":
            items = _recently_updated_items(db, limit=20)
        else:
            items = []
        out.append({
            "key": section.key,
            "title": section.title,
            "position": section.position,
            "items": items,
        })
    return {"sections": out}


def _resolve_resume_item(db, item_guid: str):
    """由 guid 找条目（藏/取消藏的键：剧集卡传单集 guid，电影卡传电影 guid）。"""
    item = db.query(em.MediaItem).filter(em.MediaItem.guid == item_guid).first()
    if item is None:
        raise HTTPException(status_code=404, detail="条目不存在")
    return item


@router.post("/resume/{item_guid}/hide")
def hide_resume_item(
    item_guid: str,
    user=Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """藏起一条继续观看（L9）：只改本人的 UserMediaData 行。"""
    item = _resolve_resume_item(db, item_guid)
    umd = (
        db.query(em.UserMediaData)
        .filter(em.UserMediaData.user_id == user.id, em.UserMediaData.item_id == item.id)
        .first()
    )
    if umd is None:
        raise HTTPException(status_code=404, detail="没有这条观看记录")
    umd.hide_from_resume = True
    db.commit()
    return {"ok": True, "item_guid": item_guid, "hidden": True}


@router.post("/resume/{item_guid}/unhide")
def unhide_resume_item(
    item_guid: str,
    user=Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """取消藏（L9）。"""
    item = _resolve_resume_item(db, item_guid)
    umd = (
        db.query(em.UserMediaData)
        .filter(em.UserMediaData.user_id == user.id, em.UserMediaData.item_id == item.id)
        .first()
    )
    if umd is None:
        raise HTTPException(status_code=404, detail="没有这条观看记录")
    umd.hide_from_resume = False
    db.commit()
    return {"ok": True, "item_guid": item_guid, "hidden": False}


@router.get("/resume/hidden")
def hidden_resume_items(
    user=Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """已藏的继续观看清单（L9）：剧集行显示剧名 + 集号，取消藏的键
    是 item_guid（单集 guid / 电影 guid，与 hide 入参一致）。"""
    rows = (
        db.query(em.UserMediaData, em.MediaItem)
        .join(em.MediaItem, em.MediaItem.id == em.UserMediaData.item_id)
        .filter(
            em.UserMediaData.user_id == user.id,
            em.UserMediaData.hide_from_resume == True,  # noqa: E712
            em.MediaItem.merged_into_id.is_(None),
            em.MediaItem.deleted_at.is_(None),
        )
        .order_by(em.UserMediaData.last_played_at.desc().nullslast())
        .limit(100)
        .all()
    )
    series_ids = {item.series_id for _umd, item in rows if item.series_id}
    series_map = {}
    if series_ids:
        series_map = {
            s.id: s
            for s in db.query(em.MediaItem)
            .filter(em.MediaItem.id.in_(series_ids), em.MediaItem.deleted_at.is_(None))
            .all()
        }
    out = []
    for umd, item in rows:
        series = series_map.get(item.series_id) if item.series_id else None
        display = series if (item.item_type == "episode" and series is not None) else item
        out.append({
            "item_guid": item.guid,
            "display_name": display.name,
            "display_type": display.item_type,
            "series_name": series.name if series else None,
            "season_number": item.season_number,
            "episode_number": item.episode_number,
            "poster_url": portal_poster_url(display, db),
            "last_played_at": umd.last_played_at.isoformat() if umd.last_played_at else None,
        })
    return {"items": out}
