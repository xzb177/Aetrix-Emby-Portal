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


class FakeClock:
    """可控时钟：monotonic 只靠 sleep/advance 推进，测试里时间完全确定。"""

    def __init__(self):
        self.now = 1000.0
        self.sleeps = []

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds

    def time(self):
        return self.now

    def advance(self, seconds):
        """模拟干活耗时（不算睡眠）。"""
        self.now += seconds


def _yield_env(monkeypatch, clock, ago=1.0, **env):
    """让路测试脚手架：假时钟 + 前台活跃度 + 默认配额参数（可覆盖）。"""
    monkeypatch.setattr(fa, "time", clock)
    if callable(ago):
        monkeypatch.setattr(fa, "seconds_since_redis_activity", ago)
    else:
        monkeypatch.setattr(
            fa, "seconds_since_redis_activity", lambda *a, **k: ago)
    defaults = {
        "SCAN_FOREGROUND_YIELD": "1",
        "SCAN_FOREGROUND_WINDOW_S": "15",
        "SCAN_FOREGROUND_SLEEP_S": "5",
        "SCAN_FOREGROUND_MAX_YIELD_S": "45",
        "SCAN_FOREGROUND_WORK_MIN_S": "60",
        "SCAN_FOREGROUND_YIELD_RATIO": "0.25",
    }
    defaults.update(env)
    for key, value in defaults.items():
        monkeypatch.setenv(key, value)
    fa.reset_for_tests()


class TestMaybeYield:
    def test_yields_until_foreground_quiet(self, monkeypatch):
        clock = FakeClock()
        # 前两次检查"刚活跃"，第三次"安静了"→ 提前结束让路
        seq = iter([1.0, 1.0, 999.0])
        _yield_env(monkeypatch, clock, ago=lambda *a, **k: next(seq),
                   SCAN_FOREGROUND_WORK_MIN_S="20")
        fa.reset_yield_clock()
        clock.advance(20)  # 干满 20 秒 → 配额 = 20×0.25 = 5…用 seq 前两拍
        # 配额 5 秒只够睡一拍；放大 ratio 让 seq 走完两拍再安静
        monkeypatch.setenv("SCAN_FOREGROUND_YIELD_RATIO", "1.0")
        yielded = fa.maybe_yield_for_foreground("scan")
        assert yielded == 10.0
        assert clock.sleeps == [5.0, 5.0]

    def test_no_yield_when_quiet(self, monkeypatch):
        clock = FakeClock()
        _yield_env(monkeypatch, clock, ago=999.0)
        fa.reset_yield_clock()
        clock.advance(120)  # 干够了，但前台安静 → 不让
        assert fa.maybe_yield_for_foreground("scan") == 0.0
        assert clock.sleeps == []

    def test_no_yield_when_never_noted(self, monkeypatch):
        clock = FakeClock()
        _yield_env(monkeypatch, clock, ago=None)
        fa.reset_yield_clock()
        clock.advance(120)
        assert fa.maybe_yield_for_foreground("scan") == 0.0
        assert clock.sleeps == []

    def test_disabled_by_env(self, monkeypatch):
        clock = FakeClock()
        _yield_env(monkeypatch, clock, ago=1.0, SCAN_FOREGROUND_YIELD="0")
        fa.reset_yield_clock()
        clock.advance(120)
        assert fa.maybe_yield_for_foreground("scan") == 0.0
        assert clock.sleeps == []

    def test_respects_max_cap(self, monkeypatch):
        clock = FakeClock()
        _yield_env(monkeypatch, clock, ago=1.0,
                   SCAN_FOREGROUND_MAX_YIELD_S="12",
                   SCAN_FOREGROUND_YIELD_RATIO="1.0")
        fa.reset_yield_clock()
        clock.advance(600)  # 干了 600 秒，ratio 配额 600 → 被绝对上限 12 钳住
        yielded = fa.maybe_yield_for_foreground("scan")
        assert yielded == 12.0
        assert clock.sleeps == [5.0, 5.0, 2.0]


class TestYieldQuota:
    """让路配额制（2026-10-23 用户拍板）：干满才许让、让 ≤ 干的 25%。"""

    def test_first_call_only_starts_clock(self, monkeypatch):
        clock = FakeClock()
        _yield_env(monkeypatch, clock, ago=1.0)
        # 本线程第一次调用：只起时钟，前台再活跃也不让
        assert fa.maybe_yield_for_foreground("scan") == 0.0
        assert clock.sleeps == []

    def test_no_yield_before_work_min(self, monkeypatch):
        clock = FakeClock()
        _yield_env(monkeypatch, clock, ago=1.0)
        fa.reset_yield_clock()
        clock.advance(59)  # 差 1 秒没干满
        assert fa.maybe_yield_for_foreground("scan") == 0.0
        assert clock.sleeps == []

    def test_yield_capped_at_quarter_of_work(self, monkeypatch):
        clock = FakeClock()
        _yield_env(monkeypatch, clock, ago=1.0)
        fa.reset_yield_clock()
        clock.advance(60)  # 干 60 秒 → 最多让 60×0.25 = 15 秒
        yielded = fa.maybe_yield_for_foreground("scan")
        assert yielded == 15.0
        assert clock.sleeps == [5.0, 5.0, 5.0]

    def test_segment_restarts_after_yield(self, monkeypatch):
        clock = FakeClock()
        _yield_env(monkeypatch, clock, ago=1.0)
        fa.reset_yield_clock()
        clock.advance(60)
        assert fa.maybe_yield_for_foreground("scan") == 15.0
        # 刚让完：下一个干活时段重新计时，立即再调不许再让
        assert fa.maybe_yield_for_foreground("scan") == 0.0
        clock.advance(60)
        assert fa.maybe_yield_for_foreground("scan") == 15.0

    def test_exempt_never_yields(self, monkeypatch):
        clock = FakeClock()
        _yield_env(monkeypatch, clock, ago=1.0)
        fa.reset_yield_clock()
        fa.set_yield_exempt(True)  # 定向扫描：完全不让
        clock.advance(3600)
        assert fa.maybe_yield_for_foreground("scan") == 0.0
        assert clock.sleeps == []
        fa.set_yield_exempt(False)
        assert fa.maybe_yield_for_foreground("scan") > 0

    def test_reset_for_tests_clears_yield_state(self, monkeypatch):
        clock = FakeClock()
        _yield_env(monkeypatch, clock, ago=1.0)
        fa.reset_yield_clock()
        clock.advance(600)
        fa.reset_for_tests()  # 清零后回到「第一次调用只起时钟」
        assert fa.maybe_yield_for_foreground("scan") == 0.0
        assert clock.sleeps == []


class TestCrossProcessVisibility:
    """模拟两个进程共享同一 Redis：API 进程打点，worker 进程读取。

    worker 进程的进程内 _last_ts 恒为 0（从未在本进程打点）——这正是
    item17 的实锤场景：旧代码里 worker 永远看不到前台活跃。
    """

    def setup_method(self):
        self.redis = FakeRedis()
        fa.reset_for_tests()

    def teardown_method(self):
        fa.reset_for_tests()

    def _use_shared_redis(self, monkeypatch, client=None):
        monkeypatch.setattr(fa, "_redis", lambda: self.redis if client is None else client)

    def test_note_writes_redis(self, monkeypatch):
        # note() 写入进程内 + Redis（同一 key，与 note_redis 一致）
        self._use_shared_redis(monkeypatch)
        fa.note()
        assert fa._last_ts > 0
        assert len(self.redis.setex_calls) == 1
        key, ttl, value = self.redis.setex_calls[0]
        assert key == fa.FOREGROUND_REDIS_KEY
        assert ttl == fa.FOREGROUND_REDIS_TTL_S
        assert float(value) > 0

    def test_active_visible_across_processes(self, monkeypatch):
        # 「API 进程」打点后，「worker 进程」（本地 _last_ts=0）能读到
        self._use_shared_redis(monkeypatch)
        fa.note()  # API 进程：写 Redis + 本地
        fa._last_ts = 0.0  # 模拟 worker 进程：本进程从未打点
        assert fa.active(60) is True
        ago = fa.last_seen_ago()
        assert ago is not None and 0 <= ago < 5

    def test_note_redis_only_also_visible(self, monkeypatch):
        # main.py 的 _check_rate_limit 走 note_redis（不走 note）：worker 同样可见
        self._use_shared_redis(monkeypatch)
        fa.note_redis(self.redis)
        fa._last_ts = 0.0  # 模拟 worker 进程
        assert fa.active(60) is True

    def test_no_redis_no_local_is_inactive(self, monkeypatch):
        # Redis 缺席 + 本地无打点 = 不活跃（旧行为）
        monkeypatch.setattr(fa, "_redis", lambda: None)
        fa.reset_for_tests()
        assert fa.active(60) is False
        assert fa.last_seen_ago() is None

    def test_redis_down_falls_back_to_local(self, monkeypatch):
        # Redis 抖动：写入失败不抛错，本地打点仍在，读取走降级路径
        monkeypatch.setattr(fa, "_redis", lambda: FakeRedis(down=True))
        fa.note()
        assert fa._last_ts > 0
        assert fa.active(60) is True
        ago = fa.last_seen_ago()
        assert ago is not None and 0 <= ago < 5

    def test_prefers_fresher_of_two_sources(self, monkeypatch):
        # Redis 里是 1000 秒前的旧打点（上次写入后 Redis 抖动没更新成功），
        # 本进程刚刚收到前台请求 → 取较新者，不漏报活跃
        self._use_shared_redis(monkeypatch)
        self.redis.setex(fa.FOREGROUND_REDIS_KEY, fa.FOREGROUND_REDIS_TTL_S,
                         time.time() - 1000)
        fa._last_ts = time.monotonic()  # 模拟"本地写入成功、Redis 写入那次抖掉了"
        assert fa.active(60) is True
        ago = fa.last_seen_ago()
        assert ago is not None and 0 <= ago < 5

    def test_stale_redis_alone_is_inactive(self, monkeypatch):
        # 只有 Redis 的旧打点（>窗口）→ 不活跃，窗口语义不变
        self._use_shared_redis(monkeypatch)
        self.redis.setex(fa.FOREGROUND_REDIS_KEY, fa.FOREGROUND_REDIS_TTL_S,
                         time.time() - 1000)
        assert fa.active(60) is False

    def test_never_raises_with_redis(self, monkeypatch):
        # 极端输入 + Redis 在场也不抛异常
        self._use_shared_redis(monkeypatch)
        fa.note()
        fa.active(-1)
        fa.active(float("inf"))
        fa.last_seen_ago()
