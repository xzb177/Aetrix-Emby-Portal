"""定时扫描：用户可控的自动扫描计划

要解决的问题：新资源传到云盘后不会自动入库，得有人去后台手动点扫描。

但"什么时候扫"由用户决定（管理后台「元数据与刮削」分组里的「定时扫描」
开关 + 时间），而不是写死一个定时任务——这是产品原则"功能只提供能力"的体现。

- 配置（SystemConfig）：``auto_scan_enabled``（"1"/"0"，默认 "1" 开）、
  ``auto_scan_time``（"HH:MM"，默认 "03:00"，服务器本地时间）、
  ``auto_scan_last_run``（"YYYY-MM-DD"，同一天不重复跑）。
- 调度：EM（面板）启动时 ``start_auto_scan_scheduler()`` 起一个 daemon 线程，
  每 60 秒检查一次；到点且当天没跑过，就把本机负责的启用库按 trigger="auto"
  入队。走 scan_queue：挂载冲突自动排队、重复入队自动合并。
- 安全：
  - 有扫描正在跑 / 排队时整轮跳过（不叠加、不打断手工扫描）；
  - 归属其它节点的库跳过（手动扫描会转发，定时任务不跨节点触发）；
  - 时间格式非法时记日志并按关闭处理，不抛错；
  - 任何异常只记日志，线程永不退出。
"""
from __future__ import annotations

import logging
import re
import threading
from datetime import datetime

from sqlalchemy.orm import Session

from backend import models as base_models
from backend.database import SessionLocal
from backend.emby_server import models as em
from backend.emby_server import nodes as node_lib
from backend.emby_server import scan_queue
from backend.integrations import store

logger = logging.getLogger(__name__)

CONFIG_ENABLED = "auto_scan_enabled"
CONFIG_TIME = "auto_scan_time"
CONFIG_LAST_RUN = "auto_scan_last_run"

DEFAULT_TIME = "03:00"
CHECK_INTERVAL = 60  # 秒

_TIME_RE = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")

_SCHEDULER_STARTED = False
_SCHEDULER_LOCK = threading.Lock()


def parse_time(value: str | None) -> str | None:
    """校验 "HH:MM"；合法返回规范化字符串，否则返回 None（调用方按关闭处理）。"""
    text = (value or "").strip()
    m = _TIME_RE.match(text)
    if not m:
        return None
    return f"{m.group(1)}:{m.group(2)}"


def get_config(db: Session) -> dict:
    """读定时扫描配置（管理后台展示用）。"""
    vals = store.read_values(
        db,
        [CONFIG_ENABLED, CONFIG_TIME, CONFIG_LAST_RUN],
        {CONFIG_ENABLED: "1", CONFIG_TIME: DEFAULT_TIME, CONFIG_LAST_RUN: ""},  # 2026-10-21 摘面板对齐：缺省=生产现值（开）
    )
    enabled = vals[CONFIG_ENABLED].strip().lower() in ("1", "true", "yes", "on")
    time_str = parse_time(vals[CONFIG_TIME]) or DEFAULT_TIME
    return {
        "enabled": enabled,
        "time": time_str,
        "last_run": vals[CONFIG_LAST_RUN].strip(),
    }


def save_config(db: Session, enabled: bool, time_str: str) -> dict:
    """保存定时扫描配置；时间格式非法时抛 ValueError（接口转 400）。

    开关落库（DB 行 > 代码缺省 "1"）。缺省保持开，与 #1163 对齐的生产现值一致。
    """
    parsed = parse_time(time_str)
    if parsed is None:
        raise ValueError(f"时间格式非法，应为 HH:MM（24 小时制），收到：{time_str!r}")
    store.write_values(
        db,
        {CONFIG_ENABLED: "1" if enabled else "0", CONFIG_TIME: parsed},
        {
            CONFIG_ENABLED: "定时扫描开关（元数据与刮削）",
            CONFIG_TIME: "定时扫描每天执行时间（HH:MM，服务器本地时间）",
        },
    )
    db.commit()
    return get_config(db)


def _should_run_today(now: datetime, time_str: str, last_run: str) -> bool:
    """到点了且今天没跑过。"""
    return now.strftime("%H:%M") == time_str and last_run != now.strftime("%Y-%m-%d")


def _enqueue_all(db: Session) -> dict:
    """把本机负责的启用库全部入队；返回 {enqueued, skipped_remote}。"""
    snap = scan_queue.snapshot()
    if snap.get("running") or snap.get("waiting"):
        logger.info("定时扫描跳过：已有扫描正在进行或排队")
        return {"enqueued": 0, "skipped_busy": True}
    self_id = node_lib.self_node_id(db)
    libs = (
        db.query(em.Library)
        .filter(em.Library.is_enabled.is_(True))
        .order_by(em.Library.id)
        .all()
    )
    enqueued = 0
    skipped_remote = 0
    for lib in libs:
        owner = node_lib.library_owner(db, lib)
        if owner is not None and owner.id != self_id:
            skipped_remote += 1
            continue
        result = scan_queue.enqueue(lib, trigger="auto")
        if result.get("created"):
            enqueued += 1
    return {"enqueued": enqueued, "skipped_remote": skipped_remote}


def _tick() -> None:
    """一次检查：到点就入队。异常只记日志，不向上传播。"""
    db = SessionLocal()
    try:
        cfg = get_config(db)
        if not cfg["enabled"]:
            return
        now = datetime.now()
        if not _should_run_today(now, cfg["time"], cfg["last_run"]):
            return
        # 先占位：同一天只跑一次（多进程/线程并发时靠 DB 行的写入顺序，后写的覆盖，
        # 入队本身是幂等的——重复入队会被 scan_queue 合并，不会重复扫描）。
        store.write_values(
            db,
            {CONFIG_LAST_RUN: now.strftime("%Y-%m-%d")},
            {CONFIG_LAST_RUN: "定时扫描上次执行日期（防重复）"},
        )
        db.commit()
        summary = _enqueue_all(db)
        if summary.get("skipped_busy"):
            # 忙：把占位让出来，明天这个时间再试（今天不再打扰手工扫描）
            return
        logger.info(
            "定时扫描已触发：%s 入队 %s 个库，跳过归属其它节点 %s 个",
            cfg["time"], summary["enqueued"], summary["skipped_remote"],
        )
    except Exception as exc:  # noqa: BLE001 — 定时线程绝不能因一次失败退出
        logger.warning("定时扫描检查异常：%s", exc)
        try:
            db.rollback()
        except Exception:  # noqa: BLE001
            pass
    finally:
        db.close()


def start_auto_scan_scheduler() -> bool:
    """启动定时扫描线程（同一进程只启动一次；daemon，随进程退出）。

    只在 EM（面板）上启动：扫描入队与配置都在面板侧。
    """
    global _SCHEDULER_STARTED
    with _SCHEDULER_LOCK:
        if _SCHEDULER_STARTED:
            return False
        _SCHEDULER_STARTED = True

    def _loop() -> None:
        while True:
            threading.Event().wait(CHECK_INTERVAL)
            _tick()

    threading.Thread(target=_loop, daemon=True, name="auto-scan-scheduler").start()
    logger.info("定时扫描调度已启动（每 %s 秒检查一次，用户可在后台开关）", CHECK_INTERVAL)
    return True


__all__ = [
    "CONFIG_ENABLED",
    "CONFIG_LAST_RUN",
    "CONFIG_TIME",
    "DEFAULT_TIME",
    "get_config",
    "parse_time",
    "save_config",
    "start_auto_scan_scheduler",
]
