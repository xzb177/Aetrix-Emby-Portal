# -*- coding: utf-8 -*-
"""首播门禁：trigger 提升 + unsupported 签名自愈（2026-10-30 修复包 PR-A）。

背景（生产实证，见 reports/pan115-firstplay-fix-plan-20261030.md）：
- 详情页预建的 prefetch 单占位后，真点播走到门禁只回
  preparing/job_in_flight，trigger 不提升 → 该单在认领排序里恒在
  playback 组之后，用户正在点的片反而排不上。
- #1539 分片上线前失败的 failed/unsupported 存量单（文案含已退役
  签名「多段上传…（未实现）」）卡在门禁终态集里，能力补齐后也不
  自愈，用户再点播只会撞回 failed。

本文件钉住：
① 跨 provider：prefetch 在途 + 点播 → trigger 就地变 playback、
   只回 preparing/job_in_flight、不重复建单、created_at 不动；
② 同 provider 同款一条；store_want 同样可提升；
③ failed/unsupported + 已退役签名 detail + 分片开关开 →
   preparing/job_retriggered 且新建 playback 单（跨/同各一条）；
④ 签名不命中（大小未知）→ 仍 failed（锚）；
⑤ 分片开关关 → 仍 failed（锚）；
⑥ 重触发预算耗尽（24h 同桶 2 行）→ 回 failed 不建单（锚）；
⑦ running 单不提升（锚）；系统触发（quota）不提升（锚）。

全部离线：sqlite 内存库；monkeypatch 一律 patch 类/模块属性；
fixture 自带 store 缓存失效，不依赖其它测试文件的状态。
"""
import os

os.environ.setdefault("DATABASE_TYPE", "sqlite")
os.environ.setdefault("REDIS_ENABLED", "false")

import logging
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend import models as base_models
from backend.emby_server import models as em
from backend.emby_server import transfer_variant as tv
from backend.integrations import store


@pytest.fixture()
def db():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False},
                           poolclass=StaticPool)
    base_models.Base.metadata.create_all(engine)
    em.Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    store.invalidate()
    yield session
    session.close()
    store.invalidate()


_counter = [0]

#: #1539 上线前旧代码写下的失败文案（生产存量 18 行同签名）。
RETIRED_DETAIL = (
    "文件 5605322158 字节超过 115 简单上传 5 GiB 上限且秒传未命中；"
    "多段上传需 initupload OSS 协议（未实现），暂不支持")
#: 不命中的 unsupported：文件本身搬不了，不是能力没造好。
UNKNOWN_SIZE_DETAIL = "Drive 源文件大小未知/为零，无法分段搬运（不猜大小）"


def _item(db, **kw):
    _counter[0] += 1
    row = em.MediaItem(guid=f"pr-{_counter[0]}", library_id=1,
                       item_type="movie", name="测试片",
                       drive_file_id="origfid",
                       file_path="mount://3/电影/a.mkv", size=1000)
    for key, value in kw.items():
        setattr(row, key, value)
    db.add(row)
    db.commit()
    return row


def _mount(db, mount_id, mount_type, path):
    row = em.StorageMount(id=mount_id, name=f"m{mount_id}",
                          mount_type=mount_type, path=path)
    db.add(row)
    db.commit()
    return row


def _binding(db, user_id, provider="drive", status="active"):
    cred = em.MountCredential(scope="user", owner_user_id=user_id,
                              status="active")
    db.add(cred)
    db.commit()
    row = em.UserStorageBinding(user_id=user_id, provider=provider,
                                account_label="test",
                                credential_id=cred.id,
                                status=status)
    db.add(row)
    db.commit()
    return row


def _user(user_id=7):
    return SimpleNamespace(id=user_id)


def _enable(monkeypatch):
    monkeypatch.setenv("TRANSFER_VARIANT_ENABLED", "1")


def _job(db, item_id, user_id, *, provider="drive", dest=None,
         status="pending", trigger="prefetch", error_kind=None,
         error_detail=None, created_at=None):
    if dest is None:
        dest = (f"pan115:user-{user_id}" if provider == "drive"
                else f"userdrive:{user_id}")
    row = em.TransferJob(item_id=item_id, trigger=trigger,
                         drive_id="drive1", provider=provider,
                         owner_scope="user", owner_ref=str(user_id),
                         dest_drive_id=dest, status=status)
    if error_kind is not None:
        row.error_kind = error_kind
    if error_detail is not None:
        row.error_detail = error_detail
    if created_at is not None:
        row.created_at = created_at
    db.add(row)
    db.commit()
    return row


def _set_sysconfig(db, key, value):
    row = (db.query(base_models.SystemConfig)
           .filter(base_models.SystemConfig.key == key).first())
    if row is None:
        db.add(base_models.SystemConfig(key=key, value=value))
    else:
        row.value = value
    db.commit()
    store.invalidate()


def _cross_setup(db, monkeypatch):
    """只绑 115 的用户播 Drive 条目 → 门禁跨 provider 分支。"""
    _enable(monkeypatch)
    _mount(db, 3, "drive", "drive:drive1")
    item = _item(db)
    _binding(db, 7, provider="pan115")
    return item


def _same_setup(db, monkeypatch):
    """绑 Drive 的用户播 Drive 条目 → 门禁同 provider 分支。"""
    _enable(monkeypatch)
    _mount(db, 3, "drive", "drive:drive1")
    item = _item(db)
    _binding(db, 7, provider="drive")
    return item


# ---------- ①② trigger 提升 ----------

def test_gate_cross_prefetch_promoted_on_playback(db, monkeypatch, caplog):
    item = _cross_setup(db, monkeypatch)
    created = datetime.now() - timedelta(minutes=30)
    job = _job(db, item.id, 7, status="pending", trigger="prefetch",
               created_at=created)
    with caplog.at_level(logging.INFO):
        gate = tv.ensure_user_copy_gate(db, item, _user())
    assert gate["verdict"] == "preparing"
    assert gate["reason"] == "job_in_flight"
    assert gate["job_id"] == job.id
    assert db.query(em.TransferJob).count() == 1  # 不重复建单
    db.refresh(job)
    assert job.trigger == "playback"  # 就地提升
    assert job.created_at == created  # 等待时长口径诚实，不动
    assert "byos_promote" in caplog.text
    assert "from=prefetch" in caplog.text


def test_gate_same_prefetch_promoted_on_playback(db, monkeypatch):
    item = _same_setup(db, monkeypatch)
    job = _job(db, item.id, 7, dest="userdrive:7", status="pending",
               trigger="prefetch")
    gate = tv.ensure_user_copy_gate(db, item, _user())
    assert gate["verdict"] == "preparing"
    assert gate["reason"] == "job_in_flight"
    assert gate["job_id"] == job.id
    assert db.query(em.TransferJob).count() == 1
    db.refresh(job)
    assert job.trigger == "playback"


def test_gate_same_store_want_promoted_on_playback(db, monkeypatch):
    item = _same_setup(db, monkeypatch)
    job = _job(db, item.id, 7, dest="userdrive:7", status="pending",
               trigger="store_want")
    gate = tv.ensure_user_copy_gate(db, item, _user())
    assert gate["reason"] == "job_in_flight"
    db.refresh(job)
    assert job.trigger == "playback"


# ---------- ⑦ 锚：不提升的情形 ----------

def test_gate_running_prefetch_not_promoted(db, monkeypatch):
    item = _cross_setup(db, monkeypatch)
    job = _job(db, item.id, 7, status="running", trigger="prefetch")
    gate = tv.ensure_user_copy_gate(db, item, _user())
    assert gate["verdict"] == "preparing"
    assert gate["reason"] == "job_in_flight"
    db.refresh(job)
    assert job.trigger == "prefetch"


def test_gate_pending_quota_not_promoted(db, monkeypatch):
    # 系统触发的后台单不许被一次路过点播改性。
    item = _same_setup(db, monkeypatch)
    job = _job(db, item.id, 7, dest="userdrive:7", status="pending",
               trigger="quota")
    gate = tv.ensure_user_copy_gate(db, item, _user())
    assert gate["reason"] == "job_in_flight"
    db.refresh(job)
    assert job.trigger == "quota"


# ---------- ③ unsupported 签名自愈 ----------

def test_gate_cross_failed_unsupported_retired_signature_revives(
        db, monkeypatch):
    item = _cross_setup(db, monkeypatch)
    _job(db, item.id, 7, status="failed", trigger="prefetch",
         error_kind="unsupported", error_detail=RETIRED_DETAIL)
    gate = tv.ensure_user_copy_gate(db, item, _user())
    assert gate["verdict"] == "preparing"
    assert gate["reason"] == "job_retriggered"
    assert db.query(em.TransferJob).count() == 2
    new_job = db.query(em.TransferJob).order_by(
        em.TransferJob.id.desc()).first()
    assert new_job.status == "pending" and new_job.trigger == "playback"
    assert new_job.dest_drive_id == "pan115:user-7"
    assert tv._is_cross_job(new_job) is True
    assert gate["job_id"] == new_job.id


def test_gate_same_failed_unsupported_retired_signature_revives(
        db, monkeypatch):
    item = _same_setup(db, monkeypatch)
    _job(db, item.id, 7, dest="userdrive:7", status="failed",
         trigger="prefetch", error_kind="unsupported",
         error_detail=RETIRED_DETAIL)
    gate = tv.ensure_user_copy_gate(db, item, _user())
    assert gate["verdict"] == "preparing"
    assert gate["reason"] == "job_retriggered"
    assert db.query(em.TransferJob).count() == 2
    new_job = db.query(em.TransferJob).order_by(
        em.TransferJob.id.desc()).first()
    assert new_job.status == "pending" and new_job.trigger == "playback"
    assert new_job.dest_drive_id == "userdrive:7"


# ---------- ④⑤⑥ 锚：不复活的情形 ----------

def test_gate_failed_unsupported_unknown_signature_stays_failed(
        db, monkeypatch):
    item = _cross_setup(db, monkeypatch)
    job = _job(db, item.id, 7, status="failed", trigger="prefetch",
               error_kind="unsupported", error_detail=UNKNOWN_SIZE_DETAIL)
    gate = tv.ensure_user_copy_gate(db, item, _user())
    assert gate["verdict"] == "failed"
    assert gate["reason"] == "unsupported"
    assert gate["job_id"] == job.id
    assert db.query(em.TransferJob).count() == 1


def test_gate_failed_unsupported_switch_off_stays_failed(db, monkeypatch):
    item = _cross_setup(db, monkeypatch)
    _set_sysconfig(db, "pan115_multipart_upload_enabled", "false")
    job = _job(db, item.id, 7, status="failed", trigger="prefetch",
               error_kind="unsupported", error_detail=RETIRED_DETAIL)
    gate = tv.ensure_user_copy_gate(db, item, _user())
    assert gate["verdict"] == "failed"
    assert gate["reason"] == "unsupported"
    assert gate["job_id"] == job.id
    assert db.query(em.TransferJob).count() == 1


def test_gate_failed_unsupported_budget_exhausted_stays_failed(
        db, monkeypatch):
    # 24h 同桶已有 2 行（旧行 + 本失败行）：预算耗尽，不再建单。
    item = _cross_setup(db, monkeypatch)
    _job(db, item.id, 7, status="done", trigger="playback")
    job = _job(db, item.id, 7, status="failed", trigger="prefetch",
               error_kind="unsupported", error_detail=RETIRED_DETAIL)
    gate = tv.ensure_user_copy_gate(db, item, _user())
    assert gate["verdict"] == "failed"
    assert gate["job_id"] == job.id
    assert db.query(em.TransferJob).count() == 2
