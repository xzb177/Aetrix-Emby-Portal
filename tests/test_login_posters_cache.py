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
    site._login_posters_cache = {"entries": {}}


class LoginPostersCacheTest(unittest.TestCase):
    def setUp(self):
        # 重置缓存，避免测试间相互污染。清掉公开地址，保证 request=None 的缓存键是空串。
        self._saved_env = {}
        for key in ("EMBY_PUBLIC_URL", "FORWARDED_ALLOW_IPS", "TRUSTED_PROXIES"):
            self._saved_env[key] = os.environ.get(key)
            os.environ.pop(key, None)
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
        for key, value in self._saved_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

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
            "entries": {
                "": {
                    "at": 1000.0 - 700,
                    "items": stale_items,
                    "emby_base_url": "http://emby.local",
                }
            }
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
        self.assertEqual(result["emby_base_url"], "http://emby.local")

    def test_cache_keyed_by_client_base_does_not_share_host(self):
        """不同请求 Host 不能共用同一条 emby_base_url。"""
        keys = ("EMBY_PUBLIC_URL", "FORWARDED_ALLOW_IPS", "TRUSTED_PROXIES")
        saved = {key: os.environ.get(key) for key in keys}
        for key in keys:
            os.environ.pop(key, None)
        try:
            row = type("Row", (), {"guid": "a" * 32, "name": "海报"})()
            db = FakeDB(rows=[row])

            def resolve(_db, _realm, request):
                return f"http://{request.headers['host']}"

            with mock.patch.object(site.time, "monotonic", return_value=1000.0), \
                    mock.patch.object(site.realms, "active_realm_id", return_value=None), \
                    mock.patch.object(site, "resolve_emby_base_url", side_effect=resolve):
                first = site.get_login_posters(db=db, request=_http_request("192.0.2.10"))
                second = site.get_login_posters(db=db, request=_http_request("192.0.2.20"))
                again = site.get_login_posters(db=db, request=_http_request("192.0.2.10"))
        finally:
            for key, value in saved.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value

        self.assertEqual(first["emby_base_url"], "http://192.0.2.10")
        self.assertEqual(second["emby_base_url"], "http://192.0.2.20")
        self.assertEqual(again["emby_base_url"], "http://192.0.2.10")
        self.assertEqual(first["items"], [{"id": "a" * 32, "name": "海报"}])
        # 两个基址各查一次库；回到第一个基址命中自己的桶
        self.assertEqual(db.query_obj.all_calls, 2)

    def test_configured_public_url_shares_one_poster_bucket(self):
        """EMBY_PUBLIC_URL 已设时，不同 Host 共用一个桶，不能靠 Host 把缓存撑满。"""
        os.environ["EMBY_PUBLIC_URL"] = "https://media.example.com"
        row = type("Row", (), {"guid": "b" * 32, "name": "海报"})()
        db = FakeDB(rows=[row])
        hosts = [f"192.0.2.{i}" for i in range(1, 21)]

        with mock.patch.object(site.time, "monotonic", return_value=1000.0), \
                mock.patch.object(site.realms, "active_realm_id", return_value=None), \
                mock.patch.object(site, "resolve_emby_base_url",
                                  return_value="https://media.example.com"):
            results = [
                site.get_login_posters(db=db, request=_http_request(host))
                for host in hosts
            ]

        self.assertEqual(db.query_obj.all_calls, 1)
        self.assertEqual(len(site._poster_entries()), 1)
        self.assertTrue(all(
            item["emby_base_url"] == "https://media.example.com" for item in results
        ))
        self.assertEqual(results[0]["items"], [{"id": "b" * 32, "name": "海报"}])

    def test_inferred_poster_buckets_are_per_base_and_bounded(self):
        """没配公开地址时按推断基址分桶，桶数不超过上限，最旧的会被挤掉。"""
        row = type("Row", (), {"guid": "c" * 32, "name": "海报"})()
        db = FakeDB(rows=[row])
        extra = 8
        hosts = [f"192.0.2.{i}" for i in range(1, site._LOGIN_POSTERS_MAX_KEYS + extra + 1)]
        clock = {"now": 8000.0}

        def tick():
            clock["now"] += 1.0
            return clock["now"]

        def resolve(_db, _realm, request):
            return f"http://{request.headers['host']}"

        with mock.patch.object(site.time, "monotonic", side_effect=tick), \
                mock.patch.object(site.realms, "active_realm_id", return_value=None), \
                mock.patch.object(site, "resolve_emby_base_url", side_effect=resolve):
            results = [
                site.get_login_posters(db=db, request=_http_request(host))
                for host in hosts
            ]

        entries = site._poster_entries()
        self.assertEqual(results[0]["emby_base_url"], "http://192.0.2.1")
        self.assertEqual(results[1]["emby_base_url"], "http://192.0.2.2")
        self.assertEqual(db.query_obj.all_calls, len(hosts))
        self.assertEqual(len(entries), site._LOGIN_POSTERS_MAX_KEYS)
        self.assertNotIn("http://192.0.2.1", entries)
        self.assertIn(f"http://192.0.2.{len(hosts)}", entries)
        self.assertEqual(
            entries[f"http://192.0.2.{len(hosts)}"]["emby_base_url"],
            f"http://192.0.2.{len(hosts)}",
        )


def _http_request(host: str):
    from starlette.requests import Request

    scope = {
        "type": "http",
        "method": "GET",
        "path": "/api/site/login-posters",
        "raw_path": b"/api/site/login-posters",
        "query_string": b"",
        "headers": [(b"host", host.encode())],
        "scheme": "http",
        "server": ("127.0.0.1", 80),
        "client": ("203.0.113.9", 50000),
        "root_path": "",
    }
    return Request(scope)


if __name__ == "__main__":
    unittest.main()