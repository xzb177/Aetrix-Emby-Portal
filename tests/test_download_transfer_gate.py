# -*- coding: utf-8 -*-
"""Download 端点首播转存门禁（完美审计 2026-11-02 R2·B2，PA-003）。

背景：/Items/{id}/Download 在可见性＋付费墙＋下载开关后直接
_play_target，全文件 playback_gate/transfer_variant/
ensure_user_copy_gate 出现 0 次——已绑定但尚无自己盘副本的用户
可经 Download 拿到共享原件字节（Drive 条目 302 到 usercontent
直链），绕过 File/流端点同款 409。

本文件锁定 Download 与 File 同口径：
- 罐装 verdict：preparing/failed → 409（detail 与 File 同款）、
  error → 503 fail-closed、proceed 放行；
- 真门禁集成：无 ready 副本 → 409 且建转存单；有 ready 副本 →
  照常 302；有效 play_sign 消费 → 不追溯；
- 不变项：下载开关关、付费墙仍在门禁之前生效；Drive 直链与
  本机文件两种目标各一例。
- 本单只补转存门禁，不加绑定门禁（下载绑定门禁口径用户
  2026-10-26 明确未定）。
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
from backend.emby_server import media_routes
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


# ---------- 罐装 verdict：Download 与 File 同款拦截/放行 ----------

@pytest.fixture()
def _download(monkeypatch):
    item = types.SimpleNamespace(
        id=31, drive_file_id="fid-123", file_path="mount://3/电影/a.mkv")
    monkeypatch.setattr(
        media_routes, "_require_visible_item",
        lambda db, user, item_id: item)
    monkeypatch.setattr(
        media_routes, "ensure_playback_allowed", lambda db, user: None)
    monkeypatch.setattr(
        media_routes, "ensure_download_allowed",
        lambda db, user, request=None: None)
    monkeypatch.setattr(
        media_routes, "_play_target",
        lambda db, item: types.SimpleNamespace(kind="url", value=""))
    from backend.emby_server import drive_auth
    monkeypatch.setattr(
        drive_auth, "drive_usercontent_download_url",
        lambda fid: "https://example.invalid/dl/" + fid)
    return item


def test_download_preparing_blocked(db, _download, monkeypatch):
    user = _user(db)
    _binding(db, user.id)
    _patch_gate(monkeypatch, {"verdict": "preparing"})
    with pytest.raises(HTTPException) as excinfo:
        media_routes.download_item("guid-1", _plain_request(), user, db)
    assert excinfo.value.status_code == 409
    assert "转存准备中" in excinfo.value.detail


def test_download_failed_blocked(db, _download, monkeypatch):
    user = _user(db)
    _binding(db, user.id)
    _patch_gate(monkeypatch, {"verdict": "failed", "detail": "转存失败说明"})
    with pytest.raises(HTTPException) as excinfo:
        media_routes.download_item("guid-1", _plain_request(), user, db)
    assert excinfo.value.status_code == 409
    assert excinfo.value.detail == "转存失败说明"


def test_download_error_is_503(db, _download, monkeypatch):
    user = _user(db)
    _binding(db, user.id)
    _patch_gate(monkeypatch, {"verdict": "error"})
    with pytest.raises(HTTPException) as excinfo:
        media_routes.download_item("guid-1", _plain_request(), user, db)
    assert excinfo.value.status_code == 503


def test_download_proceed_drive_302(db, _download, monkeypatch):
    user = _user(db)
    _binding(db, user.id)
    _patch_gate(monkeypatch, {"verdict": "proceed"})
    resp = media_routes.download_item("guid-1", _plain_request(), user, db)
    assert resp.status_code == 302
    assert resp.headers["location"].startswith("https://example.invalid/dl/")


def test_download_proceed_local_file(db, _download, tmp_path, monkeypatch):
    path = tmp_path / "movie.mkv"
    path.write_bytes(b"video-bytes")
    monkeypatch.setattr(
        media_routes, "_play_target",
        lambda db, item: types.SimpleNamespace(kind="file", value=str(path)))
    user = _user(db)
    _binding(db, user.id)
    _patch_gate(monkeypatch, {"verdict": "proceed"})
    resp = media_routes.download_item("guid-1", _plain_request(), user, db)
    assert isinstance(resp, FileResponse)
    assert resp.status_code == 200


def test_download_switch_off_still_403(db, _download, monkeypatch):
    def _off(db, user, request=None):
        raise HTTPException(status_code=403, detail="下载已关闭")
    monkeypatch.setattr(media_routes, "ensure_download_allowed", _off)
    user = _user(db)
    _binding(db, user.id)
    _patch_gate(monkeypatch, {"verdict": "proceed"})
    with pytest.raises(HTTPException) as excinfo:
        media_routes.download_item("guid-1", _plain_request(), user, db)
    assert excinfo.value.status_code == 403


def test_download_paywall_still_403(db, _download, monkeypatch):
    def _wall(db, user):
        raise HTTPException(status_code=403, detail="需要订阅")
    monkeypatch.setattr(media_routes, "ensure_playback_allowed", _wall)
    user = _user(db)
    _binding(db, user.id)
    _patch_gate(monkeypatch, {"verdict": "proceed"})
    with pytest.raises(HTTPException) as excinfo:
        media_routes.download_item("guid-1", _plain_request(), user, db)
    assert excinfo.value.status_code == 403


# ---------- 真门禁集成 ----------

@pytest.fixture()
def _download_real(db, monkeypatch):
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
        media_routes, "_require_visible_item",
        lambda db_, user, item_id: item)
    monkeypatch.setattr(
        media_routes, "ensure_playback_allowed", lambda db_, user: None)
    monkeypatch.setattr(
        media_routes, "ensure_download_allowed",
        lambda db_, user, request=None: None)
    monkeypatch.setattr(
        media_routes, "_play_target",
        lambda db_, it: types.SimpleNamespace(kind="url", value=""))
    from backend.emby_server import drive_auth
    monkeypatch.setattr(
        drive_auth, "drive_usercontent_download_url",
        lambda fid: "https://example.invalid/dl/" + fid)
    return item


def test_download_real_no_copy_blocked_and_job_created(db, _download_real):
    user = _user(db)
    _binding(db, user.id)
    with pytest.raises(HTTPException) as excinfo:
        media_routes.download_item("guid-1", _plain_request(), user, db)
    assert excinfo.value.status_code == 409
    jobs = (db.query(em.TransferJob)
            .filter(em.TransferJob.item_id == _download_real.id,
                    em.TransferJob.owner_ref == str(user.id))
            .all())
    assert len(jobs) == 1


def test_download_real_ready_copy_passes(db, _download_real):
    user = _user(db)
    _binding(db, user.id)
    db.add(em.TransferVariant(
        item_id=_download_real.id, source_file_id="origfid",
        variant_file_id="copyfid",
        variant_drive_id=f"userdrive:{user.id}",
        owner_scope="user", owner_ref=str(user.id),
        status="ready", size_bytes=1000))
    db.commit()
    resp = media_routes.download_item("guid-1", _plain_request(), user, db)
    assert resp.status_code == 302
    assert resp.headers["location"].startswith("https://example.invalid/dl/")


def test_download_real_signed_consume_not_blocked(db, _download_real,
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
    resp = media_routes.download_item("guid-1", req, user, db)
    assert resp.status_code == 302
