"""Tests for unified enrich_status writer (P2-1/P2-3/P3-1, round3-p2p3).

Covers:
- _compute_size_change: size write-back on change (bug fix: previously
  size_changed=True did not persist item.size, causing infinite re-queue)
- _apply_enrich_status: layered vs flat semantics, failed self-healing
"""
import sys
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

# Import helpers from scanner (module-level, no DB needed)
sys.path.insert(0, "/tmp")
# We test via direct import of the fixed file's helpers.
# Since scanner.py has heavy imports, we exec only the helper section.
import re

with open("/tmp/scanner_fixed.py") as f:
    src = f.read()

# Extract helper block
m = re.search(
    r"# ---- enrich_status 统一写入点.*?^def _apply_enrich_status.*?(?=^def |\Z)",
    src, re.M | re.S,
)
assert m, "helper block not found"
helper_src = m.group(0)

# Provide minimal globals the helpers need
ns = {
    "timedelta": timedelta,
    "datetime": datetime,
}
# Fake logger capturing records
records = []
class FakeLogger:
    def info(self, fmt, *a): records.append(("info", fmt % a))
    def debug(self, fmt, *a): records.append(("debug", fmt % a))
ns["logger"] = FakeLogger()

exec(helper_src, ns)
_compute_size_change = ns["_compute_size_change"]
_apply_enrich_status = ns["_apply_enrich_status"]
_write_enrich_status = ns["_write_enrich_status"]


def _item(**kw):
    return SimpleNamespace(**kw)

def _scanfile(size):
    return SimpleNamespace(size=size)


@pytest.fixture(autouse=True)
def _clear_records():
    records.clear()
    yield
    records.clear()


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


class TestApplyEnrichStatus:
    def test_layered_new_item_pending(self):
        item = _item(enrich_status=None, size=0)
        _apply_enrich_status(item, _scanfile(50), is_new=True,
                             layered=True, item_type="movie")
        assert item.enrich_status == "pending"
        assert any(r[0] == "info" and "new_item" in r[1] for r in records)

    def test_layered_no_prior_status_pending(self):
        item = _item(enrich_status=None, size=0)
        _apply_enrich_status(item, _scanfile(50), is_new=False,
                             layered=True, item_type="movie")
        assert item.enrich_status == "pending"
        assert any("no_prior_status" in r[1] for r in records)

    def test_layered_size_change_requeues(self):
        item = _item(enrich_status="done", size=100)
        _apply_enrich_status(item, _scanfile(200), is_new=False,
                             layered=True, item_type="movie")
        assert item.enrich_status == "pending"
        assert item.size == 200
        assert any("size_changed" in r[1] for r in records)

    def test_layered_failed_self_heal_resets_attempts(self):
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
        assert any("attempts_reset=True" in r[1] for r in records)

    def test_layered_mtime_only_keeps_status(self):
        item = _item(enrich_status="done", size=100)
        _apply_enrich_status(item, _scanfile(100), is_new=False,
                             layered=True, item_type="movie")
        assert item.enrich_status == "done"  # unchanged
        assert any(r[0] == "debug" for r in records)

    def test_flat_pending_finalizes_to_done(self):
        item = _item(enrich_status="pending", size=100)
        _apply_enrich_status(item, _scanfile(100), is_new=False,
                             layered=False, item_type="movie")
        assert item.enrich_status == "done"

    def test_flat_non_movie_episode_untouched(self):
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
