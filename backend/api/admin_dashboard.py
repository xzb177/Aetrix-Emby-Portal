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
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from typing import Any, Callable, Optional

from fastapi import Depends
from sqlalchemy.orm import Session

from backend.api.admin_core import admin_router, get_current_admin
from backend.database import SessionLocal
from backend import models as base_models

logger = logging.getLogger(__name__)

_DASH_TTL_SEC = 20
_dash_cache: dict = {}


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
    """并行采集 8 个 part，返回 {part: payload|None}。"""
    from backend.api.admin import get_playback_stats, get_stats_overview
    from backend.api.admin_economy import economy_stats, get_stats_trend
    from backend.api.admin_services import get_services_status
    from backend.api.realms import realms_overview
    from backend.api.servers import servers_summary
    from backend.emby_server.portal import list_libraries

    # (part 名, 调用器)：lambda 在调用时刻解析模块属性，测试 monkeypatch 依然生效
    jobs: list[tuple[str, Callable[[Any, Session], Any]]] = [
        ("overview", lambda a, d: get_stats_overview(a, d)),
        ("playback", lambda a, d: get_playback_stats(a, d)),
        ("economy", lambda a, d: economy_stats(a, d)),
        ("libraries", lambda a, d: list_libraries(a, d)),
        ("servers", lambda a, d: servers_summary(a, d)),
        ("realms", lambda a, d: realms_overview(a, d)),
        ("backend_services", lambda a, d: get_services_status(a, d)),
        ("trend", lambda a, d: get_stats_trend(days, a, d)),
    ]
    parts: dict[str, Any] = {}
    with ThreadPoolExecutor(max_workers=len(jobs), thread_name_prefix="dashagg") as pool:
        futures = {pool.submit(_call_one, key, fn, admin_id): key for key, fn in jobs}
        for fut in as_completed(futures):
            key, payload = fut.result()
            parts[key] = payload
    return parts


def _cached_collect(admin_id: int, days: int) -> dict:
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
    days = max(1, min(int(days or 14), 90))
    admin_id = current_admin.id
    parts = _cached_collect(admin_id, days)
    return {
        "success": True,
        "fetched_at": datetime.now().isoformat(timespec="seconds"),
        "parts": parts,
    }
