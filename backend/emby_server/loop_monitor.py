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


