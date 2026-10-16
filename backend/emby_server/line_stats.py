"""播放可观测（2026-10 简化）：进程内计数器

只有一条播放路径（中转），不再按线路分桶。

## 为什么是进程内而不是落库

计的是**这一进程经手了多少播放请求 / 多少字节出流量**。这类数字有两个特点：

1. 天然是「本进程」口径——流量确实只从本机出去，跨节点合并反而会重复计算
   （一体化部署时 EM=EA；分离部署时每个 EA 各出各的流）。所以面板上标注
   「本进程」，不谎报一个跨节点的水位。
2. 落库要**每次播放写一行**，这是播放热路径；而命中率这种**必须跨重启存活**
   的数字，历史上有 `LocalCacheStat` 单行表在管（本地缓存已随中继全删移除）。

计数失败绝不能影响播放：所有写入口都吞异常（``except Exception``），
最坏情况是这一轮数字没记上，播放照常。
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Optional

MB = 1024 * 1024


@dataclass
class TrafficCounter:
    """本进程的播放累计计数（单路径）"""

    requests: int = 0
    bytes_out: int = 0
    last_used_at: float = 0.0

    def as_dict(self) -> dict:
        last = self.last_used_at or 0.0
        return {
            "requests": self.requests,
            "bytes_out": self.bytes_out,
            "last_used_at": last,
            "idle_seconds": int(max(0.0, time.time() - last)) if last else None,
        }


_LOCK = threading.Lock()
_COUNTER = TrafficCounter()
_STARTED_AT = time.time()


def record_request(line: str = "") -> None:
    """记一次播放请求（`line` 参数已废弃，保留仅为兼容旧调用）"""
    try:
        with _LOCK:
            _COUNTER.requests += 1
            _COUNTER.last_used_at = time.time()
    except Exception:  # noqa: BLE001 — 计数不该影响播放
        pass


def record_bytes(count: int, line: str = "") -> None:
    """记一次出流量（``count<=0`` 忽略）

    口径：**本服务响应体里真正吐出去的字节**。不包含 ffmpeg 转码时的拉流，
    也不包含 StarletteFileResponse 整文件直发（它不走 body_iterator，
    宁可少算也不按 Content-Length 虚报）。
    """
    if count <= 0:
        return
    try:
        with _LOCK:
            _COUNTER.bytes_out += int(count)
    except Exception:  # noqa: BLE001 — 计数不该影响播放
        pass


def snapshot(lines: tuple = ()) -> list[dict]:
    """导出计数（`lines` 参数已废弃，保留仅为兼容旧调用）"""
    try:
        with _LOCK:
            d = _COUNTER.as_dict()
            if lines:
                return [{**d, "line": line} for line in lines]
            return [d]
    except Exception:  # noqa: BLE001
        return []


def counter(line: str = "") -> Optional[dict]:
    """计数快照（`line` 参数已废弃）"""
    rows = snapshot()
    return rows[0] if rows else None


def uptime_seconds() -> int:
    """本进程已运行多久（面板标注「本进程」口径时要用）"""
    return int(max(0.0, time.time() - _STARTED_AT))


def reset() -> None:
    """测试用：清空计数（**不要**在生产路径调用）"""
    global _COUNTER
    with _LOCK:
        _COUNTER = TrafficCounter()


def format_bytes(count: int) -> str:
    """给人看的字节数（面板直接用，省得前端再写一份）"""
    value = float(max(0, int(count or 0)))
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024 or unit == "TB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} TB"


__all__ = [
    "TrafficCounter", "MB", "counter", "format_bytes", "record_bytes", "record_request",
    "reset", "snapshot", "uptime_seconds",
]
