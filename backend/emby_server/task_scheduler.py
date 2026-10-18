"""
统一后台任务调度器（v2.54.1）

合并扫描（scan）与刮削（enrich）为统一任务流。

背景
----
``scan_queue`` 和 ``enrich_worker`` 是两个独立子系统，各自有线程池，
在同一个 worker 进程里抢 CPU / DB 连接 / 网络。用户点了 11 个库的扫描，
任务在队列里显示"等待调度"，而 enrich 线程还在吭哧吭哧做刮削——扫描排队
但没人干活。

设计（v2.54.1 修订：降并发让路，替代 v2.54.0 的全让路）
--------------------------------------------------------
统一调度器**不碰**两个子系统的内部实现（扫描的挂载串行、刮削的原子抢单、
TMDB 令牌桶全部保留），只做一件事：**协调**。

- 优先级：扫描（用户触发、面板可见）> 刮削（后台、可等）
- 机制：enrich worker 每次抢单前向调度器申请"开工许可"
  - 无扫描等待 → 直接开工（全部线程）
  - 有扫描等待 → 降并发开工：只允许 ``ENRICH_PREEMPT_MIN_THREADS`` 个
    enrich 线程同时干活（默认 4），其余线程睡一小会儿再问
  - 这样既给扫描让出资源，又不让刮削积压完全停滞
- 熔断器：连续让路超过 ``ENRICH_PREEMPT_MAX_YIELD_MIN`` 分钟（默认 30），
  或扫描队列长度 ``ENRICH_PREEMPT_QUEUE_STALL_MIN`` 分钟（默认 10）无变化
  （扫描疑似卡死），自动恢复全部 enrich 线程并打日志告警
- 只统计"可派发"的扫描任务：被挂载串行挡住、当前跑不起来的任务不计入，
  避免优先级倒置（为跑不起来的扫描让路）
- 全部可配置，一键关闭回退到旧行为（两个子系统独立抢资源）

另外提供 ``get_unified_status()``：一次调用看到扫描队列 + 刮削积压，
给管理后台"后台任务"统一视图用。

配置（环境变量）
----------------
- ``TASK_SCHEDULER_ENABLED``：总开关，默认 1（开）
- ``SCAN_PREEMPT_ENRICH``：扫描优先抢占刮削，默认 1（开）
- ``ENRICH_PREEMPT_WAIT_SEC``：被抢占时 enrich 每次等待秒数，默认 5
- ``ENRICH_PREEMPT_MIN_THREADS``：有扫描等待时保留的最少 enrich 线程数，
  默认 4（至少 1，永不降到 0）
- ``ENRICH_PREEMPT_MAX_YIELD_MIN``：熔断器——连续让路超过 N 分钟自动恢复，
  默认 30
- ``ENRICH_PREEMPT_QUEUE_STALL_MIN``：熔断器——队列长度 N 分钟无变化自动恢复，
  默认 10
"""
from __future__ import annotations

import logging
import os
import random
import threading
import time

logger = logging.getLogger(__name__)

# ---- 可调参数（环境变量，全部可配置，不写死） ----
TASK_SCHEDULER_ENABLED = (
    os.getenv("TASK_SCHEDULER_ENABLED", "1") or "1"
).strip().lower() not in {"0", "false", "no", "off"}

SCAN_PREEMPT_ENRICH = (
    os.getenv("SCAN_PREEMPT_ENRICH", "1") or "1"
).strip().lower() not in {"0", "false", "no", "off"}

ENRICH_PREEMPT_WAIT_SEC = max(
    1, int(os.getenv("ENRICH_PREEMPT_WAIT_SEC", "5") or 5)
)

# v2.54.1：降并发让路——有扫描等待时保留的最少 enrich 线程数（至少 1）
ENRICH_PREEMPT_MIN_THREADS = max(
    1, int(os.getenv("ENRICH_PREEMPT_MIN_THREADS", "4") or 4)
)

# v2.54.1：熔断器阈值（分钟）
ENRICH_PREEMPT_MAX_YIELD_MIN = max(
    1, int(os.getenv("ENRICH_PREEMPT_MAX_YIELD_MIN", "30") or 30)
)
ENRICH_PREEMPT_QUEUE_STALL_MIN = max(
    1, int(os.getenv("ENRICH_PREEMPT_QUEUE_STALL_MIN", "10") or 10)
)

# ---- 降并发信号量：有扫描等待时，最多 N 个 enrich 线程同时干活 ----
_REDUCED_SEMAPHORE = threading.Semaphore(ENRICH_PREEMPT_MIN_THREADS)

# ---- 让路日志节流（线程安全，P2-1 修复：加锁） ----
_YIELD_LOG_LOCK = threading.Lock()
_last_yield_log_at: float = 0.0
_YIELD_LOG_THROTTLE_SEC = 60.0

# ---- 熔断器状态（_CIRCUIT_LOCK 保护，P1-2 修复） ----
_CIRCUIT_LOCK = threading.Lock()
_circuit_yielding_since: float | None = None  # 本轮"有扫描等待"连续开始时间
_circuit_last_queue_len: int = 0              # 上次观察到的队列长度
_circuit_queue_len_since: float | None = None  # 队列长度上次变化时间
_circuit_open: bool = False                   # 熔断器是否已触发


class _EnrichPermit:
    """enrich 开工许可。

    - ``limited=False``：正常开工，无需释放（无扫描等待 / 开关关闭 / 熔断器已开）
    - ``limited=True``：降并发名额，批次处理完后必须调用 ``release_enrich_permit``
    """

    __slots__ = ("limited",)

    def __init__(self, limited: bool):
        self.limited = limited


def dispatchable_scan_count() -> int:
    """当前"可派发"的扫描任务数（v2.54.1，P1-1 修复）

    只统计 ``_pick_locked`` 现在就能选中的任务——排除被挂载串行挡住、
    当前根本跑不起来的任务，避免优先级倒置（为跑不起来的扫描让路）。

    包含进程内队列 + Redis 队列（跨进程场景下 worker 本地队列可能为空，
    但 Redis 里还有任务等调度线程拉取；Redis 里的任务挂载状态未知，
    保守按"可派发"计入）。

    只读、不抛异常：任何异常都按 0 处理（不让路，宁可多做不误事）。
    """
    try:
        from backend.emby_server import scan_queue as _sq

        # 进程内：只数可派发的（排除等挂载的）
        try:
            n = _sq.waiting_dispatchable_count()
        except AttributeError:
            # 兼容旧版 scan_queue（没有新 API 时回退到全量计数）
            with _sq._LOCK:
                n = len(_sq._QUEUE)
        if n > 0:
            return n

        # Redis 队列（API 进程入队、worker 消费的场景）
        try:
            from backend.emby_server import scan_queue_redis as _rq

            r = _rq._redis()
            if r is not None:
                return int(r.llen(_rq.REDIS_SCAN_QUEUE_KEY) or 0)
        except Exception:
            pass
        return 0
    except Exception:
        return 0


def scan_waiting_count() -> int:
    """[兼容旧 API] 当前等待调度的扫描任务数（全量，含被挂载挡住的）

    v2.54.1 起 enrich 调度改用 :func:`dispatchable_scan_count`；
    本函数保留供外部调用方兼容。
    """
    try:
        from backend.emby_server import scan_queue as _sq

        with _sq._LOCK:
            local_waiting = len(_sq._QUEUE)
        if local_waiting:
            return local_waiting
        try:
            from backend.emby_server import scan_queue_redis as _rq

            r = _rq._redis()
            if r is not None:
                return int(r.llen(_rq.REDIS_SCAN_QUEUE_KEY) or 0)
        except Exception:
            pass
        return 0
    except Exception:
        return 0


def _update_circuit(n: int) -> None:
    """更新熔断器状态（调用方无需加锁，内部处理；日志在锁外打）"""
    global _circuit_yielding_since, _circuit_last_queue_len
    global _circuit_queue_len_since, _circuit_open
    now = time.monotonic()
    # 锁外日志用的快照
    log_reset = False
    log_trip_reason = None
    log_trip_detail = None
    with _CIRCUIT_LOCK:
        if n == 0:
            # 队列排空：复位熔断器
            if _circuit_open:
                log_reset = True
            _circuit_yielding_since = None
            _circuit_last_queue_len = 0
            _circuit_queue_len_since = None
            _circuit_open = False
        else:
            # 队列长度变化跟踪
            if n != _circuit_last_queue_len:
                _circuit_last_queue_len = n
                _circuit_queue_len_since = now

            # "有扫描等待"的连续计时
            if _circuit_yielding_since is None:
                _circuit_yielding_since = now

            if not _circuit_open:
                # 熔断条件 1：连续让路超过阈值
                max_yield_sec = ENRICH_PREEMPT_MAX_YIELD_MIN * 60
                if now - _circuit_yielding_since >= max_yield_sec:
                    _circuit_open = True
                    log_trip_reason = "max_yield"
                    log_trip_detail = (
                        now - _circuit_yielding_since,
                        ENRICH_PREEMPT_MAX_YIELD_MIN,
                        n,
                    )
                else:
                    # 熔断条件 2：队列长度长时间无变化（扫描疑似卡死）
                    stall_sec = ENRICH_PREEMPT_QUEUE_STALL_MIN * 60
                    if (
                        _circuit_queue_len_since is not None
                        and now - _circuit_queue_len_since >= stall_sec
                    ):
                        _circuit_open = True
                        log_trip_reason = "queue_stall"
                        log_trip_detail = (n, ENRICH_PREEMPT_QUEUE_STALL_MIN)

    # 锁外打日志，避免 I/O 持有锁
    if log_reset:
        logger.info("统一调度：扫描队列已排空，熔断器复位，恢复正常调度")
    if log_trip_reason == "max_yield":
        elapsed, threshold_min, qlen = log_trip_detail
        logger.warning(
            "统一调度：熔断器触发——连续 %.0f 秒有扫描等待（阈值 %d 分钟），"
            "自动恢复全部 enrich 线程。扫描队列长度：%d",
            elapsed,
            threshold_min,
            qlen,
        )
    elif log_trip_reason == "queue_stall":
        qlen, threshold_min = log_trip_detail
        logger.warning(
            "统一调度：熔断器触发——扫描队列长度 %d 已 %d 分钟无变化"
            "（疑似卡死），自动恢复全部 enrich 线程",
            qlen,
            threshold_min,
        )


def _circuit_is_open() -> bool:
    with _CIRCUIT_LOCK:
        return _circuit_open


def acquire_enrich_permit() -> "_EnrichPermit | None":
    """enrich 线程每次抢单前调用，申请开工许可（永不抛异常）

    返回值：
    - ``None`` → 本轮让路（调用方睡一小会儿后 ``continue``）
    - ``permit``（``limited=False``）→ 正常开工，无需释放
    - ``permit``（``limited=True``）→ 降并发名额开工，批次处理完后
      必须调用 :func:`release_enrich_permit` 归还名额

    决策逻辑：
    1. 调度器/抢占开关关闭 → 正常开工
    2. 无可派发扫描 → 正常开工（并复位熔断器）
    3. 熔断器已触发 → 正常开工（自动恢复）
    4. 有可派发扫描 → 抢降并发名额；抢到则开工（limited），
       抢不到则让路（返回 None）
    """
    try:
        return _acquire_inner()
    except Exception:
        logger.debug("统一调度：许可申请异常，fail-safe 继续刮削", exc_info=True)
        return _EnrichPermit(limited=False)


def _acquire_inner() -> "_EnrichPermit | None":
    if not TASK_SCHEDULER_ENABLED or not SCAN_PREEMPT_ENRICH:
        return _EnrichPermit(limited=False)

    n = dispatchable_scan_count()
    _update_circuit(n)

    if n == 0:
        return _EnrichPermit(limited=False)
    if _circuit_is_open():
        return _EnrichPermit(limited=False)

    # 降并发：非阻塞抢一个名额
    if _REDUCED_SEMAPHORE.acquire(blocking=False):
        return _EnrichPermit(limited=True)
    return None


def release_enrich_permit(permit: "_EnrichPermit | None") -> None:
    """归还降并发名额（永不抛异常）"""
    try:
        if permit is not None and permit.limited:
            _REDUCED_SEMAPHORE.release()
    except Exception:
        pass


def should_enrich_yield() -> bool:
    """[兼容旧 API] 本线程现在是否应该让路

    v2.54.1 起推荐使用 :func:`acquire_enrich_permit`（支持降并发）；
    本函数保留语义为"简化判断"：有可派发扫描等待且熔断器未开 → True。
    注意：实际是否让路以 acquire_enrich_permit 的名额竞争结果为准。
    """
    try:
        if not TASK_SCHEDULER_ENABLED or not SCAN_PREEMPT_ENRICH:
            return False
        n = dispatchable_scan_count()
        if n == 0 or _circuit_is_open():
            return False
        return True
    except Exception:
        return False


def preempt_wait_with_jitter() -> float:
    """被抢占时的等待秒数（v2.54.1，P2-2 修复）

    基础值 + 0~2 秒随机抖动，打散 32 个线程的 5 秒对齐惊群，
    避免同时抢 scan queue 锁和 DB。
    """
    return ENRICH_PREEMPT_WAIT_SEC + random.uniform(0, 2.0)


def note_enrich_yielded() -> None:
    """记录一次让路（v2.54.1，P2-1 修复：日志节流加锁，线程安全）"""
    global _last_yield_log_at
    now = time.monotonic()
    with _YIELD_LOG_LOCK:
        if now - _last_yield_log_at < _YIELD_LOG_THROTTLE_SEC:
            return
        _last_yield_log_at = now
    # 锁外读数、锁外打日志，避免 I/O 持有锁
    try:
        n = dispatchable_scan_count()
    except Exception:
        n = -1
    logger.info(
        "统一调度：有 %d 个可派发扫描任务等待，刮削降并发至 %d 线程"
        "（ENRICH_PREEMPT_MIN_THREADS=%d）",
        n,
        ENRICH_PREEMPT_MIN_THREADS,
        ENRICH_PREEMPT_MIN_THREADS,
    )


def get_unified_status(light: bool = False) -> dict:
    """统一后台任务状态（管理后台用）

    一次调用看到：
    - scan: 等待数 / 可派发数 / 运行数
    - enrich: pending / enriching（DB 实时计数）
    - preempt_active: 当前是否在降并发让路
    - circuit_open: 熔断器是否触发

    light=True 跳过 enrich 侧的 DB 聚合（供 tasks/overview 用，它不读 enrich 字段）。
    """
    # 扫描侧
    scan_waiting = 0
    scan_dispatchable = 0
    scan_running = 0
    try:
        from backend.emby_server import scan_queue as _sq

        with _sq._LOCK:
            scan_waiting = len(_sq._QUEUE)
            scan_running = len(_sq._RUNNING)
        try:
            scan_dispatchable = _sq.waiting_dispatchable_count()
        except AttributeError:
            scan_dispatchable = scan_waiting
        # Redis 里可能还有（跨进程场景）
        try:
            from backend.emby_server import scan_queue_redis as _rq

            r = _rq._redis()
            if r is not None:
                redis_len = int(r.llen(_rq.REDIS_SCAN_QUEUE_KEY) or 0)
                scan_waiting += redis_len
                scan_dispatchable += redis_len
        except Exception:
            pass
    except Exception:
        pass

    # 刮削侧（DB 实时计数，轻量 GROUP BY；light 模式跳过，调用方不读 enrich 字段）
    enrich_pending = 0
    enrich_running = 0
    if not light:
        try:
            from backend.database import SessionLocal
            from backend.emby_server import models as em
            from sqlalchemy import func as _func

            db = SessionLocal()
            try:
                rows = (
                    db.query(em.MediaItem.enrich_status, _func.count(em.MediaItem.id))
                    .filter(em.MediaItem.enrich_status.in_(("pending", "enriching")))
                    .group_by(em.MediaItem.enrich_status)
                    .all()
                )
                by_status = {s or "unknown": c for s, c in rows}
                enrich_pending = int(by_status.get("pending", 0))
                enrich_running = int(by_status.get("enriching", 0))
            finally:
                db.close()
        except Exception:
            pass

    preempt_active = bool(
        TASK_SCHEDULER_ENABLED
        and SCAN_PREEMPT_ENRICH
        and scan_dispatchable > 0
        and not _circuit_is_open()
    )

    return {
        "scheduler_enabled": bool(TASK_SCHEDULER_ENABLED),
        "scan_preempt_enrich": bool(SCAN_PREEMPT_ENRICH),
        "preempt_active": preempt_active,
        "circuit_open": _circuit_is_open(),
        "min_threads_when_preempted": ENRICH_PREEMPT_MIN_THREADS,
        "scan": {
            "waiting": scan_waiting,
            "dispatchable": scan_dispatchable,
            "running": scan_running,
        },
        "enrich": {
            "pending": enrich_pending,
            "enriching": enrich_running,
        },
    }
