"""无可用线路条目自动隐藏对账（清理一期，2026-11-05 用户亲定）。

问题：上游源被停用后，它独占的条目还挂在货架上能看不能播（详情
下发不出任何线路、播放器连播放按钮都不给）。用户定版口径：这类
条目先藏起来（可逆），以后有线了自动回来。

判定口径（与 parent 2026-11-05 生产实数同口径，逐字对齐）：
- 对象：无界库（``emby_libraries.kind='applecms'``，生产即 58–66）
  的顶层活行——``parent_id IS NULL``、``deleted_at IS NULL``、
  ``merged_into_id IS NULL``；
- 子树：自身＋子行（季）＋孙行（集）三层，与实数 SQL 一致；
- 「不能播」：子树里 ``upstream_item_mappings`` ≥1 条，且没有任何
  一条指向 ``upstream_sources.enabled`` 为真的源；
- 片内单条死线（同片另有活线）不满足「没有任何一条」，不藏；
  完全无映射的条目不在本机制范围（那是身份/存量口径，另有治理）。

隐藏手段是既有 ``is_hidden``；「因本机制藏的」靠台账
``emby_item_hidden_ledger``（reason=``no_enabled_line``）与墓碑 A′、
进库开关联动、管理员手工隐藏区分。对账两条纪律：
- 藏：只藏当前可见且命中口径的条目并记台账；已经藏着的条目（无论
  谁藏的）一律不 claimed、不补台账；
- 解藏：只动台账在册（restored_at 为空）且子树已有启用线的条目；
  台账外 is_hidden 永不解藏。条目已被软删/合并的，台账行留着不
  处理（活行口径跳过，见模型 docstring）。

挂接时机：
- 苹果 CMS 库同步每轮收尾（``run_library_sync_round`` 末尾，独立
  会话、异常不许打断同步轮）跑一次全量对账——停用源的连带隐藏
  与新映射补线后的解藏都靠它兜底，最长延迟一个同步间隔
  （默认 ``APPLECMS_LIBRARY_SYNC_INTERVAL_SEC``=21600s）；
- 源被重新启用（``upstream_sources.enable_source``）时定向解藏
  挂该源的在册条目（fail-open，不挡启用本身），不用等同步轮。

后台红线：对账只在同步收尾的既有节奏里跑；写操作按批提交
（默认每批 1000 条），不进请求主链路。

清理二期（2026-11-05 同日续）：源还开着、但整部片的线经探活确认
全死（dead/gone）的条目也藏，台账 reason=``all_lines_dead`` 与一期
严格分开（见模块后半段）。探活数据来自 2026-11-04/05 单线全量探活
（``results_single.jsonl``，判定路径＝应用取流
``resolve_mapped_upstream_stream``、分类口径与探活脚本 probe.py
逐字一致）。落藏不信旧数据：候选每条线先按同一路径新鲜复检，
仍全死才藏；解藏靠有界轮转复检（每轮至多 500 条在册条目、同步
收尾挂接带最小间隔节流），不许每轮全量复检几万条。
"""

from __future__ import annotations

import logging
import os
import time
from datetime import datetime
from typing import Callable, Dict, Iterable, List, Mapping, Optional, Sequence, Set, Tuple

from sqlalchemy import bindparam, text
from sqlalchemy.orm import Session

from backend.emby_server import models as em

logger = logging.getLogger(__name__)

REASON_NO_ENABLED_LINE = "no_enabled_line"
REASON_ALL_LINES_DEAD = "all_lines_dead"

_BATCH_SIZE = 1000

_EN_CTE = (
    "en AS (SELECT DISTINCT m.local_item_id AS iid "
    "FROM upstream_item_mappings m "
    "JOIN upstream_sources s ON s.id = m.source_id WHERE s.enabled)"
)
_ANY_CTE = (
    "any_map AS (SELECT DISTINCT local_item_id AS iid "
    "FROM upstream_item_mappings)"
)


def _subtree_in(cte_name: str) -> str:
    """顶层行 i 的子树（自身/子/孙）里有 id 落在某 CTE 集合中。"""
    return (
        f"(i.id IN (SELECT iid FROM {cte_name}) "
        f"OR EXISTS (SELECT 1 FROM emby_items c WHERE c.parent_id = i.id "
        f"AND c.id IN (SELECT iid FROM {cte_name})) "
        f"OR EXISTS (SELECT 1 FROM emby_items c "
        f"JOIN emby_items g ON g.parent_id = c.id "
        f"WHERE c.parent_id = i.id "
        f"AND g.id IN (SELECT iid FROM {cte_name})))"
    )


def _chunks(ids: Sequence[int], size: int) -> Iterable[Sequence[int]]:
    for i in range(0, len(ids), size):
        yield ids[i:i + size]


def applecms_library_ids(db: Session) -> List[int]:
    """对账范围：全部无界库（kind=applecms）。本地库条目没有上游
    映射、本就不命中，范围按 kind 划是为了不把托管行等其它人群
    卷进来（与同步工人的库口径一致）。"""
    from backend.emby_server import applecms_library as acl
    return [r[0] for r in db.query(em.Library.id)
            .filter(em.Library.kind == acl.KIND_APPLECMS).all()]


def find_no_line_item_ids(db: Session, library_ids: Sequence[int]) -> List[int]:
    """命中「不能播」口径的顶层条目 id（升序）。纯查询、不写库。"""
    if not library_ids:
        return []
    sql = text(
        f"WITH {_EN_CTE}, {_ANY_CTE} "
        "SELECT i.id FROM emby_items i "
        "WHERE i.parent_id IS NULL AND i.deleted_at IS NULL "
        "AND i.merged_into_id IS NULL "
        "AND i.library_id IN :lib_ids "
        f"AND {_subtree_in('any_map')} "
        f"AND NOT {_subtree_in('en')} "
        "ORDER BY i.id"
    ).bindparams(bindparam("lib_ids", expanding=True))
    return [r[0] for r in db.execute(sql, {"lib_ids": list(library_ids)})]


def items_with_enabled_line(db: Session, item_ids: Sequence[int]) -> Set[int]:
    """给定顶层条目 id 里，子树已有启用源线路且仍是活行的集合。"""
    if not item_ids:
        return set()
    sql = text(
        f"WITH {_EN_CTE} "
        "SELECT i.id FROM emby_items i "
        "WHERE i.id IN :ids AND i.deleted_at IS NULL "
        "AND i.merged_into_id IS NULL "
        f"AND {_subtree_in('en')}"
    ).bindparams(bindparam("ids", expanding=True))
    return {r[0] for r in db.execute(sql, {"ids": list(item_ids)})}


def _open_ledger_map(db: Session, item_ids: Sequence[int],
                     reason: str = REASON_NO_ENABLED_LINE
                     ) -> Dict[int, em.ItemHiddenLedger]:
    rows = (
        db.query(em.ItemHiddenLedger)
        .filter(em.ItemHiddenLedger.reason == reason,
                em.ItemHiddenLedger.restored_at.is_(None),
                em.ItemHiddenLedger.item_id.in_(list(item_ids)))
        .all())
    out: Dict[int, em.ItemHiddenLedger] = {}
    for r in rows:  # 同一条目正常只有一行在册；万一重复取最早一行
        out.setdefault(r.item_id, r)
    return out


def _hide_items(db: Session, item_ids: Sequence[int],
                reason: str = REASON_NO_ENABLED_LINE) -> int:
    """把命中的可见条目藏掉并记台账（只 flush，提交归调用方）。

    已经藏着的条目一律跳过：台账在册的是本机制已藏（幂等），
    不在册的是别人藏的（不 claimed、不补台账）。reason 决定写哪
    一批的台账行，两批（一期 no_enabled_line / 二期
    all_lines_dead）靠它严格分开、互不 claimed。
    """
    if not item_ids:
        return 0
    now = datetime.now()
    rows = (db.query(em.MediaItem)
            .filter(em.MediaItem.id.in_(list(item_ids))).all())
    open_map = _open_ledger_map(db, [r.id for r in rows], reason)
    hidden = 0
    for row in rows:
        if row.is_hidden:
            continue
        stale = open_map.get(row.id)
        if stale is not None:
            # 在册却可见：曾被人工解藏又再度命中口径。关旧行、
            # 重新藏并记新行（台账保持一行一事件）。
            stale.restored_at = now
        row.is_hidden = True
        db.add(em.ItemHiddenLedger(
            item_id=row.id, reason=reason, hidden_at=now))
        hidden += 1
    db.flush()
    return hidden


def _restore_items(db: Session, item_ids: Sequence[int],
                   reason: str = REASON_NO_ENABLED_LINE) -> int:
    """解藏台账在册条目并记 restored_at（只 flush，提交归调用方）。

    只处理台账在册（open、且 reason 相同）的 id；不在册的 id
    传进来也直接忽略——这是「不误解藏台账外 is_hidden」的硬闸，
    reason 参数保证两批互不解藏对方的条目。
    """
    if not item_ids:
        return 0
    now = datetime.now()
    open_map = _open_ledger_map(db, item_ids, reason)
    if not open_map:
        return 0
    rows = (db.query(em.MediaItem)
            .filter(em.MediaItem.id.in_(list(open_map))).all())
    restored = 0
    for row in rows:
        if row.is_hidden:
            row.is_hidden = False
        open_map[row.id].restored_at = now
        restored += 1
    db.flush()
    return restored


def reconcile_no_line_hidden(db: Session, *,
                             batch_size: int = _BATCH_SIZE) -> Dict[str, int]:
    """全量对账：藏新命中的、解藏已有线的。本函数自带分批提交
    （调用方不要再包事务）；同步轮收尾与运维手动调用共用此口。
    幂等：重复跑 hidden/restored 均为 0。"""
    stats = {"candidates": 0, "hidden": 0, "restored": 0}
    lib_ids = applecms_library_ids(db)
    if lib_ids:
        candidates = find_no_line_item_ids(db, lib_ids)
        stats["candidates"] = len(candidates)
        for chunk in _chunks(candidates, batch_size):
            stats["hidden"] += _hide_items(db, chunk)
            db.commit()
    open_ids = list(dict.fromkeys(
        r[0] for r in db.query(em.ItemHiddenLedger.item_id)
        .filter(em.ItemHiddenLedger.reason == REASON_NO_ENABLED_LINE,
                em.ItemHiddenLedger.restored_at.is_(None))
        .all()))
    for chunk in _chunks(open_ids, batch_size):
        live = items_with_enabled_line(db, chunk)
        if live:
            stats["restored"] += _restore_items(db, sorted(live))
        db.commit()
    if stats["hidden"] or stats["restored"]:
        logger.info("无线路对账完成 candidates=%s hidden=%s restored=%s",
                    stats["candidates"], stats["hidden"], stats["restored"])
    return stats


def restore_items_with_source(db: Session, source_id: int) -> int:
    """定向解藏：台账在册条目里，子树挂着指定源映射的（只 flush）。

    供 enable_source 联动：源一启用，挂它的在册条目立即回来，
    不用等同步轮对账。源已启用是调用前提，故命中即「有启用线」；
    仍过一遍 items_with_enabled_line 做活行与真有线的双保险。
    """
    sql = text(
        "SELECT DISTINCT l.item_id FROM emby_item_hidden_ledger l "
        "WHERE l.reason = :reason AND l.restored_at IS NULL "
        "AND (l.item_id IN (SELECT local_item_id "
        "                   FROM upstream_item_mappings "
        "                   WHERE source_id = :sid) "
        "OR EXISTS (SELECT 1 FROM emby_items c "
        "           JOIN upstream_item_mappings m "
        "             ON m.local_item_id = c.id "
        "           WHERE c.parent_id = l.item_id "
        "             AND m.source_id = :sid) "
        "OR EXISTS (SELECT 1 FROM emby_items c "
        "           JOIN emby_items g ON g.parent_id = c.id "
        "           JOIN upstream_item_mappings m "
        "             ON m.local_item_id = g.id "
        "           WHERE c.parent_id = l.item_id "
        "             AND m.source_id = :sid))"
    )
    ids = [r[0] for r in db.execute(
        sql, {"reason": REASON_NO_ENABLED_LINE, "sid": int(source_id)})]
    if not ids:
        return 0
    live = items_with_enabled_line(db, ids)
    if not live:
        return 0
    return _restore_items(db, sorted(live))


# ---------------------------------------------------------------------------
# 清理二期：源开着但整片全线已死（reason=all_lines_dead）
#
# 与一期的分工：一期看「配置」（子树没有任何启用源线路）；本批看
# 「实测」（源都开着，探活却确认每条线都拉不动）。判定分两段：
# 1) 候选（find_dead_line_candidate_ids）：拿探活结论（mapping_id →
#    verdict）与当前 DB 状态求交集——顶层活行、当前可见、子树全部
#    映射都有探活结论且全 ∈ {dead, gone}、至少一条映射的源当前
#    enabled。多线条目只要有一条没被探到就出局（数据缺失不许猜）；
#    源已停用的出局（归一期）；已藏的出局（不许 claimed 别人的）。
# 2) 落藏（verify_and_hide_dead_line_items）：探活数据会旧，候选
#    每条线按 probe_mapping 同路径新鲜复检，仍全 dead/gone 且至
#    少一条映射的源仍 enabled 才藏；复检出 live = 活过来了，不
#    藏；出 unknown（判不了）也不藏——没实锤不许藏。
# 解藏（recheck_restore_dead_line_items）：没有配置信号可依（源
# 一直开着），只能靠复检发现活线。有界轮转：每轮按台账 hidden_at
# 先后取至多 limit 条在册条目逐条复检，任一条线 live 即解藏；
# maybe_ 包装带最小间隔节流（SystemConfig 时间戳），挂同步轮
# 收尾低频推进，绝不每轮全量复检几万条。
# ---------------------------------------------------------------------------

VERDICT_LIVE = "live"
VERDICT_DEAD = "dead"
VERDICT_GONE = "gone"
VERDICT_UNKNOWN = "unknown"
_DEAD_VERDICTS = frozenset({VERDICT_DEAD, VERDICT_GONE})

#: 轮转复检每轮条目数硬顶（安全闸：任何配置/参数都不许越过）
_RESTORE_LIMIT_MAX = 500
_RESTORE_LIMIT_DEFAULT = 500
#: 轮转复检最小间隔默认 1 小时（同步间隔 6h 时实际每轮同步至多跑一次）
_RECHECK_INTERVAL_DEFAULT = 3600
_RECHECK_LAST_KEY = "all_lines_dead_recheck_last_at"

#: 子树映射一行：(mapping_id, local_item_id, source_id, source_enabled)
SubtreeMapping = Tuple[int, int, int, bool]
ProbeFn = Callable[[Session, int, int], str]


def subtree_mappings(db: Session, item_ids: Sequence[int]
                     ) -> Dict[int, List[SubtreeMapping]]:
    """顶层条目 → 其子树（自身＋子＋孙，与一期口径一致）全部映射。

    子/孙行不按软删/合并过滤，与一期 _subtree_in 的 EXISTS 口径
    逐字对齐。纯查询、不写库。
    """
    out: Dict[int, List[SubtreeMapping]] = {int(t): [] for t in item_ids}
    if not item_ids:
        return out
    top_of: Dict[int, int] = {int(t): int(t) for t in item_ids}
    frontier = [int(t) for t in item_ids]
    node_ids = set(frontier)
    for _ in range(2):  # 子、孙两层
        if not frontier:
            break
        children = (db.query(em.MediaItem.id, em.MediaItem.parent_id)
                    .filter(em.MediaItem.parent_id.in_(frontier)).all())
        frontier = []
        for cid, pid in children:
            top_of[int(cid)] = top_of[int(pid)]
            node_ids.add(int(cid))
            frontier.append(int(cid))
    if not node_ids:
        return out
    maps = (db.query(em.UpstreamItemMapping)
            .filter(em.UpstreamItemMapping.local_item_id.in_(
                sorted(node_ids))).all())
    src_ids = {m.source_id for m in maps}
    enabled: Dict[int, bool] = {}
    if src_ids:
        enabled = {s.id: bool(s.enabled) for s in
                   db.query(em.UpstreamSource)
                   .filter(em.UpstreamSource.id.in_(sorted(src_ids))).all()}
    for m in maps:
        top = top_of.get(m.local_item_id)
        if top is None:
            continue
        out[top].append((m.id, m.local_item_id, m.source_id,
                         enabled.get(m.source_id, False)))
    return out


def find_dead_line_candidate_ids(db: Session, library_ids: Sequence[int],
                                 verdicts: Mapping[int, str]) -> List[int]:
    """二期候选（升序）：口径见本段头注。纯查询、不写库。

    ``verdicts`` 是探活结论 mapping_id → verdict（live/dead/gone）；
    本函数只认 dead/gone，其余值（含 live/unknown）都让条目出局。
    """
    if not library_ids or not verdicts:
        return []
    from sqlalchemy import or_
    top_ids = [r[0] for r in db.query(em.MediaItem.id).filter(
        em.MediaItem.parent_id.is_(None),
        em.MediaItem.deleted_at.is_(None),
        em.MediaItem.merged_into_id.is_(None),
        or_(em.MediaItem.is_hidden.is_(False),
            em.MediaItem.is_hidden.is_(None)),
        em.MediaItem.library_id.in_(list(library_ids)),
    ).order_by(em.MediaItem.id).all()]
    candidates: List[int] = []
    for chunk in _chunks(top_ids, 500):
        sub = subtree_mappings(db, chunk)
        for tid in chunk:
            maps = sub.get(tid) or []
            if not maps:
                continue
            vs = [verdicts.get(mid) for mid, _lid, _sid, _en in maps]
            if any(v not in _DEAD_VERDICTS for v in vs):
                continue  # 有活线 / 有没探到的 / 有判不了的 → 出局
            if not any(en for _mid, _lid, _sid, en in maps):
                continue  # 无启用源 → 一期的地盘，不进本批
            candidates.append(tid)
    return candidates


def probe_mapping(db: Session, local_item_id: int, mapping_id: int) -> str:
    """单条线路新鲜复检：与探活 probe.py 同一路径、同一分类。

    走应用取流 ``resolve_mapped_upstream_stream(db, item_id,
    mapping_id=...)``（与真实播放逐字同一路径）：成功 = live；
    ``_CmsEvictableFailure``（缓存可淘汰类失败）= gone；
    ``UpstreamPlaybackError`` = dead；其余异常 = unknown。
    唯一与 probe.py 的有意偏差：取流总闸关着时 resolve 抛
    ``upstream_line_disabled``——那是配置态不是线路死活，归
    unknown（判不了就不许据此藏片）。
    """
    from backend.emby_server import upstream_playback as upb
    evictable = getattr(upb, "_CmsEvictableFailure", None)
    try:
        upb.resolve_mapped_upstream_stream(
            db, local_item_id, mapping_id=mapping_id)
        return VERDICT_LIVE
    except Exception as exc:  # noqa: BLE001 — 分类口径要求全捕获
        try:
            db.rollback()
        except Exception:
            pass
        if evictable is not None and isinstance(exc, evictable):
            return VERDICT_GONE
        if getattr(exc, "code", "") == "upstream_line_disabled":
            return VERDICT_UNKNOWN
        if isinstance(exc, upb.UpstreamPlaybackError):
            return VERDICT_DEAD
        return VERDICT_UNKNOWN


def _probe_all(db: Session, maps: Sequence[SubtreeMapping],
               probe_fn: ProbeFn,
               pace: Optional[Callable[[], None]]) -> List[str]:
    """逐条复检一个条目的全部子树映射，返回结论列表（与 maps 同序）。

    探针自身抛异常按 unknown 计（判不了）；pace 在每次探前调用
    一次，供落地脚本做限速/抖动/熔断等待——机制本身不等。
    """
    out: List[str] = []
    for mid, lid, _sid, _en in maps:
        if pace is not None:
            pace()
        try:
            out.append(probe_fn(db, lid, mid))
        except Exception:  # noqa: BLE001 — 探针失败 = 判不了
            try:
                db.rollback()
            except Exception:
                pass
            out.append(VERDICT_UNKNOWN)
    return out


def verify_and_hide_dead_line_items(
        db: Session, item_ids: Sequence[int], *,
        probe: Optional[ProbeFn] = None,
        pace: Optional[Callable[[], None]] = None,
        batch_size: int = _BATCH_SIZE) -> Dict[str, int]:
    """落藏：候选逐条新鲜复检，全线仍死才藏（台账 all_lines_dead）。

    本函数自带分批提交（调用方不要再包事务）。逐条纪律：条目须
    仍是活行且当前可见（已藏/已删/已合并跳过，绝不 claimed）；
    子树无映射跳过；复检时至少一条映射的源须仍 enabled（源被
    停掉的归一期口径，本批不藏）；全 dead/gone 才藏，任一 live
    记 revived 跳过，任一 unknown 记 unknown 跳过。
    """
    probe_fn = probe if probe is not None else probe_mapping
    stats = {"checked": 0, "hidden": 0, "revived": 0, "unknown": 0,
             "skipped": 0}
    for chunk in _chunks(list(item_ids), batch_size):
        for tid in chunk:
            try:
                row = db.get(em.MediaItem, tid)
                if (row is None or row.deleted_at is not None
                        or row.merged_into_id is not None
                        or row.is_hidden):
                    stats["skipped"] += 1
                    continue
                maps = subtree_mappings(db, [tid]).get(tid) or []
                if not maps or not any(en for *_x, en in maps):
                    stats["skipped"] += 1
                    continue
                stats["checked"] += 1
                vs = _probe_all(db, maps, probe_fn, pace)
                if all(v in _DEAD_VERDICTS for v in vs):
                    stats["hidden"] += _hide_items(
                        db, [tid], reason=REASON_ALL_LINES_DEAD)
                elif any(v == VERDICT_LIVE for v in vs):
                    stats["revived"] += 1
                else:
                    stats["unknown"] += 1
            except Exception:  # noqa: BLE001 — 单条失败不许拖垮整批
                try:
                    db.rollback()
                except Exception:
                    pass
                stats["skipped"] += 1
                logger.exception("全死线落藏单条失败 item_id=%s", tid)
        db.commit()
    if stats["hidden"]:
        logger.info("全死线落藏完成 checked=%s hidden=%s revived=%s "
                    "unknown=%s skipped=%s",
                    stats["checked"], stats["hidden"], stats["revived"],
                    stats["unknown"], stats["skipped"])
    return stats


def _restore_limit(limit: Optional[int]) -> int:
    """轮转每轮条数：参数 > env > 默认，统一夹到 [1, 硬顶 500]。"""
    raw = limit
    if raw is None:
        env_raw = os.getenv("DEAD_LINE_RECHECK_LIMIT", "")
        try:
            raw = int(env_raw) if env_raw.strip() else _RESTORE_LIMIT_DEFAULT
        except ValueError:
            raw = _RESTORE_LIMIT_DEFAULT
    try:
        value = int(raw)
    except (TypeError, ValueError):
        value = _RESTORE_LIMIT_DEFAULT
    return max(1, min(value, _RESTORE_LIMIT_MAX))


def _recheck_interval_sec() -> int:
    env_raw = os.getenv("DEAD_LINE_RECHECK_MIN_INTERVAL_SEC", "")
    try:
        value = int(env_raw) if env_raw.strip() else _RECHECK_INTERVAL_DEFAULT
    except ValueError:
        value = _RECHECK_INTERVAL_DEFAULT
    return max(0, value)


def recheck_restore_dead_line_items(
        db: Session, *, limit: Optional[int] = _RESTORE_LIMIT_DEFAULT,
        probe: Optional[ProbeFn] = None,
        pace: Optional[Callable[[], None]] = None) -> Dict[str, int]:
    """有界轮转复检解藏：按台账 hidden_at 先后取至多 limit 条
    all_lines_dead 在册条目，逐条复检其全部子树线路，任一 live
    即解藏（只动本 reason 在册行）。本函数自带分批提交。

    - 条目已软删/合并/不存在：台账行留着追溯（stale），不解藏；
    - 条目已被人工解藏（在册却可见）：关闭台账行止损，计 restored；
    - 子树已无映射：判不了，留待后续轮次（stale）；
    - 全 dead/gone：继续藏（still_dead）；含 unknown：不动（unknown）。
    轮转公平性靠 hidden_at 升序：没轮到的下轮优先，不会饿死。
    """
    probe_fn = probe if probe is not None else probe_mapping
    take = _restore_limit(limit)
    stats = {"checked": 0, "restored": 0, "still_dead": 0,
             "unknown": 0, "stale": 0}
    open_rows = (
        db.query(em.ItemHiddenLedger)
        .filter(em.ItemHiddenLedger.reason == REASON_ALL_LINES_DEAD,
                em.ItemHiddenLedger.restored_at.is_(None))
        .order_by(em.ItemHiddenLedger.hidden_at.asc(),
                  em.ItemHiddenLedger.id.asc())
        .limit(take).all())
    done = 0
    for ledger in open_rows:
        tid = ledger.item_id
        try:
            row = db.get(em.MediaItem, tid)
            if row is None or row.deleted_at is not None \
                    or row.merged_into_id is not None:
                stats["stale"] += 1
                continue
            if not row.is_hidden:
                # 在册却可见 = 已被人工解藏：关行止损，别再反复复检它
                ledger.restored_at = datetime.now()
                stats["restored"] += 1
                continue
            maps = subtree_mappings(db, [tid]).get(tid) or []
            if not maps:
                stats["stale"] += 1
                continue
            stats["checked"] += 1
            vs = _probe_all(db, maps, probe_fn, pace)
            if any(v == VERDICT_LIVE for v in vs):
                stats["restored"] += _restore_items(
                    db, [tid], reason=REASON_ALL_LINES_DEAD)
            elif all(v in _DEAD_VERDICTS for v in vs):
                stats["still_dead"] += 1
            else:
                stats["unknown"] += 1
        except Exception:  # noqa: BLE001 — 单条失败不许拖垮整轮
            try:
                db.rollback()
            except Exception:
                pass
            stats["unknown"] += 1
            logger.exception("全死线轮转复检单条失败 item_id=%s", tid)
        done += 1
        if done % 100 == 0:
            db.commit()
    db.commit()
    if stats["restored"]:
        logger.info("全死线轮转复检完成 checked=%s restored=%s "
                    "still_dead=%s unknown=%s stale=%s",
                    stats["checked"], stats["restored"],
                    stats["still_dead"], stats["unknown"], stats["stale"])
    return stats


def maybe_recheck_restore_dead_line_items(
        db: Session, *, probe: Optional[ProbeFn] = None,
        limit: Optional[int] = None,
        now_ts: Optional[int] = None) -> Dict[str, int]:
    """同步轮收尾挂接口：带最小间隔节流的轮转复检。

    上次执行时间存 SystemConfig（store 正统通道，与 drive_changes
    page token 同类运行态 KV）。间隔未到返回 ``{"throttled": True,
    ...}`` 零复检；节流戳先落库再执行，防并发/重复轮次叠跑。
    节流判断本身失败（读不到戳）按「没跑过」处理，宁可多跑一轮
    有界复检，也不让解藏通道被脏戳永久堵死。
    """
    from backend.integrations import store
    interval = _recheck_interval_sec()
    now = int(time.time()) if now_ts is None else int(now_ts)
    last = 0
    try:
        last = int(store.read_value(db, _RECHECK_LAST_KEY, "") or 0)
    except (TypeError, ValueError):
        last = 0
    if interval > 0 and last and now - last < interval:
        return {"throttled": True, "checked": 0, "restored": 0,
                "still_dead": 0, "unknown": 0, "stale": 0}
    store.write_values(
        db, {_RECHECK_LAST_KEY: str(now)},
        {_RECHECK_LAST_KEY: "全死线条目轮转复检上次执行时间（epoch 秒）"})
    db.commit()
    stats = recheck_restore_dead_line_items(db, limit=limit, probe=probe)
    stats["throttled"] = False
    return stats
