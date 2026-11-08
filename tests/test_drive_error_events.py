# -*- coding: utf-8 -*-
"""Drive API 报错原因落库（drive_error_events）测试。

背景：谷歌 Drive 报错的 reason（userRateLimitExceeded /
downloadQuotaExceeded 等）此前全站只解析不落库，撞限时分不清
是哪种限额。本组测试钉住：
- 中央出口（drive_changes._api_get）收到带 reason 的 403 时
  落一行 drive_api_error_events，字段齐全；
- 账号标识只存不可逆短哈希，token 原文不落库；
- 落库失败绝不影响主请求原有行为；
- 保留期清理只删过期行；
- 聚合查询按 reason/天计数。
"""
import os

os.environ.setdefault("DATABASE_TYPE", "sqlite")
os.environ.setdefault("REDIS_ENABLED", "false")

import hashlib
from datetime import datetime, timedelta
from unittest import mock

import httpx
import pytest

from backend.database import SessionLocal, init_db
from backend.emby_server import drive_changes
from backend.emby_server import drive_error_events
from backend.emby_server import models as em

init_db()

_QUOTA_BODY = {
    "error": {
        "errors": [{
            "domain": "usageLimits",
            "reason": "userRateLimitExceeded",
            "message": "User Rate Limit Exceeded",
        }],
        "code": 403,
        "message": "User Rate Limit Exceeded",
    }
}


def _fake_resp(status_code, body=None):
    r = mock.Mock()
    r.status_code = status_code
    r.headers = {}
    if body is None:
        r.json.side_effect = ValueError("no json")
        r.text = ""
    else:
        r.json.return_value = body
        r.text = str(body)
    return r


def _clear_events():
    db = SessionLocal()
    try:
        db.query(em.DriveApiErrorEvent).delete(synchronize_session=False)
        db.commit()
    finally:
        db.close()


def _all_events():
    db = SessionLocal()
    try:
        return db.query(em.DriveApiErrorEvent).order_by(
            em.DriveApiErrorEvent.id).all()
    finally:
        db.close()


@pytest.fixture(autouse=True)
def _clean():
    _clear_events()
    yield
    _clear_events()


# ---------------------------------------------------------------------------
# 中央出口 _api_get：403 带 reason → 落一行
# ---------------------------------------------------------------------------

def test_api_get_records_403_reason(monkeypatch):
    monkeypatch.setattr(drive_changes, "API_MAX_RETRIES", 0)
    with mock.patch.object(drive_changes.httpx, "get",
                           return_value=_fake_resp(403, _QUOTA_BODY)):
        with pytest.raises(httpx.HTTPStatusError):
            drive_changes._api_get("http://x", "tok-secret-123", {})
    rows = _all_events()
    assert len(rows) == 1
    row = rows[0]
    assert row.status_code == 403
    assert row.reason == "userRateLimitExceeded"
    assert row.domain == "usageLimits"
    assert row.caller  # 调用方标签非空
    # 账号标识 = token 的 sha256 短哈希，原文绝不出现
    expect = hashlib.sha256(b"tok-secret-123").hexdigest()[:12]
    assert row.account_hash == expect
    assert "tok-secret-123" not in (row.reason + row.domain + row.caller)


def test_api_get_records_429(monkeypatch):
    monkeypatch.setattr(drive_changes, "API_MAX_RETRIES", 0)
    body = {"error": {"errors": [{
        "domain": "usageLimits", "reason": "rateLimitExceeded",
        "message": "Rate Limit Exceeded"}], "code": 429}}
    with mock.patch.object(drive_changes.httpx, "get",
                           return_value=_fake_resp(429, body)):
        with pytest.raises(httpx.HTTPStatusError):
            drive_changes._api_get("http://x", "tok", {})
    rows = _all_events()
    assert len(rows) == 1
    assert rows[0].status_code == 429
    assert rows[0].reason == "rateLimitExceeded"


def test_api_get_success_records_nothing(monkeypatch):
    ok = _fake_resp(200, {"ok": True})
    with mock.patch.object(drive_changes.httpx, "get", return_value=ok):
        assert drive_changes._api_get("http://x", "tok", {}) == {"ok": True}
    assert _all_events() == []


# ---------------------------------------------------------------------------
# 落库失败不影响主请求
# ---------------------------------------------------------------------------

def test_record_failure_does_not_break_main_request(monkeypatch):
    monkeypatch.setattr(drive_changes, "API_MAX_RETRIES", 0)

    def _boom():
        raise RuntimeError("db down")

    monkeypatch.setattr(drive_error_events, "SessionLocal", _boom)
    before = drive_error_events.error_stats()["write_errors"]
    with mock.patch.object(drive_changes.httpx, "get",
                           return_value=_fake_resp(403, _QUOTA_BODY)):
        # 主请求行为不变：配额 403 重试耗尽后仍抛 HTTPStatusError，
        # 不因落库失败多抛别的异常、也不被吞掉
        with pytest.raises(httpx.HTTPStatusError):
            drive_changes._api_get("http://x", "tok", {})
    after = drive_error_events.error_stats()
    assert after["write_errors"] > before


# ---------------------------------------------------------------------------
# 转存客户端中央出口 _request：403 → 落一行且原样返回响应
# ---------------------------------------------------------------------------

def test_transfer_client_records_403(monkeypatch):
    from backend.emby_server import transfer_variant

    monkeypatch.setattr(transfer_variant, "get_token_for_drive",
                        lambda drive_id, force_refresh=False: "tok-t")
    client = transfer_variant.DriveApiClient()
    with mock.patch.object(transfer_variant.httpx, "request",
                           return_value=_fake_resp(403, _QUOTA_BODY)) as req:
        resp = client._request("GET", "http://x", "DRIVE1")
    assert resp.status_code == 403  # 主行为不变：响应原样返回
    assert req.called
    rows = _all_events()
    assert len(rows) == 1
    assert rows[0].reason == "userRateLimitExceeded"
    assert rows[0].account_hash == hashlib.sha256(
        b"DRIVE1").hexdigest()[:12]


# ---------------------------------------------------------------------------
# 保留期清理：只删过期行
# ---------------------------------------------------------------------------

def test_purge_only_expired():
    db = SessionLocal()
    try:
        db.add(em.DriveApiErrorEvent(
            status_code=403, reason="oldReason", domain="",
            caller="test", account_hash="",
            created_at=datetime.now() - timedelta(days=20)))
        db.add(em.DriveApiErrorEvent(
            status_code=429, reason="freshReason", domain="",
            caller="test", account_hash="",
            created_at=datetime.now() - timedelta(days=1)))
        db.commit()
        # 无配置行 → 默认保留 14 天
        deleted = drive_error_events.purge_expired_events(db)
    finally:
        db.close()
    assert deleted == 1
    rows = _all_events()
    assert len(rows) == 1
    assert rows[0].reason == "freshReason"


# ---------------------------------------------------------------------------
# 聚合查询：按 reason / 天计数
# ---------------------------------------------------------------------------

def test_error_summary_groups_by_reason():
    drive_error_events.note_drive_error(
        status_code=403, reason="userRateLimitExceeded",
        domain="usageLimits", caller="test", account="a")
    drive_error_events.note_drive_error(
        status_code=403, reason="userRateLimitExceeded",
        domain="usageLimits", caller="test", account="b")
    drive_error_events.note_drive_error(
        status_code=429, reason="rateLimitExceeded",
        domain="usageLimits", caller="test", account="a")
    summary = drive_error_events.error_summary(SessionLocal(), days=7)
    assert summary["total"] == 3
    by_reason = {r["reason"]: r["count"] for r in summary["by_reason"]}
    assert by_reason == {"userRateLimitExceeded": 2,
                         "rateLimitExceeded": 1}
    days = {(d["day"], d["reason"]): d["count"]
            for d in summary["by_day_reason"]}
    today = datetime.now().strftime("%Y-%m-%d")
    assert days[(today, "userRateLimitExceeded")] == 2
    assert days[(today, "rateLimitExceeded")] == 1
