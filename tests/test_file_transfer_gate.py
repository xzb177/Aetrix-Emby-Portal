# -*- coding: utf-8 -*-
"""两路 /Items/{id}/File 的首播转存门禁（360° 审计 R2/R26 结转项）。

背景：门禁五路里 PlaybackInfo / video_stream / upstream_stream 都有
绑定门禁＋转存门禁，两路 File（stream_routes.item_file 与挂载替换版
mount_routes.mounted_item_file）此前只有绑定门禁——已绑定但尚无
自己盘副本的用户可经 File 直接拿到共享原件字节（Drive 条目 302
到 usercontent 直链），绕过「首播只走用户盘、无副本先转存」。

本文件锁定：
- 共享映射 playback_gate.raise_for_transfer_gate：preparing/failed
  → 409（detail 透传门禁说明、缺省文案与 video_stream 一致）、
  error → 503 fail-closed、proceed / serve_115_copy 不拦；
- 两路 File 在罐装 verdict 下同口径拦截/放行；
- 真门禁集成（mounted）：无 ready 副本 → 409 且建转存单、拿不到
  302；有 ready 副本 → 照常 302；有效 play_sign 消费 → 不追溯；
- 本机条目（stream_routes）：provider 不在门禁范围 → 照常出字节。
"""
import os

os.environ.setdefault("DATABASE_TYPE", "sqlite")
os.environ.setdefault("REDIS_ENABLED", "false")

import types

import pytest
from fastapi import HTTPException
from fastapi.responses import FileResponse
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend import models as base_models
from backend.emby_server import models as em
from backend.emby_server import mount_routes
from backend.emby_server import playback_gate as gate
from backend.emby_server import stream_routes
from backend.emby_server import transfer_variant as tv
from backend.integrations import store


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


@pytest.fixture(autouse=True)
def _clean_store_cache():
    store.invalidate()
    yield
    store.invalidate()


def _user(db, name="u1"):
    user = base_models.WebUser(username=name, password_hash="x")
    db.add(user)
    db.commit()
    return user


def _binding(db, user_id, status="active", provider="drive"):
    cred = em.MountCredential(
        mount_id=None, scope="user", owner_user_id=user_id,
        sealed_credential="", key_id="fernet-v1", status="active")
    db.add(cred)
    db.flush()
    binding = em.UserStorageBinding(
        user_id=user_id, provider=provider, account_label="盘",
        credential_id=cred.id, status=status)
    db.add(binding)
    db.commit()
    return binding


def _plain_request(item_id="guid-1"):
    return types.SimpleNamespace(
        query_params={}, path_params={"item_id": item_id})


def _patch_gate(monkeypatch, verdict_dict):
    monkeypatch.setattr(
        tv, "ensure_user_copy_gate",
        lambda db, item, user, play_sign_consume=False: verdict_dict)


# ---------- 共享映射 raise_for_transfer_gate ----------

def test_helper_preparing_default_detail():
    with pytest.raises(HTTPException) as excinfo:
        gate.raise_for_transfer_gate({"verdict": "preparing"})
    assert excinfo.value.status_code == 409
    assert excinfo.value.detail == (
        "转存准备中：正在把文件转存到你的绑定盘，转存完成后可播")


def test_helper_preparing_passes_gate_detail():
    with pytest.raises(HTTPException) as excinfo:
        gate.raise_for_transfer_gate(
            {"verdict": "preparing", "detail": "跨盘准备中 X"})
    assert excinfo.value.status_code == 409
    assert excinfo.value.detail == "跨盘准备中 X"


def test_helper_failed_default_and_detail():
    with pytest.raises(HTTPException) as excinfo:
        gate.raise_for_transfer_gate({"verdict": "failed"})
    assert excinfo.value.status_code == 409
    assert excinfo.value.detail == "转存失败，不走共享播放：请稍后重试或联系管理员"
    with pytest.raises(HTTPException) as excinfo:
        gate.raise_for_transfer_gate(
            {"verdict": "failed", "detail": "失败说明 Y"})
    assert excinfo.value.detail == "失败说明 Y"


def test_helper_error_is_503():
    with pytest.raises(HTTPException) as excinfo:
        gate.raise_for_transfer_gate({"verdict": "error"})
    assert excinfo.value.status_code == 503
    assert excinfo.value.detail == "转存门禁暂不可用，稍后重试（未走共享播放）"


def test_helper_proceed_and_serve_115_copy_pass():
    gate.raise_for_transfer_gate({"verdict": "proceed"})
    gate.raise_for_transfer_gate({"verdict": "serve_115_copy"})
    gate.raise_for_transfer_gate(None)
    gate.raise_for_transfer_gate({})


# ---------- mounted_item_file：罐装 verdict ----------

@pytest.fixture()
def _mounted(monkeypatch):
    item = types.SimpleNamespace(
        id=11, drive_file_id="fid-123", file_path="mount://3/电影/a.mkv")
    monkeypatch.setattr(
        mount_routes, "_require_visible_item",
        lambda db, user, item_id: item)
    monkeypatch.setattr(
        mount_routes, "ensure_playback_allowed", lambda db, user: None)
    monkeypatch.setattr(
        mount_routes, "_play_target",
        lambda db, item: types.SimpleNamespace(kind="url", value=""))
    from backend.emby_server import drive_auth
    monkeypatch.setattr(
        drive_auth, "drive_usercontent_download_url",
        lambda fid: "https://example.invalid/dl/" + fid)
    return item


def test_mounted_file_preparing_blocked(db, _mounted, monkeypatch):
    user = _user(db)
    _binding(db, user.id)
    _patch_gate(monkeypatch, {"verdict": "preparing"})
    with pytest.raises(HTTPException) as excinfo:
        mount_routes.mounted_item_file("guid-1", _plain_request(), user, db)
    assert excinfo.value.status_code == 409
    assert "转存准备中" in excinfo.value.detail


def test_mounted_file_failed_blocked(db, _mounted, monkeypatch):
    user = _user(db)
    _binding(db, user.id)
    _patch_gate(monkeypatch, {"verdict": "failed", "detail": "转存失败说明"})
    with pytest.raises(HTTPException) as excinfo:
        mount_routes.mounted_item_file("guid-1", _plain_request(), user, db)
    assert excinfo.value.status_code == 409
    assert excinfo.value.detail == "转存失败说明"


def test_mounted_file_error_is_503(db, _mounted, monkeypatch):
    user = _user(db)
    _binding(db, user.id)
    _patch_gate(monkeypatch, {"verdict": "error"})
    with pytest.raises(HTTPException) as excinfo:
        mount_routes.mounted_item_file("guid-1", _plain_request(), user, db)
    assert excinfo.value.status_code == 503


def test_mounted_file_proceed_still_302(db, _mounted, monkeypatch):
    user = _user(db)
    _binding(db, user.id)
    _patch_gate(monkeypatch, {"verdict": "proceed"})
    resp = mount_routes.mounted_item_file("guid-1", _plain_request(), user, db)
    assert resp.status_code == 302


# ---------- mounted_item_file：真门禁集成 ----------

@pytest.fixture()
def _mounted_real(db, monkeypatch):
    monkeypatch.setenv("TRANSFER_VARIANT_ENABLED", "1")
    mount = em.StorageMount(
        id=3, name="m3", mount_type="drive", path="drive:drive1")
    db.add(mount)
    db.commit()
    item = em.MediaItem(
        guid="guid-1", library_id=1, item_type="movie", name="测试片",
        drive_file_id="origfid", file_path="mount://3/电影/a.mkv",
        size=1000)
    db.add(item)
    db.commit()
    monkeypatch.setattr(
        mount_routes, "_require_visible_item",
        lambda db_, user, item_id: item)
    monkeypatch.setattr(
        mount_routes, "ensure_playback_allowed", lambda db_, user: None)
    monkeypatch.setattr(
        mount_routes, "_play_target",
        lambda db_, it: types.SimpleNamespace(kind="url", value=""))
    from backend.emby_server import drive_auth
    monkeypatch.setattr(
        drive_auth, "drive_usercontent_download_url",
        lambda fid: "https://example.invalid/dl/" + fid)
    return item


def test_mounted_file_no_copy_blocked_and_job_created(db, _mounted_real):
    user = _user(db)
    _binding(db, user.id)
    with pytest.raises(HTTPException) as excinfo:
        mount_routes.mounted_item_file("guid-1", _plain_request(), user, db)
    assert excinfo.value.status_code == 409
    jobs = (db.query(em.TransferJob)
            .filter(em.TransferJob.item_id == _mounted_real.id,
                    em.TransferJob.owner_ref == str(user.id))
            .all())
    assert len(jobs) == 1


def test_mounted_file_ready_copy_passes(db, _mounted_real):
    user = _user(db)
    _binding(db, user.id)
    db.add(em.TransferVariant(
        item_id=_mounted_real.id, source_file_id="origfid",
        variant_file_id="copyfid",
        variant_drive_id=f"userdrive:{user.id}",
        owner_scope="user", owner_ref=str(user.id),
        status="ready", size_bytes=1000))
    db.commit()
    resp = mount_routes.mounted_item_file("guid-1", _plain_request(), user, db)
    assert resp.status_code == 302
    assert resp.headers["location"].startswith("https://example.invalid/dl/")


def test_mounted_file_signed_consume_not_blocked(db, _mounted_real,
                                                 monkeypatch):
    user = _user(db)
    _binding(db, user.id)
    from backend.emby_server import play_sign
    monkeypatch.setattr(play_sign, "hotlink_enabled", lambda db_: True)
    monkeypatch.setattr(
        play_sign, "verify_play_sign",
        lambda uid, item_id, exp, sign: True)
    req = types.SimpleNamespace(
        query_params={"sign": "s", "exp": "9999999999", "uid": str(user.id)},
        path_params={"item_id": "guid-1"})
    resp = mount_routes.mounted_item_file("guid-1", req, user, db)
    assert resp.status_code == 302


# ---------- stream_routes.item_file ----------

@pytest.fixture()
def _local_file(tmp_path, monkeypatch):
    path = tmp_path / "movie.mkv"
    path.write_bytes(b"video-bytes")
    item = types.SimpleNamespace(
        id=21, drive_file_id="origfid", file_path=str(path))
    monkeypatch.setattr(
        stream_routes, "_require_visible_item",
        lambda db, user, item_id: item)
    monkeypatch.setattr(
        stream_routes, "ensure_playback_allowed", lambda db, user: None)
    return item


def test_stream_file_preparing_blocked(db, _local_file, monkeypatch):
    user = _user(db)
    _binding(db, user.id)
    _patch_gate(monkeypatch, {"verdict": "preparing"})
    with pytest.raises(HTTPException) as excinfo:
        stream_routes.item_file("guid-1", _plain_request(), user, db)
    assert excinfo.value.status_code == 409


def test_stream_file_proceed_serves_bytes(db, _local_file, monkeypatch):
    user = _user(db)
    _binding(db, user.id)
    _patch_gate(monkeypatch, {"verdict": "proceed"})
    resp = stream_routes.item_file("guid-1", _plain_request(), user, db)
    assert isinstance(resp, FileResponse)
    assert resp.status_code == 200


def test_stream_file_local_item_real_gate_untouched(db, _local_file,
                                                    monkeypatch):
    """本机条目 provider 不在门禁范围：真门禁开启也照常出字节。"""
    monkeypatch.setenv("TRANSFER_VARIANT_ENABLED", "1")
    _local_file.drive_file_id = ""
    user = _user(db)
    _binding(db, user.id)
    resp = stream_routes.item_file("guid-1", _plain_request(), user, db)
    assert isinstance(resp, FileResponse)
    assert resp.status_code == 200
