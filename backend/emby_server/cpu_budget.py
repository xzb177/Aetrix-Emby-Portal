# -*- coding: utf-8 -*-
"""CPU 预算：后台任务并发按核数自适应，永远给前台预留一核。

借鉴 Linger 的设计：
- 未配置时：后台并发 = max(1, 可用核数 // 2)
- 手动配置时：min(配置值, 可用核数 - 1)，至少 1
- 核心原则：**前台（API/播放）永远有至少一核可用**，后台任务不能饿死前台。

可用核数取 min(系统核数, 容器配额, CPU 亲和性)，三者都考虑。
"""
from __future__ import annotations

import logging
import os
from functools import lru_cache

logger = logging.getLogger(__name__)


def _read_cgroup_quota() -> int | None:
    """读容器 CPU 配额（cgroup v1/v2），返回核数，读不到返回 None"""
    # cgroup v2
    try:
        with open("/sys/fs/cgroup/cpu.max") as f:
            parts = f.read().strip().split()
            if len(parts) == 2 and parts[0] != "max":
                quota, period = int(parts[0]), int(parts[1])
                if quota > 0 and period > 0:
                    return max(1, quota // period)
    except (OSError, ValueError):
        pass
    # cgroup v1
    try:
        with open("/sys/fs/cgroup/cpu/cpu.cfs_quota_us") as f:
            quota = int(f.read().strip())
        with open("/sys/fs/cgroup/cpu/cpu.cfs_period_us") as f:
            period = int(f.read().strip())
        if quota > 0 and period > 0:
            return max(1, quota // period)
    except (OSError, ValueError):
        pass
    return None


@lru_cache(maxsize=1)
def cpu_count() -> int:
    """本进程实际可用的 CPU 核数（考虑容器配额与亲和性）。

    结果缓存：容器 CPU 配额启动后不变，避免每次调用都读 cgroup 文件。
    """
    candidates = []
    # 1. CPU 亲和性（最准：进程实际能跑在哪几个核上）
    try:
        candidates.append(len(os.sched_affinity(0)))
    except (OSError, AttributeError):
        pass
    # 2. cgroup 配额（容器 --cpus 限制）
    quota = _read_cgroup_quota()
    if quota:
        candidates.append(quota)
    # 3. 系统核数
    try:
        n = os.cpu_count() or 0
        if n > 0:
            candidates.append(n)
    except Exception:
        pass
    if not candidates:
        return 2
    return max(1, min(candidates))


def background_workers(env_name: str, *, default_divisor: int = 2,
                       max_cap: int = 16, io_bound: bool = False) -> int:
    """后台任务并发数。

    - 环境变量未设：max(1, cpu_count() // default_divisor)
    - 环境变量已设：min(设置值, cpu_count() - 1)，下限 1
      （永远给前台留一核；单核机器上后台与前台分时，不饿死）
    - 上限 max_cap，防止配了离谱的值
    - io_bound=True：IO 密集型后台任务（线程 90% 时间等网络，几乎不烧 CPU）：
      不按 CPU 截断——未配置默认 8，手动配置按 max_cap 截断。
      后台不抢主链路靠 max_cap + 各子系统的独立限流器（TMDB 令牌桶、图片下载
      信号量、DB 连接池）保证，而不是靠线程数。
    """
    if io_bound:
        # IO 密集型：线程主要在等网络，并发 ≠ CPU 占用。采样实证：enrich 单条
        # 90% 时间等网络，8 线程 ≈ 8 倍吞吐，CPU 占用几乎不变（2026-10-13）。
        raw = (os.getenv(env_name) or "").strip()
        if raw:
            try:
                v = int(raw)
            except ValueError:
                v = 0
            if v > 0:
                return max(1, min(v, max_cap))
            # 非法值：按未配置处理
        return max(1, min(8, max_cap))
    cpus = cpu_count()
    reserve = max(1, cpus - 1)  # 给前台留的
    raw = (os.getenv(env_name) or "").strip()
    if raw:
        try:
            v = int(raw)
        except ValueError:
            v = 0
        if v > 0:
            return max(1, min(v, reserve, max_cap))
        # 非法值：按未配置处理
    default = max(1, cpus // default_divisor)
    return max(1, min(default, reserve, max_cap))


def capped_workers(env_name: str, default: int, *, max_cap: int = 16) -> int:
    """保留自定义默认值的版本：未配置/非法值时用 default，手动配置时按 CPU 截断。

    适用于默认有特殊考量（如缩略图防 I/O 争抢默认 1）的场景。
    0/负数按 1 处理（与旧的 max(1, n) 行为一致）。
    """
    cpus = cpu_count()
    reserve = max(1, cpus - 1)
    raw = (os.getenv(env_name) or "").strip()
    if raw:
        try:
            v = int(raw)
        except ValueError:
            return default
        # 0/负数 → 1（旧行为）；正数按 CPU 截断
        return max(1, min(v if v > 0 else 1, reserve, max_cap))
    return default
