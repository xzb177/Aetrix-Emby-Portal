"""僵尸 worker 防护回归测试（第 3 轮深度审查）。

场景：worker A 认领一行后慢 IO 超过租约，janitor 把行打回 pending，
worker B 重新认领并成功处理（done）。A 迟到的 _mark_failed /
_requeue_* 必须认出自己已是僵尸（claim token 对不上），返回 "skip"，
绝不覆盖行的新状态。

- 全部在隔离临时 SQLite 里跑，不连生产库；
- A / B / janitor 用各自独立的 Session，复刻生产多线程；
- 用真实的 _claim_batch / _reclaim_stale / _mark_failed / _requeue_* 函数，
  B 的"成功处理"直接写 done（等价于 _enrich_apply 尾部）；
- 僵尸调用前按生产调用方惯例先 db.rollback()（过期 identity map，
  保证重查看到 DB 最新状态，见 race_repro/check_identity_map.py 的验证）。
"""
import os
import tempfile

os.environ.setdefault("DATABASE_TYPE", "sqlite")
os.environ.setdefault("REDIS_ENABLED", "false")
_fd, _tmppath = tempfile.mkstemp(suffix=".db")
os.close(_fd)
os.environ["DATABASE_URL"] = "sqlite:///" + _tmppath
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


def _read(item_id):
    s = SessionLocal()
    try:
        r = s.query(em.MediaItem).filter(em.MediaItem.id == item_id).first()
        return (r.enrich_status, r.enrich_attempts, r.enrich_claim_token)
    finally:
        s.close()


def _zombie_setup(dbA, attempts=4):
    """搭好完整舞台，返回 (item_id, token_a)。

    dbA = worker A 的 session；janitor / worker B 各用独立 session（生产真相）。
    token_a 是 A 那次认领的令牌，舞台结束时已失效。
    """
    it = _make_item(dbA, enrich_attempts=attempts)
    # worker A 认领
    batch_a = enrich_worker._claim_batch(dbA, 10)
    assert len(batch_a) == 1 and batch_a[0].enrich_status == "enriching"
    token_a = batch_a[0].enrich_claim_token
    assert token_a, "认领必须写入 claim token"

    # janitor：租约过期，把 A 的行打回 pending（独立 session）
    dbJ = SessionLocal()
    try:
        row = dbJ.query(em.MediaItem).filter(em.MediaItem.id == it.id).first()
        row.enrich_claimed_at = datetime.now() - timedelta(seconds=3600)
        dbJ.commit()
        assert enrich_worker._reclaim_stale(dbJ, lease_sec=60) == 1
    finally:
        dbJ.close()

    # worker B 重新认领并成功做完（独立 session）
    dbB = SessionLocal()
    try:
        batch_b = enrich_worker._claim_batch(dbB, 10)
        assert len(batch_b) == 1
        assert batch_b[0].enrich_claim_token != token_a
        row = dbB.query(em.MediaItem).filter(em.MediaItem.id == it.id).first()
        row.enrich_status = "done"       # 等价于 _enrich_apply 尾部
        row.enrich_attempts = 0
        row.enrich_next_retry_at = None
        row.enrich_claimed_at = None
        row.enrich_claim_token = None
        dbB.commit()
    finally:
        dbB.close()
    return it.id, token_a


def test_zombie_mark_failed_does_not_clobber_done(db):
    """A 迟到的 _mark_failed 不许把 B 做完的 done 翻成 failed。"""
    item_id, token_a = _zombie_setup(db, attempts=4)
    db.rollback()  # 生产调用方惯例：先 rollback 再记失败
    outcome = enrich_worker._mark_failed(
        db, item_id, 4, "late io error", claim_token=token_a)
    assert outcome == "skip"
    status, attempts, _ = _read(item_id)
    assert status == "done"
    assert attempts == 0


def test_zombie_requeue_mount_does_not_clobber_done(db):
    """A 迟到的 _requeue_mount_unavailable 不许把 done 翻回 pending。"""
    item_id, token_a = _zombie_setup(db, attempts=0)
    db.rollback()
    outcome = enrich_worker._requeue_mount_unavailable(
        db, item_id, 1, claim_token=token_a)
    assert outcome == "skip"
    status, _, _ = _read(item_id)
    assert status == "done"


def test_zombie_requeue_douban_banned_does_not_clobber_done(db):
    """A 迟到的 _requeue_douban_banned 不许把 done 翻回 pending。"""
    item_id, token_a = _zombie_setup(db, attempts=0)
    db.rollback()
    outcome = enrich_worker._requeue_douban_banned(
        db, item_id, claim_token=token_a)
    assert outcome == "skip"
    status, _, _ = _read(item_id)
    assert status == "done"


def test_owner_mark_failed_still_records_failure(db):
    """归属校验不能误伤正常流程：持有有效认领的 worker 记失败照常生效。"""
    it = _make_item(db, enrich_attempts=0)
    batch = enrich_worker._claim_batch(db, 10)
    assert len(batch) == 1
    token = batch[0].enrich_claim_token
    db.rollback()
    outcome = enrich_worker._mark_failed(
        db, it.id, 0, "boom", claim_token=token)
    assert outcome == "retry"
    status, attempts, _ = _read(it.id)
    assert status == "pending"
    assert attempts == 1


def test_owner_requeue_mount_still_works(db):
    """持有有效认领的 worker 走存储不可用分支照常生效。"""
    it = _make_item(db)
    batch = enrich_worker._claim_batch(db, 10)
    token = batch[0].enrich_claim_token
    db.rollback()
    outcome = enrich_worker._requeue_mount_unavailable(
        db, it.id, 7, claim_token=token)
    assert outcome == "mount_unavailable"
    status, _, _ = _read(it.id)
    assert status == "pending"


def test_mark_failed_without_token_keeps_legacy_behavior(db):
    """不传 token（现有测试/旧调用方）保持原行为：不做归属校验。"""
    it = _make_item(db, enrich_status="pending", enrich_attempts=0)
    outcome = enrich_worker._mark_failed(db, it.id, 0, "boom")
    assert outcome == "retry"
    status, attempts, _ = _read(it.id)
    assert status == "pending" and attempts == 1


def test_snapshot_carries_claim_token(db):
    """快照必须带上 claim token，调用方才能把它传给记失败函数。"""
    it = _make_item(db)
    batch = enrich_worker._claim_batch(db, 10)
    assert len(batch) == 1
    snap = enrich_worker._snapshot_item(batch[0])
    assert snap.enrich_claim_token == batch[0].enrich_claim_token
    assert snap.enrich_claim_token
