"""PlaybackInfo 文件头预热（点播即读文件头）。

**场景**：客户端请求 PlaybackInfo（``IsPlayback=true``）时，起播前把文件头
（默认 10MB）读一遍，让 rclone VFS / Drive 侧把热数据拉进缓存，
首包到来更快。

**不抢播放链路的设计**（详见各配置项注释）：

- **并发上限**：同时最多 ``max_concurrent`` 个预热线程（默认 4），超限
  **直接跳过、不排队**——排队会积压成隐形延迟，播放高峰时线程全被
  预热占住才是真正的抢资源。
- **分块小读**：每次只读 ``chunk_bytes``（默认 256KB），避免一次 10MB
  大块读把 FUSE/Drive 带宽打满；分块边界也是超时/取消的检查点。
- **低优先级**：工作线程尽力把本线程 nice 调高（Linux 下 ``setpriority``
  传入 tid 只影响调用线程，不影响 API 主进程）+ I/O 调度切 idle 类；
  失败则忽略，降级为普通线程，绝不报错。
- **短超时**：单次预热超过 ``timeout_seconds`` 自动放弃。
- **去重**：同一文件在 ``dedup_seconds`` 窗口内只预热一次，避免用户
  连点详情/刷新反复触发。

**已知边界**：超时/取消在分块边界检查——如果某次 ``read()`` 本身卡死
（如 FUSE 僵死），工作线程会多等这一次读返回；线程是 daemon 且并发有界，
最坏情况也只是占住几个槽位，不会拖住 API 进程退出（``shutdown`` join 有超时）。

所有阈值都可配置（SystemConfig，管理后台「系统设置 → 播放预热」，
修改后 60 秒内生效——读走 ``integrations.store`` 的热缓存）：

- ``playback_prewarm_enabled``（bool，默认 false）：总开关。默认关——
  预热读文件头会与真实播放争同一文件的网盘配额（2026-10-16 口径：
  预热全改可配开关、默认关），服主在管理后台开启后才生效。
- ``playback_prewarm_bytes``（int，默认 10485760）：每次预读字节数。
- ``playback_prewarm_max_concurrent``（int，默认 4）：并发上限。
- ``playback_prewarm_dedup_seconds``（int，默认 300）：同文件去重窗口。
- ``playback_prewarm_timeout_seconds``（int，默认 30）：单次超时。
- ``playback_prewarm_chunk_bytes``（int，默认 262144）：分块大小。

指标：``metrics_snapshot()`` 返回计数器 + 平均/最大耗时，
``/api/health`` 直接带上 ``playback_prewarm`` 一节。
"""

from __future__ import annotations

import ctypes
import logging
import os
import threading
import time
from typing import Any, Optional

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 配置
# ---------------------------------------------------------------------------

#: 配置键（前缀统一，管理后台按此前缀做写时失效）
KEY_ENABLED = "playback_prewarm_enabled"
KEY_BYTES = "playback_prewarm_bytes"
KEY_MAX_CONCURRENT = "playback_prewarm_max_concurrent"
KEY_DEDUP_SECONDS = "playback_prewarm_dedup_seconds"
KEY_TIMEOUT_SECONDS = "playback_prewarm_timeout_seconds"
KEY_CHUNK_BYTES = "playback_prewarm_chunk_bytes"

CONFIG_KEYS = (
    KEY_ENABLED,
    KEY_BYTES,
    KEY_MAX_CONCURRENT,
    KEY_DEDUP_SECONDS,
    KEY_TIMEOUT_SECONDS,
    KEY_CHUNK_BYTES,
)

#: 默认值（DB 里没有对应行时用这些）
DEFAULTS: dict[str, str] = {
    KEY_ENABLED: "false",
    KEY_BYTES: "10485760",          # 10MB
    KEY_MAX_CONCURRENT: "4",
    KEY_DEDUP_SECONDS: "300",       # 5 分钟
    KEY_TIMEOUT_SECONDS: "30",
    KEY_CHUNK_BYTES: "262144",      # 256KB：小块读，对播放链路温和
}

#: 各数值键的合法区间（超出按边界钳住，非法按默认）
_BOUNDS: dict[str, tuple[int, int]] = {
    KEY_BYTES: (0, 1 << 30),               # 0 = 读 0 字节（约等于关）
    KEY_MAX_CONCURRENT: (1, 64),
    KEY_DEDUP_SECONDS: (0, 86400),
    KEY_TIMEOUT_SECONDS: (1, 600),
    KEY_CHUNK_BYTES: (4096, 1 << 24),       # 4KB ~ 16MB
}


def _parse_bool(raw: str, default: bool) -> bool:
    text = (raw or "").strip().lower()
    if text in ("1", "true", "yes", "on"):
        return True
    if text in ("0", "false", "no", "off", ""):
        # 空串 = 行存在但被清空 → 按关处理？不：空串语义是"没填"，
        # 与默认值保持一致，避免管理员手滑清空导致功能静默关闭。
        return default if text == "" else False
    return default


def _parse_int(raw: str, key: str) -> int:
    default = int(DEFAULTS[key])
    try:
        value = int(str(raw).strip())
    except (TypeError, ValueError):
        return default
    lo, hi = _BOUNDS[key]
    return max(lo, min(hi, value))


def read_config(db) -> dict[str, Any]:
    """读一份类型化配置（热缓存读，60 秒 TTL；非法值钳回合法区间）。

    ``db`` 是 SQLAlchemy Session。返回 dict：
    ``enabled: bool, bytes: int, max_concurrent: int,
    dedup_seconds: int, timeout_seconds: float, chunk_bytes: int``。
    """
    from backend.integrations import store

    raw = store.get_values(db, CONFIG_KEYS, DEFAULTS)
    timeout = _parse_int(raw[KEY_TIMEOUT_SECONDS], KEY_TIMEOUT_SECONDS)
    return {
        "enabled": _parse_bool(raw[KEY_ENABLED], False),
        "bytes": _parse_int(raw[KEY_BYTES], KEY_BYTES),
        "max_concurrent": _parse_int(raw[KEY_MAX_CONCURRENT], KEY_MAX_CONCURRENT),
        "dedup_seconds": _parse_int(raw[KEY_DEDUP_SECONDS], KEY_DEDUP_SECONDS),
        "timeout_seconds": float(timeout),
        "chunk_bytes": _parse_int(raw[KEY_CHUNK_BYTES], KEY_CHUNK_BYTES),
    }


# ---------------------------------------------------------------------------
# 预热器
# ---------------------------------------------------------------------------

#: submit() 的返回口径（同时是指标口径的一部分）
RESULT_STARTED = "started"          # 已起线程
RESULT_DISABLED = "disabled"        # 总开关关
RESULT_DEDUP = "dedup"              # 去重窗口内命中，跳过
RESULT_BUSY = "busy"                # 并发已满，跳过（不排队）
RESULT_QUOTA = "quota"              # 文件处 Drive 配额负缓存期，跳过（不撞墙）


def _lower_thread_priority() -> None:
    """尽力降低**当前线程**的 CPU/IO 优先级（不抢播放链路）。

    - Linux 下 ``os.setpriority(PRIO_PROCESS, tid, nice)`` 传入线程 tid
      只影响调用线程，不动 API 主进程的其它线程。
    - I/O 调度切 idle 类：预热读盘时让位给真正的播放读。
    全部 best-effort：任一步失败都静默忽略，绝不让预热本身报错。
    """
    try:
        tid = threading.get_native_id()
        # nice +10：只影响本线程（Linux setpriority 按 tid 生效）
        os.setpriority(os.PRIO_PROCESS, tid, 10)
    except Exception:
        pass
    try:
        # ioprio_set(IOPRIO_WHO_THREAD=1, tid, IOPRIO_CLASS_IDLE(3) << 13)
        libc = ctypes.CDLL("libc.so.6", use_errno=True)
        libc.ioprio_set(1, threading.get_native_id(), 3 << 13)
    except Exception:
        pass


class PlaybackPrewarmer:
    """文件头预热器：有界并发 + 去重 + 超时/取消 + 指标。

    线程安全。生产用模块级单例（``get_prewarmer()``）；单元测试直接
    ``PlaybackPrewarmer()`` 造新实例，互不干扰。
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._active = 0                      # 正在跑的预热线程数
        self._last_prewarm: dict[str, float] = {}   # path -> 上次触发的 monotonic
        self._cancel_events: dict[str, threading.Event] = {}
        self._threads: list[tuple[threading.Thread, str]] = []  # (线程, 路径)
        self._counters: dict[str, int] = {
            "triggered": 0,        # submit 被调用次数
            "started": 0,          # 实际起线程次数
            "skipped_disabled": 0, # 总开关关
            "skipped_dedup": 0,    # 去重窗口命中
            "skipped_busy": 0,     # 并发已满（不排队，直接跳过）
            "skipped_quota": 0,    # 配额负缓存期跳过
            "success": 0,          # 读完目标字节数
            "timeout": 0,          # 超时放弃
            "failed": 0,            # 读异常（文件消失/权限等）
            "cancelled": 0,        # 中途被取消
        }
        self._elapsed_total = 0.0             # 成功/超时/失败任务的耗时累加（秒）
        self._elapsed_count = 0
        self._elapsed_max = 0.0
        self._bytes_total = 0                 # 成功预读的字节累加
        # 可替换的文件打开函数：单元测试注入慢速/故障文件对象；生产就是 open。
        self._open = open

    # -- 指标 ----------------------------------------------------------

    def _bump(self, key: str, delta: int = 1) -> None:
        with self._lock:
            self._counters[key] = self._counters.get(key, 0) + delta

    def metrics_snapshot(self) -> dict[str, Any]:
        """当前指标快照（给 /api/health 用；永不抛异常）"""
        try:
            with self._lock:
                count = self._elapsed_count
                avg_ms = (self._elapsed_total / count * 1000.0) if count else 0.0
                return {
                    "counters": dict(self._counters),
                    "active": self._active,
                    "avg_elapsed_ms": round(avg_ms, 2),
                    "max_elapsed_ms": round(self._elapsed_max * 1000.0, 2),
                    "total_bytes_read": self._bytes_total,
                }
        except Exception:
            return {"counters": {}, "active": 0, "avg_elapsed_ms": 0.0,
                    "max_elapsed_ms": 0.0, "total_bytes_read": 0}

    # -- 主入口 ----------------------------------------------------------

    def maybe_prewarm(self, db, path: Optional[str],
                      drive_file_id: Optional[str] = None) -> str:
        """读配置 → 尝试预热。返回 started/disabled/dedup/busy/quota 之一。

        本身不抛异常：任何意外都静默返回 disabled（调用方外层还有
        try/except，这里是第二道保险——预热绝不能影响 PlaybackInfo）。

        ``drive_file_id`` 给了且文件正处 Drive 单文件配额负缓存期时直接
        跳过：预热读的正是同一文件的字节，与真实播放争配额毫无意义。
        """
        try:
            if not path:
                return RESULT_DISABLED
            if drive_file_id:
                try:
                    from backend.emby_server.streaming import (
                        drive_quota_blocked_retry_after)
                    if drive_quota_blocked_retry_after(str(drive_file_id)) > 0:
                        self._bump("skipped_quota")
                        return RESULT_QUOTA
                except Exception:
                    pass
            cfg = read_config(db)
            return self.submit(path, cfg)
        except Exception as e:  # noqa: BLE001
            logger.debug("playback prewarm 跳过: %s", e)
            return RESULT_DISABLED

    def submit(self, path: str, cfg: dict[str, Any]) -> str:
        """不碰 DB 的版本：直接用给定的配置 dict 做去重/并发判断并起线程。

        单元测试走这里（可自由构造 cfg），生产走 ``maybe_prewarm``。
        """
        self._bump("triggered")
        if not cfg.get("enabled", True):
            self._bump("skipped_disabled")
            return RESULT_DISABLED
        # 预热 0 字节 = 实质关闭：不起线程，直接跳过
        if int(cfg.get("bytes", 0)) <= 0:
            self._bump("skipped_disabled")
            return RESULT_DISABLED

        now = time.monotonic()
        window = max(0, int(cfg.get("dedup_seconds", 0)))
        max_concurrent = max(1, int(cfg.get("max_concurrent", 4)))

        with self._lock:
            # 去重：窗口内同一文件只预热一次（只读检查，真正起线程时才落时间戳，
            # 避免 busy 跳过也污染去重窗口）
            if window > 0:
                last = self._last_prewarm.get(path)
                if last is not None and now - last < window:
                    self._counters["skipped_dedup"] += 1
                    return RESULT_DEDUP
            # 回收已结束的线程引用（有界，不泄漏；先回收，后面的存活判断才准）
            self._threads = [(t, pth) for t, pth in self._threads if t.is_alive()]
            # 有界：去重表与取消事件同生命周期，超限清掉过期条目（window=0 时
            # 去重表为空，只清已结束任务的取消事件），长期运行不泄漏
            if len(self._last_prewarm) + len(self._cancel_events) > 4096:
                cutoff = now - window
                for p in [p for p, ts in self._last_prewarm.items() if ts < cutoff]:
                    del self._last_prewarm[p]
                    self._cancel_events.pop(p, None)
                live = {p for _, p in self._threads}
                for p in [p for p in self._cancel_events
                          if p not in self._last_prewarm and p not in live]:
                    self._cancel_events.pop(p, None)
            # 并发上限：满了直接跳过，**不排队**（避免积压抢播放线程）
            if self._active >= max_concurrent:
                self._counters["skipped_busy"] += 1
                return RESULT_BUSY
            self._active += 1
            self._counters["started"] += 1
            if window > 0:
                self._last_prewarm[path] = now
            cancel = self._cancel_events.get(path)
            if cancel is None:
                cancel = threading.Event()
                self._cancel_events[path] = cancel
            else:
                cancel.clear()
            worker = threading.Thread(
                target=self._run,
                args=(path, cfg, cancel),
                name=f"pb-prewarm-{self._active}",
                daemon=True,
            )
            self._threads.append((worker, path))
            # 锁内起线程：避免 start 前被 shutdown()/prune 漏跟踪
            worker.start()

        return RESULT_STARTED

    def cancel(self, path: str) -> bool:
        """取消指定文件的在途预热（下一个分块边界生效）。返回是否找到任务。"""
        with self._lock:
            event = self._cancel_events.get(path)
        if event is None:
            return False
        event.set()
        return True

    def shutdown(self, wait: bool = True, timeout: float = 10.0) -> None:
        """取消全部在途预热并（可选）等待线程退出。测试/进程退出时用。"""
        with self._lock:
            events = list(self._cancel_events.values())
            threads = [t for t, _ in self._threads]
        for event in events:
            event.set()
        if wait:
            deadline = time.monotonic() + max(0.0, timeout)
            for thread in threads:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                thread.join(remaining)

    # -- 工作线程 --------------------------------------------------------

    def _run(self, path: str, cfg: dict[str, Any], cancel: threading.Event) -> None:
        _lower_thread_priority()
        total = max(0, int(cfg.get("bytes", 0)))
        chunk = max(4096, int(cfg.get("chunk_bytes", 262144)))
        deadline = time.monotonic() + max(1.0, float(cfg.get("timeout_seconds", 30)))
        started_at = time.monotonic()
        read_bytes = 0
        outcome = "success"
        try:
            with self._open(path, "rb") as fh:
                remaining = total
                while remaining > 0:
                    if cancel.is_set():
                        outcome = "cancelled"
                        break
                    if time.monotonic() >= deadline:
                        outcome = "timeout"
                        break
                    data = fh.read(min(chunk, remaining))
                    if not data:
                        break  # 文件比目标小：读完即成功
                    if cancel.is_set():
                        # 读返回后也查一次：单次 read 就读完目标时，
                        # 循环顶部来不及再检查
                        outcome = "cancelled"
                        break
                    read_bytes += len(data)
                    remaining -= len(data)
        except Exception as e:  # noqa: BLE001
            logger.debug("playback prewarm 读失败 %s: %s", path, e)
            outcome = "failed"
        finally:
            elapsed = time.monotonic() - started_at
            with self._lock:
                self._active -= 1
                self._counters[outcome] = self._counters.get(outcome, 0) + 1
                self._elapsed_total += elapsed
                self._elapsed_count += 1
                if elapsed > self._elapsed_max:
                    self._elapsed_max = elapsed
                if outcome == "success":
                    self._bytes_total += read_bytes

    # -- 测试辅助 --------------------------------------------------------

    def reset(self) -> None:
        """清空去重表与指标（单元测试隔离用）。"""
        with self._lock:
            self._last_prewarm.clear()
            for key in self._counters:
                self._counters[key] = 0
            self._elapsed_total = 0.0
            self._elapsed_count = 0
            self._elapsed_max = 0.0
            self._bytes_total = 0


# ---------------------------------------------------------------------------
# 模块级单例（生产入口）
# ---------------------------------------------------------------------------

_instance: Optional[PlaybackPrewarmer] = None
_instance_lock = threading.Lock()


def get_prewarmer() -> PlaybackPrewarmer:
    """进程内单例。"""
    global _instance
    if _instance is None:
        with _instance_lock:
            if _instance is None:
                _instance = PlaybackPrewarmer()
    return _instance


def maybe_prewarm(db, path: Optional[str],
                  drive_file_id: Optional[str] = None) -> str:
    """生产入口：读配置并尝试预热（api.py 的 PlaybackInfo 里调用）。"""
    return get_prewarmer().maybe_prewarm(db, path, drive_file_id)


def metrics_snapshot() -> dict[str, Any]:
    """生产入口：指标快照（/api/health 用）。"""
    return get_prewarmer().metrics_snapshot()
