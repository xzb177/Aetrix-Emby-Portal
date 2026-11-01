# -*- coding: utf-8 -*-
"""转存等待压缩 期一（纯调度，2026-11-01 方案
reports/transfer-wait-compression-design-20261101.md §6 期一）。

钉住改后口径：
- _pick_candidates 分层：可探针跨单（条目有可信 sha1 或清单
  sha1，判据 _job_probeable_sha1）在同 owner 组内优先于普通
  跨单；普通跨单对跨槽的占用受 cross_cap-1 约束（预留至少
  1 槽给可探针单）；probeable_ids 不给时挑单逐字旧行为。
- 建单日志带指纹状态（transfer_create ... sha1_present=0/1），
  最小侵入不落库新字段。
- claimed_at 在批量认领与当场先办认领时落值（纯观测列）。

期二（探针道执行面）用例在 tests/test_transfer_probe_lane.py。
全部离线：SQLite :memory:。
"""
import os

os.environ.setdefault("DATABASE_TYPE", "sqlite")
os.environ.setdefault("REDIS_ENABLED", "false")

import hashlib
import logging

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend import models as base_models
from backend.emby_server import models as em
from backend.emby_server import transfer_variant as tv
from backend.integrations import store

_TV_LOGGER = "backend.emby_server.transfer_variant"

_PAYLOAD = bytes(range(256)) * 4  # 1024 字节
_PAYLOAD_SHA1 = hashlib.sha1(_PAYLOAD).hexdigest()


@pytest.fixture()
def db_factory():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False},
                           poolclass=StaticPool)
    base_models.Base.metadata.create_all(engine)
    em.Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)


@pytest.fixture()
def db(db_factory):
    session = db_factory()
    yield session
    session.close()


@pytest.fixture(autouse=True)
def _clean_state():
    store.invalidate()
    tv._paused_until.clear()
    tv._WAKE_EVENT.clear()
    yield
    store.invalidate()
    tv._paused_until.clear()
    tv._WAKE_EVENT.clear()


def _item(db, sha1=_PAYLOAD_SHA1, **kw):
    row = em.MediaItem(guid=f"probe-{kw.pop('tag', id(object()))}",
                       library_id=1, item_type="movie", name="探针测试片",
                       drive_file_id="origfid",
                       file_path="mount://3/电影/probe.mkv",
                       size=len(_PAYLOAD))
    if sha1:
        row.content_hash = sha1
        row.content_hash_algo = "sha1"
    for key, value in kw.items():
        setattr(row, key, value)
    db.add(row)
    db.commit()
    return row


def _binding(db, user_id, provider="pan115", status="active"):
    cred = em.MountCredential(scope="user", owner_user_id=user_id,
                              status="active")
    db.add(cred)
    db.commit()
    row = em.UserStorageBinding(user_id=user_id, provider=provider,
                                account_label="test",
                                credential_id=cred.id, status=status)
    db.add(row)
    db.commit()
    return row


def _cross_job(db, item_id, user_id, **kw):
    kw.setdefault("status", "pending")
    row = em.TransferJob(item_id=item_id, trigger="playback",
                         drive_id="shared1", provider="drive",
                         owner_scope="user", owner_ref=str(user_id),
                         dest_drive_id=f"pan115:user-{user_id}", **kw)
    db.add(row)
    db.commit()
    return row


def _enable(monkeypatch):
    monkeypatch.setenv("TRANSFER_VARIANT_ENABLED", "1")


# ---------- 挑单分层：可探针优先 + 预留槽 + 无分层时不变 ----------

def test_pick_candidates_probeable_priority_and_reserve(db):
    """跨槽 cap=2：两个不可探针跨单 + 一个可探针跨单。
    不可探针单最多用 cap-1=1 槽（预留 1），可探针单必被挑中。"""
    plain_a = _cross_job(db, 101, 11)
    plain_b = _cross_job(db, 102, 12)
    probe_c = _cross_job(db, 103, 13)
    picked = tv._pick_candidates(
        [plain_a, plain_b, probe_c], 10, {}, 10,
        cross_cap=2, cross_running=0,
        probeable_ids={id(probe_c)})
    picked_ids = {j.id for j in picked}
    assert probe_c.id in picked_ids
    plain_picked = [j for j in picked if j.id in (plain_a.id, plain_b.id)]
    assert len(plain_picked) == 1  # 预留槽：不可探针只占 1 个


def test_pick_candidates_without_probeable_ids_unchanged(db):
    a = _cross_job(db, 101, 11)
    b = _cross_job(db, 102, 12)
    c = _cross_job(db, 103, 13)
    picked = tv._pick_candidates([a, b, c], 10, {}, 10,
                                 cross_cap=2, cross_running=0)
    assert len(picked) == 2  # 无探针分层时仍是先到先得占满 2 槽


def test_pick_candidates_same_owner_probeable_first(db):
    """同 owner 两单：不可探针（id 小）在前、可探针在后——
    分层后可探针单在同 owner 内先被挑。"""
    plain = _cross_job(db, 101, 11)
    probe = _cross_job(db, 102, 11)
    picked = tv._pick_candidates(
        [plain, probe], 1, {}, 10, cross_cap=3, cross_running=0,
        probeable_ids={id(probe)})
    assert [j.id for j in picked] == [probe.id]


# ---------- claimed_at 落值 ----------

def test_claim_due_jobs_sets_claimed_at(db, monkeypatch):
    _enable(monkeypatch)
    item = _item(db)
    _binding(db, 7)
    job = _cross_job(db, item.id, 7)
    claimed = tv.claim_due_jobs(db)
    assert claimed == [job.id]
    db.refresh(job)
    assert job.status == "running"
    assert job.claimed_at is not None


def test_claim_job_for_inline_sets_claimed_at(db):
    job = em.TransferJob(item_id=101, trigger="playback",
                         drive_id="shared1", provider="drive",
                         owner_scope="user", owner_ref="7",
                         dest_drive_id="userdrive:7", status="pending")
    db.add(job)
    db.commit()
    assert tv.claim_job_for_inline(db, job.id) is True
    db.refresh(job)
    assert job.status == "running"
    assert job.claimed_at is not None


# ---------- 建单日志带指纹状态 ----------

def test_create_cross_job_logs_sha1_present(db, monkeypatch, caplog):
    _enable(monkeypatch)
    # 源盘标识推导不在本用例范围（挂载行口径另有专测），桩掉只
    # 钉建单日志的指纹状态位。
    monkeypatch.setattr(tv, "_source_drive_id",
                        lambda db_arg, item_arg, provider: "shared1")
    item = _item(db)
    _binding(db, 7)
    with caplog.at_level(logging.INFO, logger=_TV_LOGGER):
        job = tv.create_cross_provider_job(db, item, 7, "drive")
    assert "sha1_present=1" in caplog.text
    assert f"job={job.id}" in caplog.text


def test_create_cross_job_logs_sha1_absent(db, monkeypatch, caplog):
    _enable(monkeypatch)
    monkeypatch.setattr(tv, "_source_drive_id",
                        lambda db_arg, item_arg, provider: "shared1")
    item = _item(db, sha1=None)
    _binding(db, 7)
    with caplog.at_level(logging.INFO, logger=_TV_LOGGER):
        tv.create_cross_provider_job(db, item, 7, "drive")
    assert "sha1_present=0" in caplog.text
