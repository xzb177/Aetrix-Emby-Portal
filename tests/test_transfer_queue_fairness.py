# -*- coding: utf-8 -*-
"""转存队列认领公平化（2026-10-30 用户亲令「不要重复问题」）。

生产案（当日实证）：跨 provider 搬运道并发 2，被 user 1064 的
两单（2.1G＋5.0G）占满，其第三单 pending 还排在 user 985 的
《醉梦》搬运单（job 1192）前面，985 的播放器只收到「准备中」
无限转圈。根因在认领排序：组内 owner 迭代序是 owner 键字典序
（"1064" < "985"），没有「在途数升序 / 等得最久的 owner 先」
这两个键——一单跑完空出槽时，补认领先挑字典序靠前的 1064
第三单，跨道槽再次被其占走。

本文件钉住改后口径：
- 排序键：trigger 组（playback 先）→ owner 在途数升序 →
  owner 名下最老候选等待最久 → owner 键兜底；同 owner 内 FIFO；
- 硬规矩：在途少的 owner 先于在途多的；达 per-owner 上限的
  owner 其 pending 绝不越过未达上限 owner 的 pending；
- 让行只在 job 之间生效（空槽补认领按公平序），跨 provider
  搬运不支持断点续传（Drive→115 是一次 simple_upload 整文件
  请求、115→Drive 的 resumable 会话不落库），不做中途挂起；
- 每次实际认领打一条 claim_pick 结构化日志（job/owner/
  trigger/cross/owner_running/wait），无认领不打。

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


# ---------- 生产案复刻：空出的跨道槽归在途为 0 的 owner ----------

def test_freed_cross_slot_goes_to_zero_running_owner(db, monkeypatch):
    """A（ref=1064，字典序靠前）两单跨单在途＋第三单 pending；
    B（ref=985）0 在途＋一单 pending。跨道满时谁都领不到；A 一单
    完成后空出一个跨道槽，补认领必须归 B，不是 A 的第三单。"""
    monkeypatch.setattr(tv, "TRANSFER_WORKER_PER_USER_MAX", 2)
    _binding(db, 1064)
    _binding(db, 985)
    a_run1 = _job(db, item_id=11, owner_ref="1064", status="running",
                  cross=True, lease_sec=300)
    a_run2 = _job(db, item_id=12, owner_ref="1064", status="running",
                  cross=True, lease_sec=300)
    a_pend = _job(db, item_id=13, owner_ref="1064", cross=True,
                  created_ago_sec=120)
    b_pend = _job(db, item_id=14, owner_ref="985", cross=True,
                  created_ago_sec=60)
    # 跨道 2 槽已满：本轮谁都领不到（行为与基线一致）。
    assert tv.claim_due_jobs(db, limit=4) == []
    # A 一单完成，空出一个跨道槽：归在途为 0 的 B。
    _finish(db, a_run1)
    claimed = tv.claim_due_jobs(db, limit=1)
    assert claimed == [b_pend.id]
    # 再空出一槽：轮到 A 的第三单（公平不是永久压制 A）。
    _finish(db, a_run2)
    claimed = tv.claim_due_jobs(db, limit=1)
    assert claimed == [a_pend.id]


# ---------- 等待键：在途相同，最老候选等得最久的 owner 先 ----------

def test_oldest_waiting_owner_wins_over_key_order(db, monkeypatch):
    """两 owner 均 0 在途：ref=1002 的单等了 600 秒、ref=1001
    （字典序靠前）的单只等了 60 秒，limit=1 必须挑等得久的。"""
    monkeypatch.setattr(tv, "TRANSFER_WORKER_PER_USER_MAX", 2)
    _job(db, item_id=21, owner_ref="1001", created_ago_sec=60)
    old = _job(db, item_id=22, owner_ref="1002", created_ago_sec=600)
    assert tv.claim_due_jobs(db, limit=1) == [old.id]


# ---------- 守卫：同 owner 内 FIFO、达上限不挡人、点播优先 ----------

def test_same_owner_fifo_preserved(db, monkeypatch):
    """同一 owner 多单可领时，按建单先后（id 序）认领，不乱序。"""
    monkeypatch.setattr(tv, "TRANSFER_WORKER_PER_USER_MAX", 10)
    j1 = _job(db, item_id=31, owner_ref="u7", created_ago_sec=30)
    j2 = _job(db, item_id=32, owner_ref="u7", created_ago_sec=20)
    j3 = _job(db, item_id=33, owner_ref="u7", created_ago_sec=10)
    claimed = tv.claim_due_jobs(db, limit=2)
    assert claimed == [j1.id, j2.id]
    assert j3.status == "pending"


def test_capped_owner_pending_never_blocks_others(db, monkeypatch):
    """A 在途已达 per-owner 上限（2）＋一单 pending，B 一单
    pending：limit=1 必须挑 B（硬规矩的 cap 侧，基线即应成立）。"""
    monkeypatch.setattr(tv, "TRANSFER_WORKER_PER_USER_MAX", 2)
    _job(db, item_id=41, owner_ref="uA", status="running", lease_sec=300)
    _job(db, item_id=42, owner_ref="uA", status="running", lease_sec=300)
    _job(db, item_id=43, owner_ref="uA")
    b = _job(db, item_id=44, owner_ref="uB")
    assert tv.claim_due_jobs(db, limit=1) == [b.id]


def test_playback_still_beats_background_across_owners(db, monkeypatch):
    """trigger 组优先不变：先建的后台单排在后建的点播单之后。"""
    monkeypatch.setattr(tv, "TRANSFER_WORKER_PER_USER_MAX", 2)
    _job(db, item_id=51, trigger="prefetch", owner_ref="uA",
         created_ago_sec=600)
    fast = _job(db, item_id=52, trigger="playback", owner_ref="uB",
                created_ago_sec=10)
    assert tv.claim_due_jobs(db, limit=1) == [fast.id]


def test_running_count_beats_wait_within_same_lane(db, monkeypatch):
    """同组内在途键优先于等待键：A 在途 1 单、其 pending 等得
    更久；B 在途 0、pending 较新——limit=1 仍归 B。"""
    monkeypatch.setattr(tv, "TRANSFER_WORKER_PER_USER_MAX", 2)
    _job(db, item_id=61, owner_ref="uA", status="running", lease_sec=300)
    _job(db, item_id=62, owner_ref="uA", created_ago_sec=900)
    b = _job(db, item_id=63, owner_ref="uB", created_ago_sec=30)
    assert tv.claim_due_jobs(db, limit=1) == [b.id]


# ---------- 可观测：认领决策结构化日志 ----------

def test_claim_pick_structured_log(db, monkeypatch, caplog):
    """实际认领到单时打一条 claim_pick 日志，含 job/owner/
    在途数；无可认领时不打（日志量受控）。"""
    monkeypatch.setattr(tv, "TRANSFER_WORKER_PER_USER_MAX", 2)
    job = _job(db, item_id=71, owner_ref="u9", created_ago_sec=45)
    with caplog.at_level(logging.INFO,
                         logger="backend.emby_server.transfer_variant"):
        claimed = tv.claim_due_jobs(db, limit=1)
    assert claimed == [job.id]
    lines = [r.getMessage() for r in caplog.records
             if "claim_pick" in r.getMessage()]
    assert len(lines) == 1
    assert f"job={job.id}" in lines[0]
    assert "owner=user:u9" in lines[0]
    assert "trigger=playback" in lines[0]
    assert "owner_running=0" in lines[0]
    caplog.clear()
    with caplog.at_level(logging.INFO,
                         logger="backend.emby_server.transfer_variant"):
        assert tv.claim_due_jobs(db, limit=1) == []
    assert not [r for r in caplog.records if "claim_pick" in r.getMessage()]
