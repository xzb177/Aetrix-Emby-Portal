"""Drive alt=media 403 处理回归测试（2026-10-15 定因：SA 级瞬时限流，换 SA 即恢复）。

覆盖：
- `_drive_403_error_reason`：Google 错误体 reason/message 解析与各种脏输入。
- `drive_auth.drop_sa_affinity`：亲和删除语义。
- `_drive_403_retry_async` / `_drive_403_retry_sync`：可重试类换 SA 重试、
  不可重试类直接失败、有界次数、永不抛异常、不记 token。

全部离线：不做真实网络/DNS。
"""
import asyncio

import pytest

from backend.emby_server import drive_auth
from backend.emby_server import streaming

API_URL = "https://www.googleapis.com/drive/v3/files/FID123?alt=media"
UC_URL = "https://drive.google.com/uc?export=download&id=FID123&confirm=t"

_ERR_RATELIMIT = (
    b'{"error": {"code": 403, "message": "The user has exceeded their Drive API quota.", '
    b'"errors": [{"domain": "usageLimits", "reason": "userRateLimitExceeded", '
    b'"message": "User rate limit exceeded."}]}}'
)
_ERR_ABUSIVE = (
    b'{"error": {"code": 403, "message": "This file cannot be downloaded.", '
    b'"errors": [{"domain": "global", "reason": "cannotDownloadAbusiveFile", '
    b'"message": "This file is flagged as abusive."}]}}'
)


class _FakeResp:
    """最小 httpx 流式响应替身。"""

    def __init__(self, status_code=403, body=b""):
        self.status_code = status_code
        self._body = body
        self.closed = False

    async def aread(self):
        return self._body

    def read(self):
        return self._body

    async def aclose(self):
        self.closed = True

    def close(self):
        self.closed = True


# ---------- _drive_403_error_reason ----------

def test_reason_parses_ratelimit():
    reason, message = streaming._drive_403_error_reason(_ERR_RATELIMIT)
    assert reason == "userRateLimitExceeded"
    assert "quota" in message


def test_reason_parses_abusive():
    reason, _ = streaming._drive_403_error_reason(_ERR_ABUSIVE)
    assert reason == "cannotDownloadAbusiveFile"


def test_reason_empty_body():
    assert streaming._drive_403_error_reason(b"") == ("", "")


def test_reason_invalid_json():
    assert streaming._drive_403_error_reason(b"<html>not json") == ("", "")


def test_reason_missing_fields():
    assert streaming._drive_403_error_reason(b'{"error": {}}') == ("", "")
    assert streaming._drive_403_error_reason(b'{"x": 1}') == ("", "")


def test_reason_message_truncated():
    long_msg = "m" * 500
    body = ('{"error": {"message": "%s", "errors": [{"reason": "rateLimitExceeded"}]}}'
            % long_msg).encode()
    reason, message = streaming._drive_403_error_reason(body)
    assert reason == "rateLimitExceeded"
    assert len(message) == 200


def test_reason_non_string_coerced():
    body = b'{"error": {"message": 123, "errors": [{"reason": 456}]}}'
    reason, message = streaming._drive_403_error_reason(body)
    assert reason == "456"
    assert message == "123"


# ---------- drop_sa_affinity ----------

def test_drop_affinity_bad_keys():
    assert drive_auth.drop_sa_affinity(None) is False
    assert drive_auth.drop_sa_affinity("") is False
    assert drive_auth.drop_sa_affinity(123) is False


def test_drop_affinity_roundtrip(monkeypatch):
    monkeypatch.setattr(drive_auth, "_sa_affinity", {})
    drive_auth._sa_affinity["k1"] = ({"client_email": "a@x"}, 999999.0)
    assert drive_auth.drop_sa_affinity("k1") is True
    assert "k1" not in drive_auth._sa_affinity
    # 不存在的 key 也不报错
    assert drive_auth.drop_sa_affinity("k1") is True


# ---------- _drive_403_retry_async ----------

def _patch_common(monkeypatch, **kwargs):
    monkeypatch.setenv("DRIVE_403_SA_RETRY_MAX", kwargs.get("max_retry", "2"))
    monkeypatch.setenv("DRIVE_403_SA_RETRY_JITTER_MS", "1")


def test_async_retry_ratelimit_switches_sa(monkeypatch):
    _patch_common(monkeypatch)
    calls = {}

    def _fake_drop(key):
        calls["drop"] = key
        return True

    async def _fake_follow(client, url, fwd, silent=False):
        calls["auth"] = fwd.get("Authorization")
        return _FakeResp(206, b"data"), url, 0

    monkeypatch.setattr("backend.emby_server.drive_auth.drop_sa_affinity", _fake_drop)
    monkeypatch.setattr(streaming, "_follow_redirects", _fake_follow)
    monkeypatch.setattr(streaming, "_fresh_sa_token", lambda old, url: "NEWTOKEN")
    monkeypatch.setattr(streaming, "_old_auth_token", lambda fwd: "Bearer OLDTOKEN")

    resp = _FakeResp(403, _ERR_RATELIMIT)
    out_resp, out_current = asyncio.run(
        streaming._drive_403_retry_async(None, API_URL, {"Authorization": "Bearer OLDTOKEN"},
                                        resp, API_URL, UC_URL))
    assert out_resp.status_code == 206
    assert out_current == API_URL
    # 打破的是转换前原始 URL 的亲和，不是 api_url
    assert calls["drop"] == UC_URL
    assert calls["auth"] == "Bearer NEWTOKEN"
    assert resp.closed  # 旧 403 响应已关闭归还连接池


def test_async_no_retry_on_abusive(monkeypatch):
    _patch_common(monkeypatch)
    followed = []

    async def _fake_follow(client, url, fwd, silent=False):
        followed.append(url)
        return _FakeResp(206, b"data"), url, 0

    monkeypatch.setattr(streaming, "_follow_redirects", _fake_follow)
    resp = _FakeResp(403, _ERR_ABUSIVE)
    out_resp, _ = asyncio.run(
        streaming._drive_403_retry_async(None, API_URL, {}, resp, API_URL, UC_URL))
    assert out_resp is resp
    assert followed == []  # 不可重试：一次也不重放


def test_async_respects_max_retry_zero(monkeypatch):
    _patch_common(monkeypatch, max_retry="0")
    followed = []

    async def _fake_follow(client, url, fwd, silent=False):
        followed.append(url)
        return _FakeResp(403, _ERR_RATELIMIT), url, 0

    monkeypatch.setattr(streaming, "_follow_redirects", _fake_follow)
    resp = _FakeResp(403, _ERR_RATELIMIT)
    out_resp, _ = asyncio.run(
        streaming._drive_403_retry_async(None, API_URL, {}, resp, API_URL, UC_URL))
    assert out_resp.status_code == 403
    assert followed == []


def test_async_never_raises(monkeypatch):
    _patch_common(monkeypatch)

    async def _boom(*a, **k):
        raise RuntimeError("boom")

    monkeypatch.setattr(streaming, "_follow_redirects", _boom)
    monkeypatch.setattr(streaming, "_fresh_sa_token", lambda old, url: "T")
    resp = _FakeResp(403, _ERR_RATELIMIT)
    out_resp, out_current = asyncio.run(
        streaming._drive_403_retry_async(None, API_URL, {}, resp, API_URL, UC_URL))
    assert out_resp.status_code == 403
    assert out_current == API_URL


# ---------- _drive_403_retry_sync ----------

def test_sync_retry_ratelimit_switches_sa(monkeypatch):
    _patch_common(monkeypatch)
    calls = {}

    def _fake_drop(key):
        calls["drop"] = key
        return True

    def _fake_open(client, url, fwd, dns_pinning, silent=False):
        calls["auth"] = fwd.get("Authorization")
        return _FakeResp(206, b"data"), url, fwd

    monkeypatch.setattr("backend.emby_server.drive_auth.drop_sa_affinity", _fake_drop)
    monkeypatch.setattr(streaming, "_open_upstream_sync", _fake_open)
    monkeypatch.setattr(streaming, "_fresh_sa_token", lambda old, url: "NEWTOKEN2")
    monkeypatch.setattr(streaming, "_old_auth_token", lambda fwd: "Bearer OLDTOKEN")

    resp = _FakeResp(403, _ERR_RATELIMIT)
    out_resp, out_current, out_forward = streaming._drive_403_retry_sync(
        None, API_URL, {"Authorization": "Bearer OLDTOKEN"}, resp, API_URL, UC_URL, {})
    assert out_resp.status_code == 206
    assert out_forward.get("Authorization") == "Bearer NEWTOKEN2"
    assert calls["drop"] == UC_URL
    assert resp.closed


def test_sync_no_retry_on_abusive(monkeypatch):
    _patch_common(monkeypatch)
    opened = []

    def _fake_open(client, url, fwd, dns_pinning, silent=False):
        opened.append(url)
        return _FakeResp(206, b"data"), url, fwd

    monkeypatch.setattr(streaming, "_open_upstream_sync", _fake_open)
    resp = _FakeResp(403, _ERR_ABUSIVE)
    out = streaming._drive_403_retry_sync(
        None, API_URL, {}, resp, API_URL, UC_URL, {})
    assert out[0] is resp
    assert opened == []


def test_sync_never_raises(monkeypatch):
    _patch_common(monkeypatch)

    def _boom(*a, **k):
        raise RuntimeError("boom")

    monkeypatch.setattr(streaming, "_open_upstream_sync", _boom)
    monkeypatch.setattr(streaming, "_fresh_sa_token", lambda old, url: "T")
    resp = _FakeResp(403, _ERR_RATELIMIT)
    out_resp, out_current, out_forward = streaming._drive_403_retry_sync(
        None, API_URL, {"A": "b"}, resp, API_URL, UC_URL, {})
    assert out_resp.status_code == 403
    assert out_forward == {"A": "b"}
