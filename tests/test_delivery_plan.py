# -*- coding: utf-8 -*-
"""换代线 step 5：播放交付编排器 v1（PR-C）。

计划＝(用户, 条目) → 有序腿清单，平时（PlaybackInfo/转存完成）
预计算落库；点播热路径只读计划：新鲜且顶腿是本人 ready 副本
时才采用，且采用前按 PK 复核变体行（状态/归属/条目全对才算
数）；无计划/过期/开关关/复核不过一律回现行选路。
"""
import os

os.environ.setdefault("DATABASE_TYPE", "sqlite")
os.environ.setdefault("REDIS_ENABLED", "false")

from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend import models as base_models
from backend.emby_server import models as em
from backend.emby_server import delivery_plan as dp
from backend.emby_server import transfer_manifest as tman
from backend.integrations import store
from backend.emby_server import stream_limits as sl


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
    sl.clear_table_policy_cache()
    store.invalidate()
    yield session
    session.close()
    sl.clear_table_policy_cache()
    store.invalidate()


_counter = [0]


def _item(db, **kw):
    _counter[0] += 1
    row = em.MediaItem(guid=f"dp-{_counter[0]}", library_id=1,
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


def _variant(db, item_id, user_id=7, status="ready", fid="copyfid"):
    row = em.TransferVariant(item_id=item_id, source_file_id="origfid",
                             variant_file_id=fid,
                             variant_drive_id=f"userdrive:{user_id}",
                             owner_scope="user", owner_ref=str(user_id),
                             status=status)
    db.add(row)
    db.commit()
    return row


def _legs(plan):
    return [leg["leg"] for leg in plan["legs"]]


# ---------- 计划生成 ----------

def test_plan_ready_copy_first_leg(db):
    item = _item(db)
    variant = _variant(db, item.id)
    plan = dp.build_plan(db, 7, item)
    assert plan is not None
    assert _legs(plan)[0] == "user_copy_ready"
    assert plan["legs"][0]["variant_id"] == variant.id
    assert _legs(plan)[-1] == "legacy"  # 终局腿恒在


def test_plan_preparing_leg_with_progress(db):
    item = _item(db)
    job = em.TransferJob(item_id=item.id, trigger="playback",
                         provider="drive", owner_scope="user",
                         owner_ref="7", dest_drive_id="userdrive:7",
                         status="running")
    db.add(job)
    db.commit()
    tman.persist_delivery(
        db, job_id=job.id, content_key="ck", dest_provider="drive",
        dest_binding_id=None, dest_ref="folder", session={},
        size=1000, state="active")
    row = db.query(em.TransferDelivery).filter_by(job_id=job.id).one()
    row.committed_offset = 400
    db.commit()
    plan = dp.build_plan(db, 7, item)
    assert _legs(plan)[0] == "user_copy_preparing"
    leg = plan["legs"][0]
    assert leg["job_id"] == job.id
    assert leg["progress_pct"] == 40


def test_plan_mount_and_upstream_legs(db, monkeypatch):
    item = _item(db)
    monkeypatch.setattr(
        "backend.emby_server.mount_serving.resolve_local_file",
        lambda db_, it: "/mnt/x/a.mkv")
    monkeypatch.setattr(
        "backend.emby_server.upstream_item_mappings.get_best_mapping",
        lambda db_, iid, validate=True: SimpleNamespace(id=5))
    plan = dp.build_plan(db, 7, item)
    assert _legs(plan) == ["mount_direct", "upstream", "legacy"]


def test_plan_probe_failure_isolated(db, monkeypatch):
    item = _item(db)

    def _boom(db_, it):
        raise RuntimeError("挂载探针炸了")

    monkeypatch.setattr(
        "backend.emby_server.mount_serving.resolve_local_file", _boom)
    plan = dp.build_plan(db, 7, item)
    assert plan is not None and _legs(plan) == ["legacy"]


def test_plan_other_users_copy_not_a_leg(db):
    item = _item(db)
    _variant(db, item.id, user_id=8)  # 别人的副本绝不进我的计划
    plan = dp.build_plan(db, 7, item)
    assert "user_copy_ready" not in _legs(plan)


# ---------- 热路径只读计划 ----------

def test_plan_ready_leg_hit_and_verify(db, monkeypatch):
    # 与现行选路同闸：转存总闸（env）开时才采用计划
    monkeypatch.setenv("TRANSFER_VARIANT_ENABLED", "1")
    item = _item(db)
    variant = _variant(db, item.id)
    dp.build_plan(db, 7, item)
    got = dp.plan_ready_leg(db, item, _user(7))
    assert got is not None
    fid, row = got
    assert fid == "copyfid" and row.id == variant.id


def test_plan_ready_leg_stale_variant_rejected(db, monkeypatch):
    monkeypatch.setenv("TRANSFER_VARIANT_ENABLED", "1")
    item = _item(db)
    variant = _variant(db, item.id)
    dp.build_plan(db, 7, item)
    variant.status = "stale"  # 计划生成后副本被标死：复核必须拦下
    db.commit()
    assert dp.plan_ready_leg(db, item, _user(7)) is None


def test_plan_ready_leg_missing_or_expired(db):
    item = _item(db)
    _variant(db, item.id)
    assert dp.plan_ready_leg(db, item, _user(7)) is None  # 无计划
    dp.build_plan(db, 7, item)
    row = db.query(em.PlaybackDeliveryPlan).filter_by(
        user_id=7, item_id=item.id).one()
    row.expires_at = datetime.now() - timedelta(seconds=1)
    db.commit()
    assert dp.plan_ready_leg(db, item, _user(7)) is None  # 已过期


def test_plan_ready_leg_other_user(db):
    item = _item(db)
    _variant(db, item.id, user_id=7)
    dp.build_plan(db, 7, item)
    assert dp.plan_ready_leg(db, item, _user(8)) is None


def test_ensure_plan_caches_until_expiry(db):
    item = _item(db)
    _variant(db, item.id)
    first = dp.ensure_plan(db, _user(7), item)
    second = dp.ensure_plan(db, _user(7), item)
    assert first["computed_at"] == second["computed_at"]  # 未重算
    rows = db.query(em.PlaybackDeliveryPlan).all()
    assert len(rows) == 1  # 同 (用户, 条目) 一行原地更新


# ---------- 开关与配置 ----------

def test_switch_off_is_full_noop(db):
    dp.write_config(db, enabled=False, ttl_sec=120)
    item = _item(db)
    _variant(db, item.id)
    assert dp.build_plan(db, 7, item) is None
    assert dp.plan_ready_leg(db, item, _user(7)) is None
    assert db.query(em.PlaybackDeliveryPlan).count() == 0


def test_config_roundtrip_and_validation(db):
    payload = dp.write_config(db, enabled=True, ttl_sec=300)
    assert payload["enabled"] is True and payload["ttl_sec"] == 300
    assert dp.plan_ttl_sec(db) == 300
    with pytest.raises(ValueError):
        dp.write_config(db, enabled=True, ttl_sec=5)  # 低于下限 30


def test_refresh_plan_for_done_job(db):
    item = _item(db)
    _variant(db, item.id)
    job = em.TransferJob(item_id=item.id, trigger="playback",
                         provider="drive", owner_scope="user",
                         owner_ref="7", dest_drive_id="userdrive:7",
                         status="done")
    db.add(job)
    db.commit()
    dp.refresh_plan_for_job(db, job)
    plan = dp.get_fresh_plan(db, 7, item.id)
    assert plan is not None and _legs(plan)[0] == "user_copy_ready"
