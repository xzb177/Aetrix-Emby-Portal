# -*- coding: utf-8 -*-
"""用户 Drive 副本已死/失联但变体行仍 ready（2026-10-30 生产实锤）。

实锤样本：item 688899《大唐妖探》、user 1 的用户副本行 id=651
status=ready、stale_count=0，但点播回 404「Drive 文件已不存在」。
生产亲验定性（属主令牌 files.get 对照）：副本文件本体仍活在用户
当前绑定的谷歌账号里（200、未进回收站），只是解绑旧绑定时
anyone-reader 被收回（share_revoked_at=2026-10-30 01:58:18 UTC，
与解绑同分钟）、行仍标 ready——非属主令牌（公开链 / SA files.get）
一律 404。缺陷面有三层，本文件逐层钉住：

- 门禁盲信 ready 行：Drive 行不像 115 行那样享有 stale strike
  不信任（stale_count>=1 仍直接 proceed）→ 记过 strike 的 Drive
  行同样落到任务逻辑走重建/准备中（同 provider + 跨 provider）。
- serve 路径把 404 原样甩用户：video_stream 直链存在性校验 404
  时先对账（reconcile_dead_drive_copy）——能自愈（重授公开）就
  重走校验放行 302；文件真死了记 strike + 预算内重建，回 409
  准备中文案；预算耗尽回 409 上限文案；绝不裸 404。
- 对账自愈口径：ensure_user_copy_public 用当前绑定令牌真打谷歌，
  文件活着才授得成；授不成才 strike+重建，且重建走
  _gate_retrigger_budget_ok 同款预算（24h 同桶 <2 行），不死循环。

全部离线：谷歌侧调用一律桩替身或 sqlite 内存库，不碰真实谷歌
与真实 DB。video_stream 桩接线沿用 test_usercopy_probe_get_shape。
"""
import os

os.environ.setdefault("DATABASE_TYPE", "sqlite")
os.environ.setdefault("REDIS_ENABLED", "false")

import asyncio
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend import models as base_models
from backend.emby_server import models as em
from backend.emby_server import transfer_variant as tv

SHARED_FID = "SHAREDorigFID000000000000000000"
COPY_FID = "USERcopyFID11111111111111111111"


@pytest.fixture()
def db():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False},
                           poolclass=StaticPool)
    base_models.Base.metadata.create_all(engine)
    em.Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    yield session
    session.close()


_counter = [0]


def _item(db, **kw):
    _counter[0] += 1
    row = em.MediaItem(guid=f"dc-{_counter[0]}", library_id=1,
                       item_type="movie", name="测试片",
                       drive_file_id="origfid",
                       file_path="mount://3/电影/a.mkv", size=1000)
    for key, value in kw.items():
        setattr(row, key, value)
    db.add(row)
    db.commit()
    return row


def _mount(db, mount_id=3, mount_type="drive", path="drive:drive1"):
    row = em.StorageMount(id=mount_id, name=f"m{mount_id}",
                          mount_type=mount_type, path=path)
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
                                credential_id=cred.id,
                                status=status)
    db.add(row)
    db.commit()
    return row


def _variant(db, item_id, scope="user", ref="7", status="ready",
             drive_id="userdrive:7", stale=0):
    row = em.TransferVariant(item_id=item_id, source_file_id="origfid",
                             variant_file_id="copyfid",
                             variant_drive_id=drive_id,
                             owner_scope=scope, owner_ref=ref,
                             status=status, size_bytes=1000,
                             stale_count=stale)
    db.add(row)
    db.commit()
    return row


def _user(user_id=7):
    return SimpleNamespace(id=user_id)


def _enable(monkeypatch):
    monkeypatch.setenv("TRANSFER_VARIANT_ENABLED", "1")


# ---------- 门禁：吃过 strike 的 Drive 行不再直接放行（同 provider） ----------

def test_gate_stale_drive_row_not_served(db, monkeypatch):
    _enable(monkeypatch)
    _mount(db)
    item = _item(db)
    _binding(db, 7, provider="drive")
    _variant(db, item.id, stale=1)
    gate = tv.ensure_user_copy_gate(db, item, _user())
    assert gate["verdict"] != "proceed"
    # 落到任务逻辑：无在途任务 → 新建一单，呈准备中（重建路）。
    assert gate["verdict"] == "preparing"
    assert gate["reason"] == "job_created"


def test_gate_fresh_drive_row_still_served(db, monkeypatch):
    _enable(monkeypatch)
    _mount(db)
    item = _item(db)
    _binding(db, 7, provider="drive")
    row = _variant(db, item.id, stale=0)
    gate = tv.ensure_user_copy_gate(db, item, _user())
    assert gate["verdict"] == "proceed"
    assert gate["reason"] == "has_ready_variant"
    assert gate["variant"].id == row.id


# ---------- 门禁：跨 provider（只绑 Drive、播 115 条目）同口径 ----------

def test_gate_cross_stale_drive_row_not_served(db, monkeypatch):
    _enable(monkeypatch)
    item = _item(db, file_path="115:/电影/a.mkv")
    _binding(db, 7, provider="drive")
    _variant(db, item.id, drive_id="userdrive:7", stale=1)
    gate = tv.ensure_user_copy_gate(db, item, _user())
    assert gate["verdict"] != "proceed"
    assert gate["verdict"] == "preparing"
    assert gate["reason"] == "job_created"


def test_gate_cross_fresh_drive_row_still_served(db, monkeypatch):
    _enable(monkeypatch)
    item = _item(db, file_path="115:/电影/a.mkv")
    _binding(db, 7, provider="drive")
    row = _variant(db, item.id, drive_id="userdrive:7", stale=0)
    gate = tv.ensure_user_copy_gate(db, item, _user())
    assert gate["verdict"] == "proceed"
    assert gate["reason"] == "has_ready_variant"
    assert gate["variant"].id == row.id


# ---------- 死副本重建：与 115 同款预算口径 ----------

def test_rebuild_dead_drive_copy_same_provider_budget(db, monkeypatch):
    _enable(monkeypatch)
    _mount(db)
    # 场景前提：属主有目标盘 active 绑定（生产调用面均先确立
    # 绑定上下文；g1 建单前置校验按此判定，见治理单回传）。
    _binding(db, 7)
    item = _item(db)
    out1 = tv.rebuild_dead_drive_copy(db, item, 7)
    assert out1["verdict"] == "preparing" and out1["reason"] == "job_rebuilt"
    job1 = db.query(em.TransferJob).filter(
        em.TransferJob.id == out1["job_id"]).first()
    assert job1.provider == "drive" and job1.status == "pending"
    # 24h 同桶 < 2 行：第二单仍可建；第三单起预算耗尽。
    out2 = tv.rebuild_dead_drive_copy(db, item, 7)
    assert out2["reason"] == "job_rebuilt"
    out3 = tv.rebuild_dead_drive_copy(db, item, 7)
    assert out3["verdict"] == "preparing" and out3["reason"] == "throttled"


def test_rebuild_dead_drive_copy_cross_provider(db, monkeypatch):
    _enable(monkeypatch)
    # 跨 provider 重建落点在用户 Drive 盘：属主须有 Drive 绑定。
    _binding(db, 7, provider="drive")
    item = _item(db, file_path="115:/电影/a.mkv")
    out = tv.rebuild_dead_drive_copy(db, item, 7)
    assert out["verdict"] == "preparing"
    assert out["reason"] == "job_rebuilt"
    job = db.query(em.TransferJob).filter(
        em.TransferJob.id == out["job_id"]).first()
    assert job.provider == "pan115" and job.status == "pending"
    assert job.dest_drive_id == "userdrive:7"
    assert tv._is_cross_job(job) is True


def test_rebuild_dead_drive_copy_unknown_provider(db, monkeypatch):
    _enable(monkeypatch)
    item = _item(db, file_path="/local/a.mkv", drive_file_id="")
    out = tv.rebuild_dead_drive_copy(db, item, 7)
    assert out["verdict"] == "failed"
    assert db.query(em.TransferJob).count() == 0


# ---------- 对账：先自愈、后 strike+重建 ----------

def test_reconcile_heals_when_reshare_ok(db, monkeypatch):
    # 实锤形态：文件本体还活着，只是公开权限被收回——重授成功即
    # healed，不记 strike、不建重建单。
    _mount(db)
    item = _item(db)
    row = _variant(db, item.id, stale=0)
    dropped = []
    monkeypatch.setattr(tv, "copy_public_cache_drop",
                        lambda uid, fid: dropped.append((uid, fid)))
    monkeypatch.setattr(tv, "ensure_user_copy_public",
                        lambda db_, uid, fid: True)
    out = tv.reconcile_dead_drive_copy(db, item, _user(), row)
    assert out["action"] == "healed"
    # 自愈前必须绕过已公开缓存（缓存可能还记着收回前的公开态）。
    assert dropped == [(7, "copyfid")]
    row = db.query(em.TransferVariant).filter(
        em.TransferVariant.id == row.id).first()
    assert row.stale_count == 0 and row.status == "ready"
    assert db.query(em.TransferJob).count() == 0


def test_reconcile_dead_file_strikes_and_rebuilds(db, monkeypatch):
    _enable(monkeypatch)
    _mount(db)
    _binding(db, 7)
    item = _item(db)
    row = _variant(db, item.id, stale=0)
    monkeypatch.setattr(tv, "copy_public_cache_drop",
                        lambda uid, fid: None)
    monkeypatch.setattr(tv, "ensure_user_copy_public",
                        lambda db_, uid, fid: False)
    out = tv.reconcile_dead_drive_copy(db, item, _user(), row)
    assert out["action"] == "rebuilding"
    row = db.query(em.TransferVariant).filter(
        em.TransferVariant.id == row.id).first()
    assert row.stale_count == 1
    jobs = db.query(em.TransferJob).all()
    assert len(jobs) == 1
    assert jobs[0].provider == "drive" and jobs[0].status == "pending"


def test_reconcile_throttled_when_budget_spent(db, monkeypatch):
    _enable(monkeypatch)
    _mount(db)
    _binding(db, 7)
    item = _item(db)
    row = _variant(db, item.id, stale=0)
    monkeypatch.setattr(tv, "copy_public_cache_drop",
                        lambda uid, fid: None)
    monkeypatch.setattr(tv, "ensure_user_copy_public",
                        lambda db_, uid, fid: False)
    # 预算先占满（24h 同桶 2 行），对账不得再建第三单。
    assert tv.rebuild_dead_drive_copy(db, item, 7)["reason"] == "job_rebuilt"
    assert tv.rebuild_dead_drive_copy(db, item, 7)["reason"] == "job_rebuilt"
    out = tv.reconcile_dead_drive_copy(db, item, _user(), row)
    assert out["action"] == "throttled"
    assert db.query(em.TransferJob).count() == 2


# ---------- video_stream：直链 404 走对账，不裸抛给用户 ----------

def _copy_variant_ns():
    return SimpleNamespace(id=9, owner_scope="user", owner_ref="7",
                           variant_drive_id="userdrive:7",
                           variant_file_id=COPY_FID, status="ready",
                           pickcode=None)


def _drive_item():
    item = em.MediaItem()
    item.guid = "item-g"
    item.id = 4242
    item.name = "测试片"
    item.file_path = "mount://3/电影/测试片.mkv"
    item.drive_file_id = SHARED_FID
    item.item_type = "movie"
    item.container = "mkv"
    item.size = 1_000_000_000
    item.duration_ticks = 36_000_000_000
    item.bitrate = 2_000_000
    return item


def _stream_setup(monkeypatch, db, *, ensure_mode, reconcile_action,
                  with_variant=True):
    """桩通 video_stream 前置链路，停在直链存在性校验处。

    ensure_mode: "always_dead" 校验恒 404；"dead_then_ok" 首次 404、
    对账自愈后复检通过。reconcile_action: 对账桩返回的 action。
    返回 (api, calls) —— calls 记录 ensure/reconcile 的调用。
    """
    from starlette.requests import Request

    from backend.emby_server import api, play_sign, playback_strategy

    calls = {"ensure": 0, "reconcile": []}
    item = _drive_item()
    variant = _copy_variant_ns() if with_variant else None

    # 内存版 CacheManager（探测/已公开缓存等都走它）。
    store = {}

    class _CM:
        @classmethod
        def get(cls, key, domain=None):
            return store.get((domain, key))

        @classmethod
        def set(cls, key, value, domain=None, ttl=None):
            store[(domain, key)] = value
            return True

        @classmethod
        def delete(cls, key, domain=None):
            store.pop((domain, key), None)
            return True

    monkeypatch.setattr("backend.database.CacheManager", _CM)
    monkeypatch.setattr(tv, "publish_drive_exists_ok",
                        lambda fid, **k: True)
    monkeypatch.setenv("TRANSFER_USERCOPY_QUOTA_PROBE_ENABLED", "1")
    monkeypatch.setenv("TRANSFER_VARIANT_ENABLED", "1")

    monkeypatch.setattr(api, "_drive_mount_id_for_item",
                        lambda db_, it: "drive-x")
    real_get = playback_strategy.store.get_value

    def fake_get(db_, key, default="", **kwargs):
        if key == playback_strategy.KEY_DRIVE_DIRECT_PLAY:
            return "true"
        if key == playback_strategy.KEY_DRIVE_RELAY:
            return "false"
        return real_get(db_, key, default, **kwargs)

    monkeypatch.setattr(playback_strategy.store, "get_value", fake_get)
    monkeypatch.setattr(api, "_require_visible_item",
                        lambda db_, u_, iid: item)
    monkeypatch.setattr(api, "ensure_playback_allowed", lambda db_, u_: None)
    monkeypatch.setattr(
        "backend.emby_server.playback_gate.ensure_binding_playback_allowed",
        lambda db_, u_, r=None: None)
    monkeypatch.setattr(
        "backend.emby_server.playback_gate._request_uses_issued_play_sign",
        lambda request, db_: False)
    monkeypatch.setattr(play_sign, "check_referer",
                        lambda request, db_: None)
    monkeypatch.setattr(api, "_enforce_stream_limit",
                        lambda db_, req, u_, it, iid: None)
    monkeypatch.setattr(api, "_observe_traffic", lambda resp: resp)
    monkeypatch.setattr(api, "_note_play_start_safe",
                        lambda item_, **kwargs: None)
    monkeypatch.setattr(api, "drive_exists_rapid_retry",
                        lambda request, user_, fid: False)
    monkeypatch.setattr(
        "backend.emby_server.drive_auth.drive_usercontent_download_url",
        lambda fid: f"https://usercontent.test/{fid}")

    if with_variant:
        monkeypatch.setattr(tv, "resolve_play_file_id",
                            lambda db_, it, u: (COPY_FID, variant))
    else:
        monkeypatch.setattr(tv, "resolve_play_file_id",
                            lambda db_, it, u: (SHARED_FID, None))
    monkeypatch.setattr(tv, "ensure_user_copy_gate",
                        lambda db_, it, u, **k: {
                            "verdict": "proceed",
                            "reason": "has_ready_variant",
                            "variant": variant})
    monkeypatch.setattr(tv, "probe_user_copy_quota",
                        lambda u, f: "ok")
    monkeypatch.setattr(tv, "usercopy_quota_probe_cached",
                        lambda u, f: False)
    monkeypatch.setattr(tv, "ready_user115_variant",
                        lambda db_, iid, uid: None)

    async def fake_ensure(db_, it, fid, **kwargs):
        calls["ensure"] += 1
        if ensure_mode == "always_dead" or (
                ensure_mode == "dead_then_ok" and calls["ensure"] == 1):
            raise HTTPException(
                404, f"媒体来源不可用：Drive 文件已不存在：{fid}")
        return True

    monkeypatch.setattr(api, "_ensure_drive_file_playable", fake_ensure)

    def fake_reconcile(db_, it, u, v):
        calls["reconcile"].append(getattr(v, "id", None))
        return {"action": reconcile_action}

    monkeypatch.setattr(tv, "reconcile_dead_drive_copy", fake_reconcile)

    scope = {"type": "http", "method": "GET",
             "path": "/emby/Videos/x/stream",
             "raw_path": b"/emby/Videos/x/stream", "query_string": b"",
             "headers": [], "scheme": "http", "server": ("testserver", 80),
             "client": ("testclient", 50000), "root_path": ""}
    return api, Request(scope), item, calls


def _run_stream(api, req, item, db):
    return asyncio.run(api.video_stream(
        item.guid, req, user=SimpleNamespace(id=7, is_staff=False), db=db))


def test_stream_drive_copy_dead_rebuilding_409(db, monkeypatch):
    api, req, item, calls = _stream_setup(
        monkeypatch, db, ensure_mode="always_dead",
        reconcile_action="rebuilding")
    with pytest.raises(HTTPException) as excinfo:
        _run_stream(api, req, item, db)
    assert excinfo.value.status_code == 409
    assert excinfo.value.detail == (
        "Drive 副本已失效，正在重新转存到你的绑定盘，转存完成后可播")
    assert calls["reconcile"] == [9]


def test_stream_drive_copy_dead_throttled_409(db, monkeypatch):
    api, req, item, calls = _stream_setup(
        monkeypatch, db, ensure_mode="always_dead",
        reconcile_action="throttled")
    with pytest.raises(HTTPException) as excinfo:
        _run_stream(api, req, item, db)
    assert excinfo.value.status_code == 409
    assert excinfo.value.detail == (
        "Drive 副本已失效，重新转存已达 24 小时上限，请稍后重试")
    assert calls["reconcile"] == [9]


def test_stream_drive_copy_healed_retries_to_302(db, monkeypatch):
    api, req, item, calls = _stream_setup(
        monkeypatch, db, ensure_mode="dead_then_ok",
        reconcile_action="healed")
    resp = _run_stream(api, req, item, db)
    assert resp.status_code == 302
    assert resp.headers["location"] == f"https://usercontent.test/{COPY_FID}"
    # 自愈后只复检一次，不循环。
    assert calls["ensure"] == 2
    assert calls["reconcile"] == [9]


def test_stream_drive_copy_healed_but_still_404_409(db, monkeypatch):
    # 自愈判 healed 但复检仍 404（谷歌权限生效延迟）：回 409 明示，
    # 不裸 404、不无限重试（ensure 恰好两次）。
    api, req, item, calls = _stream_setup(
        monkeypatch, db, ensure_mode="always_dead",
        reconcile_action="healed")
    with pytest.raises(HTTPException) as excinfo:
        _run_stream(api, req, item, db)
    assert excinfo.value.status_code == 409
    assert excinfo.value.detail == "Drive 副本权限已恢复，正在生效中，请稍后重试"
    assert calls["ensure"] == 2
    assert calls["reconcile"] == [9]


def test_stream_drive_copy_reconcile_failed_503(db, monkeypatch):
    api, req, item, calls = _stream_setup(
        monkeypatch, db, ensure_mode="always_dead",
        reconcile_action="failed")
    with pytest.raises(HTTPException) as excinfo:
        _run_stream(api, req, item, db)
    assert excinfo.value.status_code == 503
    assert calls["reconcile"] == [9]


def test_stream_shared_item_404_unchanged(db, monkeypatch):
    # 非用户副本（共享原件）的 404 行为逐字不变：不走对账、原样抛。
    api, req, item, calls = _stream_setup(
        monkeypatch, db, ensure_mode="always_dead",
        reconcile_action="rebuilding", with_variant=False)
    with pytest.raises(HTTPException) as excinfo:
        _run_stream(api, req, item, db)
    assert excinfo.value.status_code == 404
    assert calls["reconcile"] == []
