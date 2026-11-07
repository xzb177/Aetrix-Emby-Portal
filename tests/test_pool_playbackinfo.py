# -*- coding: utf-8 -*-
"""公共贡献池 PR-2：PlaybackInfo 池分支接线。

用真实 app + TestClient（沿 tests/test_detail_mediasource_stream_url.py
的既有形态）：未绑用户 + 池开 → preparing 清单（建池单）；已有
ready 池副本 → 本单播出未接通，回 failed 明示（不下发任何地址）；
已绑用户有本人 ready 副本 → 原链路逐字不变；池关 → 未绑用户 403。

种子数据用独立用户名/库名，模块收尾删净，不污染共享测试库。
"""
from __future__ import annotations

import os

os.environ.setdefault("DATABASE_TYPE", "sqlite")
os.environ.setdefault("REDIS_ENABLED", "false")
# 注意：TRANSFER_VARIANT_ENABLED 不能在这里导入期 setdefault——
# 它是运行时动态读的开关，导入期写死会泄漏进同进程后续全部用例
# （总闸被误开，别的文件的桩库/桩条目会走进转存门禁深处报错）。
# 改由下方 autouse fixture 用 monkeypatch.setenv 按用例设置、
# 用例结束自动撤销。

import uuid

import pytest
from fastapi.testclient import TestClient

from backend import config_service, models
from backend.database import SessionLocal, init_db
from backend.emby_server import models as em
from backend.emby_server import pool_gate
from backend.emby_server import transfer_variant as tv
from backend.integrations import store
from backend.security import hash_password

init_db()

PW = "poolpi-pw"
BOUND = "poolpi_bound"
FREE = "poolpi_free"
CONTRIB = "poolpi_contrib"
LIB_NAME = "池 PlaybackInfo 测试库"
MOVIE_PATH = "mount://3/电影/池测试片 (2026)/池测试片.mp4"
ORIG_FID = "origfid"
TICKS = 10_000_000
TB = 1000 ** 4


def _guid():
    return uuid.uuid4().hex


def _cleanup(db):
    for uname in (BOUND, FREE, CONTRIB):
        old = db.query(models.WebUser).filter(
            models.WebUser.username == uname).first()
        if old:
            cred_ids = [r[0] for r in db.query(em.UserStorageBinding.credential_id)
                        .filter(em.UserStorageBinding.user_id == old.id).all()]
            db.query(em.PoolMember).filter(
                em.PoolMember.user_id == old.id).delete(
                synchronize_session=False)
            db.query(em.UserStorageBinding).filter(
                em.UserStorageBinding.user_id == old.id).delete(
                synchronize_session=False)
            if cred_ids:
                db.query(em.MountCredential).filter(
                    em.MountCredential.id.in_(cred_ids)).delete(
                    synchronize_session=False)
            db.query(em.EmbyApiToken).filter(
                em.EmbyApiToken.user_id == old.id).delete(
                synchronize_session=False)
            db.query(models.WebUser).filter(
                models.WebUser.id == old.id).delete()
    db.query(em.TransferJob).filter(
        em.TransferJob.owner_scope == "pool").delete(
        synchronize_session=False)
    db.query(em.TransferVariant).filter(
        em.TransferVariant.owner_scope == "pool").delete(
        synchronize_session=False)
    db.query(em.PoolUsageDaily).delete(synchronize_session=False)
    db.query(em.TransferDecisionRound).filter(
        em.TransferDecisionRound.scope == "pool_pick").delete(
        synchronize_session=False)
    db.query(models.SystemConfig).filter(
        models.SystemConfig.key.like("pool_%")).delete(
        synchronize_session=False)
    for lib in db.query(em.Library).filter(em.Library.name == LIB_NAME).all():
        ids = [r[0] for r in db.query(em.MediaItem.id)
               .filter(em.MediaItem.library_id == lib.id).all()]
        if ids:
            db.query(em.TransferVariant).filter(
                em.TransferVariant.item_id.in_(ids)).delete(
                synchronize_session=False)
            db.query(em.TransferJob).filter(
                em.TransferJob.item_id.in_(ids)).delete(
                synchronize_session=False)
            db.query(em.MediaStream).filter(
                em.MediaStream.item_id.in_(ids)).delete(
                synchronize_session=False)
            db.query(em.ItemFacet).filter(
                em.ItemFacet.item_id.in_(ids)).delete(
                synchronize_session=False)
            db.query(em.MediaItem).filter(em.MediaItem.id.in_(ids)).delete(
                synchronize_session=False)
        db.query(em.Library).filter(em.Library.id == lib.id).delete()
    db.query(em.StorageMount).filter(em.StorageMount.id == 3).delete(
        synchronize_session=False)
    db.commit()
    store.invalidate()


@pytest.fixture(scope="module")
def seed():
    db = SessionLocal()
    _cleanup(db)
    users = {}
    for uname in (BOUND, FREE, CONTRIB):
        u = models.WebUser(username=uname, password_hash=hash_password(PW),
                           is_active=True, is_staff=True, emby_username=uname,
                           emby_password=hash_password(PW))
        db.add(u)
        db.flush()
        users[uname] = u.id
    # 贡献者：active Drive 绑定 + active 池成员
    cred = em.MountCredential(mount_id=None, scope="user",
                              owner_user_id=users[CONTRIB],
                              sealed_credential="", key_id="fernet-v1",
                              status="active")
    db.add(cred)
    db.flush()
    binding = em.UserStorageBinding(
        user_id=users[CONTRIB], provider="drive", account_label="贡献盘",
        credential_id=cred.id, status="active")
    db.add(binding)
    db.flush()
    member = em.PoolMember(
        binding_id=binding.id, user_id=users[CONTRIB], provider="drive",
        status="active")
    db.add(member)
    # 已绑用户：active Drive 绑定（对照组）
    cred2 = em.MountCredential(mount_id=None, scope="user",
                               owner_user_id=users[BOUND],
                               sealed_credential="", key_id="fernet-v1",
                               status="active")
    db.add(cred2)
    db.flush()
    db.add(em.UserStorageBinding(
        user_id=users[BOUND], provider="drive", account_label="自家盘",
        credential_id=cred2.id, status="active"))
    db.add(em.StorageMount(id=3, name="m3", mount_type="drive",
                           path="drive:drive1"))
    lib = em.Library(guid=_guid(), name=LIB_NAME, collection_type="movies",
                     is_enabled=True)
    db.add(lib)
    db.flush()
    movie = em.MediaItem(
        guid=_guid(), library_id=lib.id, item_type="movie", name="池测试片",
        drive_file_id=ORIG_FID, file_path=MOVIE_PATH, container="mp4",
        size=2_000_000_000, duration_ticks=118 * 60 * TICKS,
        probe_status="failed", probe_attempts=99)
    db.add(movie)
    db.commit()
    member_id = member.id
    movie_id = movie.id
    data = {"bound_id": users[BOUND], "free_id": users[FREE],
            "contrib_id": users[CONTRIB], "member_id": member_id,
            "lib": lib.guid, "movie": movie.guid, "movie_id": movie_id}
    db.close()
    # 池开（默认抖动 0，建单即到期可认领；测试内不跑工人）
    db = SessionLocal()
    config_service.save_config(
        db, {"pool_enabled": True, "pool_enqueue_jitter_sec": 0},
        schema="pool")
    db.commit()
    db.close()
    store.invalidate()
    yield data
    db = SessionLocal()
    try:
        _cleanup(db)
    finally:
        db.close()


@pytest.fixture(scope="module")
def client():
    from backend.main import app
    return TestClient(app)


def _auth_header():
    q = chr(34)
    return (f"MediaBrowser Client={q}PoolPiTest{q}, Device={q}iOS{q}, "
            f"DeviceId={q}poolpi-dev-1{q}, Version={q}1.0{q}")


def _token(client, uname):
    r = client.post(
        "/emby/Users/AuthenticateByName",
        json={"Username": uname, "Pw": PW},
        headers={"X-Emby-Authorization": _auth_header()})
    assert r.status_code == 200, r.text
    return r.json()["AccessToken"]


@pytest.fixture(scope="module")
def bound_token(client, seed):
    return _token(client, BOUND)


@pytest.fixture(scope="module")
def free_token(client, seed):
    return _token(client, FREE)


def _h(token):
    return {"X-Emby-Token": token}


@pytest.fixture(autouse=True)
def _fake_member_quota(monkeypatch):
    """选盘读盘剩余走假客户端（不碰真实 Drive）。"""
    # 本文件用例需要转存总闸开：按用例设置，结束自动撤销，
    # 不许导入期写死泄漏给同进程其它文件（见文件头注释）。
    monkeypatch.setenv("TRANSFER_VARIANT_ENABLED", "1")

    class _FakeQuotaClient:
        def __init__(self, db_arg, binding):
            pass

        def about_storage(self, drive_id):
            return {"limit": 5 * TB, "usage": 1 * TB, "free": 4 * TB}

    monkeypatch.setattr(tv, "UserDriveApiClient", _FakeQuotaClient)
    yield
    pool_gate.clear_pool_quota_cache()


@pytest.fixture(autouse=True)
def _clean_pool_rows(seed):
    yield
    db = SessionLocal()
    try:
        db.query(em.TransferJob).filter(
            em.TransferJob.owner_scope == "pool").delete(
            synchronize_session=False)
        db.query(em.TransferVariant).filter(
            em.TransferVariant.owner_scope == "pool").delete(
            synchronize_session=False)
        db.query(em.PoolUsageDaily).delete(synchronize_session=False)
        db.query(em.TransferDecisionRound).filter(
            em.TransferDecisionRound.scope == "pool_pick").delete(
            synchronize_session=False)
        config_service.save_config(
            db, {"pool_enabled": True, "pool_enqueue_jitter_sec": 0},
            schema="pool")
        db.commit()
    finally:
        db.close()
    store.invalidate()
    pool_gate.clear_pool_quota_cache()


def _playbackinfo(client, token, guid):
    r = client.post(f"/emby/Items/{guid}/PlaybackInfo",
                    headers=_h(token), json={})
    assert r.status_code == 200, r.text
    return r.json()


def test_playbackinfo_pool_preparing_strips_urls(client, free_token, seed):
    body = _playbackinfo(client, free_token, seed["movie"])
    assert ORIG_FID not in str(body)
    src = body["MediaSources"][0]
    assert src["TransferStatus"] == "preparing"
    assert src["Readiness"] == "preparing"
    assert "DirectPlayUrl" not in src
    assert "DirectStreamUrl" not in src
    # 池单已建（owner_scope=pool）
    db = SessionLocal()
    try:
        job = (db.query(em.TransferJob)
               .filter(em.TransferJob.item_id == seed["movie_id"],
                       em.TransferJob.owner_scope == "pool").one())
        assert job.status == "pending"
        assert job.dest_drive_id == f"pooldrive:{seed['member_id']}"
    finally:
        db.close()


def test_playbackinfo_pool_serve_returns_failed(client, free_token, seed):
    db = SessionLocal()
    try:
        # 就绪池副本：variant_drive_id 按成员 id 落（与成员行一致）。
        # 注意 SessionLocal 为 autoflush=False，add 后必须先 commit 再
        # 让端点读到（此前无 commit 的 .one() 复查在 preparing 先跑、
        # 清掉残留行后会 NoResultFound）。
        db.add(em.TransferVariant(
            item_id=seed["movie_id"], source_file_id=ORIG_FID,
            variant_file_id="poolcopy",
            variant_drive_id=f"pooldrive:{seed['member_id']}",
            owner_scope="pool", owner_ref=str(seed["member_id"]),
            status="ready", size_bytes=2_000_000_000))
        db.commit()
    finally:
        db.close()
    body = _playbackinfo(client, free_token, seed["movie"])
    assert ORIG_FID not in str(body)
    src = body["MediaSources"][0]
    assert src["TransferStatus"] == "failed"
    assert src["Readiness"] == "failed"
    assert "DirectPlayUrl" not in src
    assert "DirectStreamUrl" not in src
    # 本单不播出：不建新池单
    db = SessionLocal()
    try:
        assert (db.query(em.TransferJob)
                .filter(em.TransferJob.item_id == seed["movie_id"],
                        em.TransferJob.owner_scope == "pool").count() == 0)
    finally:
        db.close()


def test_playbackinfo_bound_user_ready_variant_unchanged(
        client, bound_token, seed):
    db = SessionLocal()
    try:
        db.add(em.TransferVariant(
            item_id=seed["movie_id"], source_file_id=ORIG_FID,
            variant_file_id="usercopy",
            variant_drive_id=f"userdrive:{seed['bound_id']}",
            owner_scope="user", owner_ref=str(seed["bound_id"]),
            status="ready", size_bytes=2_000_000_000))
        db.commit()
    finally:
        db.close()
    try:
        body = _playbackinfo(client, bound_token, seed["movie"])
        src = body["MediaSources"][0]
        assert "DirectPlayUrl" in src
        assert src.get("TransferStatus") in (None, "ready")
    finally:
        db = SessionLocal()
        try:
            db.query(em.TransferVariant).filter(
                em.TransferVariant.item_id == seed["movie_id"],
                em.TransferVariant.owner_scope == "user").delete(
                synchronize_session=False)
            db.commit()
        finally:
            db.close()


def test_playbackinfo_pool_off_403(client, free_token, seed):
    db = SessionLocal()
    try:
        config_service.save_config(db, {"pool_enabled": False},
                                   schema="pool")
        db.commit()
    finally:
        db.close()
    store.invalidate()
    r = client.post(f"/emby/Items/{seed['movie']}/PlaybackInfo",
                    headers=_h(free_token), json={})
    assert r.status_code == 403, r.text
