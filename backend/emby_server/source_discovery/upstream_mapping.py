"""内置适配器：上游聚合映射源（唯一内置的真实源）。

只用已授权的上游数据：查 ``upstream_item_mappings``（聚合已落库的
本地条目→上游条目配对），且只取来源行 ``enabled=True`` 的映射。
来源行缺失的死映射直接跳过（不展示、不报错、不猜来源名）。

匹配口径（保守，宁缺毋滥）：
- tmdb_id / imdb_id：与本地条目精确相等（索引命中）。
- 标题：归一化（去多余空白、casefold）后与本地条目的 name 或
  original_title 精确相等；不做模糊、不做子串，避免张冠李戴。
- 本地条目只算未下架行（``deleted_at IS NULL``），与商店 R0 同口径。
- 候选的清晰度 / 格式 / 大小 / 字幕信息只取本地条目已有字段，
  没有的填 None；``playable_now`` 恒为 False（须确认转存后才可播）。
"""

from __future__ import annotations

from sqlalchemy import func
from sqlalchemy.orm import Session

from backend.emby_server import models as em
from backend.emby_server.source_discovery.types import Candidate, SearchQuery

NAME = "upstream_mapping"

MAX_ITEMS = 200
MAX_CANDIDATES = 200


def _normalize(text: str) -> str:
    return " ".join((text or "").split()).casefold()


class UpstreamMappingAdapter:
    """上游聚合映射适配器。"""

    name = NAME

    def search(self, db: Session, query: SearchQuery) -> list[Candidate]:
        items = self._resolve_items(db, query)
        if not items:
            return []
        item_ids = [item.id for item in items]
        rows = (
            db.query(em.UpstreamItemMapping, em.UpstreamSource)
            .join(
                em.UpstreamSource,
                em.UpstreamSource.id == em.UpstreamItemMapping.source_id,
            )
            .filter(
                em.UpstreamItemMapping.local_item_id.in_(item_ids),
                em.UpstreamSource.enabled.is_(True),
            )
            .all()
        )
        by_id = {item.id: item for item in items}
        candidates: list[Candidate] = []
        for mapping, source in rows:
            item = by_id.get(mapping.local_item_id)
            if item is None:
                continue
            size = getattr(item, "size", None)
            candidates.append(
                Candidate(
                    adapter=NAME,
                    local_item_id=item.id,
                    local_title=item.name or "",
                    local_year=getattr(item, "production_year", None),
                    local_type=getattr(item, "item_type", "") or "",
                    tmdb_id=getattr(item, "tmdb_id", None) or None,
                    imdb_id=getattr(item, "imdb_id", None) or None,
                    source_id=source.id,
                    source_name=source.name or "",
                    upstream_item_id=mapping.upstream_item_id,
                    confidence=float(mapping.confidence or 0.0),
                    evidence=mapping.evidence or "",
                    resolution=getattr(item, "video_resolution", None) or None,
                    container=getattr(item, "container", None) or None,
                    media_source=getattr(item, "media_source", None) or None,
                    size_bytes=int(size) if size else None,
                    subtitle_languages=(
                        getattr(item, "subtitle_languages", None) or None
                    ),
                )
            )
        candidates.sort(
            key=lambda c: (-c.confidence, c.local_item_id, c.source_id or 0)
        )
        return candidates[:MAX_CANDIDATES]

    def _resolve_items(self, db: Session, query: SearchQuery) -> list:
        """按查询键解析本地可见条目（只读，不建不改）。"""
        tmdb_key = (query.tmdb_id or "").strip()
        if tmdb_key:
            return (
                db.query(em.MediaItem)
                .filter(
                    em.MediaItem.tmdb_id == tmdb_key,
                    em.MediaItem.deleted_at.is_(None),
                )
                .order_by(em.MediaItem.id)
                .limit(MAX_ITEMS)
                .all()
            )
        imdb_key = (query.imdb_id or "").strip()
        if imdb_key:
            return (
                db.query(em.MediaItem)
                .filter(
                    em.MediaItem.imdb_id == imdb_key,
                    em.MediaItem.deleted_at.is_(None),
                )
                .order_by(em.MediaItem.id)
                .limit(MAX_ITEMS)
                .all()
            )
        title = query.normalized_title()
        if title:
            return (
                db.query(em.MediaItem)
                .filter(
                    em.MediaItem.deleted_at.is_(None),
                )
                .filter(
                    (func.lower(em.MediaItem.name) == title)
                    | (func.lower(em.MediaItem.original_title) == title)
                )
                .order_by(em.MediaItem.id)
                .limit(MAX_ITEMS)
                .all()
            )
        return []
