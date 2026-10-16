# -*- coding: utf-8 -*-
"""播放读法回归测试（2026-10-16；2026-10-17 中继全删后精简）。

保留覆盖（均为保留功能）：
- drive_auth.drive_usercontent_download_url：302 直链 URL 形状（红线保留）。
- scanner.probe_metadata：远程探测第三跳（整文件兜底）对已知大文件跳过。
- probe_worker.maybe_enqueue：配额退避中的条目不许按需插队清退避。

已删除（中继全删）：配额负缓存/救援、serve_remote Accept-Ranges 相关用例。

全部离线：不做真实网络/DNS/Drive 请求。
"""
import os

os.environ.setdefault("DATABASE_TYPE", "sqlite")
os.environ.setdefault("REDIS_ENABLED", "false")

import uuid
from datetime import datetime, timedelta

from backend.emby_server import scanner


def test_usercontent_download_url_shape():
    from backend.emby_server import drive_auth
    url = drive_auth.drive_usercontent_download_url("FID123")
    assert url.startswith("https://drive.usercontent.google.com/download?")
    assert "id=FID123" in url and "export=download" in url
    assert drive_auth.drive_usercontent_download_url("") == ""


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
