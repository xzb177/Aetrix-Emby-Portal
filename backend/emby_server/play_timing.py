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

    def done(self) -> None:
        pass


_NOOP = _NoopTimer()


def new_timer(label: str) -> PlayTimer | _NoopTimer:
    """创建计时器。关闭时返回 no-op 单例，调用方无需判空。"""
    if not _ENABLED:
        return _NOOP
    return PlayTimer(label)
