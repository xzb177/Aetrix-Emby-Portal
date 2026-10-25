# -*- coding: utf-8 -*-
"""管理后台仪表盘聚合接口。

背景（2026-10-18 面板体检）：面板前端在 AWS 悉尼、后端在美国 VPS，
每次 API 调用固定多约 1 秒跨洋耗时；仪表盘首屏一次扇出多个接口，
最慢一路决定首屏就绪。本接口把各路数据一次返回，前端只跑一趟。
（2026-10-23 新增第 9 路 detail：数据明细 7 表，见 get_detail_tables。）

设计口径（与 #1073 同手法）：
- 内部 ThreadPoolExecutor 并行采集，每个采集器持独立 SessionLocal
 （SQLAlchemy Session 不跨线程共享），互不干扰、独立降级。
- 各 part = 原接口响应**原样透传**（字段一字不变）；单个采集失败时
  该 part 为 null（与前端原 soft() 兜底语义一致），不影响其余。
- 进程内整响应 20s TTL 缓存（面板统计类数据容忍秒级陈旧）；
  loader 异常不缓存。
- 老接口一律保留不动，本接口只做聚合。

鉴权：get_current_admin（与 8 个原接口中最严格者一致）。
"""
from __future__ import annotations

import json
import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError, as_completed
from datetime import datetime
from typing import Any, Callable, Optional

from fastapi import Depends
from sqlalchemy.orm import Session

from backend.api.admin_core import admin_router, get_current_admin
from backend.database import SessionLocal
from backend.database import cache as _kv_cache
from backend.emby_server.env_util import env_int
from backend import models as base_models

logger = logging.getLogger(__name__)

_DASH_TTL_SEC = 20
_DASH_PART_TIMEOUT_SEC = 30
_dash_cache: dict = {}
_dash_lock = threading.Lock()

# (part 名, builder 名)：_collect 在调用时刻用局部 import 解析 builder 名，
# 测试 monkeypatch 目标模块属性时依然生效（与原 lambda 闭包口径一致）。
_JOBS: list[tuple[str, str]] = [
    ("overview", "get_stats_overview"),
    ("playback", "get_playback_stats"),
    ("economy", "economy_stats"),
    ("libraries", "list_libraries"),
    ("servers", "servers_summary"),
    ("realms", "realms_overview"),
    ("backend_services", "get_services_status"),
    ("trend", "get_stats_trend"),
    ("detail", "get_detail_tables"),
]


def _call_one(
    key: str,
    builder: Callable[..., Any],
    admin_id: int,
    **kwargs: Any,
) -> tuple[str, Any]:
    """单个采集：独立 session + 独立身份，失败返回 None。"""
    db: Optional[Session] = None
    try:
        db = SessionLocal()
        admin = db.query(base_models.WebUser).filter(base_models.WebUser.id == admin_id).first()
        if admin is None:
            raise RuntimeError("admin not found")
        return key, builder(admin, db, **kwargs)
    except Exception as e:  # noqa: BLE001
        logger.warning("仪表盘聚合采集失败 [%s]: %s", key, e)
        return key, None
    finally:
        if db is not None:
            try:
                db.close()
            except Exception:  # noqa: BLE001
                pass


def _collect(admin_id: int, days: int) -> dict:
    """并行采集 8 个 part，返回 {part: payload|None}。

    单 part 超时（_DASH_PART_TIMEOUT_SEC）：该 part 降级为 None，
    不拖住整个聚合。超时的线程在后台跑完后由 _call_one 的 finally
    关会话，不泄漏（与 admin_tasks.tasks_overview 同手法：手动建池，
    不用 with 的隐式等待——with 会等住 hang 的线程，超时就失去意义）。
    """
    from backend.api.admin import get_playback_stats, get_stats_overview
    from backend.api.admin_economy import economy_stats, get_stats_trend
    from backend.api.admin_services import get_services_status
    from backend.api.realms import realms_overview
    from backend.api.servers import servers_summary
    from backend.emby_server.portal import list_libraries

    # 局部 import 绑定本地名；lambda 在调用时刻解析，测试 monkeypatch 依然生效。
    # trend 的 days 用闭包传入（_JOBS 在模块级，没有 days）。
    builders: dict[str, Callable[[Any, Session], Any]] = {
        "get_stats_overview": lambda a, d: get_stats_overview(a, d),
        "get_playback_stats": lambda a, d: get_playback_stats(a, d),
        "economy_stats": lambda a, d: economy_stats(a, d),
        "list_libraries": lambda a, d: list_libraries(a, d),
        "servers_summary": lambda a, d: servers_summary(a, d),
        "realms_overview": lambda a, d: realms_overview(a, d),
        "get_services_status": lambda a, d: get_services_status(a, d),
        # trend / detail 两路重聚合走定时快照（见下方 #9 段），其余各路现算
        "get_stats_trend": lambda a, d: _snapshot_or_live(
            "trend", days, "get_stats_trend", a, d),
        "get_detail_tables": lambda a, d: _snapshot_or_live(
            "detail", 0, "get_detail_tables", a, d),
    }
    parts: dict[str, Any] = {}
    ex = ThreadPoolExecutor(max_workers=len(_JOBS), thread_name_prefix="dashagg")
    try:
        futures = {ex.submit(_call_one, key, builders[name], admin_id): key
                   for key, name in _JOBS}
        for fut in as_completed(futures):
            key = futures[fut]
            try:
                _, payload = fut.result(timeout=_DASH_PART_TIMEOUT_SEC)
            except TimeoutError:
                logger.warning("仪表盘聚合采集超时 [%s]（>%ss），已降级为 None",
                               key, _DASH_PART_TIMEOUT_SEC)
                payload = None
            parts[key] = payload
    finally:
        ex.shutdown(wait=False, cancel_futures=True)
    return parts


def _cached_collect(admin_id: int, days: int) -> dict:
    # 单 flight 锁：20s TTL、管理端低频，查缓存→miss 采集→回填全程串行化，
    # 避免缓存击穿瞬间多请求重复并行采集（8 路 × N 请求）。
    with _dash_lock:
        now = time.monotonic()
        cache_key = f"dash:{days}"
        hit = _dash_cache.get(cache_key)
        if hit and hit[0] > now:
            return hit[1]
        parts = _collect(admin_id, days)
        _dash_cache[cache_key] = (now + _DASH_TTL_SEC, parts)
        return parts


# ==================== 定时快照（FMBY 批一 #9） ====================
# 9 路里最重的两路改读快照，其余不动：
# - trend（get_stats_trend）：5 路查询全是按**原始时间戳** GROUP BY 后
#   拉回 Python 再按日聚合，结果集随事件量涨（90 天窗口可达万级行）；
# - detail（get_detail_tables）：_library_rows 对**全 emby_items 表**
#   做 GROUP BY COUNT/SUM，外加 MovieRequest 全表 GROUP BY——扫的是
#   全系统最大的表。
# 其余各路不适合或没必要快照：playback 有 7 天窗口界、economy/overview
# 是 DB 侧单遍 SUM/COUNT（不回传行）、libraries 带目录可读性与扫描进度
# 等实时状态、backend_services 是心跳——实时性优先，保持现算。
#
# 机制（借鉴 FMBY「读只读提前算好的快照」）：
# - 注册过的 (part, days) 由后台线程按 REFRESH 周期重算，落 CacheManager
#   （Redis 共享，进程重启不丢；记录自带 computed_at 快照时间）；
# - 读路径只读快照：新鲜（<= REFRESH）直接用；陈旧但未过 MAX_STALE
#   先用旧的并触发后台刷新（读不等待）；缺失或过 MAX_STALE 才同步
#   算一次兜底——页面永远不会因为快照缺失而空白；
# - 快照只是同一 builder 结果的落点，payload 与原接口逐字段一致；
#   独立端点（/stats/trend 等）不经这里，行为不变。

_SNAPSHOT_REFRESH_SEC = env_int("DASHBOARD_SNAPSHOT_REFRESH_SEC", 120, 10, 3600)
_SNAPSHOT_MAX_STALE_SEC = env_int("DASHBOARD_SNAPSHOT_MAX_STALE_SEC", 900, 60, 86400)
_SNAP_STORE_PREFIX = "dashsnap"

_snap_registry: dict[str, dict] = {}
_snap_registry_lock = threading.Lock()
_snap_key_locks: dict[str, threading.Lock] = {}
_snap_refreshing: set[str] = set()
_snap_thread_started = False


def _snap_store_key(key: str) -> str:
    return f"{_SNAP_STORE_PREFIX}:{key}"


def _snap_load(key: str) -> Optional[dict]:
    """读快照记录 {computed_at, payload}；缺失/损坏一律 None（回退现算）。"""
    try:
        raw = _kv_cache.get(_snap_store_key(key))
        if not raw:
            return None
        rec = json.loads(raw)
        if not isinstance(rec, dict) or "payload" not in rec:
            return None
        rec["computed_at"] = float(rec.get("computed_at") or 0)
        return rec
    except Exception:  # noqa: BLE001
        return None


def _snap_save(key: str, payload: Any, computed_at: Optional[float] = None) -> None:
    """落快照：记录自带 computed_at；存活时长 = 最大陈旧 + 一个刷新周期，
    过了自然消亡（届时读路径按缺失处理、同步算一次）。"""
    try:
        rec = {"computed_at": computed_at if computed_at is not None else time.time(),
               "payload": payload}
        _kv_cache.set(_snap_store_key(key),
                      json.dumps(rec, ensure_ascii=False),
                      ttl=_SNAPSHOT_MAX_STALE_SEC + _SNAPSHOT_REFRESH_SEC)
    except Exception as e:  # noqa: BLE001
        logger.warning("仪表盘快照 [%s] 落盘失败（不影响本次结果）: %s", key, e)


def _snapshot_builder(builder_name: str, days: int, admin: Any, db: Session) -> Any:
    """按 builder 名现算（与 _collect 同口径的调用时局部 import，
    测试 monkeypatch 目标模块属性依然生效）。"""
    if builder_name == "get_stats_trend":
        from backend.api.admin_economy import get_stats_trend
        return get_stats_trend(days, admin, db)
    return get_detail_tables(admin, db)


def _snap_key_lock(key: str) -> threading.Lock:
    with _snap_registry_lock:
        lock = _snap_key_locks.get(key)
        if lock is None:
            lock = threading.Lock()
            _snap_key_locks[key] = lock
        return lock


def _snap_compute(key: str, builder_name: str, days: int, admin: Any,
                  db: Session) -> Any:
    """同步算一次并落快照（per-key 单飞：并发只算一份，其余等锁后读新快照）。"""
    with _snap_key_lock(key):
        rec = _snap_load(key)
        if rec is not None and \
                time.time() - rec["computed_at"] <= _SNAPSHOT_REFRESH_SEC:
            return rec["payload"]
        payload = _snapshot_builder(builder_name, days, admin, db)
        if payload is not None:
            _snap_save(key, payload)
        return payload


def _snap_register(key: str, builder_name: str, days: int, admin_id: int) -> None:
    """把 (part, days) 登记给后台刷新线程（首次登记时顺带把线程拉起来）。"""
    global _snap_thread_started
    start = False
    with _snap_registry_lock:
        _snap_registry[key] = {
            "builder": builder_name, "days": days, "admin_id": admin_id,
        }
        if not _snap_thread_started:
            _snap_thread_started = True
            start = True
    if start:
        threading.Thread(target=_snap_refresher_loop, name="dash-snapshot",
                         daemon=True).start()


def _refresh_snapshot_now(key: str) -> None:
    """同步刷新一条快照：独立 session + 登记时的管理员身份（与 _call_one
    同手法）；失败只告警、保留旧快照。管理员已不存在则注销登记。"""
    with _snap_registry_lock:
        spec = _snap_registry.get(key)
    if spec is None:
        return
    db: Optional[Session] = None
    try:
        db = SessionLocal()
        admin = db.query(base_models.WebUser).filter(
            base_models.WebUser.id == spec["admin_id"]).first()
        if admin is None:
            with _snap_registry_lock:
                _snap_registry.pop(key, None)
            return
        _snap_compute(key, spec["builder"], spec["days"], admin, db)
    except Exception as e:  # noqa: BLE001
        logger.warning("仪表盘快照 [%s] 后台刷新失败，保留旧快照: %s", key, e)
    finally:
        if db is not None:
            try:
                db.close()
            except Exception:  # noqa: BLE001
                pass


def _snap_schedule_refresh(key: str) -> None:
    """触发一次后台刷新（同 key 单飞：已在刷新就不再起线程）。"""
    with _snap_registry_lock:
        if key in _snap_refreshing or key not in _snap_registry:
            return
        _snap_refreshing.add(key)

    def _run() -> None:
        try:
            _refresh_snapshot_now(key)
        finally:
            with _snap_registry_lock:
                _snap_refreshing.discard(key)

    threading.Thread(target=_run, name="dash-snapshot-one",
                     daemon=True).start()


def _refresh_all_snapshots() -> None:
    """把已过新鲜期的登记快照逐条排上后台刷新（单条失败不影响其余）。"""
    now = time.time()
    with _snap_registry_lock:
        keys = list(_snap_registry)
    for key in keys:
        rec = _snap_load(key)
        if rec is not None and now - rec["computed_at"] < _SNAPSHOT_REFRESH_SEC:
            continue
        _snap_schedule_refresh(key)


def _snap_refresher_loop() -> None:
    while True:
        time.sleep(max(5, _SNAPSHOT_REFRESH_SEC))
        try:
            _refresh_all_snapshots()
        except Exception as e:  # noqa: BLE001
            logger.warning("仪表盘快照刷新轮失败: %s", e)


def _snapshot_or_live(part: str, days: int, builder_name: str, admin: Any,
                      db: Session) -> Any:
    """快照读路径（见上方机制说明）。builder 抛错且有旧快照时先用旧的，
    没有旧快照则原样抛（_call_one 照旧降级为 None）。"""
    key = f"{part}:{days}"
    _snap_register(key, builder_name, days, admin.id)
    rec = _snap_load(key)
    if rec is not None:
        age = time.time() - rec["computed_at"]
        if age <= _SNAPSHOT_REFRESH_SEC:
            return rec["payload"]
        if age <= _SNAPSHOT_MAX_STALE_SEC:
            _snap_schedule_refresh(key)
            return rec["payload"]
    try:
        return _snap_compute(key, builder_name, days, admin, db)
    except Exception:
        if rec is not None:
            logger.warning("仪表盘快照 [%s] 同步重算失败，先用旧快照", key)
            return rec["payload"]
        raise


def _reset_snapshots() -> None:
    """测试用：清登记表与已存快照（后台线程不停，登记为空时它空转）。"""
    with _snap_registry_lock:
        keys = list(_snap_registry)
        _snap_registry.clear()
        _snap_refreshing.clear()
    for key in keys:
        try:
            _kv_cache.delete(_snap_store_key(key))
        except Exception:  # noqa: BLE001
            pass


@admin_router.get("/dashboard/overview")
def dashboard_overview(
    days: int = 14,
    current_admin: base_models.WebUser = Depends(get_current_admin),
):
    """仪表盘首屏聚合：一次返回 8 路数据。

    - ``days``：趋势图天数（1–90，原 /stats/trend 口径，内部钳制）。
    - ``parts``：各原接口响应的原样映射，采集失败的 part 为 null。
    - 老接口全部保留，本接口失败时前端回退逐个调用。
    """
    days = max(1, min(int(days), 90))
    admin_id = current_admin.id
    parts = _cached_collect(admin_id, days)
    return {
        "success": True,
        "fetched_at": datetime.now().isoformat(timespec="seconds"),
        "parts": parts,
    }


# ==================== 数据明细（2026-10-23 用户要求补表） ====================
# 仪表盘原先只有播放排行/热门内容两张表，用户反馈「数据表有点少」。
# 这里一次给齐 7 张明细表，前端集中在一个「数据明细」区页签切换，
# 不往页面各处铺。全部走既有数据的有界查询（每表 ≤ 14 行），
# 作为聚合接口的第 9 个 part（detail）随 overview 一次返回。

_DETAIL_ROW_LIMIT = 10


def _iso(dt: Any) -> str:
    return dt.isoformat(timespec="seconds") if hasattr(dt, "isoformat") else ""


def get_detail_tables(admin: Any, db: Session) -> dict:
    """7 张仪表盘明细表（见上）。单表查询失败不拖垮其余表：调用方
    (_call_one) 已有整 part 降级；这里对每张表再兜一层，坏表返回空列表。"""
    from datetime import timedelta

    from sqlalchemy import case, func

    from backend import models
    from backend.emby_server import models as em

    out: dict[str, list] = {}

    def _safe(key: str, fn: Callable[[], list]) -> None:
        try:
            out[key] = fn()
        except Exception as e:  # noqa: BLE001
            logger.warning("仪表盘明细表 [%s] 采集失败: %s", key, e)
            out[key] = []

    def _recent_users() -> list:
        rows = (
            db.query(models.WebUser)
            .order_by(models.WebUser.created_at.desc(), models.WebUser.id.desc())
            .limit(_DETAIL_ROW_LIMIT)
            .all()
        )
        return [
            {
                "username": u.username,
                "created_at": _iso(u.created_at),
                "channel": u.register_channel or "",
                "is_welfare": bool(u.is_welfare),
            }
            for u in rows
        ]

    def _recent_orders() -> list:
        merged: list[dict] = []
        subs = (
            db.query(models.SubscriptionOrder, models.WebUser.username)
            .join(models.WebUser, models.WebUser.id == models.SubscriptionOrder.user_id)
            .order_by(models.SubscriptionOrder.created_at.desc())
            .limit(_DETAIL_ROW_LIMIT)
            .all()
        )
        for o, uname in subs:
            merged.append({
                "username": uname,
                "item": o.item_name or "订阅",
                "kind": "订阅",
                "amount": float(o.amount or 0),
                "status": o.status or "",
                "created_at": _iso(o.created_at),
            })
        recs = (
            db.query(models.RechargeOrder, models.WebUser.username,
                     models.RechargePackage.name)
            .join(models.WebUser, models.WebUser.id == models.RechargeOrder.user_id)
            .outerjoin(models.RechargePackage,
                       models.RechargePackage.id == models.RechargeOrder.package_id)
            .order_by(models.RechargeOrder.created_at.desc())
            .limit(_DETAIL_ROW_LIMIT)
            .all()
        )
        for o, uname, pkg_name in recs:
            merged.append({
                "username": uname,
                "item": pkg_name or "能量充值",
                "kind": "充值",
                "amount": float(o.price or 0),
                "status": o.status or "",
                "created_at": _iso(o.created_at),
            })
        merged.sort(key=lambda r: r["created_at"], reverse=True)
        return merged[:_DETAIL_ROW_LIMIT]

    def _library_rows() -> list:
        # 内容统计与刮削进度同出一趟分组查询（ORM 口径：软删除由全局
        # 钩子过滤，合并副本显式排除，与队列口径一致）
        rows = (
            db.query(
                em.MediaItem.library_id,
                em.MediaItem.item_type,
                func.count(em.MediaItem.id),
                func.sum(case(
                    (em.MediaItem.tmdb_id.isnot(None) & (em.MediaItem.tmdb_id != ""), 1),
                    else_=0)),
            )
            .filter(em.MediaItem.merged_into_id.is_(None))
            .group_by(em.MediaItem.library_id, em.MediaItem.item_type)
            .all()
        )
        lib_names = {l.id: l.name for l in db.query(em.Library).all()}
        per_lib: dict[int, dict] = {}
        for lib_id, item_type, cnt, with_tmdb in rows:
            entry = per_lib.setdefault(lib_id, {
                "library": lib_names.get(lib_id, f"库 {lib_id}"),
                "movies": 0, "shows": 0, "episodes": 0, "total": 0,
                "enriched": 0, "pending": 0,
            })
            cnt = int(cnt or 0)
            entry["total"] += cnt
            if item_type == "movie":
                entry["movies"] += cnt
                entry["enriched"] += int(with_tmdb or 0)
            elif item_type == "series":
                entry["shows"] += cnt
                entry["enriched"] += int(with_tmdb or 0)
            elif item_type == "episode":
                entry["episodes"] += cnt
        for entry in per_lib.values():
            entry["pending"] = entry["movies"] + entry["shows"] - entry["enriched"]
        return sorted(per_lib.values(), key=lambda r: r["total"], reverse=True)

    _library_cache: list = []

    def _library_stats() -> list:
        _library_cache.extend(_library_rows())
        return [
            {k: r[k] for k in ("library", "movies", "shows", "episodes", "total")}
            for r in _library_cache
        ]

    def _enrich_progress() -> list:
        rows = _library_cache or _library_rows()
        return [
            {k: r[k] for k in ("library", "enriched", "pending")}
            for r in rows
        ]

    def _points_flow() -> list:
        rows = (
            db.query(models.PointsLog, models.WebUser.username)
            .join(models.WebUser, models.WebUser.id == models.PointsLog.user_id)
            .order_by(models.PointsLog.created_at.desc(), models.PointsLog.id.desc())
            .limit(_DETAIL_ROW_LIMIT)
            .all()
        )
        return [
            {
                "username": uname,
                "type": log.type or "",
                "amount": int(log.amount or 0),
                "description": log.description or "",
                "created_at": _iso(log.created_at),
            }
            for log, uname in rows
        ]

    def _seek_hot() -> list:
        rows = (
            db.query(
                models.MovieRequest.movie_name,
                models.MovieRequest.type,
                func.count(models.MovieRequest.id),
                func.sum(case((models.MovieRequest.status == "pending", 1), else_=0)),
            )
            .group_by(models.MovieRequest.movie_name, models.MovieRequest.type)
            .order_by(func.count(models.MovieRequest.id).desc())
            .limit(_DETAIL_ROW_LIMIT)
            .all()
        )
        return [
            {
                "title": name,
                "media_type": mtype or "",
                "count": int(cnt or 0),
                "pending": int(pending or 0),
            }
            for name, mtype, cnt, pending in rows
        ]

    def _activity_days() -> list:
        today = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
        start = today - timedelta(days=6)
        play_rows = (
            db.query(func.date(em.PlaybackSession.start_time),
                     func.count(em.PlaybackSession.id),
                     func.count(func.distinct(em.PlaybackSession.user_id)))
            .filter(em.PlaybackSession.start_time >= start)
            .group_by(func.date(em.PlaybackSession.start_time))
            .all()
        )
        plays = {str(d): (int(c or 0), int(u or 0)) for d, c, u in play_rows}
        news = {
            str(d): int(c or 0)
            for d, c in db.query(func.date(models.WebUser.created_at),
                                 func.count(models.WebUser.id))
            .filter(models.WebUser.created_at >= start)
            .group_by(func.date(models.WebUser.created_at))
            .all()
        }
        days = []
        for i in range(7):
            day = start + timedelta(days=i)
            key = day.strftime("%Y-%m-%d")
            play_count, active_users = plays.get(key, (0, 0))
            days.append({
                "date": key,
                "active_users": active_users,
                "plays": play_count,
                "new_users": news.get(key, 0),
            })
        return days

    _safe("recent_users", _recent_users)
    _safe("recent_orders", _recent_orders)
    _safe("library_stats", _library_stats)
    _safe("enrich_progress", _enrich_progress)
    _safe("points_flow", _points_flow)
    _safe("seek_hot", _seek_hot)
    _safe("activity_days", _activity_days)
    return out
