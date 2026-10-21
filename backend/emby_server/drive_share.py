# -*- coding: utf-8 -*-
"""Drive 文件链接分享自动补开（2026-10-19 播放事故治本）。

病根：播放走 302 到 ``drive.usercontent.google.com/download``，播放器匿名自拉；
文件没有 anyone reader 权限时谷歌回登录页 HTML，播不了。2026-10-17 曾一次性
给全库开过分享，但之后新入库的文件没有任何自动开分享环节——服务器的存在性
校验用 SA 凭据（能看见文件）所以静默，播放器却拉不到字节（《狐妖小红娘》
整批即此中招）。

本模块两轨：
- 主路径：扫描器新条目入库提交后，把 (item_id, drive_file_id) 丢进内存队列，
  后台线程带随机抖动、走 SA 池轮换逐个补开。补开失败绝不影响扫描本身。
- 兜底：enrich janitor 周期调用 ``drive_share_patrol``，把主路径漏网（进程重启
  丢队列）或失败的行再捞一遍。

调用经济性：补开用 POST permissions.create 一次调用；文件已分享时谷歌回
409 duplicate——409 即视为「已分享」标记 ok，不再另做一次 GET 查询，省一半调用。

反风控：调用走 ``drive_auth.get_drive_bearer_token()`` 的 SA 轮换；每次调用间
随机抖动；遇配额/限流错误整体退避、 bounded 重试后交巡检慢慢消化。
"""
from __future__ import annotations

import collections
import logging
import os
import random
import threading
import time
from datetime import datetime, timedelta

import httpx

logger = logging.getLogger(__name__)

#: 总开关（默认开；关掉去文件配置/env 里设 0，不在管理面板留开关——
#: 2026-10-19 用户亲定：功能默认开、要关去文件配置）。
DRIVE_SHARE_AUTO_ENABLED = (
    (os.getenv("DRIVE_SHARE_AUTO_ENABLED", "1") or "1").strip().lower()
    not in {"0", "false", "no", "off"}
)

#: 补开权限的 Drive API 端点（模板）。
_PERMISSIONS_URL_TMPL = (
    "https://www.googleapis.com/drive/v3/files/{fid}/permissions"
    "?supportsAllDrives=true"
)

#: 限流类错误原因（命中任一即整体退避，不硬顶）。
_RATE_LIMIT_REASONS = frozenset({
    "rateLimitExceeded",
    "userRateLimitExceeded",
    "quotaExceeded",
    "dailyLimitExceeded",
})

#: 调用间隔随机抖动（秒）：每次 API 调用前睡一段随机时长，不许规律节拍。
_JITTER_MIN_SEC = 0.5
_JITTER_MAX_SEC = 2.0
#: 遇限流后的整体退避（秒，随机）。
_BACKOFF_MIN_SEC = 120.0
_BACKOFF_MAX_SEC = 300.0
#: 队列内单个文件最大尝试次数（超限后标记失败，交巡检次日再捞）。
_MAX_ATTEMPTS = 5
#: 巡检单轮硬上限（个文件）。
_PATROL_LIMIT = 150
#: 巡检对失败/未确认行的重捞间隔（小时）。
_PATROL_RETRY_HOURS = 24
#: 巡检跳过内存队列快照的上限（个 file_id）：在队文件数超过该值时不过滤，
#: 退化为现状（大批量入库时宁可重复调用，也不构造巨型 IN 查询）。
_PATROL_SKIP_INFLIGHT_CAP = 2000

#: 可注入的 HTTP 入口（测试桩打这里，不碰 httpx 全局）。
_http_post = httpx.post

_queue: collections.deque = collections.deque()
_in_flight: set = set()
_lock = threading.Lock()
_worker_started = False
_backoff_until = 0.0
_attempts: dict = {}


def _share_url(file_id: str) -> str:
    return _PERMISSIONS_URL_TMPL.format(fid=file_id)


def _is_rate_limit_body(resp) -> bool:
    """响应体里是否含限流类错误原因。解析失败按「不是」处理（调用方按失败走）。"""
    try:
        payload = resp.json()
    except Exception:
        return False
    try:
        errors = (payload.get("error") or {}).get("errors") or []
        for err in errors:
            if str(err.get("reason") or "") in _RATE_LIMIT_REASONS:
                return True
    except Exception:
        return False
    return False


def ensure_shared(file_id: str, bearer_token: str | None) -> str:
    """给单个 Drive 文件补开 anyone reader 权限。

    返回 'ok'（本次创建）/ 'already'（已分享，409 幂等确认）/
    'rate_limited'（配额/限流，调用方退避）/ 'failed'（其它，一律不抛异常）。
    """
    if not file_id or not bearer_token:
        return "failed"
    try:
        resp = _http_post(
            _share_url(file_id),
            headers={"Authorization": "Bearer " + bearer_token},
            json={"type": "anyone", "role": "reader"},
            timeout=10.0,
        )
    except Exception:
        return "failed"
    try:
        code = int(resp.status_code)
    except Exception:
        return "failed"
    try:
        from backend.emby_server import playback_risk
        playback_risk.note_drive_status(code)
    except Exception:  # noqa: BLE001 — 观测点永不影响补开分享
        pass
    if code in (200, 201):
        return "ok"
    if code == 409:
        # 文件已有相同权限：幂等确认，不再补查，省一次调用。
        return "already"
    if code in (403, 429) and _is_rate_limit_body(resp):
        return "rate_limited"
    return "failed"


def _mark_share_result(item_id: int, ok: bool) -> None:
    """把补开结果写回条目行（独立短会话；永不抛异常）。"""
    try:
        from backend.database import SessionLocal
        from backend.emby_server import models as em

        db = SessionLocal()
        try:
            item = db.get(em.MediaItem, int(item_id))
            if item is not None:
                item.drive_share_ok = bool(ok)
                item.drive_share_checked_at = datetime.now()
                db.commit()
        finally:
            db.close()
    except Exception:
        logger.warning("drive-share 写回条目 %s 失败", item_id, exc_info=True)


def _process_one_share(item_id: int, file_id: str) -> str:
    """处理队列里的一项：取轮换 token → 补开 → 写回；限流时退避并重排。"""
    global _backoff_until
    token = None
    try:
        from backend.emby_server import drive_auth

        token = drive_auth.get_drive_bearer_token()
    except Exception:
        token = None
    outcome = ensure_shared(file_id, token) if token else "failed"
    if outcome == "rate_limited":
        n = _attempts.get(file_id, 0) + 1
        _attempts[file_id] = n
        _backoff_until = time.monotonic() + random.uniform(
            _BACKOFF_MIN_SEC, _BACKOFF_MAX_SEC)
        logger.warning("drive-share 撞限流，整体退避（文件 %s 第 %d 次）",
                       file_id, n)
        if n < _MAX_ATTEMPTS:
            with _lock:
                _queue.appendleft((item_id, file_id))
                _in_flight.add(file_id)
            return outcome
        # 超过重试上限：标记失败，交巡检次日再捞，不在队列里死磕。
        outcome = "failed"
    _mark_share_result(item_id, outcome in ("ok", "already"))
    with _lock:
        # 终态清理该文件的重试计数：成功/失败都不再需要；同一 file_id 将来
        # 若再次入队也不应继承旧计数；同时防止 _attempts 随限流文件数无限增长。
        _attempts.pop(file_id, None)
    return outcome


def _share_worker() -> None:
    """后台线程：串行消费队列，每次调用间随机抖动。"""
    logger.info("drive-share 后台线程启动（开关=%s）", DRIVE_SHARE_AUTO_ENABLED)
    while True:
        file_id = None
        outcome = "failed"
        try:
            if not DRIVE_SHARE_AUTO_ENABLED:
                time.sleep(60)
                continue
            if time.monotonic() < _backoff_until:
                time.sleep(5)
                continue
            with _lock:
                job = _queue.popleft() if _queue else None
            if job is None:
                time.sleep(5)
                continue
            item_id, file_id = job
            # 调用间隔随机抖动：反风控，不许规律节拍。
            time.sleep(random.uniform(_JITTER_MIN_SEC, _JITTER_MAX_SEC))
            outcome = _process_one_share(item_id, file_id)
        except Exception:
            logger.warning("drive-share 后台线程单轮异常", exc_info=True)
            outcome = "failed"
            time.sleep(5)
        # 限流重排的那一项已在 _process_one_share 内保留 _in_flight 标记；
        # 其余一律清掉（失败的交巡检次日再捞）。
        if file_id is not None and outcome != "rate_limited":
            with _lock:
                _in_flight.discard(file_id)


def _ensure_worker_locked() -> None:
    """启动后台线程（调用方须已持 _lock；只启动一次）。"""
    global _worker_started
    if _worker_started:
        return
    t = threading.Thread(target=_share_worker, name="drive-share", daemon=True)
    t.start()
    _worker_started = True


def _in_flight_snapshot() -> frozenset:
    """内存队列中待处理 file_id 快照（巡检用，避免与 worker 重复补开）。"""
    with _lock:
        return frozenset(_in_flight)


def note_new_file(item_id, file_id, is_new) -> bool:
    """扫描器钩子：新入库的 Drive 条目入队待补开。

    纯内存操作、无 IO，扫描事务提交后调用，不阻塞扫描。
    返回 True=已入队；False=跳过（非新条目/无 file_id/开关关）。
    """
    if not (is_new and file_id and item_id):
        return False
    if not DRIVE_SHARE_AUTO_ENABLED:
        return False
    fid = str(file_id)
    with _lock:
        if fid in _in_flight:
            return False
        _in_flight.add(fid)
        _queue.append((int(item_id), fid))
        _ensure_worker_locked()
    return True


def drive_share_patrol(db, limit: int = _PATROL_LIMIT) -> int:
    """enrich janitor 兜底巡检：把未确认分享的行逐个补开。

    选行：可见、有 drive_file_id、未确认 ok、且（从未检查 或 上次检查超过
    24 小时）。每文件 1 次 API 调用（409 即确认），调用间随机抖动；
    撞限流立即收手、留待下轮。返回确认 ok 的个数。
    """
    if not DRIVE_SHARE_AUTO_ENABLED:
        return 0
    try:
        from backend.emby_server import models as em

        cutoff = datetime.now() - timedelta(hours=_PATROL_RETRY_HOURS)
        rows = (
            db.query(em.MediaItem.id, em.MediaItem.drive_file_id)
            .filter(
                em.MediaItem.drive_file_id.isnot(None),
                em.MediaItem.drive_file_id != "",
                em.MediaItem.deleted_at.is_(None),
                em.MediaItem.merged_into_id.is_(None),
                em.MediaItem.drive_share_ok.isnot(True),
                ((em.MediaItem.drive_share_checked_at.is_(None))
                 | (em.MediaItem.drive_share_checked_at < cutoff)),
            )
            .order_by(em.MediaItem.id)
            .limit(int(limit))
            .all()
        )
    except Exception:
        logger.warning("drive-share 巡检查询失败", exc_info=True)
        return 0
    if not rows:
        return 0
    # 主路径（扫描入库后内存队列）里待处理的行不是「漏网」：worker 几分钟内会
    # 处理，巡检跳过它们，避免大批量入库时对同一文件发重复的补开调用
    # （409 幂等收敛、正确性无损，但烧配额）。快照过大时不过滤，退化为现状。
    in_flight = _in_flight_snapshot()
    if in_flight and len(in_flight) <= _PATROL_SKIP_INFLIGHT_CAP:
        rows = [r for r in rows if r[1] not in in_flight]
        if not rows:
            return 0
    try:
        from backend.emby_server import drive_auth
    except Exception:
        logger.warning("drive-share 巡检加载 drive_auth 失败", exc_info=True)
        return 0
    confirmed = 0
    for item_id, file_id in rows:
        if not DRIVE_SHARE_AUTO_ENABLED:
            break
        time.sleep(random.uniform(_JITTER_MIN_SEC, _JITTER_MAX_SEC))
        token = None
        try:
            token = drive_auth.get_drive_bearer_token()
        except Exception:
            token = None
        outcome = ensure_shared(str(file_id), token) if token else "failed"
        if outcome == "rate_limited":
            global _backoff_until
            _backoff_until = time.monotonic() + random.uniform(
                _BACKOFF_MIN_SEC, _BACKOFF_MAX_SEC)
            logger.warning("drive-share 巡检撞限流，本轮收手")
            break
        try:
            item = db.get(em.MediaItem, int(item_id))
            if item is not None:
                item.drive_share_ok = outcome in ("ok", "already")
                item.drive_share_checked_at = datetime.now()
                db.commit()
                if item.drive_share_ok:
                    confirmed += 1
        except Exception:
            db.rollback()
            logger.warning("drive-share 巡检写回条目 %s 失败", item_id,
                           exc_info=True)
    if confirmed:
        logger.info("drive-share 巡检确认 %d 个文件", confirmed)
    return confirmed
