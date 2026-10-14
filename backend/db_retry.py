"""写路径兜底：SQLite 写锁（``database is locked``）的指数退避重试

**这不是解法，是兜底。** SQLite 只有一个写者，真正让写锁被占满 30 秒的是「事务范围」——
在一次写事务里等网络（rclone 列目录、TMDB 刮削、ffprobe）或者删磁盘文件，
其他写请求就会一路等到 ``busy_timeout``（见 ``backend/database.py``，30 秒）耗尽后报错。
根治办法是把 I/O 移出事务（见 ``docs/performance.md`` 的「事务范围」一节），
本模块只负责另一半：**即便事务范围已经很干净，仍然会有别人短暂占着写锁**——

* 另一台 EA / 另一个进程也在写这同一个库；
* 运维手工 ``sqlite3``、备份工具、``VACUUM`` / WAL checkpoint；
* checkpoint 恰好和这次提交撞在一起。

这些情况下 ``busy_timeout`` 一旦等满，SQLAlchemy 直接抛
``OperationalError: database is locked``。用户看到的是「点一下没反应 / 前端 30 秒超时」，
而实际上只要退避几十毫秒再试一次就能成功。所以写路径统一走这里：

* 退避是**指数**的（默认 0.25s → 0.5s → 1s），最多试 ``attempts`` 次（默认 3 次）；
* 只对「写锁」这一类错误重试，其它 ``OperationalError``（语法错误、磁盘满、库损坏）原样抛出；
* 重试到底仍失败才抛给上层——**宁可这一次扫描 / 退款失败，也不能把错误吞掉**。

重试的语义：这里重试的是一次**提交**。SQLite 的 ``COMMIT`` 撞锁失败时事务还在、改动没丢，
所以退避后直接重提是安全的；而 ``flush`` 阶段的失败会把会话置成「需要回滚」，
那类失败不在这里重试（调用方回滚后用同一批数据重跑整段写，见 ``scanner._iter_prepared``）。
"""
from __future__ import annotations

import logging
import time
from typing import Any, Callable, TypeVar

from sqlalchemy.exc import OperationalError

import random

logger = logging.getLogger(__name__)

T = TypeVar("T")

# 写锁错误的判定关键字：SQLite 的原文是 "database is locked"，
# 表级锁（同一连接里跨表写）是 "database table is locked"，模式锁是 "database schema is locked"。
_LOCK_MARKERS = ("database is locked", "database table is locked", "database schema is locked")


def is_deadlock(exc: BaseException) -> bool:
    """这个异常是不是 PG 死锁（``DeadlockDetected`` / 40P01）

    PG 检测到事务间循环等待会 abort 其中一个并抛死锁，SQLAlchemy 把它包成
    ``OperationalError``，``orig`` 是 ``psycopg2.errors.DeadlockDetected``。
    不硬依赖 psycopg2：类名 + 消息双重判定，SQLite 上永远 False。
    """
    raw = getattr(exc, "orig", None) or exc
    if raw.__class__.__name__ == "DeadlockDetected":
        return True
    return "deadlock detected" in str(raw).lower()


def deadlock_backoff(attempt: int) -> None:
    """死锁重试前的抖动退避

    死锁是两个事务互相等对方手里的行锁：立刻重试大概率撞上对方还没提交的
    同一批行。随机抖动（0.1~0.5s × 次数）让两边错开，比固定间隔更不容易
    再次互锁。
    """
    time.sleep(random.uniform(0.1, 0.5) * attempt)


def is_write_lock(exc: BaseException) -> bool:
    """这个异常是不是「写锁被占」（而不是别的数据库错误）"""
    raw = getattr(exc, "orig", None) or exc
    text = str(raw).lower()
    return any(marker in text for marker in _LOCK_MARKERS)


def retry_write(work: Callable[[], T], *, attempts: int = 3,
                base_delay: float = 0.25, label: str = "") -> T:
    """跑一次写库调用（通常是 ``db.commit``），撞写锁时指数退避重试

    ``attempts=3`` 表示「最多试 3 次」（含第一次），退避 0.25 → 0.5 秒：
    三轮之后仍然锁着，说明锁是真的被别人长期占着，这时候抛出去比继续等更诚实
    （上层能记日志、能报错，而不是假装成功）。
    """
    delay = base_delay
    for attempt in range(1, attempts + 1):
        try:
            return work()
        except OperationalError as exc:
            if not is_write_lock(exc) or attempt >= attempts:
                raise
            logger.warning(
                "写库撞上 SQLite 写锁（%s），%.2fs 后重试（第 %d/%d 次）",
                label or "写操作", delay, attempt, attempts,
            )
            time.sleep(delay)
            delay *= 2
    raise AssertionError("unreachable")  # pragma: no cover — 循环里必然 return 或 raise


def commit_with_retry(db: Any, *, attempts: int = 3, base_delay: float = 0.25,
                      label: str = "") -> None:
    """``db.commit()`` 的退避重试版本（写路径统一用它收尾）"""
    retry_write(db.commit, attempts=attempts, base_delay=base_delay, label=label)


__all__ = ["commit_with_retry", "deadlock_backoff", "is_deadlock", "is_write_lock", "retry_write"]
