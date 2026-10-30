"""媒体库列表投影读模型（FMBY 列表投影读模型，清单第 16 项试点）。

对应魂 2「读写分离」：读侧只读提前算好的投影表；写侧不现场算。
试点只切一个列表面：``user_views``（客户端媒体库列表页，Views）。

机制（与 #1202 仪表盘快照同口径，但落 PG 表而非 Redis——行在库里，
api/ea 任一进程读到的都是同一份，不依赖进程内存）：
- 周期重建：worker 进程的后台线程每 LIST_PROJECTION_REFRESH_SEC
  重算全部启用非虚拟库的三值（条目数/最早入库/封面 tag）并 upsert；
  失败只记警告、不抛、不挡任何请求（见 run_rebuild_loop）。
- 整库重建走 staging 切换（清单第 20 项）：重建 = build_staging
  （全量新行写 staging 表，live 行一行不动）→ switch_to_staging
  （校验 staging 覆盖全部目标库后，一步删 + 插替换 live 行）。
  覆盖不全时切换直接抛错、live 行原样保留；读侧永远只读 live 表，
  不存在新旧混用的中间态。rebuild_projection 即这两步的组合，
  调用方（工人/测试）负责提交，失败回滚后 live 与 staging 同归旧态。
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

事故注记（2026-10-30，投影冻结 4 天）：生产 SessionLocal 是
``autoflush=False``（backend/database.py），build_staging 用 db.add()
攒的 staging 行在 switch_to_staging 查询前没有落库，切换校验永远
「缺全部目标库」→ 每轮回滚、live 行冻结、staging 恒空；而既有测试用
默认 session（autoflush=True）测不出。故本模块凡「写后同事务再查」
的两处（build 写完、switch 查前）都必须显式 db.flush()，不许依赖
autoflush。失败可见性：refresh_now 累计连续失败次数，达
FAILURE_ALERT_THRESHOLD 后日志升 ERROR；且 health_report 经
projection_health() 把 live 行过期/缺行报成 warn issue，冻结不再
只能翻 worker 日志才知道。
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

#: 连续重建失败达此次数后，refresh_now 的日志从 WARNING 升 ERROR；
#: 跨进程的可见信号走 projection_health → health warn issue（见下）。
FAILURE_ALERT_THRESHOLD = 3

_consecutive_failures = 0
_last_failure_error: str | None = None
_last_failure_at: datetime | None = None


def _note_rebuild_success() -> None:
    global _consecutive_failures, _last_failure_error, _last_failure_at
    _consecutive_failures = 0
    _last_failure_error = None
    _last_failure_at = None


def _note_rebuild_failure(exc: Exception) -> int:
    global _consecutive_failures, _last_failure_error, _last_failure_at
    _consecutive_failures += 1
    _last_failure_error = str(exc)[:300]
    _last_failure_at = datetime.now()
    return _consecutive_failures


def rebuild_failure_snapshot() -> dict:
    """当前连续失败态（运维/测试观察口；进程内计数，重启清零）。"""
    return {
        "consecutive_failures": _consecutive_failures,
        "last_error": _last_failure_error,
        "last_failed_at": _last_failure_at,
    }


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


def _target_libraries(db) -> list:
    """重建目标库：启用非虚拟库（与旧 rebuild_projection 的筛选逐字同）。"""
    libs = (
        db.query(em.Library)
        .filter(em.Library.is_enabled == True)  # noqa: E712
        .all()
    )
    return [lib for lib in libs if not bool(getattr(lib, "is_virtual", False))]


def _compute_rows(db, target_libs: list) -> list[dict]:
    """算出全部目标库的投影值（纯计算，不写任何表）。

    一次构建共用一个 computed_at：同批行的延迟口径一致，读侧的
    过期判定不因逐行时间差抖动。
    """
    earliest = _live_earliest_map(db, [lib.id for lib in target_libs])
    now = datetime.now()
    rows = []
    for lib in target_libs:
        rows.append({
            "library_id": lib.id,
            "child_count": _live_child_count(db, lib),
            "earliest_added": earliest.get(lib.id),
            "cover_tag": _cover_tag(getattr(lib, "cover_path", None)),
            "computed_at": now,
        })
    return rows


def build_staging(db) -> dict:
    """把全量新行写进 staging 表（live 行一行不动）。

    先清空 staging 再写：staging 只代表最近一次构建，不依赖旧内容。
    不提交，调用方负责提交。返回 {libraries, staged}。
    """
    target_libs = _target_libraries(db)
    db.query(em.LibraryListProjectionStaging).delete()
    # 先 flush 再插：staging 表 library_id 有唯一约束，同事务里
    # 删旧行 + 插同库新行时必须保证 DELETE 先落库，否则撞唯一键。
    db.flush()
    rows = _compute_rows(db, target_libs)
    for values in rows:
        db.add(em.LibraryListProjectionStaging(**values))
    # 必须显式 flush：生产 SessionLocal autoflush=False，pending 行不落库
    # 则 switch_to_staging 的查询看不见本轮 staging（2026-10-30 冻结根因）。
    db.flush()
    return {"libraries": len(target_libs), "staged": len(rows)}


def switch_to_staging(db) -> dict:
    """校验 staging 覆盖后，一步把 staging 行替换进 live 表。

    校验：staging 必须覆盖全部当前目标库，缺任一 → 抛 ValueError，
    live 行原样保留（本函数在抛错前不删不写 live 行）。
    切换：删全部 live 行，只把属于目标库的 staging 行拷进 live
    （staging 里多余的非目标行直接丢弃，不复活已停用/已删的库）。
    live 行主键每轮重建会换新（删 + 插），读侧只按 library_id 取，
    无外键指向本表，换新无影响。
    不提交，调用方负责提交。返回 {switched, deleted}，其中 deleted
    = 本轮消失的目标外 live 行数（与旧 rebuild_projection 同口径）。
    """
    # 先 flush 再查：调用方可能刚用 db.add() 写完 staging 行未提交，
    # autoflush=False 的 session 不 flush 就查不到（同 build_staging 注记）。
    db.flush()
    target_libs = _target_libraries(db)
    target_ids = {lib.id for lib in target_libs}
    staged = {row.library_id: row for row in db.query(em.LibraryListProjectionStaging).all()}
    missing = target_ids - set(staged)
    if missing:
        raise ValueError(f"列表投影 staging 覆盖不全，缺库：{sorted(missing)}")
    live_rows = db.query(em.LibraryListProjection).all()
    deleted = sum(1 for row in live_rows if row.library_id not in target_ids)
    for row in live_rows:
        db.delete(row)
    # 先 flush 再插：live 表 library_id 有唯一约束，同事务里删旧行 +
    # 插同库新行时必须保证 DELETE 先落库，否则撞唯一键。
    db.flush()
    switched = 0
    for library_id in sorted(target_ids):
        src = staged[library_id]
        db.add(em.LibraryListProjection(
            library_id=src.library_id,
            child_count=src.child_count,
            earliest_added=src.earliest_added,
            cover_tag=src.cover_tag,
            computed_at=src.computed_at,
        ))
        switched += 1
    return {"switched": switched, "deleted": deleted}


def rebuild_projection(db) -> dict:
    """整库重建：build_staging → switch_to_staging。

    幂等可重跑；调用方（工人/测试）负责提交。返回统计
    {libraries, updated, deleted}，与旧 in-place 重建同口径：
    libraries = 目标库数，updated = 换进 live 的行数，
    deleted = 本轮消失的目标外 live 行数。失败由调用方决定
    （工人只记警告；回滚后 live 行原样保留）。
    """
    built = build_staging(db)
    switched = switch_to_staging(db)
    return {
        "libraries": built["libraries"],
        "updated": switched["switched"],
        "deleted": switched["deleted"],
    }


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


def projection_health(db) -> dict:
    """投影新鲜度快照（health_report 消费；跨进程只读 DB 口径）。

    判定：开关关或无目标库 → 不陈旧。有目标库但 live 无行，或最新
    computed_at 超过 MAX_STALE_SEC（读侧同口径，超了读路径已在整包
    回落现算）→ 陈旧。陈旧本身不代表数字错（回落保证正确），代表
    重建工人已经连续多轮没成、列表在走慢路，必须有人看得见。
    """
    result = {
        "targets": 0, "rows": 0, "newest_computed_at": None,
        "age_sec": None, "stale": False, "reason": None,
    }
    if not enabled():
        return result
    target_ids = {lib.id for lib in _target_libraries(db)}
    result["targets"] = len(target_ids)
    if not target_ids:
        return result
    rows = (
        db.query(em.LibraryListProjection)
        .filter(em.LibraryListProjection.library_id.in_(sorted(target_ids)))
        .all()
    )
    result["rows"] = len(rows)
    newest = max((row.computed_at for row in rows if row.computed_at), default=None)
    result["newest_computed_at"] = newest
    if newest is None:
        result["stale"] = True
        result["reason"] = "no_rows"
        return result
    try:
        age = (datetime.now() - newest).total_seconds()
    except TypeError:
        result["stale"] = True
        result["reason"] = "bad_computed_at"
        return result
    result["age_sec"] = age
    if age > MAX_STALE_SEC:
        result["stale"] = True
        result["reason"] = "expired"
    return result


def refresh_now(session_factory) -> dict | None:
    """同步重建一次（测试与运维触发用）：独立 session，失败返回 None。

    失败累计连续次数：达 FAILURE_ALERT_THRESHOLD 后日志升 ERROR
    （单次抖动仍只是 WARNING），不再静默冻死在警告堆里。
    """
    db = None
    try:
        db = session_factory()
        stats = rebuild_projection(db)
        db.commit()
        _note_rebuild_success()
        return stats
    except Exception as e:  # noqa: BLE001
        count = _note_rebuild_failure(e)
        if count >= FAILURE_ALERT_THRESHOLD:
            logger.error(
                "列表投影同步重建连续失败 %s 次（保留旧行）: %s", count, e)
        else:
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
