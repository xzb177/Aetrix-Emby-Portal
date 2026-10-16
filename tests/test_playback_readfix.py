# -*- coding: utf-8 -*-
"""播放读法修复回归测试（2026-10-16）。

覆盖三处：
- streaming：downloadQuotaExceeded 是文件级配额，换 SA 无用 → 不换 SA 重试；
  先走零空间救援（换 usercontent 入口 / 短冷却重试原入口），全失败才记入
  跨请求负缓存；负缓存的记/查/过期/禁用语义。
- streaming：远程代理响应在上游没给 Accept-Ranges 时也要补上（本地路径
  一直有），否则播放器以为不可分段、大文件只能顺序硬下。
- scanner.probe_metadata：远程探测第三跳（整文件兜底）对已知大文件跳过。
- probe_worker.maybe_enqueue：配额退避中的条目不许按需插队清退避。

全部离线：不做真实网络/DNS/Drive 请求。
"""
import os

os.environ.setdefault("DATABASE_TYPE", "sqlite")
os.environ.setdefault("REDIS_ENABLED", "false")

import asyncio
import uuid
from datetime import datetime, timedelta
from types import SimpleNamespace

from backend.emby_server import streaming
from backend.emby_server import scanner

API_URL = "https://www.googleapis.com/drive/v3/files/FID123?alt=media"
UC_URL = "https://drive.google.com/uc?export=download&id=FID123&confirm=t"

_ERR_QUOTA = (
    b'{"error": {"code": 403, "message": "The download quota for this file has been exceeded.", '
    b'"errors": [{"domain": "usageLimits", "reason": "downloadQuotaExceeded", '
    b'"message": "The download quota for this file has been exceeded."}]}}'
)
_ERR_RATELIMIT = (
    b'{"error": {"code": 403, "message": "The user has exceeded their Drive API quota.", '
    b'"errors": [{"domain": "usageLimits", "reason": "userRateLimitExceeded", '
    b'"message": "User rate limit exceeded."}]}}'
)


class _FakeResp:
    """最小 httpx 流式响应替身（与 test_drive_403_sa_retry 同口径）。"""

    def __init__(self, status_code=403, body=b"", headers=None):
        self.status_code = status_code
        self._body = body
        self.headers = headers or {}
        self.closed = False

    async def aread(self):
        return self._body

    def read(self):
        return self._body

    async def aclose(self):
        self.closed = True

    def close(self):
        self.closed = True

    def iter_bytes(self, chunk_size=65536):
        yield self._body


def _fid():
    return "FID" + uuid.uuid4().hex[:16]


# ---------- 配额：不可重试 + 负缓存 ----------

def test_quota_reason_classification():
    # 文件级配额不可重试；SA 级限流保持可重试（换 SA 有效，#915 实测）
    assert "downloadQuotaExceeded" in streaming._DRIVE_403_NON_RETRYABLE
    assert "userRateLimitExceeded" not in streaming._DRIVE_403_NON_RETRYABLE
    assert "rateLimitExceeded" not in streaming._DRIVE_403_NON_RETRYABLE


def test_negative_cache_roundtrip(monkeypatch):
    monkeypatch.delenv("DRIVE_QUOTA_NEGATIVE_CACHE_SEC", raising=False)
    fid = _fid()
    assert streaming.drive_quota_blocked_retry_after(fid) == 0
    streaming.note_drive_quota_exceeded(fid)
    assert streaming.drive_quota_blocked_retry_after(fid) > 0


def test_negative_cache_disabled_by_env(monkeypatch):
    monkeypatch.setenv("DRIVE_QUOTA_NEGATIVE_CACHE_SEC", "0")
    fid = _fid()
    streaming.note_drive_quota_exceeded(fid)
    assert streaming.drive_quota_blocked_retry_after(fid) == 0


def _uc_url():
    from backend.emby_server import drive_auth
    return drive_auth.drive_usercontent_download_url("FID123")


def test_usercontent_download_url_shape():
    from backend.emby_server import drive_auth
    url = drive_auth.drive_usercontent_download_url("FID123")
    assert url.startswith("https://drive.usercontent.google.com/download?")
    assert "id=FID123" in url and "export=download" in url
    assert drive_auth.drive_usercontent_download_url("") == ""


def test_async_quota_rescue_entry_success(monkeypatch):
    # 配额 403 → 先换入口：备用入口拿到真媒体就不记负缓存、不回 403
    monkeypatch.setenv("DRIVE_403_SA_RETRY_MAX", "2")
    monkeypatch.setenv("DRIVE_403_SA_RETRY_JITTER_MS", "1")
    monkeypatch.setenv("DRIVE_QUOTA_RESCUE_WAIT_SEC", "0")
    monkeypatch.delenv("DRIVE_QUOTA_NEGATIVE_CACHE_SEC", raising=False)
    streaming._QUOTA_NEGATIVE_CACHE.pop("FID123", None)
    followed = []

    async def _fake_follow(client, url, fwd, silent=False):
        followed.append(url)
        return _FakeResp(206, b"data", {"content-type": "video/x-matroska"}), url, 0

    monkeypatch.setattr(streaming, "_follow_redirects", _fake_follow)
    before = streaming.quota_rescue_counters()
    resp = _FakeResp(403, _ERR_QUOTA)
    out_resp, out_current = asyncio.run(
        streaming._drive_403_retry_async(None, API_URL, {}, resp, API_URL, UC_URL))
    assert followed == [_uc_url()]  # 只试了备用入口，没换 SA、没等冷却
    assert out_resp.status_code == 206 and out_resp is not resp
    assert out_current == _uc_url()
    assert streaming.drive_quota_blocked_retry_after("FID123") == 0
    after = streaming.quota_rescue_counters()
    assert after["entry_attempt"] == before["entry_attempt"] + 1
    assert after["entry_success"] == before["entry_success"] + 1


def test_async_quota_rescue_all_fail_negative_cached(monkeypatch):
    # 换入口也 403、冷却关闭 → 回到旧口径：记负缓存、原样回 403、不换 SA
    monkeypatch.setenv("DRIVE_403_SA_RETRY_MAX", "2")
    monkeypatch.setenv("DRIVE_403_SA_RETRY_JITTER_MS", "1")
    monkeypatch.setenv("DRIVE_QUOTA_RESCUE_WAIT_SEC", "0")
    monkeypatch.delenv("DRIVE_QUOTA_NEGATIVE_CACHE_SEC", raising=False)
    streaming._QUOTA_NEGATIVE_CACHE.pop("FID123", None)
    followed = []

    async def _fake_follow(client, url, fwd, silent=False):
        followed.append(url)
        return _FakeResp(403, _ERR_QUOTA), url, 0

    monkeypatch.setattr(streaming, "_follow_redirects", _fake_follow)
    resp = _FakeResp(403, _ERR_QUOTA)
    out_resp, _ = asyncio.run(
        streaming._drive_403_retry_async(None, API_URL, {}, resp, API_URL, UC_URL))
    assert out_resp is resp
    assert followed == [_uc_url()]  # 只有备用入口一次：没有 SA 重放
    # 已记入负缓存：后续同文件请求会被入口护盾本地拒绝
    assert streaming.drive_quota_blocked_retry_after("FID123") > 0
    streaming._QUOTA_NEGATIVE_CACHE.pop("FID123", None)


def test_async_quota_rescue_confirm_page_rejected(monkeypatch):
    # 备用入口回「病毒扫描确认页」（200 text/html）不算成功，不许交付播放器
    monkeypatch.setenv("DRIVE_QUOTA_RESCUE_WAIT_SEC", "0")
    monkeypatch.delenv("DRIVE_QUOTA_NEGATIVE_CACHE_SEC", raising=False)
    streaming._QUOTA_NEGATIVE_CACHE.pop("FID123", None)

    async def _fake_follow(client, url, fwd, silent=False):
        return (_FakeResp(200, b"<html>scan confirm</html>",
                          {"content-type": "text/html; charset=utf-8"}), url, 0)

    monkeypatch.setattr(streaming, "_follow_redirects", _fake_follow)
    resp = _FakeResp(403, _ERR_QUOTA)
    out_resp, _ = asyncio.run(
        streaming._drive_403_retry_async(None, API_URL, {}, resp, API_URL, UC_URL))
    assert out_resp is resp
    assert streaming.drive_quota_blocked_retry_after("FID123") > 0
    streaming._QUOTA_NEGATIVE_CACHE.pop("FID123", None)


def test_async_quota_rescue_wait_retry_success(monkeypatch):
    # 换入口关、短冷却开：等一小会儿后原入口恢复 → 直接拿到媒体，不记负缓存
    monkeypatch.setenv("DRIVE_QUOTA_RESCUE_ENTRY", "0")
    monkeypatch.setenv("DRIVE_QUOTA_RESCUE_WAIT_SEC", "0.01")
    monkeypatch.delenv("DRIVE_QUOTA_NEGATIVE_CACHE_SEC", raising=False)
    streaming._QUOTA_NEGATIVE_CACHE.pop("FID123", None)
    followed = []

    async def _fake_follow(client, url, fwd, silent=False):
        followed.append(url)
        return _FakeResp(206, b"data", {"content-type": "video/mp4"}), url, 0

    monkeypatch.setattr(streaming, "_follow_redirects", _fake_follow)
    before = streaming.quota_rescue_counters()
    resp = _FakeResp(403, _ERR_QUOTA)
    out_resp, _ = asyncio.run(
        streaming._drive_403_retry_async(None, API_URL, {}, resp, API_URL, UC_URL))
    assert followed == [API_URL]  # 等待后试的是原入口（不换 SA 不换入口）
    assert out_resp.status_code == 206 and out_resp is not resp
    assert streaming.drive_quota_blocked_retry_after("FID123") == 0
    after = streaming.quota_rescue_counters()
    assert after["wait_attempt"] == before["wait_attempt"] + 1
    assert after["wait_success"] == before["wait_success"] + 1


def test_sync_quota_rescue_entry_success(monkeypatch):
    monkeypatch.setenv("DRIVE_QUOTA_RESCUE_WAIT_SEC", "0")
    monkeypatch.delenv("DRIVE_QUOTA_NEGATIVE_CACHE_SEC", raising=False)
    streaming._QUOTA_NEGATIVE_CACHE.pop("FID123", None)
    opened = []

    def _fake_open(client, url, fwd, pinning, silent=False):
        opened.append(url)
        return _FakeResp(206, b"data", {"content-type": "video/x-matroska"}), url, fwd

    monkeypatch.setattr(streaming, "_open_upstream_sync", _fake_open)
    resp = _FakeResp(403, _ERR_QUOTA)
    out_resp, out_current, _ = streaming._drive_403_retry_sync(
        None, API_URL, {}, resp, API_URL, UC_URL, {})
    assert opened == [_uc_url()]
    assert out_resp.status_code == 206 and out_resp is not resp
    assert out_current == _uc_url()
    assert streaming.drive_quota_blocked_retry_after("FID123") == 0


def test_sync_quota_rescue_all_fail_negative_cached(monkeypatch):
    monkeypatch.setenv("DRIVE_403_SA_RETRY_MAX", "2")
    monkeypatch.setenv("DRIVE_403_SA_RETRY_JITTER_MS", "1")
    monkeypatch.setenv("DRIVE_QUOTA_RESCUE_WAIT_SEC", "0")
    opened = []

    def _fake_open(client, url, fwd, pinning, silent=False):
        opened.append(url)
        return _FakeResp(403, _ERR_QUOTA), url, fwd

    monkeypatch.setattr(streaming, "_open_upstream_sync", _fake_open)
    resp = _FakeResp(403, _ERR_QUOTA)
    out_resp, _, _ = streaming._drive_403_retry_sync(
        None, API_URL, {}, resp, API_URL, UC_URL, {})
    assert out_resp is resp
    assert opened == [_uc_url()]
    # 同步路径与 async 版同口径：救援全失败才把文件记入跨请求负缓存。
    # 负缓存是进程级全局状态，验完必须清掉——不清会串进后续测试文件
    # （CI 实测：本条残留令 test_playback_strategy 的直链用例恒回落中继）。
    assert streaming.drive_quota_blocked_retry_after("FID123") > 0
    streaming._QUOTA_NEGATIVE_CACHE.pop("FID123", None)


# ---------- Accept-Ranges 补齐（同步 serve_remote） ----------

def test_serve_remote_adds_accept_ranges(monkeypatch):
    upstream = _FakeResp(200, b"payload",
                         headers={"content-length": "7", "content-type": "video/mp4"})

    def _fake_open(client, url, fwd, pinning, silent=False):
        return upstream, url, fwd

    monkeypatch.setattr(streaming, "_open_upstream_sync", _fake_open)
    resp = streaming.serve_remote("https://example.com/v.mp4",
                                  SimpleNamespace(headers={}))
    assert resp.status_code == 200
    assert resp.headers.get("accept-ranges") == "bytes"


def test_serve_remote_keeps_upstream_accept_ranges(monkeypatch):
    upstream = _FakeResp(206, b"payload",
                         headers={"content-range": "bytes 0-6/100",
                                  "accept-ranges": "bytes",
                                  "content-length": "7"})

    def _fake_open(client, url, fwd, pinning, silent=False):
        return upstream, url, fwd

    monkeypatch.setattr(streaming, "_open_upstream_sync", _fake_open)
    resp = streaming.serve_remote(
        "https://example.com/v.mp4",
        SimpleNamespace(headers={"range": "bytes=0-6"}))
    assert resp.status_code == 206
    assert resp.headers.get("content-range") == "bytes 0-6/100"
    assert resp.headers.get("accept-ranges") == "bytes"


# ---------- probe_metadata 第三跳流量闸 ----------

def _patch_probe(monkeypatch):
    calls = []

    def fake_ffprobe(path, headers=None, size=0, ranged=True, local_path=""):
        calls.append({"ranged": ranged})
        return {}

    monkeypatch.setattr(scanner, "_ffprobe", fake_ffprobe)
    monkeypatch.setattr(scanner, "_dual_range_probe", lambda *a, **k: None)
    monkeypatch.setattr(scanner, "_mediainfo", lambda *a, **k: None)
    monkeypatch.setattr(scanner, "_last_ffprobe_timed_out", lambda: False)
    monkeypatch.setattr(scanner, "PROBE_WHOLE_FILE_MAX_BYTES", 1 << 30)
    return calls


def test_probe_skips_whole_file_hop_for_big_remote(monkeypatch):
    calls = _patch_probe(monkeypatch)
    info = scanner.probe_metadata("https://example.com/big.mp4",
                                  size=2 << 30, container="mp4")
    assert info.get("_degraded") is True
    # 只许有限 Range 那一跳，整文件兜底（ranged=False）不许发生
    assert calls and all(c["ranged"] for c in calls)


def test_probe_keeps_whole_file_hop_for_small_remote(monkeypatch):
    calls = _patch_probe(monkeypatch)
    scanner.probe_metadata("https://example.com/small.mp4",
                           size=100 << 20, container="mp4")
    assert any(c["ranged"] is False for c in calls)


# ---------- maybe_enqueue：配额退避不许插队 ----------

from backend.database import SessionLocal, init_db  # noqa: E402
from backend.emby_server import models as em  # noqa: E402
from backend.emby_server import probe_worker  # noqa: E402

init_db()


def _guid():
    return uuid.uuid4().hex


def _make_pending_item(db, last_error):
    lib = em.Library(guid=_guid(), name=f"prf_{_guid()[:8]}",
                     collection_type="movies", paths="/tmp/prf_test")
    db.add(lib)
    db.commit()
    db.refresh(lib)
    item = em.MediaItem(
        guid=_guid(), library_id=lib.id, name=f"item_{_guid()[:8]}",
        item_type="movie", file_path="/tmp/prf_test/a.mkv",
        video_codec=None, probe_status="pending", probe_attempts=0,
        probe_priority=100,
        probe_next_retry_at=datetime.now() + timedelta(hours=1),
        probe_last_error=last_error,
        date_added=datetime.now(),
    )
    db.add(item)
    db.commit()
    db.refresh(item)
    return lib, item


def _cleanup(db, lib):
    db.query(em.MediaItem).filter(em.MediaItem.library_id == lib.id) \
        .delete(synchronize_session=False)
    db.query(em.Library).filter(em.Library.id == lib.id).delete()
    db.commit()


def test_maybe_enqueue_respects_quota_backoff():
    db = SessionLocal()
    lib, item = _make_pending_item(
        db, "[quota] 远端配额耗尽（HTTP 403）（约24h后重试）")
    try:
        assert probe_worker.maybe_enqueue(db, item) is False
        db.refresh(item)
        assert item.probe_priority == 100  # 没被提到 1000
        assert item.probe_next_retry_at is not None  # 退避没被清掉
    finally:
        _cleanup(db, lib)
        db.close()


def test_maybe_enqueue_still_bumps_non_quota_backoff():
    db = SessionLocal()
    lib, item = _make_pending_item(db, "[http] 远端返回 500")
    try:
        assert probe_worker.maybe_enqueue(db, item) is True
        db.refresh(item)
        assert item.probe_priority == probe_worker.ONDEMAND_PRIORITY
        assert item.probe_next_retry_at is None
    finally:
        _cleanup(db, lib)
        db.close()
