"""前台用户活跃打点——跨进程共享最近一次前台用户请求的时间。

供 maintenance 的重活（缩略图补生成等）在用户活跃时让路。

存储：Redis 全进程共享（key 见 FOREGROUND_REDIS_KEY），写入/读取都优先走
Redis；Redis 不可用时优雅降级到进程内变量（行为不比旧版差）。时间源：
进程内用 time.monotonic（NTP 跳变不影响）；Redis 里存 wall clock（与既有
note_redis 保持一致，秒级精度即可，同一机器读写无时钟差问题）。

窗口秒数由调用方读环境变量 FOREGROUND_ACTIVE_SECONDS（默认 60）。
线程安全；任何异常永不抛给调用方（中间件里调用，绝不能影响主链路）。
"""
from __future__ import annotations

import logging
import os
import threading
import time
from backend.emby_server.env_util import env_float

logger = logging.getLogger(__name__)

_last_ts = 0.0  # 降级用：Redis 不可用时的进程内打点（time.monotonic）
_lock = threading.Lock()

FOREGROUND_REDIS_KEY = "aetrix:foreground:last_api_ts"
FOREGROUND_REDIS_TTL_S = 300


def _redis():
    """拿 Redis 客户端（backend.database.redis_client，与 scan_queue_redis 同一套）。

    拿不到（未配置/未连上）返回 None，调用方按降级路径处理。"""
    try:
        from backend.database import redis_client

        return redis_client
    except Exception:
        return None


def note() -> None:
    """记录一次前台请求（emby_api 中间件对 /emby/ 用户路径调用）。

    优先写 Redis（全进程可见），Redis 不可用时只写进程内变量。永不抛异常。
    """
    global _last_ts
    try:
        with _lock:
            _last_ts = time.monotonic()
    except Exception:
        pass
    try:
        rc = _redis()
        if rc is not None:
            rc.setex(FOREGROUND_REDIS_KEY, FOREGROUND_REDIS_TTL_S, time.time())
    except Exception:
        pass


def _seconds_since() -> float | None:
    """距上次前台请求的秒数；取 Redis 与进程内两个来源中较新的，都没有返回 None。

    Redis 是跨进程共享的主来源；进程内是降级来源（Redis 抖动/未启用时兜底）。
    取较新者是为了不漏掉任何一次活跃信号（漏报=后台重活不给前台让路）。
    """
    best: float | None = None
    try:
        rc = _redis()
        if rc is not None:
            v = rc.get(FOREGROUND_REDIS_KEY)
            if v is not None:
                if isinstance(v, bytes):
                    v = v.decode("utf-8", "ignore")
                best = time.time() - float(v)
    except Exception:
        pass
    try:
        with _lock:
            ts = _last_ts
        if ts > 0:
            local_ago = time.monotonic() - ts
            if best is None or local_ago < best:
                best = local_ago
    except Exception:
        pass
    return best


def active(within_seconds: float = 60.0) -> bool:
    """最近 within_seconds 秒内是否有前台请求（跨进程）。"""
    try:
        ago = _seconds_since()
        return ago is not None and ago <= max(0.0, within_seconds)
    except Exception:
        return False


def last_seen_ago() -> float | None:
    """距上次前台请求的秒数（跨进程）；从未记录返回 None。"""
    try:
        return _seconds_since()
    except Exception:
        return None


def reset_for_tests() -> None:
    """测试用：清零进程内打点与当前线程的让路配额状态。"""
    global _last_ts
    try:
        with _lock:
            _last_ts = 0.0
    except Exception:
        pass
    try:
        _yield_local.exempt = False
        if hasattr(_yield_local, "segment_start"):
            del _yield_local.segment_start
    except Exception:
        pass


# ---------------------------------------------------------------------------
# 跨进程前台打点（2026-10-16 网页加速批）：扫描跑在 worker 进程，看不见 EA 的
# 进程内打点。用 Redis 做跨进程共享：aetrix-api 的限流中间件每次复用线程池
# 调用时顺手 SETEX（零新增请求路径），worker 的扫描在批次边界读它让路。
# Redis 故障/未启用时全部退化成"不让路"（=现状），永不抛异常。
# ---------------------------------------------------------------------------


def note_redis(redis_client=None) -> None:
    """跨进程打点一次前台用户请求。永不抛异常（请求热路径调用）。"""
    try:
        rc = redis_client if redis_client is not None else _redis()
        if rc is None:
            return
        rc.setex(FOREGROUND_REDIS_KEY, FOREGROUND_REDIS_TTL_S, time.time())
    except Exception:
        pass


def seconds_since_redis_activity(redis_client=None):
    """距上次跨进程打点的秒数；无打点/Redis 不可用返回 None。永不抛异常。"""
    try:
        rc = redis_client if redis_client is not None else _redis()
        if rc is None:
            return None
        v = rc.get(FOREGROUND_REDIS_KEY)
        if v is None:
            return None
        if isinstance(v, bytes):
            v = v.decode("utf-8", "ignore")
        return time.time() - float(v)
    except Exception:
        return None


# ---- 让路配额（2026-10-23 用户拍板）----
# 旧语义：批次边界一发现前台活跃就小睡到前台安静或触及单次上限——前台
# 长期有人时扫描「干一点、睡满一轮」，用户越多越跑不完（2026-10-23 生产
# 实证：245 用户在线时让路把国产剧整库增量拖成以天计）。现改为配额制：
# 本线程先连续干满 SCAN_FOREGROUND_WORK_MIN_S 秒才换取一次让路，单次让路
# ≤ 已干时长 × SCAN_FOREGROUND_YIELD_RATIO（且 ≤ SCAN_FOREGROUND_MAX_YIELD_S）；
# 保证后台活至少约 1/(1+ratio) 的时间在真干活，前台再忙也跑得完。
# 定向扫描（新片入库、只扫几个变化目录）由 scanner 开扫时标记豁免，完全
# 不让——它短、用户正等着新片上架。
_yield_local = threading.local()


def set_yield_exempt(exempt: bool) -> None:
    """标记当前线程的活是否豁免让路（scanner 对定向扫描设置）。永不抛异常。"""
    try:
        _yield_local.exempt = bool(exempt)
    except Exception:
        pass


def reset_yield_clock() -> None:
    """新一轮活开始时重置配额时钟（scanner 开扫时调用）。永不抛异常。"""
    try:
        _yield_local.segment_start = time.monotonic()
    except Exception:
        pass


def maybe_yield_for_foreground(tag: str = "scan") -> float:
    """前台活跃时按配额让路。返回让出的秒数。

    语义：只延迟、不丢活——重活照样跑完，只是给正在用网页的用户让 CPU。
    配额：先连续干满 ``SCAN_FOREGROUND_WORK_MIN_S``（默认 60）秒，才许让
    一次；单次让路时长 ≤ 已干时长 × ``SCAN_FOREGROUND_YIELD_RATIO``
    （默认 0.25），且 ≤ ``SCAN_FOREGROUND_MAX_YIELD_S``。让路途中前台
    安静（超过 ``SCAN_FOREGROUND_WINDOW_S`` 没有请求）立即结束。
    开关 ``SCAN_FOREGROUND_YIELD=0`` 可整局关闭（=旧行为）。永不抛异常。
    """
    yielded = 0.0
    try:
        if os.getenv("SCAN_FOREGROUND_YIELD", "1") != "1":
            return 0.0
        if getattr(_yield_local, "exempt", False):
            return 0.0
        window_s = env_float("SCAN_FOREGROUND_WINDOW_S", 120.0)
        sleep_s = env_float("SCAN_FOREGROUND_SLEEP_S", 5.0)
        max_s = env_float("SCAN_FOREGROUND_MAX_YIELD_S", 900.0)
        if sleep_s <= 0 or max_s <= 0:
            return 0.0
        work_min_s = env_float("SCAN_FOREGROUND_WORK_MIN_S", 60.0)
        ratio = env_float("SCAN_FOREGROUND_YIELD_RATIO", 0.25)
        now = time.monotonic()
        segment_start = getattr(_yield_local, "segment_start", None)
        if segment_start is None:
            # 本线程第一次调用：从现在开始计干活时长，本次不让
            _yield_local.segment_start = now
            return 0.0
        worked_s = now - segment_start
        if worked_s < work_min_s:
            return 0.0
        cap_s = min(max_s, worked_s * ratio) if ratio > 0 else 0.0
        if cap_s <= 0:
            return 0.0
        while yielded < cap_s:
            ago = seconds_since_redis_activity()
            if ago is None or ago > max(0.0, window_s):
                break
            step = min(sleep_s, cap_s - yielded)
            time.sleep(step)
            yielded += step
        # 无论让没让满，下一个干活时段从现在重新计时（防攒出超长让路）
        _yield_local.segment_start = time.monotonic()
        if yielded > 0:
            logger.info("后台让路：前台 %ss 内有用户请求，%s 已干 %.0fs，暂停 %.0fs",
                        window_s, tag, worked_s, yielded)
    except Exception:
        pass
    return yielded
