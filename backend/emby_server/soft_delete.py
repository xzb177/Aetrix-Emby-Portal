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


def mark_soft_deleted(db, item_ids: list) -> int:
    """把条目标记为已删除（不碰从属数据：进度、收藏都留着等它回来）"""
    from backend.emby_server.models import MediaItem

    ids = [int(i) for i in item_ids if i]
    if not ids:
        return 0
    db.query(MediaItem).filter(MediaItem.id.in_(ids)).update(
        {"deleted_at": datetime.now()}, synchronize_session=False)
    return len(ids)


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
    _purge_items(db, ids)
    # 列表去重缓存：物理删除改变条目集合 → bump 版本号（提交后可见）。
    # 唯一调用方（扫描清理）恒传 library_id；None 时退化为全库 bump。
    try:
        from backend.emby_server import items_dedup_cache as _idc
        from backend.emby_server.models import Library
        _libs = [library_id] if library_id else [
            r[0] for r in db.query(Library.id).filter(Library.is_enabled == True).all()]  # noqa: E712
        _idc.bump_library_item_versions(db, _libs, force=True)
    except Exception:  # noqa: BLE001
        pass
    db.commit()
    logger.info("软删除回收：%d 条隐藏超过 %d 天的条目已物理删除", len(ids), days)
    return len(ids)


def restore_all(db) -> int:
    """把**所有**隐藏条目放回来（关掉软删除开关时的回滚步骤）"""
    from backend.emby_server.models import MediaItem

    return int(db.query(MediaItem).filter(MediaItem.deleted_at.isnot(None)).update(
        {"deleted_at": None}, synchronize_session=False) or 0)