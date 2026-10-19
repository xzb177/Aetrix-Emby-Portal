# -*- coding: utf-8 -*-
"""Drive 链接分享自动补开（2026-10-19 播放事故治本）。

病根：播放 302 到 usercontent 匿名直链，要求文件有 anyone reader 权限；
2026-10-17 批量开分享后新入库文件无自动环节，静默播不了。本文件覆盖：
- ensure_shared 的四种结局映射（ok/already/rate_limited/failed），Drive API 全 mock
- note_new_file 入队语义（开关/新条目/file_id 三门）
- drive_share_patrol 的选行、标记、限流收手

全部离线：不做真实网络与 Drive 请求。
"""
import os
import tempfile

os.environ.setdefault("DATABASE_TYPE", "sqlite")
os.environ.setdefault("REDIS_ENABLED", "false")
_fd, _tmppath = tempfile.mkstemp(suffix=".db")
os.close(_fd)
os.environ["DATABASE_URL"] = f"sqlite:///{_tmppath}"

from backend import database as _dbmod
from sqlalchemy import create_engine as _ce
from sqlalchemy.orm import sessionmaker as _sm

_dbmod.engine = _ce(os.environ["DATABASE_URL"])
_dbmod.configure_session_local(_sm(bind=_dbmod.engine))

import uuid
from datetime import datetime, timedelta

import pytest

from backend.database import SessionLocal, init_db
from backend.emby_server import models as em
from backend.emby_server import drive_share as dsh
from backend.emby_server import drive_auth

init_db()


class _FakeResp:
    def __init__(self, status_code, payload=None):
        self.status_code = status_code
        self._payload = payload or {}

    def json(self):
        return self._payload


def _post_ok(url, headers=None, json=None, timeout=None):
    return _FakeResp(200, {"id": "perm1"})


def _post_dup(url, headers=None, json=None, timeout=None):
    return _FakeResp(409, {"error": {"errors": [{"reason": "duplicate"}]}})


def _post_rate(url, headers=None, json=None, timeout=None):
    return _FakeResp(403, {"error": {"errors": [
        {"reason": "rateLimitExceeded", "message": "slow down"}]}})


def _post_429(url, headers=None, json=None, timeout=None):
    return _FakeResp(429, {"error": {"errors": [
        {"reason": "userRateLimitExceeded"}]}})


def _post_forbidden(url, headers=None, json=None, timeout=None):
    return _FakeResp(403, {"error": {"errors": [
        {"reason": "insufficientPermissions"}]}})


def _post_boom(url, headers=None, json=None, timeout=None):
    raise RuntimeError("network down")


@pytest.fixture()
def db():
    s = SessionLocal()
    try:
        s.query(em.MediaStream).delete()
        s.query(em.MediaItem).delete()
        s.commit()
        yield s
    finally:
        s.query(em.MediaStream).delete()
        s.query(em.MediaItem).delete()
        s.commit()
        s.close()


@pytest.fixture()
def share_state(monkeypatch):
    """隔离 drive_share 的进程级队列/线程状态；抖动与退避归零。"""
    monkeypatch.setattr(dsh, "_JITTER_MIN_SEC", 0.0)
    monkeypatch.setattr(dsh, "_JITTER_MAX_SEC", 0.0)
    monkeypatch.setattr(dsh, "_BACKOFF_MIN_SEC", 0.0)
    monkeypatch.setattr(dsh, "_BACKOFF_MAX_SEC", 0.0)
    monkeypatch.setattr(dsh, "_http_post", _post_ok)
    monkeypatch.setattr(dsh, "DRIVE_SHARE_AUTO_ENABLED", True)
    monkeypatch.setattr(drive_auth, "get_drive_bearer_token",
                        lambda *a, **k: "tok")
    with dsh._lock:
        dsh._queue.clear()
        dsh._in_flight.clear()
        dsh._attempts.clear()
        dsh._backoff_until = 0.0
    yield
    with dsh._lock:
        dsh._queue.clear()
        dsh._in_flight.clear()
        dsh._attempts.clear()
        dsh._backoff_until = 0.0


def _item(db, fid=None, ok=None, checked_at=None, item_type="episode"):
    it = em.MediaItem(
        guid=uuid.uuid4().hex, library_id=1, item_type=item_type,
        name="t", file_path="/tmp/x.mp4", drive_file_id=fid,
        drive_share_ok=ok, drive_share_checked_at=checked_at)
    db.add(it)
    db.commit()
    return it


# --- ensure_shared 结局映射 ---

def test_ensure_shared_ok(monkeypatch):
    monkeypatch.setattr(dsh, "_http_post", _post_ok)
    assert dsh.ensure_shared("fid1", "tok") == "ok"


def test_ensure_shared_409_is_already(monkeypatch):
    monkeypatch.setattr(dsh, "_http_post", _post_dup)
    assert dsh.ensure_shared("fid1", "tok") == "already"


def test_ensure_shared_rate_limited_403(monkeypatch):
    monkeypatch.setattr(dsh, "_http_post", _post_rate)
    assert dsh.ensure_shared("fid1", "tok") == "rate_limited"


def test_ensure_shared_rate_limited_429(monkeypatch):
    monkeypatch.setattr(dsh, "_http_post", _post_429)
    assert dsh.ensure_shared("fid1", "tok") == "rate_limited"


def test_ensure_shared_other_403_is_failed(monkeypatch):
    # 非限流的 403（如无权分享）：失败，不当限流退避。
    monkeypatch.setattr(dsh, "_http_post", _post_forbidden)
    assert dsh.ensure_shared("fid1", "tok") == "failed"


def test_ensure_shared_exception_is_failed(monkeypatch):
    monkeypatch.setattr(dsh, "_http_post", _post_boom)
    assert dsh.ensure_shared("fid1", "tok") == "failed"


def test_ensure_shared_no_token_is_failed():
    assert dsh.ensure_shared("fid1", None) == "failed"
    assert dsh.ensure_shared("", "tok") == "failed"


def test_share_url_has_supports_all_drives():
    url = dsh._share_url("abc123")
    assert "drive/v3/files/abc123/permissions" in url
    assert "supportsAllDrives=true" in url


# --- note_new_file 入队语义 ---

def test_note_new_file_enqueues(share_state, monkeypatch):
    monkeypatch.setattr(dsh, "_process_one_share", lambda i, f: "ok")
    monkeypatch.setattr(dsh, "_ensure_worker_locked", lambda: None)
    assert dsh.note_new_file(11, "fid-new", True) is True
    with dsh._lock:
        assert (11, "fid-new") in dsh._queue


def test_note_new_file_dedups_in_flight(share_state, monkeypatch):
    monkeypatch.setattr(dsh, "_process_one_share", lambda i, f: "ok")
    monkeypatch.setattr(dsh, "_ensure_worker_locked", lambda: None)
    assert dsh.note_new_file(11, "fid-dup", True) is True
    assert dsh.note_new_file(12, "fid-dup", True) is False
    with dsh._lock:
        assert len(dsh._queue) == 1


def test_note_new_file_switch_off(share_state, monkeypatch):
    monkeypatch.setattr(dsh, "DRIVE_SHARE_AUTO_ENABLED", False)
    assert dsh.note_new_file(11, "fid-x", True) is False
    with dsh._lock:
        assert len(dsh._queue) == 0


def test_note_new_file_not_new_or_no_fid(share_state):
    assert dsh.note_new_file(11, "fid-x", False) is False
    assert dsh.note_new_file(11, "", True) is False
    assert dsh.note_new_file(None, "fid-x", True) is False


# --- patrol 选行与标记 ---

def test_patrol_confirms_unchecked_and_skips_rest(share_state, db,
                                                 monkeypatch):
    monkeypatch.setattr(dsh, "ensure_shared", lambda f, t: "ok")
    unchecked = _item(db, fid="fid-u1")
    no_fid = _item(db, fid=None)
    already_ok = _item(db, fid="fid-ok", ok=True,
                       checked_at=datetime.now())
    n = dsh.drive_share_patrol(db, limit=150)
    assert n == 1
    db.refresh(unchecked)
    db.refresh(no_fid)
    db.refresh(already_ok)
    assert unchecked.drive_share_ok is True
    assert unchecked.drive_share_checked_at is not None
    assert no_fid.drive_share_ok is None  # 无 file_id：不动
    assert already_ok.drive_share_checked_at is not None  # 已确认：不动


def test_patrol_retries_failed_after_24h(share_state, db, monkeypatch):
    monkeypatch.setattr(dsh, "ensure_shared", lambda f, t: "ok")
    old_fail = _item(db, fid="fid-f1", ok=False,
                     checked_at=datetime.now() - timedelta(hours=25))
    fresh_fail = _item(db, fid="fid-f2", ok=False,
                       checked_at=datetime.now() - timedelta(hours=1))
    n = dsh.drive_share_patrol(db, limit=150)
    assert n == 1
    db.refresh(old_fail)
    db.refresh(fresh_fail)
    assert old_fail.drive_share_ok is True
    assert fresh_fail.drive_share_ok is False  # 24h 内失败过：本轮不动


def test_patrol_rate_limited_stops_round(share_state, db, monkeypatch):
    monkeypatch.setattr(dsh, "ensure_shared", lambda f, t: "rate_limited")
    a = _item(db, fid="fid-r1")
    b = _item(db, fid="fid-r2")
    n = dsh.drive_share_patrol(db, limit=150)
    assert n == 0
    db.refresh(a)
    db.refresh(b)
    assert a.drive_share_ok is None  # 撞限流：本轮收手，一行不动
    assert b.drive_share_ok is None


def test_patrol_switch_off(share_state, db, monkeypatch):
    monkeypatch.setattr(dsh, "DRIVE_SHARE_AUTO_ENABLED", False)
    _item(db, fid="fid-s1")
    assert dsh.drive_share_patrol(db, limit=150) == 0


def test_patrol_only_creates_reader_permission(share_state, db, monkeypatch):
    seen = {}

    def fake_post(url, headers=None, json=None, timeout=None):
        seen["url"] = url
        seen["body"] = json
        return _FakeResp(200, {})

    monkeypatch.setattr(dsh, "_http_post", fake_post)
    # 走真实 ensure_shared（只桩掉 HTTP 层），确认只创 reader 权限
    _item(db, fid="fid-body")
    n = dsh.drive_share_patrol(db, limit=150)
    assert n == 1
    assert seen["body"] == {"type": "anyone", "role": "reader"}
