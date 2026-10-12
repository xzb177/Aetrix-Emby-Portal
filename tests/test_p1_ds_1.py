"""P1 修复回归测试（DeepSeek 生成）：连接池常量、CacheManager 并发、PlaybackSession 索引。"""
from __future__ import annotations

import os
import threading

os.environ.setdefault("DATABASE_TYPE", "sqlite")

import pytest


def _import_database():
    try:
        from backend import database
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"backend.database 导入失败，跳过: {exc!r}")
    return database


def test_p1_ds_1_pool_size_and_overflow():
    """E-P1-1: 连接池 pool_size / max_overflow 默认值应为 10 / 10。"""
    database = _import_database()
    assert database._POOL_SIZE == 10, f"_POOL_SIZE 期望 10，实际 {database._POOL_SIZE!r}"
    assert database._MAX_OVERFLOW == 10, f"_MAX_OVERFLOW 期望 10，实际 {database._MAX_OVERFLOW!r}"


def test_p1_ds_1_cache_manager_concurrent_write():
    """E-P1-2: 多线程并发写不抛异常，且 _memory_cache_order 有界。"""
    database = _import_database()
    cm = database.CacheManager
    # 测试前清理
    with cm._memory_cache_lock:
        cm._memory_cache.clear()
        cm._memory_cache_order.clear()

    errors: list = []
    barrier = threading.Barrier(8)

    def worker(start: int, end: int) -> None:
        try:
            barrier.wait(timeout=10)
            for i in range(start, end):
                cm._memory_set(f"key:{i}", str(i))
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = []
    for t in range(8):
        s, e = t * 250, (t + 1) * 250
        th = threading.Thread(target=worker, args=(s, e))
        th.start()
        threads.append(th)
    for th in threads:
        th.join(timeout=30)

    assert all(not th.is_alive() for th in threads), "并发写入线程未在超时时间内结束"
    assert not errors, f"并发写入抛出异常: {errors[:3]!r}"
    # order 有界：最多 MAX 条目 + 少量并发余量
    assert len(cm._memory_cache_order) <= cm._MEMORY_CACHE_MAX + 50, (
        f"_memory_cache_order 长度 {len(cm._memory_cache_order)} 超出上限"
    )
    # dict 与 order 一致性：order 中的 key 都应在 cache 中（允许少量过期）
    with cm._memory_cache_lock:
        for k in cm._memory_cache_order:
            assert k in cm._memory_cache, f"order 中的 key {k} 不在 cache 中"


def test_p1_ds_1_cache_expiry_cleans_order():
    """E-P1-2: 过期 key 被清理时 order 同步缩减。"""
    import time

    database = _import_database()
    cm = database.CacheManager
    with cm._memory_cache_lock:
        cm._memory_cache.clear()
        cm._memory_cache_order.clear()

    cm._memory_set("__expire_me__", "v", ttl=1)
    assert "__expire_me__" in cm._memory_cache_order
    time.sleep(1.1)
    assert cm._memory_get("__expire_me__") is None
    assert "__expire_me__" not in cm._memory_cache_order, "过期 key 未从 order 中清理"


def test_p1_ds_1_playback_session_index():
    """E-P1-5: PlaybackSession 应声明 (ended_at, last_update_at) 复合索引。"""
    from sqlalchemy import Index

    try:
        from backend.emby_server.models import PlaybackSession
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"backend.emby_server.models 导入失败，跳过: {exc!r}")

    table_args = getattr(PlaybackSession, "__table_args__", ())
    indexes = [a for a in table_args if isinstance(a, Index)]
    target = next(
        (i for i in indexes if i.name == "ix_emby_playback_sessions_ended_last_update"),
        None,
    )
    assert target is not None, f"未找到目标索引，现有: {[i.name for i in indexes]}"
    cols = [c.name for c in target.columns]
    assert cols == ["ended_at", "last_update_at"], f"索引列不匹配: {cols}"
