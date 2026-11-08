"""播放链路耗时日志（``[play-timing]``）。

用途：定位「点播放 → 出画面」慢在哪。下次用户复现时 grep 日志即可分解，
不用靠猜。

开关：环境变量 ``PLAY_TIMING_LOG=0`` 关闭（默认开启）。import 时读取一次，
热路径上零额外开销。

性能：``time.perf_counter()`` 约 20ns；字符串拼接只在 ``done()`` 且开启时
发生；关闭时 ``new_timer()`` 直接返回单例 no-op，连对象都不创建。
"""
from __future__ import annotations

import logging
import os
import time

logger = logging.getLogger(__name__)

# ---- 日志自给自足（2026-10-12）----
# EA（emby_api/main.py）没有 logging.basicConfig，uvicorn 默认也不配置 root logger，
# 导致本 logger 的有效级别为 WARNING，logger.info() 全被吞掉——生产 EA 一条
# [play-timing] 都没有，而 EM（backend/main.py）有 basicConfig 则正常。
# 在此兜底：级别显式设为 INFO；若 root 也没有 handler，自己加一个 StreamHandler
#（走传播即可时不重复加，避免 EM 侧日志重复）。
if logger.level == logging.NOTSET:
    logger.setLevel(logging.INFO)
if not logger.handlers and not logging.getLogger().handlers:
    _h = logging.StreamHandler()
    # done() 里已拼好 "[play-timing] ..." 前缀，这里透传；格式向 uvicorn 看齐。
    _h.setFormatter(logging.Formatter("%(levelname)s:     %(message)s"))
    logger.addHandler(_h)
    logger.propagate = False

# import 时读一次：避免每次 mark 都查环境变量
_ENABLED = os.getenv("PLAY_TIMING_LOG", "1") not in ("0", "false", "False", "")


def is_enabled() -> bool:
    """耗时日志是否开启（供测试/外部查询）。"""
    return _ENABLED


class PlayTimer:
    """轻量计时器：记录命名检查点，最后统一输出一行汇总。"""

    __slots__ = ("label", "_start", "_marks")

    def __init__(self, label: str):
        self.label = label
        self._start = time.perf_counter()
        self._marks: list[tuple[str, float]] = []

    def mark(self, name: str) -> None:
        """记录一个检查点（毫秒精度）。"""
        self._marks.append((name, (time.perf_counter() - self._start) * 1000))

    def elapsed_ms(self) -> int:
        """自计时器创建起已耗的毫秒数（起播事件 total_ms 的取数口）。"""
        return int((time.perf_counter() - self._start) * 1000)

    def done(self) -> None:
        """输出汇总日志：``[play-timing] <label> <阶段>=<毫秒>ms ... total=<毫秒>ms``。"""
        total = (time.perf_counter() - self._start) * 1000
        parts = [f"{name}={ms:.0f}ms" for name, ms in self._marks]
        parts.append(f"total={total:.0f}ms")
        # lazy 格式化：logger 的 %s 风格，关闭级别时不拼接字符串
        logger.info("[play-timing] %s %s", self.label, " ".join(parts))


class _NoopTimer:
    """关闭时的零开销替身：所有调用都是空操作。"""

    __slots__ = ()

    def mark(self, name: str) -> None:  # noqa: ARG002
        pass

    def elapsed_ms(self) -> None:
        """关闭时无计时：返回 None（调用方据此把 total_ms 留空）。"""
        return None

    def done(self) -> None:
        pass


_NOOP = _NoopTimer()


def elapsed_ms(timer) -> int | None:
    """安全取计时器的已耗毫秒：None / 无 elapsed_ms 的桩替身一律 None。

    供跨函数传递计时器的记账点用（如额度后路的 pt 参数，测试桩可能
    只实现 mark）：取不到就留空，绝不因打点取数打断播放。
    """
    fn = getattr(timer, "elapsed_ms", None)
    if not callable(fn):
        return None
    try:
        value = fn()
    except Exception:  # noqa: BLE001 — 打点取数永不抛
        return None
    return None if value is None else int(value)


def new_timer(label: str) -> PlayTimer | _NoopTimer:
    """创建计时器。关闭时返回 no-op 单例，调用方无需判空。"""
    if not _ENABLED:
        return _NOOP
    return PlayTimer(label)
