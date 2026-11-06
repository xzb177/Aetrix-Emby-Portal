# -*- coding: utf-8 -*-
"""交付计划落库竞态回归（2026-11-06 生产实证）。

事故：同一 (user, item) 两路并发 build_plan（PlaybackInfo
后台预计算 + 转存完成刷新），两边都先查后插，后到者的纯
INSERT 撞 uq_playback_plan_user_item 唯一约束，整份新计划被
丢弃（日志 warning「delivery_plan persist failed」）。当日
丢掉的恰是带 user_copy_ready 的新计划，存活行是另一路更早
算出的 user_copy_preparing 快照（job_status=pending），该行
过期后也无人替换。本文件钉：

1. commit 时撞唯一约束必须回滚重查走更新，不许丢计划，且
   最终落库的是较新算出的那份（computed_at 单调守卫：旧快照
   不许覆盖新快照）；
2. 计划生成时转存未完成、其后才完成：过期重建 / 转存完成
   刷新后顶腿变 user_copy_ready，热路径 plan_ready_leg 命中；
3. 现状口径钉子（另案，非本单改动）：share_revoked_at 已置
   位的变体仍算 ready——公开分享被收回有自愈重授口径
   （ensure_user_copy_public / reconcile_dead_drive_copy），
   ready 判定不因收回戳改判，防后人误改。
"""
import os

os.environ.setdefault("DATABASE_TYPE", "sqlite")
os.environ.setdefault("REDIS_ENABLED", "false")

import json
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from backend import models as base_models
from backend.emby_server import models as em
from backend.emby_server import delivery_plan as dp
from backend.integrations import store
from backend.emby_server import stream_limits as sl


@pytest.fixture()
def engine():
    eng = create_engine(
        "sqlite://", connect_args={"check_same_thread": False},
        poolclass=StaticPool)
    base_models.Base.metadata.create_all(eng)
    em.Base.metadata.create_all(eng)
    return eng


@pytest.fixture()
def db_factory(engine):
    return sessionmaker(bind=engine)


@pytest.fixture()
def db(db_factory):
    session = db_factory()
    sl.clear_table_policy_cache()
    store.invalidate()
    yield session
    session.close()
    sl.clear_table_policy_cache()
    store.invalidate()


_counter = [100]


def _item(db, **kw):
    _counter[0] += 1
    row = em.MediaItem(guid=f"upu-{_counter[0]}", library_id=1,
                       item_type="movie", name="测试片",
                       drive_file_id="origfid",
                       file_path="mount://3/电影/a.mkv", size=1000)
    for key, value in kw.items():
        setattr(row, key, value)
    db.add(row)
    db.commit()
    return row


def _user(uid=7):
    return SimpleNamespace(id=uid)


def _variant(db, item_id, user_id=7, status="ready", fid="copyfid",
             **kw):
    row = em.TransferVariant(item_id=item_id, source_file_id="origfid",
                             variant_file_id=fid,
                             variant_drive_id=f"userdrive:{user_id}",
                             owner_scope="user", owner_ref=str(user_id),
                             status=status, **kw)
    db.add(row)
    db.commit()
    return row


def _job(db, item_id, user_id=7, status="pending"):
    row = em.TransferJob(item_id=item_id, trigger="playback",
                         provider="drive", owner_scope="user",
                         owner_ref=str(user_id),
                         dest_drive_id=f"userdrive:{user_id}",
                         status=status)
    db.add(row)
    db.commit()
    return row


def _plan_row(db, user_id=7, item_id=None):
    return (db.query(em.PlaybackDeliveryPlan)
            .filter_by(user_id=user_id, item_id=item_id).one_or_none())


def _legs_of(row):
    return [leg["leg"] for leg in json.loads(row.plan_json)["legs"]]


# ---------- 1. 并发落库：撞约束不丢计划、新者胜 ----------

def _conflict_session(db_factory, engine, winner_kwargs):
    """第一次 commit 时模拟 PG 并发竞态：另一路已先落行（胜者
    行由独立会话写入），本会话的 INSERT 撞唯一约束抛
    IntegrityError——与生产日志里的形态一致。"""
    state = {"fired": False}

    class ConflictSession(Session):
        def commit(self):
            if not state["fired"]:
                state["fired"] = True
                super().rollback()
                winner = db_factory()
                winner.add(em.PlaybackDeliveryPlan(**winner_kwargs))
                winner.commit()
                winner.close()
                raise IntegrityError(
                    "INSERT INTO playback_delivery_plans", {},
                    Exception("duplicate key value violates unique "
                              "constraint uq_playback_plan_user_item"))
            return super().commit()

    return sessionmaker(bind=engine, class_=ConflictSession)()


def test_concurrent_persist_conflict_keeps_fresher_plan(
        db, db_factory, engine):
    item = _item(db)
    _variant(db, item.id)  # 本路现算：顶腿应是 user_copy_ready
    older = datetime.now() - timedelta(seconds=5)
    winner_plan = {
        "user_id": 7, "item_id": item.id,
        "legs": [{"leg": "user_copy_preparing", "state": "preparing",
                  "job_id": 999, "job_status": "pending"},
                 {"leg": "legacy", "state": "fallback"}],
        "computed_at": older.isoformat(timespec="seconds"),
        "expires_at": (older + timedelta(seconds=120)).isoformat(
            timespec="seconds")}
    loser_db = _conflict_session(db_factory, engine, {
        "user_id": 7, "item_id": item.id,
        "plan_json": json.dumps(winner_plan, ensure_ascii=False),
        "computed_at": older,
        "expires_at": older + timedelta(seconds=120)})
    try:
        plan = dp.build_plan(loser_db, 7, item)
    finally:
        loser_db.close()
    # 撞约束不许把整份新计划丢掉（旧代码这里回 None）
    assert plan is not None
    assert plan["legs"][0]["leg"] == "user_copy_ready"
    row = _plan_row(db, item_id=item.id)
    assert row is not None
    # 最终落库的是较新算出的 ready 计划，不是胜者的旧快照
    assert _legs_of(row)[0] == "user_copy_ready"
    assert db.query(em.PlaybackDeliveryPlan).count() == 1


def test_older_snapshot_must_not_clobber_newer_row(db):
    item = _item(db)
    _variant(db, item.id)
    plan = dp.build_plan(db, 7, item)
    assert plan["legs"][0]["leg"] == "user_copy_ready"
    row = _plan_row(db, item_id=item.id)
    # 模拟已存行是「未来才算出」的更新计划
    row.computed_at = datetime.now() + timedelta(hours=1)
    db.commit()
    stored_json = row.plan_json
    # 副本消失后重算：新算出的计划更旧，不许覆盖已存的新行
    variant = db.query(em.TransferVariant).filter_by(
        item_id=item.id).one()
    variant.status = "stale"
    db.commit()
    dp.build_plan(db, 7, item)
    row = _plan_row(db, item_id=item.id)
    assert row.plan_json == stored_json
    assert _legs_of(row)[0] == "user_copy_ready"


# ---------- 2. 转存后完成：重建/刷新后顶腿变 ready ----------

def test_expired_plan_rebuilt_after_job_done(db, monkeypatch):
    monkeypatch.setenv("TRANSFER_VARIANT_ENABLED", "1")
    item = _item(db)
    job = _job(db, item.id, status="pending")
    plan = dp.build_plan(db, 7, item)
    assert plan["legs"][0]["leg"] == "user_copy_preparing"
    assert plan["legs"][0]["job_status"] == "pending"
    # 转存随后完成、变体 ready（计划快照里的 pending 已过期）
    job.status = "done"
    db.commit()
    variant = _variant(db, item.id)
    row = _plan_row(db, item_id=item.id)
    old_computed = row.computed_at
    row.expires_at = datetime.now() - timedelta(seconds=1)
    db.commit()
    # 过期计划必须真重建并替换存量行，而不是继续用旧结论
    fresh = dp.ensure_plan(db, _user(7), item)
    assert fresh["legs"][0]["leg"] == "user_copy_ready"
    assert db.query(em.PlaybackDeliveryPlan).count() == 1
    row = _plan_row(db, item_id=item.id)
    assert row.computed_at > old_computed
    assert _legs_of(row)[0] == "user_copy_ready"
    got = dp.plan_ready_leg(db, item, _user(7))
    assert got is not None and got[1].id == variant.id


def test_refresh_after_job_done_makes_ready_leg(db, monkeypatch):
    monkeypatch.setenv("TRANSFER_VARIANT_ENABLED", "1")
    item = _item(db)
    job = _job(db, item.id, status="pending")
    dp.build_plan(db, 7, item)
    job.status = "done"
    db.commit()
    variant = _variant(db, item.id)
    # 转存完成事件刷新：不等过期，顶腿立即换成实时状态
    dp.refresh_plan_for_job(db, job)
    fresh = dp.get_fresh_plan(db, 7, item.id)
    assert fresh["legs"][0]["leg"] == "user_copy_ready"
    got = dp.plan_ready_leg(db, item, _user(7))
    assert got is not None and got[0] == "copyfid"
    assert got[1].id == variant.id


# ---------- 3. 现状口径钉子：share_revoked_at 仍算 ready ----------

def test_share_revoked_variant_still_ready_by_design(db, monkeypatch):
    monkeypatch.setenv("TRANSFER_VARIANT_ENABLED", "1")
    item = _item(db)
    variant = _variant(db, item.id,
                       share_revoked_at=datetime.now())
    plan = dp.build_plan(db, 7, item)
    assert plan["legs"][0]["leg"] == "user_copy_ready"
    got = dp.plan_ready_leg(db, item, _user(7))
    assert got is not None and got[1].id == variant.id
