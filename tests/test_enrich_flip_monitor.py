"""Tests for enrich done→pending flip monitoring (v2.55.x).

Covers:
- enrich_flip_monitor.should_alert: threshold logic
- enrich_flip_monitor.summarize_flips: aggregation
- enrich_flip_monitor.record_flip_event / alert_flip: PG persistence + warning log
- enrich_flip_monitor.flip_count_last_hours: /api/health metric source
- scanner._write_enrich_status: returns True on done→pending flip
- scanner._apply_enrich_status: flip_tracker records flips
"""
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend.database import Base
from backend.emby_server import enrich_flip_monitor as fm
from backend.emby_server import models as em
from backend.emby_server import scanner
from backend.emby_server.scanner import (
    _apply_enrich_status,
    _write_enrich_status,
)


@pytest.fixture()
def db():
    """函数级内存 SQLite，建全表（含新增的 enrich_flip_events）。"""
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


@pytest.fixture()
def capture(monkeypatch):
    records = []

    class FakeLogger:
        def warning(self, fmt, *a):
            records.append(("warning", fmt % a))

        def info(self, fmt, *a):
            records.append(("info", fmt % a))

        def debug(self, fmt, *a):
            records.append(("debug", fmt % a))

    monkeypatch.setattr(fm, "logger", FakeLogger())
    monkeypatch.setattr(scanner, "logger", FakeLogger())
    return records


def _item(**kw):
    return SimpleNamespace(**kw)


def _scanfile(size):
    return SimpleNamespace(size=size)


class TestShouldAlert:
    def test_below_threshold_no_alert(self):
        assert fm.should_alert(99, threshold=100) is False

    def test_at_threshold_alerts(self):
        assert fm.should_alert(100, threshold=100) is True

    def test_above_threshold_alerts(self):
        assert fm.should_alert(5723, threshold=100) is True

    def test_zero_never_alerts(self):
        assert fm.should_alert(0, threshold=1) is False


class TestSummarizeFlips:
    def test_aggregation(self):
        flips = [
            {"item_id": 1, "item_type": "movie", "reason": "size_changed"},
            {"item_id": 2, "item_type": "movie", "reason": "size_changed"},
            {"item_id": 3, "item_type": "episode", "reason": "new_item"},
        ]
        s = fm.summarize_flips(flips)
        assert s["reasons"] == {"size_changed": 2, "new_item": 1}
        assert s["item_types"] == {"movie": 2, "episode": 1}
        assert s["sample_ids"] == [1, 2, 3]

    def test_sample_ids_capped_at_10(self):
        flips = [{"item_id": i, "item_type": "movie", "reason": "size_changed"}
                 for i in range(50)]
        s = fm.summarize_flips(flips)
        assert len(s["sample_ids"]) == 10
        assert s["reasons"] == {"size_changed": 50}


class TestRecordFlipEvent:
    def test_writes_persistent_row(self, db, capture, monkeypatch):
        # F-P1-2：record_flip_event 现用独立会话，测试时注入测试 db
        monkeypatch.setattr(
            fm, "_default_session_factory", lambda: db
        )
        ok = fm.record_flip_event(
            db, flip_count=5723, trigger="scan:manual",
            library_id=7, scan_run_id=725,
            details={"reasons": {"size_changed": 5723}},
            threshold=100,
        )
        assert ok is True
        rows = db.query(em.EnrichFlipEvent).all()
        assert len(rows) == 1
        r = rows[0]
        assert r.flip_count == 5723
        assert r.trigger == "scan:manual"
        assert r.library_id == 7
        assert r.scan_run_id == 725
        assert r.threshold == 100
        assert "size_changed" in (r.details or "")

    def test_db_failure_returns_false_not_raise(self, capture):
        class BadDB:
            def add(self, *a):
                raise RuntimeError("db down")

            def commit(self):
                raise RuntimeError("db down")

            def rollback(self):
                pass

        ok = fm.record_flip_event(BadDB(), flip_count=5, trigger="scan")
        assert ok is False  # 监控失败不能抛异常拖垮业务


class TestAlertFlip:
    def test_below_threshold_no_log_no_row(self, db, capture):
        fired = fm.alert_flip(db, flip_count=50, trigger="scan",
                              flips=[{"item_id": 1, "item_type": "movie",
                                      "reason": "size_changed"}],
                              threshold=100)
        assert fired is False
        assert db.query(em.EnrichFlipEvent).count() == 0
        assert not any(r[0] == "warning" for r in capture)

    def test_above_threshold_warns_and_persists(self, db, capture, monkeypatch):
        # F-P1-2：record_flip_event 现用独立会话，测试时注入测试 db
        monkeypatch.setattr(
            fm, "_default_session_factory", lambda: db
        )
        fired = fm.alert_flip(
            db, flip_count=150, trigger="scan:manual",
            library_id=3, scan_run_id=99,
            flips=[{"item_id": i, "item_type": "episode",
                    "reason": "size_changed"} for i in range(150)],
            threshold=100,
        )
        assert fired is True
        assert any(r[0] == "warning" and "150" in r[1] for r in capture)
        rows = db.query(em.EnrichFlipEvent).all()
        assert len(rows) == 1
        assert rows[0].flip_count == 150
        assert rows[0].trigger == "scan:manual"


class TestFlipCountLastHours:
    def test_counts_only_recent(self, db):
        now = datetime.now()
        db.add(em.EnrichFlipEvent(trigger="scan", flip_count=100,
                                  threshold=100,
                                  created_at=now - timedelta(minutes=30)))
        db.add(em.EnrichFlipEvent(trigger="scan", flip_count=200,
                                  threshold=100,
                                  created_at=now - timedelta(minutes=59)))
        db.add(em.EnrichFlipEvent(trigger="scan", flip_count=999,
                                  threshold=100,
                                  created_at=now - timedelta(hours=2)))
        db.commit()
        assert fm.flip_count_last_hours(db, hours=1) == 300
        assert fm.flip_count_last_hours(db, hours=3) == 1299

    def test_empty_is_zero(self, db):
        assert fm.flip_count_last_hours(db, hours=1) == 0


class TestWriteEnrichStatusFlipReturn:
    def test_done_to_pending_returns_true(self, capture):
        item = _item(enrich_status="done", size=100)
        flipped = _write_enrich_status(
            item, "pending", reason="size_changed",
            old_status="done", old_size=100, new_size=200)
        assert flipped is True
        assert item.enrich_status == "pending"

    def test_pending_to_pending_returns_false(self, capture):
        item = _item(enrich_status="pending", size=100)
        flipped = _write_enrich_status(
            item, "pending", reason="new_item",
            old_status=None, old_size=0, new_size=100)
        assert flipped is False

    def test_done_to_done_returns_false(self, capture):
        item = _item(enrich_status="done", size=100)
        flipped = _write_enrich_status(
            item, "done", reason="flat_scan_finalized",
            old_status="pending", old_size=100, new_size=100)
        assert flipped is False


class TestApplyEnrichStatusTracker:
    def test_size_change_flip_recorded(self, capture):
        item = _item(enrich_status="done", size=100, id=42)
        tracker = []
        _apply_enrich_status(item, _scanfile(200), is_new=False,
                             layered=True, item_type="movie",
                             flip_tracker=tracker)
        assert item.enrich_status == "pending"
        assert len(tracker) == 1
        assert tracker[0]["item_id"] == 42
        assert tracker[0]["reason"] == "size_changed"
        assert tracker[0]["old_size"] == 100
        assert tracker[0]["new_size"] == 200

    def test_no_flip_no_record(self, capture):
        item = _item(enrich_status="done", size=100, id=43)
        tracker = []
        _apply_enrich_status(item, _scanfile(100), is_new=False,
                             layered=True, item_type="movie",
                             flip_tracker=tracker)
        assert item.enrich_status == "done"
        assert tracker == []

    def test_tracker_optional_backward_compat(self, capture):
        """老调用方不传 flip_tracker 也能正常工作（现有测试即此路径）。"""
        item = _item(enrich_status="done", size=100)
        _apply_enrich_status(item, _scanfile(200), is_new=False,
                             layered=True, item_type="movie")
        assert item.enrich_status == "pending"


class TestMaybeAlertEnrichFlipUsesSharedAlert:
    """F2：_maybe_alert_enrich_flip 复用 alert_flip 共享能力。

    旧代码手写 logger.warning + record_flip_event，details 里 item_types 恒为 {}。
    新代码走 alert_flip(summary=...)，item_types 正常落盘。
    本测试在旧代码上失败（item_types == {}），新代码上通过。
    """

    def test_item_types_persisted_not_empty(self, db, capture, monkeypatch):
        import json
        from backend.emby_server import enrich_flip_monitor

        # F-P1-2：record_flip_event 现用独立会话，测试时注入测试 db
        monkeypatch.setattr(
            enrich_flip_monitor, "_default_session_factory", lambda: db
        )

        stats = {
            "enrich_flips": 150,
            "enrich_flip_reasons": {"size_changed": 150},
            "enrich_flip_sample_ids": [1, 2, 3],
            "enrich_flip_item_types": {"movie": 150},
        }
        scanner._maybe_alert_enrich_flip(
            db, SimpleNamespace(id=7), stats, run_id=None)
        rows = db.query(em.EnrichFlipEvent).all()
        assert len(rows) == 1
        details = json.loads(rows[0].details or "{}")
        assert details["item_types"] == {"movie": 150}, \
            f"item_types 丢失：{details}"
        assert details["reasons"] == {"size_changed": 150}
        assert rows[0].trigger == "scan"

    def test_alert_flip_accepts_summary(self, db, capture, monkeypatch):
        """alert_flip(summary=...) 跳过 summarize_flips，直接用传入的摘要。"""
        # F-P1-2：record_flip_event 现用独立会话，测试时注入测试 db
        monkeypatch.setattr(
            fm, "_default_session_factory", lambda: db
        )
        fired = fm.alert_flip(
            db, flip_count=150, trigger="scan",
            summary={"reasons": {"size_changed": 150},
                     "item_types": {"movie": 150},
                     "sample_ids": [1]},
            threshold=100,
        )
        assert fired is True
        rows = db.query(em.EnrichFlipEvent).all()
        assert len(rows) == 1


class TestFlipCountExcludeTriggers:
    """F3：flip_count_last_hours(exclude_triggers=...)。

    管理端手动 retry_unmatched 是已知操作，不应计入 /api/health 的 warn 指标。
    旧代码无 exclude_triggers 参数 → TypeError，本测试在旧代码上失败。
    """

    def test_exclude_manual_retry(self, db):
        now = datetime.now()
        db.add(em.EnrichFlipEvent(trigger="scan", flip_count=100,
                                  threshold=100,
                                  created_at=now - timedelta(minutes=10)))
        db.add(em.EnrichFlipEvent(trigger="retry_unmatched", flip_count=200,
                                  threshold=100,
                                  created_at=now - timedelta(minutes=10)))
        db.commit()
        assert fm.flip_count_last_hours(db, hours=1) == 300
        assert fm.flip_count_last_hours(
            db, hours=1, exclude_triggers=("retry_unmatched",)) == 100
