"""前台用户活跃打点——EA 进程内记录最近一次前台用户请求的时间。

供 maintenance 的重活（缩略图补生成等）在用户活跃时让路。只用 time.monotonic
（不碰 wall clock，NTP 跳变不影响）；线程安全；任何异常永不抛给调用方
（中间件里调用，绝不能影响主链路）。

窗口秒数由调用方读环境变量 FOREGROUND_ACTIVE_SECONDS（默认 60）。
"""
from __future__ import annotations

import threading
import time

_last_ts = 0.0
_lock = threading.Lock()


def note() -> None:
    """记录一次前台请求（EA 中间件对 /emby/ 用户路径调用）。"""
    global _last_ts
    try:
        with _lock:
            _last_ts = time.monotonic()
    except Exception:
        pass


def active(within_seconds: float = 60.0) -> bool:
    """最近 within_seconds 秒内是否有前台请求。"""
    try:
        with _lock:
            ts = _last_ts
        return ts > 0 and (time.monotonic() - ts) <= max(0.0, within_seconds)
    except Exception:
        return False


def last_seen_ago() -> float | None:
    """距上次前台请求的秒数；从未记录返回 None。"""
    try:
        with _lock:
            ts = _last_ts
        if ts <= 0:
            return None
        return time.monotonic() - ts
    except Exception:
        return None


def reset_for_tests() -> None:
    """测试用：清零打点。"""
    global _last_ts
    try:
        with _lock:
            _last_ts = 0.0
    except Exception:
        pass
