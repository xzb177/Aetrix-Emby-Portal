"""条目 ↔ 分类值（流派 / 工作室 / 标签 / 发行平台）的关联表：让筛选走索引

**为什么要有它**

这些值原先只存在 `emby_items` 的逗号分隔文本列里（``genres`` / ``studios`` / ``tags`` /
``platforms``）。客户端点「类型 / 制作公司」时，服务端的筛选只能写成
``genres ILIKE '%动作%'`` —— 前置通配符**用不上任何索引**，等于每次筛选都把整库扫一遍：
十万级库里每点一次筛选就是几秒 CPU，筛选菜单（``/Items/Filters``）还得再扫一遍。

**怎么维护（写入侧）**

挂在 SQLAlchemy 的 Session flush 事件上（见文件末尾）：谁在事务里改了条目、或者新建/删除
了条目，同一个事务里就把关联行改好——扫描器、图片修复、将来任何新的写入路径都不用各自
记得调用一次同步，漏一处也不会静默变脏。批量 Core 删除（``query(...).delete()``）
走不到 ORM 事件，由 :func:`prune_orphans` 周期性兜底；升级上来的老库由
:func:`ensure_backfill` 按 id 水位增量补齐（启动维护会跑，筛选路径发现没跑完也会补一块）。

**怎么查（读取侧）**

:func:`candidate_condition` 先取该 kind 的全部取值（几千个短字符串，走
``(kind, value, item_id)`` 覆盖索引），在内存里按「全等或包含」筛出命中的 value
（语义与旧的 ``ILIKE '%x%'`` 一致，含大小写不敏感），再让主查询用
``item_id IN (SELECT item_id FROM emby_item_facets WHERE kind=? AND value IN (...))``
过滤 —— 这一步是纯索引查找，不再扫 ``emby_items``。所以客户端传「Action」时，
「Action-Adventure」照样命中，与升级前行为一致。
"""
from __future__ import annotations

import logging
import os
import threading
import time

from sqlalchemy import event, false, func, insert, inspect as sa_inspect, select, text
from sqlalchemy.orm import Session

from backend import models
from backend.emby_server import models as em
from backend.emby_server.env_util import env_float, env_int

logger = logging.getLogger(__name__)

KIND_GENRE = "genre"
KIND_STUDIO = "studio"
KIND_TAG = "tag"
KIND_PLATFORM = "platform"
KINDS = (KIND_GENRE, KIND_STUDIO, KIND_TAG, KIND_PLATFORM)

# 取值最长长度（与列定义一致：超长直接截断，否则整批插入会失败）
MAX_VALUE_LEN = 200
# 取值缓存 TTL（秒）：扫描 / 回填会主动失效，这里只是兜底
VALUES_CACHE_TTL = env_float("ITEM_FACETS_CACHE_TTL", 600.0)
# 回填水位（system_configs 里的一个键，值为「已同步到哪个 item id」）
BACKFILL_KEY = "item_facets_backfill_max_id"
BACKFILL_BATCH = env_int("ITEM_FACETS_BACKFILL_BATCH", 2000)
# 筛选路径里一次最多补多少条（读请求不该被回填拖住）
FILTER_BACKFILL_BUDGET = env_int("ITEM_FACETS_FILTER_BUDGET", 2000)
# 两次「筛选路径补块」之间的最小间隔（秒）：并发请求不要一起抢写锁
FILTER_BACKFILL_COOLDOWN = 5.0

_values_cache: dict[str, tuple[float, tuple[str, ...]]] = {}
# 关联表的代际号：每次同步/回填/清理 +1，调用方据此失效自己的缓存
_values_generation = 0
# 「分类取值变了」的回调（如 api.py 的筛选菜单缓存）
#
# 关联表是分类值的唯一真相，所以失效统一从这里发出去，而不是让每条写入路径
# （扫描、图片修复、将来任何改元数据的入口）各自记得调一次——多一处就是漏一处。
_change_listeners: list = []
_backfill_lock = threading.Lock()
_last_filter_attempt = 0.0
_FACET_COLUMNS = ("genres", "studios", "tags", "platforms")


# ==================== 写入侧 ====================


def values_of(item) -> dict[str, list[str]]:
    """条目当前的分类取值（按 kind 分组：去空、去重、限长）"""
    raw = {
        KIND_GENRE: getattr(item, "genres", "") or "",
        KIND_STUDIO: getattr(item, "studios", "") or "",
        KIND_TAG: getattr(item, "tags", "") or "",
        KIND_PLATFORM: getattr(item, "platforms", "") or "",
    }
    out: dict[str, list[str]] = {}
    for kind, text in raw.items():
        seen: list[str] = []
        for piece in str(text).split(","):
            value = piece.strip()[:MAX_VALUE_LEN]
            if value and value not in seen:
                seen.append(value)
        out[kind] = seen
    return out


def _rows_for(item) -> list[dict]:
    return [
        {"item_id": item.id, "kind": kind, "value": value}
        for kind, values in values_of(item).items()
        for value in values
    ]


def sync_item(db: Session, item) -> int:
    """重建一个条目的关联行（**不提交**，跟随调用方的事务）

    返回写入的行数。分类值很少变，而每个条目只有几行，「先删后插」比重算差异简单得多。
    """
    if item is None or getattr(item, "id", None) is None:
        return 0
    db.query(em.ItemFacet).filter(em.ItemFacet.item_id == item.id).delete(
        synchronize_session=False
    )
    rows = _rows_for(item)
    if rows:
        db.execute(insert(em.ItemFacet), rows)
    invalidate_values_cache()
    return len(rows)


def sync_items(db: Session, items) -> int:
    """批量重建（删除一次性做掉，插入按块）"""
    listed = [i for i in items if i is not None and getattr(i, "id", None) is not None]
    if not listed:
        return 0
    ids = [i.id for i in listed]
    db.query(em.ItemFacet).filter(em.ItemFacet.item_id.in_(ids)).delete(
        synchronize_session=False
    )
    rows = [row for item in listed for row in _rows_for(item)]
    if rows:
        db.execute(insert(em.ItemFacet), rows)
    invalidate_values_cache()
    return len(rows)


def prune_orphans(db: Session, commit: bool = True) -> int:
    """清掉指向已不存在条目的关联行

    批量删除（``db.query(MediaItem).filter(...).delete()``）走的是 Core 语句，
    ORM 的删除事件看不到，所以这里用一条 ``NOT IN`` 的 SQL 兜底：扫描的条目清理、
    后台删条目、任何将来的批量删除路径都不会留下孤儿行。

    v2.48.1: NOT IN 在 PG 大表上太慢，改用 NOT EXISTS + 30s 超时。
    """
    from sqlalchemy import text as sa_text
    try:
        # statement_timeout 是 PG 专用语法：SQLite/MySQL 下跳过
        #（冒烟测试跑 SQLite；生产是 PG，超时熔断只在生产生效）
        if db.bind is not None and db.bind.dialect.name == "postgresql":
            db.execute(sa_text("SET LOCAL statement_timeout = '30s'"))
        result = db.execute(
            sa_text("DELETE FROM emby_item_facets WHERE NOT EXISTS (SELECT 1 FROM emby_items WHERE emby_items.id = emby_item_facets.item_id)")
        )
        removed = result.rowcount or 0
        if removed:
            invalidate_values_cache()
            if commit:
                db.commit()
            logger.info("清理 %d 行失效的分类关联记录", removed)
        return int(removed)
    except Exception as exc:
        logger.warning("清理分类孤儿行失败（下次启动重试）: %s", exc)
        db.rollback()
        return 0


def orphan_count(db: Session) -> int:
    """指向不存在条目的关联行数（健康指标 / 测试断言用）"""
    existing = select(em.MediaItem.id)
    return int(
        db.query(func.count())
        .select_from(em.ItemFacet)
        .filter(~em.ItemFacet.item_id.in_(existing))
        .scalar()
        or 0
    )


# ==================== 查询侧 ====================


def add_change_listener(callback) -> None:
    """注册「分类取值已变」回调（幂等：同一个回调只挂一次）

    回调必须是**纯内存操作**：它在写入事务的 flush 钩子上同步执行，
    慢一点就直接拖慢扫描。真正的重活（回查数据库）留给下一次请求。
    """
    if callback not in _change_listeners:
        _change_listeners.append(callback)


def _notify_change_listeners() -> None:
    for callback in list(_change_listeners):
        try:
            callback()
        except Exception:  # noqa: BLE001 — 派生缓存失效失败不该影响业务写入
            logger.exception("分类取值变更回调失败")


def invalidate_values_cache() -> None:
    """关联表变了（扫描 / 回填 / 删条目）→ 丢掉取值缓存，并推进代际号

    代际号给调用方（如合成 Id 反查表）做跨请求缓存的失效依据：只要它没变，
    缓存的结果就一定还是对的。挂上来的回调也在这一步被通知。
    """
    global _values_generation
    _values_generation += 1
    _values_cache.clear()
    _notify_change_listeners()


def values_generation() -> int:
    """关联表的当前代际（每次同步 / 回填 / 清理都会 +1）"""
    return _values_generation


def distinct_values(db: Session, kind: str) -> tuple[str, ...]:
    """该 kind 的全部取值（走 ``(kind, value, item_id)`` 的覆盖索引，值很少）

    带进程内缓存：取值只在扫描/回填后变化，那时候会主动失效；TTL 只是兜底。
    """
    now = time.monotonic()
    hit = _values_cache.get(kind)
    if hit is not None and now - hit[0] < VALUES_CACHE_TTL:
        return hit[1]
    rows = (
        db.execute(select(em.ItemFacet.value).where(em.ItemFacet.kind == kind).distinct())
        .scalars()
        .all()
    )
    values = tuple(v for v in rows if v)
    _values_cache[kind] = (now, values)
    return values


def match_values(db: Session, kind: str, names) -> list[str]:
    """把客户端传来的分类名解析成关联表里的实际取值

    语义与升级前的 ``col ILIKE '%name%'`` 一致：全等命中，或取值里包含这个名字
    （大小写不敏感）。所以「Action」照样会命中「Action-Adventure」。
    """
    wanted = [str(n).strip() for n in names if str(n).strip()]
    if not wanted:
        return []
    folded = [w.casefold() for w in wanted]
    hits: list[str] = []
    for value in distinct_values(db, kind):
        low = value.casefold()
        if any(low == w or w in low for w in folded):
            hits.append(value)
    return hits


def candidate_condition(kind: str, names, db: Session):
    """筛选条件：``MediaItem.id IN (关联表里命中该分类的条目)``

    没传分类名 → ``None``（不加条件）；传了库里不存在的分类名 → 空结果条件
    （与旧实现的语义一致：``ILIKE`` 匹配不到任何行）。
    """
    wanted = [str(n).strip() for n in names if str(n).strip()]
    if not wanted:
        return None
    values = match_values(db, kind, wanted)
    if not values:
        return false()
    subq = select(em.ItemFacet.item_id).where(
        em.ItemFacet.kind == kind, em.ItemFacet.value.in_(values)
    )
    return em.MediaItem.id.in_(subq)


def kind_values(db: Session, kind: str) -> list[str]:
    """该 kind 的取值列表（筛选菜单 / 合成 Id 用；一次索引扫描拿全）"""
    return list(distinct_values(db, kind))


def count_virtual_items(db: Session, library) -> int:
    """虚拟媒体库的条目数（跨库的「发行平台视图」，按平台标签计数）

    旧实现在 ``platforms ILIKE '%Netflix%'`` 上计数——同样是全表扫描；这里改成先命中
    关联表（走索引）再数条目。老库没回填完时退回旧口径，数字保持一致。
    与 ``scanner.count_virtual_items`` 同签名，供调用方直接换用。
    """
    platform = (getattr(library, "platform", "") or "").strip()
    if not platform:
        return 0
    base = (
        db.query(func.count())
        .select_from(em.MediaItem)
        .filter(
            em.MediaItem.is_hidden == False,  # noqa: E712
            em.MediaItem.item_type.in_(["movie", "series"]),
        )
    )
    if ready(db):
        condition = candidate_condition(KIND_PLATFORM, [platform], db)
        if condition is None:
            return 0
        return int(base.filter(condition).scalar() or 0)
    return int(base.filter(em.MediaItem.platforms.ilike(f"%{platform}%")).scalar() or 0)


# ==================== 老库回填 ====================


def _watermark(db: Session) -> int:
    row = (
        db.query(models.SystemConfig)
        .filter(models.SystemConfig.key == BACKFILL_KEY)
        .first()
    )
    try:
        return int(row.value) if row and row.value else 0
    except (TypeError, ValueError):
        return 0


def _set_watermark(db: Session, value: int) -> None:
    row = (
        db.query(models.SystemConfig)
        .filter(models.SystemConfig.key == BACKFILL_KEY)
        .first()
    )
    if row is None:
        db.add(models.SystemConfig(key=BACKFILL_KEY, value=str(value)))
    else:
        row.value = str(value)


def max_item_id(db: Session) -> int:
    return int(db.query(func.max(em.MediaItem.id)).scalar() or 0)


def ready(db: Session) -> bool:
    """关联表是否已能代表全库（老库回填到最新条目 / 库里本来就没条目）

    这是每次筛选都要问一句的问题，所以刻意用**一条 Core 语句**同时取「水位」与「最新 id」：
    走两次唯一索引 / 主键索引查找，不构造 ORM 实体（否则两次 query() 的固定开销比查询本身还大）。
    """
    row = db.execute(
        text(
            "SELECT (SELECT value FROM system_configs WHERE key = :k) AS watermark, "
            "(SELECT MAX(id) FROM emby_items) AS max_id"
        ),
        {"k": BACKFILL_KEY},
    ).first()
    if row is None:
        return False
    try:
        watermark = int(row[0]) if row[0] is not None else 0
    except (TypeError, ValueError):
        watermark = 0
    return watermark >= int(row[1] or 0)


def ensure_backfill(db: Session, batch_size: int = BACKFILL_BATCH, commit: bool = True) -> int:
    """给还没有关联行的老条目补齐（按 id 水位增量，内存有界）

    新表刚建出来时（升级上来的老库）一条关联行都没有，筛选会全部落空；启动维护会调它跑完，
    筛选路径发现没跑完也会按预算补一块。每处理完一块推进水位并提交，
    进程被杀也不必从头再来。返回本次处理的条目数。
    """
    total = 0
    while True:
        rows = (
            db.query(em.MediaItem)
            .filter(em.MediaItem.id > _watermark(db))
            .order_by(em.MediaItem.id.asc())
            .limit(batch_size)
            .all()
        )
        if not rows:
            break
        sync_items(db, rows)
        _set_watermark(db, rows[-1].id)
        total += len(rows)
        if commit:
            db.commit()
        if len(rows) < batch_size:
            break
    if total:
        logger.info("分类关联表回填 %d 个条目（水位 → %d）", total, _watermark(db))
    return total


def ensure_ready(db: Session, budget: int = FILTER_BACKFILL_BUDGET) -> bool:
    """筛选前调一次：没回填完就按预算补一块，并回答「这次能不能用关联表」

    补不完也没关系——调用方退回旧的全表 ``ILIKE`` 匹配，结果一样正确，只是慢；
    下一批请求会接着补，直到水位追上最新条目。
    """
    global _last_filter_attempt
    if ready(db):
        return True
    now = time.monotonic()
    if now - _last_filter_attempt < FILTER_BACKFILL_COOLDOWN:
        return False
    with _backfill_lock:
        _last_filter_attempt = now
        try:
            ensure_backfill(db, batch_size=max(1, budget), commit=True)
        except Exception:  # noqa: BLE001 — 回填失败不能影响这次筛选的结果
            logger.exception("分类关联表回填失败（本次退回全表匹配）")
            db.rollback()
            return False
    return ready(db)


def summary(db: Session) -> dict:
    """关联表概览（健康检查 / 测试断言用）"""
    rows = db.query(em.ItemFacet.kind, func.count()).group_by(em.ItemFacet.kind).all()
    return {
        "items": max_item_id(db),
        "backfilled_to": _watermark(db),
        "ready": ready(db),
        "by_kind": {kind: int(count) for kind, count in rows},
    }


# ==================== ORM 事件：任何写入路径都自动同步 ====================
#
# 扫描器（scanner.py）是「一个条目一批地 commit」，后台图片修复与将来可能的
# 「手动改元数据」也会动这几个列。与其在每个调用点加一次 sync_item（漏一处就静默变脏），
# 不如挂在 Session 的 flush 事件上：谁改了条目，谁的事务里就把关联行改好——
# 同一个事务，要么一起成功，要么一起回滚，不存在「条目更新了、关联表还是旧的」的中间态。
#
# 关联行的写入走 session.connection().execute(...)（Core 层），不会再触发 flush，
# 所以不会递归；批量 Core 删除走不到这里，由 prune_orphans 周期兜底。


def _collect_facet_targets(session) -> tuple[list, list[int]]:
    """在 flush 之前挑出：分类可能变了的条目 + 被删除的条目 id

    必须在 ``before_flush`` 里做：flush 之后属性历史会被清掉，就分不出「改过」和「没改过」。
    """
    new_objects = [o for o in session.new if isinstance(o, em.MediaItem)]
    new_ids = {id(o) for o in new_objects}
    # 已有条目：只有分类列真的变了才重建（否则一个「只改了文件路径」的条目也会白重建一遍）
    dirty: list = []
    for obj in session.dirty:
        if not isinstance(obj, em.MediaItem) or id(obj) in new_ids:
            continue
        state = sa_inspect(obj)
        for name in _FACET_COLUMNS:
            attr = state.attrs.get(name)
            if attr is not None and attr.history.has_changes():
                dirty.append(obj)
                break
    deleted_ids = [
        o.id for o in session.deleted if isinstance(o, em.MediaItem) and o.id is not None
    ]
    return new_objects, dirty, deleted_ids


@event.listens_for(Session, "before_flush")
def _facets_before_flush(session, flush_context, instances):  # noqa: ARG001
    new_items, dirty_items, deleted_ids = _collect_facet_targets(session)
    if new_items or dirty_items or deleted_ids:
        session.info["_facet_new"] = new_items
        session.info["_facet_dirty"] = dirty_items
        session.info["_facet_deleted"] = deleted_ids


@event.listens_for(Session, "after_flush")
def _facets_after_flush(session, flush_context):  # noqa: ARG001
    """flush 完（新条目的 id 已经拿到）就写这些条目的关联行，同事务生效

    新条目一定没有关联行，**跳过删除**——扫描器每批都是新条目，这一步是热路径上省下来的
    固定开销（见 scripts/benchmark_item_facets.py 的「写入代价」一节）。
    """
    new_items = session.info.pop("_facet_new", None)
    dirty_items = session.info.pop("_facet_dirty", None)
    deleted_ids = session.info.pop("_facet_deleted", None)
    if not new_items and not dirty_items and not deleted_ids:
        return
    try:
        conn = session.connection()
        if deleted_ids:
            conn.execute(
                em.ItemFacet.__table__.delete().where(em.ItemFacet.item_id.in_(deleted_ids))
            )
            invalidate_values_cache()
        items = [o for o in (new_items or []) + (dirty_items or []) if o.id is not None]
        if items:
            rebuild_ids = [o.id for o in (dirty_items or []) if o.id is not None]
            if rebuild_ids:  # 分类值变过的老条目：先清旧行（新条目不需要）
                conn.execute(
                    em.ItemFacet.__table__.delete().where(
                        em.ItemFacet.item_id.in_(rebuild_ids)
                    )
                )
            rows = [row for item in items for row in _rows_for(item)]
            if rows:
                conn.execute(em.ItemFacet.__table__.insert(), rows)
            invalidate_values_cache()
    except Exception:  # noqa: BLE001 — 关联表是派生数据，写失败不该拖垮业务写入
        logger.exception("同步分类关联表失败（条目本身已写入，可用回填/清理修复）")
