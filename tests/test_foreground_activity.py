"""前台活跃打点：后台重活（缩略图补生成）在用户活跃时让路。"""
from __future__ import annotations

import os
import threading
import time

os.environ.setdefault("DATABASE_TYPE", "sqlite")

from backend.emby_server import foreground_activity as fa  # noqa: E402


class TestForegroundActivity:
    def setup_method(self):
        fa.reset_for_tests()

    def teardown_method(self):
        fa.reset_for_tests()

    def test_never_noted_is_inactive(self):
        assert fa.active(60) is False
        assert fa.last_seen_ago() is None

    def test_note_then_active(self):
        fa.note()
        assert fa.active(60) is True
        ago = fa.last_seen_ago()
        assert ago is not None and 0 <= ago < 5

    def test_window_expiry(self):
        fa.note()
        assert fa.active(0.05) is True
        time.sleep(0.08)
        assert fa.active(0.05) is False

    def test_thread_safe(self):
        errors = []

        def worker():
            try:
                for _ in range(200):
                    fa.note()
                    fa.active(60)
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [threading.Thread(target=worker) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert not errors
        assert fa.active(60) is True

    def test_never_raises(self):
        # 极端输入也不抛异常（中间件里调用，绝不能影响主链路）
        fa.active(-1)
        fa.active(float("inf"))
        fa.note()


class FakeRedis:
    """最小 Redis 替身：setex/get，支持故障注入。"""

    def __init__(self, down=False):
        self.store = {}
        self.down = down
        self.setex_calls = []

    def setex(self, key, ttl, value):
        if self.down:
            raise ConnectionError("redis down")
        self.setex_calls.append((key, ttl, value))
        self.store[key] = value

    def get(self, key):
        if self.down:
            raise ConnectionError("redis down")
        return self.store.get(key)


class TestForegroundRedis:
    def test_note_redis_writes_key(self):
        r = FakeRedis()
        fa.note_redis(r)
        assert len(r.setex_calls) == 1
        key, ttl, value = r.setex_calls[0]
        assert key == fa.FOREGROUND_REDIS_KEY
        assert ttl == fa.FOREGROUND_REDIS_TTL_S
        assert float(value) > 0

    def test_seconds_since_roundtrip(self):
        r = FakeRedis()
        assert fa.seconds_since_redis_activity(r) is None
        fa.note_redis(r)
        ago = fa.seconds_since_redis_activity(r)
        assert ago is not None and 0 <= ago < 5

    def test_none_redis_is_noop(self):
        # 客户端拿不到（未配置/故障）：全部退化，不抛异常
        fa.note_redis(None)
        assert fa.seconds_since_redis_activity(None) is None

    def test_redis_down_never_raises(self):
        r = FakeRedis(down=True)
        fa.note_redis(r)
        assert fa.seconds_since_redis_activity(r) is None
        assert fa.maybe_yield_for_foreground("scan") == 0.0


class TestMaybeYield:
    def test_yields_while_foreground_active(self, monkeypatch):
        sleeps = []
        monkeypatch.setattr(fa.time, "sleep", lambda s: sleeps.append(s))
        # 前两次检查"刚活跃"，第三次"安静了"
        seq = iter([1.0, 1.0, 999.0])
        monkeypatch.setattr(fa, "seconds_since_redis_activity", lambda *a, **k: next(seq))
        monkeypatch.setenv("SCAN_FOREGROUND_YIELD", "1")
        monkeypatch.setenv("SCAN_FOREGROUND_WINDOW_S", "120")
        monkeypatch.setenv("SCAN_FOREGROUND_SLEEP_S", "5")
        monkeypatch.setenv("SCAN_FOREGROUND_MAX_YIELD_S", "900")
        yielded = fa.maybe_yield_for_foreground("scan")
        assert yielded == 10.0
        assert sleeps == [5.0, 5.0]

    def test_no_yield_when_quiet(self, monkeypatch):
        sleeps = []
        monkeypatch.setattr(fa.time, "sleep", lambda s: sleeps.append(s))
        monkeypatch.setattr(fa, "seconds_since_redis_activity", lambda *a, **k: 999.0)
        assert fa.maybe_yield_for_foreground("scan") == 0.0
        assert sleeps == []

    def test_no_yield_when_never_noted(self, monkeypatch):
        sleeps = []
        monkeypatch.setattr(fa.time, "sleep", lambda s: sleeps.append(s))
        monkeypatch.setattr(fa, "seconds_since_redis_activity", lambda *a, **k: None)
        assert fa.maybe_yield_for_foreground("scan") == 0.0
        assert sleeps == []

    def test_disabled_by_env(self, monkeypatch):
        sleeps = []
        monkeypatch.setattr(fa.time, "sleep", lambda s: sleeps.append(s))
        monkeypatch.setattr(fa, "seconds_since_redis_activity", lambda *a, **k: 1.0)
        monkeypatch.setenv("SCAN_FOREGROUND_YIELD", "0")
        assert fa.maybe_yield_for_foreground("scan") == 0.0
        assert sleeps == []

    def test_respects_max_cap(self, monkeypatch):
        sleeps = []
        monkeypatch.setattr(fa.time, "sleep", lambda s: sleeps.append(s))
        monkeypatch.setattr(fa, "seconds_since_redis_activity", lambda *a, **k: 1.0)
        monkeypatch.setenv("SCAN_FOREGROUND_YIELD", "1")
        monkeypatch.setenv("SCAN_FOREGROUND_SLEEP_S", "5")
        monkeypatch.setenv("SCAN_FOREGROUND_MAX_YIELD_S", "12")
        yielded = fa.maybe_yield_for_foreground("scan")
        assert yielded == 12.0
        assert sleeps == [5.0, 5.0, 2.0]
