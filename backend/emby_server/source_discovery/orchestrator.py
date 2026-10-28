"""编排层：汇总多适配器、去重、排序；确认时复核候选真实性。

- 搜索本身只读，绝不建转存任务（自动模式也不例外）。
- 去重键：(适配器, 本地条目 id, 来源 id, 上游条目 id)。
- 排序：置信度降序 → 适配器配置顺序 → 本地条目 id。
- 确认复核：按三元组重查映射与来源行、本地条目可见性，
  对不上的一律视为候选不存在（不接受前端自编的候选）。
"""

from __future__ import annotations

import logging

from sqlalchemy.orm import Session

from backend.emby_server import models as em
from backend.emby_server.source_discovery import adapters, config
from backend.emby_server.source_discovery.types import Candidate, SearchQuery

logger = logging.getLogger(__name__)


def search_candidates(
    db: Session, query: SearchQuery
) -> tuple[list[Candidate], list[str]]:
    """跑全部生效适配器，返回（候选，去重后实际用到的适配器名）。

    日志只记数量与适配器名，不记查询文本与标题。
    """
    names = config.enabled_adapters()
    order = {name: index for index, name in enumerate(names)}
    merged: dict[tuple, Candidate] = {}
    used: list[str] = []
    for name in names:
        adapter = adapters.get_adapter(name)
        if adapter is None:
            continue
        try:
            found = adapter.search(db, query)
        except Exception:
            # 单适配器失败不毁整次搜索：记日志、继续下一个。
            logger.exception("找源适配器失败 adapter=%s", name)
            continue
        if found:
            used.append(name)
        for candidate in found:
            merged.setdefault(candidate.dedupe_key(), candidate)
    candidates = sorted(
        merged.values(),
        key=lambda c: (
            -c.confidence,
            order.get(c.adapter, 1 << 30),
            c.local_item_id,
        ),
    )
    logger.info(
        "找源搜索完成 adapters=%s candidates=%d",
        ",".join(used),
        len(candidates),
    )
    return candidates, used


def resolve_candidate(
    db: Session,
    local_item_id: int,
    source_id: int,
    upstream_item_id: str,
) -> tuple | None:
    """复核候选三元组：映射在、来源行在且 enabled、本地条目未下架。

    任一对不上返回 None（调用方按“候选不存在”诚实回 404）。
    """
    mapping = (
        db.query(em.UpstreamItemMapping)
        .filter(
            em.UpstreamItemMapping.local_item_id == local_item_id,
            em.UpstreamItemMapping.source_id == source_id,
            em.UpstreamItemMapping.upstream_item_id == upstream_item_id,
        )
        .first()
    )
    if mapping is None:
        return None
    source = (
        db.query(em.UpstreamSource)
        .filter(
            em.UpstreamSource.id == source_id,
            em.UpstreamSource.enabled.is_(True),
        )
        .first()
    )
    if source is None:
        return None
    item = (
        db.query(em.MediaItem)
        .filter(
            em.MediaItem.id == local_item_id,
            em.MediaItem.deleted_at.is_(None),
        )
        .first()
    )
    if item is None:
        return None
    return (item, mapping, source)
