"""perfection-long-D（配置/可观测性）：两项实锤修复的回归测试。

1. loop_monitor.start_loop_monitor 恢复：0d13b5f0 连带删掉了它，
   emby_api/main.py 的 lifespan 仍在调用，调用处包在 try/except 里，
   于是 EA 每次启动只记一条“监控启动失败（可忽略）”，P0 哨兵实际
   从未跑起来。本测试钉住：函数存在、可导入、幂等（两次调用只建
   一个任务）。
2. env.example 漂移盲区：r44 扫描器只认 _cpu_workers 别名，漏掉了经
   cpu_budget.capped_workers（别名 _capped_workers）读取的键；
   SCAN_WALK_WORKERS / THUMBNAIL_WORKERS 曾长期在代码里被读、
   在 env.example 里零收录。本测试钉住：经两个别名读取的全部字面
   键名都必须在 env.example 里出现。
   写法说明：这两键在 env.example 里目前是散文注释行、不是标准
   “键=值”行——因为 r44 扫描器双向都漏 _capped_workers：写成键=值行
   会使 r44 test_documented_keys_live 反向误报（已本地实测 1 failed）。
   把 r44 扫描器补上 _capped_workers 模式属于 r44 测试文件（非 D 批
   范围），本批不碰；补模式后应把这两行改回标准键=值行，届时本测试
   （子串口径）仍通过，r44 三项亦全绿。
"""
from __future__ import annotations

import asyncio
import pathlib
import re

REPO = pathlib.Path(__file__).resolve().parent.parent
ENV_EXAMPLE = REPO / "env.example"


def test_cpu_budget_alias_keys_documented():
    """经 cpu_budget 两个别名读取的 env 键必须全部在 env.example 出现。"""
    pat = re.compile(r"\b_c(?:pu_workers|apped_workers)\(\s*\"([A-Z][A-Z0-9_]*)\"")
    found: set[str] = set()
    for path in (REPO / "backend").rglob("*.py"):
        if "test" in path.parts:
            continue
        found.update(pat.findall(path.read_text(encoding="utf-8", errors="ignore")))
    assert found, "后端未扫到任何 cpu_budget 别名读取的 env 键，扫描模式可能已失效"
    # 本批实锤的两键必须在内（防测试本身被悄悄放水）
    assert {"SCAN_WALK_WORKERS", "THUMBNAIL_WORKERS"} <= found
    text = ENV_EXAMPLE.read_text(encoding="utf-8")
    missing = sorted(k for k in found if k not in text)
    assert not missing, f"以下 cpu_budget 键在代码里被读但 env.example 未收录：{missing}"


def test_start_loop_monitor_exists_and_idempotent():
    """start_loop_monitor 可导入、幂等、只建一个任务。"""
    from backend.emby_server import loop_monitor

    assert callable(getattr(loop_monitor, "start_loop_monitor", None))

    async def _check():
        try:
            loop_monitor.start_loop_monitor()
            first = loop_monitor._monitor_task
            assert first is not None and not first.done()
            loop_monitor.start_loop_monitor()
            assert loop_monitor._monitor_task is first, "二次调用建了新任务，不幂等"
        finally:
            task = loop_monitor._monitor_task
            loop_monitor._monitor_task = None
            if task is not None and not task.done():
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass

    asyncio.run(_check())
