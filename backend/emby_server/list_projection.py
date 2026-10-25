"""媒体库列表投影读模型（FMBY 列表投影读模型，清单第 16 项试点）。

对应魂 2「读写分离」：读侧只读提前算好的投影表；写侧不现场算。
试点只切一个列表面：``user_views``（客户端媒体库列表页，Views）。

机制（与 #1202 仪表盘快照同口径，但落 PG 表而非 Redis——行在库里，
api/ea 任一进程读到的都是同一份，不依赖进程内存）：
- 周期重建：worker 进程的后台线程每 LIST_PROJECTION_REFRESH_SEC
  重算全部启用非虚拟库的三值（条目数/最早入库/封面 tag）并 upsert；
  失败只记警告、不抛、不挡任何请求（见 run_rebuild_loop）。
- 读路径：一次 IN 查询取所需库的投影行；缺行、过期（超过
  LIST_PROJECTION_MAX_STALE_SEC）、读错 → 整包回落现算并记警告，
  绝不混用新旧值、绝不静默出旧数（FMBY #19 实测教训：投影延迟肉眼
  可见，界面不许假装实时；computed_at 即延迟的可观测点）。
- 试点期零写路径改动：不碰扫描、入库、绑定、封面上传的任何代码；
  打脏标记是后续批次的事（届时再接扫描收尾/封面变更的 bump 点）。
- 两处诚实回落（投影故意不用）：
  1. 身份门开时：ChildCount 必须按身份过滤现算（与 user_views 现行
     分支同口径），共享投影行是全量口径，不能用；
  2. 虚拟库：count_virtual_items 是另一套聚合口径，不进投影。

开关（env_util 单一入口，全是 env_int，不新造布尔解析）：
- LIST_PROJECTION_ENABLED：1=读路径走投影（默认开，fail-open 回落
  兜底；0=全站现行行为，投影表照常可由工人刷新、读路径不用）。
- LIST_PROJECTION_REFRESH_SEC：重建周期，默认 120（10~3600）。
- LIST_PROJECTION_MAX_STALE_SEC：行可接受的最老年龄，默认 900
  （60~86400）；超了整包回落现算。
"""

from __future__ import annotations

import logging
import os
import threading
import time
from datetime import datetime

from sqlalchemy import func

from backend.emby_server import models as em
from backend.emby_server import soft_delete
from backend.emby_server.env_util import env_int

logger = logging.getLogger(__name__)

ENABLED = env_int("LIST_PROJECTION_ENABLED", 1, 0, 1)
REFRESH_SEC = env_int("LIST_PROJECTION_REFRESH_SEC", 120, 10, 3600)
MAX_STALE_SEC = env_int("LIST_PROJECTION_MAX_STALE_SEC", 900, 60, 86400)

# user_views 现行口径：只计 movie/series（见 api._library_item_count）。
_COUNT_TYPES = ("movie", "series")

_worker_thread: threading.Thread | None = None
_worker_lock = threading.Lock()


def enabled() -> bool:
    """读路径是否走投影（测试可 monkeypatch 本模块 ENABLED）。"""
    return ENABLED == 1


def _cover_tag(cover_path: str | None) -> str:
    """库封面 tag：与 api._library_cover_tag 同语义（不 import api 防循环）。

    无封面 → "1"；有封面 → 文件 mtime，读不到 → "1"。
    """
    if not cover_path:
        return "1"
    if os.path.isabs(cover_path):
        abs_path = cover_path
    else:
        from backend.emby_server import image_store as _is

        abs_path = os.path.abspath(os.path.join(_is.image_dir(), cover_path))
    try:
        return str(int(os.path.getmtime(abs_path)))
    except OSError:
        return "1"


def _live_child_count(db, lib) -> int:
    """现行非门分支的条目数口径（与 api._library_item_count 非门分支逐字同）。

    item_count 列有值（扫描结束时写入）直接用；否则实时计数。
    """
    if lib.item_count is not None:
        return lib.item_count
    return soft_delete.count_visible(
        db,
        em.MediaItem.library_id == lib.id,
        em.MediaItem.item_type.in_(_COUNT_TYPES),
    )


def _live_earliest_map(db, library_ids: list[int]) -> dict[int, object]:
    """现行最早入库口径（与 user_views 的 need_earliest 查询同形状）。

    软删除可见性由全局钩子作用于 MediaItem 查询自动生效（与现行一致，
    这里不另加显式条件，避免与现行口径漂移）。
    """
    if not library_ids:
        return {}
    rows = (
        db.query(em.MediaItem.library_id, func.min(em.MediaItem.date_added))
        .filter(
            em.MediaItem.library_id.in_(library_ids),
            em.MediaItem.item_type.in_(_COUNT_TYPES),
        )
        .group_by(em.MediaItem.library_id)
        .all()
    )
    return {lid: dt for lid, dt in rows if dt is not None}


def rebuild_projection(db) -> dict:
    """重算全部启用非虚拟库的投影行并 upsert，删掉已失效库的旧行。

    幂等可重跑；调用方（工人/测试）负责提交。返回统计
    {libraries, updated, deleted}，失败由调用方决定（工人只记警告）。
    """
    libs = (
        db.query(em.Library)
        .filter(em.Library.is_enabled == True)  # noqa: E712
        .all()
    )
    target_libs = [lib for lib in libs if not bool(getattr(lib, "is_virtual", False))]
    earliest = _live_earliest_map(db, [lib.id for lib in target_libs])
    existing = {
        row.library_id: row
        for row in db.query(em.LibraryListProjection).all()
    }
    updated = 0
    for lib in target_libs:
        row = existing.get(lib.id)
        if row is None:
            row = em.LibraryListProjection(library_id=lib.id)
            db.add(row)
        row.child_count = _live_child_count(db, lib)
        row.earliest_added = earliest.get(lib.id)
        row.cover_tag = _cover_tag(getattr(lib, "cover_path", None))
        row.computed_at = datetime.now()
        updated += 1
    target_ids = {lib.id for lib in target_libs}
    deleted = 0
    for library_id, row in existing.items():
        if library_id not in target_ids:
            db.delete(row)
            deleted += 1
    return {"libraries": len(target_libs), "updated": updated, "deleted": deleted}


def load_projection(db, library_ids: list[int]) -> dict[int, em.LibraryListProjection] | None:
    """取所需库的投影行；缺行、过期、读错任一 → None（调用方整包回落现算）。

    不抛：读错只记警告。空库 id 列表 → 空 dict（无需回落）。
    """
    if not library_ids:
        return {}
    try:
        rows = (
            db.query(em.LibraryListProjection)
            .filter(em.LibraryListProjection.library_id.in_(library_ids))
            .all()
        )
    except Exception as e:  # noqa: BLE001 — 读错 = 回落信号，不是请求错误
        logger.warning("列表投影读失败，回落现算: %s", e)
        return None
    by_id = {row.library_id: row for row in rows}
    if any(lid not in by_id for lid in library_ids):
        return None
    now = datetime.now()
    for row in by_id.values():
        computed = row.computed_at
        if computed is None:
            return None
        try:
            age = (now - computed).total_seconds()
        except TypeError:
            return None
        if age > MAX_STALE_SEC:
            return None
    return by_id


def refresh_now(session_factory) -> dict | None:
    """同步重建一次（测试与运维触发用）：独立 session，失败返回 None。"""
    db = None
    try:
        db = session_factory()
        stats = rebuild_projection(db)
        db.commit()
        return stats
    except Exception as e:  # noqa: BLE001
        logger.warning("列表投影同步重建失败（保留旧行）: %s", e)
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


def start_projection_worker(stop_event=None) -> bool:
    """启动投影重建工人（worker.py 调用，照 playback_bus_snapshot 的写法）。

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
            daemon=True, name="list-projection-rebuild",
        )
        t.start()
        _worker_thread = t
        return True
