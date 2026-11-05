# -*- coding: utf-8 -*-
"""同剧按季拆行自动折叠（同步后增量治理）。

盘点结论（为什么现有通道折不住按季拆行）：
- 严格通道（merge_versions_worker.find_duplicate_groups，N4-1 后）
  分组键 = (库, 类型, tmdb)：源站每季一个条目、同剧各季行挂不同
  TMDB（如《不良人》83463 与 216820 是 TMDB 自身拆的两个条目）或
  无 TMDB → 异键/无键，永不成组。
- 同剧宽松通道（_find_series_loose_groups）分组键 = normalize_name_loose
  （全名）：该归一化不剥季号后缀，「…第四季」与总剧名异键；其守卫
  「组内 ≥2 个不同 tmdb 整组否决」也拦双编号形态。且其折叠力学是
  版本合并（merge_group 按 (季,集) 配对成副本），不改季号、不建
  季行——季拆行的集全标 Season 1，塞进去只会把第四季的集错并进
  正主第一季。故新建本模块而不是扩宽松通道：分组键、逐行守卫、
  折叠力学三者都不同；复用零件：标题季号解析（applecms_backfill，
  全仓一套）、映射迁移（upstream_item_mappings.migrate_mappings）、
  托管季行 guid 锚（upstream_import.hosted_season_guid）、合并工人
  的配置与一程挂接。

判定口径（照 2026-11-05 手工折叠验证版，自动化从严）：
- 分组键 = 同库 + dedup.normalize_name(底名)，底名由
  applecms_backfill.season_suffix_parts 剥尾部季号/动画版后缀得到；
- 只折明确标记行（第X季/第X部/Season N/Sxx）；纯尾数行只报不折；
- 正主唯一：无标记总剧行优先成池，否则明确季行成池；池内按
  （是否全组众数 tmdb、可见集数）排名，榜首并列 → 整组不折；
- 逐行硬条件（缺一不可，只跳该行）：与正主 TMDB 无冲突（任一为
  空或相等）、与正主年份差 <2、同季子组内年份跨度 <2、被折行集
  的季号分布 ⊆ {1, 空} 或全等于目标季（混装不折）；
- 折叠力学：被折行 merged_into→正主 + 迁映射；其季行并入正主对
  应季（缺季按需新建）；集改挂正主季行并改季号，同 (季,集号)
  撞正主已有可见集 → 后到集转集级合并副本；被折行既有副本集随
  主集改挂；已合并到被折行的 series/season 子行改指正主；正主缺
  TMDB 且全组非空编号唯一 → 补空。
- 幂等：已折行 merged_into 非空后不再入组，第二遍零变更。
"""
import hashlib
import logging
from collections import Counter
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

#: 候选轻量列扫描倍数（与宽松通道 MERGE_CANDIDATE_MULTIPLIER 同口径）。
_CANDIDATE_MULTIPLIER = 10


def _episode_counts(db, series_ids: List[int]) -> Dict[int, int]:
    from backend.emby_server import models as em

    if not series_ids:
        return {}
    from sqlalchemy import func

    rows = (
        db.query(em.MediaItem.series_id, func.count(em.MediaItem.id))
        .filter(
            em.MediaItem.item_type == "episode",
            em.MediaItem.series_id.in_(series_ids),
            em.MediaItem.deleted_at.is_(None),
            em.MediaItem.merged_into_id.is_(None),
        )
        .group_by(em.MediaItem.series_id)
        .all()
    )
    return {sid: int(cnt) for sid, cnt in rows}


def _episode_season_values(db, series_id: int):
    from backend.emby_server import models as em

    rows = (
        db.query(em.MediaItem.season_number)
        .filter(
            em.MediaItem.item_type == "episode",
            em.MediaItem.series_id == series_id,
            em.MediaItem.deleted_at.is_(None),
            em.MediaItem.merged_into_id.is_(None),
        )
        .distinct()
        .all()
    )
    return {r[0] for r in rows}


def _elect_primary(rows, parts, ep_counts):
    """正主选举：返回唯一正主行，并列/无池返回 None（整组不折）。

    池 = 无标记行（如有），否则明确标记行；纯尾数行永不入池。
    排名键 = (是否携带全组众数 tmdb, 可见集数)，须榜首唯一。
    """
    unmarked = [r for r in rows if parts[r.id][1] is None]
    explicit = [r for r in rows if parts[r.id][1] == "explicit"]
    pool = unmarked if unmarked else explicit
    if not pool:
        return None
    if len(pool) == 1:
        return pool[0]
    counts = Counter(
        (r.tmdb_id or "").strip() for r in rows if (r.tmdb_id or "").strip()
    )
    modal = None
    if counts:
        top = counts.most_common()
        if len(top) == 1 or top[0][1] > top[1][1]:
            modal = top[0][0]

    def rank(r):
        return (
            1 if modal and (r.tmdb_id or "").strip() == modal else 0,
            ep_counts.get(r.id, 0),
        )

    best = max(rank(r) for r in pool)
    winners = [r for r in pool if rank(r) == best]
    return winners[0] if len(winners) == 1 else None


def _row_foldable(db, row, primary, season_no: int, info: Dict[str, int]) -> bool:
    """逐行硬条件：与正主 TMDB/年份冲突、集季号混装 → 不折该行。"""
    rt = (row.tmdb_id or "").strip()
    pt = (primary.tmdb_id or "").strip()
    if rt and pt and rt != pt:
        info["skip_tmdb_conflict"] += 1
        return False
    if (
        row.production_year
        and primary.production_year
        and abs(int(row.production_year) - int(primary.production_year)) >= 2
    ):
        info["skip_year_conflict"] += 1
        return False
    seasons = _episode_season_values(db, row.id)
    if seasons <= {1, None} or seasons == {season_no}:
        return True
    info["skip_mixed_seasons"] += 1
    return False


def find_season_fold_plan(db, cfg) -> Tuple[List[dict], Dict[str, int]]:
    """分组 + 选主 + 逐行判定。返回 (plans, info)。

    plan = {"primary_id", "folds": [(row_id, 目标季号)], "group_ids"}。
    """
    from backend.emby_server import applecms_backfill as _bf
    from backend.emby_server import dedup as dedup_lib
    from backend.emby_server import models as em

    info: Dict[str, int] = Counter()
    plans: List[dict] = []
    if not cfg.get("enabled", True) or not cfg.get("season_fold_enabled", True):
        return plans, dict(info)
    limit = int(cfg.get("batch_limit", 1000))
    light_rows = (
        db.query(
            em.MediaItem.id,
            em.MediaItem.library_id,
            em.MediaItem.name,
            em.MediaItem.production_year,
            em.MediaItem.tmdb_id,
        )
        .filter(
            em.MediaItem.item_type == "series",
            em.MediaItem.deleted_at.is_(None),
            em.MediaItem.merged_into_id.is_(None),
        )
        .order_by(em.MediaItem.id.asc())
        .limit(max(limit * _CANDIDATE_MULTIPLIER, 1))
        .all()
    )
    parts: Dict[int, Tuple[Optional[int], Optional[str]]] = {}
    groups: Dict[Tuple[int, str], list] = {}
    for row in light_rows:
        base, season, marker = _bf.season_suffix_parts(row.name)
        key_name = dedup_lib.normalize_name(base)
        if not key_name:
            continue
        parts[row.id] = (season, marker)
        groups.setdefault((row.library_id, key_name), []).append(row)
    ep_counts = _episode_counts(db, list(parts.keys()))
    for _key, rows in groups.items():
        if len(plans) >= limit:
            break
        if len(rows) < 2:
            continue
        explicit = [r for r in rows if parts[r.id][1] == "explicit"]
        if not explicit:
            continue
        info["groups_seen"] += 1
        primary = _elect_primary(rows, parts, ep_counts)
        if primary is None:
            info["groups_no_primary"] += 1
            logger.info(
                "按季折叠跳过（正主多候选）: 库 %s 组 %s",
                _key[0], [r.id for r in rows],
            )
            continue
        # 同季年份冲突预判：同目标季的待折行年份跨度 ≥2 → 该季全跳。
        by_season: Dict[int, list] = {}
        for r in explicit:
            if r.id != primary.id:
                by_season.setdefault(parts[r.id][0], []).append(r)
        vetoed = set()
        for _n, rs in by_season.items():
            years = [r.production_year for r in rs if r.production_year]
            if len(years) >= 2 and max(years) - min(years) >= 2:
                vetoed.update(r.id for r in rs)
                info["skip_same_season_year"] += len(rs)
        folds = []
        for r in explicit:
            if r.id == primary.id or r.id in vetoed:
                continue
            if _row_foldable(db, r, primary, parts[r.id][0], info):
                folds.append((r.id, parts[r.id][0]))
        if folds:
            plans.append(
                {
                    "primary_id": primary.id,
                    "folds": folds,
                    "group_ids": [r.id for r in rows],
                }
            )
    return plans, dict(info)


def _primary_episode_index(db, primary_id: int) -> Dict[Tuple, int]:
    from backend.emby_server import models as em

    rows = (
        db.query(
            em.MediaItem.id, em.MediaItem.season_number, em.MediaItem.episode_number
        )
        .filter(
            em.MediaItem.item_type == "episode",
            em.MediaItem.series_id == primary_id,
            em.MediaItem.deleted_at.is_(None),
            em.MediaItem.merged_into_id.is_(None),
            em.MediaItem.episode_number.isnot(None),
        )
        .all()
    )
    return {(r.season_number, r.episode_number): r.id for r in rows}


def _ensure_target_season(db, primary, season_no: int, stats: Dict[str, int]):
    """正主对应季行：已有可见季行直接用；缺失时新建（懒建）。"""
    from backend.emby_server import models as em

    row = (
        db.query(em.MediaItem)
        .filter(
            em.MediaItem.item_type == "season",
            em.MediaItem.series_id == primary.id,
            em.MediaItem.season_number == season_no,
            em.MediaItem.deleted_at.is_(None),
            em.MediaItem.merged_into_id.is_(None),
        )
        .order_by(em.MediaItem.id.asc())
        .first()
    )
    if row is not None:
        return row
    if primary.upstream_source_id:
        # 托管正主：用托管季行 guid 锚，与进库侧幂等键一致。
        from backend.emby_server.upstream_import import hosted_season_guid

        guid = hosted_season_guid(primary.upstream_source_id, primary.id, season_no)
    else:
        guid = hashlib.md5(
            f"seasonfold:{primary.id}:{season_no}".encode("utf-8")
        ).hexdigest()
    existing = (
        db.query(em.MediaItem).filter(em.MediaItem.guid == guid).first()
    )
    if existing is not None:
        if (
            existing.series_id == primary.id
            and existing.season_number == season_no
            and existing.deleted_at is None
            and existing.merged_into_id is None
        ):
            return existing
        logger.warning(
            "按季折叠季行 guid 锚不一致，跳过建季: primary=%s season=%s guid=%s",
            primary.id, season_no, guid,
        )
        return None
    from datetime import datetime

    row = em.MediaItem(
        guid=guid,
        library_id=primary.library_id,
        item_type="season",
        name=f"第 {season_no} 季",
        parent_id=primary.id,
        series_id=primary.id,
        season_number=season_no,
        upstream_source_id=primary.upstream_source_id,
        probe_status="done",
        date_added=datetime.now(),
    )
    db.add(row)
    db.flush()
    stats["seasons_created"] += 1
    return row


def _fold_row(db, primary, row, season_no: int, stats: Dict[str, int],
              ep_index: Dict[Tuple, int]) -> bool:
    """把一行季拆行折进正主。返回是否完成折叠。"""
    from backend.emby_server import models as em
    from backend.emby_server import upstream_item_mappings as _uim

    MI = em.MediaItem
    season_rows = (
        db.query(MI)
        .filter(
            MI.item_type == "season",
            MI.series_id == row.id,
            MI.deleted_at.is_(None),
        )
        .all()
    )
    visible_eps = (
        db.query(MI)
        .filter(
            MI.item_type == "episode",
            MI.series_id == row.id,
            MI.deleted_at.is_(None),
            MI.merged_into_id.is_(None),
        )
        .order_by(MI.id.asc())
        .all()
    )
    target_season = None
    if season_rows or visible_eps:
        target_season = _ensure_target_season(db, primary, season_no, stats)
        if target_season is None:
            return False
    if target_season is not None:
        for sr in season_rows:
            if sr.merged_into_id is None:
                sr.merged_into_id = target_season.id
                _uim.migrate_mappings(db, sr.id, target_season.id)
                stats["seasons_merged"] += 1
            else:
                tgt = db.query(MI).filter(MI.id == sr.merged_into_id).first()
                if tgt is None or tgt.series_id == row.id:
                    sr.merged_into_id = target_season.id
                    stats["seasons_merged"] += 1
        moved: Dict[int, int] = {}
        for ep in visible_eps:
            ep.parent_id = target_season.id
            ep.series_id = primary.id
            ep.season_number = season_no
            winner = None
            if ep.episode_number is not None:
                winner = ep_index.get((season_no, ep.episode_number))
            if winner is not None and winner != ep.id:
                ep.merged_into_id = winner
                _uim.migrate_mappings(db, ep.id, winner)
                moved[ep.id] = winner
                stats["episodes_copied"] += 1
            else:
                if ep.episode_number is not None:
                    ep_index[(season_no, ep.episode_number)] = ep.id
                moved[ep.id] = ep.id
                stats["episodes_moved"] += 1
        # 被折行既有副本集：随主集一跳改挂（目标被撞成副本时跟随新指向）。
        copies = (
            db.query(MI)
            .filter(
                MI.item_type == "episode",
                MI.series_id == row.id,
                MI.deleted_at.is_(None),
                MI.merged_into_id.isnot(None),
            )
            .all()
        )
        for cp in copies:
            if cp.merged_into_id in moved:
                cp.parent_id = target_season.id
                cp.series_id = primary.id
                cp.season_number = season_no
                cp.merged_into_id = moved[cp.merged_into_id]
                stats["copies_rebased"] += 1
    # 此前已合并到被折行的 series/season 子行改指正主。
    children = (
        db.query(MI)
        .filter(
            MI.merged_into_id == row.id,
            MI.deleted_at.is_(None),
            MI.item_type.in_(("series", "season")),
        )
        .all()
    )
    for ch in children:
        ch.merged_into_id = primary.id
        stats["children_repointed"] += 1
    row.merged_into_id = primary.id
    row.enrich_status = "done"
    _uim.migrate_mappings(db, row.id, primary.id)
    stats["folded"] += 1
    return True


def _maybe_fill_tmdb(db, plan: dict, primary, stats: Dict[str, int]) -> None:
    """正主缺 TMDB 且全组非空编号唯一 → 补空（只填空、不覆盖）。"""
    if (primary.tmdb_id or "").strip():
        return
    from backend.emby_server import models as em

    rows = (
        db.query(em.MediaItem.tmdb_id)
        .filter(em.MediaItem.id.in_(plan["group_ids"]))
        .all()
    )
    distinct = {(r[0] or "").strip() for r in rows} - {""}
    if len(distinct) == 1:
        primary.tmdb_id = next(iter(distinct))
        stats["tmdb_filled"] += 1


def fold_season_groups(db, cfg) -> Dict[str, int]:
    """执行一轮按季折叠（逐组提交，单组失败只回滚该组并记日志）。

    返回 stats：groups/folded/episodes_moved/episodes_copied/
    copies_rebased/seasons_created/seasons_merged/children_repointed/
    tmdb_filled/libraries + 判定 info（groups_seen/skip_* 等）。
    """
    from backend.emby_server import models as em

    stats: Dict[str, int] = Counter({
        "groups": 0, "folded": 0, "episodes_moved": 0, "episodes_copied": 0,
        "copies_rebased": 0, "seasons_created": 0, "seasons_merged": 0,
        "children_repointed": 0, "tmdb_filled": 0,
    })
    plans, info = find_season_fold_plan(db, cfg)
    stats.update(info)
    touched_libs = set()
    for plan in plans:
        try:
            primary = (
                db.query(em.MediaItem)
                .filter(em.MediaItem.id == plan["primary_id"])
                .first()
            )
            if primary is None or primary.merged_into_id is not None:
                continue
            ep_index = _primary_episode_index(db, primary.id)
            folded_here = 0
            for row_id, season_no in plan["folds"]:
                row = (
                    db.query(em.MediaItem)
                    .filter(em.MediaItem.id == row_id)
                    .first()
                )
                if row is None or row.merged_into_id is not None:
                    continue
                if _fold_row(db, primary, row, season_no, stats, ep_index):
                    folded_here += 1
            if folded_here:
                _maybe_fill_tmdb(db, plan, primary, stats)
                touched_libs.add(primary.library_id)
                stats["groups"] += 1
            db.commit()
        except Exception as exc:  # noqa: BLE001 — 单组失败不拖垮整轮
            db.rollback()
            logger.warning(
                "按季折叠组失败 primary=%s: %s", plan.get("primary_id"), exc
            )
    stats["libraries"] = sorted(touched_libs)  # type: ignore[assignment]
    return dict(stats)
