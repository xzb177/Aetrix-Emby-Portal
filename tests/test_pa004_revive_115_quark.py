"""PA-004 复活口补全（2026-11-02 用户亲点）：115/夸克绑定保存成功 /
重验通过也要复活该用户 failed/no_binding 转存单。

背景：#1676 只给 Drive OAuth 重绑加了复活口（drive_oauth 的
reset_mkdir_failed_jobs）；user_bindings.create_binding /
verify_binding（115/夸克唯一建绑/重验口）完全不碰 TransferJob，
只绑 115/夸克的用户重绑后死单无人复活。本文件钉死：

- 复活实现全站只有一份（transfer_variant.revive_binding_failed_jobs），
  115/夸克成功点与 Drive 调用同一份，字段口径一致（status→pending、
  attempts 清零、error_kind/error_detail/next_retry_at 清掉、
  租约 lease_expires_at/claimed_by 一并清）；
- 任一 provider 绑定成功即复活该用户全部 failed/no_binding 单
  （不按 dest 过滤，与 Drive 语义一致）；
- 保存失败/重验不通过不复活；他人行、其他 kind 行、pending 行不动；
  重复保存幂等。

上游网络全部用桩替身（monkeypatch ub 的验活/VIP 口），不碰真实
115/夸克。
"""

from datetime import datetime, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend import models as base_models
from backend.emby_server import models as em
from backend.emby_server import transfer115
from backend.emby_server import user_bindings as ub
from backend.emby_server.upstream_sources import seal_secret


@pytest.fixture()
def db_factory():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False},
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
def _stub_verify(monkeypatch):
    """验活全过 + VIP 门全放行（各用例可再覆写单个桩改行为）。"""
    monkeypatch.setattr(ub, "_verify_pan115_cookie", lambda cookie: None)
    monkeypatch.setattr(ub, "_verify_quark_cookie", lambda cookie: cookie)
    monkeypatch.setattr(ub, "_pan115_vip_blocked", lambda db, cookie: False)
    monkeypatch.setattr(ub, "_quark_vip_blocked", lambda db, cookie: False)
    monkeypatch.setattr(ub, "quark_bind_enabled", lambda db: True)
    yield


def _seed_job(db, item_id, user_id="7", status="failed",
              error_kind="no_binding", dest_drive_id="pan115:user-7",
              attempts=3):
    job = em.TransferJob(
        item_id=item_id, trigger="playback", provider="drive",
        owner_scope="user", owner_ref=user_id, status=status,
        attempts=attempts, error_kind=error_kind,
        error_detail="目标盘未绑定" if error_kind == "no_binding" else "其它失败",
        dest_drive_id=dest_drive_id,
        next_retry_at=datetime.now() - timedelta(seconds=60)
        if status == "failed" else None,
        lease_expires_at=datetime.now() + timedelta(minutes=30)
        if status == "failed" else None,
        claimed_by="worker-x" if status == "failed" else None)
    db.add(job)
    db.commit()
    return job


def _seed_binding(db, user_id=7, provider="pan115", label="主号"):
    cred = em.MountCredential(
        mount_id=None, scope="user", owner_user_id=user_id,
        sealed_credential=seal_secret("COOKIE-OLD"), key_id="fernet-v1",
        status="invalid")
    db.add(cred)
    db.flush()
    binding = em.UserStorageBinding(
        user_id=user_id, provider=provider, account_label=label,
        credential_id=cred.id, status="invalid")
    db.add(binding)
    db.commit()
    return binding


def _assert_revived(db, job):
    db.refresh(job)
    assert job.status == "pending"
    assert job.attempts == 0
    assert job.error_kind is None
    assert job.error_detail is None
    assert job.next_retry_at is None
    assert job.lease_expires_at is None
    assert job.claimed_by is None


def _assert_still_failed(db, job):
    db.refresh(job)
    assert job.status == "failed"
    assert job.error_kind == "no_binding"
    assert job.attempts == 3
    assert job.claimed_by == "worker-x"


# ---------------------------------------------------------------------------
# 建绑成功 → 复活（115 / 夸克各一例；不按 dest 过滤）
# ---------------------------------------------------------------------------

def test_create_pan115_binding_revives_no_binding_jobs(db):
    mine_cross = _seed_job(db, 11, dest_drive_id="userdrive:7")
    mine_115 = _seed_job(db, 12, dest_drive_id="pan115:user-7")
    other_user = _seed_job(db, 13, user_id="8",
                           dest_drive_id="pan115:user-8")
    other_kind = _seed_job(db, 14, error_kind="dead")
    pending = _seed_job(db, 15, status="pending", error_kind=None)

    binding = ub.create_binding(db, 7, "pan115", "主号", "COOKIE-NEW")
    assert binding.status == "active"

    _assert_revived(db, mine_cross)
    _assert_revived(db, mine_115)
    _assert_still_failed(db, other_user)
    db.refresh(other_kind)
    assert other_kind.status == "failed" and other_kind.error_kind == "dead"
    db.refresh(pending)
    assert pending.status == "pending" and pending.attempts == 3


def test_create_quark_binding_revives_no_binding_jobs(db):
    job = _seed_job(db, 21, dest_drive_id="quark:user-7")

    binding = ub.create_binding(db, 7, "quark", "夸克号", "COOKIE-Q")
    assert binding.status == "active"

    _assert_revived(db, job)


# ---------------------------------------------------------------------------
# 重验通过 → 复活（115 / 夸克各一例）
# ---------------------------------------------------------------------------

def test_verify_pan115_binding_revives_no_binding_jobs(db):
    binding = _seed_binding(db, provider="pan115")
    job = _seed_job(db, 31, dest_drive_id="pan115:user-7")

    out = ub.verify_binding(db, 7, binding.id)
    assert out.status == "active"

    _assert_revived(db, job)


def test_verify_quark_binding_revives_no_binding_jobs(db):
    binding = _seed_binding(db, provider="quark", label="夸克号")
    job = _seed_job(db, 41, dest_drive_id="quark:user-7")

    out = ub.verify_binding(db, 7, binding.id)
    assert out.status == "active"

    _assert_revived(db, job)


# ---------------------------------------------------------------------------
# 保存失败 / 重验不通过 → 不复活
# ---------------------------------------------------------------------------

def test_create_pan115_auth_failure_does_not_revive(db, monkeypatch):
    def _boom(cookie):
        raise transfer115.Pan115AuthError("Cookie 已失效")
    monkeypatch.setattr(ub, "_verify_pan115_cookie", _boom)
    job = _seed_job(db, 51)

    binding = ub.create_binding(db, 7, "pan115", "过期号", "COOKIE-OLD")
    assert binding.status != "active"

    _assert_still_failed(db, job)


def test_create_pan115_transient_failure_does_not_revive(db, monkeypatch):
    def _boom(cookie):
        raise transfer115.Pan115Error("115 返回 HTTP 500")
    monkeypatch.setattr(ub, "_verify_pan115_cookie", _boom)
    job = _seed_job(db, 52)

    with pytest.raises(ub.BindingError):
        ub.create_binding(db, 7, "pan115", "抖号", "COOKIE-T")

    _assert_still_failed(db, job)


def test_verify_pan115_failure_does_not_revive(db, monkeypatch):
    def _boom(cookie):
        raise transfer115.Pan115AuthError("Cookie 已失效")
    monkeypatch.setattr(ub, "_verify_pan115_cookie", _boom)
    binding = _seed_binding(db, provider="pan115")
    job = _seed_job(db, 53)

    out = ub.verify_binding(db, 7, binding.id)
    assert out.status != "active"

    _assert_still_failed(db, job)


# ---------------------------------------------------------------------------
# 幂等：复活后再保存成功，pending 行不被二次处理
# ---------------------------------------------------------------------------

def test_second_successful_save_is_idempotent(db):
    job = _seed_job(db, 61)
    ub.create_binding(db, 7, "pan115", "一号", "COOKIE-1")
    _assert_revived(db, job)

    ub.create_binding(db, 7, "pan115", "二号", "COOKIE-2")
    db.refresh(job)
    assert job.status == "pending"
    assert job.attempts == 0
    assert job.error_kind is None
