# -*- coding: utf-8 -*-
"""公共贡献池 PR-2 进池（goal-33）：选盘 / 建单 / 门禁判定 / 工人落盘。

口径来源：hidden_files/pr2-task.md + design-pool-v1 §4.2/§5.1 第 9 步/
§6.1/§6.2。全部离线：SQLite + 假 Drive 客户端；真实 Drive 不碰。

本单合并后池能进片、还播不出去是预期状态（serve 态门禁回
failed 明示，见 test_gate_serve_returns_failed_state）。
"""
from __future__ import annotations

import os

os.environ.setdefault("DATABASE_TYPE", "sqlite")
os.environ.setdefault("REDIS_ENABLED", "false")

import uuid
from datetime import date, datetime, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend import config_service, models as base_models
from backend.emby_server import models as em
from backend.emby_server import pool_gate
from backend.emby_server import transfer_decisions as td
from backend.emby_server import transfer_variant as tv
from backend.emby_server.transfer_variant import TransferError
from backend.integrations import store

GIB = 1024 ** 3
TB = 1000 ** 4
ORIG_FID = "origfid"


@pytest.fixture()
def db_factory():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False},
        poolclass=StaticPool)
    base_models.Base.metadata.create_all(engine)
    em.Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    yield factory
    engine.dispose()


@pytest.fixture()
def db(db_factory):
    session = db_factory()
    yield session
    session.close()


@pytest.fixture(autouse=True)
def _note_here(db_factory, monkeypatch):
    """留痕模块的独立会话落到测试库（沿 test_transfer_decision_rounds
    的既有 note_here 口径，只打模块属性）。"""
    monkeypatch.setattr(td, "SessionLocal", db_factory)


@pytest.fixture(autouse=True)
def _clean_store_cache():
    yield
    store.invalidate()


@pytest.fixture(autouse=True)
def _transfer_master_on(monkeypatch):
    # 建单沿 create_user_job 口径查转存总闸
    monkeypatch.setenv("TRANSFER_VARIANT_ENABLED", "1")


@pytest.fixture(autouse=True)
def _clean_quota_cache():
    pool_gate.clear_pool_quota_cache()
    yield
    pool_gate.clear_pool_quota_cache()


def _user(db, name):
    user = base_models.WebUser(username=name, password_hash="x")
    db.add(user)
    db.commit()
    return user


def _binding(db, user_id, status="active", provider="drive"):
    cred = em.MountCredential(
        mount_id=None, scope="user", owner_user_id=user_id,
        sealed_credential="", key_id="fernet-v1", status=status)
    db.add(cred)
    db.flush()
    binding = em.UserStorageBinding(
        user_id=user_id, provider=provider, account_label="贡献盘",
        credential_id=cred.id, status=status)
    db.add(binding)
    db.commit()
    return binding


def _mount(db, mount_id=3):
    existing = (db.query(em.StorageMount)
                .filter(em.StorageMount.id == mount_id).first())
    if existing is not None:
        return existing
    row = em.StorageMount(id=mount_id, name=f"m{mount_id}",
                          mount_type="drive", path="drive:drive1")
    db.add(row)
    db.commit()
    return row


def _item(db, size=1_000_000):
    _mount(db)
    lib = em.Library(guid=uuid.uuid4().hex, name="池进片库")
    db.add(lib)
    db.commit()
    item = em.MediaItem(
        guid=uuid.uuid4().hex, library_id=lib.id, item_type="movie",
        name="池测试片", drive_file_id=ORIG_FID,
        file_path="mount://3/电影/a.mkv", size=size)
    db.add(item)
    db.commit()
    return item


def _member(db, user_id, status="active", capacity_cap_bytes=None):
    binding = _binding(db, user_id)
    row = em.PoolMember(
        binding_id=binding.id, user_id=user_id, provider="drive",
        status=status, capacity_cap_bytes=capacity_cap_bytes,
        joined_at=datetime.now())
    db.add(row)
    db.commit()
    return row


def _set_pool(db, **values):
    config_service.save_config(db, values, schema="pool")
    db.commit()


def _enable_pool(db, **overrides):
    values = {"pool_enabled": True}
    values.update(overrides)
    _set_pool(db, **values)


def _quota_ok(db, binding):
    return {"limit": 5 * TB, "usage": 1 * TB, "free": 4 * TB}


def _quota_free(free_bytes):
    def _fn(db, binding):
        return {"limit": 5 * TB, "usage": 5 * TB - free_bytes,
                "free": free_bytes}
    return _fn


def _job(db, item_id, member_id, status="pending", error_kind=None,
         error_detail=None, created_at=None):
    row = em.TransferJob(
        item_id=item_id, trigger="playback", drive_id="drive1",
        provider="drive", owner_scope="pool", owner_ref=str(member_id),
        dest_drive_id=f"pooldrive:{member_id}", status=status,
        error_kind=error_kind, error_detail=error_detail)
    db.add(row)
    db.flush()
    if created_at is not None:
        row.created_at = created_at
        row.cooldown_until = created_at + timedelta(seconds=86400)
    db.commit()
    return row


def _variant(db, item_id, member_id, status="ready", stale_count=0,
             fid="poolcopy"):
    row = em.TransferVariant(
        item_id=item_id, source_file_id=ORIG_FID, variant_file_id=fid,
        variant_drive_id=f"pooldrive:{member_id}", owner_scope="pool",
        owner_ref=str(member_id), status=status, size_bytes=1_000_000)
    db.add(row)
    db.flush()
    if stale_count:
        row.stale_count = stale_count
    db.commit()
    return row


def _decision_rounds(db, scope="pool_pick"):
    return (db.query(em.TransferDecisionRound)
            .filter(em.TransferDecisionRound.scope == scope).all())


# ---------------------------------------------------------------- 选盘


def test_pick_single_member(db):
    item = _item(db)
    member = _member(db, 11)
    picked, candidates = pool_gate.pick_pool_member(
        db, item, quota_fn=_quota_ok)
    assert picked is not None and picked.id == member.id
    assert len(candidates) == 1
    assert candidates[0]["member_id"] == member.id
    assert candidates[0]["eligible"] is True


def test_pick_skips_over_capacity_member(db):
    item = _item(db, size=500 * GIB)
    # 余量 400GiB：扣 300GiB 保底剩 100GiB < 500GiB 文件 → 不合格
    _member(db, 11)
    picked, candidates = pool_gate.pick_pool_member(
        db, item, quota_fn=_quota_free(400 * GIB))
    assert picked is None
    assert candidates[0]["eligible"] is False


def test_pick_skips_daily_bytes_limit(db):
    item = _item(db)
    member = _member(db, 11)
    db.add(em.PoolUsageDaily(
        member_id=member.id, day=date.today(),
        transfer_jobs=0, transfer_bytes=599 * GIB, play_starts=0,
        quota_hits=0))
    db.commit()
    # 日限 600GiB：已用 599GiB + 1MB 文件仍在限内 → 应选中
    picked, _ = pool_gate.pick_pool_member(db, item, quota_fn=_quota_ok)
    assert picked is not None and picked.id == member.id
    # 再记一笔把日用量顶满 → 不再选中
    row = db.query(em.PoolUsageDaily).filter_by(
        member_id=member.id, day=date.today()).one()
    row.transfer_bytes = 600 * GIB
    db.commit()
    pool_gate.clear_pool_quota_cache()
    picked, _ = pool_gate.pick_pool_member(db, item, quota_fn=_quota_ok)
    assert picked is None


def test_pick_skips_daily_jobs_limit(db):
    item = _item(db)
    member = _member(db, 11)
    _enable_pool(db, pool_member_daily_transfer_jobs=1)
    db.add(em.PoolUsageDaily(
        member_id=member.id, day=date.today(),
        transfer_jobs=1, transfer_bytes=0, play_starts=0, quota_hits=0))
    db.commit()
    picked, _ = pool_gate.pick_pool_member(db, item, quota_fn=_quota_ok)
    assert picked is None


def test_pick_top_k_random_within_best(db):
    item = _item(db)
    members = [_member(db, 11 + i) for i in range(3)]
    free = {members[0].id: 4 * TB, members[1].id: 3 * TB,
            members[2].id: 1 * TB}

    def _fn(db_arg, binding):
        member_id = (db_arg.query(em.PoolMember)
                     .filter(em.PoolMember.binding_id == binding.id)
                     .one().id)
        free_bytes = free[member_id]
        return {"limit": 5 * TB, "usage": 5 * TB - free_bytes,
                "free": free_bytes}

    _enable_pool(db, pool_pick_top_k=2)
    seen = set()
    for _ in range(30):
        pool_gate.clear_pool_quota_cache()
        picked, _ = pool_gate.pick_pool_member(db, item, quota_fn=_fn)
        assert picked is not None
        # 只在打分前二里随机挑，第三名（余量最少）永不中选
        assert picked.id in {members[0].id, members[1].id}
        seen.add(picked.id)
    # 30 次里前二都该中过（随机性断言，失败概率约 2^-29）
    assert seen == {members[0].id, members[1].id}


def test_pick_excludes_member_with_recent_pool_job(db):
    item = _item(db)
    member_a = _member(db, 11)
    member_b = _member(db, 12)
    _job(db, item.id, member_a.id, status="done")
    picked, _ = pool_gate.pick_pool_member(db, item, quota_fn=_quota_ok)
    assert picked is not None and picked.id == member_b.id


def test_pick_no_active_members_returns_none_and_notes_decision(db):
    item = _item(db)
    picked, candidates = pool_gate.pick_pool_member(
        db, item, quota_fn=_quota_ok)
    assert picked is None
    assert candidates == []
    rounds = _decision_rounds(db)
    assert len(rounds) == 1
    assert rounds[0].decision == "none"


def test_quota_read_cached_for_10_minutes(db):
    item = _item(db)
    _member(db, 11)
    calls = {"n": 0}

    def _counting(db_arg, binding):
        calls["n"] += 1
        return _quota_ok(db_arg, binding)

    pool_gate.pick_pool_member(db, item, quota_fn=_counting)
    pool_gate.pick_pool_member(db, item, quota_fn=_counting)
    assert calls["n"] == 1
    pool_gate.clear_pool_quota_cache()
    pool_gate.pick_pool_member(db, item, quota_fn=_counting)
    assert calls["n"] == 2

# ---------------------------------------------------------------- 建单


def test_create_pool_job_row(db):
    item = _item(db)
    member = _member(db, 11)
    _enable_pool(db)
    job = pool_gate.create_pool_job(db, item, quota_fn=_quota_ok)
    assert job.id is not None
    assert job.provider == "drive"
    assert job.owner_scope == "pool"
    assert job.owner_ref == str(member.id)
    assert job.dest_drive_id == f"pooldrive:{member.id}"
    assert job.drive_id == "drive1"
    assert job.trigger == "playback"
    assert job.status == "pending"
    assert job.cooldown_until is not None
    usage = (db.query(em.PoolUsageDaily)
             .filter_by(member_id=member.id, day=date.today()).one())
    assert usage.transfer_jobs == 1
    rounds = _decision_rounds(db)
    assert len(rounds) == 1
    assert rounds[0].decision == f"member:{member.id}"
    assert rounds[0].resource_key == f"item:{item.id}"


def test_create_pool_job_jitter_sets_next_retry(db):
    item = _item(db)
    _member(db, 11)
    _enable_pool(db, pool_enqueue_jitter_sec=120)
    before = datetime.now()
    job = pool_gate.create_pool_job(db, item, quota_fn=_quota_ok)
    assert job.next_retry_at is not None
    assert before <= job.next_retry_at <= before + timedelta(seconds=120)


def test_create_pool_job_no_jitter_leaves_next_retry_none(db):
    item = _item(db)
    _member(db, 11)
    _enable_pool(db, pool_enqueue_jitter_sec=0)
    job = pool_gate.create_pool_job(db, item, quota_fn=_quota_ok)
    assert job.next_retry_at is None


def test_create_pool_job_disabled_raises(db):
    item = _item(db)
    _member(db, 11)
    with pytest.raises(TransferError) as excinfo:
        pool_gate.create_pool_job(db, item, quota_fn=_quota_ok)
    assert excinfo.value.kind == "pool_disabled"


def test_create_pool_job_throttled_global_concurrency(db):
    item = _item(db)
    other = _item(db)
    member = _member(db, 11)
    _enable_pool(db, pool_global_concurrency=1)
    _job(db, other.id, member.id, status="running")
    with pytest.raises(TransferError) as excinfo:
        pool_gate.create_pool_job(db, item, quota_fn=_quota_ok)
    assert excinfo.value.kind == "throttled"
    assert db.query(em.TransferJob).filter_by(item_id=item.id).count() == 0


def test_create_pool_job_throttled_same_member_24h(db):
    item = _item(db)
    member = _member(db, 11)
    _enable_pool(db)
    _job(db, item.id, member.id, status="done")
    with pytest.raises(TransferError) as excinfo:
        pool_gate.create_pool_job(db, item, quota_fn=_quota_ok)
    assert excinfo.value.kind == "throttled"
    # force 越过 24h 节流（门禁重触发专用）
    job = pool_gate.create_pool_job(db, item, force=True, quota_fn=_quota_ok)
    assert job.id is not None


def test_create_pool_job_no_member_raises(db):
    item = _item(db)
    _enable_pool(db)
    with pytest.raises(TransferError) as excinfo:
        pool_gate.create_pool_job(db, item, quota_fn=_quota_ok)
    assert excinfo.value.kind == "no_member"


# ---------------------------------------------------------------- 门禁判定


def _free_user(db):
    return _user(db, "free")


def test_gate_creates_job_when_no_copy(db):
    item = _item(db)
    _member(db, 11)
    _enable_pool(db)
    user = _free_user(db)
    gate = pool_gate.ensure_pool_copy_gate(db, item, user, quota_fn=_quota_ok)
    assert gate["verdict"] == "preparing"
    assert gate["reason"] == "job_created"
    assert gate.get("pool") is True
    assert gate["job_id"] is not None
    assert ORIG_FID not in str(gate)


def test_gate_in_flight_no_duplicate_job(db):
    item = _item(db)
    member = _member(db, 11)
    _enable_pool(db)
    existing = _job(db, item.id, member.id, status="pending")
    user = _free_user(db)
    gate = pool_gate.ensure_pool_copy_gate(db, item, user, quota_fn=_quota_ok)
    assert gate["verdict"] == "preparing"
    assert gate["reason"] == "job_in_flight"
    assert gate["job_id"] == existing.id
    assert db.query(em.TransferJob).filter_by(item_id=item.id).count() == 1


def test_gate_serve_ready_variant(db):
    item = _item(db)
    member = _member(db, 11)
    _enable_pool(db)
    variant = _variant(db, item.id, member.id)
    user = _free_user(db)
    gate = pool_gate.ensure_pool_copy_gate(db, item, user, quota_fn=_quota_ok)
    assert gate["verdict"] == "serve_pool_copy"
    assert gate["variant"].id == variant.id
    assert ORIG_FID not in str(gate)


def test_gate_serve_ignores_stale_variant(db):
    item = _item(db)
    member = _member(db, 11)
    _enable_pool(db)
    _variant(db, item.id, member.id, stale_count=1)
    user = _free_user(db)
    gate = pool_gate.ensure_pool_copy_gate(db, item, user, quota_fn=_quota_ok)
    assert gate["verdict"] == "preparing"
    assert gate["reason"] == "job_created"


def test_gate_serve_ignores_suspended_member(db):
    item = _item(db)
    member = _member(db, 11, status="suspended")
    _enable_pool(db)
    _variant(db, item.id, member.id)
    user = _free_user(db)
    gate = pool_gate.ensure_pool_copy_gate(db, item, user, quota_fn=_quota_ok)
    # 停用成员的副本不播出；又无其他成员可选 → 失败明示（暂无可用盘）
    assert gate["verdict"] == "failed"
    assert gate["reason"] == "no_member"
    assert "暂无可用贡献盘" in (gate.get("detail") or "")


def test_gate_failed_terminal_passthrough(db):
    item = _item(db)
    member = _member(db, 11)
    _enable_pool(db)
    _job(db, item.id, member.id, status="failed", error_kind="dead",
         error_detail="源文件已删")
    user = _free_user(db)
    gate = pool_gate.ensure_pool_copy_gate(db, item, user, quota_fn=_quota_ok)
    assert gate["verdict"] == "failed"
    assert gate["reason"] == "dead"
    assert "源文件已删" in (gate.get("detail") or "")
    assert ORIG_FID not in str(gate)
    assert db.query(em.TransferJob).filter_by(item_id=item.id).count() == 1


def test_gate_failed_retriable_retriggers_within_budget(db):
    item = _item(db)
    member = _member(db, 11)
    _enable_pool(db)
    _job(db, item.id, member.id, status="failed", error_kind="retriable",
         error_detail="429 限流")
    user = _free_user(db)
    gate = pool_gate.ensure_pool_copy_gate(db, item, user, quota_fn=_quota_ok)
    assert gate["verdict"] == "preparing"
    assert gate["reason"] == "job_retriggered"
    assert db.query(em.TransferJob).filter_by(item_id=item.id).count() == 2


def test_gate_failed_retriable_budget_exhausted(db):
    item = _item(db)
    member = _member(db, 11)
    _enable_pool(db)
    _job(db, item.id, member.id, status="failed", error_kind="retriable",
         error_detail="429 限流")
    _job(db, item.id, member.id, status="failed", error_kind="retriable",
         error_detail="429 限流")
    user = _free_user(db)
    gate = pool_gate.ensure_pool_copy_gate(db, item, user, quota_fn=_quota_ok)
    assert gate["verdict"] == "failed"
    assert gate["reason"] == "retriable"
    assert db.query(em.TransferJob).filter_by(item_id=item.id).count() == 2


def test_gate_done_job_without_ready_rebuilds(db):
    item = _item(db)
    member = _member(db, 11)
    _enable_pool(db)
    _job(db, item.id, member.id, status="done")
    user = _free_user(db)
    gate = pool_gate.ensure_pool_copy_gate(db, item, user, quota_fn=_quota_ok)
    assert gate["verdict"] == "preparing"
    assert gate["reason"] == "job_rebuilt"


def test_gate_pool_disabled_returns_failed(db):
    item = _item(db)
    _member(db, 11)
    user = _free_user(db)
    gate = pool_gate.ensure_pool_copy_gate(db, item, user, quota_fn=_quota_ok)
    assert gate["verdict"] == "failed"
    assert gate["reason"] == "pool_disabled"


def test_gate_license_check_propagates(monkeypatch, db):
    item = _item(db)
    _member(db, 11)
    _enable_pool(db)
    user = _free_user(db)
    from fastapi import HTTPException

    def _deny(db_arg):
        raise HTTPException(status_code=403, detail="授权无效")

    from backend.emby_server import playback_gate as _gate
    monkeypatch.setattr(_gate, "_ensure_license_playback_allowed", _deny)
    with pytest.raises(HTTPException) as excinfo:
        pool_gate.ensure_pool_copy_gate(db, item, user, quota_fn=_quota_ok)
    assert excinfo.value.status_code == 403


def test_entry_license_check_propagates(monkeypatch, db):
    item = _item(db)
    user = _free_user(db)
    from fastapi import HTTPException

    def _deny(db_arg):
        raise HTTPException(status_code=403, detail="授权无效")

    from backend.emby_server import playback_gate as _gate
    monkeypatch.setattr(_gate, "_ensure_license_playback_allowed", _deny)
    with pytest.raises(HTTPException) as excinfo:
        pool_gate.ensure_playback_allowed_for_item(db, user, item)
    assert excinfo.value.status_code == 403


def test_bound_user_still_kind_bound(db):
    item = _item(db)
    viewer = _user(db, "viewer")
    _binding(db, viewer.id)
    decision = pool_gate.ensure_playback_allowed_for_item(db, viewer, item)
    assert decision.allowed is True
    assert decision.kind == "bound"


def test_unbound_user_pool_decision(db):
    item = _item(db)
    _member(db, 11)
    _enable_pool(db)
    user = _free_user(db)
    decision = pool_gate.ensure_playback_allowed_for_item(db, user, item)
    assert decision.allowed is True
    assert decision.kind == "pool"


def test_unbound_user_pool_off_403(db):
    item = _item(db)
    user = _free_user(db)
    from fastapi import HTTPException
    with pytest.raises(HTTPException) as excinfo:
        pool_gate.ensure_playback_allowed_for_item(db, user, item)
    assert excinfo.value.status_code == 403

# ---------------------------------------------------------------- 工人落盘


class _FakeMemberDrive:
    """成员盘假客户端（Path A：copy → 目录核对 → anyone-reader）。"""

    def __init__(self, dir_ids=None, perm_fail=False, copy_exc=None):
        self.dir_ids = dir_ids
        self.perm_fail = perm_fail
        self.copy_exc = copy_exc
        self.perm_calls = []
        self.copy_calls = []

    def ensure_folder(self, drive_id, dir_name):
        return "pfolder"

    def list_file_ids_by_name(self, drive_id, folder_id, name):
        if self.dir_ids is not None:
            return list(self.dir_ids)
        return ["poolcopy"]

    def copy_file(self, drive_id, source_fid, folder_id, name):
        self.copy_calls.append(source_fid)
        if self.copy_exc is not None:
            raise self.copy_exc
        return {"id": "poolcopy", "size": 1_000_000}

    def add_anyone_reader(self, drive_id, fid):
        self.perm_calls.append(fid)
        if self.perm_fail:
            raise Exception("perm fail")

    def about_storage(self, drive_id):
        return {"limit": 5 * TB, "usage": 1 * TB, "free": 4 * TB}


class _FakeSA:
    def ensure_folder(self, drive_id, dir_name):
        return "tfolder"


def _pool_job(db, item_id, member_id, status="pending"):
    return _job(db, item_id, member_id, status=status)


def test_worker_pool_copy_success(db, monkeypatch):
    item = _item(db)
    member = _member(db, 11)
    job = _pool_job(db, item.id, member.id)
    fake = _FakeMemberDrive()
    monkeypatch.setattr(tv, "UserDriveApiClient",
                        lambda db_arg, binding: fake)
    res = tv.run_transfer_job_once(db, job, _FakeSA())
    assert res["outcome"] == "done"
    assert fake.copy_calls == [ORIG_FID]
    assert fake.perm_calls == ["poolcopy"]
    row = (db.query(em.TransferVariant)
           .filter(em.TransferVariant.item_id == item.id).one())
    assert row.status == "ready"
    assert row.owner_scope == "pool"
    assert row.owner_ref == str(member.id)
    assert row.variant_drive_id == f"pooldrive:{member.id}"
    assert row.variant_file_id == "poolcopy"
    usage = (db.query(em.PoolUsageDaily)
             .filter_by(member_id=member.id, day=date.today()).one())
    assert usage.transfer_bytes == 1_000_000
    assert usage.transfer_jobs == 0  # 建单时记 jobs、工人完成时记 bytes


def test_worker_pool_copy_unresolved(db, monkeypatch):
    item = _item(db)
    member = _member(db, 11)
    job = _pool_job(db, item.id, member.id)
    fake = _FakeMemberDrive(dir_ids=[])
    monkeypatch.setattr(tv, "UserDriveApiClient",
                        lambda db_arg, binding: fake)
    res = tv.run_transfer_job_once(db, job, _FakeSA())
    assert res["outcome"] == "failed"
    assert res["error_kind"] == "copy_unresolved"
    assert fake.perm_calls == []


def test_worker_pool_perm_fail_orphan(db, monkeypatch):
    item = _item(db)
    member = _member(db, 11)
    job = _pool_job(db, item.id, member.id)
    fake = _FakeMemberDrive(perm_fail=True)
    monkeypatch.setattr(tv, "UserDriveApiClient",
                        lambda db_arg, binding: fake)
    res = tv.run_transfer_job_once(db, job, _FakeSA())
    assert res["outcome"] == "failed"
    assert res["error_kind"] == "orphan"
    row = (db.query(em.TransferVariant)
           .filter(em.TransferVariant.item_id == item.id).one())
    assert row.status == "orphan"


def test_worker_pool_no_member(db, monkeypatch):
    item = _item(db)
    job = _pool_job(db, item.id, 9999)
    fake = _FakeMemberDrive()
    monkeypatch.setattr(tv, "UserDriveApiClient",
                        lambda db_arg, binding: fake)
    res = tv.run_transfer_job_once(db, job, _FakeSA())
    assert res["outcome"] == "failed"
    assert res["error_kind"] == "no_member"
    assert fake.copy_calls == []


def test_worker_pool_binding_invalid(db, monkeypatch):
    item = _item(db)
    member = _member(db, 11)
    binding = (db.query(em.UserStorageBinding)
               .filter_by(id=member.binding_id).one())
    binding.status = "invalid"
    db.commit()
    job = _pool_job(db, item.id, member.id)
    fake = _FakeMemberDrive()
    monkeypatch.setattr(tv, "UserDriveApiClient",
                        lambda db_arg, binding: fake)
    res = tv.run_transfer_job_once(db, job, _FakeSA())
    assert res["outcome"] == "failed"
    assert res["error_kind"] == "no_binding"
    assert fake.copy_calls == []


def test_worker_pool_copy_forbidden_terminal(db, monkeypatch):
    item = _item(db)
    member = _member(db, 11)
    job = _pool_job(db, item.id, member.id)
    fake = _FakeMemberDrive(copy_exc=tv._CopyForbidden("copy 被拒：403"))
    monkeypatch.setattr(tv, "UserDriveApiClient",
                        lambda db_arg, binding: fake)
    res = tv.run_transfer_job_once(db, job, _FakeSA())
    assert res["outcome"] == "failed"
    assert res["error_kind"] == "copy_forbidden"
    # 终态失败，不重试死循环
    assert db.query(em.TransferJob).filter_by(id=job.id).one().status == "failed"


def test_worker_pool_quota_counts_hit(db, monkeypatch):
    item = _item(db)
    member = _member(db, 11)
    job = _pool_job(db, item.id, member.id)
    fake = _FakeMemberDrive(
        copy_exc=tv._CopyQuotaExceeded("copy 被拒：配额不足"))
    monkeypatch.setattr(tv, "UserDriveApiClient",
                        lambda db_arg, binding: fake)
    res = tv.run_transfer_job_once(db, job, _FakeSA())
    assert res["outcome"] == "failed"
    assert res["error_kind"] == "insufficient_space"
    usage = (db.query(em.PoolUsageDaily)
             .filter_by(member_id=member.id, day=date.today()).one())
    assert usage.quota_hits == 1


def test_worker_pool_second_member_copy_coexists(db, monkeypatch):
    """两成员同条目各一份 ready：新登记只释放本成员旧行，不动他人行。"""
    item = _item(db)
    member_a = _member(db, 11)
    member_b = _member(db, 12)
    _variant(db, item.id, member_a.id, fid="copy-a")
    job = _pool_job(db, item.id, member_b.id)
    fake = _FakeMemberDrive()
    monkeypatch.setattr(tv, "UserDriveApiClient",
                        lambda db_arg, binding: fake)
    res = tv.run_transfer_job_once(db, job, _FakeSA())
    assert res["outcome"] == "done"
    rows = (db.query(em.TransferVariant)
            .filter(em.TransferVariant.item_id == item.id,
                    em.TransferVariant.status == "ready")
            .order_by(em.TransferVariant.id).all())
    assert len(rows) == 2
    assert {r.owner_ref for r in rows} == {str(member_a.id),
                                            str(member_b.id)}


def test_worker_pool_same_member_rebuild_releases_old_row(db, monkeypatch):
    """同成员重建：只释放本成员旧 ready 行。"""
    item = _item(db)
    member = _member(db, 11)
    _variant(db, item.id, member.id, fid="copy-old")
    job = _pool_job(db, item.id, member.id)
    fake = _FakeMemberDrive()
    monkeypatch.setattr(tv, "UserDriveApiClient",
                        lambda db_arg, binding: fake)
    res = tv.run_transfer_job_once(db, job, _FakeSA())
    assert res["outcome"] == "done"
    rows = (db.query(em.TransferVariant)
            .filter(em.TransferVariant.item_id == item.id).all())
    ready = [r for r in rows if r.status == "ready"]
    released = [r for r in rows if r.status == "released"]
    assert len(ready) == 1 and ready[0].variant_file_id == "poolcopy"
    assert len(released) == 1 and released[0].variant_file_id == "copy-old"


def test_run_transfer_job_once_rejects_unknown_scope(db):
    item = _item(db)
    job = em.TransferJob(
        item_id=item.id, trigger="playback", drive_id="drive1",
        provider="drive", owner_scope="bogus", owner_ref="1",
        dest_drive_id="bogus:1", status="pending")
    db.add(job)
    db.commit()
    res = tv.run_transfer_job_once(db, job, _FakeSA())
    assert res["outcome"] == "failed"
    assert res["error_kind"] == "unsupported"
