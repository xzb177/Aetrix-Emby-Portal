"""可观测底座：健康探针 + 进程内指标 + 聚合快照（v2.56.0）

## 为什么要做

``/api/health`` 已经能回答「系统好不好」，但回答不了「哪里不好、不好到什么
程度」——probe 队列多深、enrich 卡在哪、SA 配额还剩多少，
全要翻日志或连 DB 查。这是「健康一页」「一键体检」「可观测看板」的前置：
先把各子系统的状态**采集**起来，后面的 UI 才有东西可画。

## 设计原则

1. **只读探针**：所有探针只读 DB/内存状态，**绝不调用 Drive API**
   （用户底线：不触发 Google 风控；探针高频打 API = 自己制造限流）。
2. **不抢主链路**：探针走聚合 COUNT，不做全表扫描；指标是进程内环形缓冲，
   记录一次 = 一次 deque.append + 计数器更新，全程持有**单个全局锁**
   （P3-4，2026-10-12：临界区仅微秒级，默认关闭时零开销；此前文档误写
   "无锁或细粒度锁"，以实现为准）。
3. **开关默认关**：``OBSERVABILITY_ENABLED=1`` 才采集详细指标与探针；
   关闭时聚合接口返回 ``{"enabled": false}``，中间件不记录。
4. **阈值可配置**：所有红黄灯阈值走环境变量，不写死业务数值（铁律：
   只做能力，不做死业务逻辑）。
5. **模块顶层不导入项目内模块**（只用标准库；P3-6，2026-10-12：各探针在函数内
   按需导入 backend.* ——刻意为之，避免循环依赖；此前"不导入任何项目内模块"
   表述不准确），调用方把 db 传进来（参考 scan_progress.py 的做法）。

## 子系统清单

| key        | 探针内容                              | 数据来源（只读）            |
|------------|---------------------------------------|-----------------------------|
| database   | SELECT 1                              | db                          |
| redis      | ping                                  | database.redis_client       |
| disk       | 余量                                  | maintenance.resource_report |
| probe      | 队列深度/速率/失败率/熔断             | probe_worker.status_snapshot|
| enrich     | 积压/速率/失败率                      | DB 聚合 + scan_progress     |
| scan       | 各库扫描状态                          | library_scan_summary        |
| sa_quota   | SA 数量（不打 API）                   | drive_auth.sa_count（文件清单缓存）|
| workers    | 后台线程存活                          | worker_registry.snapshot    |
| api        | P50/P99/错误率（进程内）              | 本模块环形缓冲              |
"""
from __future__ import annotations

import logging
import os
import threading
import time
from collections import deque
from typing import Any

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 开关与阈值（全部可配置，默认值只是兜底）
# ---------------------------------------------------------------------------

def enabled() -> bool:
    """总开关，默认关。"""
    return (os.getenv("OBSERVABILITY_ENABLED", "0") or "0").strip().lower() not in {
        "", "0", "false", "no", "off",
    }


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except (ValueError, TypeError):
        return default


def _env_int(name: str, default: int) -> int:
    try:
        return int(float(os.getenv(name, str(default))))
    except (ValueError, TypeError):
        return default


# 阈值：warn/dowm 分界（单位见名）
THRESHOLDS = {
    # probe 队列堆积多少条算 warn
    "probe_queue_warn": lambda: _env_int("OBS_PROBE_QUEUE_WARN", 1000),
    # enrich 积压多少条算 warn（health_report 的 down 线是 500，这里只做展示分级）
    "enrich_backlog_warn": lambda: _env_int("OBS_ENRICH_BACKLOG_WARN", 500),
    # API P99 超过多少毫秒算 warn
    "api_p99_ms_warn": lambda: _env_float("OBS_API_P99_MS_WARN", 2000.0),
    # API 错误率超过多少算 warn
    "api_error_ratio_warn": lambda: _env_float("OBS_API_ERROR_RATIO_WARN", 0.05),
    # 磁盘余量低于多少算 warn
    "disk_free_warn": lambda: _env_float("OBS_DISK_FREE_WARN", 15.0),
}

# ---------------------------------------------------------------------------
# 进程内 API 指标（环形缓冲，中间件调用 record_request）
# ---------------------------------------------------------------------------

# 路由归一化：把 /api/items/123 这类收敛成 /api/items/{id}，避免基数爆炸
def _normalize_path(path: str) -> str:
    parts = path.strip("/").split("/")
    out = []
    for p in parts:
        if p.isdigit():
            out.append("{id}")
        elif len(p) >= 32 and all(c in "0123456789abcdef-" for c in p.lower()):
            out.append("{uuid}")
        else:
            out.append(p)
    return "/" + "/".join(out)


# 登录/播放相关的路径模式（用于分类计数，不写死具体业务；匹配时统一小写）
_LOGIN_PATTERNS = ("/api/auth/login", "/api/login", "/emby/users/authenticate")
_PLAYBACK_PATTERNS = ("/api/play", "/api/stream", "/emby/videos/", "/videos/")

_MAX_SAMPLES = 2000  # 每个路由组最多保留的样本数

_lock = threading.Lock()
# (normalized_path, method) -> deque[(elapsed_ms, status_code, ts)]
_LAT: dict[tuple[str, str], deque] = {}
_counters: dict[str, int] = {"requests": 0, "logins": 0, "playbacks": 0, "errors": 0}


def record_request(path: str, method: str, status_code: int, elapsed_ms: float) -> None:
    """中间件调用：记录一次请求耗时。关闭时直接返回，零开销。"""
    if not enabled():
        return
    try:
        key = (_normalize_path(path), method.upper())
        now = time.monotonic()
        with _lock:
            dq = _LAT.get(key)
            if dq is None:
                dq = deque(maxlen=_MAX_SAMPLES)
                _LAT[key] = dq
            dq.append((elapsed_ms, status_code, now))
            _counters["requests"] += 1
            if status_code >= 500:
                _counters["errors"] += 1
            lp = path.lower()
            if any(p in lp for p in _LOGIN_PATTERNS):
                _counters["logins"] += 1
            if any(p in lp for p in _PLAYBACK_PATTERNS):
                _counters["playbacks"] += 1
    except Exception:  # noqa: BLE001 — 指标记录永不影响主链路
        pass


def _percentile(sorted_vals: list[float], pct: float) -> float:
    if not sorted_vals:
        return 0.0
    k = (len(sorted_vals) - 1) * pct
    f = int(k)
    c = min(f + 1, len(sorted_vals) - 1)
    if f == c:
        return sorted_vals[f]
    return sorted_vals[f] + (sorted_vals[c] - sorted_vals[f]) * (k - f)


def api_stats(top_n: int = 20) -> dict[str, Any]:
    """各路由组的 P50/P99/错误率。只统计，不打 API。"""
    out: dict[str, Any] = {"routes": [], "totals": {}}
    try:
        with _lock:
            totals = dict(_counters)
            items = list(_LAT.items())
        routes = []
        for (path, method), dq in items:
            samples = [s[0] for s in dq]
            errors = sum(1 for s in dq if s[1] >= 500)
            total = len(dq)
            if not samples:
                continue
            samples.sort()
            routes.append({
                "route": f"{method} {path}",
                "count": total,
                "p50_ms": round(_percentile(samples, 0.5), 1),
                "p99_ms": round(_percentile(samples, 0.99), 1),
                "max_ms": round(samples[-1], 1),
                "error_ratio": round(errors / total, 4) if total else 0.0,
            })
        routes.sort(key=lambda r: r["count"], reverse=True)
        out["routes"] = routes[:top_n]
        out["totals"] = totals
        # 全局 P99（跨路由，用于红黄灯）
        all_samples: list[float] = []
        with _lock:
            for dq in _LAT.values():
                all_samples.extend(s[0] for s in dq)
        all_samples.sort()
        out["global_p99_ms"] = round(_percentile(all_samples, 0.99), 1)
        out["global_error_ratio"] = round(
            totals.get("errors", 0) / totals["requests"], 4) if totals.get("requests") else 0.0
    except Exception as exc:  # noqa: BLE001
        logger.debug("计算 API 指标失败: %s", exc)
    return out


def reset_metrics() -> None:
    """测试隔离用：清空进程内指标。"""
    with _lock:
        _LAT.clear()
        for k in _counters:
            _counters[k] = 0


# ---------------------------------------------------------------------------
# 健康探针（全部只读，不调用 Drive API）
# ---------------------------------------------------------------------------

def _ok(key: str, metrics: dict | None = None, message: str = "") -> dict:
    return {"key": key, "status": "ok", "message": message, "metrics": metrics or {}}


def _warn(key: str, message: str, metrics: dict | None = None) -> dict:
    return {"key": key, "status": "warn", "message": message, "metrics": metrics or {}}


def _down(key: str, message: str, metrics: dict | None = None) -> dict:
    return {"key": key, "status": "down", "message": message, "metrics": metrics or {}}


def _probe_database(db) -> dict:
    try:
        from sqlalchemy import text
        db.execute(text("SELECT 1"))
        return _ok("database", message="数据库连通")
    except Exception as exc:  # noqa: BLE001
        return _down("database", f"数据库不可用：{str(exc)[:120]}")


def _probe_redis() -> dict:
    try:
        from backend import database as dbmod
        r = dbmod.redis_client
        if r is None:
            return _warn("redis", "Redis 未配置（有本地回退）")
        r.ping()
        return _ok("redis", message="Redis 连通")
    except Exception as exc:  # noqa: BLE001
        return _warn("redis", f"Redis 不可用：{str(exc)[:120]}")


def _probe_disk() -> dict:
    try:
        from backend.emby_server import maintenance
        rep = maintenance.resource_report()
        free_pct = rep.get("disk_free_percent")
        m = {"disk_free_percent": free_pct,
             "transcode_sessions": rep.get("transcode_sessions", 0)}
        if free_pct is not None and float(free_pct) < THRESHOLDS["disk_free_warn"]():
            return _warn("disk", f"磁盘余量偏低（{float(free_pct):.0f}% 剩余）", m)
        return _ok("disk", m, "磁盘余量正常")
    except Exception as exc:  # noqa: BLE001
        logger.debug("磁盘探针失败: %s", exc)
        return _warn("disk", "磁盘信息读取失败")


def _probe_workers() -> dict:
    try:
        from backend.emby_server import worker_registry as _wr
        workers = _wr.snapshot()
        dead = [n for n, s in workers.items() if not s.get("alive")]
        m = {"total": len(workers), "dead": dead}
        if dead:
            return _warn("workers", f"后台 worker 线程已死亡：{', '.join(dead)}", m)
        return _ok("workers", m, f"{len(workers)} 个 worker 存活")
    except Exception as exc:  # noqa: BLE001
        logger.debug("worker 探针失败: %s", exc)
        return _warn("workers", "worker 状态读取失败")


def _probe_queue(db) -> dict:
    """probe 队列深度/速率/熔断（只读内存+DB，不打 Drive API）。"""
    try:
        from backend.emby_server import probe_worker
        ps = probe_worker.status_snapshot(db)
        rt = ps.get("runtime") or {}
        m = {
            "pending_ready": ps.get("pending_ready", 0),
            "retrying": ps.get("retrying", 0),
            "stale_probing": ps.get("stale_probing", 0),
            "paused": ps.get("paused", False),
            "running": rt.get("running", False),
            "rate_per_min": rt.get("rate_per_min"),
            "eta_hours": ps.get("eta_hours"),
        }
        open_breakers = [k for k, b in (rt.get("breakers") or {}).items()
                         if b.get("open_for_sec")]
        m["open_breakers"] = open_breakers
        if ps.get("enabled") and not ps.get("paused") and ps.get("pending_ready", 0) > 0 \
                and not rt.get("running"):
            return _warn("probe", f"{ps['pending_ready']} 条待探测但 worker 没在跑", m)
        if open_breakers:
            return _warn("probe", f"熔断中的挂载：{'、'.join(open_breakers)}", m)
        if ps.get("pending_ready", 0) >= THRESHOLDS["probe_queue_warn"]():
            return _warn("probe", f"探测队列堆积 {ps['pending_ready']} 条", m)
        return _ok("probe", m, "探测队列正常")
    except Exception as exc:  # noqa: BLE001
        logger.debug("probe 探针失败: %s", exc)
        return _warn("probe", "探测状态读取失败")


def _probe_enrich(db) -> dict:
    """enrich 积压（DB 聚合，只读）。

    P3-5（2026-10-12）：这里与 ``health_report.collect`` 跑完全相同的 4 次
    enrich_status COUNT（走 ``idx_item_enrich`` 索引，单次开销小）。重复是刻意的：
    本模块顶层不导入项目内模块（设计原则#5），不与 health_report 共享中间结果，
    避免循环依赖；开关默认关，/api/health 额外 4 次索引 COUNT 可接受。
    r37：与 health_report._enrich_count 同口径——只数真实条目，合并副本
    （merged_into_id 非空）按设计不参与补全，算进来会让积压常年虚高几万条、
    探针长 warn（2026-10-16 实测：真实 pending 2 条、副本 pending 72167 条）。
    """
    try:
        from backend.emby_server import models as em
        from sqlalchemy import func
        counts = {}
        for st in ("pending", "enriching", "done", "failed"):
            try:
                counts[st] = db.query(func.count(em.MediaItem.id)).filter(
                    em.MediaItem.enrich_status == st,
                    em.MediaItem.merged_into_id.is_(None)).scalar() or 0
            except Exception:  # noqa: BLE001
                counts[st] = 0
        backlog = counts["pending"] + counts["enriching"]
        m = {"backlog": backlog, **counts}
        try:
            from backend.emby_server import scan_progress as progress
            tp = progress.throughput()
            m["done_per_min"] = tp.get("done_per_min", 0)
            m["idle_sec"] = tp.get("idle_sec")
        except Exception:  # noqa: BLE001
            pass
        if backlog >= THRESHOLDS["enrich_backlog_warn"]():
            return _warn("enrich", f"补全队列积压 {backlog} 条", m)
        return _ok("enrich", m, "补全队列正常")
    except Exception as exc:  # noqa: BLE001
        logger.debug("enrich 探针失败: %s", exc)
        return _warn("enrich", "补全状态读取失败")


def _probe_scan(db) -> dict:
    """各库扫描状态（复用 health_report 的汇总，只读）。"""
    try:
        from backend.health_report import library_scan_summary
        s = library_scan_summary(db)
        if s["failed"]:
            names = "、".join(x["name"] for x in s["last_failed"][:3])
            return _warn("scan", f"{s['failed']} 个库扫描失败：{names}", s)
        if s["scanning"]:
            return _ok("scan", s, f"{s['scanning']} 个库正在扫描")
        return _ok("scan", s, "扫描状态正常")
    except Exception as exc:  # noqa: BLE001
        logger.debug("扫描探针失败: %s", exc)
        return _warn("scan", "扫描状态读取失败")


def _probe_sa_quota() -> dict:
    """SA 配额水位：读 SA 文件清单缓存（``_discover_sa_files`` 的 ``_sa_files``
    全局缓存），绝不调用 Drive API 查配额。

    为什么不去查真实配额：Drive API 的配额查询本身消耗配额，探针高频调用
    = 自己制造限流，违反用户「不触发 Google 风控」底线。
    熔断状态由 probe 探针统一报告（避免重复查库），这里只报 SA 数量。
    """
    try:
        from backend.emby_server import drive_auth as _da
        m: dict[str, Any] = {"sa_count": _da.sa_count()}
        if m["sa_count"] == 0:
            return _warn("sa_quota", "未配置 SA 账号", m)
        return _ok("sa_quota", m, f"{m['sa_count']} 个 SA 就绪")
    except Exception as exc:  # noqa: BLE001
        logger.debug("SA 探针失败: %s", exc)
        return _warn("sa_quota", "SA 状态读取失败")


def _probe_api() -> dict:
    """API 指标红黄灯（进程内数据，不打 API）。"""
    try:
        stats = api_stats(top_n=5)
        p99 = stats.get("global_p99_ms", 0)
        err = stats.get("global_error_ratio", 0)
        m = {
            "global_p99_ms": p99,
            "global_error_ratio": err,
            "totals": stats.get("totals", {}),
            "top_routes": stats.get("routes", []),
        }
        if err >= THRESHOLDS["api_error_ratio_warn"]():
            return _warn("api", f"API 错误率 {err:.1%}", m)
        if p99 >= THRESHOLDS["api_p99_ms_warn"]():
            return _warn("api", f"API P99 延迟 {p99:.0f}ms", m)
        return _ok("api", m, "API 指标正常")
    except Exception as exc:  # noqa: BLE001
        logger.debug("API 探针失败: %s", exc)
        return _warn("api", "API 指标读取失败")


# 探针注册表：key → 函数（新增子系统只加一行）
_PROBES = [
    ("database", _probe_database),
    ("redis", _probe_redis),
    ("disk", _probe_disk),
    ("workers", _probe_workers),
    ("probe", _probe_queue),
    ("enrich", _probe_enrich),
    ("scan", _probe_scan),
    ("sa_quota", _probe_sa_quota),
    ("api", _probe_api),
]


def probe_subsystems(db) -> dict[str, dict]:
    """跑全部探针，返回 {key: {status, message, metrics}}。单个探针异常不影响其他。"""
    out: dict[str, dict] = {}
    for key, fn in _PROBES:
        try:
            if key == "database":
                out[key] = fn(db)
            elif key in ("probe", "enrich", "scan"):
                out[key] = fn(db)
            else:
                out[key] = fn()
        except Exception as exc:  # noqa: BLE001
            logger.debug("探针 %s 异常: %s", key, exc)
            out[key] = _warn(key, "探针执行异常")
    return out


def snapshot(db) -> dict[str, Any]:
    """聚合快照：开关关时返回 {"enabled": false}；开时返回各子系统状态。"""
    if not enabled():
        return {"enabled": False}
    subsystems = probe_subsystems(db)
    order = {"ok": 0, "warn": 1, "down": 2}
    worst = "ok"
    for s in subsystems.values():
        if order.get(s["status"], 0) > order.get(worst, 0):
            worst = s["status"]
    return {
        "enabled": True,
        "level": worst,
        "subsystems": subsystems,
        "thresholds": {k: fn() for k, fn in THRESHOLDS.items()},
    }
