# -*- coding: utf-8 -*-
"""夸克 / 115 用户绑定线下线回归（2026-11-02 删除单）。

站主亲定删除两家用户绑盘线后，以下口径必须恒成立：
- 建绑 / 重验 quark、pan115 一律拒绝（unsupported + 下线文案），不落行；
- providers 名单永不返回 quark / pan115（挂载映射与 env 额外名单两侧）；
- 存量绑定行视为不存在（get_active_binding / 播放门禁只认 drive）；
- 建单目标为 quark / pan115 时以 provider_removed 拒绝；源为 115、
  落点 Drive 的跨单不受影响；
- 选路 / 交付计划不再采用 quark / pan115 存量副本行；
- 巡检整行跳过两家副本（不验活、不记 strike、不复活）；
- 认领前复核与执行入口对两家落点单终态化 provider_removed；
- 重绑复活不许复活两家落点单；
- TG 机器人绑定展示只认 Drive，存量行只留一句下线提醒。

全部离线：外部 API 一律桩替身，不发真实请求。
"""
import os

os.environ.setdefault("DATABASE_TYPE", "sqlite")
os.environ.setdefault("REDIS_ENABLED", "false")

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend import models as base_models
from backend.emby_server import models as em
from backend.emby_server import delivery_plan as dp
from backend.emby_server import playback_gate as gate
from backend.emby_server import transfer_variant as tv
from backend.emby_server import user_bindings as ub
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
    # 播种 Drive 绑定开关=开（2026-11-04 新增、默认关）：本套件测
    # 已下线 provider 的收敛口径，期望名单含 drive；开关两态由
    # test_drive_binding_switch.py 专门覆盖。
    session.add(base_models.SystemConfig(
        key="drive_binding_enabled", value="true"))
    session.commit()
    store.invalidate()
    yield session
    session.close()
    store.invalidate()


_counter = [0]


def _item(db, path="mount://9/电影/a.mkv", fid="srcfid"):
    _counter[0] += 1
    row = em.MediaItem(guid=f"removed-{_counter[0]}", library_id=1,
                       item_type="movie", name="测试片",
                       drive_file_id=fid, file_path=path, size=1000)
    db.add(row)
    db.commit()
    return row


def _binding(db, user_id, provider, status="active"):
    row = em.UserStorageBinding(user_id=user_id, provider=provider,
                                account_label=f"{provider}-label",
                                credential_id=999000 + user_id,
                                status=status)
    db.add(row)
    db.commit()
    return row


def _variant(db, item_id, user_id, drive_id, status="ready"):
    row = em.TransferVariant(
        item_id=item_id, source_file_id="src-1",
        variant_file_id=f"fid-{drive_id}",
        variant_drive_id=drive_id, owner_scope="user",
        owner_ref=str(user_id), status=status, stale_count=0)
    db.add(row)
    db.commit()
    return row


def _job(db, item_id, user_id, dest, status="pending", kind=None,
         provider="drive"):
    row = em.TransferJob(item_id=item_id, trigger="playback",
                         provider=provider, owner_scope="user",
                         owner_ref=str(user_id), dest_drive_id=dest,
                         status=status, error_kind=kind)
    db.add(row)
    db.commit()
    return row


# ---------------------------------------------------------------------------
# 入口关死：建绑 / 重验 / 名单 / 绑定读取
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("provider", ["pan115", "quark"])
def test_create_binding_removed_providers_rejected(db, provider):
    with pytest.raises(ub.BindingError) as excinfo:
        ub.create_binding(db, 7, provider, "我的盘", "UID=x; CID=y; SEID=z")
    assert excinfo.value.kind == "unsupported"
    assert "已下线" in excinfo.value.message
    assert "Google Drive" in excinfo.value.message
    # 不落任何绑定行 / 凭据行
    assert db.query(em.UserStorageBinding).count() == 0
    assert db.query(em.MountCredential).count() == 0


@pytest.mark.parametrize("provider", ["pan115", "quark"])
def test_verify_binding_removed_provider_rejected(db, provider):
    row = _binding(db, 7, provider, status="expired")
    with pytest.raises(ub.BindingError) as excinfo:
        ub.verify_binding(db, 7, row.id)
    assert excinfo.value.kind == "unsupported"
    assert "已下线" in excinfo.value.message


def test_providers_list_excludes_removed(db, monkeypatch):
    for i, mt in enumerate(("drive", "115", "quark")):
        db.add(em.StorageMount(name=f"m{i}", mount_type=mt, path="",
                               is_enabled=True))
    db.commit()
    assert ub.list_binding_providers(db) == ["drive"]
    # env 额外名单也开放不了已下线 provider
    monkeypatch.setenv("BYOS_EXTRA_BINDING_PROVIDERS", "pan115,quark")
    assert ub.list_binding_providers(db) == ["drive"]


@pytest.mark.parametrize("provider", ["pan115", "quark"])
def test_get_active_binding_removed_returns_none(db, provider):
    _binding(db, 7, provider, status="active")
    assert ub.get_active_binding(db, 7, provider) is None
    assert tv._active_binding(db, 7, provider) is None


def test_playback_gate_counts_only_drive(db):
    _binding(db, 7, "quark")
    _binding(db, 8, "pan115")
    _binding(db, 9, "drive")
    assert gate.has_active_binding(db, 7) is False
    assert gate.has_active_binding(db, 8) is False
    assert gate.has_active_binding(db, 9) is True


# ---------------------------------------------------------------------------
# 转存关死：建单拒绝 / 跨单落点 / 门禁口径
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("provider", ["pan115", "quark"])
def test_create_user_job_removed_provider_rejected(db, provider):
    item = _item(db)
    with pytest.raises(tv.TransferError) as excinfo:
        tv.create_user_job(db, item, 7, provider)
    assert excinfo.value.kind == "provider_removed"
    assert db.query(em.TransferJob).count() == 0


@pytest.mark.parametrize("dest_provider", ["pan115", "quark"])
def test_create_cross_job_removed_dest_rejected(db, monkeypatch,
                                                dest_provider):
    monkeypatch.setenv("TRANSFER_VARIANT_ENABLED", "1")
    item = _item(db)
    _binding(db, 7, dest_provider)
    with pytest.raises(tv.TransferError) as excinfo:
        tv.create_cross_provider_job(db, item, 7, "drive",
                                     dest_provider=dest_provider)
    assert excinfo.value.kind == "provider_removed"
    assert db.query(em.TransferJob).count() == 0


def test_create_cross_job_115_source_to_drive_still_works(db, monkeypatch):
    # 源为共享 115、落点用户 Drive：搬的是服主库来源，不在删除范围。
    monkeypatch.setenv("TRANSFER_VARIANT_ENABLED", "1")
    item = _item(db, path="115:/a.mkv", fid="fid115")
    _binding(db, 7, "drive")
    job = tv.create_cross_provider_job(db, item, 7, "pan115",
                                       dest_provider="drive")
    assert job.dest_drive_id == "userdrive:7"
    assert job.provider == "pan115"
    assert job.status == "pending"


def test_gate_115_item_with_drive_binding_goes_cross(db, monkeypatch):
    monkeypatch.setenv("TRANSFER_VARIANT_ENABLED", "1")
    item = _item(db, path="115:/a.mkv", fid="fid115")
    _binding(db, 7, "drive")
    user = type("U", (), {"id": 7})()
    out = tv.ensure_user_copy_gate(db, item, user)
    assert out["verdict"] == "preparing"
    job = db.query(em.TransferJob).one()
    assert job.dest_drive_id == "userdrive:7"


def test_gate_drive_item_with_only_removed_binding_proceeds(db, monkeypatch):
    # 只剩存量夸克绑定 = 无匹配绑定：本门禁不越权拦（#1341 门禁
    # 负责拦新播放），且绝不给夸克落点建单。
    monkeypatch.setenv("TRANSFER_VARIANT_ENABLED", "1")
    db.add(em.StorageMount(id=9, name="gd", mount_type="drive", path="",
                           is_enabled=True))
    db.commit()
    item = _item(db)
    _binding(db, 7, "quark")
    user = type("U", (), {"id": 7})()
    out = tv.ensure_user_copy_gate(db, item, user)
    assert out["verdict"] == "proceed"
    assert out["reason"] == "no_matching_binding"
    assert db.query(em.TransferJob).count() == 0


# ---------------------------------------------------------------------------
# 播放关死：存量副本不被选路 / 交付计划采用
# ---------------------------------------------------------------------------

def test_ready_variant_selection_skips_removed_rows(db):
    item = _item(db)
    drive_row = _variant(db, item.id, 7, "userdrive:7")
    _variant(db, item.id, 7, "quark:user-7")  # id 更大、更新
    got = tv.get_ready_variant_for_user(db, item.id, 7)
    assert got is not None and got.id == drive_row.id


def test_ready_variant_only_removed_rows_returns_none(db):
    item = _item(db)
    _variant(db, item.id, 7, "pan115:user-7")
    assert tv.get_ready_variant_for_user(db, item.id, 7) is None


def test_delivery_plan_probe_skips_removed_rows(db):
    item = _item(db)
    _variant(db, item.id, 7, "quark:user-7")
    user = type("U", (), {"id": 7})()
    assert dp._probe_user_copy_ready(db, 7, item) is None
    drive_row = _variant(db, item.id, 7, "userdrive:7")
    leg = dp._probe_user_copy_ready(db, 7, item)
    assert leg is not None and leg["variant_id"] == drive_row.id


# ---------------------------------------------------------------------------
# 巡检 / 认领 / 执行 / 复活：两家落点全部停动
# ---------------------------------------------------------------------------

class _DriveApiStub:
    def __init__(self):
        self.calls = []

    def get_file(self, drive_id, file_id):
        self.calls.append((drive_id, file_id))
        return {"id": file_id}


def test_patrol_skips_removed_rows(db):
    item = _item(db)
    q = _variant(db, item.id, 7, "quark:user-7")
    p = _variant(db, item.id, 7, "pan115:user-7")
    d = _variant(db, item.id, 7, "shareddrive-1")
    stub = _DriveApiStub()
    stats = tv.patrol_once(db, lambda: stub)
    assert stats["checked"] == 1
    assert stub.calls == [("shareddrive-1", "fid-shareddrive-1")]
    for row in (q, p):
        db.refresh(row)
        assert row.status == "ready"
        assert row.stale_count == 0
        assert row.last_checked_at is None
    db.refresh(d)
    assert d.last_checked_at is not None


def test_claim_fails_removed_dest_jobs(db):
    item = _item(db)
    job = _job(db, item.id, 7, "quark:user-7")
    claimed = tv.claim_due_jobs(db)
    assert claimed == []
    db.refresh(job)
    assert job.status == "failed"
    assert job.error_kind == "provider_removed"


def test_run_once_fails_removed_dest_job(db):
    item = _item(db)
    job = _job(db, item.id, 7, "pan115:user-7", status="running",
               provider="pan115")
    out = tv.run_transfer_job_once(db, job, _DriveApiStub())
    db.refresh(job)
    assert job.status == "failed"
    assert job.error_kind == "provider_removed"
    assert out["error_kind"] == "provider_removed"


def test_revive_skips_removed_dest_jobs(db):
    item = _item(db)
    dead_q = _job(db, item.id, 7, "quark:user-7", status="failed",
                  kind="no_binding")
    dead_d = _job(db, item.id, 7, "userdrive:7", status="failed",
                  kind="no_binding")
    n = tv.revive_binding_failed_jobs(db, 7)
    assert n == 1
    db.refresh(dead_q)
    db.refresh(dead_d)
    assert dead_q.status == "failed"
    assert dead_d.status == "pending"


# ---------------------------------------------------------------------------
# 存量收敛：provider_removed 落盘档位与序列化文案（2026-11-03 收敛单）
# ---------------------------------------------------------------------------

def test_provider_removed_error_message(db):
    # 收敛档位的文案必须与下线定版同一句，不另编新文案。
    assert ub.error_message("provider_removed") == ub.REMOVED_PROVIDER_MESSAGE
    assert ub.error_message("provider_removed", "pan115") == \
        ub.REMOVED_PROVIDER_MESSAGE
    assert ub.error_message("provider_removed", "quark") == \
        ub.REMOVED_PROVIDER_MESSAGE
    # 落盘状态档位：provider_removed → invalid（终态）。
    assert ub.ERROR_KIND_TO_STATUS["provider_removed"] == "invalid"


def test_serialize_converged_removed_binding(db):
    # 收敛后的存量行（invalid + provider_removed）序列化出来不许再
    # 呈现为可用：状态 invalid、error_message 是已下线文案。
    row = _binding(db, 7, "pan115", status="invalid")
    row.last_error_kind = "provider_removed"
    db.commit()
    payload = ub.serialize_binding(row)
    assert payload["status"] == "invalid"
    assert payload["error_message"] == ub.REMOVED_PROVIDER_MESSAGE


# ---------------------------------------------------------------------------
# TG 机器人：绑定展示只认 Drive
# ---------------------------------------------------------------------------

def test_tg_bindings_card_hides_removed_rows(db):
    from types import SimpleNamespace

    from backend.tg_bot import queries

    _binding(db, 7, "quark")
    text = queries.render_bindings(db, SimpleNamespace(id=7))
    assert "还没有绑定网盘" in text
    assert "已下线" in text and "改绑 Google Drive" in text


def test_tg_bindings_card_lists_drive(db):
    from types import SimpleNamespace

    from backend.tg_bot import queries

    _binding(db, 7, "drive")
    _binding(db, 7, "pan115", status="invalid")
    text = queries.render_bindings(db, SimpleNamespace(id=7))
    assert "谷歌网盘" in text
    assert "115 网盘" not in text


def test_tg_account_bindings_line_drive_only(db):
    from backend.tg_bot import features

    _binding(db, 7, "quark")
    _binding(db, 8, "pan115")
    _binding(db, 9, "drive")
    assert "未绑定" in features._bindings_line(db, 7)
    assert "未绑定" in features._bindings_line(db, 8)
    line9 = features._bindings_line(db, 9)
    assert "Drive 已绑" in line9 and "115" not in line9
