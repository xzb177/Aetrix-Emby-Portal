"""条目别名独立表（FMBY 媒体库学习 L8：只学机制，不抄表名/字段名）。

魂：别名是识别与搜索的共同证据源，不是搜索框的附庸。

现状与分工（只增不改）：
- 存量 ``MediaItem.aliases`` 列 + trigram 索引 + 搜索命中**原样不动**：
  本模块不改该列、不改搜索代码、不改索引；
- 新表 ``emby_media_aliases``（条目 + 别名 + 来源 + 置信）是增量证据源；
- 存量列数据由 :func:`migrate_aliases_from_column` 幂等迁入
  （source=legacy_column，可重跑、重跑零新增）；
- 识别候选生成时（enrich_worker._alias_tmdb_id 的零 API 本地别名索引）
  开关开时同时读本表，列别名优先、表别名不覆盖已有映射（先到先得，
  与既有索引语义一致）。

总开关：env ``MEDIA_ALIAS_TABLE_ENABLED``（默认 0=关），模块加载时
读一次（与 IDENTITY_SHELF_GATE 同口径，改需重启）。关 = 别名表
不被任何读路径使用，行为与现行逐字一致。管理后台不另立开关。
"""
from __future__ import annotations

import logging

from backend.emby_server import models as em
from backend.emby_server.env_util import env_int

logger = logging.getLogger(__name__)

#: 别名独立表总开关（默认关）。启动时读一次，热路径不碰 env。
MEDIA_ALIAS_TABLE_ENABLED = env_int("MEDIA_ALIAS_TABLE_ENABLED", 0, 0, 1)


def alias_table_enabled() -> bool:
    """别名表是否启用（全仓读开关的唯一入口；测试 patch 本模块常量）。"""
    return MEDIA_ALIAS_TABLE_ENABLED > 0


def alias_candidates(db, item_id=None, name: str = ""):
    """识别候选生成用的别名证据：

    - 开关关 → []（现行行为逐字一致，别名表不被使用）；
    - 开关开 → 该条目的别名行（置信倒序、别名正序），或按名字反查
      （name 命中别名时返回其条目与别名，供识别候选比对）。
    返回 [{item_id, alias, source, confidence}]，不含 MediaItem 列别名
    （列别名仍走既有列路径，两边互不覆盖）。
    """
    if not alias_table_enabled():
        return []
    q = db.query(em.MediaAlias)
    if item_id is not None:
        q = q.filter(em.MediaAlias.item_id == item_id)
    elif name:
        q = q.filter(em.MediaAlias.alias == name.strip())
    else:
        return []
    rows = (q.order_by(em.MediaAlias.confidence.desc(),
                       em.MediaAlias.alias.asc()).all())
    return [{"item_id": r.item_id, "alias": r.alias, "source": r.source,
             "confidence": r.confidence} for r in rows]


def migrate_aliases_from_column(db) -> dict:
    """存量 MediaItem.aliases 列数据幂等迁入别名表。

    - 列值按逗号切分、strip、去空、单条目内去重；
    - 已存在 (item_id, alias, source=legacy_column) 的行跳过；
    - **只 INSERT，不 UPDATE / DELETE 任何行**，不改 aliases 列本身；
    - 可重跑：第二次跑新增 0。
    返回 {"scanned_items": n, "inserted": n, "skipped_existing": n}。
    """
    items = (db.query(em.MediaItem.id, em.MediaItem.aliases)
             .filter(em.MediaItem.aliases.isnot(None))
             .filter(em.MediaItem.aliases != "").all())
    inserted = 0
    skipped = 0
    scanned = 0
    for item_id, raw in items:
        aliases = [a.strip() for a in (raw or "").split(",")]
        aliases = list(dict.fromkeys(a for a in aliases if a))
        if not aliases:
            continue
        scanned += 1
        existing = {r[0] for r in db.query(em.MediaAlias.alias).filter(
            em.MediaAlias.item_id == item_id,
            em.MediaAlias.source == "legacy_column").all()}
        for alias in aliases:
            if alias in existing:
                skipped += 1
                continue
            db.add(em.MediaAlias(item_id=item_id, alias=alias,
                                 source="legacy_column", confidence=1.0))
            inserted += 1
    db.flush()
    return {"scanned_items": scanned, "inserted": inserted,
            "skipped_existing": skipped}
