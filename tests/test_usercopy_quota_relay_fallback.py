# -*- coding: utf-8 -*-
"""用户副本额度后路（2026-10-29 用户亲令）。

公开 usercontent 直链被谷歌额度页挡住（probe 判 quota）时，先用用户
本人 Drive 绑定令牌走 API 中继取同一份副本；中继失败回现行口径。

钉死：
- video_stream：quota + 中继成功 → 出流（非 409），且中继先于 115；
  quota + 中继失败（含 403）→ 有本人 115 走 115、否则 409 +
  USERCOPY_QUOTA_DETAIL 逐字；quota + 无绑定令牌 → 中继不发起；
  开关关 → 中继不发起、行为与后路之前完全一致；
  probe ok → 原 302 路径不受扰。
- PlaybackInfo：quota + 开关开 + 有 active Drive 绑定 → 保留本站
  /stream 入口、无 ErrorCode；无绑定 / 开关关 → 剥光如旧。
- 中继腿红线（真实 serve_drive_relay_async）：access_token 腿从不调
  get_token_for_drive（SA 入口被钉成 AssertionError），上游失败只试
  一次不轮换。
全部离线。
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
from starlette.requests import Request
from starlette.responses import Response

from backend import models  # noqa: F401 — 注册表
from backend.database import Base
from backend.emby_server import api, drive_changes, drive_oauth, first_play
from backend.emby_server import play_sign, streaming
from backend.emby_server import play_variants, playback_strategy
from backend.emby_server import probe_worker, stream_accel, user_bindings
from backend.emby_server import models as em
from backend.emby_server import transfer_variant as tv

SHARED_FID = "SHAREDorigFID000000000000000000"
COPY_FID = "USERcopyFID11111111111111111111"
USER_TOKEN = "USER_TOKEN_SELF_BOUND"


@pytest.fixture()
def db():
    engine = create_engine("sqlite://",
                           connect_args={"check_same_thread": False},
                           poolclass=StaticPool)
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    try:
        yield session
    finally:
        session.rollback()
        session.close()
        engine.dispose()


@pytest.fixture(autouse=True)
def _no_cache_no_sa(monkeypatch):
    """缓存走内存字典；任何 SA 令牌获取都直接判测试失败。"""
    store = {}

    class _CM:
        @staticmethod
        def get(key, *, domain=None):
            return store.get((domain, key))

        @staticmethod
        def set(key, value, ttl=300, *, domain=None):
            store[(domain, key)] = value
            return True

        @staticmethod
        def delete(key, *, domain=None):
            store.pop((domain, key), None)
            return True

    monkeypatch.setattr("backend.database.CacheManager", _CM)

    def _sa_forbidden(*a, **k):
        raise AssertionError("用户副本后路不许取共享 SA 令牌")

    monkeypatch.setattr(drive_changes, "get_token_for_drive", _sa_forbidden)
    monkeypatch.setattr(tv, "get_token_for_drive", _sa_forbidden)
    monkeypatch.setattr(first_play, "_resolve_for_playback", _sa_forbidden)
    monkeypatch.setattr(tv, "publish_drive_exists_ok", lambda fid, **k: True)
    monkeypatch.setenv("TRANSFER_USERCOPY_QUOTA_PROBE_ENABLED", "1")
    yield store


def _copy_variant():
    return SimpleNamespace(id=9, owner_scope="user", owner_ref="7",
                           variant_drive_id="userdrive:7",
                           variant_file_id=COPY_FID, status="ready",
                           pickcode=None)


def _v115(pickcode="pc115"):
    return SimpleNamespace(id=10, owner_scope="user", owner_ref="7",
                           variant_drive_id="pan115:user-7",
                           variant_file_id="115fid", status="ready",
                           pickcode=pickcode)


def _gate_proceed(monkeypatch):
    gate = {"verdict": "proceed", "reason": "has_ready_variant",
            "variant": _copy_variant()}
    monkeypatch.setattr(tv, "ensure_user_copy_gate",
                        lambda db_, it, u, **k: dict(gate))


def _drive_item(guid="item-q"):
    item = em.MediaItem()
    item.guid = guid
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


def _user():
    return SimpleNamespace(id=7, is_staff=False)


def _token_ok(monkeypatch):
    monkeypatch.setattr(drive_oauth, "get_user_drive_access_token",
                        lambda db_, uid: USER_TOKEN)


def _token_missing(monkeypatch):
    def _raise(db_, uid):
        raise drive_oauth.DriveOAuthError(
            "not_found", "当前用户没有可用的 Drive 绑定。")
    monkeypatch.setattr(drive_oauth, "get_user_drive_access_token", _raise)


def _relay_fake(record, resp=None, exc=None):
    async def fake_relay(request, drive_id, file_id, **kwargs):
        record.update(drive_id=drive_id, file_id=file_id, kwargs=kwargs)
        if exc is not None:
            raise exc
        return resp
    return fake_relay


# ---------------------------------------------------------------------------
# video_stream
# ---------------------------------------------------------------------------

def _stream_request():
    scope = {"type": "http", "method": "GET", "path": "/emby/Videos/x/stream",
             "raw_path": b"/emby/Videos/x/stream", "query_string": b"",
             "headers": [], "scheme": "http", "server": ("testserver", 80),
             "client": ("testclient", 50000), "root_path": ""}
    return Request(scope)


def _stub_stream(monkeypatch, item, switch=None):
    monkeypatch.setattr(api, "_drive_mount_id_for_item",
                        lambda db_, it: "drive-x")
    real_get = playback_strategy.store.get_value

    def fake_get(db_, key, default="", **kwargs):
        if key == playback_strategy.KEY_DRIVE_DIRECT_PLAY:
            return "true"
        if key == playback_strategy.KEY_DRIVE_RELAY:
            return "false"
        if (switch is not None
                and key == playback_strategy
                .KEY_USERCOPY_QUOTA_RELAY_FALLBACK):
            return switch
        return real_get(db_, key, default, **kwargs)

    monkeypatch.setattr(playback_strategy.store, "get_value", fake_get)
    monkeypatch.setattr(api, "_require_visible_item",
                        lambda db_, u_, iid: item)
    monkeypatch.setattr(api, "ensure_playback_allowed", lambda db_, u_: None)
    monkeypatch.setattr(
        "backend.emby_server.playback_gate.ensure_binding_playback_allowed",
        lambda db_, u_, r=None: None)
    monkeypatch.setattr(play_sign, "check_referer", lambda request, db_: None)
    monkeypatch.setattr(api, "_enforce_stream_limit",
                        lambda db_, req, u_, it, iid: None)
    monkeypatch.setattr(api, "drive_exists_rapid_retry",
                        lambda request, user_, fid: False)
    monkeypatch.setattr(
        "backend.emby_server.drive_auth.drive_usercontent_download_url",
        lambda fid: f"https://usercontent.test/{fid}")

    async def fake_ensure(db_, it, fid, **kwargs):
        return True

    monkeypatch.setattr(api, "_ensure_drive_file_playable", fake_ensure)
    _gate_proceed(monkeypatch)


def _stream(monkeypatch, db, switch=None):
    item = _drive_item()
    _stub_stream(monkeypatch, item, switch=switch)
    return asyncio.run(api.video_stream(
        item.guid, _stream_request(), user=_user(), db=db))


def test_stream_quota_relay_success_streams(db, monkeypatch):
    monkeypatch.setattr(tv, "probe_user_copy_quota", lambda u, f: "quota")
    monkeypatch.setattr(tv, "ready_user115_variant",
                        lambda db_, iid, uid: _v115())

    def _no_115(db_, uid, pc, ua=""):
        raise AssertionError("中继成功时不该再走 115")

    monkeypatch.setattr(user_bindings, "resolve_user115_copy_url", _no_115)
    _token_ok(monkeypatch)
    record = {}
    monkeypatch.setattr(api, "serve_drive_relay_async", _relay_fake(
        record, resp=Response(b"vid", status_code=206)))
    resp = _stream(monkeypatch, db)
    assert resp.status_code == 206
    assert record["file_id"] == COPY_FID
    assert record["drive_id"] == "userdrive:7"
    assert record["kwargs"]["access_token"] == USER_TOKEN


def test_stream_quota_relay_403_falls_back_to_115(db, monkeypatch):
    monkeypatch.setattr(tv, "probe_user_copy_quota", lambda u, f: "quota")
    monkeypatch.setattr(tv, "ready_user115_variant",
                        lambda db_, iid, uid: _v115())
    monkeypatch.setattr(user_bindings, "resolve_user115_copy_url",
                        lambda db_, uid, pc, ua="": f"https://cdn.115.test/{pc}")
    _token_ok(monkeypatch)
    record = {}
    monkeypatch.setattr(api, "serve_drive_relay_async", _relay_fake(
        record, exc=HTTPException(status_code=403, detail="Forbidden")))
    resp = _stream(monkeypatch, db)
    assert resp.status_code == 302
    assert resp.headers["location"] == "https://cdn.115.test/pc115"
    assert SHARED_FID not in resp.headers["location"]
    assert record["kwargs"]["access_token"] == USER_TOKEN


def test_stream_quota_relay_403_no_115_409_verbatim(db, monkeypatch):
    monkeypatch.setattr(tv, "probe_user_copy_quota", lambda u, f: "quota")
    monkeypatch.setattr(tv, "ready_user115_variant",
                        lambda db_, iid, uid: None)
    _token_ok(monkeypatch)
    monkeypatch.setattr(api, "serve_drive_relay_async", _relay_fake(
        {}, exc=HTTPException(status_code=403, detail="Forbidden")))
    with pytest.raises(HTTPException) as excinfo:
        _stream(monkeypatch, db)
    assert excinfo.value.status_code == 409
    assert excinfo.value.detail == tv.USERCOPY_QUOTA_DETAIL


def test_stream_quota_no_token_relay_not_attempted(db, monkeypatch):
    monkeypatch.setattr(tv, "probe_user_copy_quota", lambda u, f: "quota")
    monkeypatch.setattr(tv, "ready_user115_variant",
                        lambda db_, iid, uid: None)
    _token_missing(monkeypatch)
    record = {}
    monkeypatch.setattr(api, "serve_drive_relay_async", _relay_fake(
        record, resp=Response(b"vid", status_code=206)))
    with pytest.raises(HTTPException) as excinfo:
        _stream(monkeypatch, db)
    assert excinfo.value.status_code == 409
    assert excinfo.value.detail == tv.USERCOPY_QUOTA_DETAIL
    assert record == {}


def test_stream_quota_switch_off_no_relay(db, monkeypatch):
    monkeypatch.setattr(tv, "probe_user_copy_quota", lambda u, f: "quota")
    monkeypatch.setattr(tv, "ready_user115_variant",
                        lambda db_, iid, uid: None)
    _token_ok(monkeypatch)
    record = {}
    monkeypatch.setattr(api, "serve_drive_relay_async", _relay_fake(
        record, resp=Response(b"vid", status_code=206)))
    with pytest.raises(HTTPException) as excinfo:
        _stream(monkeypatch, db, switch="false")
    assert excinfo.value.status_code == 409
    assert excinfo.value.detail == tv.USERCOPY_QUOTA_DETAIL
    assert record == {}


def test_stream_probe_ok_relay_not_touched(db, monkeypatch):
    monkeypatch.setattr(tv, "probe_user_copy_quota", lambda u, f: "ok")

    async def no_relay(*a, **k):
        raise AssertionError("probe ok 不许走中继后路")

    monkeypatch.setattr(api, "serve_drive_relay_async", no_relay)
    _token_ok(monkeypatch)
    resp = _stream(monkeypatch, db)
    assert resp.status_code == 302
    assert resp.headers["location"] == f"https://usercontent.test/{COPY_FID}"


# ---------------------------------------------------------------------------
# 中继腿红线：真实 serve_drive_relay_async + access_token
# ---------------------------------------------------------------------------

def _stub_mount_relay(monkeypatch, calls, exc=None):
    async def fake_mount(url, request, headers=None, media_type="video/mp4",
                         pt=None, sniff_quota_page=False):
        calls.append({"url": url, "headers": dict(headers or {})})
        if exc is not None:
            raise exc
        return Response(b"vid", status_code=206)
    monkeypatch.setattr(streaming, "serve_mount_relay_async", fake_mount)


def test_relay_leg_never_fetches_sa_token(db, monkeypatch):
    # 不 patch api.serve_drive_relay_async：用真实实现。autouse fixture
    # 已把 drive_changes.get_token_for_drive 钉成 AssertionError——
    # access_token 腿若取 SA 令牌，本测试立刻红。
    monkeypatch.setattr(tv, "probe_user_copy_quota", lambda u, f: "quota")
    monkeypatch.setattr(tv, "ready_user115_variant",
                        lambda db_, iid, uid: None)
    _token_ok(monkeypatch)
    calls = []
    _stub_mount_relay(monkeypatch, calls)
    resp = _stream(monkeypatch, db)
    assert resp.status_code == 206
    assert len(calls) == 1
    assert calls[0]["headers"]["Authorization"] == f"Bearer {USER_TOKEN}"
    assert COPY_FID in calls[0]["url"]
    assert SHARED_FID not in calls[0]["url"]


def test_relay_leg_upstream_403_single_attempt_then_409(db, monkeypatch):
    monkeypatch.setattr(tv, "probe_user_copy_quota", lambda u, f: "quota")
    monkeypatch.setattr(tv, "ready_user115_variant",
                        lambda db_, iid, uid: None)
    _token_ok(monkeypatch)
    calls = []
    _stub_mount_relay(monkeypatch, calls,
                      exc=HTTPException(status_code=403, detail="no"))
    with pytest.raises(HTTPException) as excinfo:
        _stream(monkeypatch, db)
    assert excinfo.value.status_code == 409
    assert excinfo.value.detail == tv.USERCOPY_QUOTA_DETAIL
    assert len(calls) == 1  # 只试一次，不轮换凭据重试


# ---------------------------------------------------------------------------
# PlaybackInfo
# ---------------------------------------------------------------------------

def _pi_request():
    scope = {"type": "http", "method": "GET",
             "path": "/Items/x/PlaybackInfo",
             "raw_path": b"/Items/x/PlaybackInfo", "query_string": b"",
             "headers": [], "scheme": "http", "server": ("testserver", 80),
             "client": ("testclient", 50000), "root_path": ""}
    return Request(scope)


def _pi(monkeypatch, db, switch=None):
    item = _drive_item("guid-pi-q")
    monkeypatch.setattr(api, "_require_visible_item",
                        lambda db_, u_, iid: item)
    monkeypatch.setattr(api, "ensure_playback_allowed", lambda db_, u_: None)
    monkeypatch.setattr(
        "backend.emby_server.playback_gate.ensure_binding_playback_allowed",
        lambda db_, u_, r=None: None)
    monkeypatch.setattr(probe_worker, "maybe_enqueue", lambda db_, it: None)
    monkeypatch.setattr(stream_accel, "rewrite_playback_urls_unified",
                        lambda db_, s, base: (s, None))
    monkeypatch.setattr(api, "_drive_mount_id_for_item",
                        lambda db_, it: "drive-x")
    monkeypatch.setattr(play_variants, "_ENABLED", False)
    real_get = playback_strategy.store.get_value

    def fake_get(db_, key, default="", **kwargs):
        if key == playback_strategy.KEY_DRIVE_DIRECT_PLAY:
            return "true"
        if key == playback_strategy.KEY_DRIVE_RELAY:
            return "false"
        if (switch is not None
                and key == playback_strategy
                .KEY_USERCOPY_QUOTA_RELAY_FALLBACK):
            return switch
        return real_get(db_, key, default, **kwargs)

    monkeypatch.setattr(playback_strategy.store, "get_value", fake_get)
    monkeypatch.setattr(tv, "ensure_user_copy_public",
                        lambda db_, uid, fid: True)
    monkeypatch.setattr(tv, "note_usercopy_issued", lambda *a, **k: None)
    _gate_proceed(monkeypatch)
    return asyncio.run(api.playback_info(
        item.guid, _pi_request(), user_id="", user=_user(), db=db))


def _assert_pi_safe(resp):
    for src in resp.get("MediaSources") or []:
        for key in ("DirectPlayUrl", "DirectStreamUrl", "TranscodingUrl",
                    "Path"):
            assert SHARED_FID not in str(src.get(key) or "")
        headers = src.get("RequiredHttpHeaders") or {}
        assert "Authorization" not in headers


def _binding_present(monkeypatch):
    monkeypatch.setattr(user_bindings, "get_active_binding",
                        lambda db_, uid, provider: SimpleNamespace(id=3))


def _binding_absent(monkeypatch):
    monkeypatch.setattr(user_bindings, "get_active_binding",
                        lambda db_, uid, provider: None)


def test_pi_quota_with_binding_keeps_stream_entry(db, monkeypatch):
    monkeypatch.setattr(tv, "probe_user_copy_quota", lambda u, f: "quota")
    monkeypatch.setattr(tv, "ready_user115_variant",
                        lambda db_, iid, uid: None)
    _binding_present(monkeypatch)
    resp = _pi(monkeypatch, db)
    _assert_pi_safe(resp)
    assert "ErrorCode" not in resp
    assert "ErrorMessage" not in resp
    src = resp["MediaSources"][0]
    # 保留的是本站 /stream 入口（客户端打进来走中继后路），不是副本直链。
    assert src.get("DirectStreamUrl")
    assert COPY_FID not in str(src.get("DirectStreamUrl") or "")


def test_pi_quota_no_binding_strips_as_before(db, monkeypatch):
    monkeypatch.setattr(tv, "probe_user_copy_quota", lambda u, f: "quota")
    monkeypatch.setattr(tv, "ready_user115_variant",
                        lambda db_, iid, uid: None)
    _binding_absent(monkeypatch)
    resp = _pi(monkeypatch, db)
    _assert_pi_safe(resp)
    assert resp["ErrorMessage"] == tv.USERCOPY_QUOTA_DETAIL
    assert resp["ErrorCode"]
    for src in resp["MediaSources"]:
        assert not src.get("DirectPlayUrl")
        assert not src.get("DirectStreamUrl")


def test_pi_quota_switch_off_with_binding_strips(db, monkeypatch):
    monkeypatch.setattr(tv, "probe_user_copy_quota", lambda u, f: "quota")
    monkeypatch.setattr(tv, "ready_user115_variant",
                        lambda db_, iid, uid: None)
    _binding_present(monkeypatch)
    resp = _pi(monkeypatch, db, switch="false")
    _assert_pi_safe(resp)
    assert resp["ErrorMessage"] == tv.USERCOPY_QUOTA_DETAIL
    for src in resp["MediaSources"]:
        assert not src.get("DirectPlayUrl")
        assert not src.get("DirectStreamUrl")
