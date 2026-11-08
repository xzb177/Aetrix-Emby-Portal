# -*- coding: utf-8 -*-
"""公共贡献池 PR-4（goal-33）：成员生命周期 + H2 状态流水 + 两端入口。

覆盖（design §2.4/§6.4/§8/§11.5-H2 + 通审必带项）：
- H2：成员每次状态变更都经 pool_gate.transition_member_status 收口
  落 pool_member_status_events 流水（from/to/reason 齐全、顺序不乱）；
- 生命周期服务：申请（三种 join_mode）/批准/拒绝/暂停/恢复/清退/
  退出，含候选行绑定挂接、绑定失效与解绑两路连带；
- 清退违规标志：pool_members.violation_* 字段（谁置位、谁清除、
  时间与原因留痕），供 PR-6 结算按「违规清退不结」判定；
- 管理入口：grant_pool_candidate 端点 + 成员管理/总览/副本下线；
- 用户端：GET/POST /pool/contribution 三口。
红线：池总开关默认关、无成员时线上零变化不在本文件（foundation
已钉）；本文件只验生命周期语义本身。
"""
from __future__ import annotations

import os

os.environ.setdefault("DATABASE_TYPE", "sqlite")
os.environ.setdefault("REDIS_ENABLED", "false")

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend import config_service, models as base_models
from backend.api.admin_core import get_current_admin
from backend.database import get_db
from backend.emby_server import models as em
from backend.emby_server import pool_gate, user_bindings
from backend.integrations import store

FULL_SCOPES = (
    "https://www.googleapis.com/auth/drive.file "
    "https://www.googleapis.com/auth/drive.readonly")
READONLY_SCOPES = "https://www.googleapis.com/auth/drive.readonly"


@pytest.fixture()
def db():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False},
        poolclass=StaticPool)
    base_models.Base.metadata.create_all(engine)
    em.Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    yield session
    session.close()
    engine.dispose()


@pytest.fixture(autouse=True)
def _clean_store_cache():
    yield
    store.invalidate()


def _set_pool(db, **values):
    config_service.save_config(db, values, schema="pool")
    db.commit()


def _user(db, name):
    user = base_models.WebUser(username=name, password_hash="x")
    db.add(user)
    db.commit()
    return user


def _binding(db, user_id, status="active", provider="drive",
             scopes=FULL_SCOPES, label="贡献盘"):
    cred = em.MountCredential(
        mount_id=None, scope="user", owner_user_id=user_id,
        sealed_credential="", key_id="fernet-v1", status=status)
    db.add(cred)
    db.flush()
    binding = em.UserStorageBinding(
        user_id=user_id, provider=provider, account_label=label,
        credential_id=cred.id, status=status, granted_scopes=scopes)
    db.add(binding)
    db.commit()
    return binding


def _member(db, user_id, binding_id=None, status="active", **kw):
    row = em.PoolMember(
        user_id=user_id, binding_id=binding_id,
        provider="drive" if binding_id else None, status=status, **kw)
    db.add(row)
    db.commit()
    return row


def _variant(db, member_id, status="ready", size=1000, item_id=42):
    row = em.TransferVariant(
        item_id=item_id, source_file_id="orig-fid",
        variant_file_id=f"pool-fid-{member_id}-{item_id}-{status}",
        variant_drive_id=f"pooldrive:{member_id}",
        owner_scope="pool", owner_ref=str(member_id), status=status,
        size_bytes=size)
    db.add(row)
    db.commit()
    return row


def _events(db, member_id):
    return [
        (e.from_status, e.to_status, e.reason)
        for e in db.query(em.PoolMemberStatusEvent)
        .filter(em.PoolMemberStatusEvent.member_id == member_id)
        .order_by(em.PoolMemberStatusEvent.id.asc())
        .all()
    ]


class _FakeDriveClient:
    """记录删文件调用的成员盘客户端替身（不打真实 Drive）。"""

    def __init__(self, fail=False):
        self.calls = []
        self.fail = fail

    def delete_file_ignore_missing(self, drive_id, fid):
        self.calls.append((drive_id, fid))
        if self.fail:
            raise RuntimeError("drive delete boom")


# ------------------------------------------------------------ H2 与流转


def test_h2_full_lifecycle_events_in_order(db):
    user = _user(db, "h2flow")
    binding = _binding(db, user.id)
    member = pool_gate.apply_pool_membership(db, user.id, binding.id)
    assert member.status == "pending"
    pool_gate.approve_pool_member(db, member.id, actor_id=99)
    pool_gate.suspend_pool_member(db, member.id, actor_id=99)
    pool_gate.resume_pool_member(db, member.id, actor_id=99)
    pool_gate.eject_pool_member(db, member.id, actor_id=99)
    assert _events(db, member.id) == [
        (None, "pending", "apply"),
        ("pending", "active", "approve"),
        ("active", "suspended", "manual"),
        ("suspended", "active", "resume"),
        ("active", "ejected", "admin_eject"),
    ]
    db.refresh(member)
    assert member.status == "ejected"
    assert member.suspended_reason == "admin_eject"
    assert member.status_changed_at is not None


def test_apply_attaches_candidate_row(db):
    user = _user(db, "candidate-attach")
    candidate = pool_gate.grant_pool_candidate(db, user.id)
    binding = _binding(db, user.id)
    member = pool_gate.apply_pool_membership(db, user.id, binding.id)
    assert member.id == candidate.id
    assert member.binding_id == binding.id
    assert member.provider == "drive"
    assert member.status == "pending"
    assert _events(db, member.id) == [("candidate", "pending", "apply")]


def test_apply_open_mode_activates_immediately(db):
    _set_pool(db, pool_join_mode="open")
    user = _user(db, "open-mode")
    binding = _binding(db, user.id)
    member = pool_gate.apply_pool_membership(db, user.id, binding.id)
    assert member.status == "active"
    assert member.joined_at is not None


def test_apply_admin_assign_mode(db):
    _set_pool(db, pool_join_mode="admin_assign")
    user = _user(db, "assign-nocand")
    binding = _binding(db, user.id)
    with pytest.raises(pool_gate.PoolLifecycleError):
        pool_gate.apply_pool_membership(db, user.id, binding.id)
    user2 = _user(db, "assign-cand")
    pool_gate.grant_pool_candidate(db, user2.id)
    binding2 = _binding(db, user2.id)
    member = pool_gate.apply_pool_membership(db, user2.id, binding2.id)
    assert member.status == "active"
    assert member.joined_at is not None


def test_apply_rejects_bad_bindings(db):
    user = _user(db, "bad-bindings")
    dead = _binding(db, user.id, status="invalid", label="死盘")
    with pytest.raises(pool_gate.PoolLifecycleError):
        pool_gate.apply_pool_membership(db, user.id, dead.id)
    readonly = _binding(db, user.id, scopes=READONLY_SCOPES, label="只读盘")
    with pytest.raises(pool_gate.PoolLifecycleError):
        pool_gate.apply_pool_membership(db, user.id, readonly.id)
    other = _user(db, "other-owner")
    foreign = _binding(db, other.id)
    with pytest.raises(pool_gate.PoolLifecycleError):
        pool_gate.apply_pool_membership(db, user.id, foreign.id)


def test_approve_candidate_without_binding_fails(db):
    user = _user(db, "approve-nobind")
    candidate = pool_gate.grant_pool_candidate(db, user.id)
    with pytest.raises(pool_gate.PoolLifecycleError):
        pool_gate.approve_pool_member(db, candidate.id, actor_id=99)


def test_resume_blocked_when_binding_dead(db):
    user = _user(db, "resume-dead")
    binding = _binding(db, user.id)
    member = _member(db, user.id, binding.id, status="suspended",
                     suspended_reason="manual")
    binding.status = "invalid"
    db.commit()
    with pytest.raises(pool_gate.PoolLifecycleError):
        pool_gate.resume_pool_member(db, member.id, actor_id=99)
    db.refresh(member)
    assert member.status == "suspended"


def test_reject_and_terminal_guards(db):
    user = _user(db, "reject-flow")
    binding = _binding(db, user.id)
    member = pool_gate.apply_pool_membership(db, user.id, binding.id)
    pool_gate.reject_pool_member(db, member.id, actor_id=99)
    assert member.status == "rejected"
    with pytest.raises(pool_gate.PoolLifecycleError):
        pool_gate.approve_pool_member(db, member.id, actor_id=99)


# ------------------------------------------------------------ 违规标志


def test_eject_with_violation_sets_flag_fields(db):
    user = _user(db, "eject-viol")
    binding = _binding(db, user.id)
    member = _member(db, user.id, binding.id, status="active")
    pool_gate.eject_pool_member(
        db, member.id, actor_id=7, violation=True,
        violation_reason="盘内塞私货")
    db.refresh(member)
    assert member.status == "ejected"
    assert member.violation_flagged is True
    assert member.violation_reason == "盘内塞私货"
    assert member.violation_flagged_at is not None
    assert member.violation_flagged_by == 7
    assert member.violation_cleared_at is None
    # PR-6 结算的判定依据：违规清退 = ejected + flagged
    assert pool_gate.member_violation_ejected(db, member.id) is True


def test_eject_without_violation_not_flagged(db):
    user = _user(db, "eject-plain")
    binding = _binding(db, user.id)
    member = _member(db, user.id, binding.id, status="active")
    pool_gate.eject_pool_member(db, member.id, actor_id=7)
    db.refresh(member)
    assert member.violation_flagged is False
    assert pool_gate.member_violation_ejected(db, member.id) is False


def test_clear_violation_records_clearer(db):
    user = _user(db, "clear-viol")
    binding = _binding(db, user.id)
    member = _member(db, user.id, binding.id, status="ejected",
                     suspended_reason="admin_eject",
                     violation_flagged=True, violation_reason="误判",
                     violation_flagged_by=7)
    pool_gate.clear_member_violation(db, member.id, actor_id=8)
    db.refresh(member)
    assert member.violation_flagged is False
    assert member.violation_cleared_by == 8
    assert member.violation_cleared_at is not None
    # 置位痕迹保留（谁置位仍可查）
    assert member.violation_flagged_by == 7
    assert member.violation_reason == "误判"


# ------------------------------------------------------------ 退出与连带


def test_exit_releases_copies_best_effort(db):
    user = _user(db, "exit-copies")
    binding = _binding(db, user.id)
    member = _member(db, user.id, binding.id, status="active")
    v1 = _variant(db, member.id, item_id=42)
    v2 = _variant(db, member.id, item_id=43)
    fake = _FakeDriveClient()
    result = pool_gate.exit_pool_membership(
        db, member.id, client_factory=lambda db_, b_: fake)
    db.refresh(member)
    assert member.status == "exited"
    assert member.suspended_reason == "contributor_exit"
    assert sorted(c[1] for c in fake.calls) == sorted(
        [v1.variant_file_id, v2.variant_file_id])
    assert all(c[0] == f"myDrive:{user.id}" for c in fake.calls)
    for v in (v1, v2):
        db.refresh(v)
        assert v.status == "released"
    assert result["released"] == 2
    assert _events(db, member.id)[-1] == (
        "active", "exited", "contributor_exit")


def test_exit_delete_failure_still_releases_and_exits(db):
    user = _user(db, "exit-del-fail")
    binding = _binding(db, user.id)
    member = _member(db, user.id, binding.id, status="active")
    v1 = _variant(db, member.id)
    fake = _FakeDriveClient(fail=True)
    result = pool_gate.exit_pool_membership(
        db, member.id, client_factory=lambda db_, b_: fake)
    db.refresh(member)
    db.refresh(v1)
    assert member.status == "exited"
    assert v1.status == "released"
    assert result["delete_failed"] == 1


def test_binding_failed_hook_suspends_member(db):
    user = _user(db, "hook-failed")
    binding = _binding(db, user.id)
    member = pool_gate.apply_pool_membership(db, user.id, binding.id)
    pool_gate.approve_pool_member(db, member.id, actor_id=99)
    # 与 drive_oauth._apply_refresh_failure 同一落态口（ub._mark_failed）
    user_bindings._mark_failed(db, binding, "auth_error")
    db.commit()
    db.refresh(member)
    assert member.status == "suspended"
    assert member.suspended_reason == "binding_invalid"
    assert _events(db, member.id)[-1] == (
        "active", "suspended", "binding_invalid")


def test_binding_failed_hook_noop_without_member(db):
    user = _user(db, "hook-noop")
    binding = _binding(db, user.id)
    user_bindings._mark_failed(db, binding, "expired")
    db.commit()
    assert db.query(em.PoolMember).count() == 0


def test_delete_binding_hook_exits_member(db):
    user = _user(db, "hook-delete")
    binding = _binding(db, user.id)
    member = pool_gate.apply_pool_membership(db, user.id, binding.id)
    pool_gate.approve_pool_member(db, member.id, actor_id=99)
    variant = _variant(db, member.id)
    user_bindings.delete_binding(db, user.id, binding.id)
    db.refresh(member)
    db.refresh(variant)
    assert member.status == "exited"
    assert variant.status == "released"
    assert db.query(em.UserStorageBinding).filter_by(
        id=binding.id).first() is None
    assert _events(db, member.id)[-1][1] == "exited"


# ------------------------------------------------------------ 管理端 API


@pytest.fixture()
def admin_user(db):
    user = base_models.WebUser(
        username="pooladmin", password_hash="x",
        is_active=True, is_staff=True, admin_role="super")
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


@pytest.fixture()
def admin_client(db, admin_user):
    from backend.main import app

    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_current_admin] = lambda: admin_user
    try:
        yield TestClient(app, raise_server_exceptions=False)
    finally:
        app.dependency_overrides.clear()


def test_admin_grant_candidate_endpoint(admin_client, db):
    user = _user(db, "grant-me")
    resp = admin_client.post(
        "/api/admin/pool/candidates", json={"user_id": user.id})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "candidate"
    assert body["user_id"] == user.id
    again = admin_client.post(
        "/api/admin/pool/candidates", json={"user_id": user.id})
    assert again.json()["id"] == body["id"]
    missing = admin_client.post(
        "/api/admin/pool/candidates", json={"user_id": 999999})
    assert missing.status_code == 404


def test_admin_member_lifecycle_endpoints(admin_client, db):
    user = _user(db, "api-flow")
    binding = _binding(db, user.id)
    member = pool_gate.apply_pool_membership(db, user.id, binding.id)

    listed = admin_client.get("/api/admin/pool/members")
    assert listed.status_code == 200, listed.text
    rows = listed.json()["members"]
    assert len(rows) == 1
    assert rows[0]["username"] == "api-flow"
    assert rows[0]["status"] == "pending"

    ok = admin_client.post(f"/api/admin/pool/members/{member.id}/approve")
    assert ok.status_code == 200, ok.text
    assert ok.json()["status"] == "active"

    bad = admin_client.post(f"/api/admin/pool/members/{member.id}/approve")
    assert bad.status_code == 400

    patched = admin_client.patch(
        f"/api/admin/pool/members/{member.id}",
        json={"capacity_cap_bytes": 12345})
    assert patched.status_code == 200, patched.text
    assert patched.json()["capacity_cap_bytes"] == 12345

    suspended = admin_client.post(
        f"/api/admin/pool/members/{member.id}/suspend")
    assert suspended.json()["status"] == "suspended"
    resumed = admin_client.post(
        f"/api/admin/pool/members/{member.id}/resume")
    assert resumed.json()["status"] == "active"

    ejected = admin_client.post(
        f"/api/admin/pool/members/{member.id}/eject",
        json={"violation": True, "violation_reason": "塞私货"})
    assert ejected.status_code == 200, ejected.text
    assert ejected.json()["violation_flagged"] is True

    cleared = admin_client.post(
        f"/api/admin/pool/members/{member.id}/clear-violation")
    assert cleared.status_code == 200, cleared.text
    assert cleared.json()["violation_flagged"] is False
    assert cleared.json()["violation_cleared_by"] is not None


def test_admin_overview_and_copies(admin_client, db):
    user = _user(db, "overview")
    binding = _binding(db, user.id)
    member = _member(db, user.id, binding.id, status="active")
    variant = _variant(db, member.id, item_id=77, size=2048)

    overview = admin_client.get("/api/admin/pool/overview")
    assert overview.status_code == 200, overview.text
    body = overview.json()
    assert body["members_active"] == 1
    assert body["copies_total"] == 1
    assert body["copies_bytes"] == 2048

    copies = admin_client.get(
        "/api/admin/pool/copies", params={"item_id": 77})
    assert copies.status_code == 200, copies.text
    rows = copies.json()["copies"]
    assert len(rows) == 1 and rows[0]["id"] == variant.id

    released = admin_client.post(
        f"/api/admin/pool/copies/{variant.id}/release")
    assert released.status_code == 200, released.text
    db.refresh(variant)
    assert variant.status == "stale"


# ------------------------------------------------------------ 用户端 API


@pytest.fixture()
def user_client(db):
    from backend.emby_server.portal import get_admin_or_emby_user
    from backend.main import app

    holder = {}

    def _current_user():
        return holder["user"]

    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_admin_or_emby_user] = _current_user
    try:
        yield TestClient(app, raise_server_exceptions=False), holder
    finally:
        app.dependency_overrides.clear()


def test_user_contribution_flow(user_client, db):
    client, holder = user_client
    user = _user(db, "contributor")
    holder["user"] = user
    binding = _binding(db, user.id)

    before = client.get("/api/user/emby/pool/contribution")
    assert before.status_code == 200, before.text
    body = before.json()
    assert body["pool_enabled"] is False
    entry = [b for b in body["bindings"] if b["binding_id"] == binding.id]
    assert len(entry) == 1
    assert entry[0]["member"] is None
    assert entry[0]["can_apply"] is True

    applied = client.post(
        "/api/user/emby/pool/contribution/apply",
        json={"binding_id": binding.id})
    assert applied.status_code == 200, applied.text
    assert applied.json()["member"]["status"] == "pending"

    after = client.get("/api/user/emby/pool/contribution")
    entry = [b for b in after.json()["bindings"]
             if b["binding_id"] == binding.id][0]
    assert entry["member"]["status"] == "pending"

    member_id = entry["member"]["id"]
    withdrawn = client.post(
        "/api/user/emby/pool/contribution/withdraw",
        json={"member_id": member_id})
    assert withdrawn.status_code == 200, withdrawn.text
    assert withdrawn.json()["member"]["status"] == "exited"


def test_user_contribution_no_binding_states(user_client, db):
    client, holder = user_client
    user = _user(db, "no-bindings")
    holder["user"] = user
    resp = client.get("/api/user/emby/pool/contribution")
    assert resp.status_code == 200, resp.text
    assert resp.json()["bindings"] == []
    bad = client.post(
        "/api/user/emby/pool/contribution/apply",
        json={"binding_id": 424242})
    assert bad.status_code in (400, 404)
