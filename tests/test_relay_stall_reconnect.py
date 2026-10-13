"""慢连接自动重连（2026-10-13）：中继层塌速监测 + make-before-break 续传。

覆盖：
- _StallDetector：快流不触发、慢流触发、宽限期、窗口滑动、客户端慢不误触发
- _ReconnectBudget：令牌桶获取/耗尽/回充
- _ReconnectBreaker：阈值熔断、冷却、自动闭合、老记录过期
- _check_new_upstream / _resume_offset / _upstream_resumable / _fresh_sa_token
- _RelayStallState：退避、resume_at 回退一整块
- 集成（异步）：假慢速上游 → 触发重连 → 字节无缝（无缺口无重复）、客户端
  状态码/响应头不变、重连 Range 偏移正确
- 集成（异步）：新连接失败（非 206）→ make-before-break，保持旧连接不断流
- 集成（异步）：不可续传上游 → 不重连
- 集成（同步）：serve_remote 同样无缝重连
"""
import asyncio
import time

import pytest
from starlette.requests import Request

from backend.emby_server import streaming
from backend.emby_server.streaming import (
    READ_CHUNK,
    _check_new_upstream,
    _fresh_sa_token,
    _ReconnectBreaker,
    _ReconnectBudget,
    _RelayStallState,
    _reset_reconnect_state,
    _resume_offset,
    _StallDetector,
    _upstream_resumable,
)

# 公网 IP 字面量：避免测试里做真实 DNS（沿用 test_relay_redirect_leak.py 惯例）
ORIGIN = "https://93.184.216.34/v.mp4"
TOTAL = 4 * 65536  # 256KB 测试文件


def _request(headers=None):
    scope = {
        "type": "http",
        "method": "GET",
        "path": "/v.mp4",
        "raw_path": b"/v.mp4",
        "query_string": b"",
        "headers": [
            (k.lower().encode("latin-1"), v.encode("latin-1"))
            for k, v in (headers or {}).items()
        ],
    }
    return Request(scope)


def _payload(seed=7):
    return bytes((i * 31 + seed) % 251 for i in range(TOTAL))


# ---------------------------------------------------------------------------
# 判定器单元测试
# ---------------------------------------------------------------------------

class TestStallDetector:
    def _fast(self, d, n=10):
        for _ in range(n):
            d.observe(1024 * 1024, 0.005)  # 1MB / 5ms = 200MB/s

    def test_fast_never_stalls(self):
        d = _StallDetector()
        self._fast(d, 50)
        assert not d.stalled()

    def test_slow_triggers(self, monkeypatch):
        monkeypatch.setattr(streaming, "_RELAY_STALL_GRACE_BYTES", 1024)
        monkeypatch.setattr(streaming, "_RELAY_STALL_GRACE_SEC", 0.0)
        monkeypatch.setattr(streaming, "_RELAY_STALL_WINDOW_SEC", 5.0)
        d = _StallDetector()
        for _ in range(4):
            d.observe(32 * 1024, 0.5)  # 64KB/s，远低于 180KB/s 阈值
        assert d.stalled()

    def test_grace_period_protects(self, monkeypatch):
        # 宽限期内（字节不够、时间不够）即使慢也不触发
        monkeypatch.setattr(streaming, "_RELAY_STALL_GRACE_BYTES", 10 * 1024 * 1024)
        monkeypatch.setattr(streaming, "_RELAY_STALL_GRACE_SEC", 3600.0)
        d = _StallDetector()
        for _ in range(4):
            d.observe(32 * 1024, 0.5)
        assert not d.stalled()

    def test_grace_wall_clock_expires(self, monkeypatch):
        # 字节不够但墙钟超了宽限 → 开始判定
        monkeypatch.setattr(streaming, "_RELAY_STALL_GRACE_BYTES", 10 * 1024 * 1024)
        monkeypatch.setattr(streaming, "_RELAY_STALL_GRACE_SEC", 0.0)
        monkeypatch.setattr(streaming, "_RELAY_STALL_WINDOW_SEC", 5.0)
        d = _StallDetector()
        for _ in range(4):
            d.observe(32 * 1024, 0.5)
        assert d.stalled()

    def test_window_slides(self, monkeypatch):
        # 先快后慢：旧的快速样本滑出窗口后触发
        monkeypatch.setattr(streaming, "_RELAY_STALL_GRACE_BYTES", 1024)
        monkeypatch.setattr(streaming, "_RELAY_STALL_GRACE_SEC", 0.0)
        monkeypatch.setattr(streaming, "_RELAY_STALL_WINDOW_SEC", 1.0)
        d = _StallDetector()
        self._fast(d, 20)
        assert not d.stalled()
        for _ in range(10):
            d.observe(1024, 1.0)  # 1KB/s
        assert d.stalled()

    def test_slow_client_does_not_trigger(self, monkeypatch):
        """客户端慢（yield park 久）不计入上游等待 → 不误触发。

        模拟：上游每次 5ms 就吐出 1MB（极快），但客户端每块阻塞 0.3s。
        检测器只收到 (nbytes, wait_sec)，wall 间隔不影响判定。
        """
        monkeypatch.setattr(streaming, "_RELAY_STALL_GRACE_BYTES", 1024)
        monkeypatch.setattr(streaming, "_RELAY_STALL_GRACE_SEC", 0.0)
        d = _StallDetector()
        for _ in range(20):
            d.observe(1024 * 1024, 0.005)
            time.sleep(0.05)  # 模拟客户端慢：只拉长 wall 时间，不改变 wait
        assert not d.stalled()

    def test_reset_clears_window(self, monkeypatch):
        monkeypatch.setattr(streaming, "_RELAY_STALL_GRACE_BYTES", 1024)
        monkeypatch.setattr(streaming, "_RELAY_STALL_GRACE_SEC", 0.0)
        monkeypatch.setattr(streaming, "_RELAY_STALL_WINDOW_SEC", 5.0)
        d = _StallDetector()
        for _ in range(4):
            d.observe(32 * 1024, 0.5)
        assert d.stalled()
        d.reset()
        assert not d.stalled()
        # 宽限只给一次：reset 后 total 保留，慢速重新积累仍会触发
        for _ in range(4):
            d.observe(32 * 1024, 0.5)
        assert d.stalled()


# ---------------------------------------------------------------------------
# 令牌桶 / 熔断器
# ---------------------------------------------------------------------------

class _FakeClock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t

    def advance(self, sec):
        self.t += sec


class TestReconnectBudget:
    def test_acquire_up_to_capacity(self):
        c = _FakeClock()
        b = _ReconnectBudget(3, _clock=c)
        assert b.try_acquire()
        assert b.try_acquire()
        assert b.try_acquire()
        assert not b.try_acquire()

    def test_refill_over_time(self):
        c = _FakeClock()
        b = _ReconnectBudget(60, _clock=c)  # 1 token/sec
        for _ in range(60):
            assert b.try_acquire()
        assert not b.try_acquire()
        c.advance(2.5)
        assert b.try_acquire()
        assert b.try_acquire()
        assert not b.try_acquire()

    def test_capacity_cap(self):
        c = _FakeClock()
        b = _ReconnectBudget(5, _clock=c)
        c.advance(3600)  # 闲置很久
        for _ in range(5):
            assert b.try_acquire()
        assert not b.try_acquire()  # 不会无限累积


class TestReconnectBreaker:
    def test_trips_at_threshold(self):
        c = _FakeClock()
        br = _ReconnectBreaker(60.0, 3, 300.0, _clock=c)
        assert not br.is_open()
        br.record()
        br.record()
        assert not br.is_open()
        br.record()
        assert br.is_open()

    def test_cooldown_closes(self):
        c = _FakeClock()
        br = _ReconnectBreaker(60.0, 2, 100.0, _clock=c)
        br.record()
        br.record()
        assert br.is_open()
        c.advance(99)
        assert br.is_open()
        c.advance(2)
        assert not br.is_open()

    def test_old_attempts_age_out(self):
        c = _FakeClock()
        br = _ReconnectBreaker(60.0, 3, 300.0, _clock=c)
        br.record()
        br.record()
        c.advance(61)  # 前两次滑出窗口
        br.record()
        assert not br.is_open()


# ---------------------------------------------------------------------------
# 小 helper
# ---------------------------------------------------------------------------

class _H:
    def __init__(self, d):
        self._d = {k.lower(): v for k, v in d.items()}

    def get(self, k, default=None):
        return self._d.get(k.lower(), default)


class _R:
    def __init__(self, status, headers):
        self.status_code = status
        self.headers = _H(headers)


def test_check_new_upstream():
    ok = _R(206, {"content-range": "bytes 1048576-2097151/5242880"})
    assert _check_new_upstream(ok, 1048576) is None
    bad_status = _R(200, {"content-range": "bytes 0-1048575/5242880"})
    assert "206" in _check_new_upstream(bad_status, 1048576)
    no_cr = _R(206, {})
    assert "Content-Range" in _check_new_upstream(no_cr, 1048576)
    wrong_start = _R(206, {"content-range": "bytes 0-1048575/5242880"})
    assert "不符" in _check_new_upstream(wrong_start, 1048576)


def test_resume_offset():
    r = _R(206, {"content-range": "bytes 300-599/1000"})
    assert _resume_offset(r, "bytes=0-") == 300
    r2 = _R(206, {})
    assert _resume_offset(r2, "bytes=123-") == 123
    assert _resume_offset(r2, None) == 0


def test_upstream_resumable():
    assert _upstream_resumable(_R(206, {"accept-ranges": "bytes"}))
    assert _upstream_resumable(_R(200, {"content-range": "bytes 0-99/100"}))
    assert not _upstream_resumable(_R(200, {}))


def test_fresh_sa_token_non_drive():
    # 非 Drive 上游：不换 SA
    assert _fresh_sa_token("Bearer old", "https://93.184.216.34/v.mp4") is None


def test_fresh_sa_token_switches(monkeypatch):
    import backend.emby_server.drive_auth as da
    calls = []

    def fake_get(affinity_key=None):
        calls.append(affinity_key)
        return "tok-new"

    monkeypatch.setattr(da, "get_drive_bearer_token", fake_get)
    url = "https://www.googleapis.com/drive/v3/files/abc?alt=media"
    assert _fresh_sa_token("Bearer tok-old", url) == "tok-new"
    assert calls == [None]  # affinity_key=None → 打破亲和


def test_fresh_sa_token_avoids_same(monkeypatch):
    import backend.emby_server.drive_auth as da
    seq = ["tok-old", "tok-old", "tok-new"]

    def fake_get(affinity_key=None):
        return seq.pop(0)

    monkeypatch.setattr(da, "get_drive_bearer_token", fake_get)
    url = "https://www.googleapis.com/drive/v3/files/abc?alt=media"
    assert _fresh_sa_token("Bearer tok-old", url) == "tok-new"


def test_stall_state_backoff_and_resume_at(monkeypatch):
    monkeypatch.setattr(streaming, "_RELAY_STALL_BACKOFF_BASE", 2.0)
    monkeypatch.setattr(streaming, "_RELAY_STALL_BACKOFF_CAP", 30.0)
    monkeypatch.setattr(streaming, "_RELAY_STALL_MAX_RECONNECTS", 3)
    s = _RelayStallState(100, "ctx", enabled=False)
    s.sent = 2 * READ_CHUNK
    assert s.resume_at == 100 + 2 * READ_CHUNK - READ_CHUNK
    assert not s.want_reconnect()  # disabled
    s2 = _RelayStallState(0, "ctx", enabled=True)
    s2.note_attempt()
    assert not s2.want_reconnect()  # 退避中
    assert s2.attempts == 1


# ---------------------------------------------------------------------------
# 集成测试：假上游
# ---------------------------------------------------------------------------

class _FakeAsyncResp:
    """模拟 httpx 流式响应：aiter_bytes 按 chunk_size 重切（模拟 ByteChunker），
    每 piece 可 sleep 模拟慢速上游。"""

    def __init__(self, status_code, headers, data: bytes, piece=65536, delay=0.0):
        self.status_code = status_code
        self.headers = _H(headers)
        self._data = data
        self._piece = piece
        self._delay = delay
        self.aclose_calls = 0

    def aiter_bytes(self, chunk_size):
        return self._gen(chunk_size)

    async def _gen(self, chunk_size):
        buf = bytearray()
        for i in range(0, len(self._data), self._piece):
            if self._delay:
                await asyncio.sleep(self._delay)
            buf += self._data[i:i + self._piece]
            while len(buf) >= chunk_size:
                yield bytes(buf[:chunk_size])
                del buf[:chunk_size]
        if buf:
            yield bytes(buf)

    async def aclose(self):
        self.aclose_calls += 1


class _FakeSyncResp:
    def __init__(self, status_code, headers, data: bytes, piece=65536, delay=0.0):
        self.status_code = status_code
        self.headers = _H(headers)
        self._data = data
        self._piece = piece
        self._delay = delay
        self.close_calls = 0

    def iter_bytes(self, chunk_size):
        buf = bytearray()
        for i in range(0, len(self._data), self._piece):
            if self._delay:
                time.sleep(self._delay)
            buf += self._data[i:i + self._piece]
            while len(buf) >= chunk_size:
                yield bytes(buf[:chunk_size])
                del buf[:chunk_size]
        if buf:
            yield bytes(buf)

    def close(self):
        self.close_calls += 1


def _hdrs(start=0):
    return {
        "content-range": f"bytes {start}-{TOTAL - 1}/{TOTAL}",
        "accept-ranges": "bytes",
        "content-length": str(TOTAL - start),
        "content-type": "video/mp4",
    }


def _stall_cfg(monkeypatch):
    monkeypatch.setattr(streaming, "_RELAY_STALL_MIN_KBPS", 180.0)
    monkeypatch.setattr(streaming, "_RELAY_STALL_WINDOW_SEC", 3.0)
    monkeypatch.setattr(streaming, "_RELAY_STALL_GRACE_BYTES", 1024)
    monkeypatch.setattr(streaming, "_RELAY_STALL_GRACE_SEC", 0.0)
    monkeypatch.setattr(streaming, "_RELAY_STALL_MAX_RECONNECTS", 3)
    monkeypatch.setattr(streaming, "_RELAY_STALL_BACKOFF_BASE", 0.1)
    monkeypatch.setattr(streaming, "READ_CHUNK", 65536)
    _reset_reconnect_state()


@pytest.fixture
def follow_script(monkeypatch):
    """替换 _follow_redirects：按脚本返回假上游，记录每次调用的 Range。"""
    calls = []

    async def fake_follow(client, start_url, forward, silent=False):
        calls.append((start_url, dict(forward)))
        item = fake_follow.script.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item, start_url, 0

    fake_follow.script = []
    fake_follow.calls = calls
    monkeypatch.setattr(streaming, "_follow_redirects", fake_follow)
    monkeypatch.setattr(streaming, "get_relay_client", lambda: object())
    return fake_follow


def _run(coro):
    return asyncio.run(coro)


def test_async_reconnect_seamless(monkeypatch, follow_script, caplog):
    """慢速上游 → 触发重连 → 客户端拿到完整字节（无缺口无重复），状态码/头不变。"""
    import logging
    _stall_cfg(monkeypatch)
    data = _payload()
    slow = _FakeAsyncResp(206, _hdrs(0), data, piece=65536, delay=0.4)  # 160KB/s
    fast = _FakeAsyncResp(206, _hdrs(0), data)  # 重连后 Range bytes=0-，全量快发
    follow_script.script = [slow, fast]

    async def go():
        with caplog.at_level(logging.INFO, logger="backend.emby_server.streaming"):
            resp = await streaming.serve_remote_async(
                ORIGIN, _request({"range": "bytes=0-"}))
            body = b"".join([c async for c in resp.body_iterator])
            return resp, body

    resp, body = _run(go())
    assert body == data  # 无缝：无缺口无重复
    assert resp.status_code == 206
    assert resp.headers["Content-Range"] == f"bytes 0-{TOTAL - 1}/{TOTAL}"
    # 重连确实发生了：第二次建连的 Range 从 resume_at 开始
    assert len(follow_script.calls) == 2
    assert follow_script.calls[1][1]["Range"] == "bytes=0-"
    # 旧连接在新连接确认后才关闭（make-before-break：都关过）
    assert slow.aclose_calls >= 1
    assert "[relay-reconnect]" in caplog.text


def test_async_reconnect_new_conn_fails_keeps_old(monkeypatch, follow_script):
    """新连接失败（非 206）→ make-before-break：继续用旧连接不断流。"""
    _stall_cfg(monkeypatch)
    data = _payload()
    slow = _FakeAsyncResp(206, _hdrs(0), data, piece=65536, delay=0.4)
    bad = _FakeAsyncResp(200, {}, data)  # 上游忽略 Range 回 200 → 不可用
    follow_script.script = [slow, bad, bad, bad]

    async def go():
        resp = await streaming.serve_remote_async(
            ORIGIN, _request({"range": "bytes=0-"}))
        body = b"".join([c async for c in resp.body_iterator])
        return resp, body

    resp, body = _run(go())
    assert body == data  # 旧连接完整吐完（新连接失败不断流）
    assert resp.status_code == 206
    # 至少尝试过一次重连（失败后按退避重试，上限 3 次，次数与抖动 timing 相关）
    assert 2 <= len(follow_script.calls) <= 4
    assert follow_script.calls[1][1]["Range"] == "bytes=0-"


def test_async_no_reconnect_when_not_resumable(monkeypatch, follow_script):
    """上游不支持续传 → 不重连，原样透传。"""
    _stall_cfg(monkeypatch)
    data = _payload()
    slow = _FakeAsyncResp(200, {"content-length": str(TOTAL)}, data,
                          piece=65536, delay=0.4)
    follow_script.script = [slow]

    async def go():
        resp = await streaming.serve_remote_async(ORIGIN, _request())
        body = b"".join([c async for c in resp.body_iterator])
        return resp, body

    resp, body = _run(go())
    assert body == data
    assert resp.status_code == 200
    assert len(follow_script.calls) == 1  # 没触发重连


def test_async_no_reconnect_when_disabled(monkeypatch, follow_script):
    """总开关关闭 → 行为与原来一致。"""
    _stall_cfg(monkeypatch)
    monkeypatch.setattr(streaming, "_RELAY_STALL_RECONNECT", False)
    data = _payload()
    slow = _FakeAsyncResp(206, _hdrs(0), data, piece=65536, delay=0.4)
    follow_script.script = [slow]

    async def go():
        resp = await streaming.serve_remote_async(
            ORIGIN, _request({"range": "bytes=0-"}))
        body = b"".join([c async for c in resp.body_iterator])
        return resp, body

    resp, body = _run(go())
    assert body == data
    assert len(follow_script.calls) == 1


def test_breaker_counts_failed_attempts(monkeypatch, follow_script):
    """熔断计数的是尝试次数：新连接批量失败（账号级限速场景）也要能打开熔断。"""
    _stall_cfg(monkeypatch)
    br = _ReconnectBreaker(60.0, 2, 300.0)  # 2 次尝试就熔断
    monkeypatch.setattr(streaming, "_RECONNECT_BREAKER", br)
    data = _payload()
    slow = _FakeAsyncResp(206, _hdrs(0), data, piece=65536, delay=0.4)
    bad = _FakeAsyncResp(200, {}, data)
    follow_script.script = [slow, bad, bad, bad]

    async def go():
        resp = await streaming.serve_remote_async(
            ORIGIN, _request({"range": "bytes=0-"}))
        body = b"".join([c async for c in resp.body_iterator])
        return resp, body

    resp, body = _run(go())
    assert body == data  # 旧连接不断流
    assert br.is_open()  # 失败的尝试也被计数，熔断已打开


def test_sync_reconnect_seamless(monkeypatch):
    """同步版 serve_remote 同样无缝重连。"""
    _stall_cfg(monkeypatch)
    data = _payload()
    slow = _FakeSyncResp(206, _hdrs(0), data, piece=65536, delay=0.4)
    fast = _FakeSyncResp(206, _hdrs(0), data)
    calls = []
    script = [slow, fast]

    def fake_open(client, api_url, forward, dns_pinning, silent=False):
        calls.append((api_url, dict(forward)))
        return script.pop(0), api_url, forward

    monkeypatch.setattr(streaming, "_open_upstream_sync", fake_open)
    resp = streaming.serve_remote(ORIGIN, _request({"range": "bytes=0-"}))

    async def _collect():
        # Starlette 1.7 起 body_iterator 恒为 async（同步迭代器会被包一层）
        return b"".join([c async for c in resp.body_iterator])

    body = asyncio.run(_collect())
    assert body == data
    assert resp.status_code == 206
    assert len(calls) == 2
    assert calls[1][1]["Range"] == "bytes=0-"
    assert slow.close_calls >= 1
