# -*- coding: utf-8 -*-
"""公共贡献池 PR-3：池副本播出（serve）+ H1 计量钩子。

钉死：
- PlaybackInfo：未绑用户 + ready 池副本 → 下发池副本免凭证直链
  （usercontent + 空头），响应里绝不出现共享原件 fid；授权限失败
  回落 alt=media + 贡献者令牌头（绝不下发 SA 令牌）；授权限与令牌
  都失败 → 保留本站 /stream（由取流端点现场裁决），不记 H1。
- quota 改道：探测判额度页时，后路关 → 剥清单 + NoCompatibleStream
  明示；后路开 → 保留 /stream（取流时走贡献者令牌中继）。
- 成功下发记账：pool_play_events 落一条原始事件（H1）、
  pool_usage_daily.play_starts +1、变体 last_played_at 更新。
- video_stream：无签直打走池门禁——ready → 302 池副本；准备中 →
  409 明示；池关 → 原 403 文案；显式选其它变体 → 409 不走共享；
  已下发签名消费同样落池副本（不落共享原件）。
- sign 轨：未绑用户池 eligible 且副本 ready 才可签（design §4.1）。
- 变体清单：pool_copy 信号 + 变体行复核双对上才列「公共池」。

种子数据用独立用户名/库名/挂载 id，模块收尾删净。
"""
from __future__ import annotations

import os

os.environ.setdefault("DATABASE_TYPE", "sqlite")
os.environ.setdefault("REDIS_ENABLED", "false")
os.environ.setdefault("TRANSFER_VARIANT_ENABLED", "1")

import time
import uuid
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from backend import config_service, models
from backend.database import SessionLocal, init_db
from backend.emby_server import models as em
from backend.emby_server import playback_gate
from backend.emby_server import playback_strategy
from backend.emby_server import play_sign
from backend.emby_server import play_variants
from backend.emby_server import pool_gate
from backend.emby_server import transfer_variant as tv
from backend.integrations import store
from backend.security import hash_password

init_db()

PW = "poolsrv-pw"
BOUND = "poolsrv_bound"
FREE = "poolsrv_free"
CONTRIB = "poolsrv_contrib"
LIB_NAME = "池播出测试库"
MOUNT_ID = 9871
MOVIE_PATH = f"mount://{MOUNT_ID}/电影/池播出片 (2026)/池播出片.mp4"
ORIG_FID = "srvorigfid"
POOL_FID = "poolcopy"
MEMBER_TOKEN = "tok-member-xyz"
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
    db.query(em.PoolPlayEvent).delete(synchronize_session=False) \
        if hasattr(em, "PoolPlayEvent") else None
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
    db.query(em.StorageMount).filter(
        em.StorageMount.id == MOUNT_ID).delete(synchronize_session=False)
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
    cred2 = em.MountCredential(mount_id=None, scope="user",
                               owner_user_id=users[BOUND],
                               sealed_credential="", key_id="fernet-v1",
                               status="active")
    db.add(cred2)
    db.flush()
    db.add(em.UserStorageBinding(
        user_id=users[BOUND], provider="drive", account_label="自家盘",
        credential_id=cred2.id, status="active"))
    db.add(em.StorageMount(id=MOUNT_ID, name="msrv", mount_type="drive",
                           path="drive:drive1"))
    lib = em.Library(guid=_guid(), name=LIB_NAME, collection_type="movies",
                     is_enabled=True)
    db.add(lib)
    db.flush()
    movie = em.MediaItem(
        guid=_guid(), library_id=lib.id, item_type="movie", name="池播出片",
        drive_file_id=ORIG_FID, file_path=MOVIE_PATH, container="mp4",
        size=2_000_000_000, duration_ticks=118 * 60 * TICKS,
        probe_status="failed", probe_attempts=99)
    db.add(movie)
    db.commit()
    data = {"bound_id": users[BOUND], "free_id": users[FREE],
            "contrib_id": users[CONTRIB], "member_id": member.id,
            "binding_id": binding.id,
            "lib": lib.guid, "movie": movie.guid, "movie_id": movie.id}
    db.close()
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
    return (f"MediaBrowser Client={q}PoolSrvTest{q}, Device={q}iOS{q}, "
            f"DeviceId={q}poolsrv-dev-1{q}, Version={q}1.0{q}")


def _token(client, uname):
    r = client.post(
        "/emby/Users/AuthenticateByName",
        json={"Username": uname, "Pw": PW},
        headers={"X-Emby-Authorization": _auth_header()})
    assert r.status_code == 200, r.text
    return r.json()["AccessToken"]


@pytest.fixture(scope="module")
def free_token(client, seed):
    return _token(client, FREE)


def _h(token):
    return {"X-Emby-Token": token}


@pytest.fixture(autouse=True)
def _fake_member_quota(monkeypatch):
    class _FakeQuotaClient:
        def __init__(self, db_arg, binding):
            pass

        def about_storage(self, drive_id):
            return {"limit": 5 * TB, "usage": 1 * TB, "free": 4 * TB}

    monkeypatch.setattr(tv, "UserDriveApiClient", _FakeQuotaClient)
    yield
    pool_gate.clear_pool_quota_cache()


@pytest.fixture(autouse=True)
def _fake_drive_serve(monkeypatch):
    """池播出的外部腿全假：成员令牌、授权限、quota 探测两腿。"""
    from backend.emby_server import drive_oauth, drive_share
    monkeypatch.setattr(
        drive_oauth, "get_binding_drive_access_token",
        lambda db, binding, **kw: MEMBER_TOKEN)
    monkeypatch.setattr(
        drive_share, "ensure_shared", lambda fid, tok: "ok")
    monkeypatch.setattr(
        tv, "_usercopy_probe_http",
        lambda url: (200, "video/mp4", b"\x00" * 32))
    monkeypatch.setattr(
        tv, "_usercopy_probe_http_get",
        lambda url: (200, "video/mp4", b"\x00" * 32))


@pytest.fixture(autouse=True)
def _clean_pool_rows(seed):
    yield
    # 进程内缓存（已公开缓存/quota 探测缓存）跨用例不清会串味：
    # 已公开缓存是 6h TTL 的生产设计，测试间必须显式清。
    try:
        from backend.database import CacheManager
        with CacheManager._memory_cache_lock:
            for _k in [k for k in CacheManager._memory_cache
                       if "usercopy_public" in k or "usercopy_quota" in k]:
                CacheManager._memory_cache.pop(_k, None)
    except Exception:
        pass
    db = SessionLocal()
    try:
        db.query(em.TransferJob).filter(
            em.TransferJob.owner_scope == "pool").delete(
            synchronize_session=False)
        db.query(em.TransferVariant).filter(
            em.TransferVariant.owner_scope == "pool").delete(
            synchronize_session=False)
        db.query(em.PoolUsageDaily).delete(synchronize_session=False)
        if hasattr(em, "PoolPlayEvent"):
            db.query(em.PoolPlayEvent).delete(synchronize_session=False)
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


def _add_pool_variant(db, seed, fid=POOL_FID):
    row = em.TransferVariant(
        item_id=seed["movie_id"], source_file_id=ORIG_FID,
        variant_file_id=fid,
        variant_drive_id=f"pooldrive:{seed['member_id']}",
        owner_scope="pool", owner_ref=str(seed["member_id"]),
        status="ready", size_bytes=2_000_000_000)
    db.add(row)
    db.commit()
    db.refresh(row)
    return row.id


def _playbackinfo(client, token, guid):
    r = client.post(f"/emby/Items/{guid}/PlaybackInfo",
                    headers=_h(token), json={})
    assert r.status_code == 200, r.text
    return r.json()


def _play_events(db):
    return db.query(em.PoolPlayEvent).all()


def _usage_play_starts(db, member_id):
    row = (db.query(em.PoolUsageDaily)
           .filter(em.PoolUsageDaily.member_id == member_id).first())
    return int(row.play_starts or 0) if row else 0


# ---------------------------------------------------------------- 单元


def test_prefer_pool_variant():
    variant = SimpleNamespace(
        owner_scope="pool", variant_file_id=POOL_FID)
    gate = {"verdict": "serve_pool_copy", "variant": variant}
    fid, var = tv.prefer_pool_variant("orig", None, gate)
    assert fid == POOL_FID and var is variant
    # 非 serve / 非池行一律不动
    assert tv.prefer_pool_variant("orig", None, {"verdict": "proceed"}) \
        == ("orig", None)
    user_var = SimpleNamespace(owner_scope="user", variant_file_id="u1")
    assert tv.prefer_pool_variant(
        "orig", None, {"verdict": "serve_pool_copy", "variant": user_var}) \
        == ("orig", None)


def test_pool_shared_play_blocked():
    assert tv.pool_shared_play_blocked(None) is False
    assert tv.pool_shared_play_blocked(play_variants.POOL_COPY) is False
    assert tv.pool_shared_play_blocked("drive_direct") is True
    assert tv.pool_shared_play_blocked("drive_relay") is True


def test_ensure_variant_copy_public_pool_uses_member_token(seed, monkeypatch):
    from backend.emby_server import drive_share
    db = SessionLocal()
    try:
        variant_id = _add_pool_variant(db, seed)
        variant = db.query(em.TransferVariant).filter(
            em.TransferVariant.id == variant_id).one()
        seen = {}

        def _share(fid, tok):
            seen["fid"] = fid
            seen["tok"] = tok
            return "ok"

        monkeypatch.setattr(drive_share, "ensure_shared", _share)
        assert tv.ensure_variant_copy_public(db, variant, POOL_FID) is True
        assert seen == {"fid": POOL_FID, "tok": MEMBER_TOKEN}
    finally:
        db.close()


def test_ensure_variant_copy_public_pool_failures(seed, monkeypatch):
    from backend.emby_server import drive_oauth, drive_share
    db = SessionLocal()
    try:
        variant_id = _add_pool_variant(db, seed)
        variant = db.query(em.TransferVariant).filter(
            em.TransferVariant.id == variant_id).one()
        # 授权限失败
        monkeypatch.setattr(
            drive_share, "ensure_shared", lambda fid, tok: "failed")
        assert tv.ensure_variant_copy_public(db, variant, POOL_FID) is False
        # 令牌取不到
        monkeypatch.setattr(
            drive_share, "ensure_shared", lambda fid, tok: "ok")
        monkeypatch.setattr(
            drive_oauth, "get_binding_drive_access_token",
            lambda db_, binding, **kw: "")
        assert tv.ensure_variant_copy_public(db, variant, POOL_FID) is False
        # 成员不存在
        ghost = SimpleNamespace(
            owner_scope="pool", owner_ref="99999999",
            variant_drive_id="pooldrive:99999999")
        assert tv.ensure_variant_copy_public(db, ghost, POOL_FID) is False
        # 缺 fid / 非池非用户行
        assert tv.ensure_variant_copy_public(db, variant, "") is False
        src = SimpleNamespace(owner_scope="source", owner_ref=None)
        assert tv.ensure_variant_copy_public(db, src, POOL_FID) is False
    finally:
        db.close()


def test_ensure_variant_copy_public_user_delegates(seed, monkeypatch):
    seen = {}

    def _fake_user_public(db, uid, fid):
        seen["uid"] = uid
        seen["fid"] = fid
        return True

    monkeypatch.setattr(tv, "ensure_user_copy_public", _fake_user_public)
    variant = SimpleNamespace(owner_scope="user", owner_ref="4242")
    db = SessionLocal()
    try:
        assert tv.ensure_variant_copy_public(db, variant, "ufid") is True
    finally:
        db.close()
    assert seen == {"uid": 4242, "fid": "ufid"}


def test_note_pool_play_served_writes_all(seed):
    db = SessionLocal()
    try:
        variant_id = _add_pool_variant(db, seed)
        pool_gate.note_pool_play_served(
            seed["member_id"], variant_id, seed["movie_id"],
            seed["free_id"])
        events = _play_events(db)
        assert len(events) == 1
        ev = events[0]
        assert ev.member_id == seed["member_id"]
        assert ev.variant_id == variant_id
        assert ev.item_id == seed["movie_id"]
        assert ev.viewer_user_id == seed["free_id"]
        assert ev.created_at is not None
        assert _usage_play_starts(db, seed["member_id"]) == 1
        row = db.query(em.TransferVariant).filter(
            em.TransferVariant.id == variant_id).one()
        assert row.last_played_at is not None
    finally:
        db.close()


def test_note_pool_play_served_silent_on_failure(seed, monkeypatch):
    def _boom():
        raise RuntimeError("db down")

    monkeypatch.setattr("backend.database.SessionLocal", _boom)
    # 永不抛：记账失败不许打挂播出
    pool_gate.note_pool_play_served(1, 1, 1, 1)


def test_pool_member_access_token(seed, monkeypatch):
    from backend.emby_server import drive_oauth
    db = SessionLocal()
    try:
        assert pool_gate.pool_member_access_token(
            db, seed["member_id"]) == MEMBER_TOKEN
        assert pool_gate.pool_member_access_token(db, 99999999) == ""
        monkeypatch.setattr(
            drive_oauth, "get_binding_drive_access_token",
            lambda db_, binding, **kw: "")
        assert pool_gate.pool_member_access_token(
            db, seed["member_id"]) == ""
    finally:
        db.close()


def test_sign_allowed_pool_matrix(seed):
    db = SessionLocal()
    try:
        item = db.query(em.MediaItem).filter(
            em.MediaItem.id == seed["movie_id"]).one()
        free = seed["free_id"]
        # 无池副本：不可签（与 PR-3 前一致）
        assert playback_gate.sign_allowed_for_item(db, free, item) is False
        _add_pool_variant(db, seed)
        # 池开 + ready 池副本：可签
        assert playback_gate.sign_allowed_for_item(db, free, item) is True
        # 已绑用户恒可签（与池无关）
        assert playback_gate.sign_allowed_for_item(
            db, seed["bound_id"], item) is True
        # 池关：回到不可签
        config_service.save_config(db, {"pool_enabled": False},
                                   schema="pool")
        db.commit()
        store.invalidate()
        assert playback_gate.sign_allowed_for_item(db, free, item) is False
    finally:
        db.close()


def test_list_variants_pool_copy_signal(seed):
    db = SessionLocal()
    try:
        item = db.query(em.MediaItem).filter(
            em.MediaItem.id == seed["movie_id"]).one()
        names = [v.name for v in play_variants.list_variants(db, item)]
        assert play_variants.POOL_COPY not in names
        vs = play_variants.list_variants(
            db, item, pool_copy_available=True)
        pool_vs = [v for v in vs if v.name == play_variants.POOL_COPY]
        assert len(pool_vs) == 1
        v = pool_vs[0]
        assert v.label == "公共池"
        assert v.protocol == "Http"
        assert v.is_remote is True
        assert v.needs_relay is False
    finally:
        db.close()


# ---------------------------------------------------------------- PlaybackInfo


def test_playbackinfo_pool_serve_usercontent(client, free_token, seed):
    db = SessionLocal()
    try:
        variant_id = _add_pool_variant(db, seed)
    finally:
        db.close()
    body = _playbackinfo(client, free_token, seed["movie"])
    text = str(body)
    assert ORIG_FID not in text
    assert MEMBER_TOKEN not in text
    src = body["MediaSources"][0]
    assert "usercontent" in src["DirectPlayUrl"]
    assert POOL_FID in src["DirectPlayUrl"]
    assert src["RequiredHttpHeaders"] == {}
    assert src.get("TransferStatus") in (None, "ready")
    # H1 + §5.3 记账已落
    db = SessionLocal()
    try:
        events = _play_events(db)
        assert len(events) == 1
        assert events[0].member_id == seed["member_id"]
        assert events[0].variant_id == variant_id
        assert events[0].viewer_user_id == seed["free_id"]
        assert _usage_play_starts(db, seed["member_id"]) == 1
    finally:
        db.close()


def test_playbackinfo_pool_serve_alt_media_fallback(
        client, free_token, seed, monkeypatch):
    from backend.emby_server import drive_share
    monkeypatch.setattr(
        drive_share, "ensure_shared", lambda fid, tok: "failed")
    db = SessionLocal()
    try:
        _add_pool_variant(db, seed)
    finally:
        db.close()
    body = _playbackinfo(client, free_token, seed["movie"])
    assert ORIG_FID not in str(body)
    src = body["MediaSources"][0]
    assert "alt=media" in src["DirectPlayUrl"]
    assert POOL_FID in src["DirectPlayUrl"]
    # 回落头里是贡献者令牌，绝不是 SA 令牌
    assert src["RequiredHttpHeaders"] == {
        "Authorization": f"Bearer {MEMBER_TOKEN}"}
    db = SessionLocal()
    try:
        assert len(_play_events(db)) == 1
    finally:
        db.close()


def test_playbackinfo_pool_serve_public_fail_keeps_stream(
        client, free_token, seed, monkeypatch):
    from backend.emby_server import drive_oauth, drive_share
    monkeypatch.setattr(
        drive_share, "ensure_shared", lambda fid, tok: "failed")
    monkeypatch.setattr(
        drive_oauth, "get_binding_drive_access_token",
        lambda db_, binding, **kw: "")
    db = SessionLocal()
    try:
        _add_pool_variant(db, seed)
    finally:
        db.close()
    body = _playbackinfo(client, free_token, seed["movie"])
    assert ORIG_FID not in str(body)
    src = body["MediaSources"][0]
    assert "usercontent" not in (src.get("DirectPlayUrl") or "")
    assert "stream" in (src.get("DirectPlayUrl") or "")
    # 未下发直链 → 本次不记 H1（取流端点下发时再记）
    db = SessionLocal()
    try:
        assert _play_events(db) == []
    finally:
        db.close()


def _quota_probe(monkeypatch):
    monkeypatch.setattr(
        tv, "_usercopy_probe_http",
        lambda url: (200, "text/html; charset=utf-8",
                     b"<html><body>Download quota exceeded</body></html>"))
    monkeypatch.setattr(
        tv, "_usercopy_probe_http_get",
        lambda url: (200, "text/html; charset=utf-8",
                     b"<html><body>Download quota exceeded</body></html>"))


def _fallback_switch(monkeypatch, value):
    real_get = playback_strategy.store.get_value

    def fake_get(db_, key, default="", **kwargs):
        if key == playback_strategy.KEY_USERCOPY_QUOTA_RELAY_FALLBACK:
            return value
        return real_get(db_, key, default, **kwargs)

    monkeypatch.setattr(playback_strategy.store, "get_value", fake_get)


def test_playbackinfo_pool_quota_no_fallback_strips(
        client, free_token, seed, monkeypatch):
    _quota_probe(monkeypatch)
    _fallback_switch(monkeypatch, "false")
    db = SessionLocal()
    try:
        _add_pool_variant(db, seed)
    finally:
        db.close()
    body = _playbackinfo(client, free_token, seed["movie"])
    assert ORIG_FID not in str(body)
    assert body["ErrorCode"] == "NoCompatibleStream"
    assert body["ErrorMessage"] == tv.POOLCOPY_QUOTA_DETAIL
    src = body["MediaSources"][0]
    assert "DirectPlayUrl" not in src
    assert "DirectStreamUrl" not in src
    db = SessionLocal()
    try:
        assert _play_events(db) == []
    finally:
        db.close()


def test_playbackinfo_pool_quota_with_fallback_keeps_stream(
        client, free_token, seed, monkeypatch):
    _quota_probe(monkeypatch)
    _fallback_switch(monkeypatch, "true")
    db = SessionLocal()
    try:
        _add_pool_variant(db, seed)
    finally:
        db.close()
    body = _playbackinfo(client, free_token, seed["movie"])
    assert ORIG_FID not in str(body)
    assert body.get("ErrorCode") is None
    src = body["MediaSources"][0]
    assert "stream" in (src.get("DirectPlayUrl") or "")
    assert "usercontent" not in (src.get("DirectPlayUrl") or "")


# ---------------------------------------------------------------- video_stream


def _stream(client, token, guid, query=""):
    return client.get(f"/emby/Videos/{guid}/stream.mp4{query}",
                      headers=_h(token), follow_redirects=False)


def test_stream_pool_serve_302(client, free_token, seed):
    db = SessionLocal()
    try:
        variant_id = _add_pool_variant(db, seed)
    finally:
        db.close()
    r = _stream(client, free_token, seed["movie"])
    assert r.status_code == 302, r.text
    loc = r.headers.get("location") or ""
    assert "usercontent" in loc and POOL_FID in loc
    assert ORIG_FID not in loc
    assert ORIG_FID not in r.text
    db = SessionLocal()
    try:
        events = _play_events(db)
        assert len(events) == 1
        assert events[0].variant_id == variant_id
        assert events[0].viewer_user_id == seed["free_id"]
        assert _usage_play_starts(db, seed["member_id"]) == 1
    finally:
        db.close()


def test_stream_pool_preparing_409(client, free_token, seed, monkeypatch):
    monkeypatch.setenv("TRANSFER_WAIT_STREAM_ENABLED", "0")
    r = _stream(client, free_token, seed["movie"])
    assert r.status_code == 409, r.text
    assert "转存准备中" in r.text
    assert ORIG_FID not in r.text


def test_stream_pool_explicit_other_variant_409(client, free_token, seed):
    db = SessionLocal()
    try:
        _add_pool_variant(db, seed)
    finally:
        db.close()
    r = _stream(client, free_token, seed["movie"],
                query="?variant=drive_direct")
    assert r.status_code == 409, r.text
    assert tv.POOL_SHARED_BLOCKED_DETAIL in r.text
    assert ORIG_FID not in r.text


def test_stream_pool_off_403(client, free_token, seed):
    db = SessionLocal()
    try:
        _add_pool_variant(db, seed)
        config_service.save_config(db, {"pool_enabled": False},
                                   schema="pool")
        db.commit()
    finally:
        db.close()
    store.invalidate()
    r = _stream(client, free_token, seed["movie"])
    assert r.status_code == 403, r.text
    assert playback_gate.GATE_MESSAGE in r.text


def test_stream_pool_quota_no_fallback_409(
        client, free_token, seed, monkeypatch):
    _quota_probe(monkeypatch)
    _fallback_switch(monkeypatch, "false")
    db = SessionLocal()
    try:
        _add_pool_variant(db, seed)
    finally:
        db.close()
    r = _stream(client, free_token, seed["movie"])
    assert r.status_code == 409, r.text
    assert tv.POOLCOPY_QUOTA_DETAIL in r.text
    assert "location" not in {k.lower() for k in r.headers}
    assert ORIG_FID not in r.text


def test_stream_pool_signed_url_serves_copy(client, free_token, seed):
    db = SessionLocal()
    try:
        _add_pool_variant(db, seed)
    finally:
        db.close()
    exp = int(time.time()) + 3600
    sign = play_sign.make_play_sign(seed["free_id"], seed["movie"], exp)
    r = _stream(client, free_token, seed["movie"],
                query=f"?uid={seed['free_id']}&exp={exp}&sign={sign}")
    assert r.status_code == 302, r.text
    loc = r.headers.get("location") or ""
    assert POOL_FID in loc
    assert ORIG_FID not in loc
    db = SessionLocal()
    try:
        assert len(_play_events(db)) == 1
    finally:
        db.close()
