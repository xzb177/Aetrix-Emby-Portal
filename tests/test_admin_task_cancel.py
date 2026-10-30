"""转存任务队列列表 + 取消（GET/POST /api/admin/tasks/transfer-jobs*）。

- 只许取消 pending（含失败待重试）；running/done/failed/cancelled 回 409
  并明示当前状态；任务不存在回 404
- 取消走带条件的原子 UPDATE（与工人认领 pending→running 互斥），置终态
  cancelled 不删行，并写审计 AdminLog
- 列表：在飞（pending/running）在前、终态倒序补足，计数与可取消标记正确

全部在隔离临时 SQLite 里跑，不连生产库、不碰网络。
"""
import os
import tempfile
from datetime import datetime, timedelta
from types import SimpleNamespace

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

import pytest
from fastapi import HTTPException

from backend import models as base_models
from backend.api import admin_tasks
from backend.emby_server import models as em
from backend.integrations import store
from backend.database import SessionLocal, init_db

init_db()

ADMIN = SimpleNamespace(id=7)


@pytest.fixture()
def db():
    s = SessionLocal()
    try:
        s.query(em.TransferJob).delete()
        s.query(em.MediaItem).delete()
        s.query(base_models.AdminLog).delete()
        s.query(base_models.SystemConfig).delete()
        s.commit()
        store.invalidate()
        yield s
    finally:
        s.close()


def _job(db, item_id=1, status="pending", **kw):
    row = em.TransferJob(
        item_id=item_id, trigger=kw.pop("trigger", "playback"),
        provider=kw.pop("provider", "drive"),
        owner_scope=kw.pop("owner_scope", "user"),
        owner_ref=kw.pop("owner_ref", "42"), status=status, **kw)
    db.add(row)
    db.commit()
    return row


def _item(db, item_id=1, name="测试片"):
    row = em.MediaItem(id=item_id, guid=f"g-{item_id}", library_id=1,
                       item_type="movie", name=name)
    db.add(row)
    db.commit()
    return row


# ---------- 列表 ----------

def test_list_active_first_counts_and_names(db):
    _item(db, 1, "甲片")
    j_pending = _job(db, 1, "pending")
    j_done = _job(db, 1, "done")
    j_running = _job(db, 999, "running")  # 条目已删：名字回落「条目 id」
    j_failed = _job(db, 1, "failed")
    _job(db, 1, "cancelled")

    res = admin_tasks.list_transfer_jobs(db, limit=30)
    assert res["counts"] == {"pending": 1, "running": 1, "done": 1,
                             "failed": 1, "cancelled": 1}
    ids = [j["id"] for j in res["jobs"]]
    # 在飞在前（id 升序），终态在后（id 降序）
    assert ids[:2] == [j_pending.id, j_running.id]
    assert ids[2:] == sorted(ids[2:], reverse=True)
    by_id = {j["id"]: j for j in res["jobs"]}
    assert by_id[j_pending.id]["cancellable"] is True
    assert by_id[j_running.id]["cancellable"] is False
    assert by_id[j_done.id]["cancellable"] is False
    assert by_id[j_failed.id]["cancellable"] is False
    assert by_id[j_pending.id]["item_name"] == "甲片"
    assert by_id[j_running.id]["item_name"] == "条目 999"


def test_list_limit_caps_terminal_rows(db):
    _job(db, 1, "pending")
    for _ in range(5):
        _job(db, 1, "done")
    res = admin_tasks.list_transfer_jobs(db, limit=3)
    assert len(res["jobs"]) == 3
    assert res["jobs"][0]["status"] == "pending"


# ---------- 取消 ----------

def test_cancel_pending_job(db):
    job = _job(db, 1, "pending",
               next_retry_at=datetime.now() + timedelta(minutes=5))
    res = admin_tasks.cancel_transfer_job(job.id, db=db, admin=ADMIN)
    assert res["cancelled"] is True
    db.refresh(job)
    assert job.status == "cancelled"
    assert job.error_kind == "cancelled"
    assert job.next_retry_at is None
    logs = db.query(base_models.AdminLog).all()
    assert len(logs) == 1
    assert logs[0].action == "transfer_job.cancel"
    assert logs[0].admin_user_id == 7
    assert logs[0].target_id == job.id


def test_cancel_running_conflict(db):
    job = _job(db, 1, "running", claimed_by="worker-1")
    with pytest.raises(HTTPException) as ei:
        admin_tasks.cancel_transfer_job(job.id, db=db, admin=ADMIN)
    assert ei.value.status_code == 409
    assert "running" in ei.value.detail
    db.refresh(job)
    assert job.status == "running"
    assert db.query(base_models.AdminLog).count() == 0


def test_cancel_terminal_states_conflict(db):
    for status in ("done", "failed", "cancelled"):
        job = _job(db, 1, status)
        with pytest.raises(HTTPException) as ei:
            admin_tasks.cancel_transfer_job(job.id, db=db, admin=ADMIN)
        assert ei.value.status_code == 409
        assert status in ei.value.detail
        db.refresh(job)
        assert job.status == status
    assert db.query(base_models.AdminLog).count() == 0


def test_cancel_missing_job_404(db):
    with pytest.raises(HTTPException) as ei:
        admin_tasks.cancel_transfer_job(999999, db=db, admin=ADMIN)
    assert ei.value.status_code == 404
