# -*- coding: utf-8 -*-
"""跨 provider 单 aging 防饿死（2026-10-30 修复包 PR-B）。

生产案：1174（prefetch 跨单、6.9GB）自 17:53 CST 起 pending、
attempts=0 饿约 12 小时——_pick_candidates 先 fast（playback）
后 slow 且无 aging，slow 组跨单的等待只决定组内 owner 顺序，
永不提权；跨槽（cross_cap=2）一空出来就被新 playback 跨单拿走。

本文件钉住改后口径：
- slow 组跨单等待（now - created_at）≥ transfer_cross_aging_sec
  （SystemConfig，默认 1800，钳 [60, 86400]）时，在跨槽竞争中按
  fast 组待遇参与挑选；组内公平键（owner 在途数、owner 最老
  等待、owner 键）不变；
- 非跨单（同 provider）排序零变化：再老的 prefetch 同 provider
  单也不提权；
- claim_pick 日志给提权单加 aged=1 标记；
- 阈值配成极大（功能等同关）时行为与现行一致。

全部离线：SQLite :memory:，不碰真实 Drive/115、不碰 Redis。
"""
import os

os.environ.setdefault("DATABASE_TYPE", "sqlite")
os.environ.setdefault("REDIS_ENABLED", "false")

import logging
from datetime import datetime, timedelta

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
    tv._paused_until.clear()
    tv._WAKE_EVENT.clear()
    yield
    store.invalidate()
    tv._paused_until.clear()
    tv._WAKE_EVENT.clear()


def _job(db, item_id=1, trigger="playback", owner_ref="u1", status="pending",
         cross=False, created_ago_sec=None, lease_sec=None, **kw):
    if cross:
        kw.setdefault("provider", "drive")
        kw.setdefault("dest_drive_id", f"pan115:user-{owner_ref}")
    row = em.TransferJob(item_id=item_id, trigger=trigger, drive_id="drive1",
                         owner_scope="user", owner_ref=owner_ref,
                         status=status, **kw)
    db.add(row)
    db.commit()
    if created_ago_sec is not None:
        row.created_at = datetime.now() - timedelta(seconds=created_ago_sec)
    if lease_sec is not None:
        row.lease_expires_at = datetime.now() + timedelta(seconds=lease_sec)
    if created_ago_sec is not None or lease_sec is not None:
        db.commit()
    return row


def _finish(db, row):
    row.status = "done"
    row.lease_expires_at = None
    row.claimed_by = None
    db.commit()


def _binding(db, user_id, provider="pan115", status="active"):
    cred = em.MountCredential(scope="user", owner_user_id=user_id,
                              status="active")
    db.add(cred)
    db.commit()
    row = em.UserStorageBinding(user_id=user_id, provider=provider,
                                account_label="test",
                                credential_id=cred.id, status=status)
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


def _two_cross_slots_busy(db):
    """两个跨槽被 playback 跨单占满（cross_cap 默认 2）。"""
    run1 = _job(db, item_id=11, owner_ref="1064", status="running",
                cross=True, lease_sec=300)
    run2 = _job(db, item_id=12, owner_ref="1064", status="running",
                cross=True, lease_sec=300)
    return run1, run2


# ---------- ① 超龄 prefetch 跨单提权（红→绿） ----------

def test_aged_prefetch_cross_wins_freed_slot(db, monkeypatch, caplog):
    """槽空出时：超龄 prefetch 跨单（等 3600s ≥ 默认阈值 1800）
    与新 playback 跨单（等 10s）竞争——新码先领超龄单（组内
    公平键不变：两 owner 均 0 在途，超龄单 owner 的最老等待更久）；
    旧码 fast 组只有 playback，先领新 playback 单。"""
    monkeypatch.setattr(tv, "TRANSFER_WORKER_PER_USER_MAX", 2)
    _binding(db, 985)
    _binding(db, 1001)
    run1, run2 = _two_cross_slots_busy(db)
    aged = _job(db, item_id=13, trigger="prefetch", owner_ref="985",
                cross=True, created_ago_sec=3600)
    fresh = _job(db, item_id=14, trigger="playback", owner_ref="1001",
                 cross=True, created_ago_sec=10)
    # 跨槽满：谁都领不到（与基线一致）。
    assert tv.claim_due_jobs(db, limit=4) == []
    _finish(db, run1)
    with caplog.at_level(logging.INFO,
                         logger="backend.emby_server.transfer_variant"):
        claimed = tv.claim_due_jobs(db, limit=1)
    assert claimed == [aged.id]
    lines = [r.getMessage() for r in caplog.records
             if "claim_pick" in r.getMessage()]
    assert len(lines) == 1
    assert f"job={aged.id}" in lines[0]
    assert "aged=1" in lines[0]
    # 下一槽空出：新 playback 单照常领走（提权不是永久压制点播）。
    _finish(db, run2)
    assert tv.claim_due_jobs(db, limit=1) == [fresh.id]


# ---------- ② 锚：未超龄 prefetch 仍排在 playback 之后 ----------

def test_unaged_prefetch_cross_still_after_playback(db, monkeypatch):
    monkeypatch.setattr(tv, "TRANSFER_WORKER_PER_USER_MAX", 2)
    _binding(db, 985)
    _binding(db, 1001)
    run1, _run2 = _two_cross_slots_busy(db)
    _job(db, item_id=15, trigger="prefetch", owner_ref="985",
         cross=True, created_ago_sec=60)
    fresh = _job(db, item_id=16, trigger="playback", owner_ref="1001",
                 cross=True, created_ago_sec=10)
    _finish(db, run1)
    assert tv.claim_due_jobs(db, limit=1) == [fresh.id]


# ---------- ③ 锚：同 provider 排序零变化（aging 只管跨单） ----------

def test_aged_prefetch_same_provider_not_promoted(db, monkeypatch):
    """同 provider 的 prefetch 单哪怕等了 2 小时也不提权：
    playback 同 provider 单仍先领（非跨单排序零变化）。"""
    monkeypatch.setattr(tv, "TRANSFER_WORKER_PER_USER_MAX", 2)
    _job(db, item_id=17, trigger="prefetch", owner_ref="985",
         created_ago_sec=7200)
    fast = _job(db, item_id=18, trigger="playback", owner_ref="1001",
                created_ago_sec=10)
    assert tv.claim_due_jobs(db, limit=1) == [fast.id]


# ---------- ④ 锚：阈值极大（功能等同关）行为与现行一致 ----------

def test_huge_threshold_disables_aging(db, monkeypatch):
    monkeypatch.setattr(tv, "TRANSFER_WORKER_PER_USER_MAX", 2)
    _set_sysconfig(db, "transfer_cross_aging_sec", "86400")
    _binding(db, 985)
    _binding(db, 1001)
    run1, _run2 = _two_cross_slots_busy(db)
    _job(db, item_id=19, trigger="prefetch", owner_ref="985",
         cross=True, created_ago_sec=3600)
    fresh = _job(db, item_id=20, trigger="playback", owner_ref="1001",
                 cross=True, created_ago_sec=10)
    _finish(db, run1)
    assert tv.claim_due_jobs(db, limit=1) == [fresh.id]


# ---------- 配置键口径 ----------

def test_cross_aging_config_defaults_and_clamps(db):
    assert TRANSFER_CONFIG_SCHEMA["transfer_cross_aging_sec"] == (
        "int", 60, 86400)
    assert tv.cross_aging_sec(db) == 1800
    assert tv.transfer_config_payload(db)["transfer_cross_aging_sec"] == 1800
    _set_sysconfig(db, "transfer_cross_aging_sec", "30")
    assert tv.cross_aging_sec(db) == 60
    _set_sysconfig(db, "transfer_cross_aging_sec", "999999")
    assert tv.cross_aging_sec(db) == 86400


def test_cross_aging_write_roundtrip(db):
    payload = tv.write_transfer_config(db, transfer_cross_aging_sec=900)
    assert payload["transfer_cross_aging_sec"] == 900
    assert tv.cross_aging_sec(db) == 900
    with pytest.raises(ValueError):
        tv.write_transfer_config(db, transfer_cross_aging_sec=10)
