"""登录页海报拼贴缓存：monotonic 初值 / 异常兜底 / 负缓存 / single-flight 回归。"""
import os, sys
os.environ.setdefault("DATABASE_TYPE", "sqlite")
os.environ.setdefault("REDIS_ENABLED", "false")
sys.path.insert(0, ".")

import threading
import unittest
from unittest import mock

from backend.api import site


class FakeQuery:
    """桩掉 db.query(...).filter(...).order_by(...).limit(...) 链式调用。"""

    def __init__(self, rows):
        self._rows = rows
        # 记录 all() 被调用的次数，用于断言是否命中 DB
        self.all_calls = 0

    def filter(self, *args, **kwargs):
        return self

    def order_by(self, *args, **kwargs):
        return self

    def limit(self, *args, **kwargs):
        return self

    def all(self):
        self.all_calls += 1
        return list(self._rows)


class FakeDB:
    """最小化 DB 桩：query 返回同一个 FakeQuery，便于统计调用次数。"""

    def __init__(self, rows=None):
        self.query_obj = FakeQuery(rows or [])

    def query(self, *args, **kwargs):
        return self.query_obj


def _reset_cache():
    """每个测试前把全局缓存重置为从未刷新状态。"""
    site._login_posters_cache = {"at": None, "items": [], "emby_base_url": ""}


class LoginPostersCacheTest(unittest.TestCase):
    def setUp(self):
        # 重置缓存，避免测试间相互污染
        _reset_cache()
        # 兜底：确保锁在测试开始前是空闲的
        if site._login_posters_lock.locked():
            try:
                site._login_posters_lock.release()
            except RuntimeError:
                pass

    def tearDown(self):
        # 兜底释放锁，防止测试异常路径把锁占住影响后续用例
        if site._login_posters_lock.locked():
            try:
                site._login_posters_lock.release()
            except RuntimeError:
                pass
        _reset_cache()

    def test_never_refreshed_queries_db_even_when_monotonic_small(self):
        """at=None 表示从未刷新，即使 monotonic 很小也必须查 DB。"""
        db = FakeDB(rows=[])
        with mock.patch.object(site.time, "monotonic", return_value=100.0), \
                mock.patch.object(site.realms, "active_realm_id", return_value=None), \
                mock.patch.object(site, "resolve_emby_base_url", return_value=""):
            site.get_login_posters(db=db, request=None)

        # 关键断言：不能因为 monotonic 小就误判为"缓存新鲜"
        self.assertEqual(db.query_obj.all_calls, 1)

    def test_realm_resolve_failure_degrades_gracefully(self):
        """active_realm_id 抛异常时统一兜底，不向外抛异常。"""
        db = FakeDB(rows=[])
        with mock.patch.object(site.time, "monotonic", return_value=1000.0), \
                mock.patch.object(site.realms, "active_realm_id",
                                  side_effect=RuntimeError("realm boom")), \
                mock.patch.object(site, "resolve_emby_base_url", return_value=""):
            # 不应抛异常
            result = site.get_login_posters(db=db, request=None)

        self.assertEqual(result, {"items": [], "emby_base_url": ""})

    def test_failure_negative_caches_for_30s(self):
        """失败后 30 秒内走负缓存，不再打 DB；超过 30 秒后允许重试。"""
        db = FakeDB(rows=[])
        # 用一个可变容器模拟单调时钟推进
        clock = {"now": 5000.0}

        with mock.patch.object(site.time, "monotonic",
                               side_effect=lambda: clock["now"]), \
                mock.patch.object(site.realms, "active_realm_id",
                                  side_effect=RuntimeError("realm boom")), \
                mock.patch.object(site, "resolve_emby_base_url", return_value=""):
            # 第一次调用：失败并写入负缓存
            site.get_login_posters(db=db, request=None)
            self.assertEqual(db.query_obj.all_calls, 1)

            # 推进 10 秒：仍在负缓存窗口内，不应再查 DB
            clock["now"] += 10
            site.get_login_posters(db=db, request=None)
            self.assertEqual(db.query_obj.all_calls, 1)

            # 推进到 31 秒：负缓存过期，允许第二次查 DB
            clock["now"] += 21
            site.get_login_posters(db=db, request=None)
            self.assertEqual(db.query_obj.all_calls, 2)

    def test_single_flight_returns_stale_cache_without_db_hit(self):
        """拿不到 single-flight 锁时直接返回旧缓存，且不打 DB。"""
        stale_items = [{"id": 1, "name": "poster-1"}]
        # 预置一份已过期的旧缓存（at = now - 700 > TTL 600）
        site._login_posters_cache = {
            "at": 1000.0 - 700,
            "items": stale_items,
            "emby_base_url": "http://emby.local",
        }
        db = FakeDB(rows=[{"id": 2, "name": "poster-2"}])

        # 主线程先占住锁，模拟另一个请求正在刷新
        site._login_posters_lock.acquire()
        try:
            with mock.patch.object(site.time, "monotonic", return_value=1000.0), \
                    mock.patch.object(site.realms, "active_realm_id",
                                      return_value=None), \
                    mock.patch.object(site, "resolve_emby_base_url",
                                      return_value="http://emby.local"):
                result = site.get_login_posters(db=db, request=None)
        finally:
            # 必须释放锁，避免污染后续测试
            site._login_posters_lock.release()

        # 未命中 DB，直接返回旧缓存
        self.assertEqual(db.query_obj.all_calls, 0)
        self.assertEqual(result["items"], stale_items)


if __name__ == "__main__":
    unittest.main()