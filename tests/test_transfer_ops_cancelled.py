"""ops_cancelled 统计口径（预见层 v2 第一层余项 PR-1）。

背景（2026-10-31 失败分类复核实证）：2026-10-29 运维批量处置用
一条 SQL 把 293 单直接写成 status=failed + error_kind=dead，
把「dead 占失败 62%」的假基线灌进了失败率/判死耗时统计。本单
落地独立落点：

- 操作者发起的取消（管理端转存任务取消口，全仓唯一的代码取消
  入口）终态 error_kind 落 ``ops_cancelled``（status 仍为
  cancelled，与既有终态语义逐字一致）；
- 统计消费点（额度驾驶舱转存队列计数 + 最近失败列表）一律剔除
  ops_cancelled：哪怕某行以 failed 形态带着 ops_cancelled 类别
  出现（模拟运维处置型写入），也不许计入 failed 计数与最近
  失败列表；
- 系统判死（_fail_job 的 dead 等类别）与用户侧口径逐字不变。

全部在隔离临时 SQLite 里跑，不连生产库、不碰网络。
"""
import os
import tempfile
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

from backend import models as base_models
from backend.api import admin_tasks
from backend.emby_server import models as em
from backend.emby_server import quota_dashboard as qd
from backend.emby_server import transfer_variant as tv
from backend.integrations import store
from backend.database import SessionLocal, init_db

init_db()

ADMIN = SimpleNamespace(id=7)


@pytest.fixture()
def db():
    s = SessionLocal()
    try:
        s.query(em.TransferJob).delete()
        s.query(base_models.AdminLog).delete()
        s.query(base_models.SystemConfig).delete()
        s.commit()
        store.invalidate()
        yield s
    finally:
        s.close()


def _job(db, status="pending", error_kind=None, **kw):
    row = em.TransferJob(
        item_id=kw.pop("item_id", 1), trigger=kw.pop("trigger", "playback"),
        provider=kw.pop("provider", "drive"),
        owner_scope=kw.pop("owner_scope", "user"),
        owner_ref=kw.pop("owner_ref", "42"), status=status,
        error_kind=error_kind, **kw)
    db.add(row)
    db.commit()
    return row


# ---------- 落点：操作者取消 → ops_cancelled ----------

def test_admin_cancel_lands_ops_cancelled(db):
    job = _job(db, "pending")
    res = admin_tasks.cancel_transfer_job(job.id, db=db, admin=ADMIN)
    assert res["cancelled"] is True
    db.refresh(job)
    assert job.status == "cancelled"
    assert job.error_kind == "ops_cancelled"
    assert tv.OPS_CANCELLED_KIND == "ops_cancelled"
    assert tv.is_ops_cancelled(job) is True


def test_is_ops_cancelled_only_matches_ops_kind(db):
    assert tv.is_ops_cancelled(_job(db, "failed", "dead")) is False
    assert tv.is_ops_cancelled(_job(db, "failed", "retriable")) is False
    assert tv.is_ops_cancelled(_job(db, "cancelled", "cancelled")) is False
    assert tv.is_ops_cancelled(_job(db, "pending", None)) is False


# ---------- 统计剔除：驾驶舱计数与最近失败 ----------

def test_quota_queue_section_excludes_ops_cancelled(db):
    _job(db, "failed", "dead")
    _job(db, "failed", "dead")
    _job(db, "failed", "retriable")
    # 运维处置型写入：以 failed 形态出现也必须剔除
    _job(db, "failed", "ops_cancelled")
    # 正常取消落点（status=cancelled）本就不进 failed
    _job(db, "cancelled", "ops_cancelled")
    _job(db, "done")
    _job(db, "pending")

    section = qd._queue_section(db)
    assert section["counts"]["failed"] == 3
    assert section["counts"]["done"] == 1
    assert section["counts"]["pending"] == 1
    kinds = {row["error_kind"] for row in section["recent_failed"]}
    assert kinds == {"dead", "retriable"}


# ---------- 不变口径：系统判死照旧 ----------

def test_fail_job_terminal_kinds_unchanged(db):
    job = _job(db, "running")
    out = tv._fail_job(db, job, "dead", "源已死")
    assert out["error_kind"] == "dead"
    db.refresh(job)
    assert job.status == "failed"
    assert job.error_kind == "dead"
    assert tv.is_ops_cancelled(job) is False
