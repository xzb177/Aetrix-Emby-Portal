# -*- coding: utf-8 -*-
"""公共贡献池 PR-1 地基（goal-33）：两表 + 配置家族 + 判定纯函数 + 绑定上限。

本单只落地基、不接线：新表/新配置/新判定入口全部 standalone，
既有 ensure_binding_playback_allowed* 的行为必须逐字不变（红线回归）。
口径来源：hidden_files/pr1-task.md + design-pool-v1 §2/§3/§4.1 +
GOAL.md 通审决议（默认值按补充节修正值：单盘 4.5TB、保底 300GiB、
日转存 600GiB；新增第 16 键 pool_member_max_bindings_per_user 默认 10）。
"""
from __future__ import annotations

import os

os.environ.setdefault("DATABASE_TYPE", "sqlite")
os.environ.setdefault("REDIS_ENABLED", "false")

import uuid
from datetime import date

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend import config_schema, config_service, models as base_models
from backend.emby_server import models as em
from backend.emby_server import playback_gate as gate
from backend.emby_server import pool_gate
from backend.integrations import store

GIB = 1024 ** 3

EXPECTED_DEFAULTS = {
    "pool_enabled": False,
    "pool_audience": "all",
    "pool_join_mode": "apply_review",
    "pool_enabled_providers": "drive",
    "pool_member_capacity_bytes": 4_500_000_000_000,
    "pool_member_reserve_free_bytes": 300 * GIB,
    "pool_member_daily_transfer_bytes": 600 * GIB,
    "pool_member_daily_transfer_jobs": 0,
    "pool_item_max_copies": 2,
    "pool_copy_idle_evict_days": 30,
    "pool_global_concurrency": 2,
    "pool_enqueue_jitter_sec": 120,
    "pool_breaker_quota_hits_per_day": 3,
    "pool_breaker_cooldown_sec": 3600,
    "pool_pick_top_k": 3,
    "pool_member_max_bindings_per_user": 10,
}


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


def _user(db, name):
    user = base_models.WebUser(username=name, password_hash="x")
    db.add(user)
    db.commit()
    return user


def _binding(db, user_id, status="active", provider="drive", label="盘"):
    cred = em.MountCredential(
        mount_id=None, scope="user", owner_user_id=user_id,
        sealed_credential="", key_id="fernet-v1", status=status)
    db.add(cred)
    db.flush()
    binding = em.UserStorageBinding(
        user_id=user_id, provider=provider, account_label=label,
        credential_id=cred.id, status=status)
    db.add(binding)
    db.commit()
    return binding


def _library(db, name, upstream_source_ids=""):
    lib = em.Library(guid=uuid.uuid4().hex, name=name,
                     upstream_source_ids=upstream_source_ids)
    db.add(lib)
    db.commit()
    return lib


def _item(db, lib_id):
    item = em.MediaItem(
        guid=uuid.uuid4().hex, library_id=lib_id, item_type="movie",
        name="片")
    db.add(item)
    db.commit()
    return item


def _set_pool(db, **values):
    config_service.save_config(db, values, schema="pool")
    db.commit()


# ---------------------------------------------------------------- 两表


def test_pool_tables_created_with_candidate_nullable_binding(db):
    member = pool_gate.grant_pool_candidate(db, 4242)
    assert member.status == "candidate"
    assert member.binding_id is None
    assert member.user_id == 4242
    row = db.query(em.PoolMember).filter_by(user_id=4242).one()
    assert row.id == member.id


def test_pool_member_binding_id_unique(db):
    b = _binding(db, 1)
    db.add(em.PoolMember(binding_id=b.id, user_id=1, provider="drive",
                         status="active"))
    db.commit()
    db.add(em.PoolMember(binding_id=b.id, user_id=2, provider="drive",
                         status="active"))
    with pytest.raises(IntegrityError):
        db.commit()
    db.rollback()


def test_pool_usage_daily_unique_per_member_day(db):
    db.add(em.PoolUsageDaily(member_id=7, day=date(2026, 11, 7),
                             transfer_jobs=1, transfer_bytes=100,
                             play_starts=2, quota_hits=0))
    db.commit()
    db.add(em.PoolUsageDaily(member_id=7, day=date(2026, 11, 7)))
    with pytest.raises(IntegrityError):
        db.commit()
    db.rollback()
    # 同成员不同日、同日不同成员都合法
    db.add(em.PoolUsageDaily(member_id=7, day=date(2026, 11, 8)))
    db.add(em.PoolUsageDaily(member_id=8, day=date(2026, 11, 7)))
    db.commit()


# ---------------------------------------------------------------- 配置家族


def test_pool_config_defaults_exact(db):
    payload = pool_gate.pool_config_payload(db)
    assert payload == EXPECTED_DEFAULTS
    assert len(payload) == 16


def test_pool_schema_registered_with_16_keys():
    assert "pool" in config_service.SCHEMAS
    schema = config_service.SCHEMAS["pool"]
    assert set(schema) == set(EXPECTED_DEFAULTS)
    assert schema is config_schema.POOL_CONFIG_SCHEMA


def test_pool_config_save_roundtrip(db):
    _set_pool(db, pool_enabled=True, pool_item_max_copies=4,
              pool_member_max_bindings_per_user=7)
    payload = pool_gate.pool_config_payload(db)
    assert payload["pool_enabled"] is True
    assert payload["pool_item_max_copies"] == 4
    assert payload["pool_member_max_bindings_per_user"] == 7
    # 其余键仍回默认
    assert payload["pool_member_capacity_bytes"] == 4_500_000_000_000


def test_pool_config_invalid_batch_rejected_wholly(db):
    with pytest.raises(config_schema.ConfigValueError):
        config_service.save_config(
            db, {"pool_item_max_copies": 4, "pool_audience": "nobody"},
            schema="pool")
    db.rollback()
    # 整批拒绝：合法键也不许落库
    assert db.query(base_models.SystemConfig).filter_by(
        key="pool_item_max_copies").first() is None
    assert pool_gate.pool_config_payload(db)["pool_item_max_copies"] == 2


@pytest.mark.parametrize("key,bad", [
    ("pool_item_max_copies", 0),
    ("pool_item_max_copies", 9),
    ("pool_member_max_bindings_per_user", 0),
    ("pool_breaker_cooldown_sec", 30),
    ("pool_join_mode", "whatever"),
    ("pool_enabled", "maybe"),
])
def test_pool_config_invalid_values(db, key, bad):
    with pytest.raises(config_schema.ConfigValueError):
        config_service.save_config(db, {key: bad}, schema="pool")
    db.rollback()


# ---------------------------------------------------------------- 判定纯函数


def test_eligible_cms_item_is_off(db):
    _set_pool(db, pool_enabled=True)
    lib = _library(db, "cms库", upstream_source_ids="7")
    item = _item(db, lib.id)
    user = _user(db, "u1")
    assert pool_gate.pool_eligible(db, user, item) == "off"
    decision = pool_gate.ensure_playback_allowed_for_item(db, user, item)
    assert decision.allowed is True
    assert decision.kind == "not_applicable"


def test_eligible_pool_off_local_blocked_original_message(db):
    lib = _library(db, "本地库")
    item = _item(db, lib.id)
    user = _user(db, "u2")
    assert pool_gate.pool_eligible(db, user, item) == "off"
    with pytest.raises(HTTPException) as excinfo:
        pool_gate.ensure_playback_allowed_for_item(db, user, item)
    assert excinfo.value.status_code == 403
    assert excinfo.value.detail == gate.GATE_MESSAGE


def test_eligible_pool_on_all_audience(db):
    _set_pool(db, pool_enabled=True)
    lib = _library(db, "本地库")
    item = _item(db, lib.id)
    user = _user(db, "u3")
    assert pool_gate.pool_eligible(db, user, item) == "eligible"
    decision = pool_gate.ensure_playback_allowed_for_item(db, user, item)
    assert decision.allowed is True
    assert decision.kind == "pool"


def test_eligible_whitelist_audience_outside_blocked(db, monkeypatch):
    _set_pool(db, pool_enabled=True, pool_audience="whitelist")
    lib = _library(db, "本地库")
    item = _item(db, lib.id)
    user = _user(db, "u4")
    monkeypatch.setattr("backend.codes.is_whitelist_account",
                        lambda db_, user_: False)
    assert pool_gate.pool_eligible(db, user, item) == "off"
    with pytest.raises(HTTPException) as excinfo:
        pool_gate.ensure_playback_allowed_for_item(db, user, item)
    # 人群外不泄露池：仍是原文案
    assert excinfo.value.detail == gate.GATE_MESSAGE
    monkeypatch.setattr("backend.codes.is_whitelist_account",
                        lambda db_, user_: True)
    assert pool_gate.pool_eligible(db, user, item) == "eligible"


def test_eligible_subscribers_audience(db, monkeypatch):
    _set_pool(db, pool_enabled=True, pool_audience="subscribers")
    lib = _library(db, "本地库")
    item = _item(db, lib.id)
    user = _user(db, "u5")
    monkeypatch.setattr("backend.subscriptions.has_active_subscription",
                        lambda db_, user_id, realm_id=None: False)
    assert pool_gate.pool_eligible(db, user, item) == "off"
    monkeypatch.setattr("backend.subscriptions.has_active_subscription",
                        lambda db_, user_id, realm_id=None: True)
    assert pool_gate.pool_eligible(db, user, item) == "eligible"


def test_decision_bound_user_short_circuits_pool(db):
    # 池关着，已绑用户照样走 bound 放行（池不介入）
    lib = _library(db, "本地库")
    item = _item(db, lib.id)
    user = _user(db, "u6")
    _binding(db, user.id)
    decision = pool_gate.ensure_playback_allowed_for_item(db, user, item)
    assert decision.allowed is True
    assert decision.kind == "bound"


def test_decision_removed_provider_message_preserved(db):
    lib = _library(db, "本地库")
    item = _item(db, lib.id)
    user = _user(db, "u7")
    _binding(db, user.id, status="invalid", provider="pan115")
    with pytest.raises(HTTPException) as excinfo:
        pool_gate.ensure_playback_allowed_for_item(db, user, item)
    assert excinfo.value.detail == gate.GATE_MESSAGE_REMOVED_PROVIDER


def test_redline_existing_gate_unchanged_when_pool_enabled(db):
    """硬闸回归：配置里把池打开，既有门禁对未绑用户仍是原 403。

    新入口本单只 standalone 落地、未接线任何端点——线上行为零变化
    必须由本用例钉住，直到 PR-3 接线时才许改这条断言口径。
    """
    _set_pool(db, pool_enabled=True)
    lib = _library(db, "本地库")
    item = _item(db, lib.id)
    user = _user(db, "u8")
    with pytest.raises(HTTPException) as excinfo:
        gate.ensure_binding_playback_allowed_for_item(db, user, item)
    assert excinfo.value.status_code == 403
    assert excinfo.value.detail == gate.GATE_MESSAGE
    with pytest.raises(HTTPException):
        gate.ensure_binding_playback_allowed(db, user)
    assert gate.sign_allowed_for_item(db, user.id, item) is False


# ---------------------------------------------------------------- 绑定上限


def test_binding_limit_plain_user_still_3(db, monkeypatch):
    monkeypatch.delenv("BYOS_BINDING_LIMIT_PER_USER", raising=False)
    user = _user(db, "plain")
    assert pool_gate.binding_limit_for_user(db, user) == 3
    assert pool_gate.binding_limit_for_user(db, user.id) == 3


def test_grant_pool_candidate_idempotent(db):
    user = _user(db, "cand")
    first = pool_gate.grant_pool_candidate(db, user.id)
    second = pool_gate.grant_pool_candidate(db, user.id)
    assert first.id == second.id
    rows = db.query(em.PoolMember).filter_by(user_id=user.id).all()
    assert len(rows) == 1
    assert rows[0].status == "candidate"


def test_grant_pool_candidate_existing_member_not_duplicated(db):
    user = _user(db, "member")
    db.add(em.PoolMember(user_id=user.id, provider="drive",
                         status="active"))
    db.commit()
    got = pool_gate.grant_pool_candidate(db, user.id)
    assert got.status == "active"
    assert db.query(em.PoolMember).filter_by(user_id=user.id).count() == 1


def test_binding_limit_candidate_uses_pool_key(db, monkeypatch):
    monkeypatch.delenv("BYOS_BINDING_LIMIT_PER_USER", raising=False)
    user = _user(db, "cand2")
    pool_gate.grant_pool_candidate(db, user.id)
    assert pool_gate.binding_limit_for_user(db, user) == 10
    # 改配置即生效
    _set_pool(db, pool_member_max_bindings_per_user=7)
    assert pool_gate.binding_limit_for_user(db, user) == 7


@pytest.mark.parametrize("status", ["pending", "active", "suspended"])
def test_binding_limit_member_statuses_use_pool_key(db, status):
    user = _user(db, f"m-{status}")
    db.add(em.PoolMember(user_id=user.id, provider="drive", status=status))
    db.commit()
    assert pool_gate.binding_limit_for_user(db, user) == 10


def test_binding_limit_exited_member_falls_back_to_3(db, monkeypatch):
    monkeypatch.delenv("BYOS_BINDING_LIMIT_PER_USER", raising=False)
    user = _user(db, "exited")
    db.add(em.PoolMember(user_id=user.id, provider="drive",
                         status="exited"))
    db.commit()
    assert pool_gate.binding_limit_for_user(db, user) == 3


def test_drive_oauth_quota_uses_pool_limit_for_candidate(db, monkeypatch):
    """drive_oauth 建绑校验改走新判定：普通用户 3 个满额拒绝、
    候选用户同数未满（上限 10）放行校验。"""
    from backend.emby_server import drive_oauth

    monkeypatch.delenv("BYOS_BINDING_LIMIT_PER_USER", raising=False)
    plain = _user(db, "oauth-plain")
    cand = _user(db, "oauth-cand")
    for i in range(3):
        _binding(db, plain.id, label=f"p{i}")
        _binding(db, cand.id, label=f"c{i}")
    pool_gate.grant_pool_candidate(db, cand.id)
    # 候选：第 4 个 label 校验放行
    assert drive_oauth._validate_label_and_quota(
        db, cand.id, "新盘") == "新盘"
    # 普通用户：已达个人上限 3，拒绝
    with pytest.raises(drive_oauth.DriveOAuthError) as excinfo:
        drive_oauth._validate_label_and_quota(db, plain.id, "新盘")
    assert "上限" in excinfo.value.message
