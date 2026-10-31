"""播放修复：115 死链与 quota 结构化日志（2026-10-29 晚两起故障）。

故障 A：变体 ready 后 stream 409——最可能分支是 #1479 的用户副本
额度探测（按 user+fid 缓存 300 秒）；quota 409 此前只有
_note_play_start_safe 打点，应用日志里没有带 item/user/detail 的
结构化行。本文件钉住：quota 409 分支旁的 log_byos 行带 item、user、
fid 与缓存命中标记。

故障 B：serve_115_copy verdict=ready 后签名 stream 404——门禁 ready
只是 DB 行状态不复检文件；115 行 stale 后仍被放行，播放器跟随
302 撞已死的 115 链。本文件钉住：stale_count>=1 的 115 行不再直接
放行；302 前对 115 地址做存在性校验，死链改 409 并预算内重建；
serve_115_copy 302 分支本身补专属测试（旧欠账）。

全部离线：115 探测 HTTP、门禁、重建用桩或 sqlite 内存库；不碰真实
谷歌与真实 DB。video_stream 302 分支沿用 test_drive_relay 的桩接线。
"""
import os

os.environ.setdefault("DATABASE_TYPE", "sqlite")
os.environ.setdefault("REDIS_ENABLED", "false")

import asyncio
import logging
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend import models as base_models
from backend.emby_server import models as em
from backend.emby_server import transfer_variant as tv
from backend.emby_server import user_bindings as ub


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
    row = em.MediaItem(guid=f"pb-{_counter[0]}", library_id=1,
                       item_type="movie", name="测试片",
                       drive_file_id="origfid",
                       file_path="115:/电影/a.mkv", size=1000)
    for key, value in kw.items():
        setattr(row, key, value)
    db.add(row)
    db.commit()
    return row


def _mount(db, mount_id, mount_type, path):
    row = em.StorageMount(id=mount_id, name=f"m{mount_id}",
                          mount_type=mount_type, path=path)
    db.add(row)
    db.commit()
    return row


def _binding(db, user_id, provider="pan115", status="active"):
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
             drive_id="pan115:user-7", stale=0):
    row = em.TransferVariant(item_id=item_id, source_file_id="origfid",
                             variant_file_id="copyfid",
                             variant_drive_id=drive_id,
                             owner_scope=scope, owner_ref=ref,
                             status=status, size_bytes=1000,
                             stale_count=stale, pickcode="PC123")
    db.add(row)
    db.commit()
    return row


def _user(user_id=7):
    return SimpleNamespace(id=user_id)


def _enable(monkeypatch):
    monkeypatch.setenv("TRANSFER_VARIANT_ENABLED", "1")


# ---------- 115 地址存在性校验定性 ----------

def test_check_115_download_alive_classifies(monkeypatch):
    monkeypatch.setattr(ub, "_pan115_probe_http", lambda url: 200)
    assert ub.check_115_download_alive("https://115.example/dl") == "alive"
    monkeypatch.setattr(ub, "_pan115_probe_http", lambda url: 206)
    assert ub.check_115_download_alive("https://115.example/dl") == "alive"
    monkeypatch.setattr(ub, "_pan115_probe_http", lambda url: 404)
    assert ub.check_115_download_alive("https://115.example/dl") == "dead"
    monkeypatch.setattr(ub, "_pan115_probe_http", lambda url: 410)
    assert ub.check_115_download_alive("https://115.example/dl") == "dead"
    # 403/5xx 定性不清：不拦播放（unknown），绝不因探测抖动多造失败。
    monkeypatch.setattr(ub, "_pan115_probe_http", lambda url: 403)
    assert ub.check_115_download_alive("https://115.example/dl") == "unknown"
    monkeypatch.setattr(ub, "_pan115_probe_http", lambda url: 500)
    assert ub.check_115_download_alive("https://115.example/dl") == "unknown"

    def _boom(url):
        raise RuntimeError("net down")

    monkeypatch.setattr(ub, "_pan115_probe_http", _boom)
    assert ub.check_115_download_alive("https://115.example/dl") == "unknown"
    assert ub.check_115_download_alive("") == "unknown"


# ---------- 门禁：stale 115 行不再直接放行 ----------

def test_gate_stale_115_row_not_served(db, monkeypatch):
    _enable(monkeypatch)
    item = _item(db)
    _binding(db, 7, provider="pan115")
    _variant(db, item.id, stale=1)
    gate = tv.ensure_user_copy_gate(db, item, _user())
    assert gate["verdict"] != "serve_115_copy"
    # 落到任务逻辑：无在途任务 → 新建一单，呈准备中（重建路）。
    assert gate["verdict"] == "preparing"
    assert gate["reason"] == "job_created"


def test_gate_fresh_115_row_still_served(db, monkeypatch):
    _enable(monkeypatch)
    item = _item(db)
    _binding(db, 7, provider="pan115")
    row = _variant(db, item.id, stale=0)
    gate = tv.ensure_user_copy_gate(db, item, _user())
    assert gate["verdict"] == "serve_115_copy"
    assert gate["variant"].id == row.id


def test_gate_cross_stale_115_row_not_served(db, monkeypatch):
    # Drive 条目 + 只绑 115：跨 provider 分支，stale 的 115 行同样不放行。
    _enable(monkeypatch)
    _mount(db, 3, "drive", "drive:drive1")
    item = _item(db, file_path="mount://3/电影/a.mkv")
    _binding(db, 7, provider="pan115")
    _variant(db, item.id, drive_id="pan115:user-7", stale=1)
    gate = tv.ensure_user_copy_gate(db, item, _user())
    assert gate["verdict"] != "serve_115_copy"
    # 落到任务逻辑：无在途任务 → 新建一单，呈准备中（重建路）。
    # 2026-10-30 跨 provider 重开后与同 provider stale 口径一致；
    # stale 行不放行口径不变。
    assert gate["verdict"] == "preparing"
    assert gate["reason"] == "job_created"


def test_gate_cross_fresh_115_row_still_served(db, monkeypatch):
    _enable(monkeypatch)
    _mount(db, 3, "drive", "drive:drive1")
    item = _item(db, file_path="mount://3/电影/a.mkv")
    _binding(db, 7, provider="pan115")
    row = _variant(db, item.id, drive_id="pan115:user-7", stale=0)
    gate = tv.ensure_user_copy_gate(db, item, _user())
    assert gate["verdict"] == "serve_115_copy"
    assert gate["variant"].id == row.id


# ---------- 死链重建：与 _rebuild_done_job 同款预算口径 ----------

def test_rebuild_dead_115_same_provider_budget(db, monkeypatch):
    _enable(monkeypatch)
    # 场景前提：属主有目标盘 active 绑定（生产调用面均先确立
    # 绑定上下文；g1 建单前置校验按此判定，见治理单回传）。
    _binding(db, 7)
    item = _item(db)
    out1 = tv.rebuild_dead_user115_copy(db, item, 7)
    assert out1["verdict"] == "preparing" and out1["reason"] == "job_rebuilt"
    job1 = db.query(em.TransferJob).filter(
        em.TransferJob.id == out1["job_id"]).first()
    assert job1.provider == "pan115" and job1.status == "pending"
    # 24h 同桶 < 2 行：第二单仍可建；第三单起预算耗尽。
    out2 = tv.rebuild_dead_user115_copy(db, item, 7)
    assert out2["reason"] == "job_rebuilt"
    out3 = tv.rebuild_dead_user115_copy(db, item, 7)
    assert out3["verdict"] == "preparing" and out3["reason"] == "throttled"


def test_rebuild_dead_115_cross_provider(db, monkeypatch):
    # 2026-10-30 重开：Drive 源→用户 115 盘的重建恢复建跨单
    # （provider=源 drive、dest=pan115:user-<uid>），与同 provider
    # 115 重建（上一用例）同一套预算口径。
    _enable(monkeypatch)
    # 跨 provider 重建落点在用户 115 盘：属主须有 115 绑定。
    _binding(db, 7, provider="pan115")
    _mount(db, 3, "drive", "drive:drive1")
    item = _item(db, file_path="mount://3/电影/a.mkv")
    out = tv.rebuild_dead_user115_copy(db, item, 7)
    assert out["verdict"] == "preparing"
    assert out["reason"] == "job_rebuilt"
    job = db.query(em.TransferJob).filter(
        em.TransferJob.id == out["job_id"]).first()
    assert job.provider == "drive" and job.status == "pending"
    assert job.dest_drive_id == "pan115:user-7"
    assert tv._is_cross_job(job) is True


def test_rebuild_dead_115_unknown_provider(db, monkeypatch):
    _enable(monkeypatch)
    item = _item(db, file_path="/local/a.mkv", drive_file_id="")
    out = tv.rebuild_dead_user115_copy(db, item, 7)
    assert out["verdict"] == "failed"
    assert db.query(em.TransferJob).count() == 0


# ---------- 记 strike：与巡检同款语义 ----------

def test_mark_variant_stale_once(db):
    row = _variant(db, 1, stale=0)
    assert tv.mark_variant_stale_once(db, row.id) == 1
    row = db.query(em.TransferVariant).filter(
        em.TransferVariant.id == row.id).first()
    assert row.status == "ready"
    assert tv.mark_variant_stale_once(db, row.id) == 2
    row = db.query(em.TransferVariant).filter(
        em.TransferVariant.id == row.id).first()
    assert row.status == "stale"
    assert tv.mark_variant_stale_once(db, 999999) is None
    assert tv.mark_variant_stale_once(db, None) is None


# ---------- quota 探测缓存 peek ----------

def test_usercopy_quota_probe_cached_peek(monkeypatch):
    # 缓存里没有 → 未命中；写进去 → 命中；顺序语义由调用方保证。
    from backend.database import CacheManager

    monkeypatch.setattr(CacheManager, "get", lambda key, domain=None: None)
    assert tv.usercopy_quota_probe_cached(7, "FID1") is False
    monkeypatch.setattr(CacheManager, "get",
                        lambda key, domain=None: "quota")
    assert tv.usercopy_quota_probe_cached(7, "FID1") is True
    assert tv.usercopy_quota_probe_cached(7, "") is False


# ---------- video_stream serve_115_copy 302 分支 ----------

def _run(coro):
    return asyncio.run(coro)


class _FakeQuery:
    def filter(self, *args, **kwargs):
        return self

    def order_by(self, *args, **kwargs):
        return self

    def first(self):
        return None

    def one_or_none(self):
        return None

    def all(self):
        # 无授权行：路径授权门禁 layers 为空即放行（与真实空表一致）。
        return []


class _FakeDB:
    def query(self, *args, **kwargs):
        return _FakeQuery()


def _stream_setup(monkeypatch, gate_verdict, alive="alive"):
    """桩通 video_stream 前置链路，停在 serve_115_copy 分支门口。"""
    from starlette.requests import Request

    from backend.emby_server import api
    from backend.emby_server import playback_strategy as _strategy

    item = SimpleNamespace(id=123, drive_file_id="origfid",
                           container="mkv", file_path="115:/电影/a.mkv")
    monkeypatch.setattr(api, "_require_visible_item",
                        lambda db, user, item_id: item)
    monkeypatch.setattr(api, "ensure_playback_allowed", lambda db, user: None)
    monkeypatch.setattr(
        "backend.emby_server.playback_gate.ensure_binding_playback_allowed",
        lambda db, u, r=None: None)
    monkeypatch.setattr(
        "backend.emby_server.playback_gate._request_uses_issued_play_sign",
        lambda request, db: False)
    monkeypatch.setattr(api, "_observe_traffic", lambda resp: resp)
    monkeypatch.setattr(api.play_sign, "check_referer",
                        lambda request, db: None)
    monkeypatch.setattr(api, "_note_play_start_safe",
                        lambda item, **kwargs: None)

    def fake_get(db, key, default="", **kwargs):
        return default

    monkeypatch.setattr(_strategy.store, "get_value", fake_get)
    monkeypatch.setattr(tv, "resolve_play_file_id",
                        lambda db, item, user: ("origfid", None))
    monkeypatch.setattr(tv, "ensure_user_copy_gate",
                        lambda db, item, user, **kwargs: gate_verdict)
    monkeypatch.setattr(ub, "resolve_user115_copy_url",
                        lambda db, user_id, pickcode, ua="":
                        "https://115.example/dl")
    monkeypatch.setattr(ub, "check_115_download_alive", lambda url: alive)

    scope = {"type": "http", "method": "GET", "path": "/Videos/item/stream",
             "raw_path": b"/Videos/item/stream",
             "query_string": b"",
             "headers": [], "scheme": "http", "server": ("testserver", 80),
             "client": ("testclient", 50000), "root_path": ""}
    return api, Request(scope), SimpleNamespace(id=7), _FakeDB()


def test_video_stream_115_copy_302_alive(monkeypatch):
    api, req, user, db = _stream_setup(
        monkeypatch,
        {"verdict": "serve_115_copy",
         "variant": SimpleNamespace(pickcode="PC123", id=99)})
    resp = _run(api.video_stream("item", req, user, db))
    assert resp.status_code == 302
    assert resp.headers["location"] == "https://115.example/dl"


def test_video_stream_115_copy_dead_409_and_rebuild(monkeypatch):
    calls = {}
    monkeypatch.setattr(tv, "mark_variant_stale_once",
                        lambda db, vid: calls.setdefault("marked", vid))
    def _fake_rebuild(db, item, uid):
        calls["rebuilt"] = (item.id, uid)
        return {"verdict": "preparing", "reason": "job_rebuilt", "job_id": 5}

    monkeypatch.setattr(tv, "rebuild_dead_user115_copy", _fake_rebuild)
    api, req, user, db = _stream_setup(
        monkeypatch,
        {"verdict": "serve_115_copy",
         "variant": SimpleNamespace(pickcode="PC123", id=99)},
        alive="dead")
    with pytest.raises(HTTPException) as excinfo:
        _run(api.video_stream("item", req, user, db))
    assert excinfo.value.status_code == 409
    assert excinfo.value.detail == "115 副本已失效，正在重新转存"
    assert calls["marked"] == 99
    assert calls["rebuilt"] == (123, 7)


def test_video_stream_115_copy_unknown_probe_still_302(monkeypatch):
    # 探测定性不清（unknown）时不拦播放，沿用旧行为 302。
    api, req, user, db = _stream_setup(
        monkeypatch,
        {"verdict": "serve_115_copy",
         "variant": SimpleNamespace(pickcode="PC123", id=99)},
        alive="unknown")
    resp = _run(api.video_stream("item", req, user, db))
    assert resp.status_code == 302


def test_video_stream_quota_409_logs_structured(monkeypatch, caplog):
    """quota 409 分支旁的 log_byos 行带 item/user/fid/缓存命中标记。"""
    from backend.emby_server import api
    from backend.emby_server import playback_strategy as _strategy

    variant = SimpleNamespace(owner_scope="user",
                              variant_drive_id="userdrive:7",
                              variant_file_id="copyfid")
    item = SimpleNamespace(id=321, drive_file_id="origfid",
                           container="mkv", file_path="mount://3/电影/a.mkv")
    monkeypatch.setattr(api, "_require_visible_item",
                        lambda db, user, item_id: item)
    monkeypatch.setattr(api, "ensure_playback_allowed", lambda db, user: None)
    monkeypatch.setattr(
        "backend.emby_server.playback_gate.ensure_binding_playback_allowed",
        lambda db, u, r=None: None)
    monkeypatch.setattr(
        "backend.emby_server.playback_gate._request_uses_issued_play_sign",
        lambda request, db: False)
    monkeypatch.setattr(api, "_observe_traffic", lambda resp: resp)
    monkeypatch.setattr(api.play_sign, "check_referer",
                        lambda request, db: None)
    monkeypatch.setattr(api, "_note_play_start_safe",
                        lambda item, **kwargs: None)

    def fake_get(db, key, default="", **kwargs):
        return default

    monkeypatch.setattr(_strategy.store, "get_value", fake_get)
    monkeypatch.setattr(tv, "resolve_play_file_id",
                        lambda db, item, user: ("origfid", variant))
    monkeypatch.setattr(tv, "ensure_user_copy_gate",
                        lambda db, item, user, **kwargs: {
                            "verdict": "proceed",
                            "reason": "has_ready_variant",
                            "variant": variant})
    monkeypatch.setattr(tv, "probe_user_copy_quota", lambda u, f: "quota")
    monkeypatch.setattr(tv, "usercopy_quota_probe_cached", lambda u, f: True)
    monkeypatch.setattr(tv, "ready_user115_variant", lambda db, i, u: None)

    from starlette.requests import Request
    scope = {"type": "http", "method": "GET", "path": "/Videos/item/stream",
             "raw_path": b"/Videos/item/stream",
             "query_string": b"",
             "headers": [], "scheme": "http", "server": ("testserver", 80),
             "client": ("testclient", 50000), "root_path": ""}
    with caplog.at_level(logging.INFO):
        with pytest.raises(HTTPException) as excinfo:
            _run(api.video_stream("item", Request(scope),
                                  SimpleNamespace(id=7), _FakeDB()))
    assert excinfo.value.status_code == 409
    assert "byos_usercopy_quota" in caplog.text
    assert "item=321" in caplog.text
    assert "fid=copyfid" in caplog.text
    assert "cached=True" in caplog.text
