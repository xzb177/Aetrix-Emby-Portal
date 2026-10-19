"""软删除：条目「下架」而不是物理消失（v2.48.0）

以前清理阶段是**硬删**：从库里删掉条目以及它的外挂进度、收藏、分面。误删一次（尤其是
补种、临时卸载、网络抖动那几种被删除保护之外的场景）就再也回不来了——用户点过「继续
观看」的位置、收藏、已刮削好的元数据全跟着没了。

现在改成：

- **标记**：清理阶段只写 ``deleted_at``，一行 ``UPDATE``，从属数据原样留着；
- **隐藏**：所有 ORM 查询自动带上 ``deleted_at IS NULL``（见下面的 ``do_orm_execute`` 钩子），
  所以**读路径一行都不用改**，用户端、管理端、搜索、统计看到的口径与硬删完全一致；
- **复活**：文件重新出现（补种回来、下架片重新入库）时扫描器会把 ``deleted_at`` 清空，
  连同它的播放进度、收藏一起回来；
- **回收**：隐藏超过 ``MEDIA_SOFT_DELETE_PURGE_DAYS`` 天才真正物理删除，表不会无限涨。

**可回滚**：把 ``MEDIA_SOFT_DELETE`` 设为 ``0`` 即可。关掉之后启动时会先把已隐藏的条目
全部放回来（否则它们会「复活」成一批删不掉也不显示的僵尸），清理阶段照旧硬删——与本
功能上线前的行为完全一致。
"""
from __future__ import annotations

import logging
import os
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timedelta

from sqlalchemy import event
from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

_TRUTHY = {"1", "true", "yes", "on"}

#: ``emby_items`` 的表名（可见性过滤靠 FROM 判定，不 import 模型也就不会有循环导入）
ITEM_TABLE_NAME = "emby_items"


def soft_delete_enabled() -> bool:
    """软删除开关（默认开）。关掉 = 回到 v2.47.0 的硬删行为"""
    return (os.getenv("MEDIA_SOFT_DELETE", "1") or "1").strip().lower() in _TRUTHY


def soft_delete_purge_days() -> int:
    """隐藏多少天之后才物理删除（0 = 永不自动清理）"""
    try:
        return max(0, int((os.getenv("MEDIA_SOFT_DELETE_PURGE_DAYS", "30") or "30").strip()))
    except ValueError:
        return 30


#: 单批回收上限（与扫描清理的 CLEANUP_BATCH 同量级，别让一条 commit 太大）
PURGE_BATCH = 500

#: 需要看到「已软删条目」的调用方（复活、恢复、删库、测试）把它临时置 True
_allow_deleted: ContextVar[bool] = ContextVar("aetrix_allow_deleted_items", default=False)


@contextmanager
def include_deleted():
    """在块内允许看到 ``deleted_at`` 非空的条目（复活 / 恢复 / 删库 / 排查用）"""
    token = _allow_deleted.set(True)
    try:
        yield
    finally:
        _allow_deleted.reset(token)


def _touches_items(statement) -> bool:
    """这条语句的 FROM 里有没有 ``emby_items``（避免把条件加到无关语句上）

    看的是 FROM 而不是「选了哪些列」：``db.query(func.count()).select_from(MediaItem)``、
    按类型分组计数、与 UserMediaData 联结这些**一个 MediaItem 列都不选**，但它们同样
    需要把下架的条目算出去。
    """
    get_final = getattr(statement, "get_final_froms", None)
    if not callable(get_final):
        return False
    return any(getattr(f, "name", None) == ITEM_TABLE_NAME
               for f in get_final() or ())


@event.listens_for(Session, "do_orm_execute")
def _hide_soft_deleted(execute_state):
    """给所有查 MediaItem 的 ORM 语句自动补上 ``deleted_at IS NULL``

    装在 ``Session`` 类上（全局、进程级）：三十多个模块、几百处 ``db.query(MediaItem)``
    只要漏改一处，被删的条目就会换个页面重新冒出来。这里一处兜住，且

    - 只作用于 ``SELECT``：``UPDATE`` / ``DELETE``（含批量删与回收）不受影响，
      否则回收就会变成「删不到自己」的死循环；
    - 只在 FROM 里出现 ``emby_items`` 时才加：给无关语句加这个条件会凭空多出一张表的
      笛卡尔积；
    - ``include_deleted()`` 里显式放行（复活与删库路径需要看到隐藏行）。

    **唯一挡不住的写法**是 ``Query.count()``：它会把语句包成子查询
    （``SELECT count(*) FROM (SELECT … FROM emby_items)``），条件下推到内层需要重建整条
    语句，代价与风险都不小。数条目的地方一律用 :func:`count_visible`（或
    ``query(func.count(MediaItem.id)).scalar()``），它不走子查询包装。
    Core 语句（``session.execute(select(...))``）不经过这个钩子，涉及条目的地方要自己带
    ``deleted_at IS NULL``（见 facets / image_store / scanner 的清理遍历）。
    """
    if not soft_delete_enabled() or _allow_deleted.get():
        return
    if not execute_state.is_select:
        return
    from backend.emby_server.models import MediaItem

    if not _touches_items(execute_state.statement):
        return
    execute_state.statement = execute_state.statement.where(MediaItem.deleted_at.is_(None))


def count_visible(db, *filters) -> int:
    """数「用户看得见」的条目（不过滤时**必须**用它，不要用 ``Query.count()``）

    ``Query.count()`` 会把语句包成子查询，全局可见性过滤下不到那里去，结果会把已下架
    的条目也算进来——媒体库卡片上的条目数、客户端 ``Items/Counts`` 就会比实际能看到的多。
    """
    from backend.emby_server.models import MediaItem
    from sqlalchemy import func

    query = db.query(func.count(MediaItem.id))
    for condition in filters:
        query = query.filter(condition)
    return int(query.scalar() or 0)


#: 合并链深度上限（与 api._family_series_ids 的 8 层同口径）
_MERGE_CHAIN_DEPTH = 8


def _visible_merge_target(db, item, skip_ids=frozenset()):
    """沿 merged_into_id 向上找「可见的」合并链顶（不含 item 自身）。

    只认 ORM 可见行（全局软删过滤自动生效）且不在 skip_ids（本批待删）里的行；
    item 本来就没有上游 / 上游不可见（已软删）/ 上游在本批待删 / 成环 /
    深度耗尽 → 返回 None（调用方走家族 root 或提升分身兜底）。

    注意：读的是同一 Session 的实例——调用方之前改过的 merged_into_id
    （未提交）在内存里同样生效，批量处理时浅层先行即靠这一点保持一致。
    """
    from backend.emby_server.models import MediaItem

    nxt_id = item.merged_into_id
    if not nxt_id:
        return None
    seen = {item.id}
    top = None
    for _ in range(_MERGE_CHAIN_DEPTH):
        if not nxt_id or nxt_id in seen or nxt_id in skip_ids:
            break
        seen.add(nxt_id)
        parent = db.query(MediaItem).filter(MediaItem.id == nxt_id).first()
        if parent is None:
            break  # 上游不可见（已软删）：链已断
        top = parent
        nxt_id = parent.merged_into_id
    return top


def _find_alternate_family_root(db, item, skip_ids=frozenset()):
    """在同库家族里找另一个可见 root。

    准入与 api._family_series_ids 一致：同 item_type、同库、
    merged_into_id 为空、非本行、非本批待删，且（tmdb_id 非空且相等，
    或 dedup.normalize_name(name) 相等）。多个候选时按 pick_primary 取最优。
    """
    from backend.emby_server import models as em
    from backend.emby_server.dedup import normalize_name, pick_primary

    norm = normalize_name(item.name)
    tmdb = (item.tmdb_id or "").strip()
    q = db.query(em.MediaItem).filter(
        em.MediaItem.item_type == item.item_type,
        em.MediaItem.library_id == item.library_id,
        em.MediaItem.merged_into_id.is_(None),
        em.MediaItem.id != item.id,
    )
    if skip_ids:
        q = q.filter(~em.MediaItem.id.in_(skip_ids))
    cands = []
    for cand in q.all():
        cand_tmdb = (cand.tmdb_id or "").strip()
        if (tmdb and cand_tmdb == tmdb) or (norm and normalize_name(cand.name) == norm):
            cands.append(cand)
    return pick_primary(cands) if cands else None


def _merge_depth(db, item) -> int:
    """合并链深度（root=0）：批量软删时浅层先行，保证父行先改指。"""
    from backend.emby_server.models import MediaItem

    depth, seen, nxt_id = 0, {item.id}, item.merged_into_id
    for _ in range(_MERGE_CHAIN_DEPTH):
        if not nxt_id or nxt_id in seen:
            break
        seen.add(nxt_id)
        depth += 1
        parent = db.query(MediaItem).filter(MediaItem.id == nxt_id).first()
        if parent is None:
            break
        nxt_id = parent.merged_into_id
    return depth


def rehome_merged_children(db, item, skip_ids=frozenset()) -> str:
    """软删/回收某行之前，把指向它的分身（merged_into 链）改指到其它可见行。

    防治缺集普查 2026-10-19 桶 C2 的「孤儿集」：分身的合并目标 root 被软删后，
    分身名下可见的季/集再也触达不到（没有任何可见 root 的 family 能含它）。

    处置优先级：
    1. item 有可见合并上游链顶（排除本批待删）→ 分身改指链顶；
    2. 否则 item 就是可见链顶：在同库家族里找另一个可见 root
       （tmdb 相同或归一化名相同，准入与 api._family_series_ids 一致）
       → 分身改指它；
    3. 否则把最合适的分身提升为可见 root（merged_into_id=None，
       pick_primary 评分），其余分身改指它。

    只改 merged_into_id，不碰 deleted_at 与从属数据；失败抛异常，
    由调用方 fail-closed（本批跳过该行：宁可本次不删，不造孤儿）。

    返回处置动作：no-children / repointed-to-chain-top /
    repointed-to-family-root / promoted-child。
    """
    from backend.emby_server import models as em
    from backend.emby_server.dedup import DEDUP_TYPES, pick_primary

    # include_deleted：要改指的**所有**分身（含已隐藏的）——否则回收隐藏 root
    # 时，已隐藏分身的 merged_into 会悬空指向一个物理上不存在的行
    # （PG 外键还会直接让回收报错）。
    with include_deleted():
        children = db.query(em.MediaItem).filter(
            em.MediaItem.merged_into_id == item.id).all()
    if not children:
        return "no-children"
    top = _visible_merge_target(db, item, skip_ids)
    target, action = None, None
    if top is not None:
        target, action = top, "repointed-to-chain-top"
    elif item.item_type in DEDUP_TYPES:
        alt = _find_alternate_family_root(db, item, skip_ids)
        if alt is not None:
            target, action = alt, "repointed-to-family-root"
    if target is not None:
        for child in children:
            child.merged_into_id = target.id
    else:
        # 兜底：提升最合适的分身。优先选可见且非本批待删的分身——本批待删的
        # 分身稍后由它们自己的守卫处理自己的分身；若分身全在本批，任选其一
        # （随后同样被删，无孤儿产生）。
        candidates = [c for c in children
                      if c.id not in skip_ids and c.deleted_at is None]
        if not candidates:
            candidates = [c for c in children if c.id not in skip_ids] or children
        best = pick_primary(candidates)
        best.merged_into_id = None
        for child in children:
            if child.id != best.id:
                child.merged_into_id = best.id
        target, action = best, "promoted-child"
    logger.info("软删孤儿守卫：%s(id=%s) 的 %d 个分身已改指 id=%s（%s）",
                item.item_type, item.id, len(children), target.id, action)
    return action


def mark_soft_deleted(db, item_ids: list) -> int:
    """把条目标记为已删除（不碰从属数据：进度、收藏都留着等它回来）

    C2 孤儿防治（缺集普查 2026-10-19）：先对每行跑合并链守卫
    （rehome_merged_children），浅层先行——父行先改指，子行再基于已改指的
    状态处理。守卫失败的行从本批剔除（fail-closed：宁可本次不删，不造孤儿）。
    """
    from backend.emby_server.models import MediaItem

    ids = [int(i) for i in item_ids if i]
    if not ids:
        return 0
    # r55 性能：原来逐行 load（N 条）+ 逐行 _merge_depth（每行 ≤8 条），
    # 5000 行一批就是数万条 PK 查询。这里先批量把行一次拉回（1 条），再一次
    # 查出"被分身指向"的行（1 条，include_deleted：隐藏分身也要算，否则漏守卫
    # 会留悬空 merged_into_id）。既无分身、自身 merged_into_id 又为空的行，
    # 守卫注定 no-children、深度为 0，直接进 safe 名单，不再逐行查；
    # 剩下的（合并链成员）才走原来的深度排序 + 逐行守卫，语义不变。
    rows = db.query(MediaItem).filter(MediaItem.id.in_(ids)).all()
    if not rows:
        return 0
    by_id = {r.id: r for r in rows}
    with include_deleted():
        child_target_ids = {
            r[0] for r in db.query(MediaItem.merged_into_id).filter(
                MediaItem.merged_into_id.in_(list(by_id))).all()
        }
    skip = set(by_id)
    safe_ids = []
    guarded = []
    for row in rows:
        if row.merged_into_id is None and row.id not in child_target_ids:
            safe_ids.append(row.id)
        else:
            guarded.append(row)
    guarded.sort(key=lambda r: _merge_depth(db, r))
    for row in guarded:
        try:
            rehome_merged_children(db, row, skip_ids=skip)
            safe_ids.append(row.id)
        except Exception as exc:  # noqa: BLE001
            logger.error("软删孤儿守卫失败，跳过本行 id=%s: %s", row.id, exc)
    if not safe_ids:
        return 0
    db.query(MediaItem).filter(MediaItem.id.in_(safe_ids)).update(
        {"deleted_at": datetime.now()}, synchronize_session=False)
    return len(safe_ids)


def resurrect(db, item_ids: list) -> int:
    """把条目放回可见状态（文件又出现了 / 手工恢复）"""
    from backend.emby_server.models import MediaItem

    ids = [int(i) for i in item_ids if i]
    if not ids:
        return 0
    return int(db.query(MediaItem).filter(MediaItem.id.in_(ids)).update(
        {"deleted_at": None}, synchronize_session=False) or 0)


def purge_expired(db, purge_days: int = None, library_id: int = None) -> int:
    """物理删除隐藏够久的条目，返回条数（``purge_days=0`` 表示不回收）"""
    from backend.emby_server.models import MediaItem
    from backend.emby_server.scanner import _purge_items

    days = soft_delete_purge_days() if purge_days is None else int(purge_days)
    if days <= 0:
        return 0
    cutoff = datetime.now() - timedelta(days=days)
    # include_deleted：要找的**就是**那些被隐藏的行，用默认可见性口径一条都找不到
    with include_deleted():
        query = db.query(MediaItem.id).filter(MediaItem.deleted_at.isnot(None),
                                              MediaItem.deleted_at <= cutoff)
        if library_id is not None:
            query = query.filter(MediaItem.library_id == library_id)
        ids = [row[0] for row in query.limit(PURGE_BATCH).all()]
    if not ids:
        return 0
    # C2 孤儿防治：物理回收前同样先改指分身——隐藏 root 被回收时，其分身
    # （可见或已隐藏）可能还指着它；不改指则悬空（PG 外键还会直接让回收报错）。
    # 注意：行加载用 include_deleted（要删的就是隐藏行），但改指的目标搜索必须
    # 走正常可见性，所以 rehome 调用放在 include_deleted 块之外。
    purgeable = []
    skip = set(ids)
    for _id in ids:
        with include_deleted():
            row = db.query(MediaItem).filter(MediaItem.id == _id).first()
        if row is None:
            continue
        try:
            rehome_merged_children(db, row, skip_ids=skip)
            purgeable.append(_id)
        except Exception as exc:  # noqa: BLE001
            logger.error("回收孤儿守卫失败，跳过本行 id=%s: %s", _id, exc)
    if not purgeable:
        return 0
    _purge_items(db, purgeable)
    # 列表去重缓存：物理删除改变条目集合 → bump 版本号（提交后可见）。
    # 唯一调用方（扫描清理）恒传 library_id；None 时退化为全库 bump。
    try:
        from backend.emby_server import items_dedup_cache as _idc
        from backend.emby_server.models import Library
        _libs = [library_id] if library_id else [
            r[0] for r in db.query(Library.id).filter(Library.is_enabled == True).all()]  # noqa: E712
        _idc.bump_library_item_versions(db, _libs, force=True)
    except Exception as exc:  # noqa: BLE001
        logger.warning("去重缓存版本号 bump 失败（软删除回收）: %s", exc)
    db.commit()
    # r55：守卫 fail-closed 跳过的行不在 purgeable 里，计数/返回值必须按实际
    # 物理删除的走（之前用 len(ids) 虚高）。
    logger.info("软删除回收：%d 条隐藏超过 %d 天的条目已物理删除", len(purgeable), days)
    return len(purgeable)


def restore_all(db) -> int:
    """把**所有**隐藏条目放回来（关掉软删除开关时的回滚步骤）"""
    from backend.emby_server.models import MediaItem

    return int(db.query(MediaItem).filter(MediaItem.deleted_at.isnot(None)).update(
        {"deleted_at": None}, synchronize_session=False) or 0)