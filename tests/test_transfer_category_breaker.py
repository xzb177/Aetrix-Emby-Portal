# -*- coding: utf-8 -*-
"""转存类别级熔断开关（预见层 v2 第一层余项 PR-2）。

背景（2026-10-31 复核实证）：外部条件整体突变（授权范围变更、
某 provider 整体不可用）时没有按类停派开关，只能人肉全表取消
（2026-10-29 的 293 单即如此）。本单落地按「provider＋失败
类别（error_kind，复用现有分类字段，不新造第二套）」维度的
暂停规则：

- 配置 SystemConfig transfer_paused_categories，JSON 数组
  [{provider, error_kind}]；error_kind="*" = 该 provider 全部
  类别（含尚无失败记录的新单）；空配置 = 现行行为逐字不变；
- worker 认领（claim_due_jobs）与点播当场派发
  （claim_job_for_inline）都跳过命中规则的任务：留在 pending、
  不烧 attempts、不判死、不回落；解暂停后自然恢复被认领。

全部离线：SQLite :memory:，不碰真实 Drive/115、不碰 Redis。
"""
import os

os.environ.setdefault("DATABASE_TYPE", "sqlite")
os.environ.setdefault("REDIS_ENABLED", "false")

import json
from datetime import datetime

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend import models as base_models
from backend.emby_server import models as em
from backend.emby_server import transfer_variant as tv
from backend.config_schema import TRANSFER_CONFIG_SCHEMA
from backend.integrations import store


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
    yield
    store.invalidate()


def _set_config(db, raw):
    row = (db.query(base_models.SystemConfig)
           .filter(base_models.SystemConfig.key
                   == "transfer_paused_categories").first())
    if row is None:
        db.add(base_models.SystemConfig(
            key="transfer_paused_categories", value=raw))
    else:
        row.value = raw
    db.commit()
    store.invalidate()


def _job(db, provider="drive", error_kind=None, attempts=0, owner_ref="u1",
         **kw):
    row = em.TransferJob(item_id=kw.pop("item_id", 1), trigger="playback",
                         drive_id="drive1", provider=provider,
                         owner_scope="user", owner_ref=owner_ref,
                         status="pending", error_kind=error_kind,
                         attempts=attempts, **kw)
    db.add(row)
    db.commit()
    return row


# ---------- 认领跳过 ----------

def test_paused_category_not_claimed_attempts_unchanged(db):
    _set_config(db, json.dumps(
        [{"provider": "drive", "error_kind": "retriable"}]))
    held = _job(db, "drive", "retriable", attempts=2, owner_ref="u1")
    fresh = _job(db, "drive", None, attempts=0, owner_ref="u2")
    other = _job(db, "pan115", "retriable", attempts=1, owner_ref="u3")

    claimed = tv.claim_due_jobs(db, now=datetime.now(), limit=10)
    assert held.id not in claimed
    assert fresh.id in claimed
    assert other.id in claimed
    db.refresh(held)
    assert held.status == "pending"
    assert held.attempts == 2


def test_provider_wildcard_pauses_all_kinds(db):
    _set_config(db, json.dumps(
        [{"provider": "drive", "error_kind": "*"}]))
    fresh = _job(db, "drive", None, owner_ref="u1")
    retried = _job(db, "drive", "retriable", attempts=1, owner_ref="u2")
    other = _job(db, "pan115", None, owner_ref="u3")

    claimed = tv.claim_due_jobs(db, now=datetime.now(), limit=10)
    assert claimed == [other.id]
    db.refresh(fresh)
    db.refresh(retried)
    assert fresh.status == "pending"
    assert retried.status == "pending"


def test_unpause_resumes_claiming(db):
    _set_config(db, json.dumps(
        [{"provider": "drive", "error_kind": "retriable"}]))
    held = _job(db, "drive", "retriable", attempts=1)
    assert tv.claim_due_jobs(db, now=datetime.now(), limit=10) == []

    _set_config(db, "[]")
    claimed = tv.claim_due_jobs(db, now=datetime.now(), limit=10)
    assert claimed == [held.id]


def test_empty_config_is_current_behavior(db):
    a = _job(db, "drive", "retriable", attempts=1, owner_ref="u1")
    b = _job(db, "pan115", None, owner_ref="u2")
    claimed = tv.claim_due_jobs(db, now=datetime.now(), limit=10)
    assert sorted(claimed) == sorted([a.id, b.id])


def test_inline_claim_blocked_when_paused(db):
    _set_config(db, json.dumps(
        [{"provider": "drive", "error_kind": "retriable"}]))
    job = _job(db, "drive", "retriable", attempts=1)
    assert tv.claim_job_for_inline(db, job.id) is False
    db.refresh(job)
    assert job.status == "pending"
    assert job.attempts == 1

    _set_config(db, "[]")
    assert tv.claim_job_for_inline(db, job.id) is True
    db.refresh(job)
    assert job.status == "running"


# ---------- 配置四齐：注册 + 写入校验 + 回读 ----------

def test_config_registered_and_roundtrip(db):
    assert "transfer_paused_categories" in TRANSFER_CONFIG_SCHEMA
    payload = tv.write_transfer_config(
        db, transfer_paused_categories=(
            '[{"provider": "drive", "error_kind": "retriable"}]'))
    assert json.loads(payload["transfer_paused_categories"]) == [
        {"provider": "drive", "error_kind": "retriable"}]
    assert tv.transfer_paused_categories(db) == [
        {"provider": "drive", "error_kind": "retriable"}]


def test_write_rejects_bad_rules(db):
    with pytest.raises(ValueError):
        tv.write_transfer_config(db, transfer_paused_categories="不是JSON")
    with pytest.raises(ValueError):
        tv.write_transfer_config(
            db, transfer_paused_categories=(
                '[{"provider": "nosuch", "error_kind": "dead"}]'))
    with pytest.raises(ValueError):
        tv.write_transfer_config(
            db, transfer_paused_categories='[{"provider": "drive"}]')


def test_runtime_read_skips_bad_entries(db):
    _set_config(db, '[{"provider": "drive"}, {"bad": 1},'
                    ' {"provider": "pan115", "error_kind": "dead"}]')
    assert tv.transfer_paused_categories(db) == [
        {"provider": "pan115", "error_kind": "dead"}]
