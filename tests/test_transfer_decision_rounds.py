# -*- coding: utf-8 -*-
"""PanSave 借鉴落地 A 线 PR-1：决策留痕（transfer_decision_rounds）。

蓝本：reports/pansave-deepstudy-20261102.md 第一节落地层——新增
决策记录表，写入点两处（转存门禁 verdict 计算处、聚合任务开轮
处），写入失败静默丢弃不阻断主流程，配保留期清理，管理端只读
查询面看最近轮次。

钉住的口径：
- policy_hash = 参与决策的配置键值规范化 JSON 的 sha256：键序
  无关、同值同哈希、值变哈希变；
- note_decision 用独立会话落库，成功 True；DB 失败返回 False
  且绝不抛（照 play_start_events 写失败静默丢弃先例）；
- 门禁 ensure_user_copy_gate 每次算完 verdict 落一行
  （scope=transfer_gate），留痕炸了门禁照常返回 verdict；
- 聚合 _enqueue_locked 只有真开新轮（created=True）才落一行
  （scope=upstream_aggregate，execution_ref=task_id），复用
  进行中任务不重复落；
- 保留期默认 30 天（SystemConfig 可配），purge 只删过期行；
- list 口径：按 id 倒序、limit 钳制、scope 可过滤。

全部离线：临时文件 SQLite；monkeypatch 只打模块属性（与既有
转存/聚合测试同口径），不打单例实例属性。
"""
import os

os.environ.setdefault("DATABASE_TYPE", "sqlite")
os.environ.setdefault("REDIS_ENABLED", "false")

import json
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend import models as base_models
from backend.emby_server import models as em
from backend.emby_server import transfer_decisions as td
from backend.emby_server import transfer_variant as tv
from backend.emby_server import upstream_aggregate_jobs as uaj
from backend.integrations import store as config_store


@pytest.fixture()
def db_factory(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path}/decision_rounds.db",
        connect_args={"check_same_thread": False, "timeout": 30})
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


@pytest.fixture()
def note_here(db_factory, monkeypatch):
    """让留痕模块的独立会话落到测试库（只打模块属性）。"""
    monkeypatch.setattr(td, "SessionLocal", db_factory)
    return db_factory


def _rows(db):
    return (db.query(em.TransferDecisionRound)
            .order_by(em.TransferDecisionRound.id).all())


# ---------------------------------------------------------------------------
# policy_hash
# ---------------------------------------------------------------------------

def test_policy_hash_ignores_key_order():
    a = td.policy_hash_for({"b": 1, "a": "x", "c": True})
    b = td.policy_hash_for({"c": True, "a": "x", "b": 1})
    assert a == b
    assert len(a) == 64


def test_policy_hash_changes_with_value():
    a = td.policy_hash_for({"limit": 3})
    b = td.policy_hash_for({"limit": 4})
    assert a != b


def test_policy_hash_empty_policy_stable():
    assert td.policy_hash_for({}) == td.policy_hash_for(None)


# ---------------------------------------------------------------------------
# note_decision 本体
# ---------------------------------------------------------------------------

def test_note_decision_writes_row(db, note_here):
    ok = td.note_decision(
        scope="transfer_gate", resource_key="item:11",
        policy={"master": True}, candidates=[{"fid": "a"}],
        decision="preparing", reason="job_created", execution_ref="9")
    assert ok is True
    rows = _rows(db)
    assert len(rows) == 1
    row = rows[0]
    assert row.scope == "transfer_gate"
    assert row.resource_key == "item:11"
    assert row.decision == "preparing"
    assert row.reason == "job_created"
    assert row.execution_ref == "9"
    assert row.policy_hash == td.policy_hash_for({"master": True})
    assert json.loads(row.candidates_json) == [{"fid": "a"}]
    assert row.created_at is not None


def test_note_decision_swallows_db_failure(monkeypatch):
    def _boom():
        raise RuntimeError("db down")

    monkeypatch.setattr(td, "SessionLocal", _boom)
    ok = td.note_decision(scope="transfer_gate", resource_key="item:1",
                          decision="proceed", reason="master_off")
    assert ok is False
    assert td.decision_stats()["write_errors"] >= 1


def test_note_decision_accepts_unserializable_candidates(db, note_here):
    ok = td.note_decision(
        scope="transfer_gate", resource_key="item:12",
        candidates={"at": datetime(2026, 11, 6, 9, 0)},
        decision="proceed", reason="has_ready_variant")
    assert ok is True
    row = _rows(db)[0]
    assert "2026" in row.candidates_json


# ---------------------------------------------------------------------------
# 写入点一：转存门禁 verdict 计算处
# ---------------------------------------------------------------------------

def test_gate_writes_decision_row(db, note_here, monkeypatch):
    monkeypatch.delenv("TRANSFER_VARIANT_ENABLED", raising=False)
    item = SimpleNamespace(id=123, file_path="")
    user = SimpleNamespace(id=7)
    result = tv.ensure_user_copy_gate(db, item, user)
    assert result["verdict"] == "proceed"
    assert result["reason"] == "master_off"
    rows = _rows(db)
    assert len(rows) == 1
    row = rows[0]
    assert row.scope == "transfer_gate"
    assert row.resource_key == "item:123"
    assert row.decision == "proceed"
    assert row.reason == "master_off"
    snap = json.loads(row.candidates_json)
    assert snap["item_id"] == 123
    assert snap["user_id"] == 7


def test_gate_survives_decision_write_failure(db, monkeypatch):
    monkeypatch.delenv("TRANSFER_VARIANT_ENABLED", raising=False)

    def _boom():
        raise RuntimeError("db down")

    monkeypatch.setattr(td, "SessionLocal", _boom)
    item = SimpleNamespace(id=124, file_path="")
    user = SimpleNamespace(id=8)
    result = tv.ensure_user_copy_gate(db, item, user)
    assert result["verdict"] == "proceed"
    assert result["reason"] == "master_off"


# ---------------------------------------------------------------------------
# 写入点二：聚合任务开轮处
# ---------------------------------------------------------------------------

def test_aggregate_round_open_writes_decision(db, note_here):
    view, created = uaj._enqueue_locked(
        db, source_id=42, page_size=50, trigger="manual")
    assert created is True
    rows = _rows(db)
    assert len(rows) == 1
    row = rows[0]
    assert row.scope == "upstream_aggregate"
    assert row.resource_key == "source:42"
    assert row.decision == "round_opened"
    assert row.reason == "manual"
    assert row.execution_ref == view["task_id"]


def test_aggregate_reuse_does_not_write_decision(db, note_here):
    _, created1 = uaj._enqueue_locked(
        db, source_id=43, page_size=50, trigger="manual")
    _, created2 = uaj._enqueue_locked(
        db, source_id=43, page_size=50, trigger="scheduled")
    assert created1 is True and created2 is False
    assert len(_rows(db)) == 1


# ---------------------------------------------------------------------------
# 保留期清理与只读列表
# ---------------------------------------------------------------------------

def _insert_round(db, *, age_days, scope="transfer_gate"):
    row = em.TransferDecisionRound(
        scope=scope, resource_key="item:1", policy_hash="x" * 64,
        candidates_json="{}", decision="proceed", reason="r",
        created_at=datetime.now() - timedelta(days=age_days))
    db.add(row)
    db.commit()
    return row


def test_purge_expired_rounds_default_retention(db):
    _insert_round(db, age_days=5)
    _insert_round(db, age_days=40)
    deleted = td.purge_expired_rounds(db)
    assert deleted == 1
    rows = _rows(db)
    assert len(rows) == 1


def test_purge_expired_rounds_config_override(db):
    _insert_round(db, age_days=5)
    db.add(base_models.SystemConfig(
        key=td.RETENTION_KEY, value="3"))
    db.commit()
    config_store.invalidate(td.RETENTION_KEY)
    deleted = td.purge_expired_rounds(db)
    assert deleted == 1
    assert _rows(db) == []


def test_list_recent_rounds_order_limit_scope(db, note_here):
    for i in range(3):
        td.note_decision(scope="transfer_gate",
                         resource_key=f"item:{i}", decision="proceed",
                         reason="master_off")
    td.note_decision(scope="upstream_aggregate",
                     resource_key="source:1", decision="round_opened",
                     reason="manual")
    all_rows = td.list_recent_rounds(db, limit=10)
    assert [r["resource_key"] for r in all_rows] == [
        "source:1", "item:2", "item:1", "item:0"]
    limited = td.list_recent_rounds(db, limit=2)
    assert len(limited) == 2
    scoped = td.list_recent_rounds(db, scope="upstream_aggregate")
    assert len(scoped) == 1
    assert scoped[0]["decision"] == "round_opened"
