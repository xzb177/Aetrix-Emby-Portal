# -*- coding: utf-8 -*-
"""管理后台仪表盘聚合接口。

背景（2026-10-18 面板体检）：面板前端在 AWS 悉尼、后端在美国 VPS，
每次 API 调用固定多约 1 秒跨洋耗时；仪表盘首屏一次扇出 8 个接口，
最慢一路决定首屏就绪。本接口把 8 路数据一次返回，前端只跑一趟。

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
        "get_stats_trend": lambda a, d: get_stats_trend(days, a, d),
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
