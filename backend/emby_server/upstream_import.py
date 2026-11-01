# -*- coding: utf-8 -*-
"""上游独有资源进库（一期：电影先行，2026-11-01 用户按推荐方案拍板）。

聚合配对之后，上游有、本地没有的作品此前只在对账计数里露面
（identity_no_local_match），用户在站里看不见、播不了。本模块把
其中「未配对 + Type=Movie + 带 tmdb_id」的独有电影落成本地行：

- 落点：按上游库名找本地**同名**库（只认非虚拟库，同名多个取 id
  最小）；找不到同名库本期跳过并计数——自动建库是二期；
- 建行：guid 按「源 id + 上游 Item Id」md5 确定性派生（32 位 hex，
  与 MediaItem.guid 列宽对齐），tmdb_id 落行，sort_name 走
  pinyin_sort.make_sort_name（与 scanner 同一构造点），打上游托管
  标记 MediaItem.upstream_source_id=源 id，probe_status 直接置
  done（无本地文件可探，probe 侧 needs 判定本就跳过无路径行，
  这里把状态写明），enrich 保持默认 pending——托管行无 file_path，
  enrich 的 TMDB-only 路径已兜住（见 enrich_worker._enrich_fetch_pre
  的托管分支），图片/简介走 TMDB 回填，绝不直用上游图（上游图片
  地址带对方 api_key，直用等于外泄凭据）；
- 同时写 upstream_item_mappings（local_item_id 指向新行）：播放三环
  （详情线路 / PlaybackInfo / 取流口）全靠映射寻址，没有映射的行
  播不了——所以映射写开关（UPSTREAM_ITEM_MAPPING_ENABLED）关时
  **不建行**，只计数（blocked=mapping_off），绝不建孤儿行；
- 幂等：guid 已存在即视为已进库（计数 existing，映射走
  record_mapping 的 upsert 刷新），重复跑不重复建行；
- limit：内部口（上线试点用），只限**新建**行数，调度路径不传
  = 不限量；已存在的行不占限额（试点重跑不被旧行挤掉名额）。

前置门（任一不过整批不建行，stats["blocked"] 记原因）：
- 源进库开关 import_enabled 关 → "switch_off"；
- 映射写开关关 → "mapping_off"。

候选口径与对账的 identity_no_local_match 等价：聚合作品任一线路
（本源）出现在本窗配对结果里即视为已配对，整部跳过——同一作品
在同源有多个条目时，第一条未配对线路建行，其余线路的映射一并
挂到这一行（同作品多线路与配对侧多版本同理）。

本模块不做（二期及以后）：剧/季/集进库、已配对剧缺集补齐、
生命周期对账（上游下架跟随）、自动建库、无身份条目进库。
"""

from __future__ import annotations

import hashlib
import logging
from typing import Dict, Iterable, Optional, Set

from sqlalchemy.orm import Session

from backend.emby_server import models as em
from backend.emby_server import upstream_item_mappings as uim
from backend.emby_server.pinyin_sort import make_sort_name

logger = logging.getLogger(__name__)

#: 进库映射的置信度/证据：身份来自上游条目自带的 Tmdb ProviderId，
#: 与配对侧 tmdb 精确命中同档（CONFIDENCE_TMDB=1.0、evidence="tmdb"）。
IMPORT_CONFIDENCE = 1.0
IMPORT_EVIDENCE = "tmdb"


def hosted_guid(source_id: int, upstream_item_id: str) -> str:
    """托管行 guid：md5("upstream:{source_id}:{upstream_item_id}")。

    确定性派生是幂等的根：同一上游条目无论跑多少轮都算出同一个
    guid，存在性检查即可去重，不需要额外登记表。
    """
    return hashlib.md5(
        f"upstream:{source_id}:{upstream_item_id}".encode("utf-8")
    ).hexdigest()


def _resolve_target_library(db: Session, upstream_library_name: str):
    """按上游库名找本地同名库：只认非虚拟库，同名多个取 id 最小。

    与 upstream_aggregate_run.resolve_target_library 的筛法同口径
    （那边是备注分类对位，这边是进库落点，两处都只认真实库）。
    """
    name = (upstream_library_name or "").strip()
    if not name:
        return None
    rows = (
        db.query(em.Library)
        .filter(em.Library.name == name)
        .order_by(em.Library.id.asc())
        .all()
    )
    for row in rows:
        if not row.is_virtual:  # NULL 与 False 同口径：非虚拟
            return row
    return None


def _zero_stats() -> Dict[str, object]:
    return {
        "candidates": 0, "imported": 0, "existing": 0,
        "no_library": 0, "limited": 0, "blocked": None,
    }


def import_unpaired_movies(db: Session, source, items: Iterable,
                           paired_upstream_ids: Set[str], *,
                           upstream_library_name: str,
                           limit: Optional[int] = None) -> Dict[str, object]:
    """把一窗聚合作品里的未配对独有电影进库，返回统计 dict。

    只 flush 不 commit（调用方事务，与聚合写映射同口径）。
    ``paired_upstream_ids`` 是本窗配对命中的上游条目 id 集合。
    统计键：candidates（候选作品数）/ imported（新建行）/ existing
    （guid 已存在）/ no_library（无同名库跳过）/ limited（超限额
    跳过）/ blocked（前置门原因或 None）。
    """
    stats = _zero_stats()
    if not getattr(source, "import_enabled", True):
        stats["blocked"] = "switch_off"
        return stats
    if not uim.mapping_enabled():
        # 没映射的行播不了：绝不建孤儿行。
        stats["blocked"] = "mapping_off"
        return stats
    paired = set(paired_upstream_ids or ())
    target_library = None
    target_resolved = False
    for item in items or ():
        if (getattr(item, "item_type", "") or "") != "Movie":
            continue
        tmdb_id = (getattr(item, "tmdb_id", None) or "").strip()
        if not tmdb_id:
            continue
        source_lines = [
            line for line in (getattr(item, "lines", None) or [])
            if str(getattr(line, "source_id", "")) == str(source.id)
        ]
        if not source_lines:
            continue
        if any(line.upstream_item_id in paired for line in source_lines):
            continue  # 任一线路已配对 = 作品已有本地行，整部跳过
        stats["candidates"] += 1
        if not target_resolved:
            target_library = _resolve_target_library(
                db, upstream_library_name)
            target_resolved = True
        if target_library is None:
            stats["no_library"] += 1
            continue
        local_row = None
        for line in source_lines:
            guid = hosted_guid(source.id, line.upstream_item_id)
            row = (db.query(em.MediaItem)
                   .filter(em.MediaItem.guid == guid).first())
            if row is None:
                if limit is not None and stats["imported"] >= limit:
                    stats["limited"] += 1
                    break
                row = em.MediaItem(
                    guid=guid,
                    library_id=target_library.id,
                    item_type="movie",
                    name=item.name,
                    sort_name=make_sort_name(item.name),
                    production_year=getattr(item, "production_year", None),
                    tmdb_id=tmdb_id,
                    imdb_id=(getattr(item, "imdb_id", None) or None),
                    upstream_source_id=source.id,
                    probe_status="done",
                )
                db.add(row)
                db.flush()
                stats["imported"] += 1
                logger.info(
                    "上游独有电影进库 source_id=%s upstream_item_id=%s "
                    "local_item_id=%s library_id=%s name=%r",
                    source.id, line.upstream_item_id, row.id,
                    target_library.id, item.name)
            elif local_row is None:
                stats["existing"] += 1
            local_row = local_row or row
            # 同作品多线路共用一行：映射逐条挂到同一本地行。
            uim.record_mapping(
                db, local_item_id=local_row.id, source_id=source.id,
                upstream_item_id=line.upstream_item_id,
                confidence=IMPORT_CONFIDENCE, evidence=IMPORT_EVIDENCE)
    return stats
