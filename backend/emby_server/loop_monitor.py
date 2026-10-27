"""事件循环健康监控（P0，2026-09-29）。

背景：EA 是单 uvicorn worker。曾出现运行一段时间后事件循环退化、
登录从 0.4s 退化到 15.7s，重启才恢复。根因是 async 路由里直接调同步 DB
（已修），但为防漏网之鱼，加这个监控做哨兵。

原理：每秒在事件循环里醒一次，算「期望醒来时间 vs 实际醒来时间」的差值
（loop lag）。loop 被阻塞时差值会变大。超过阈值就记 warning，方便定位。
"""
from __future__ import annotations

import asyncio
import logging
import time

logger = logging.getLogger(__name__)

# loop lag 超过这个值（秒）就告警
LAG_WARN_SECONDS = 1.0
# 连续多少次超标才告警（避免偶发抖动刷屏）
LAG_WARN_CONSECUTIVE = 3

_monitor_task: asyncio.Task | None = None


def start_loop_monitor() -> None:
    """在 lifespan 里调用，启动监控任务（幂等）。

    恢复说明：本函数在 0d13b5f0 被连带删掉（该提交正文只说恢复 3 个脚本，
    未提及 loop_monitor），而 emby_api/main.py 的 lifespan 仍在调用它；
    调用处包在 try/except 里，于是 EA 每次启动只记一条“监控启动失败
    （可忽略）”，P0 哨兵实际从未跑起来。本恢复与删除前的实现逐字一致。
    """
    global _monitor_task
    if _monitor_task is not None and not _monitor_task.done():
        return
    _monitor_task = asyncio.create_task(_monitor_loop())
    logger.info("事件循环健康监控已启动（lag > %.1fs 告警）", LAG_WARN_SECONDS)


async def _monitor_loop() -> None:
    consecutive = 0
    while True:
        start = time.monotonic()
        await asyncio.sleep(1.0)
        lag = time.monotonic() - start - 1.0
        if lag >= LAG_WARN_SECONDS:
            consecutive += 1
            if consecutive >= LAG_WARN_CONSECUTIVE:
                logger.warning(
                    "事件循环疑似被阻塞：loop lag %.2fs（连续 %d 次超标），"
                    "检查是否有 async 路由直接调了同步阻塞操作",
                    lag, consecutive,
                )
                consecutive = 0  # 告警一次后重置，避免刷屏
        else:
            consecutive = 0


