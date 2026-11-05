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
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Dict, Iterable, List, Sequence, Set

from sqlalchemy import bindparam, text
from sqlalchemy.orm import Session

from backend.emby_server import models as em

logger = logging.getLogger(__name__)

REASON_NO_ENABLED_LINE = "no_enabled_line"

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


def _open_ledger_map(db: Session, item_ids: Sequence[int]) -> Dict[int, em.ItemHiddenLedger]:
    rows = (
        db.query(em.ItemHiddenLedger)
        .filter(em.ItemHiddenLedger.reason == REASON_NO_ENABLED_LINE,
                em.ItemHiddenLedger.restored_at.is_(None),
                em.ItemHiddenLedger.item_id.in_(list(item_ids)))
        .all())
    out: Dict[int, em.ItemHiddenLedger] = {}
    for r in rows:  # 同一条目正常只有一行在册；万一重复取最早一行
        out.setdefault(r.item_id, r)
    return out


def _hide_items(db: Session, item_ids: Sequence[int]) -> int:
    """把命中的可见条目藏掉并记台账（只 flush，提交归调用方）。

    已经藏着的条目一律跳过：台账在册的是本机制已藏（幂等），
    不在册的是别人藏的（不 claimed、不补台账）。
    """
    if not item_ids:
        return 0
    now = datetime.now()
    rows = (db.query(em.MediaItem)
            .filter(em.MediaItem.id.in_(list(item_ids))).all())
    open_map = _open_ledger_map(db, [r.id for r in rows])
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
            item_id=row.id, reason=REASON_NO_ENABLED_LINE, hidden_at=now))
        hidden += 1
    db.flush()
    return hidden


def _restore_items(db: Session, item_ids: Sequence[int]) -> int:
    """解藏台账在册条目并记 restored_at（只 flush，提交归调用方）。

    只处理台账在册（open）的 id；不在册的 id 传进来也直接忽略——
    这是「不误解藏台账外 is_hidden」的硬闸。
    """
    if not item_ids:
        return 0
    now = datetime.now()
    open_map = _open_ledger_map(db, item_ids)
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
