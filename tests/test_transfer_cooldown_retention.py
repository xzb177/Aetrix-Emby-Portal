# -*- coding: utf-8 -*-
"""PanSave A 线 PR-2：转存冷却/保留期字段化。

设计口径（规则语义一字不变，只把「现算」变「落字」）：
- ``emby_transfer_jobs.cooldown_until``：建单与工人终态时顺手落字，
  值恒为 ``created_at + 24h 节流窗``；三个节流消费方改读字段，
  存量行（字段为 NULL）回退现算口径——新旧结果必须逐项一致。
- ``emby_transfer_variants.retention_reason / retention_until``：
  auto-migrate 补列、无数据迁移；清理器删副本前多一道 retention
  判断：``retention_until`` 未到不删（文件不碰、行不动）。

全部离线：SQLite + 行级构造 + 桩 Drive 客户端，不碰真实盘。
"""
import os

os.environ.setdefault("DATABASE_TYPE", "sqlite")
os.environ.setdefault("REDIS_ENABLED", "false")

from datetime import datetime, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend import models as base_models
from backend.emby_server import models as em
from backend.emby_server import transfer_cleanup as tc
from backend.emby_server import transfer_variant as tv
from backend.integrations import store

WINDOW = timedelta(seconds=tv.THROTTLE_SEC)


@pytest.fixture()
def db_factory():
    engine = create_engine("sqlite://",
                           connect_args={"check_same_thread": False},
                           poolclass=StaticPool)
    base_models.Base.metadata.create_all(engine)
    em.Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)


@pytest.fixture()
def db(db_factory):
    session = db_factory()
    store.invalidate()
    yield session
    session.close()
    store.invalidate()


@pytest.fixture(autouse=True)
def _enable_master(monkeypatch):
    monkeypatch.setenv("TRANSFER_VARIANT_ENABLED", "1")


_counter = [1000]


def _drive_item(db, **kw):
    _counter[0] += 1
    row = em.MediaItem(guid=f"cd-{_counter[0]}", library_id=1,
                       item_type="movie", name="测试片",
                       drive_file_id="origfid",
                       file_path="mount://3/电影/a.mkv", size=1000)
    for key, value in kw.items():
        setattr(row, key, value)
    db.add(row)
    db.commit()
    return row


def _mount(db, mount_id=3, mount_type="drive", path="drive:drive1"):
    row = em.StorageMount(id=mount_id, name=f"m{mount_id}",
                          mount_type=mount_type, path=path)
    db.add(row)
    db.commit()
    return row


def _binding(db, user_id, provider="drive"):
    cred = em.MountCredential(scope="user", owner_user_id=user_id,
                              status="active")
    db.add(cred)
    db.commit()
    row = em.UserStorageBinding(user_id=user_id, provider=provider,
                                account_label="test",
                                credential_id=cred.id, status="active")
    db.add(row)
    db.commit()
    return row


def _job(db, item_id, *, scope="source", ref=None, dest=None,
         status="done", created_at=None, cooldown_until="unset"):
    row = em.TransferJob(item_id=item_id, trigger="playback",
                         drive_id="drive1", provider="drive",
                         owner_scope=scope, owner_ref=ref,
                         dest_drive_id=dest, status=status)
    if created_at is not None:
        row.created_at = created_at
    if cooldown_until != "unset":
        row.cooldown_until = cooldown_until
    db.add(row)
    db.commit()
    return row


# ---------------------------------------------------------------------------
# 建单落字
# ---------------------------------------------------------------------------

def test_create_user_job_stamps_cooldown(db):
    item = _drive_item(db)
    _mount(db)
    _binding(db, 7)
    job = tv.create_user_job(db, item, 7, "drive")
    db.refresh(job)
    assert job.cooldown_until is not None
    assert abs((job.cooldown_until - job.created_at) - WINDOW) \
        < timedelta(seconds=2)


def test_create_manual_job_stamps_cooldown(db):
    item = _drive_item(db)
    job = tv.create_manual_job(db, item, "drive1")
    db.refresh(job)
    assert job.cooldown_until is not None
    assert abs((job.cooldown_until - job.created_at) - WINDOW) \
        < timedelta(seconds=2)


# ---------------------------------------------------------------------------
# 消费方改读字段（存量 NULL 行回退现算，结果与旧口径一致）
# ---------------------------------------------------------------------------

def test_recent_job_exists_field_governs(db):
    now = datetime.now()
    item = _drive_item(db)
    # 字段未到期：created 已过窗仍节流（字段说了算）
    _job(db, item.id, created_at=now - timedelta(hours=25),
         cooldown_until=now + timedelta(hours=1))
    assert tv._recent_job_exists(db, item.id, "source", None) is True


def test_recent_job_exists_field_expired_releases(db):
    now = datetime.now()
    item = _drive_item(db)
    # 字段已到期：created 还在窗内也不节流（字段说了算）
    _job(db, item.id, created_at=now - timedelta(hours=1),
         cooldown_until=now - timedelta(hours=1))
    assert tv._recent_job_exists(db, item.id, "source", None) is False


def test_recent_job_exists_legacy_null_falls_back(db):
    now = datetime.now()
    item = _drive_item(db)
    _job(db, item.id, created_at=now - timedelta(hours=1),
         cooldown_until=None)
    assert tv._recent_job_exists(db, item.id, "source", None) is True
    item2 = _drive_item(db)
    _job(db, item2.id, created_at=now - timedelta(hours=25),
         cooldown_until=None)
    assert tv._recent_job_exists(db, item2.id, "source", None) is False


def test_user_throttle_state_field_governs(db):
    now = datetime.now()
    item = _drive_item(db)
    dest = "userdrive:7"
    # 同方案历史单：created 已过窗、字段未到期 → blocked
    _job(db, item.id, scope="user", ref="7", dest=dest,
         created_at=now - timedelta(hours=25),
         cooldown_until=now + timedelta(hours=1))
    state, _ = tv._user_throttle_state(db, item.id, 7, dest)
    assert state == "blocked"
    # 同方案历史单：created 在窗内、字段已到期 → clear
    item2 = _drive_item(db)
    _job(db, item2.id, scope="user", ref="7", dest=dest,
         created_at=now - timedelta(hours=1),
         cooldown_until=now - timedelta(hours=1))
    state2, _ = tv._user_throttle_state(db, item2.id, 7, dest)
    assert state2 == "clear"


def test_retrigger_budget_field_governs(db):
    now = datetime.now()
    item = _drive_item(db)
    for _ in range(2):
        _job(db, item.id, scope="user", ref="7",
             created_at=now - timedelta(hours=25),
             cooldown_until=now + timedelta(hours=1))
    assert tv._gate_retrigger_budget_ok(db, item.id, 7) is False
    item2 = _drive_item(db)
    for _ in range(2):
        _job(db, item2.id, scope="user", ref="7",
             created_at=now - timedelta(hours=1),
             cooldown_until=now - timedelta(hours=1))
    assert tv._gate_retrigger_budget_ok(db, item2.id, 7) is True


# ---------------------------------------------------------------------------
# 工人终态落字（存量行收尾时补字）
# ---------------------------------------------------------------------------

def test_terminal_stamp_fills_legacy_row(db):
    created = datetime.now() - timedelta(hours=3)
    job = _job(db, 1, status="done", created_at=created,
               cooldown_until=None)
    tv._note_job_terminal_cooldown(job)
    assert job.cooldown_until is not None
    assert abs((job.cooldown_until - created) - WINDOW) \
        < timedelta(seconds=2)


def test_terminal_stamp_ignores_non_terminal(db):
    job = _job(db, 1, status="pending", cooldown_until=None)
    tv._note_job_terminal_cooldown(job)
    assert job.cooldown_until is None


# ---------------------------------------------------------------------------
# 变体保留期：模型列 + 迁移清单 + 清理器删前判断
# ---------------------------------------------------------------------------

def test_variant_retention_columns_and_migration():
    assert hasattr(em.TransferVariant, "retention_reason")
    assert hasattr(em.TransferVariant, "retention_until")
    assert hasattr(em.TransferJob, "cooldown_until")
    import inspect as _inspect
    from backend import database as _dbmod
    src = _inspect.getsource(_dbmod._auto_migrate)
    assert '("cooldown_until", "DATETIME", "NULL")' in src
    assert '("retention_reason", "VARCHAR(120)", "NULL")' in src
    assert '("retention_until", "DATETIME", "NULL")' in src


class _NotFound(Exception):
    pass


class _FakeClient:
    def __init__(self, existing=None):
        self.existing = set(existing or [])
        self.deleted = []
        self.get_calls = []

    def get_file(self, drive_marker, fid):
        self.get_calls.append((drive_marker, fid))
        if fid not in self.existing:
            raise _NotFound("404")
        return {"id": fid, "trashed": False, "size": 1000}

    def delete_file(self, drive_marker, fid):
        self.deleted.append((drive_marker, fid))
        self.existing.discard(fid)


def _variant(db, fid="copyfid", retention_until="unset",
             retention_reason=None):
    row = em.TransferVariant(item_id=11, source_file_id="origfid",
                             variant_file_id=fid,
                             variant_drive_id="userdrive:1",
                             owner_scope="user", owner_ref="1",
                             status="stale", size_bytes=1000)
    if retention_until != "unset":
        row.retention_until = retention_until
    if retention_reason is not None:
        row.retention_reason = retention_reason
    db.add(row)
    db.commit()
    return row


def test_cleanup_retention_blocks_delete(db):
    now = datetime.now()
    row = _variant(db, retention_until=now + timedelta(hours=1),
                   retention_reason="人工保留")
    client = _FakeClient(existing={"copyfid"})
    outcome, freed = tc._delete_one_drive_file(db, row, client, "d:1", now)
    assert outcome == "retained"
    assert freed == 0
    assert client.deleted == [] and client.get_calls == []
    db.refresh(row)
    assert row.status == "stale"


def test_cleanup_retention_expired_deletes(db):
    now = datetime.now()
    row = _variant(db, retention_until=now - timedelta(hours=1))
    client = _FakeClient(existing={"copyfid"})
    outcome, _ = tc._delete_one_drive_file(db, row, client, "d:1", now)
    assert outcome == "deleted"
    assert client.deleted == [("d:1", "copyfid")]
    db.refresh(row)
    assert row.status == "deleted"


def test_cleanup_no_retention_deletes(db):
    now = datetime.now()
    row = _variant(db)
    client = _FakeClient(existing={"copyfid"})
    outcome, _ = tc._delete_one_drive_file(db, row, client, "d:1", now)
    assert outcome == "deleted"
    db.refresh(row)
    assert row.status == "deleted"
