# -*- coding: utf-8 -*-
"""首播门禁：同 provider 终态失败必须透传真实原因，不许包装成准备中。

背景（2026-11-01 生产实证）：user 739 播《Stray Kids：主宰一切
的体验》(951931，16.5GB)，转存单 1714 failed/insufficient_space，
error_detail 明写「用户盘空间不足：文件 16491976779 字节，剩余
11963971786 字节…」，但门禁同 provider 分支把不在
_TERMINAL_JOB_KINDS 里的终态 kind（insufficient_space /
no_binding）归 preparing/throttled 且不带 detail，取流只回通用
句「转存准备中：正在把文件转存到你的绑定盘，转存完成后可播」
——空间不足等再久也不会好，用户永远干等。跨 provider 分支的
_cross_failed_detail 则会透传「转存到你的{目标}失败：{原因}」，
两边口径不一致。

终态口径以代码既有定义为准，不新造分类：
- _TERMINAL_JOB_KINDS：门禁既有终态集（此前同分支命中时只回
  failed 不带 detail，本单起同样带 detail）；
- insufficient_space：_SpaceInsufficient 明定终态（只经
  _fail_job 落行、绝不转重试）；
- no_binding：_AUTH_TERMINAL_KINDS 成员、只经 _fail_job 落行、
  认领前复核「秒判终态」，detail 本就含重绑指引。

本文件钉住：
① 同 provider failed/insufficient_space → failed + detail 含
   空间不足原文，经取流映射（raise_for_transfer_gate）回 409
   且 detail 含空间不足原因（红点）；
② 同 provider failed/no_binding → failed + detail 含重绑指引；
③ 同 provider 既有终态集 kind（dead）→ failed + detail 同构
   （「转存到你的谷歌盘失败：{原文}」）；
④ 同 provider（115）failed/insufficient_space → failed +
   detail 用 115 网盘名；
⑤ 在途单（pending/running）仍 preparing、无 detail、取流映射
   回 409 通用准备中句（行为不变锚）；
⑥ failed/retriable 未耗尽仍走重触发回 preparing（行为不变锚）；
⑦ 跨 provider 分支 failed/insufficient_space 仍 preparing/
   throttled（本单不动跨分支的锚）。

全部离线：sqlite 内存库；monkeypatch 一律 patch 类/模块属性；
fixture 自带 store 缓存失效，不依赖其它测试文件的状态。
"""
import os

os.environ.setdefault("DATABASE_TYPE", "sqlite")
os.environ.setdefault("REDIS_ENABLED", "false")

from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend import models as base_models
from backend.emby_server import models as em
from backend.emby_server import playback_gate as pg
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

#: 生产单 1714 的 error_detail 同款（空间三数报清、明示不回落）。
SPACE_DETAIL = (
    "用户盘空间不足：文件 16491976779 字节，剩余 11963971786 字节，"
    "已腾无可腾（未静默回落共享）")
#: 工人落 no_binding 时的原文（_fail_job 各执行器同口径，含重绑指引）。
NO_BINDING_DETAIL = (
    "用户 Drive 绑定已失效或不存在（任务创建后解绑，"
    "不重试；重绑后下次播放节流窗外可重建）")
#: 终态集既有 kind 的原文示例。
DEAD_DETAIL = "源文件已不存在或无权访问（copy 404）"
#: 取流映射的通用准备中句（raise_for_transfer_gate 缺省文案）。
GENERIC_PREPARING_DETAIL = "转存准备中：正在把文件转存到你的绑定盘，转存完成后可播"


def _item(db, **kw):
    _counter[0] += 1
    row = em.MediaItem(guid=f"fd-{_counter[0]}", library_id=1,
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
         status="pending", trigger="playback", error_kind=None,
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


def _same_setup(db, monkeypatch):
    """绑 Drive 的用户播 Drive 条目 → 门禁同 provider 分支。"""
    _enable(monkeypatch)
    _mount(db, 3, "drive", "drive:drive1")
    item = _item(db)
    _binding(db, 7, provider="drive")
    return item


def _same115_setup(db, monkeypatch):
    """绑 115 的用户播 115 条目 → 门禁同 provider 分支。"""
    _enable(monkeypatch)
    item = _item(db, file_path="115:/电影/a.mkv")
    _binding(db, 7, provider="pan115")
    return item


def _cross_setup(db, monkeypatch):
    """只绑 115 的用户播 Drive 条目 → 门禁跨 provider 分支。"""
    _enable(monkeypatch)
    _mount(db, 3, "drive", "drive:drive1")
    item = _item(db)
    _binding(db, 7, provider="pan115")
    return item


def _raise(gate):
    with pytest.raises(HTTPException) as exc_info:
        pg.raise_for_transfer_gate(gate)
    return exc_info.value


# ---------- ① 红点：空间不足必须透传 ----------

def test_gate_same_failed_insufficient_space_failed_with_detail(
        db, monkeypatch):
    item = _same_setup(db, monkeypatch)
    job = _job(db, item.id, 7, dest="userdrive:7", status="failed",
               error_kind="insufficient_space", error_detail=SPACE_DETAIL,
               created_at=datetime.now() - timedelta(minutes=5))
    gate = tv.ensure_user_copy_gate(db, item, _user())
    assert gate["verdict"] == "failed"
    assert gate["reason"] == "insufficient_space"
    assert gate["job_id"] == job.id
    # detail 与跨 provider 分支同构：明示失败 + 任务行原因原文。
    assert gate["detail"] == f"转存到你的谷歌盘失败：{SPACE_DETAIL}"
    assert db.query(em.TransferJob).count() == 1  # 不重复建单
    # 取流口径：409 且 detail 带空间不足原因，不是通用准备中句。
    exc = _raise(gate)
    assert exc.status_code == 409
    assert "空间不足" in exc.detail
    assert "16491976779" in exc.detail
    assert exc.detail != GENERIC_PREPARING_DETAIL


# ---------- ② no_binding 透传重绑指引 ----------

def test_gate_same_failed_no_binding_failed_with_rebind_detail(
        db, monkeypatch):
    item = _same_setup(db, monkeypatch)
    job = _job(db, item.id, 7, dest="userdrive:7", status="failed",
               error_kind="no_binding", error_detail=NO_BINDING_DETAIL,
               created_at=datetime.now() - timedelta(minutes=5))
    gate = tv.ensure_user_copy_gate(db, item, _user())
    assert gate["verdict"] == "failed"
    assert gate["reason"] == "no_binding"
    assert gate["job_id"] == job.id
    assert "重绑" in gate["detail"]
    assert NO_BINDING_DETAIL in gate["detail"]
    assert db.query(em.TransferJob).count() == 1
    exc = _raise(gate)
    assert exc.status_code == 409
    assert "重绑" in exc.detail


# ---------- ③ 既有终态集 kind 现在带 detail ----------

def test_gate_same_failed_terminal_kind_carries_detail(db, monkeypatch):
    item = _same_setup(db, monkeypatch)
    job = _job(db, item.id, 7, dest="userdrive:7", status="failed",
               error_kind="dead", error_detail=DEAD_DETAIL,
               created_at=datetime.now() - timedelta(minutes=5))
    gate = tv.ensure_user_copy_gate(db, item, _user())
    assert gate["verdict"] == "failed"
    assert gate["reason"] == "dead"
    assert gate["job_id"] == job.id
    assert gate["detail"] == f"转存到你的谷歌盘失败：{DEAD_DETAIL}"
    exc = _raise(gate)
    assert exc.status_code == 409
    assert DEAD_DETAIL in exc.detail


# ---------- ④ 115 同 provider 同款 ----------

def test_gate_same115_failed_insufficient_space_failed_with_detail(
        db, monkeypatch):
    item = _same115_setup(db, monkeypatch)
    job = _job(db, item.id, 7, provider="pan115", dest="pan115:user-7",
               status="failed", error_kind="insufficient_space",
               error_detail=SPACE_DETAIL,
               created_at=datetime.now() - timedelta(minutes=5))
    gate = tv.ensure_user_copy_gate(db, item, _user())
    assert gate["verdict"] == "failed"
    assert gate["reason"] == "insufficient_space"
    assert gate["job_id"] == job.id
    assert gate["detail"] == f"转存到你的115 网盘失败：{SPACE_DETAIL}"
    exc = _raise(gate)
    assert exc.status_code == 409
    assert "空间不足" in exc.detail


# ---------- ⑤ 锚：在途单仍 preparing 通用句 ----------

def test_gate_same_inflight_still_preparing_generic(db, monkeypatch):
    _same_setup(db, monkeypatch)
    for status in ("pending", "running"):
        item = _item(db)
        job = _job(db, item.id, 7, dest="userdrive:7", status=status,
                   trigger="playback")
        gate = tv.ensure_user_copy_gate(db, item, _user())
        assert gate["verdict"] == "preparing"
        assert gate["reason"] == "job_in_flight"
        assert gate["job_id"] == job.id
        assert "detail" not in gate  # 同 provider 在途不带 detail（旧形）
        exc = _raise(gate)
        assert exc.status_code == 409
        assert exc.detail == GENERIC_PREPARING_DETAIL


# ---------- ⑥ 锚：retriable 未耗尽仍走重触发 ----------

def test_gate_same_failed_retriable_retriggers_preparing(db, monkeypatch):
    item = _same_setup(db, monkeypatch)
    old = _job(db, item.id, 7, dest="userdrive:7", status="failed",
               trigger="prefetch", error_kind="retriable",
               error_detail="Drive API 503 临时失败",
               created_at=datetime.now() - timedelta(minutes=5))
    gate = tv.ensure_user_copy_gate(db, item, _user())
    assert gate["verdict"] == "preparing"
    assert gate["reason"] == "job_retriggered"
    assert gate["job_id"] != old.id
    assert db.query(em.TransferJob).count() == 2
    new_job = db.query(em.TransferJob).order_by(
        em.TransferJob.id.desc()).first()
    assert new_job.status == "pending" and new_job.trigger == "playback"


# ---------- ⑦ 锚：跨 provider 分支不动 ----------

def test_gate_cross_failed_insufficient_space_still_preparing(
        db, monkeypatch):
    item = _cross_setup(db, monkeypatch)
    job = _job(db, item.id, 7, status="failed", trigger="prefetch",
               error_kind="insufficient_space", error_detail=SPACE_DETAIL)
    gate = tv.ensure_user_copy_gate(db, item, _user())
    assert gate["verdict"] == "preparing"
    assert gate["reason"] == "throttled"
    assert gate["job_id"] == job.id
    assert "正在转存到你的115 网盘" in gate["detail"]
    assert db.query(em.TransferJob).count() == 1
