# -*- coding: utf-8 -*-
"""r53：115 直链缓存与共享客户端的锁初始化竞态回归。

背景：`backend/emby_server/mounts.py` 里 `_pan115_url_cache_lock` 与
`_shared_clients_lock` 曾用懒初始化（`if _lock is None: _lock = Lock()`）。
两线程在进程启动后首次并发调用时可各建一把锁，互斥被架空——同文件的
`_LIST_CACHE_LOCK` 早就是模块级直接初始化的正确口径。

本文件覆盖：
1. 模块导入后两把锁已是就绪的 Lock 实例（非 None）——懒初始化模式已删除。
2. `_get/_set/_invalidate_115_url` 功能正确：读写、过期淘汰、上限驱逐。
3. 多线程并发读写缓存不丢不坏（互斥真实生效）。
4. `_shared_http_client` 同一 key 返回同一实例（锁保护下的单例语义仍在）。

全部离线，不碰 115 外网、不碰 DB。
"""
import os

os.environ.setdefault("DATABASE_TYPE", "sqlite")
os.environ.setdefault("REDIS_ENABLED", "false")

import threading
import time

import pytest

from backend.emby_server import mounts as m


@pytest.fixture(autouse=True)
def _no_proxy_env(monkeypatch):
    """沙箱有代理环境变量（httpx 建 Client 时解析报 Invalid port），
    与被测逻辑无关，测试内清掉。"""
    for k in ("http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY",
              "all_proxy", "ALL_PROXY", "no_proxy", "NO_PROXY"):
        monkeypatch.delenv(k, raising=False)


def _reset_cache():
    with m._pan115_url_cache_lock:
        m._pan115_url_cache.clear()


def test_locks_initialized_at_import():
    """两把锁在模块导入时已就绪，不再是 None（懒初始化已删）"""
    assert isinstance(m._pan115_url_cache_lock, type(threading.Lock())), \
        "_pan115_url_cache_lock 必须在导入时就是 Lock 实例"
    assert isinstance(m._shared_clients_lock, type(threading.Lock())), \
        "_shared_clients_lock 必须在导入时就是 Lock 实例"


def test_115_url_cache_roundtrip():
    _reset_cache()
    assert m._get_cached_115_url("pc1") is None
    m._set_cached_115_url("pc1", "https://example.com/a", ttl=60)
    assert m._get_cached_115_url("pc1") == "https://example.com/a"
    m._invalidate_115_url("pc1")
    assert m._get_cached_115_url("pc1") is None
    # 删不存在的 key 不抛
    m._invalidate_115_url("pc-missing")


def test_115_url_cache_expiry():
    _reset_cache()
    m._set_cached_115_url("pc-exp", "https://example.com/e", ttl=0.05)
    assert m._get_cached_115_url("pc-exp") == "https://example.com/e"
    time.sleep(0.1)
    # 过期后读不到，且过期条目被顺手清掉
    assert m._get_cached_115_url("pc-exp") is None
    with m._pan115_url_cache_lock:
        assert "pc-exp" not in m._pan115_url_cache


def test_115_url_cache_eviction_bounded():
    _reset_cache()
    for i in range(1050):
        m._set_cached_115_url(f"pc-{i}", f"https://example.com/{i}", ttl=3600)
    with m._pan115_url_cache_lock:
        size = len(m._pan115_url_cache)
    # 1000 上限触发驱逐：一次清掉最早过期的 500 条，规模回到 1000 以内
    assert size <= 1000, f"缓存无界增长: {size}"


def test_115_url_cache_concurrent_rw():
    """32 线程并发读写：条目不丢、值不错（互斥真实生效）"""
    _reset_cache()
    errors = []

    def writer(n):
        try:
            for i in range(50):
                m._set_cached_115_url(f"w{n}-{i}", f"url-{n}-{i}", ttl=60)
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    def reader(n):
        try:
            for i in range(50):
                m._get_cached_115_url(f"w{n}-{i}")
                m._invalidate_115_url(f"nope-{n}-{i}")
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = ([threading.Thread(target=writer, args=(n,)) for n in range(16)] +
               [threading.Thread(target=reader, args=(n,)) for n in range(16)])
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors, f"并发读写出错: {errors[:3]}"
    # writer 共写入 16*50=800 条（reader 只读/删不存在的 key），应全在
    with m._pan115_url_cache_lock:
        size = len(m._pan115_url_cache)
    assert size == 800, f"并发写入丢条目: {size} != 800"


def test_shared_http_client_singleton_per_key():
    """同一 key 并发取到同一客户端实例（锁保护的单例语义）"""
    results = []
    barrier = threading.Barrier(16)

    def worker():
        barrier.wait()
        results.append(m._shared_http_client("r53-test-key"))

    threads = [threading.Thread(target=worker) for _ in range(16)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len({id(c) for c in results}) == 1, "同 key 应返回同一客户端实例"
    # 不同 key 隔离
    other = m._shared_http_client("r53-other-key")
    assert other is not results[0]
