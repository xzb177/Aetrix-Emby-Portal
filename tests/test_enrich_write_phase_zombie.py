"""R4 轮回归测试：enrich 认领—回收—失败终态链路的两项修复。

R4-3：_mark_failed / _requeue_mount_unavailable / _requeue_douban_banned
     自身抛异常时不再静默（必须打 logger.warning），调用方仍拿到 "skip"。
R4-4：写库阶段（_process_item 与 _process_batch Phase 3）加僵尸 worker
     归属校验——慢 IO 期间认领已失效时，迟到的写库返回 "skip"，不许覆盖
     别的 worker 已写好的新状态。

脚手架复用 test_enrich_worker_zombie.py 的隔离 SQLite 方案。
"""
import logging
import os
import tempfile
import uuid
from datetime import datetime, timedelta

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

import pytest

from backend.database import SessionLocal, init_db
from backend.emby_server import models as em
from backend.emby_server import enrich_worker

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


def _read_status(item_id):
    s = SessionLocal()
    try:
        r = s.query(em.MediaItem).filter(em.MediaItem.id == item_id).first()
        return r.enrich_status
    finally:
        s.close()


def _zombie_stage(db):
    """A 认领 → janitor 回收 → B 认领并 done。返回 (item_id, snapshot_a)。"""
    it = _make_item(db)
    batch_a = enrich_worker._claim_batch(db, 10)
    assert len(batch_a) == 1
    snap_a = enrich_worker._snapshot_item(batch_a[0])
    assert snap_a.enrich_claim_token

    dbJ = SessionLocal()
    try:
        row = dbJ.query(em.MediaItem).filter(em.MediaItem.id == it.id).first()
        row.enrich_claimed_at = datetime.now() - timedelta(seconds=3600)
        dbJ.commit()
        assert enrich_worker._reclaim_stale(dbJ, lease_sec=60) == 1
    finally:
        dbJ.close()

    dbB = SessionLocal()
    try:
        batch_b = enrich_worker._claim_batch(dbB, 10)
        assert len(batch_b) == 1
        assert batch_b[0].enrich_claim_token != snap_a.enrich_claim_token
        row = dbB.query(em.MediaItem).filter(em.MediaItem.id == it.id).first()
        row.enrich_status = "done"
        row.enrich_claimed_at = None
        row.enrich_claim_token = None
        dbB.commit()
    finally:
        dbB.close()
    return it.id, snap_a


class TestWritePhaseZombieGuard:
    """R4-4：写库阶段僵尸防护"""

    def _patch_io(self, monkeypatch, apply_mock):
        monkeypatch.setattr(
            enrich_worker, "_enrich_fetch",
            lambda snapshot, holder=None, inherit_parent=None: {"ok": True},
        )
        monkeypatch.setattr(enrich_worker, "_enrich_apply", apply_mock)

    def test_zombie_write_phase_skips(self, db, monkeypatch):
        """A 的迟到写库不许覆盖 B 的 done：_enrich_apply 不被调用，返回 skip。"""
        item_id, snap_a = _zombie_stage(db)
        calls = []
        self._patch_io(monkeypatch, lambda d, fresh, fetched: calls.append(fresh.id))
        db.rollback()
        outcome = enrich_worker._process_item(db, snap_a)
        assert outcome == "skip"
        assert calls == [], "僵尸 worker 的写库必须被拦下"
        assert _read_status(item_id) == "done"

    def test_owner_write_phase_still_writes(self, db, monkeypatch):
        """归属校验不能误伤正常流程：持有有效认领的写库照常落盘。"""
        it = _make_item(db)
        batch = enrich_worker._claim_batch(db, 10)
        snap = enrich_worker._snapshot_item(batch[0])
        calls = []

        def _fake_apply(d, fresh, fetched):
            calls.append(fresh.id)
            fresh.enrich_status = "done"

        self._patch_io(monkeypatch, _fake_apply)
        db.rollback()
        outcome = enrich_worker._process_item(db, snap)
        assert outcome == "done"
        assert calls == [it.id]
        assert _read_status(it.id) == "done"

    def test_batch_phase3_zombie_skips_write(self, db, monkeypatch):
        """批量路径 Phase 3 的迟到写库同样不许覆盖 B 的 done。"""
        item_id, snap_a = _zombie_stage(db)
        calls = []
        # Phase 1 走 pre 直出 ok、tmdb_ctx=None：跳过 Phase 2，直达 Phase 3 写库点
        monkeypatch.setattr(
            enrich_worker, "_enrich_fetch_pre",
            lambda snapshot, holder=None, inherit_parent=None: ({"ok": True}, None),
        )
        monkeypatch.setattr(
            enrich_worker, "_enrich_apply",
            lambda d, fresh, fetched: calls.append(fresh.id),
        )
        db.rollback()
        enrich_worker._process_batch(db, [snap_a])
        assert calls == [], "批量 Phase 3 僵尸写库必须被拦下"
        assert _read_status(item_id) == "done"


class TestSilentFailureLogging:
    """R4-3：记失败/重排队自身异常必须打日志"""

    def _boom_commit(self, db, monkeypatch):
        def _boom():
            raise RuntimeError("commit boom")
        monkeypatch.setattr(db, "commit", _boom)

    def test_mark_failed_logs_on_internal_error(self, db, monkeypatch, caplog):
        it = _make_item(db, enrich_attempts=0)
        batch = enrich_worker._claim_batch(db, 10)
        token = batch[0].enrich_claim_token
        self._boom_commit(db, monkeypatch)
        with caplog.at_level(logging.WARNING, logger="backend.emby_server.enrich_worker"):
            outcome = enrich_worker._mark_failed(
                db, it.id, 0, "boom", claim_token=token)
        assert outcome == "skip"
        assert any("补全记失败异常" in r.message and str(it.id) in r.message
                   for r in caplog.records), "必须有 warning 日志"

    def test_requeue_mount_logs_on_internal_error(self, db, monkeypatch, caplog):
        it = _make_item(db)
        batch = enrich_worker._claim_batch(db, 10)
        token = batch[0].enrich_claim_token
        self._boom_commit(db, monkeypatch)
        with caplog.at_level(logging.WARNING, logger="backend.emby_server.enrich_worker"):
            outcome = enrich_worker._requeue_mount_unavailable(
                db, it.id, 7, claim_token=token)
        assert outcome == "skip"
        assert any("补全存储不可用重排异常" in r.message for r in caplog.records)

    def test_requeue_douban_banned_logs_on_internal_error(self, db, monkeypatch, caplog):
        it = _make_item(db)
        batch = enrich_worker._claim_batch(db, 10)
        token = batch[0].enrich_claim_token
        self._boom_commit(db, monkeypatch)
        with caplog.at_level(logging.WARNING, logger="backend.emby_server.enrich_worker"):
            outcome = enrich_worker._requeue_douban_banned(
                db, it.id, claim_token=token)
        assert outcome == "skip"
        assert any("补全豆瓣封禁重排异常" in r.message for r in caplog.records)
