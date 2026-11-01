"""条目排序投影（媒体库学习 L5）。

一行 = 一个活条目（movie / series）的排序键：title_key（中文按拼音，
归一化口径与搜索共用 pinyin_sort.pinyin_full）与 year_key
（production_year，空 = NULL，排序时 nulls last）。读侧只读本表
（游标分页），写侧由周期重建工人增量刷新：缺行补、键变化（改名/
改年/换库）重算，不碰其它写路径。注意 MediaItem 没有 updated_at 列，
脏检查只认键比较——键的全部输入就是 name / production_year /
library_id，键不变就不用重算。

回落口径照 #1235（list_projection）：覆盖不全/页内行过期/读错一律
回落现算并记警告，绝不静默出旧数。开关 ITEM_CURSOR_PAGINATION_ENABLED
（默认 0）：关时游标分页口直接 404（新口，无旧口可保）；开时读投影、
回落现算。游标是不透明字符串（base64url 编码的 JSON），客户端只透传。

排序语义（游标条件与 ORDER BY 严格同序，否则翻页会丢/重）：
- title：title_key 升序，tie-break item_id 升序（title_key 永不 NULL）。
- year：year_key 降序、NULL 最后，tie-break item_id 升序。游标在
  非 NULL 年时，后续 =（更小年）或（同年更大 id）或（NULL 年）；
  游标已在 NULL 段时，后续 =（NULL 年且更大 id）。
"""

from __future__ import annotations

import base64
import json
import logging
import threading
import time
from datetime import datetime

from sqlalchemy import func

from backend.emby_server import models as em
from backend.emby_server.env_util import env_int
from backend.emby_server.pinyin_sort import pinyin_full

logger = logging.getLogger(__name__)

ENABLED = env_int("ITEM_CURSOR_PAGINATION_ENABLED", 0, 0, 1)
REFRESH_SEC = env_int("ITEM_SORT_PROJECTION_REFRESH_SEC", 300, 10, 3600)
MAX_STALE_SEC = env_int("ITEM_SORT_PROJECTION_MAX_STALE_SEC", 900, 60, 86400)

_SORT_TYPES = ("movie", "series")
_SORTS = ("title", "year")

_worker_thread: threading.Thread | None = None
_worker_lock = threading.Lock()


def enabled() -> bool:
    """游标分页口是否开启（测试可 monkeypatch 本模块 ENABLED）。"""
    return ENABLED == 1


def compute_keys(item) -> tuple[str, int | None]:
    """由条目现算排序键（重建与回落共用，逐字一致）。"""
    title_key = pinyin_full(getattr(item, "name", None) or "")
    year = getattr(item, "production_year", None)
    year_key = int(year) if year is not None else None
    return title_key, year_key


def encode_cursor(sort: str, key, item_id: int) -> str:
    """游标编码：{"s": sort, "k": key, "i": item_id} 的 base64url。"""
    payload = json.dumps({"s": sort, "k": key, "i": int(item_id)},
                         separators=(",", ":"), ensure_ascii=False)
    return base64.urlsafe_b64encode(payload.encode("utf-8")).decode("ascii")


def decode_cursor(raw: str) -> tuple[str, object, int]:
    """游标解码；非法 → ValueError（调用方转 400，不静默回第一页）。

    key 的类型由 sort 决定：title → str，year → int 或 None。
    """
    try:
        payload = json.loads(base64.urlsafe_b64decode(raw.encode("ascii")).decode("utf-8"))
        sort, key, item_id = payload["s"], payload["k"], int(payload["i"])
    except Exception as e:  # noqa: BLE001 — 任何解码失败都是非法游标
        raise ValueError(f"非法游标：{raw!r}") from e
    if sort not in _SORTS:
        raise ValueError(f"非法游标排序：{sort!r}")
    if sort == "title" and not isinstance(key, str):
        raise ValueError("title 游标的 key 必须是字符串")
    if sort == "year" and key is not None and not isinstance(key, int):
        raise ValueError("year 游标的 key 必须是整数或 null")
    return sort, key, item_id


def _live_items_query(db, library_id: int):
    """活条目查询（movie / series，未软删、未合并、未隐藏；软删除钩子
    另会自动过滤 deleted_at，这里显式写出 merged 口径，见数据库口径。
    is_hidden 由 PA-011 补上：重建与现算回落同源，旧投影行由重建的
    删失效行逻辑清掉）。"""
    return (
        db.query(em.MediaItem)
        .filter(
            em.MediaItem.library_id == library_id,
            em.MediaItem.item_type.in_(_SORT_TYPES),
            em.MediaItem.merged_into_id.is_(None),
            em.MediaItem.is_hidden == False,  # noqa: E712
        )
    )


def rebuild_projection(db) -> dict:
    """增量重建全部启用非虚拟库的排序投影行并 upsert，删掉失效行。

    幂等可重跑；调用方（工人/测试）负责提交。返回统计
    {libraries, updated, deleted}，失败由调用方决定（工人只记警告）。
    """
    libs = (
        db.query(em.Library)
        .filter(em.Library.is_enabled == True)  # noqa: E712
        .all()
    )
    target_libs = [lib for lib in libs if not bool(getattr(lib, "is_virtual", False))]
    existing = {row.item_id: row for row in db.query(em.MediaItemSortProjection).all()}
    updated = 0
    live_ids: dict[int, set[int]] = {}
    now = datetime.now()
    for lib in target_libs:
        items = _live_items_query(db, lib.id).all()
        live_ids[lib.id] = set()
        for item in items:
            live_ids[lib.id].add(item.id)
            title_key, year_key = compute_keys(item)
            row = existing.get(item.id)
            if row is None:
                row = em.MediaItemSortProjection(item_id=item.id)
                db.add(row)
                existing[item.id] = row
                row.library_id = lib.id
                row.title_key = title_key
                row.year_key = year_key
                row.computed_at = now
                updated += 1
                continue
            # 脏检查只认键比较（见模块 docstring：MediaItem 无 updated_at）。
            dirty = (
                row.library_id != lib.id
                or row.title_key != title_key
                or row.year_key != year_key
            )
            if dirty:
                row.library_id = lib.id
                row.title_key = title_key
                row.year_key = year_key
                row.computed_at = now
                updated += 1
    target_ids = {lib.id for lib in target_libs}
    deleted = 0
    for item_id, row in list(existing.items()):
        if row.library_id not in target_ids or item_id not in live_ids.get(row.library_id, set()):
            db.delete(row)
            deleted += 1
    return {"libraries": len(target_libs), "updated": updated, "deleted": deleted}


def _projection_page(db, library_id: int, sort: str, cursor_key, cursor_id, limit: int):
    """投影读一页：返回 ([(row, item)], has_more)；覆盖不全/页内行过期/
    读错 → None（调用方回落现算）。"""
    try:
        live_count = _live_items_query(db, library_id).count()
        proj_count = (
            db.query(func.count(em.MediaItemSortProjection.item_id))
            .filter(em.MediaItemSortProjection.library_id == library_id)
            .scalar()
        )
    except Exception as e:  # noqa: BLE001 — 读错 = 回落信号，不是请求错误
        logger.warning("条目排序投影计数读失败，回落现算: %s", e)
        return None
    if live_count != proj_count:
        logger.warning(
            "条目排序投影覆盖不全（库 %s：活条目 %s 行、投影 %s 行），回落现算",
            library_id, live_count, proj_count,
        )
        return None
    q = (
        db.query(em.MediaItemSortProjection, em.MediaItem)
        .join(em.MediaItem, em.MediaItem.id == em.MediaItemSortProjection.item_id)
        .filter(
            em.MediaItemSortProjection.library_id == library_id,
            em.MediaItem.item_type.in_(_SORT_TYPES),
            em.MediaItem.merged_into_id.is_(None),
        )
    )
    if sort == "title":
        if cursor_key is not None:
            q = q.filter(
                (em.MediaItemSortProjection.title_key > cursor_key)
                | (
                    (em.MediaItemSortProjection.title_key == cursor_key)
                    & (em.MediaItemSortProjection.item_id > cursor_id)
                )
            )
        q = q.order_by(
            em.MediaItemSortProjection.title_key.asc(),
            em.MediaItemSortProjection.item_id.asc(),
        )
    else:
        if cursor_key is None and cursor_id is not None:
            q = q.filter(
                em.MediaItemSortProjection.year_key.is_(None),
                em.MediaItemSortProjection.item_id > cursor_id,
            )
        elif cursor_key is not None:
            q = q.filter(
                (em.MediaItemSortProjection.year_key < cursor_key)
                | (
                    (em.MediaItemSortProjection.year_key == cursor_key)
                    & (em.MediaItemSortProjection.item_id > cursor_id)
                )
                | (em.MediaItemSortProjection.year_key.is_(None))
            )
        q = q.order_by(
            em.MediaItemSortProjection.year_key.desc().nullslast(),
            em.MediaItemSortProjection.item_id.asc(),
        )
    try:
        rows = q.limit(limit + 1).all()
    except Exception as e:  # noqa: BLE001
        logger.warning("条目排序投影分页读失败，回落现算: %s", e)
        return None
    has_more = len(rows) > limit
    page = rows[:limit]
    now = datetime.now()
    for row, _item in page:
        computed = row.computed_at
        if computed is None:
            logger.warning("条目排序投影行 %s 无 computed_at，回落现算", row.item_id)
            return None
        try:
            age = (now - computed).total_seconds()
        except TypeError:
            return None
        if age > MAX_STALE_SEC:
            logger.warning(
                "条目排序投影行 %s 已过期（%ss），回落现算", row.item_id, round(age),
            )
            return None
    return page, has_more


def live_page(db, library_id: int, sort: str, cursor_key, cursor_id, limit: int):
    """回落现算一页：全库活条目现算键、内存排序、游标切片。

    回落是异常路径（工人滞后时），全量取数可接受；正常路径走投影。
    返回 ([MediaItem], next_cursor|None)。
    """
    items = _live_items_query(db, library_id).all()
    keyed = []
    for item in items:
        title_key, year_key = compute_keys(item)
        keyed.append((title_key, year_key, item))
    if sort == "title":
        keyed.sort(key=lambda t: (t[0], t[2].id))
        if cursor_key is not None:
            keyed = [t for t in keyed if (t[0], t[2].id) > (cursor_key, cursor_id)]
        page = keyed[:limit]
        has_more = len(keyed) > limit
        next_cursor = (
            encode_cursor("title", page[-1][0], page[-1][2].id)
            if has_more and page
            else None
        )
    else:
        def _year_order(t):
            # year 降序、NULL 最后：NULL 排在所有非 NULL 之后
            return (t[1] is None, -(t[1] or 0), t[2].id)

        keyed.sort(key=_year_order)
        if cursor_key is None and cursor_id is not None:
            # 游标已在 NULL 段：后续 = NULL 年且更大 id
            keyed = [t for t in keyed if t[1] is None and t[2].id > cursor_id]
        elif cursor_key is not None:
            # 游标在非 NULL 年：后续 = 排序键大于游标键的行（更小年、
            # 同年更大 id、或 NULL 年——NULL 的排序键恒大于任何非 NULL）
            cursor_order = (False, -cursor_key, cursor_id)
            keyed = [t for t in keyed if _year_order(t) > cursor_order]
        page = keyed[:limit]
        has_more = len(keyed) > limit
        next_cursor = (
            encode_cursor("year", page[-1][1], page[-1][2].id)
            if has_more and page
            else None
        )
    return [t[2] for t in page], next_cursor


def load_page(db, library_id: int, sort: str, cursor_key, cursor_id, limit: int):
    """读一页：投影优先，投影不可用时回落现算（并记警告）。

    返回 ([MediaItem], next_cursor|None)。
    """
    projected = _projection_page(db, library_id, sort, cursor_key, cursor_id, limit)
    if projected is not None:
        page, has_more = projected
        items = [item for _row, item in page]
        if not has_more or not page:
            return items, None
        last_row, last_item = page[-1]
        key = last_row.title_key if sort == "title" else last_row.year_key
        return items, encode_cursor(sort, key, last_item.id)
    return live_page(db, library_id, sort, cursor_key, cursor_id, limit)


def refresh_now(session_factory) -> dict | None:
    """同步重建一次（测试与运维触发用）：独立 session，失败返回 None。"""
    db = None
    try:
        db = session_factory()
        stats = rebuild_projection(db)
        db.commit()
        return stats
    except Exception as e:  # noqa: BLE001
        logger.warning("条目排序投影同步重建失败（保留旧行）: %s", e)
        if db is not None:
            try:
                db.rollback()
            except Exception:  # noqa: BLE001
                pass
        return None
    finally:
        if db is not None:
            try:
                db.close()
            except Exception:  # noqa: BLE001
                pass


def run_rebuild_loop(session_factory, stop_event=None) -> None:
    """工人循环：每 REFRESH_SEC 重建一次；单次失败只记警告、不停循环。"""
    while True:
        if stop_event is not None and stop_event.is_set():
            return
        refresh_now(session_factory)
        if stop_event is not None:
            if stop_event.wait(REFRESH_SEC):
                return
        else:
            time.sleep(REFRESH_SEC)


def start_sort_projection_worker(stop_event=None) -> bool:
    """启动排序投影重建工人（worker.py 调用，照 list_projection 的写法）。

    开关关时返回 False、不启动；幂等：已有存活线程时直接返回 True。
    """
    global _worker_thread
    if not enabled():
        return False
    with _worker_lock:
        alive = _worker_thread
        if alive is not None and alive.is_alive():
            return True
        from backend.database import SessionLocal

        t = threading.Thread(
            target=run_rebuild_loop,
            args=(SessionLocal,),
            kwargs={"stop_event": stop_event},
            daemon=True, name="item-sort-projection-rebuild",
        )
        t.start()
        _worker_thread = t
        return True
