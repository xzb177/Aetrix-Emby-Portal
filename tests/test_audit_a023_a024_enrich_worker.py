"""A-023/A-024 回归：季海报瞬态失败不杀 worker 线程；豆瓣封禁降级 PUT 不再谎称保存。

先红后绿用例，口径见 R8-08.md §A-023、§A-024。
"""
import os
import tempfile

os.environ.setdefault("DATABASE_TYPE", "sqlite")
os.environ.setdefault("REDIS_ENABLED", "false")
_fd, _tmppath = tempfile.mkstemp(suffix=".db"); os.close(_fd)
os.environ["DATABASE_URL"] = "sqlite:///" + _tmppath
from backend import database as _dbmod
from sqlalchemy import create_engine as _ce
from sqlalchemy.orm import sessionmaker as _sm
_dbmod.engine = _ce(os.environ["DATABASE_URL"])
_dbmod.configure_session_local(_sm(bind=_dbmod.engine))

import threading
import uuid
from types import SimpleNamespace
from unittest import mock

import pytest
from fastapi import HTTPException

from backend.database import SessionLocal, init_db
from backend.emby_server import models as em
from backend.emby_server import enrich_worker as ew
from backend.emby_server import tmdb as tmdb_mod

init_db()


@pytest.fixture()
def db():
    s = SessionLocal()
    try:
        s.query(em.MediaStream).delete()
        s.query(em.MediaItem).delete()
        s.commit()
        yield s
    finally:
        s.close()


def _make_item(db, **kw):
    it = em.MediaItem(
        guid=uuid.uuid4().hex,
        library_id=kw.pop("library_id", 1),
        item_type=kw.pop("item_type", "movie"),
        name=kw.pop("name", "测试"),
        file_path=kw.pop("file_path", "/tmp/x.mp4"),
        enrich_status=kw.pop("enrich_status", "pending"),
        **kw,
    )
    db.add(it)
    db.commit()
    return it


def test_season_poster_transient_returns_retry_not_raise(db):
    series = _make_item(
        db, item_type="series", name="测试剧", file_path="/tmp/series.mp4",
        enrich_status="enriching", enrich_attempts=0, tmdb_id="12345")
    _make_item(
        db, item_type="season", name="S1", file_path="/tmp/s1.mp4",
        series_id=series.id, season_number=1,
        poster_path="", primary_image_url="",
        backdrop_path="", backdrop_image_url="",
        enrich_status="pending")

    def fake_fetch(snapshot, **_kw):
        return {"ok": True, "tmdb_id": "12345"}

    def boom(*_a, **_k):
        raise tmdb_mod.TmdbTransientError("TMDB transient test")

    with mock.patch.object(ew, "_enrich_fetch", fake_fetch), \
            mock.patch.object(tmdb_mod.tmdb_client, "season_poster", boom):
        outcome = ew._process_item(db, series)
    assert outcome == "retry"
    db.refresh(series)
    assert series.enrich_status == "pending"
    assert series.enrich_attempts == 1
    assert series.enrich_next_retry_at is not None


def test_worker_loop_survives_unexpected_batch_error(monkeypatch):
    orig_event_state = ew._stop_event.is_set()
    orig_threads = ew._worker_threads
    try:
        ew._stop_event.clear()
        ew._worker_threads = []
        monkeypatch.setattr(ew, "ENRICH_IDLE_POLL_SEC", 0)
        monkeypatch.setattr(
            ew, "_enrich_tuning_config",
            lambda db=None: {"values": {ew.CFG_ENRICH_BATCH: 10,
                                        ew.CFG_ENRICH_TMDB_BATCH: False}})
        from backend.emby_server import task_scheduler as ts_mod
        from backend.emby_server import enrich_queues as eq_mod
        monkeypatch.setattr(ts_mod, "acquire_enrich_permit",
                            lambda: object())
        monkeypatch.setattr(ts_mod, "release_enrich_permit",
                            lambda _p: None)
        monkeypatch.setattr(eq_mod, "two_queue_enabled", lambda: False)
        monkeypatch.setattr(ew, "_note_enrich_batch_feed",
                            lambda *a, **k: None)
        calls = {"claim": 0, "process": 0}

        def fake_claim(db_arg, limit, queue=None):
            calls["claim"] += 1
            if calls["claim"] == 1:
                item = mock.MagicMock()
                item.series_id = None
                item.id = 999
                return [item]
            ew._stop_event.set()
            return []

        def boom_process(db_arg, item, holder=None, queue=None):
            calls["process"] += 1
            raise RuntimeError("unexpected batch boom")

        monkeypatch.setattr(ew, "_claim_batch", fake_claim)
        monkeypatch.setattr(ew, "_process_item", boom_process)
        ew._worker_loop(0)
        assert calls["process"] == 1
        assert calls["claim"] >= 2
    finally:
        ew._stop_event.clear()
        if orig_event_state:
            ew._stop_event.set()
        ew._worker_threads = orig_threads


class _StubThread:
    def __init__(self, alive, name):
        self._alive = alive
        self.name = name
        self._started = False

    def is_alive(self):
        return self._alive

    def join(self, timeout=None):
        return None


class _FakeThread:
    def __init__(self, target=None, args=(), name=None, daemon=None):
        self.target = target
        self.args = args
        self.name = name
        self.daemon = daemon
        self._started = False

    def start(self):
        self._started = True

    def is_alive(self):
        return self._started

    def join(self, timeout=None):
        return None


def test_start_recovers_dead_workers_when_janitor_alive(monkeypatch):
    orig_threads = ew._worker_threads
    orig_janitor = getattr(ew, "_janitor_thread", None)
    orig_effective = ew._effective_workers
    orig_event_state = ew._stop_event.is_set()
    try:
        dead = _StubThread(False, "enrich-worker-0")
        janitor = _StubThread(True, "enrich-janitor")
        ew._worker_threads = [dead, janitor]
        ew._janitor_thread = janitor
        ew._stop_event.clear()
        monkeypatch.setattr(ew, "ENRICH_ENABLED", True)
        monkeypatch.setattr(ew, "_resolve_enrich_workers", lambda db: 2)
        monkeypatch.setattr(ew, "_recover_crashed", lambda db: 0)
        monkeypatch.setattr(ew, "_suppress_flood", lambda db: 0)
        fake_db = mock.MagicMock()
        fake_db.close.return_value = None
        monkeypatch.setattr(ew, "SessionLocal", lambda: fake_db)
        created = []

        def fake_thread_factory(target=None, args=(), name=None, daemon=None):
            t = _FakeThread(target=target, args=args, name=name, daemon=daemon)
            created.append(t)
            return t

        monkeypatch.setattr(threading, "Thread", fake_thread_factory)
        ew.start()
        workers = [t for t in ew._worker_threads
                   if getattr(t, "name", None) != "enrich-janitor"]
        assert len(workers) == 2
        assert all(t._started for t in workers)
        assert janitor in ew._worker_threads
        assert len(created) == 2
    finally:
        ew._worker_threads = orig_threads
        ew._janitor_thread = orig_janitor
        ew._effective_workers = orig_effective
        ew._stop_event.clear()
        if orig_event_state:
            ew._stop_event.set()


def test_put_douban_ban_downgrade_returns_410_and_get_unchanged(db):
    from backend.api import admin_scrape
    from backend.integrations import store
    from backend import models as _models
    staff = SimpleNamespace(is_superuser=True)
    store.write_values(db, {ew.CFG_DOUBAN_BAN_DOWNGRADE: "0"},
                       {ew.CFG_DOUBAN_BAN_DOWNGRADE: "A-024 test"})
    db.commit()
    store.invalidate(ew.CFG_DOUBAN_BAN_DOWNGRADE)
    try:
        got = admin_scrape.get_douban_ban_downgrade(staff=staff, db=db)
        assert got["enabled"] is False
        with pytest.raises(HTTPException) as excinfo:
            admin_scrape.save_douban_ban_downgrade(
                admin_scrape._DoubanBanDowngradeBody(enabled=True),
                staff=staff, db=db)
        assert excinfo.value.status_code == 410
        assert "ENRICH_DOUBAN_BAN_DOWNGRADE" in str(excinfo.value.detail)
        got2 = admin_scrape.get_douban_ban_downgrade(staff=staff, db=db)
        assert got2["enabled"] is False
    finally:
        db.query(_models.SystemConfig).filter(
            _models.SystemConfig.key == ew.CFG_DOUBAN_BAN_DOWNGRADE
        ).delete(synchronize_session=False)
        db.commit()
        store.invalidate(ew.CFG_DOUBAN_BAN_DOWNGRADE)
