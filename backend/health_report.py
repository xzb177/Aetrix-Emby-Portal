"""后台健康检查：把「真的出事了」反映到 status 上。

## 为什么要做

``/health`` 以前硬编码返回 ``status="healthy"``——不管数据库断开、Redis 挂了、
worker 刷几千条探测失败、扫描队列堆积，前台「服务健康」页一律显示正常。

这不是"少一个指标"，而是**看板在最需要的时候失明**：出问题时没人能从页面看到，
只能去翻日志。生产上刮削链整条挂掉（series 全部搜不到）而页面一片绿，
就是这么发生的。

## 口径

按严重度分三档，让页面上能一眼看出该不该慌：

- ``ok``      一切正常
- ``warn``   降级但还能用：磁盘吃紧、Redis 不可用（有本地回退）、探测/刮削失败率高
- ``down``    核心不可用：数据库不通、补全队列堆积到处理不过来

**刻意不纳入**：单条条目的刮削失败不算 warn（TMDB 对中文剧集收录差是常态，
几百条 failed 属于正常业务状态，报出来只会天天误警）。只统计**失败率**与
**堆积量**，这两者才代表系统出了问题。
"""
from __future__ import annotations

import logging
import threading
import time
from typing import Any

from sqlalchemy import func
from backend.emby_server.env_util import env_float

logger = logging.getLogger(__name__)

from backend.error_lexicon import translate as _translate_error

# 阈值：经验值，按公测规模调过
WARN_DISK_FREE_PERCENT = 15.0
WARN_PROBE_FAIL_RATIO = 0.30        # 探测失败率超过 30%
WARN_ENRICH_FAIL_RATIO = 0.30       # 刮削失败率超过 30%
DOWN_QUEUE_BACKLOG = 500            # 补全队列堆积超过这个数就是处理不过来了


def _count(db, item_type: str, status: str) -> int:
    from backend.emby_server import models as em
    try:
        return db.query(func.count(em.MediaItem.id)).filter(
            em.MediaItem.item_type == item_type,
            em.MediaItem.probe_status == status,
        ).scalar() or 0
    except Exception as exc:  # noqa: BLE001 — 统计失败不该让健康检查 500
        logger.debug("统计 probe %s 失败: %s", status, exc)
        return 0


def _enrich_count(db, status: str) -> int:
    from backend.emby_server import models as em
    try:
        # 只数真实条目：合并副本（merged_into_id 非空）按设计不参与补全
        # （母条目补完即可），把它们算进来会让积压常年虚高几万条、
        # 健康灯长红（2026-10-16 实测：真实 pending 2 条、副本 pending 72167 条）。
        return db.query(func.count(em.MediaItem.id)).filter(
            em.MediaItem.enrich_status == status,
            em.MediaItem.merged_into_id.is_(None)).scalar() or 0
    except Exception as exc:  # noqa: BLE001
        logger.debug("统计 enrich %s 失败: %s", status, exc)
        return 0


def library_scan_summary(db) -> dict:
    """各媒体库最近一轮扫描的结果概览（扫描失败的库一眼可见）"""
    from backend.emby_server import models as em
    out = {
        "libraries_total": 0,
        "scanning": 0,
        "failed": 0,
        "interrupted": 0,
        "never_scanned": 0,
        "last_failed": [],
    }
    try:
        rows = db.query(em.Library).all()
    except Exception as exc:  # noqa: BLE001
        logger.debug("读媒体库失败: %s", exc)
        return out
    out["libraries_total"] = len(rows)
    for lib in rows:
        status = (getattr(lib, "scan_status", None) or "").strip()
        if getattr(lib, "is_scanning", False) or status == "running":
            out["scanning"] += 1
        elif status == "failed":
            out["failed"] += 1
            if len(out["last_failed"]) < 5:
                err = (lib.scan_error or "")[:200]
                out["last_failed"].append({
                    "id": lib.id, "name": lib.name,
                    "error": err,
                    # 错误码大白话：原文旁边直接给中文解释 + 检查步骤
                    "hint": _translate_error(err),
                    "at": str(lib.last_scan_at or ""),
                })
        elif status == "interrupted":
            # 被进程重启打断：不是失败，不计 failed、不出告警；
            # 启动维护会自动续扫，这里只留一个计数供观察。
            out["interrupted"] += 1
        elif not lib.last_scan_at:
            out["never_scanned"] += 1
    return out


def collect(db) -> dict:
    """返回 ``{level, status, issues[], metrics{}}``"""
    from sqlalchemy import func

    issues: list[dict] = []
    metrics: dict[str, Any] = {}
    level = "ok"

    def _bump(new_level: str) -> None:
        nonlocal level
        order = {"ok": 0, "warn": 1, "down": 2}
        if order.get(new_level, 0) > order.get(level, 0):
            level = new_level

    # ---- 数据库 ----
    try:
        from sqlalchemy import text
        db.execute(text("SELECT 1"))
        metrics["database"] = "ok"
    except Exception as exc:  # noqa: BLE001
        metrics["database"] = "error"
        _bump("down")
        issues.append({"level": "down", "key": "database",
                       "message": f"数据库不可用：{str(exc)[:120]}"})

    # ---- Redis（队列/熔断器/分布式锁都靠它）----
    try:
        from backend import database as dbmod
        r = dbmod.redis_client
        if r is None:
            metrics["redis"] = "not_configured"
        else:
            r.ping()
            metrics["redis"] = "ok"
    except Exception as exc:  # noqa: BLE001
        metrics["redis"] = "error"
        _bump("warn")
        issues.append({"level": "warn", "key": "redis",
                       "message": f"Redis 不可用：{str(exc)[:120]}"})

    # ---- 磁盘余量 ----
    try:
        from backend.emby_server import maintenance
        rep = maintenance.resource_report()
        free_pct = rep.get("disk_free_percent")
        metrics["disk_free_percent"] = free_pct
        if free_pct is not None and float(free_pct) < WARN_DISK_FREE_PERCENT:
            _bump("warn")
            issues.append({"level": "warn", "key": "disk",
                           "message": f"磁盘余量偏低（{float(free_pct):.0f}% 剩余）"})
    except Exception as exc:  # noqa: BLE001
        logger.debug("读磁盘信息失败: %s", exc)

    # ---- 后台 worker 存活（StrmAssistant 打磨 R2）----
    # 线程静默死亡时这里报 warn，避免"后台任务停摆了却没人知道"
    try:
        from backend.emby_server import worker_registry as _wr
        workers = _wr.snapshot()
        metrics["workers"] = workers
        dead = [n for n, s in workers.items() if not s["alive"]]
        if dead:
            _bump("warn")
            issues.append({"level": "warn", "key": "workers",
                           "message": f"后台 worker 线程已死亡：{', '.join(dead)}"})
    except Exception as exc:  # noqa: BLE001
        logger.debug("读 worker 状态失败: %s", exc)

    # ---- 媒体探测失败率 ----
    try:
        done = _count(db, "movie", "done") + _count(db, "episode", "done")
        degraded = _count(db, "movie", "degraded") + _count(db, "episode", "degraded")
        # ffprobe 跑完但没时长：可播、不算失败，但必须进分母，
        # 否则它们从总量里消失，失败率会虚低（v2.42.14 新增状态）
        no_duration = (_count(db, "movie", "probed_no_duration")
                       + _count(db, "episode", "probed_no_duration"))
        failed = _count(db, "movie", "failed") + _count(db, "episode", "failed")
        total = done + degraded + no_duration + failed
        ratio = (failed / total) if total else 0.0
        metrics["probe_degraded"] = degraded
        metrics["probe_no_duration"] = no_duration
        metrics["probe_failed"] = failed
        metrics["probe_ratio"] = round(ratio, 4)
        # 样本太少时不算数（刚开机的库会误报）
        if total >= 20 and ratio >= WARN_PROBE_FAIL_RATIO:
            _bump("warn")
            issues.append({
                "level": "warn", "key": "probe",
                "message": f"媒体探测失败率 {ratio:.0%}（{failed}/{total}），"
                           "通常是存储源读不到或云盘异常"})
    except Exception as exc:  # noqa: BLE001
        logger.debug("统计探测失败率出错: %s", exc)

    # ---- 探测 worker 是否真的在干活（v2.53）----
    # 以前只看失败率：worker 不跑时 pending 只增不减，健康页却一片绿。
    try:
        from backend.emby_server import probe_worker
        ps = probe_worker.status_snapshot(db)
        rt = ps.get("runtime") or {}
        metrics["probe_worker"] = {
            "pending_ready": ps["pending_ready"], "retrying": ps["retrying"],
            "stale_probing": ps["stale_probing"], "paused": ps["paused"],
            "eta_hours": ps["eta_hours"], "rate_per_min": rt.get("rate_per_min"),
            "running": rt.get("running"), "breakers": rt.get("breakers"),
            "last_error": rt.get("last_error"),
        }
        if ps["enabled"] and not ps["paused"] and ps["pending_ready"] > 0 and not rt.get("running"):
            _bump("warn")
            issues.append({
                "level": "warn", "key": "probe_worker",
                "message": f"{ps['pending_ready']} 条待探测，但探测 worker 没有在运行"})
        if ps["stale_probing"]:
            _bump("warn")
            issues.append({
                "level": "warn", "key": "probe_stale",
                "message": f"{ps['stale_probing']} 条探测抢单已过期（worker 卡死或崩溃），"
                           "会自动回收；也可在后台手动重置"})
        if rt.get("breakers") and any(b.get("open_for_sec") for b in rt["breakers"].values()):
            _bump("warn")
            names = "、".join(k for k, b in rt["breakers"].items() if b.get("open_for_sec"))
            issues.append({"level": "warn", "key": "probe_breaker",
                           "message": f"探测熔断中的挂载：{names}（连续超时，挂载可能挂死）"})
    except Exception as exc:  # noqa: BLE001
        logger.debug("统计探测 worker 状态出错: %s", exc)

    # ---- 补全/刮削失败率与堆积 ----
    try:
        edone = _enrich_count(db, "done")
        efailed = _enrich_count(db, "failed")
        epending = _enrich_count(db, "pending")
        eenriching = _enrich_count(db, "enriching")
        etotal = edone + efailed
        eratio = (efailed / etotal) if etotal else 0.0
        metrics.update({
            "enrich_done": edone, "enrich_failed": efailed,
            "enrich_pending": epending, "enriching": eenriching,
            "enrich_fail_ratio": round(eratio, 4),
        })
        if etotal >= 20 and eratio >= WARN_ENRICH_FAIL_RATIO:
            _bump("warn")
            issues.append({
                "level": "warn", "key": "enrich",
                "message": f"元数据补全失败率 {eratio:.0%}（{efailed}/{etotal}），"
                           "检查 TMDB Key 与网络"})
        backlog = epending + eenriching
        metrics["enrich_backlog"] = backlog
        if backlog >= DOWN_QUEUE_BACKLOG:
            _bump("down")
            issues.append({
                "level": "down", "key": "enrich_backlog",
                "message": f"补全队列堆积 {backlog} 条，已经处理不过来"})

        # v2.54.x: 把「堆积」「卡住」「刚启动」拆成三态 —— 处置完全不同：
        # 堆积（enrich_backlog/down）：队列太长，等它跑（或加线程）；
        # 卡住（enrich_stalled/down）：有动作但无成功，去查 worker 是不是死锁/崩溃；
        # 刚启动（enrich_no_progress/warn）：本进程还没完成过任何条目，
        #   不能把「刚起来」当故障报 down，先观察。
        #
        # 判定依据是 scan_progress.throughput()（进程内计数器）：
        # 测试用 scan_progress.reset() 做隔离，确定性高；
        # 生产上 /api/health 若与 worker 同进程部署时最准。
        # 注意分离部署时 API 进程计数器恒为 0，此时只会报 warn（诚实），
        # 不会误报 down —— 「没在跑」本来就该是 warn 级别。
        try:
            from backend.emby_server import enrich_worker
            from backend.emby_server import scan_progress as progress

            tp = progress.throughput()
            done_per_min = tp["done_per_min"]
            metrics["enrich_done_per_min"] = done_per_min
            metrics["enrich_idle_sec"] = tp["idle_sec"]
            try:
                metrics["enrich_stages"] = progress.stage_stats()
            except Exception:  # noqa: BLE001 — 进程内统计拿不到就留空，不影响判定
                metrics["enrich_stages"] = {}
            # worker 被显式关掉（ENRICH_WORKER=0）时不报「没在跑」：那就是设计如此
            if (enrich_worker.ENRICH_ENABLED
                    and backlog >= DOWN_QUEUE_BACKLOG
                    and done_per_min <= 0):
                total = tp["done_total"] + tp["retry_total"] + tp["failed_total"]
                if total > 0:
                    _bump("down")
                    issues.append({
                        "level": "down", "key": "enrich_stalled",
                        "message": f"补全队列堆积 {backlog} 条，有动作但近 "
                                   f"{tp['window_sec']} 秒没有任何成功 —— 是卡住了，不是跑得慢"})
                else:
                    _bump("warn")
                    issues.append({
                        "level": "warn", "key": "enrich_no_progress",
                        "message": f"补全队列堆积 {backlog} 条，本进程尚未完成任何条目 —— "
                                   "worker 可能刚启动或没在跑，先观察再排查"})
        except Exception as exc:  # noqa: BLE001
            logger.debug("统计补全速率出错: %s", exc)
    except Exception as exc:  # noqa: BLE001
        logger.debug("统计补全状态出错: %s", exc)

    # ---- enrich done→pending 批量翻转监控（v2.55.x）----
    # 2026-10-11 生产事故：5723 条翻转触发源未知、日志丢失。从此任何超阈值的
    # 翻转都写 EnrichFlipEvent（PG 持久），这里暴露近 1 小时翻转数；>0 就 warn。
    try:
        from backend.emby_server import enrich_flip_monitor as _fm
        # retry_unmatched 是管理端手动触发的已知操作：事件仍在 PG 留痕审计，
        # 但不应把 health 打成 warn。
        flip_1h = _fm.flip_count_last_hours(
            db, hours=1, exclude_triggers=("retry_unmatched",))
        metrics["enrich_flip_last_hour"] = flip_1h
        if flip_1h > 0:
            _bump("warn")
            issues.append({
                "level": "warn", "key": "enrich_flip",
                "message": f"近 1 小时 enrich done→pending 翻转 {flip_1h} 条 —— "
                           "检查翻转告警日志与 EnrichFlipEvent 记录，确认触发源"})
    except Exception as exc:  # noqa: BLE001
        logger.debug("统计 enrich 翻转数出错: %s", exc)

    # ---- 扫描（库级别的真实故障）----
    try:
        scans = library_scan_summary(db)
        metrics["scans"] = scans
        if scans["failed"]:
            names = "、".join(x["name"] for x in scans["last_failed"][:3])
            _bump("warn")
            issues.append({
                "level": "warn", "key": "scan",
                "message": f"{scans['failed']} 个媒体库最近一次扫描失败：{names}"})
    except Exception as exc:  # noqa: BLE001
        logger.debug("统计扫描状态出错: %s", exc)

    # ---- 错误码大白话：每条 issue 旁边挂中文解释 + 检查步骤 ----
    # translate() 命中返回 dict、无命中返回 None（前端按 None 隐藏）。
    for issue in issues:
        try:
            issue["hint"] = _translate_error(issue.get("message", ""))
        except Exception:  # noqa: BLE001 — 翻译失败不影响健康检查
            issue["hint"] = None

    return {
        "level": level,
        "status": {"ok": "healthy", "warn": "degraded", "down": "unhealthy"}[level],
        "issues": issues,
        "metrics": metrics,
    }


# ---- collect() 的短 TTL 缓存入口（2026-10-16 网页加载二轮优化）----
# 实测：扫描打满 2 核生产机时 collect() 一次约 5–10 秒（约 20 个 SQL +
# 目录 walk + Redis ping）；每次打开「服务器」页与每次健康探针都全量重跑，
# 是「服务器」页 5 秒空白的最大头。健康快照 60 秒级延迟可接受，故加进程内
# 短 TTL 缓存：/api/health、业务健康端点、面板 pulse/vitals 全部经此入口。
# collect() 本体保持无缓存——测试与需要实时口径的调用方不受影响。
# TTL 经环境变量 HEALTH_REPORT_TTL_SECONDS 可配（默认 60；<=0 关闭缓存）。
_HEALTH_CACHE = {"at": 0.0, "result": None}
_HEALTH_CACHE_LOCK = threading.Lock()
DEFAULT_HEALTH_TTL_SECONDS = 60.0


def _health_ttl_seconds() -> float:
    return env_float("HEALTH_REPORT_TTL_SECONDS", DEFAULT_HEALTH_TTL_SECONDS)


def collect_cached(db, ttl_seconds: float | None = None) -> dict:
    """collect() 的短 TTL 缓存版：同一进程内 TTL 窗口内只聚合一次。

    刷新失败（理论上 collect() 内部已吞掉一切异常）时返回旧快照，
    绝不让健康检查 500。
    """
    ttl = _health_ttl_seconds() if ttl_seconds is None else float(ttl_seconds)
    if ttl <= 0:
        return collect(db)
    now = time.monotonic()
    with _HEALTH_CACHE_LOCK:
        entry = _HEALTH_CACHE
        if entry["result"] is not None and (now - entry["at"]) < ttl:
            return entry["result"]
        try:
            result = collect(db)
        except Exception:  # noqa: BLE001 — 刷新失败时退回旧快照
            if entry["result"] is not None:
                return entry["result"]
            raise
        entry["at"] = now
        entry["result"] = result
        return result


def _reset_health_cache() -> None:
    """测试隔离用：清空进程内健康缓存。生产代码不调用。"""
    with _HEALTH_CACHE_LOCK:
        _HEALTH_CACHE["at"] = 0.0
        _HEALTH_CACHE["result"] = None
