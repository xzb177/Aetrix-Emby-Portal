"""管理后台 · 任务总览（路线 2-A）

一页看所有后台任务的状态、进度、开关：

    GET /api/admin/tasks/overview

只读聚合，不重复造轮子：复用各模块现有的状态函数
（task_scheduler.get_unified_status、probe_worker.status_snapshot、
enrich_worker.get_progress、image_store.backfill_status、
change_watcher.get_config，以及 ScanRun 流水）。

单个子系统采集失败不影响整体：每个任务独立 try/except，
失败的任务 status=error 并在 summary 里写清原因，前端照常渲染其余卡片。

status 取值：running（运行中）/ ok（正常）/ warning（留意）/
            error（异常）/ idle（空闲）/ disabled（已关闭）
"""
from __future__ import annotations

import json
import logging
import os
from datetime import datetime

from fastapi import APIRouter, Depends
from sqlalchemy import func
from sqlalchemy.orm import Session

from backend.api.admin_core import get_current_admin
from backend.database import get_db

logger = logging.getLogger(__name__)

tasks_router = APIRouter(prefix="/api/admin", tags=["管理后台·任务总览"])


# ---------------------------------------------------------------------------
# 小工具
# ---------------------------------------------------------------------------

def _iso(v) -> str | None:
    if v is None:
        return None
    try:
        return v.isoformat()
    except Exception:  # noqa: BLE001
        return str(v)


def _card(key: str, title: str, status: str, status_text: str, summary: str,
          metrics: list | None = None, last_run: str | None = None,
          last_run_label: str = "上次运行", switch: dict | None = None,
          link: str | None = None) -> dict:
    return {
        "key": key,
        "title": title,
        "status": status,
        "status_text": status_text,
        "summary": summary,
        "metrics": metrics or [],
        "last_run": last_run,
        "last_run_label": last_run_label,
        "switch": switch,
        "link": link,
    }


def _failed_card(key: str, title: str, reason: str) -> dict:
    return _card(key, title, "error", "异常",
                 f"读不到这个任务的状态：{reason[:80]}")


# ---------------------------------------------------------------------------
# 1. 媒体库扫描
# ---------------------------------------------------------------------------

def _task_scan(db: Session) -> dict:
    from backend.emby_server import task_scheduler, auto_scan
    from backend.emby_server import models as em

    sched = task_scheduler.get_unified_status()
    scan = sched.get("scan", {})
    waiting = int(scan.get("waiting") or 0)
    running = int(scan.get("running") or 0)
    circuit_open = bool(sched.get("circuit_open"))
    preempt = bool(sched.get("preempt_active"))

    # 各库最近一轮扫描（单次查询取最近 300 条，按库取第一条）
    libs = (db.query(em.Library.id, em.Library.name, em.Library.scan_status)
            .order_by(em.Library.id).all())
    runs = (db.query(em.ScanRun)
            .order_by(em.ScanRun.id.desc()).limit(300).all())
    latest: dict = {}
    for r in runs:
        if r.library_id not in latest:
            latest[r.library_id] = r
    lib_rows = []
    last_run: str | None = None
    for lid, name, scan_status in libs:
        r = latest.get(lid)
        finished = _iso(r.finished_at) if r and r.finished_at else None
        started = _iso(r.started_at) if r and r.started_at else None
        if finished and (last_run is None or finished > last_run):
            last_run = finished
        lib_rows.append({
            "id": lid,
            "name": name or f"库 {lid}",
            "scan_status": scan_status or (r.status if r else None),
            "last_status": r.status if r else None,
            "last_started_at": started,
            "last_finished_at": finished,
            "last_error": (r.error or "")[:120] if r else "",
        })

    # 正在跑的库：最新一轮还没结束（finished_at 为空）的
    active_runs = [r for r in latest.values()
                   if r.status == "running" and not r.finished_at]
    running_libs = len(active_runs)
    busy = max(running_libs, running)

    auto = auto_scan.get_config(db)
    metrics = [
        {"label": "正在扫", "value": f"{busy} 个库"},
        {"label": "排队", "value": f"{waiting} 个"},
        {"label": "熔断器", "value": "开（暂停派发）" if circuit_open else "关"},
        {"label": "刮削让路", "value": "让路中（刮削降速）" if preempt else "没让路"},
    ]
    if busy > 0:
        status, status_text = "running", "运行中"
        summary = f"{busy} 个库正在扫描" + (f"，{waiting} 个在排队" if waiting else "")
    elif circuit_open:
        status, status_text = "warning", "留意"
        summary = "熔断器开着，扫描暂停派发，等熔断恢复会自动继续"
    elif waiting > 0:
        status, status_text = "warning", "排队中"
        summary = f"{waiting} 个扫描在排队等执行"
    elif not sched.get("scheduler_enabled"):
        status, status_text = "disabled", "已关闭"
        summary = "统一调度器关了，扫描不会自动派发"
    else:
        status, status_text = "ok", "空闲"
        summary = "没有在扫的库，队列也是空的"

    card = _card(
        "scan", "媒体库扫描", status, status_text, summary,
        metrics=metrics, last_run=last_run, last_run_label="最近一轮跑完",
        switch={"label": "定时扫描", "enabled": bool(auto.get("enabled")),
                "keep": {"time": auto.get("time") or "03:00"}},
        link="/emby",
    )
    card["libraries"] = lib_rows
    return card


# ---------------------------------------------------------------------------
# 2. 媒体探测（probe）
# ---------------------------------------------------------------------------

def _task_probe(db: Session) -> dict:
    from backend.emby_server import probe_worker

    snap = probe_worker.status_snapshot(db)
    counts = snap.get("counts") or {}
    pending = int(counts.get("pending") or 0)
    failed = int(counts.get("failed") or 0)
    done = int(counts.get("done") or 0)
    total = sum(int(v) for v in counts.values())
    fail_rate = (failed / total * 100) if total else 0.0
    rt = snap.get("runtime") or {}
    breakers = rt.get("breakers") or {}
    open_breakers = [k for k, v in breakers.items()
                     if isinstance(v, dict) and int(v.get("open_for_sec") or 0) > 0]
    rate = rt.get("rate_per_min") or 0
    eta = snap.get("eta_hours")

    metrics = [
        {"label": "待探测", "value": f"{pending} 条"},
        {"label": "已失败", "value": f"{failed} 条（{fail_rate:.1f}%）"},
        {"label": "探测速度", "value": f"{rate} 条/分" if rate else "没在跑"},
        {"label": "熔断", "value": f"{len(open_breakers)} 个开着" if open_breakers else "都没开"},
    ]
    if eta:
        metrics.append({"label": "预计清空", "value": f"约 {eta} 小时"})

    paused = bool(snap.get("paused"))
    if not snap.get("enabled"):
        status, status_text = "disabled", "已关闭"
        summary = "探测功能关了（环境变量），不会自动探测新条目"
    elif paused:
        status, status_text = "disabled", "已暂停"
        summary = "探测被手动暂停了，恢复后会接着做"
    elif open_breakers:
        status, status_text = "warning", "留意"
        summary = f"{len(open_breakers)} 个挂载的熔断器开着，相关探测暂停重试"
    elif failed > 0 and fail_rate >= 20 and total >= 20:
        status, status_text = "warning", "留意"
        summary = f"失败率 {fail_rate:.1f}%（{failed}/{total}），点「元数据来源」看具体报错"
    elif pending > 0:
        status, status_text = "running", "运行中"
        summary = f"还有 {pending} 条排队" + (f"，约 {eta} 小时清完" if eta else "")
    else:
        status, status_text = "ok", "正常"
        summary = f"队列清空，共探测完 {done} 条"

    return _card(
        "probe", "媒体探测", status, status_text, summary,
        metrics=metrics,
        switch={"label": "探测开关", "enabled": not paused},
        link="/metadata-sources",
    )


# ---------------------------------------------------------------------------
# 3. 元数据刮削（enrich）
# ---------------------------------------------------------------------------

def _task_enrich(db: Session) -> dict:
    from backend.emby_server import enrich_worker

    prog = enrich_worker.get_progress()
    en = prog.get("enrich") or {}
    pending = int(en.get("pending") or 0)
    enriching = int(en.get("enriching") or 0)
    done = int(en.get("done") or 0)
    failed = int(en.get("failed") or 0)
    backlog = pending + enriching
    night = prog.get("episode_night") or {}
    night_enabled = bool(night.get("enabled"))
    in_window = bool(night.get("in_window"))
    night_text = "没开"
    if night_enabled:
        window = f"{night.get('night_start', '')}–{night.get('night_end', '')}"
        night_text = f"{window}（{'窗口内' if in_window else '窗口外'}）"

    metrics = [
        {"label": "待刮削", "value": f"{pending} 条"},
        {"label": "刮削中", "value": f"{enriching} 条"},
        {"label": "已完成", "value": f"{done} 条"},
        {"label": "失败", "value": f"{failed} 条"},
        {"label": "单集夜间刮削", "value": night_text},
    ]
    if night.get("from_env"):
        metrics.append({"label": "提醒", "value": "夜间配置被环境变量接管，后台改了不生效"})

    if not prog.get("enabled"):
        status, status_text = "disabled", "已关闭"
        summary = "刮削 worker 关了（环境变量），新条目不会自动补海报简介"
    elif backlog > 0:
        status, status_text = "running", "运行中"
        summary = f"还有 {backlog} 条没刮完（待 {pending} / 进行中 {enriching}）"
    elif failed > 0:
        status, status_text = "warning", "留意"
        summary = f"队列清空了，但有 {failed} 条刮失败，点「元数据来源」看原因"
    else:
        status, status_text = "ok", "正常"
        summary = f"全部刮完，共 {done} 条"

    return _card(
        "enrich", "元数据刮削", status, status_text, summary,
        metrics=metrics,
        switch={"label": "单集夜间刮削", "enabled": night_enabled,
                "keep": {"night_start": night.get("night_start") or "02:00",
                         "night_end": night.get("night_end") or "06:00",
                         "night_tz": night.get("night_tz") or "Asia/Shanghai"}},
        link="/metadata-sources",
    )


# ---------------------------------------------------------------------------
# 5. 缩略图补生成
# ---------------------------------------------------------------------------

def _task_thumbnail(db: Session) -> dict:
    from backend.emby_server import image_store
    from backend.emby_server import models as em

    st = image_store.backfill_status(db)
    last_id = int(st.get("last_id") or 0)
    max_id = db.query(func.max(em.MediaItem.id)).scalar() or 0
    pct = (last_id / max_id * 100) if max_id else 100.0
    enabled = bool(st.get("enabled"))

    metrics = [
        {"label": "补到", "value": f"id {last_id} / {max_id}（约 {pct:.0f}%）"},
        {"label": "尺寸档", "value": "、".join(str(w) for w in (st.get("widths") or [])) or "—"},
    ]

    if not enabled:
        status, status_text = "disabled", "已关闭"
        summary = "图片本地化关了（环境变量），缩略图不会补"
    elif pct >= 99.9:
        status, status_text = "ok", "补完了"
        summary = "历史缩略图已经补完，新条目进来会自动带上"
    else:
        status, status_text = "running", "补生成中"
        summary = f"正在按 id 顺序补，约 {pct:.0f}%，由定时维护推进"

    # 开关是环境变量（EMBY_LOCALIZE_IMAGES），没有管理后台 API：只读展示
    return _card("thumbnail", "缩略图补生成", status, status_text, summary,
                 metrics=metrics)


# ---------------------------------------------------------------------------
# 6. 追新（新资源监听）
# ---------------------------------------------------------------------------

def _task_chase_new(db: Session) -> dict:
    from backend.emby_server import change_watcher
    from backend.emby_server import models as em

    cfg = change_watcher.get_config(db)
    enabled = bool(cfg.get("enabled"))
    last_check = cfg.get("last_check") or None
    last_found = int(cfg.get("last_found") or 0)
    try:
        interval = int(cfg.get("interval") or 0)
    except Exception:  # noqa: BLE001
        interval = 0

    dc = {"running": False, "last_poll": None, "last_changes": 0,
          "last_libs_triggered": 0}
    try:
        from backend.emby_server import drive_changes as _dc
        dc = _dc.status()
    except Exception:  # noqa: BLE001
        logger.debug("drive_changes.status() 读失败", exc_info=True)

    latest = (db.query(em.ChaseRun)
              .order_by(em.ChaseRun.id.desc()).first())
    alert_sources = (db.query(func.count(em.ChaseSourceState.source_key))
                     .filter(em.ChaseSourceState.consec_failures >= 3)
                     .scalar() or 0)

    metrics = [
        {"label": "检查间隔", "value": f"每 {interval} 分钟" if interval else "—"},
        {"label": "上次发现", "value": f"{last_found} 个新文件"},
        {"label": "Drive 轮询", "value": "跑着" if dc.get("running") else "没在跑"},
        {"label": "连续失败的数据源", "value": f"{alert_sources} 个" if alert_sources else "没有"},
    ]

    if not enabled:
        status, status_text = "disabled", "已关闭"
        summary = "追新关了，新资源不会自动发现，要手动扫"
    elif alert_sources:
        status, status_text = "warning", "留意"
        summary = f"{alert_sources} 个数据源连续失败 ≥3 次，点「媒体库」看追新配置"
    elif dc.get("running"):
        status, status_text = "running", "监听中"
        summary = "Drive 变更轮询跑着" + (f"，上次 {dc.get('last_poll')}" if dc.get("last_poll") else "")
    elif last_check:
        status, status_text = "ok", "正常"
        summary = f"上次检查 {last_check}，发现 {last_found} 个新文件"
    else:
        status, status_text = "idle", "没跑过"
        summary = "开关开着但还没跑过一次检查"

    return _card(
        "chase_new", "追新（新资源监听）", status, status_text, summary,
        metrics=metrics, last_run=latest.started_at.isoformat() if latest and latest.started_at else None,
        last_run_label="最近一次检查",
        switch={"label": "追新开关", "enabled": enabled,
                "keep": {"interval": interval,
                         "excluded": cfg.get("excluded") or ""}},
        link="/emby",
    )


# ---------------------------------------------------------------------------
# 聚合入口
# ---------------------------------------------------------------------------

_BUILDERS = (
    ("scan", "媒体库扫描", "_task_scan"),
    ("probe", "媒体探测", "_task_probe"),
    ("enrich", "元数据刮削", "_task_enrich"),
    ("thumbnail", "缩略图补生成", "_task_thumbnail"),
    ("chase_new", "追新（新资源监听）", "_task_chase_new"),
)
# 注：存函数名而不是函数对象，测试时 monkeypatch 模块属性才能生效。


@tasks_router.get("/tasks/overview")
def tasks_overview(db: Session = Depends(get_db),
                   admin=Depends(get_current_admin)):
    """任务总览：一次返回所有后台任务的状态、进度、开关。

    每个任务独立采集、独立降级：单个失败不影响其余卡片。
    """
    tasks = []
    for key, title, builder_name in _BUILDERS:
        try:
            tasks.append(globals()[builder_name](db))
        except Exception as e:  # noqa: BLE001
            logger.warning("任务总览采集失败 [%s]: %s", key, e, exc_info=True)
            try:
                db.rollback()
            except Exception:  # noqa: BLE001
                pass
            tasks.append(_failed_card(key, title, str(e)))
    return {
        "success": True,
        "fetched_at": datetime.now().isoformat(timespec="seconds"),
        "tasks": tasks,
    }
