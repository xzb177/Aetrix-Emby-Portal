"""EA 搜索接口（相关度排序版）

为什么单独成模块：`api.py` 已经接近 2000 行，且历史上 `/Search/Hints` 的实现
只是 `LIKE + limit`，返回顺序由数据库决定。这里用与 `/Items?SearchTerm=` **同一套**
相关度排序（`search.rank_items`）提供搜索建议：

    标题完全匹配 > 标题前缀 > 词边界前缀 > 标题包含 > 别名 > 分类元数据 > 模糊

并把中英文、繁简体与多别名的候选一起送进预筛（`search.search_variants`）。

**注册顺序很重要**：本模块的路由必须在 `emby_router` 之前 `include_router`，
否则 `api.py` 里遗留的同名实现会先匹配到（FastAPI 按注册顺序取第一个匹配）。
`backend/main.py`（EM）与 `emby_api/main.py`（EA）都已按此顺序挂载。
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from sqlalchemy import or_
from sqlalchemy.orm import Session

from backend import models
from backend.database import get_db
from backend.emby_server import models as em
from backend.emby_server.api import _base_url, _emby_type, _image_url
from backend import library_scope
from backend.emby_server.auth import get_emby_user
from backend.emby_server.search import (
    CANDIDATE_LIMIT as SEARCH_CANDIDATE_LIMIT,
    rank_items,
    search_variants,
)

search_router = APIRouter(tags=["搜索"])


def _search_clauses(term: str) -> list:
    """SQL 预筛：标题族 + 别名族，并把繁简/去发布标签的变体一起放进来

    预筛只负责“别漏”，精排交给 rank_items。
    """
    clauses = []
    for variant in search_variants(term):
        like = f"%{variant}%"
        clauses.extend([
            em.MediaItem.name.ilike(like),
            em.MediaItem.original_title.ilike(like),
            em.MediaItem.sort_name.ilike(like),
            em.MediaItem.aliases.ilike(like),
        ])
    return clauses


@search_router.get("/emby/Search/Hints")
@search_router.get("/Search/Hints")
def search_hints(request: Request, user: models.WebUser = Depends(get_emby_user),
                       db: Session = Depends(get_db)):
    """Emby 搜索建议（客户端输入时的下拉/联想）"""
    q = request.query_params
    term = (q.get("SearchTerm") or "").strip()
    try:
        limit = max(1, min(int(q.get("Limit") or 20), 200))
    except ValueError:
        limit = 20
    include = [t.strip().lower() for t in (q.get("IncludeItemTypes") or "").split(",") if t.strip()]
    base = _base_url(request)

    query = db.query(em.MediaItem).filter(em.MediaItem.is_hidden == False)  # noqa: E712
    # 单集多版本：被合并的版本不在搜索建议里重复出现（与 get_episodes 同口径；
    # 这里没有 dedup 层，不过滤就会把同一集的多个版本都列出来）。
    # 只管 episode：电影/剧集的多版本在搜索建议里的行为保持历史口径，不动。
    query = query.filter(
        or_(em.MediaItem.item_type != "episode",
            em.MediaItem.merged_into_id.is_(None))
    )
    # 媒体库可见范围：搜索建议是独立于 /Users/{id}/Items 的另一个出口，
    # 不挂上同一份范围，被藏起来的库照样能从搜索框里被搜出来。
    query = library_scope.scope_query(query, library_scope.effective_ids_safe(db, user))
    if term:
        clauses = _search_clauses(term)
        if clauses:
            query = query.filter(or_(*clauses))
    if include:
        type_map = {"movie": "movie", "series": "series", "episode": "episode", "season": "season"}
        query = query.filter(em.MediaItem.item_type.in_([type_map.get(t, t) for t in include]))

    rows = query.order_by(em.MediaItem.sort_name.asc()).limit(SEARCH_CANDIDATE_LIMIT).all()
    ordered = rank_items(rows, term) if term else rows

    hints = [
        {
            "ItemId": it.guid,
            "Id": it.guid,
            "Name": it.name,
            "Type": _emby_type(it.item_type),
            "MediaType": "Video" if it.item_type in ("movie", "episode") else None,
            "ProductionYear": it.production_year,
            "RunTimeTicks": it.duration_ticks or None,
            "IndexNumber": it.episode_number,
            "ParentIndexNumber": it.season_number,
            "SeriesId": it.series.guid if it.series else None,
            "PrimaryImageTag": "1" if _image_url(base, it) else None,
        }
        for it in ordered[:limit]
    ]
    return {"SearchHints": hints, "TotalRecordCount": len(hints)}
