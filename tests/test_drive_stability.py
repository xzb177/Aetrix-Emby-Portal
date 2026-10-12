"""Drive API 稳定性测试（2026-10-12 .strm 生成器加固）。

覆盖：
- _retry_sleep_sec 退避数学（2s 起、翻倍、±20% 抖动、cap 60s）
- 429 Retry-After 头处理
- 网络层瞬时异常重试（超时/连接重置）
- 401/404 快速失败不重试
- 重试耗尽抛异常（不静默）
- 线程本地重试计数器隔离
- ApiRateLimiter 令牌桶
- list_changes 不再吞错（异常向上传播）
"""
from __future__ import annotations

import threading
import time
from unittest import mock

import pytest

from backend.emby_server import drive_changes as dc


# ---------------------------------------------------------------------------
# 退避数学
# ---------------------------------------------------------------------------

class TestRetrySleep:
    def test_base_doubling(self):
        with mock.patch.object(dc._random, "uniform", return_value=1.0):
            assert dc._retry_sleep_sec(0) == 2.0
            assert dc._retry_sleep_sec(1) == 4.0
            assert dc._retry_sleep_sec(2) == 8.0
            assert dc._retry_sleep_sec(3) == 16.0
            assert dc._retry_sleep_sec(4) == 32.0

    def test_cap_60(self):
        with mock.patch.object(dc._random, "uniform", return_value=1.0):
            assert dc._retry_sleep_sec(10) == 60.0
            assert dc._retry_sleep_sec(100) == 60.0

    def test_jitter_bounds(self):
        # ±20%：2s 基础应在 [1.6, 2.4]
        for _ in range(50):
            v = dc._retry_sleep_sec(0)
            assert 1.6 <= v <= 2.4, v
        # 绝不能接近 0（原来 full-jitter 的 bug）
        for _ in range(50):
            assert dc._retry_sleep_sec(0) >= 1.0

    def test_429_with_retry_after(self):
        with mock.patch.object(dc._random, "uniform", return_value=1.0):
            assert dc._retry_sleep_sec(0, True, 10.0) == 10.0
            assert dc._retry_sleep_sec(2, True, 10.0) == 10.0  # 头优先，不看 attempt
            assert dc._retry_sleep_sec(0, True, 500.0) == 300.0  # 钳制

    def test_429_without_header_double_backoff(self):
        with mock.patch.object(dc._random, "uniform", return_value=1.0):
            assert dc._retry_sleep_sec(0, True, None) == 4.0
            assert dc._retry_sleep_sec(1, True, None) == 8.0
            assert dc._retry_sleep_sec(2, True, None) == 16.0

    def test_429_jitter(self):
        for _ in range(30):
            v = dc._retry_sleep_sec(0, True, 10.0)
            assert 8.0 <= v <= 12.0, v


class TestParseRetryAfter:
    def test_valid(self):
        assert dc._parse_retry_after("10") == 10.0
        assert dc._parse_retry_after("  5.5 ") == 5.5
        assert dc._parse_retry_after("0") == 0.0

    def test_invalid(self):
        assert dc._parse_retry_after(None) is None
        assert dc._parse_retry_after("") is None
        assert dc._parse_retry_after("   ") is None
        assert dc._parse_retry_after("abc") is None
        assert dc._parse_retry_after("-5") is None
        assert dc._parse_retry_after("Wed, 21 Oct 2015") is None  # HTTP-date 不处理


# ---------------------------------------------------------------------------
# _api_get 行为（mock httpx）
# ---------------------------------------------------------------------------

def _resp(status, headers=None, json_data=None):
    r = mock.MagicMock()
    r.status_code = status
    r.headers = headers or {}
    if json_data is not None:
        r.json.return_value = json_data
    return r


class TestApiGet:
    def test_success_no_retry(self):
        with mock.patch.object(dc.httpx, "get", return_value=_resp(200, {}, {"a": 1})), \
             mock.patch.object(dc, "_api_limiter") as lim:
            assert dc._api_get("http://x", "t", {}) == {"a": 1}
            assert dc.get_last_retry_count() == 0
            lim.acquire.assert_called()

    def test_503_retry_then_success(self):
        calls = []
        def fake_get(url, headers, params, timeout):
            calls.append(1)
            return _resp(503) if len(calls) < 3 else _resp(200, {}, {"ok": 1})
        with mock.patch.object(dc.httpx, "get", side_effect=fake_get), \
             mock.patch.object(dc.time, "sleep"), \
             mock.patch.object(dc, "_api_limiter"):
            assert dc._api_get("http://x", "t", {}) == {"ok": 1}
            assert len(calls) == 3
            assert dc.get_last_retry_count() == 2

    def test_503_backoff_timing(self):
        sleeps = []
        def fake_get(url, headers, params, timeout):
            return _resp(503)
        with mock.patch.object(dc.httpx, "get", side_effect=fake_get), \
             mock.patch.object(dc.time, "sleep", side_effect=lambda s: sleeps.append(s)), \
             mock.patch.object(dc._random, "uniform", return_value=1.0), \
             mock.patch.object(dc, "_api_limiter"):
            with pytest.raises(Exception):
                dc._api_get("http://x", "t", {})
        # 5 次重试：2, 4, 8, 16, 32
        assert sleeps == [2.0, 4.0, 8.0, 16.0, 32.0], sleeps

    def test_exhaust_raises_not_silent(self):
        def fake_get(url, headers, params, timeout):
            return _resp(503)
        with mock.patch.object(dc.httpx, "get", side_effect=fake_get), \
             mock.patch.object(dc.time, "sleep"), \
             mock.patch.object(dc, "_api_limiter"):
            with pytest.raises(Exception) as ei:
                dc._api_get("http://x", "t", {})
            assert "503" in str(ei.value)

    def test_401_fast_fail(self):
        calls = []
        def fake_get(url, headers, params, timeout):
            calls.append(1)
            return _resp(401)
        with mock.patch.object(dc.httpx, "get", side_effect=fake_get), \
             mock.patch.object(dc, "_api_limiter"):
            with pytest.raises(PermissionError):
                dc._api_get("http://x", "t", {})
        assert len(calls) == 1  # 不重试

    def test_404_fast_fail(self):
        calls = []
        def fake_get(url, headers, params, timeout):
            calls.append(1)
            return _resp(404)
        with mock.patch.object(dc.httpx, "get", side_effect=fake_get), \
             mock.patch.object(dc, "_api_limiter"):
            with pytest.raises(dc.DriveNotFoundError):
                dc._api_get("http://x", "t", {"driveId": "d1"})
        assert len(calls) == 1  # 不重试

    def test_429_retry_after_respected(self):
        sleeps = []
        calls = []
        def fake_get(url, headers, params, timeout):
            calls.append(1)
            if len(calls) == 1:
                return _resp(429, {"Retry-After": "10"})
            return _resp(200, {}, {})
        with mock.patch.object(dc.httpx, "get", side_effect=fake_get), \
             mock.patch.object(dc.time, "sleep", side_effect=lambda s: sleeps.append(s)), \
             mock.patch.object(dc._random, "uniform", return_value=1.0), \
             mock.patch.object(dc, "_api_limiter"):
            dc._api_get("http://x", "t", {})
        assert sleeps == [10.0], sleeps

    def test_429_no_header_double_backoff(self):
        sleeps = []
        def fake_get(url, headers, params, timeout):
            return _resp(429)
        with mock.patch.object(dc.httpx, "get", side_effect=fake_get), \
             mock.patch.object(dc.time, "sleep", side_effect=lambda s: sleeps.append(s)), \
             mock.patch.object(dc._random, "uniform", return_value=1.0), \
             mock.patch.object(dc, "_api_limiter"):
            with pytest.raises(Exception):
                dc._api_get("http://x", "t", {})
        # 2 倍退避：4, 8, 16, 32, 60(cap)
        assert sleeps == [4.0, 8.0, 16.0, 32.0, 60.0], sleeps

    def test_network_error_retried(self):
        calls = []
        def fake_get(url, headers, params, timeout):
            calls.append(1)
            if len(calls) < 3:
                raise dc.httpx.ConnectError("connection reset")
            return _resp(200, {}, {"ok": 1})
        with mock.patch.object(dc.httpx, "get", side_effect=fake_get), \
             mock.patch.object(dc.time, "sleep"), \
             mock.patch.object(dc, "_api_limiter"):
            assert dc._api_get("http://x", "t", {}) == {"ok": 1}
            assert dc.get_last_retry_count() == 2

    def test_timeout_retried(self):
        def fake_get(url, headers, params, timeout):
            raise dc.httpx.TimeoutException("read timed out")
        with mock.patch.object(dc.httpx, "get", side_effect=fake_get), \
             mock.patch.object(dc.time, "sleep"), \
             mock.patch.object(dc, "_api_limiter"):
            with pytest.raises(dc.httpx.TimeoutException):
                dc._api_get("http://x", "t", {})

    def test_400_not_retried(self):
        calls = []
        def fake_get(url, headers, params, timeout):
            calls.append(1)
            r = _resp(400)
            r.raise_for_status.side_effect = Exception("400 bad request")
            return r
        with mock.patch.object(dc.httpx, "get", side_effect=fake_get), \
             mock.patch.object(dc, "_api_limiter"):
            with pytest.raises(Exception):
                dc._api_get("http://x", "t", {})
        assert len(calls) == 1

    def test_rate_limiter_called_each_attempt(self):
        lim = mock.MagicMock()
        calls = []
        def fake_get(url, headers, params, timeout):
            calls.append(1)
            return _resp(503) if len(calls) < 2 else _resp(200, {}, {})
        with mock.patch.object(dc.httpx, "get", side_effect=fake_get), \
             mock.patch.object(dc.time, "sleep"), \
             mock.patch.object(dc, "_api_limiter", lim):
            dc._api_get("http://x", "t", {})
        # 每次请求前都过速率限制器（含重试）
        assert lim.acquire.call_count == 2


class TestRetryCounterThreads:
    def test_isolation(self):
        results = {}
        def worker(n, val):
            dc._retry_state.retry_count = val
            dc._retry_state.total = val * 2
            results[n] = (dc.get_last_retry_count(), dc.get_total_retry_count())
        ts = [threading.Thread(target=worker, args=(i, i * 10)) for i in range(5)]
        [t.start() for t in ts]
        [t.join() for t in ts]
        assert results == {i: (i * 10, i * 20) for i in range(5)}

    def test_total_accumulates(self):
        dc.reset_total_retry_count()
        assert dc.get_total_retry_count() == 0
        dc._retry_state.total = 3
        assert dc.get_total_retry_count() == 3


# ---------------------------------------------------------------------------
# 速率限制器
# ---------------------------------------------------------------------------

class TestApiRateLimiter:
    def test_burst_then_limited(self):
        lim = dc.ApiRateLimiter(10.0)
        t0 = time.monotonic()
        for _ in range(10):  # 桶满，瞬间通过
            lim.acquire()
        assert time.monotonic() - t0 < 0.5
        t0 = time.monotonic()
        lim.acquire()  # 第 11 个要等 ~0.1s
        assert 0.05 < time.monotonic() - t0 < 0.5

    def test_sustained_rate(self):
        lim = dc.ApiRateLimiter(5.0)
        lim._tokens = 0
        t0 = time.monotonic()
        for _ in range(10):
            lim.acquire()
        dt = time.monotonic() - t0
        assert 1.5 < dt < 3.5, dt  # 10 个令牌 @5QPS ≈ 2s

    def test_qps_clamp(self):
        assert dc.ApiRateLimiter(100.0)._qps == 20.0
        assert dc.ApiRateLimiter(0.01)._qps == 1.0
        lim = dc.ApiRateLimiter(8.0)
        lim.set_qps(50.0)
        assert lim._qps == 20.0

    def test_set_api_qps(self):
        dc.set_api_qps(5.0)
        assert dc._api_limiter._qps == 5.0
        dc.set_api_qps(8.0)  # 恢复默认


# ---------------------------------------------------------------------------
# list_changes 不吞错
# ---------------------------------------------------------------------------

class TestListChangesNoSwallow:
    def test_exception_propagates(self):
        with mock.patch.object(dc, "_get_token", return_value="tok"), \
             mock.patch.object(dc, "_api_get", side_effect=RuntimeError("boom")):
            with pytest.raises(RuntimeError, match="boom"):
                dc.list_changes("token123", "drive1")
            # 确认不是返回 ([], None)
            # （上面的 pytest.raises 已保证抛出来了）

    def test_drive_not_found_still_raises(self):
        with mock.patch.object(dc, "_get_token", return_value="tok"), \
             mock.patch.object(dc, "_api_get", side_effect=dc.DriveNotFoundError("d1")):
            with pytest.raises(dc.DriveNotFoundError):
                dc.list_changes("token123", "drive1")

    def test_empty_changes_still_ok(self):
        # 真的无变化时返回 ([], token)，不是抛错
        with mock.patch.object(dc, "_get_token", return_value="tok"), \
             mock.patch.object(dc, "_api_get", return_value={
                 "changes": [], "newStartPageToken": "newtok", "nextPageToken": None,
             }):
            changes, new_token = dc.list_changes("token123", "drive1")
            assert changes == []
            assert new_token == "newtok"
