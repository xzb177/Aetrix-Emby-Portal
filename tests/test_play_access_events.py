# -*- coding: utf-8 -*-
"""goal-38 审计第一步：个人盘起播取用事件（drive_play_access_events）。

钉住的口径：
- note_play_access 用独立会话落库，成功 True；DB 失败返回
  False 且绝不抛（照 transfer_decisions 先例）；
- 同一 (user, item, action, result, reason) 在限频窗口内只落
  一行，节流命中返回 False 不写库；
- binding_id 未传时尽力解析该用户 active 的 Drive 绑定，
  解析不到留空、不丢整行；
- purge 只删超保留期（默认 30 天）的行；
- 池门禁 ensure_pool_copy_gate 的 verdict 落一行决策留痕
  （scope=pool_gate），返回值逐字不变。

全部离线：临时文件 SQLite；monkeypatch 只打模块属性（与既有
决策留痕测试同口径），不打单例实例属性。
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
from backend.emby_server import play_access_events as pae
from backend.emby_server import pool_gate
from backend.emby_server import transfer_decisions as td


@pytest.fixture()
def db_factory(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path}/play_access.db",
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


@pytest.fixture(autouse=True)
def _reset_throttle():
    pae._last_written.clear()
    for key in pae._stats:
        pae._stats[key] = 0
    yield
    pae._last_written.clear()


@pytest.fixture()
def note_here(db_factory, monkeypatch):
    """让记录模块的独立会话落到测试库（只打模块属性）。"""
    monkeypatch.setattr(pae, "SessionLocal", db_factory)
    return db_factory


def _rows(db):
    return (db.query(em.DrivePlayAccessEvent)
            .order_by(em.DrivePlayAccessEvent.id).all())


# ---------------------------------------------------------------------------
# note_play_access 本体
# ---------------------------------------------------------------------------

def test_note_writes_row_with_detail(db, note_here):
    ok = pae.note_play_access(
        user_id=7, item_id=100, entry="video_stream",
        action="copy_resolve", result="hit",
        detail={"variant_id": 3, "owner_scope": "user"})
    assert ok is True
    rows = _rows(db)
    assert len(rows) == 1
    row = rows[0]
    assert row.user_id == 7
    assert row.item_id == 100
    assert row.entry == "video_stream"
    assert row.action == "copy_resolve"
    assert row.result == "hit"
    assert json.loads(row.detail) == {"variant_id": 3,
                                      "owner_scope": "user"}
    assert row.created_at is not None


def test_note_resolves_active_drive_binding(db, note_here):
    binding = em.UserStorageBinding(
        user_id=7, provider="drive", account_label="mine",
        credential_id=999, status="active")
    db.add(binding)
    db.commit()
    db.refresh(binding)
    assert pae.note_play_access(
        user_id=7, item_id=1, entry="playback_info",
        action="copy_resolve", result="miss") is True
    rows = _rows(db)
    assert rows[0].binding_id == binding.id


def test_note_without_binding_keeps_row(db, note_here):
    assert pae.note_play_access(
        user_id=8, item_id=1, entry="playback_info",
        action="gate_reject", result="denied",
        reason="请先绑定自己的网盘账号才能点播") is True
    rows = _rows(db)
    assert len(rows) == 1
    assert rows[0].binding_id is None
    assert rows[0].reason == "请先绑定自己的网盘账号才能点播"


def test_note_throttles_duplicates(db, note_here):
    kwargs = dict(user_id=7, item_id=5, entry="video_stream",
                  action="copy_resolve", result="miss")
    assert pae.note_play_access(**kwargs) is True
    assert pae.note_play_access(**kwargs) is False
    assert len(_rows(db)) == 1
    # 换 result 不算重复
    assert pae.note_play_access(
        user_id=7, item_id=5, entry="video_stream",
        action="copy_resolve", result="hit") is True
    assert len(_rows(db)) == 2
    assert pae.stats()["throttled"] == 1


def test_note_write_failure_is_silent(db_factory, monkeypatch):
    def _boom():
        raise RuntimeError("db down")

    monkeypatch.setattr(pae, "SessionLocal", _boom)
    assert pae.note_play_access(
        user_id=1, item_id=1, action="gate_reject",
        result="denied") is False
    assert pae.stats()["write_errors"] >= 1


# ---------------------------------------------------------------------------
# 保留期清理
# ---------------------------------------------------------------------------

def test_purge_deletes_only_expired(db, note_here):
    now = datetime.now()
    old = em.DrivePlayAccessEvent(
        user_id=1, entry="video_stream", action="copy_resolve",
        result="hit", created_at=now - timedelta(days=31))
    fresh = em.DrivePlayAccessEvent(
        user_id=1, entry="video_stream", action="copy_resolve",
        result="hit", created_at=now - timedelta(days=1))
    db.add_all([old, fresh])
    db.commit()
    assert pae.purge_expired_events(db, now=now) == 1
    rows = _rows(db)
    assert len(rows) == 1
    assert rows[0].created_at > now - timedelta(days=2)


# ---------------------------------------------------------------------------
# 池门禁 verdict 留痕（scope=pool_gate）
# ---------------------------------------------------------------------------

def test_pool_gate_notes_decision(db, db_factory, monkeypatch):
    monkeypatch.setattr(td, "SessionLocal", db_factory)
    item = SimpleNamespace(id=100)
    user = SimpleNamespace(id=8)
    result = pool_gate.ensure_pool_copy_gate(db, item, user)
    # 池未开启时 impl 的原 verdict 逐字不变
    assert result["verdict"] == "failed"
    assert result["reason"] == "pool_disabled"
    assert result["pool"] is True
    rows = (db.query(em.TransferDecisionRound)
            .filter(em.TransferDecisionRound.scope == "pool_gate")
            .all())
    assert len(rows) == 1
    row = rows[0]
    assert row.resource_key == "item:100"
    assert row.decision == "failed"
    assert row.reason == "pool_disabled"
    cand = json.loads(row.candidates_json)
    assert cand["item_id"] == 100
    assert cand["user_id"] == 8
    assert cand["verdict"] == "failed"
