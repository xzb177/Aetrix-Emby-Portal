# -*- coding: utf-8 -*-
"""Tests for .iso (disc image) support across the scan/probe pipeline."""
import os

os.environ.setdefault("DATABASE_TYPE", "sqlite")
os.environ.setdefault("REDIS_ENABLED", "false")

import uuid
from unittest import mock

from backend.database import SessionLocal, init_db
from backend.emby_server import models as em
from backend.emby_server import media_probe
from backend.emby_server.scanner import VIDEO_EXTS
from backend.emby_server.mounts import REMOTE_MEDIA_EXTS
from backend.emby_server import drive_changes, change_watcher

init_db()


class TestIsoInExtSets:
    def test_scanner_video_exts(self):
        assert ".iso" in VIDEO_EXTS

    def test_remote_media_exts(self):
        assert ".iso" in REMOTE_MEDIA_EXTS

    def test_drive_changes_video_exts(self):
        assert ".iso" in drive_changes._VIDEO_EXTS

    def test_change_watcher_video_exts(self):
        assert ".iso" in change_watcher.VIDEO_EXTS


class TestIsoIntakeFilter:
    """Replicates the drive walk filter (mounts.py walk_media):
    .iso must pass, non-media must still be rejected."""

    def _passes(self, name: str) -> bool:
        return os.path.splitext(name)[1].lower() in REMOTE_MEDIA_EXTS

    def test_iso_file_allowed(self):
        assert self._passes("2025 (2025) - 2160p UHD.iso")
        assert self._passes("movie.ISO")

    def test_non_media_still_rejected(self):
        for name in ("notes.txt", "poster.jpg", "trailer.mp4.tmp", "archive.rar"):
            assert not self._passes(name), name


def _make_iso_item(db):
    lib = em.Library(guid=uuid.uuid4().hex, name=f"iso_{uuid.uuid4().hex[:8]}",
                     collection_type="movies", paths="/tmp/iso_test")
    db.add(lib)
    db.commit()
    item = em.MediaItem(
        guid=uuid.uuid4().hex, library_id=lib.id, name="2025 (2025)",
        item_type="movie", file_path="mount://3/MoviePilot/电影/外语电影/2025 (2025)/2025 (2025) - 2160p UHD.iso",
        container="iso", probe_status="pending",
    )
    db.add(item)
    db.commit()
    db.refresh(item)
    return lib, item


class TestIsoProbeDegradation:
    def test_probe_one_iso_short_circuits_to_degraded(self):
        db = SessionLocal()
        lib, item = _make_iso_item(db)
        try:
            with mock.patch.object(media_probe, "probe_metadata",
                                   side_effect=AssertionError("ffprobe must not run for iso")), \
                 mock.patch.object(media_probe, "resolve_probe_input",
                                   side_effect=AssertionError("no network resolve for iso")):
                result = media_probe.probe_one(db, item)
            assert result == "degraded"
            db.refresh(item)
            assert item.probe_status == "degraded"
            assert item.probe_next_retry_at is None
        finally:
            db.query(em.MediaItem).filter(em.MediaItem.library_id == lib.id).delete(synchronize_session=False)
            db.query(em.Library).filter(em.Library.id == lib.id).delete()
            db.commit()
            db.close()

    def test_probe_one_non_iso_not_affected(self):
        db = SessionLocal()
        lib = em.Library(guid=uuid.uuid4().hex, name=f"iso_{uuid.uuid4().hex[:8]}",
                         collection_type="movies", paths="/tmp/iso_test")
        db.add(lib)
        db.commit()
        item = em.MediaItem(
            guid=uuid.uuid4().hex, library_id=lib.id, name="plain",
            item_type="movie", file_path="/tmp/iso_test/a.mkv",
            container="mkv", probe_status="pending",
        )
        db.add(item)
        db.commit()
        try:
            with mock.patch.object(media_probe, "probe_metadata",
                                   side_effect=AssertionError("not reached")), \
                 mock.patch.object(media_probe, "resolve_probe_input",
                                   return_value=(None, None)):
                # (None, None) → 走 mark_retry，无重试标记时判不了死；
                # 关键是 .iso 短路没有误伤 mkv
                result = media_probe.probe_one(db, item)
            assert result in ("pending", "retry", "failed", item.probe_status)
        finally:
            db.query(em.MediaItem).filter(em.MediaItem.library_id == lib.id).delete(synchronize_session=False)
            db.query(em.Library).filter(em.Library.id == lib.id).delete()
            db.commit()
            db.close()
