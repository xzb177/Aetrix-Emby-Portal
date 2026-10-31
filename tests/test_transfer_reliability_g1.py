# -*- coding: utf-8 -*-
"""蓝图 v2 第一层·可靠 治理一单（2026-10-31）。

钉住的口径（依据：转存失败分类抽样复核报告
reports/transfer-failure-classification-review-20261031.md）：

- retriable 错误指纹熔断：确定性错误（Drive 列目录 401、建目录
  401/403、115 initupload 响应解析失败）重试永远修不好，首次
  失败即转终态，不再空烧数小时；真瞬态（5xx 等）保持现行
  有界重试节奏不变。
- no_binding 双前置校验：建单（create_user_job /
  create_cross_provider_job）与认领（claim_due_jobs）两处都查
  目标 provider 的 active 绑定，无绑定秒判、不进执行——现状
  平均拖 129 分钟、最高重试 4 次才落终态。

全部离线：SQLite :memory: + 行级构造，不碰真实 Drive/115。
"""
import os

os.environ.setdefault("DATABASE_TYPE", "sqlite")
os.environ.setdefault("REDIS_ENABLED", "false")

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


def _mount(db, mount_id=3, mount_type="drive", path="drive:drive1"):
    row = em.StorageMount(id=mount_id, name=f"m{mount_id}",
                          mount_type=mount_type, path=path)
    db.add(row)
    db.commit()
    return row


def _item(db, **kw):
    row = em.MediaItem(guid="g1-rel", library_id=1, item_type="movie",
                       name="测试片", drive_file_id="origfid",
                       file_path="mount://3/电影/a.mkv", size=1000)
    for key, value in kw.items():
        setattr(row, key, value)
    db.add(row)
    db.commit()
    return row


def _job(db, **kw):
    defaults = dict(item_id=1, trigger="playback", drive_id="drive1",
                    provider="drive", owner_scope="user", owner_ref="7",
                    dest_drive_id="userdrive:7", status="pending",
                    attempts=0)
    defaults.update(kw)
    row = em.TransferJob(**defaults)
    db.add(row)
    db.commit()
    return row


def _fresh(db, job_id):
    return (db.query(em.TransferJob)
            .filter(em.TransferJob.id == job_id).first())


# ---------- retriable 错误指纹熔断 ----------

def test_breaker_list_dir_401_terminal_auth_error(db):
    """列转存目录 401 = 令牌已死：首次失败即 failed/auth_error，
    不进退避重试（复核实证该组平均空烧 436 分钟）。"""
    job = _job(db, attempts=1)
    outcome = tv._schedule_retry(
        db, job, tv._Retriable("列转存目录失败：401 abc"))
    assert outcome == "failed"
    fresh = _fresh(db, job.id)
    assert fresh.status == "failed"
    assert fresh.error_kind == "auth_error"
    assert fresh.next_retry_at is None


def test_breaker_mkdir_403_terminal_auth_error(db):
    """建转存目录 403 = 只读令牌无写权限：确定性错误，即终态。"""
    job = _job(db, attempts=1)
    outcome = tv._schedule_retry(
        db, job, tv._Retriable("建转存目录失败：403 abc"))
    assert outcome == "failed"
    fresh = _fresh(db, job.id)
    assert fresh.status == "failed"
    assert fresh.error_kind == "auth_error"
    assert fresh.next_retry_at is None


def test_breaker_mkdir_401_terminal_auth_error(db):
    """建转存目录 401 与列目录 401 同族（凭据已死），即终态。"""
    job = _job(db, attempts=1)
    outcome = tv._schedule_retry(
        db, job, tv._Retriable("建转存目录失败：401 abc"))
    assert outcome == "failed"
    assert _fresh(db, job.id).error_kind == "auth_error"


def test_breaker_initupload_parse_terminal_unsupported(db):
    """115 initupload 响应解析失败 = 协议/客户端确定性错误：
    即终态 unsupported，不再重试（复核 8 单平均空烧 116 分钟）。"""
    job = _job(db, attempts=1)
    outcome = tv._schedule_retry(
        db, job, Exception("115 initupload 响应解析失败："
                           "数据块长度非 AES 块长整数倍"))
    assert outcome == "failed"
    fresh = _fresh(db, job.id)
    assert fresh.status == "failed"
    assert fresh.error_kind == "unsupported"
    assert fresh.next_retry_at is None


def test_transient_mkdir_500_still_retried(db):
    """建目录 500 是服务端瞬态：不许被指纹误杀，保持退避重试。"""
    job = _job(db, attempts=1)
    outcome = tv._schedule_retry(
        db, job, tv._Retriable("建转存目录失败：500 abc"))
    assert outcome == "retried"
    fresh = _fresh(db, job.id)
    assert fresh.status == "pending"
    assert fresh.error_kind == "retriable"
    assert fresh.next_retry_at is not None


def test_transient_list_dir_503_still_retried(db):
    """列目录 503 同理：保持现行重试节奏。"""
    job = _job(db, attempts=1)
    outcome = tv._schedule_retry(
        db, job, tv._Retriable("列转存目录失败：503 abc"))
    assert outcome == "retried"
    fresh = _fresh(db, job.id)
    assert fresh.status == "pending"
    assert fresh.error_kind == "retriable"


def test_transient_exhaustion_still_failed_retriable(db):
    """真瞬态烧满 attempts：维持既有 failed/retriable 落点
    （门禁靠它走预算内重触发，口径不动）。"""
    job = _job(db, attempts=3)
    outcome = tv._schedule_retry(
        db, job, tv._Retriable("copy 失败：503"))
    assert outcome == "failed"
    fresh = _fresh(db, job.id)
    assert fresh.status == "failed"
    assert fresh.error_kind == "retriable"


# ---------- no_binding 建单前置校验 ----------

def test_create_user_job_no_binding_rejected(db):
    """无目标盘绑定：建单直接抛 no_binding，不落任务行。"""
    _mount(db)
    item = _item(db)
    with pytest.raises(tv.TransferError) as excinfo:
        tv.create_user_job(db, item, 7, "drive", trigger="playback")
    assert excinfo.value.kind == "no_binding"
    assert db.query(em.TransferJob).count() == 0


def test_create_user_job_expired_binding_rejected(db):
    """绑定行在但非 active（已失效）：同判 no_binding。"""
    _mount(db)
    item = _item(db)
    _binding(db, 7, provider="drive", status="expired")
    with pytest.raises(tv.TransferError) as excinfo:
        tv.create_user_job(db, item, 7, "drive", trigger="playback")
    assert excinfo.value.kind == "no_binding"
    assert db.query(em.TransferJob).count() == 0


def test_create_user_job_with_binding_ok(db):
    """有 active 绑定：正常建单（校验不误伤正常路径）。"""
    _mount(db)
    item = _item(db)
    _binding(db, 7, provider="drive")
    job = tv.create_user_job(db, item, 7, "drive", trigger="playback")
    assert job.status == "pending"
    assert job.dest_drive_id == "userdrive:7"


def test_create_cross_job_dest_no_binding_rejected(db):
    """跨单 Drive 源→115 盘：只有 Drive 绑定、没有 115 绑定时，
    目标盘无绑定 → no_binding 秒判，不落行（复核样本原型：
    只有某盘却往另一盘转）。"""
    _mount(db)
    item = _item(db)
    _binding(db, 7, provider="drive")
    with pytest.raises(tv.TransferError) as excinfo:
        tv.create_cross_provider_job(db, item, 7, "drive",
                                     trigger="playback")
    assert excinfo.value.kind == "no_binding"
    assert db.query(em.TransferJob).count() == 0


def test_create_cross_job_with_dest_binding_ok(db):
    """跨单目标盘有绑定：正常建单。"""
    _mount(db)
    item = _item(db)
    _binding(db, 7, provider="pan115")
    job = tv.create_cross_provider_job(db, item, 7, "drive",
                                       trigger="playback")
    assert job.status == "pending"
    assert job.dest_drive_id == "pan115:user-7"


def test_create_cross_job_115_source_dest_drive_ok(db):
    """跨单 115 源→Drive 盘：Drive 绑定在即可建（fastlane 既有
    用例的语义在补齐绑定前置后继续成立）。"""
    item = SimpleNamespace(id=42, drive_file_id="fid1",
                           file_path="115:/电影/a.mkv")
    _binding(db, 7, provider="drive")
    job = tv.create_cross_provider_job(db, item, 7, "pan115")
    assert job.status == "pending"
    assert job.dest_drive_id == "userdrive:7"


# ---------- no_binding 认领前置校验 ----------

def test_claim_sweeps_user_job_without_binding(db):
    """认领前复核：user 单目标盘无绑定 → 直接 failed/no_binding，
    不被认领、attempts 不动（秒判，不再拖 129 分钟）。"""
    job = _job(db)
    claimed = tv.claim_due_jobs(db, limit=10)
    assert claimed == []
    fresh = _fresh(db, job.id)
    assert fresh.status == "failed"
    assert fresh.error_kind == "no_binding"
    assert fresh.attempts == 0


def test_claim_sweeps_cross_job_with_wrong_binding(db):
    """跨单落 115 盘但用户只有 Drive 绑定：认领前同判 no_binding。"""
    _binding(db, 7, provider="drive")
    job = _job(db, dest_drive_id="pan115:user-7")
    claimed = tv.claim_due_jobs(db, limit=10)
    assert claimed == []
    fresh = _fresh(db, job.id)
    assert fresh.status == "failed"
    assert fresh.error_kind == "no_binding"


def test_claim_keeps_user_job_with_binding(db):
    """有绑定：认领照常（复核不误伤）。"""
    _binding(db, 7, provider="drive")
    job = _job(db)
    claimed = tv.claim_due_jobs(db, limit=10)
    assert claimed == [job.id]
    assert _fresh(db, job.id).status == "running"


def test_claim_does_not_sweep_source_job(db):
    """服主 source 单不查用户绑定：照常认领。"""
    job = _job(db, owner_scope="source", owner_ref=None,
               dest_drive_id=None)
    claimed = tv.claim_due_jobs(db, limit=10)
    assert claimed == [job.id]
    assert _fresh(db, job.id).status == "running"
