# -*- coding: utf-8 -*-
"""换绑后转存节流放行＋豁免限频（2026-10-30 用户亲定）。

事故原型：user 590 先绑 Drive 转存《醉梦》（job 落 userdrive:），
随后解绑 Drive 改绑 115，再播同片时新单被旧 Drive 单计入 24h
节流挡死（门禁 throttled、取流口 409）。亲定口径：

- 节流只数「落点与当前绑定面一致」的历史单；落到已解绑/已替换
  绑定的旧单不计入，换绑后可立即为新盘重建；
- 这份放行是豁免：每用户每 7 天（SystemConfig 可配）最多成功
  使用 1 次（按用户计、不按片计），用尽回落普通节流；
- 豁免使用落库可查；绑定/解绑接口本身不动。

全部离线：SQLite + 行级构造，不碰真实 Drive/115。
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
from backend.emby_server import transfer_variant as tv
from backend.integrations import store


@pytest.fixture()
def db_factory():
    engine = create_engine("sqlite://",
                           connect_args={"check_same_thread": False},
                           poolclass=StaticPool)
    base_models.Base.metadata.create_all(engine)
    em.Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)


@pytest.fixture()
def db(db_factory):
    session = db_factory()
    store.invalidate()
    yield session
    session.close()
    store.invalidate()


@pytest.fixture(autouse=True)
def _enable_master(monkeypatch):
    monkeypatch.setenv("TRANSFER_VARIANT_ENABLED", "1")


def _set_config(db, key, value):
    row = (db.query(base_models.SystemConfig)
           .filter(base_models.SystemConfig.key == key).first())
    if row is None:
        db.add(base_models.SystemConfig(key=key, value=value))
    else:
        row.value = value
    db.commit()
    store.invalidate()


def _mount(db, mount_id=3, mount_type="drive", path="drive:drive1"):
    row = em.StorageMount(id=mount_id, name=f"m{mount_id}",
                          mount_type=mount_type, path=path)
    db.add(row)
    db.commit()
    return row


_counter = [0]


def _drive_item(db, **kw):
    _counter[0] += 1
    row = em.MediaItem(guid=f"rw-{_counter[0]}", library_id=1,
                       item_type="movie", name="测试片",
                       drive_file_id="origfid",
                       file_path="mount://3/电影/a.mkv", size=1000)
    for key, value in kw.items():
        setattr(row, key, value)
    db.add(row)
    db.commit()
    return row


def _binding(db, user_id, provider="drive", status="active"):
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


def _user_job(db, item_id, user_id, dest, provider="drive",
              status="done", created_at=None):
    row = em.TransferJob(item_id=item_id, trigger="playback",
                         drive_id="drive1", provider=provider,
                         owner_scope="user", owner_ref=str(user_id),
                         dest_drive_id=dest, status=status)
    if created_at is not None:
        row.created_at = created_at
    db.add(row)
    db.commit()
    return row


def _user(user_id=7):
    return SimpleNamespace(id=user_id)


def _waiver_rows(db, user_id=7):
    return (db.query(em.TransferThrottleWaiver)
            .filter(em.TransferThrottleWaiver.user_id == user_id)
            .order_by(em.TransferThrottleWaiver.id)
            .all())


# ---------- ① 换绑放行：Drive 单 → 解绑 → 绑 115 → 同片可建单 ----------

def test_gate_rebind_allows_cross_job_and_records_waiver(db):
    _mount(db)
    item = _drive_item(db)
    # 旧 Drive 单（24h 内、已 done），用户现只绑 115（Drive 已解绑，
    # 绑定行不在了）——事故现场的等价构造。
    _user_job(db, item.id, 7, dest="userdrive:7", status="done")
    _binding(db, 7, provider="pan115")

    gate = tv.ensure_user_copy_gate(db, item, _user())
    assert gate["verdict"] == "preparing"
    assert gate["reason"] == "job_created"
    new_job = (db.query(em.TransferJob)
               .filter(em.TransferJob.id == gate["job_id"]).one())
    assert new_job.dest_drive_id == "pan115:user-7"
    rows = _waiver_rows(db)
    assert len(rows) == 1
    assert rows[0].item_id == item.id
    assert rows[0].to_scheme == "pan115:"


def test_create_cross_job_waiver_direct(db):
    _mount(db)
    item = _drive_item(db)
    _user_job(db, item.id, 7, dest="userdrive:7", status="failed")
    job = tv.create_cross_provider_job(db, item, 7, "drive",
                                       trigger="playback")
    assert job.dest_drive_id == "pan115:user-7"
    assert len(_waiver_rows(db)) == 1


# ---------- ② 同窗口第二片：豁免已用完 → 回落 throttled ----------

def test_second_item_in_window_falls_back_to_throttled(db):
    _mount(db)
    item_a = _drive_item(db)
    item_b = _drive_item(db)
    _user_job(db, item_a.id, 7, dest="userdrive:7", status="done")
    _user_job(db, item_b.id, 7, dest="userdrive:7", status="done")
    _binding(db, 7, provider="pan115")

    gate_a = tv.ensure_user_copy_gate(db, item_a, _user())
    assert gate_a["reason"] == "job_created"
    assert len(_waiver_rows(db)) == 1

    gate_b = tv.ensure_user_copy_gate(db, item_b, _user())
    assert gate_b["verdict"] == "preparing"
    assert gate_b["reason"] == "throttled"
    # 豁免用尽要在用户侧文案里区分出来（沿用 detail 通道）。
    assert "换绑放行" in (gate_b.get("detail") or "")
    assert len(_waiver_rows(db)) == 1  # 没再记行、没建新单
    with pytest.raises(tv.TransferError) as excinfo:
        tv.create_cross_provider_job(db, item_b, 7, "drive",
                                     trigger="playback")
    assert excinfo.value.kind == "throttled"


# ---------- ③ 窗口过后豁免恢复 ----------

def test_waiver_recovers_after_window(db):
    _mount(db)
    item_a = _drive_item(db)
    item_b = _drive_item(db)
    _user_job(db, item_a.id, 7, dest="userdrive:7", status="done")
    _user_job(db, item_b.id, 7, dest="userdrive:7", status="done")
    tv.create_cross_provider_job(db, item_a, 7, "drive",
                                 trigger="playback")
    assert len(_waiver_rows(db)) == 1
    # 把既有豁免记录推到默认 7 天窗之外。
    row = _waiver_rows(db)[0]
    row.created_at = datetime.now() - timedelta(days=8)
    db.commit()

    job_b = tv.create_cross_provider_job(db, item_b, 7, "drive",
                                         trigger="playback")
    assert job_b.dest_drive_id == "pan115:user-7"
    assert len(_waiver_rows(db)) == 2


# ---------- ④ 没换绑：同方案历史单照常节流，豁免零消耗 ----------

def test_same_scheme_recent_job_still_throttles(db):
    _mount(db)
    item = _drive_item(db)
    _user_job(db, item.id, 7, dest="pan115:user-7", status="done")
    with pytest.raises(tv.TransferError) as excinfo:
        tv.create_cross_provider_job(db, item, 7, "drive",
                                     trigger="playback")
    assert excinfo.value.kind == "throttled"
    assert _waiver_rows(db) == []

    item2 = _drive_item(db)
    _user_job(db, item2.id, 7, dest="userdrive:7", status="done")
    with pytest.raises(tv.TransferError) as excinfo2:
        tv.create_user_job(db, item2, 7, "drive", trigger="playback")
    assert excinfo2.value.kind == "throttled"
    assert _waiver_rows(db) == []


def test_mixed_recent_jobs_throttle_without_waiver(db):
    # 同片 24h 内既有旧方案单、也有当前方案单：当前方案单本身就
    # 构成节流，不算换绑场景，豁免不消耗。
    item = _drive_item(db)
    _user_job(db, item.id, 7, dest="userdrive:7", status="done")
    _user_job(db, item.id, 7, dest="pan115:user-7", status="failed")
    with pytest.raises(tv.TransferError) as excinfo:
        tv.create_cross_provider_job(db, item, 7, "drive",
                                     trigger="playback")
    assert excinfo.value.kind == "throttled"
    assert _waiver_rows(db) == []


def test_plain_create_without_recent_job_records_no_waiver(db):
    _mount(db)
    item = _drive_item(db)
    job = tv.create_user_job(db, item, 7, "drive", trigger="playback")
    assert job.dest_drive_id == "userdrive:7"
    assert _waiver_rows(db) == []


# ---------- 豁免只在真正建单成功时消耗 ----------

def test_waiver_not_consumed_when_create_fails_later(db):
    # 115 源条目无 fid：节流判定可放行，但建单在 fid 校验处失败
    # → 不许消耗豁免、不许落豁免行。
    item = _drive_item(db, file_path="115:/片/a.mkv", drive_file_id="")
    _user_job(db, item.id, 7, dest="pan115:user-7", provider="pan115",
              status="done")
    with pytest.raises(tv.TransferError) as excinfo:
        tv.create_cross_provider_job(db, item, 7, "pan115",
                                     trigger="playback")
    assert excinfo.value.kind == "no_source_fid"
    assert _waiver_rows(db) == []


# ---------- 配置口径：默认 7 天 / 1 次，可被 SystemConfig 覆盖 ----------

def test_waiver_config_defaults_and_override(db):
    assert tv.rebind_waiver_window_days(db) == 7
    assert tv.rebind_waiver_max_uses(db) == 1
    payload = tv.transfer_config_payload(db)
    assert payload["transfer_rebind_waiver_window_days"] == 7
    assert payload["transfer_rebind_waiver_max_uses"] == 1

    _set_config(db, "transfer_rebind_waiver_max_uses", "2")
    _mount(db)
    item_a = _drive_item(db)
    item_b = _drive_item(db)
    item_c = _drive_item(db)
    for it in (item_a, item_b, item_c):
        _user_job(db, it.id, 7, dest="userdrive:7", status="done")
    tv.create_cross_provider_job(db, item_a, 7, "drive",
                                 trigger="playback")
    tv.create_cross_provider_job(db, item_b, 7, "drive",
                                 trigger="playback")
    assert len(_waiver_rows(db)) == 2
    with pytest.raises(tv.TransferError) as excinfo:
        tv.create_cross_provider_job(db, item_c, 7, "drive",
                                     trigger="playback")
    assert excinfo.value.kind == "throttled"


def test_write_transfer_config_roundtrip(db):
    result = tv.write_transfer_config(
        db, transfer_rebind_waiver_window_days=14,
        transfer_rebind_waiver_max_uses=3)
    assert result["transfer_rebind_waiver_window_days"] == 14
    assert result["transfer_rebind_waiver_max_uses"] == 3
    assert tv.rebind_waiver_window_days(db) == 14
    assert tv.rebind_waiver_max_uses(db) == 3
