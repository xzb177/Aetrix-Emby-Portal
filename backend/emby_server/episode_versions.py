# -*- coding: utf-8 -*-
"""单集多版本（对标 Emby 多版本逻辑）。

同一 ``(library_id, series_id, season_number, episode_number)`` 下的多个文件
= 同一集的多个版本（如 2160p / 1080p / H265 等不同压制版本）。

数据模型（复用 ``merged_into_id``，与电影/剧集多版本同一套机制）：
- 主记录：``merged_into_id IS NULL``，在剧集列表中展示
- 其他版本：``merged_into_id`` 指向主记录，通过
  ``/emby/Items/{id}/AlternateVersions`` 查看/选择播放哪个版本

与 ``merge_versions_worker`` 的关系：
- 那个模块负责电影/剧集级（按 tmdb_id 分组），本模块负责单集级
  （按 series/season/episode 分组）
- 共用 ``merged_into_id`` 列、``AlternateVersions`` 接口、``dedup`` 过滤、
  各 worker 的 merged 跳过口径

调用点：
- 扫描器：``_scan_library_body`` 结束时对本库做一次合并（增量：只处理
  有 2+ 可见单集的组，SAVEPOINT 包裹，失败不影响扫描结果）
- 管理后台：``POST /api/admin/media/episode-versions/merge`` 手动触发
  （用于存量 5291 组多版本的回填，或扫描之外的主动整理）
"""

from __future__ import annotations

import logging
import os
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


def _env_flag(name: str, default: str = "1") -> bool:
    return (os.getenv(name, default) or default).strip().lower() not in (
        "0", "false", "no", "off",
    )


# 总开关：默认开。一切可配置（用户铁律：能力可配，不写死业务逻辑）。
EPISODE_VERSIONS_ENABLED = _env_flag("EPISODE_VERSIONS_ENABLED", "1")


def version_signature(file_path: Optional[str]) -> Tuple[Optional[str], Optional[str], Optional[str]]:
    """从文件名提取版本签名（分辨率， 发行来源， 视频编码）。

    纯字符串解析，零 IO（``filename_meta`` 设计原则），可在热路径调用。
    签名用于：
    1. 改名识别指纹的版本区分（同集不同版本不再互认为改名）
    2. 主版本选择的分辨率排序（无探测数据时的回退）
    """
    if not file_path:
        return (None, None, None)
    try:
        from backend.emby_server import filename_meta as _fm
        m = _fm.parse_filename(file_path)
        return (m.get("resolution"), m.get("source"), m.get("video_codec"))
    except Exception:  # noqa: BLE001 — 解析失败视为无签名，不阻断主流程
        return (None, None, None)


def _resolution_height(item: Any) -> int:
    """条目的有效高度：探测值优先，文件名解析回退。"""
    h = getattr(item, "height", None) or 0
    if h:
        return int(h)
    try:
        from backend.emby_server import filename_meta as _fm
        res = _fm.parse_filename(getattr(item, "file_path", "") or "").get("resolution")
        if res:
            return int(_fm.resolution_to_wh(res)[1])
    except Exception:  # noqa: BLE001
        pass
    return 0


def choose_primary(items: List[Any]) -> Any:
    """从同组版本里确定性地选主记录。

    规则：分辨率（高度）越高越优先 → 文件越大越优先 → id 越小越优先。
    高度用探测值，缺失时用文件名解析回退（零 IO）。
    播放默认走主记录 = 默认播最高画质版本，符合用户直觉。
    """
    def _key(it: Any) -> Tuple[int, int, int]:
        return (_resolution_height(it), int(getattr(it, "size", 0) or 0),
                -int(getattr(it, "id", 0) or 0))

    return max(items, key=_key)


def _group_key(item: Any) -> Optional[Tuple[int, int, int, int]]:
    series_id = getattr(item, "series_id", None)
    season = getattr(item, "season_number", None)
    episode = getattr(item, "episode_number", None)
    library_id = getattr(item, "library_id", None)
    if series_id is None or season is None or episode is None or library_id is None:
        return None
    return (int(library_id), int(series_id), int(season), int(episode))


def merge_episode_versions(db, library_id: Optional[int] = None) -> Dict[str, int]:
    """合并单集多版本。

    对每个 ``(library_id, series_id, season_number, episode_number)`` 组：
    - 可见行（``deleted_at IS NULL``）中选主记录，其余 ``merged_into_id`` 指向主记录
    - 主记录被删后：孤儿回收先解散合并，下一轮重新选主（最终一致，至多多一轮；
      详见 having 条件的注释）
    - 组内只剩一行且被合并过（孤儿）：解除合并，恢复独立

    幂等：重复调用结果不变。只写 ``merged_into_id``，不动其他字段。

    返回 ``{"groups": 组数, "merged": 合并的版本数, "promoted": 晋升数}``。
    """
    from sqlalchemy import case as _case
    from sqlalchemy import func as _func
    from backend.emby_server import models as em

    stats = {"groups": 0, "merged": 0, "promoted": 0, "unmerged_orphans": 0}
    if not EPISODE_VERSIONS_ENABLED:
        return stats

    MI = em.MediaItem
    q = (
        db.query(
            MI.library_id, MI.series_id, MI.season_number, MI.episode_number,
            _func.count(MI.id).label("cnt"),
        )
        .filter(
            MI.item_type == "episode",
            MI.deleted_at.is_(None),
            MI.series_id.isnot(None),
            MI.season_number.isnot(None),
            MI.episode_number.isnot(None),
        )
    )
    if library_id is not None:
        q = q.filter(MI.library_id == library_id)
    groups = (
        q.group_by(MI.library_id, MI.series_id, MI.season_number, MI.episode_number)
        # 只处理还有 ≥2 个未合并行的组：已收敛的组（仅主记录未合并）直接跳过，
        # 避免每轮对全部存量组做 N+1 查询。count(case…) 只计
        # merged_into_id IS NULL 的行（count 跳过 NULL）。主记录被删时
        # survivors 都是已合并行，本轮由下面的孤儿回收解散，
        # 下一轮再以未合并身份重新合并（最终一致）。
        .having(_func.count(_case((MI.merged_into_id.is_(None), 1))) > 1)
        .all()
    )
    for lib_id, series_id, season_number, episode_number, _cnt in groups:
        rows = (
            db.query(MI)
            .filter(
                MI.item_type == "episode",
                MI.deleted_at.is_(None),
                MI.library_id == lib_id,
                MI.series_id == series_id,
                MI.season_number == season_number,
                MI.episode_number == episode_number,
            )
        ).all()
        if len(rows) < 2:
            continue
        stats["groups"] += 1
        primary = choose_primary(rows)
        for r in rows:
            if r.id == primary.id:
                if r.merged_into_id is not None:
                    # 晋升：原主记录已删（或被合并），本行接任主记录
                    r.merged_into_id = None
                    stats["promoted"] += 1
                    logger.info(
                        "单集多版本晋升主记录：S%02dE%02d -> 条目 %s（%s）",
                        season_number, episode_number, r.id, r.file_path,
                    )
            else:
                if r.merged_into_id != primary.id:
                    r.merged_into_id = primary.id
                    stats["merged"] += 1
        # 只打前 3 个组的明细：or 会让 merged<=10 的条件在回填时长期为真，
        # 数千条 INFO 刷屏；and 保证"组数≤3 且合并数≤10"才打
        if stats["groups"] <= 3 and stats["merged"] <= 10:
            logger.info(
                "单集多版本合并：series=%s S%02dE%02d，主记录=%s，共 %d 个版本",
                series_id, season_number, episode_number, primary.id, len(rows),
            )

    # 孤儿回收：merged_into_id 指向已删除/已合并行的，解除合并恢复独立
    # （主记录被物理删除或自身又被合并的极端情况）
    orphans = (
        db.query(MI)
        .filter(
            MI.item_type == "episode",
            MI.deleted_at.is_(None),
            MI.merged_into_id.isnot(None),
        )
    )
    if library_id is not None:
        orphans = orphans.filter(MI.library_id == library_id)
    orphan_rows = orphans.all()
    if orphan_rows:
        primaries = (
            db.query(MI.id)
            .filter(
                MI.id.in_([r.merged_into_id for r in orphan_rows]),
                MI.deleted_at.is_(None),
                MI.merged_into_id.is_(None),
            )
            .all()
        )
        alive = {row[0] for row in primaries}
        for r in orphan_rows:
            if r.merged_into_id not in alive:
                r.merged_into_id = None
                stats["unmerged_orphans"] += 1
                logger.info("单集多版本孤儿恢复独立：条目 %s（%s）", r.id, r.file_path)

    if stats["groups"] or stats["unmerged_orphans"]:
        logger.info("单集多版本合并完成：%s", stats)
    return stats


def get_version_group(db, item: Any) -> List[Any]:
    """取某单集所属的版本组（含主记录自己），按主记录优先、id 排序。"""
    key = _group_key(item)
    if key is None:
        return [item]
    from backend.emby_server import models as em

    library_id, series_id, season, episode = key
    rows = (
        db.query(em.MediaItem)
        .filter(
            em.MediaItem.item_type == "episode",
            em.MediaItem.deleted_at.is_(None),
            em.MediaItem.library_id == library_id,
            em.MediaItem.series_id == series_id,
            em.MediaItem.season_number == season,
            em.MediaItem.episode_number == episode,
        )
        .order_by(em.MediaItem.id.asc())
        .all()
    )
    return rows or [item]
