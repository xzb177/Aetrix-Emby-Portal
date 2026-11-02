# -*- coding: utf-8 -*-
"""苹果 CMS 集行结构（新媒体库类型一期·集行单，只预留结构）。

一期只灌电影、剧集库不开：``applecms_library_sync.sync_library``
对 tvshows 库仍返回 ``blocked=unsupported_collection``。本模块把
二期灌剧集要用的行形约定先钉死并桩测通过，放行时同步工人直接
接线，不许到时另起一套形状（设计稿集行方案）：

- 剧行：``item_type="series"``，guid=``hosted_guid(源, vod_id)``，
  与电影行同一托管口径（upstream_source_id=主来源、
  probe_status=done、enrich_status=pending 交 enrich 补身份）。
- 合成单季行：CMS 没有季概念，一部剧合成一张季行
  （``season_number=1``、名「第 1 季」、guid=
  ``hosted_season_guid``），多季条目在 CMS 侧本就混排在一部
  vod 里，这是已知局限，集序号即直链组内顺序。
- 集行：``item_type="episode"``，一集一行，guid 由
  ``hosted_guid(源, "<vod_id>#<集序号>")`` 派生（一个 vod_id
  覆盖全剧，集靠序号区分）；``parent_id``=季行、
  ``series_id``=剧行、``season_number=1``、
  ``episode_number``=组内 1 起序号。
- 集标题入库、播放地址绝不落库：detail 只用来数集与取集名
  （直链组口径与取流口 ``resolve_play`` 完全一致——用
  ``parse_play_groups`` + ``pick_direct_group`` 选组），地址
  点播时现取。
- 集映射带 ``episode_index``（与电影映射的 NULL 分开），
  mapped 取流口按 (行， 集序号） 正是靠这条映射定位 vod。
"""

from __future__ import annotations

import logging
from typing import Dict, List

from sqlalchemy.orm import Session

from backend.emby_server import applecms_fetcher as acf
from backend.emby_server import models as em
from backend.emby_server import upstream_import as uimp
from backend.emby_server import upstream_item_mappings as uim
from backend.emby_server.applecms_library_sync import (
    CMS_ENRICH_PRIORITY, CMS_MAPPING_CONFIDENCE, CMS_MAPPING_EVIDENCE,
)
from backend.emby_server.pinyin_sort import make_sort_name

logger = logging.getLogger(__name__)

#: 合成季号：CMS 无季概念，全剧合成第 1 季。
SYNTHETIC_SEASON_NUMBER = 1


def episode_row_guid(source_id: int, vod_id: str,
                     episode_index: int) -> str:
    """集行 guid：一个 vod_id 覆盖全剧，集靠组内序号区分。"""
    return uimp.hosted_guid(source_id, f"{vod_id}#{episode_index}")


def plan_episodes(detail: dict) -> List[Dict[str, object]]:
    """从 detail 的直链组产出集计划 [{index, title}]（不含地址）。

    选组口径与取流口一致：``parse_play_groups`` 后取首个全直链
    组；没有直链组返回空表（解析型线路一期不建行，播放时也选
    不中，建了也是死行）。集名缺失时回落「第N集」。
    """
    if not isinstance(detail, dict):
        return []
    groups = acf.parse_play_groups(
        detail.get("vod_play_from"), detail.get("vod_play_url"))
    direct = acf.pick_direct_group(groups)
    if not direct:
        return []
    plan: List[Dict[str, object]] = []
    for idx, episode in enumerate(direct.get("episodes") or [], start=1):
        title = str(episode.get("title") or "").strip()
        plan.append({"index": idx, "title": title or f"第{idx}集"})
    return plan


def _get_or_create(db: Session, guid: str, factory) -> tuple:
    row = uimp._usable_row_by_guid(db, guid)
    if row is not None:
        return row, False
    row = factory()
    db.add(row)
    db.flush()
    return row, True


def ensure_series_tree(db: Session, library, source, record,
                       detail: dict) -> Dict[str, int]:
    """建/复用一部剧的托管树：剧行＋合成季行＋集行＋集映射。

    ``record`` 是 ``applecms_sync.cms_entry_to_record`` 的产出
    （name / production_year 已清洗）。幂等：重跑只补缺失的集，
    已有行原样复用。调用方负责 commit 与映射开关前置门。
    """
    stats = {"series_created": 0, "season_created": 0,
             "episodes_added": 0, "episodes_existing": 0, "mapped": 0}
    vod_id = str(detail.get("vod_id") or "").strip()
    series_row, created = _get_or_create(
        db, uimp.hosted_guid(source.id, vod_id),
        lambda: em.MediaItem(
            guid=uimp.hosted_guid(source.id, vod_id),
            library_id=library.id, item_type="series",
            name=record.name, sort_name=make_sort_name(record.name),
            production_year=record.production_year,
            upstream_source_id=source.id, probe_status="done",
            enrich_priority=CMS_ENRICH_PRIORITY))
    if created:
        stats["series_created"] = 1
    season_row = (
        db.query(em.MediaItem)
        .filter(em.MediaItem.item_type == "season",
                em.MediaItem.series_id == series_row.id,
                em.MediaItem.season_number == SYNTHETIC_SEASON_NUMBER,
                em.MediaItem.merged_into_id.is_(None))
        .order_by(em.MediaItem.id)
        .first())
    if season_row is None:
        season_row, created = _get_or_create(
            db, uimp.hosted_season_guid(
                source.id, series_row.id, SYNTHETIC_SEASON_NUMBER),
            lambda: em.MediaItem(
                guid=uimp.hosted_season_guid(
                    source.id, series_row.id, SYNTHETIC_SEASON_NUMBER),
                library_id=library.id, item_type="season",
                name=f"第 {SYNTHETIC_SEASON_NUMBER} 季",
                parent_id=series_row.id, series_id=series_row.id,
                season_number=SYNTHETIC_SEASON_NUMBER,
                upstream_source_id=source.id, probe_status="done",
                enrich_priority=CMS_ENRICH_PRIORITY))
        if created:
            stats["season_created"] = 1
    for entry in plan_episodes(detail):
        index = int(entry["index"])
        guid = episode_row_guid(source.id, vod_id, index)
        episode_row, created = _get_or_create(
            db, guid,
            lambda index=index, title=str(entry["title"]): em.MediaItem(
                guid=guid, library_id=library.id, item_type="episode",
                name=title, sort_name=make_sort_name(title),
                parent_id=season_row.id, series_id=series_row.id,
                season_number=SYNTHETIC_SEASON_NUMBER,
                episode_number=index,
                upstream_source_id=source.id, probe_status="done",
                enrich_priority=CMS_ENRICH_PRIORITY))
        if created:
            stats["episodes_added"] += 1
        else:
            stats["episodes_existing"] += 1
        uim.record_mapping(
            db, local_item_id=episode_row.id, source_id=source.id,
            upstream_item_id=vod_id,
            confidence=CMS_MAPPING_CONFIDENCE,
            evidence=CMS_MAPPING_EVIDENCE, episode_index=index)
        stats["mapped"] += 1
    return stats
