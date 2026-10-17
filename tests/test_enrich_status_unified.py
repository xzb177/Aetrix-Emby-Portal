"""Tests for unified enrich_status writer (P2-1/P2-3/P3-1, round3-p2p3).

Covers:
- _compute_size_change: size write-back on change (bug fix: previously
  size_changed=True did not persist item.size, causing infinite re-queue)
- _apply_enrich_status: layered vs flat semantics, failed self-healing
- _write_enrich_status: reason mandatory, log levels
"""
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

from backend.emby_server import scanner
from backend.emby_server.scanner import (
    _apply_enrich_status,
    _compute_size_change,
    _scan_has_metadata,
    _write_enrich_status,
)


def _item(**kw):
    return SimpleNamespace(**kw)


def _scanfile(size):
    return SimpleNamespace(size=size)


@pytest.fixture
def _capture_logs(monkeypatch):
    records = []

    class FakeLogger:
        def info(self, fmt, *a):
            records.append(("info", fmt % a))

        def debug(self, fmt, *a):
            records.append(("debug", fmt % a))

    monkeypatch.setattr(scanner, "logger", FakeLogger())
    return records


class TestComputeSizeChange:
    def test_first_learn_adopts_size_not_changed(self):
        item = _item(size=0)
        old, new, changed = _compute_size_change(item, _scanfile(100))
        assert (old, new, changed) == (0, 100, False)
        assert item.size == 100

    def test_size_changed_writes_back(self):
        """Regression: size change must persist, else infinite re-queue."""
        item = _item(size=100)
        old, new, changed = _compute_size_change(item, _scanfile(200))
        assert changed is True
        assert item.size == 200  # written back!

    def test_size_unchanged_no_write(self):
        item = _item(size=100)
        old, new, changed = _compute_size_change(item, _scanfile(100))
        assert changed is False
        assert item.size == 100


class TestComputeSizeChangeStrm:
    """strm 双口径：纸条大小（.strm 文本）与视频大小（网盘）不得互判变化。

    生产事故（2026-10-15）：两种扫描交替回写 item.size，同一批 episode
    被 size_changed 无限翻转（24h 约 10 万次），enrich 队列常驻 7 万。
    """

    def _sitem(self, size, path="/strm/show/ep.strm"):
        return _item(size=size, file_path=path)

    def _sscan(self, size, path="/strm/show/ep.strm"):
        return SimpleNamespace(size=size, stored_path=path)

    def test_stub_then_video_adopts_video_no_flip(self):
        item = self._sitem(145)
        old, new, changed = _compute_size_change(item, self._sscan(262089236))
        assert changed is False
        assert item.size == 262089236

    def test_video_then_stub_keeps_video_no_flip(self):
        item = self._sitem(262089236)
        old, new, changed = _compute_size_change(item, self._sscan(145))
        assert changed is False
        assert item.size == 262089236

    def test_stub_vs_stub_no_flip_no_writeback(self):
        item = self._sitem(145)
        old, new, changed = _compute_size_change(item, self._sscan(150))
        assert changed is False
        assert item.size == 145

    def test_video_vs_video_still_flips(self):
        item = self._sitem(2000000000)
        old, new, changed = _compute_size_change(item, self._sscan(8000000000))
        assert changed is True
        assert item.size == 8000000000

    def test_first_learn_stub_then_video(self):
        item = self._sitem(0)
        old, new, changed = _compute_size_change(item, self._sscan(145))
        assert changed is False
        assert item.size == 145
        old, new, changed = _compute_size_change(item, self._sscan(262089236))
        assert changed is False
        assert item.size == 262089236

    def test_strm_detected_via_stored_path_only(self):
        item = _item(size=145)  # 无 file_path
        old, new, changed = _compute_size_change(item, self._sscan(262089236))
        assert changed is False
        assert item.size == 262089236

    def test_strm_extension_case_insensitive(self):
        item = self._sitem(145, path="/strm/show/EP.STRM")
        old, new, changed = _compute_size_change(item, self._sscan(262089236))
        assert changed is False
        assert item.size == 262089236

    def test_non_strm_small_sizes_still_flip(self):
        """回归保护：普通文件的小大小变化仍按变化处理。"""
        item = _item(size=145, file_path="/media/movie.mkv")
        scan = SimpleNamespace(size=150, stored_path="/media/movie.mkv")
        old, new, changed = _compute_size_change(item, scan)
        assert changed is True
        assert item.size == 150


class TestApplyEnrichStatus:
    def test_layered_new_item_pending(self, _capture_logs):
        item = _item(enrich_status=None, size=0)
        _apply_enrich_status(item, _scanfile(50), is_new=True,
                             layered=True, item_type="movie")
        assert item.enrich_status == "pending"
        assert any(r[0] == "info" and "new_item" in r[1] for r in _capture_logs)

    def test_layered_no_prior_status_pending(self, _capture_logs):
        item = _item(enrich_status=None, size=0)
        _apply_enrich_status(item, _scanfile(50), is_new=False,
                             layered=True, item_type="movie")
        assert item.enrich_status == "pending"
        assert any("no_prior_status" in r[1] for r in _capture_logs)

    def test_layered_size_change_requeues(self, _capture_logs):
        item = _item(enrich_status="done", size=100)
        _apply_enrich_status(item, _scanfile(200), is_new=False,
                             layered=True, item_type="movie")
        assert item.enrich_status == "pending"
        assert item.size == 200
        assert any("size_changed" in r[1] for r in _capture_logs)

    def test_layered_failed_self_heal_resets_attempts(self, _capture_logs):
        item = _item(enrich_status="failed", size=100, enrich_attempts=5,
                     enrich_next_retry_at=None)
        before = datetime.now()
        _apply_enrich_status(item, _scanfile(200), is_new=False,
                             layered=True, item_type="movie")
        assert item.enrich_status == "pending"
        assert item.enrich_attempts == 0
        assert item.enrich_next_retry_at is not None
        delta = item.enrich_next_retry_at - before
        assert timedelta(minutes=50) < delta < timedelta(minutes=70)
        assert any("attempts_reset=True" in r[1] for r in _capture_logs)

    def test_layered_mtime_only_keeps_status(self, _capture_logs):
        item = _item(enrich_status="done", size=100)
        _apply_enrich_status(item, _scanfile(100), is_new=False,
                             layered=True, item_type="movie")
        assert item.enrich_status == "done"  # unchanged
        assert any(r[0] == "debug" for r in _capture_logs)

    def test_flat_pending_finalizes_to_done(self, _capture_logs):
        item = _item(enrich_status="pending", size=100)
        _apply_enrich_status(item, _scanfile(100), is_new=False,
                             layered=False, item_type="movie",
                             has_metadata=True)
        assert item.enrich_status == "done"
        assert any(r[0] == "debug" and "flat_scan_finalized" in r[1]
                   for r in _capture_logs)

    def test_flat_pending_defaults_to_done_backward_compat(self, _capture_logs):
        """老调用方不传 has_metadata 时保持旧行为（done），不静默改口径。"""
        item = _item(enrich_status="pending", size=100)
        _apply_enrich_status(item, _scanfile(100), is_new=False,
                             layered=False, item_type="movie")
        assert item.enrich_status == "done"

    def test_flat_no_metadata_goes_pending(self, _capture_logs):
        """第 0 批：无 NFO、无 TMDB 命中、无有效文本 → pending（假完成修复）。

        用与分层远程文件相同的待刮状态值，等 enrich_worker 后台补。
        """
        item = _item(enrich_status="pending", size=100)
        _apply_enrich_status(item, _scanfile(100), is_new=False,
                             layered=False, item_type="movie",
                             has_metadata=False)
        assert item.enrich_status == "pending"
        assert any(r[0] == "info" and "flat_scan_no_metadata" in r[1]
                   for r in _capture_logs)

    def test_flat_new_item_no_metadata_pending(self):
        """第 0 批：新行（old_status None）在非分层下无元数据同样 pending。"""
        item = _item(enrich_status=None, size=0)
        _apply_enrich_status(item, _scanfile(50), is_new=True,
                             layered=False, item_type="movie",
                             has_metadata=False)
        assert item.enrich_status == "pending"

    def test_flat_non_movie_episode_untouched(self, _capture_logs):
        """Non movie/episode types are not finalized by flat scan (legacy)."""
        item = _item(enrich_status="pending", size=100)
        _apply_enrich_status(item, _scanfile(100), is_new=False,
                             layered=False, item_type="series")
        assert item.enrich_status == "pending"

    def test_single_writer_reason_mandatory(self):
        """_write_enrich_status requires reason (TypeError if missing)."""
        item = _item()
        with pytest.raises(TypeError):
            _write_enrich_status(item, "pending")  # missing keyword-only reason


class TestScanHasMetadata:
    """第 0 批：_scan_has_metadata 判定口径。"""

    def test_tmdb_hit_counts(self):
        pending = SimpleNamespace(tmdb_hit={"id": 123}, nfo_data=None)
        assert _scan_has_metadata(_item(), pending) is True

    def test_nfo_with_title_counts(self):
        pending = SimpleNamespace(tmdb_hit=None, nfo_data={"title": "赛博浪客"})
        assert _scan_has_metadata(_item(), pending) is True

    def test_nfo_with_overview_counts(self):
        pending = SimpleNamespace(tmdb_hit=None, nfo_data={"overview": "简介"})
        assert _scan_has_metadata(_item(), pending) is True

    def test_nfo_with_tmdb_id_counts(self):
        pending = SimpleNamespace(tmdb_hit=None, nfo_data={"tmdb_id": 12345})
        assert _scan_has_metadata(_item(), pending) is True

    def test_empty_nfo_does_not_count(self):
        pending = SimpleNamespace(tmdb_hit=None, nfo_data={})
        assert _scan_has_metadata(_item(), pending) is False

    def test_existing_tmdb_id_counts(self):
        pending = SimpleNamespace(tmdb_hit=None, nfo_data=None)
        assert _scan_has_metadata(_item(tmdb_id="123"), pending) is True

    def test_authoritative_source_counts(self):
        pending = SimpleNamespace(tmdb_hit=None, nfo_data=None)
        for src in ("nfo", "tmdb", "tmdb_img", "douban", "bangumi",
                    "multisource", "inherit"):
            assert _scan_has_metadata(_item(metadata_source=src), pending) is True

    def test_none_source_does_not_count(self):
        """metadata_source='none'（搜过、没有）不算有元数据 → 重新排队。"""
        pending = SimpleNamespace(tmdb_hit=None, nfo_data=None)
        assert _scan_has_metadata(_item(metadata_source="none"), pending) is False

    def test_overview_counts_as_effective_text(self):
        pending = SimpleNamespace(tmdb_hit=None, nfo_data=None)
        assert _scan_has_metadata(_item(overview="  有简介  "), pending) is True

    def test_bare_row_is_pending(self):
        """三无行（无 NFO、无 TMDB 命中、无有效文本）→ False。"""
        pending = SimpleNamespace(tmdb_hit=None, nfo_data=None)
        assert _scan_has_metadata(_item(name="xxx.mkv", overview="  "),
                                  pending) is False
