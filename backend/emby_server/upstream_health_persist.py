# -*- coding: utf-8 -*-
"""上游健康落库：Redis 状态 → PG health_status（FMBY 学习 #25 清单第 14 项）。

## 为什么需要这一层（25-3 遗留的真不一致）

- 25-3 的 ``upstream_health`` 只写 Redis，状态值是 FMBY 实测的
  ``"Active"`` / ``"Unavailable"``；
- 25-1 的 PG ``upstream_sources.health_status`` 允许集是
  ``("unknown", "healthy", "unhealthy")``（见 models 的全仓唯一定义点）。
两者值域不同，直接搬运会撞 CHECK 约束。本模块是两者之间**唯一**的
翻译与同步口：翻译函数单点，不许各处散写映射。

## 翻译表（唯一口径，定死在这里）

- Redis ``"Active"``      → PG ``"healthy"``
- Redis ``"Unavailable"`` → PG ``"unhealthy"``
- 无记录（``None``）      → PG ``"unknown"``
- 其它未知值             → PG ``"unknown"`` + warning（绝不把未在允许集
  里的值写进 PG；CHECK 约束是最后一道防线，不是第一道）

``"unknown"`` 的语义是“我方不知道”，不是“源死了”：它不阻塞任何
消费方——25-4 的调度过滤读的是 Redis 的 Active 语义，PG 值目前
没有任何调度消费。

## 同步语义（钉死，不许悄悄改）

- 只写变化：目标值与 PG 现值相等时不动（``updated_at`` 不 churn）；
  变化时走 ``upstream_sources.set_health_status``（魂 6 唯一写入口，
  不许自己拼 UPDATE）。PG 的 ``last_checked_at`` 由该函数刷新为
  写入时刻——即它是“上次状态写入时间”，状态不变时不刷新；
  探活节律看 Redis 记录的 ``last_checked_at``，别拿 PG 这一列当探活心跳。
- 无记录当 unknown：PG 行在 Redis 里没有记录 → 写 unknown。
- Redis 读不到与无记录同值：``RedisHealthStore`` 读失败返回 None
  （25-3 的 fail-open），同步无法区分“Redis 挂了”与“真没记录”，
  两者都落 unknown；Redis 恢复后下轮同步自动纠正。绝不因此把源
  判 unhealthy。
- 失败隔离：单行 PG 写失败、单行 Redis 读失败只记 warning、计入
  errors，不中断其余行；整个同步不抛异常（调用方是后台线程，
  抛异常只会杀线程）。
- 开关：``UPSTREAM_HEALTH_PG_SYNC_ENABLED`` 默认 0；关时
  ``sync_health_to_pg`` 直接返回 disabled，不读 Redis、不写 PG。

## 不碰的东西

- 不碰 25-3：本模块只读 ``store.get_source``，不调 probe、不改
  判定语义、不改 Redis 值。25-3 的工人与本模块的同步是两条独立链路。
- 不改 worker.py：周期守护线程须显式调用才跑；生产 worker 接线等
  25-3 探活守护接线之后再做（探活本身未接线时同步无记录可搬），
  本 PR 不给 worker 加任何启动块。
- 不碰凭据：同步只搬状态值，不读、不写、不记任何密封凭据。
"""

from __future__ import annotations

import logging
import threading
from typing import Optional

from backend.emby_server import upstream_health as uh
from backend.emby_server import upstream_sources as ups
from backend.emby_server.env_util import env_float

logger = logging.getLogger(__name__)

#: PG 健康状态允许集（只读 models 的唯一定义点，不许在此另起一套）。
PG_UNKNOWN = "unknown"
PG_HEALTHY = "healthy"
PG_UNHEALTHY = "unhealthy"

#: Redis 键约定：25-3 的 UpstreamSource.key 在 25-1 落地后对齐为
#: 源 id 的字符串形态（见 upstream_health.UpstreamSource docstring）。
#: 非数字键（25-1 之前的遗留键）在 PG 里没有对应行，同步时跳过并警告。
def redis_key_for_source(source) -> str:
    return str(source.id)


def translate_redis_state(state: Optional[str]) -> str:
    """Redis 状态 → PG health_status（本文件唯一的翻译口）。

    - ``"Active"`` → ``"healthy"``；``"Unavailable"`` → ``"unhealthy"``；
    - ``None``（无记录）→ ``"unknown"``；
    - 其它值 → ``"unknown"`` + warning，绝不输出允许集外的值。
    """
    if state == uh.STATE_ACTIVE:
        return PG_HEALTHY
    if state == uh.STATE_UNAVAILABLE:
        return PG_UNHEALTHY
    if state is None:
        return PG_UNKNOWN
    logger.warning("上游健康状态翻译：未知 Redis 状态 %r，按 unknown 计", state)
    return PG_UNKNOWN


def sync_enabled() -> bool:
    """落库同步总开关，默认关（与探活总开关同口径的真值判断）。"""
    import os
    return (str(os.getenv("UPSTREAM_HEALTH_PG_SYNC_ENABLED", "0") or "0")
            .strip().lower() not in {"0", "false", "no", "off"})


def sync_interval_sec() -> float:
    """周期同步间隔（秒）。默认值 60 与探活周期同口径，我方起步值。"""
    return env_float("UPSTREAM_HEALTH_PG_SYNC_INTERVAL_SEC", 60.0, 1.0, 86400.0)


def sync_health_to_pg(db, store) -> dict:
    """一次性回填/同步：把全部 PG 源行的 Redis 状态翻译后写回 PG。

    入参 ``db`` 是 SQLAlchemy 会话，``store`` 是 HealthStore
    （生产用 ``RedisHealthStore``，测试用 ``InMemoryHealthStore``）。
    返回计数字典：``disabled/total/updated/unchanged/no_record/errors``。
    关时返回 ``disabled=True`` 且其余计数全 0，不读 Redis、不写 PG。
    本函数不抛异常：源列表读失败、单行读写失败都只记 warning。
    """
    if not sync_enabled():
        return {"disabled": True, "total": 0, "updated": 0,
                "unchanged": 0, "no_record": 0, "errors": 0}
    counts = {"disabled": False, "total": 0, "updated": 0,
              "unchanged": 0, "no_record": 0, "errors": 0}
    try:
        rows = ups.list_sources(db)
    except Exception:
        logger.warning("上游健康同步：读源列表失败", exc_info=True)
        counts["errors"] += 1
        return counts
    for row in rows:
        counts["total"] += 1
        try:
            record = store.get_source(redis_key_for_source(row))
        except Exception:
            logger.warning("上游健康同步：源 id=%s 读 Redis 失败，已跳过",
                           row.id, exc_info=True)
            counts["errors"] += 1
            continue
        if record is None:
            counts["no_record"] += 1
            target = PG_UNKNOWN
        else:
            target = translate_redis_state(record.availability_state)
        if target == row.health_status:
            counts["unchanged"] += 1
            continue
        try:
            ups.set_health_status(db, row, target)
            counts["updated"] += 1
        except Exception:
            logger.warning("上游健康同步：源 id=%s 写 PG 失败，已跳过",
                           row.id, exc_info=True)
            counts["errors"] += 1
    return counts


def start_health_sync_daemon(session_factory=None, store=None,
                             *, interval_sec: Optional[float] = None,
                             stop_event: Optional[threading.Event] = None):
    """启动周期同步守护线程（必须显式调用；import 时绝不自动启动）。

    - ``session_factory``：无参可调用，返回新会话；默认懒加载
      ``backend.database.SessionLocal``（import 时不碰 DB）。
    - ``store``：默认 ``RedisHealthStore``（懒加载 get_redis 在首次
      get/set 时发生，不在建 store 时发生）。
    - ``interval_sec``：不传则读 ``UPSTREAM_HEALTH_PG_SYNC_INTERVAL_SEC``。
    - 开关关时线程空转等待，不开会话、不读写；开关开后每轮开新会话、
      同步一次、关闭会话，轮次异常只记 warning、不杀线程。
    - 返回 ``(thread, stop_event)``：调用方用 ``stop_event.set()`` 停它。
    """
    if session_factory is None:
        from backend.database import SessionLocal
        session_factory = SessionLocal
    if store is None:
        store = uh.RedisHealthStore()
    interval = sync_interval_sec() if interval_sec is None else float(interval_sec)
    stop = stop_event or threading.Event()

    def _loop() -> None:
        while not stop.is_set():
            try:
                if sync_enabled():
                    db = session_factory()
                    try:
                        result = sync_health_to_pg(db, store)
                        logger.info("上游健康同步完成：%s", result)
                    finally:
                        db.close()
            except Exception:
                logger.warning("上游健康同步轮次异常，已跳过本轮", exc_info=True)
            stop.wait(interval)

    thread = threading.Thread(target=_loop, name="upstream-health-pg-sync",
                              daemon=True)
    thread.start()
    return thread, stop


__all__ = [
    "PG_UNKNOWN", "PG_HEALTHY", "PG_UNHEALTHY",
    "redis_key_for_source", "translate_redis_state",
    "sync_enabled", "sync_interval_sec",
    "sync_health_to_pg", "start_health_sync_daemon",
]
