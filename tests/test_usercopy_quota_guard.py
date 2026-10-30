# -*- coding: utf-8 -*-
"""用户副本额度探测 + “绝不回落共享原件”护栏（2026-10-29）。

生产实证：用户副本 usercontent 直链对 ``Range: bytes=0-`` 回 200
text/html “Google Drive - Quota exceeded”，播放器拿到额度页黑屏。

钉死（用户硬底线：共享盘 SA 下载额度绝不能碰）：
- probe_user_copy_quota：额度页 → quota；206/200 视频 → ok；网络异常 /
  非 2xx → error；ok/quota 缓存，error 不缓存；开关关恒 ok；第一腿请求
  必须带 ``Range: bytes=0-`` 且不带任何凭据。整拉第二腿与其事故形态
  见 test_usercopy_probe_get_shape.py。
- video_stream：本人 Drive 副本额度满 → 有本人 115 副本走 115 现签 302，
  无 115 / 115 签发失败 → 409 明示；探测 error → 照常 302 本人副本。
  任何分支 Location 都不含共享原件 fid，且不取 SA 令牌。
- PlaybackInfo：额度满且无 115 → 清掉直链、带 ErrorCode；任何分支
  清单 URL 都不含共享原件 fid、RequiredHttpHeaders 无 Authorization，
  且不取 SA 令牌。
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

from backend import models  # noqa: F401 — 注册表
from backend.database import Base
from backend.emby_server import api, drive_changes, first_play, play_sign
from backend.emby_server import play_variants, playback_strategy
from backend.emby_server import probe_worker, stream_accel, user_bindings
from backend.emby_server import models as em
from backend.emby_server import transfer_variant as tv

SHARED_FID = "SHAREDorigFID000000000000000000"
COPY_FID = "USERcopyFID11111111111111111111"


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
        raise AssertionError("绑定用户路径不许取共享 SA 令牌")

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


# ---------------------------------------------------------------------------
# probe_user_copy_quota
# ---------------------------------------------------------------------------

QUOTA_HTML = (b"<!DOCTYPE html><html><head><title>Google Drive - Quota "
              b"exceeded</title>")


def test_probe_quota_page(monkeypatch):
    seen = {}

    def fake_http(url):
        seen["url"] = url
        return 200, "text/html; charset=utf-8", QUOTA_HTML

    monkeypatch.setattr(tv, "_usercopy_probe_http", fake_http)

    def _get_forbidden(url):
        raise AssertionError("Range 形态已判 quota，不许再跑整拉第二腿")

    monkeypatch.setattr(tv, "_usercopy_probe_http_get", _get_forbidden)
    assert tv.probe_user_copy_quota(7, COPY_FID) == "quota"
    assert COPY_FID in seen["url"] and SHARED_FID not in seen["url"]


def test_probe_video_ok_and_cached(monkeypatch):
    calls = {"n": 0}

    def fake_http(url):
        calls["n"] += 1
        return 206, "video/mp4", b"\x00\x00\x00 ftypisom"

    monkeypatch.setattr(tv, "_usercopy_probe_http", fake_http)
    monkeypatch.setattr(
        tv, "_usercopy_probe_http_get",
        lambda url: (200, "video/mp4", b"\x00\x00\x00 ftypisom"))
    assert tv.probe_user_copy_quota(7, COPY_FID) == "ok"
    assert tv.probe_user_copy_quota(7, COPY_FID) == "ok"
    assert calls["n"] == 1


def test_probe_quota_cached(monkeypatch):
    calls = {"n": 0}

    def fake_http(url):
        calls["n"] += 1
        return 200, "text/html", QUOTA_HTML

    monkeypatch.setattr(tv, "_usercopy_probe_http", fake_http)
    assert tv.probe_user_copy_quota(7, COPY_FID) == "quota"
    assert tv.probe_user_copy_quota(7, COPY_FID) == "quota"
    assert calls["n"] == 1


def test_probe_error_not_cached(monkeypatch):
    calls = {"n": 0}

    def boom(url):
        calls["n"] += 1
        raise RuntimeError("timeout")

    monkeypatch.setattr(tv, "_usercopy_probe_http", boom)
    assert tv.probe_user_copy_quota(7, COPY_FID) == "error"
    assert tv.probe_user_copy_quota(7, COPY_FID) == "error"
    assert calls["n"] == 2


def test_probe_non_2xx_is_error(monkeypatch):
    monkeypatch.setattr(tv, "_usercopy_probe_http",
                        lambda url: (404, "text/html", b"not found"))
    assert tv.probe_user_copy_quota(7, COPY_FID) == "error"


def test_probe_switch_off(monkeypatch):
    monkeypatch.setenv("TRANSFER_USERCOPY_QUOTA_PROBE_ENABLED", "0")

    def boom(url):
        raise AssertionError("开关关不许探测")

    monkeypatch.setattr(tv, "_usercopy_probe_http", boom)
    assert tv.probe_user_copy_quota(7, COPY_FID) == "ok"


def test_probe_http_uses_open_range_no_auth(monkeypatch):
    seen = {}

    class _Resp:
        status_code = 206
        headers = {"content-type": "video/mp4"}

        def iter_bytes(self):
            yield b"x" * 1024
            raise AssertionError("读够 512 字节就该停")

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def fake_stream(method, url, headers=None, **kw):
        seen["headers"] = dict(headers or {})
        return _Resp()

    monkeypatch.setattr(tv, "_shared_probe_client",
                        lambda: SimpleNamespace(stream=fake_stream))
    status, ctype, head = tv._usercopy_probe_http("https://x/u")
    assert status == 206
    assert seen["headers"] == {"Range": "bytes=0-"}


# ---------------------------------------------------------------------------
# video_stream
# ---------------------------------------------------------------------------

def _stream_request():
    scope = {"type": "http", "method": "GET", "path": "/emby/Videos/x/stream",
             "raw_path": b"/emby/Videos/x/stream", "query_string": b"",
             "headers": [], "scheme": "http", "server": ("testserver", 80),
             "client": ("testclient", 50000), "root_path": ""}
    return Request(scope)


def _stub_stream(monkeypatch, item):
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

    async def no_relay(*a, **k):
        raise AssertionError("不许走中继")

    monkeypatch.setattr(api, "serve_drive_relay_async", no_relay)
    _gate_proceed(monkeypatch)


def _stream(monkeypatch, db):
    item = _drive_item()
    _stub_stream(monkeypatch, item)
    return asyncio.run(api.video_stream(
        item.guid, _stream_request(), user=_user(), db=db))


def _assert_not_shared(location):
    assert SHARED_FID not in (location or "")


def test_stream_probe_ok_302_user_copy(db, monkeypatch):
    monkeypatch.setattr(tv, "probe_user_copy_quota", lambda u, f: "ok")
    resp = _stream(monkeypatch, db)
    assert resp.status_code == 302
    assert resp.headers["location"] == f"https://usercontent.test/{COPY_FID}"


def test_stream_probe_error_still_302_user_copy(db, monkeypatch):
    monkeypatch.setattr(tv, "probe_user_copy_quota", lambda u, f: "error")
    resp = _stream(monkeypatch, db)
    assert resp.status_code == 302
    assert resp.headers["location"] == f"https://usercontent.test/{COPY_FID}"


def test_stream_quota_with_115_copy_redirects_115(db, monkeypatch):
    monkeypatch.setattr(tv, "probe_user_copy_quota", lambda u, f: "quota")
    monkeypatch.setattr(tv, "ready_user115_variant",
                        lambda db_, iid, uid: _v115())
    monkeypatch.setattr(user_bindings, "resolve_user115_copy_url",
                        lambda db_, uid, pc: f"https://cdn.115.test/{pc}")
    resp = _stream(monkeypatch, db)
    assert resp.status_code == 302
    assert resp.headers["location"] == "https://cdn.115.test/pc115"
    _assert_not_shared(resp.headers["location"])


def test_stream_quota_no_115_is_409(db, monkeypatch):
    monkeypatch.setattr(tv, "probe_user_copy_quota", lambda u, f: "quota")
    monkeypatch.setattr(tv, "ready_user115_variant",
                        lambda db_, iid, uid: None)
    with pytest.raises(HTTPException) as excinfo:
        _stream(monkeypatch, db)
    assert excinfo.value.status_code == 409
    assert excinfo.value.detail == tv.USERCOPY_QUOTA_DETAIL


def test_stream_quota_115_no_pickcode_is_409(db, monkeypatch):
    monkeypatch.setattr(tv, "probe_user_copy_quota", lambda u, f: "quota")
    monkeypatch.setattr(tv, "ready_user115_variant",
                        lambda db_, iid, uid: _v115(pickcode=""))
    with pytest.raises(HTTPException) as excinfo:
        _stream(monkeypatch, db)
    assert excinfo.value.status_code == 409


def test_stream_quota_115_sign_fails_is_409(db, monkeypatch):
    monkeypatch.setattr(tv, "probe_user_copy_quota", lambda u, f: "quota")
    monkeypatch.setattr(tv, "ready_user115_variant",
                        lambda db_, iid, uid: _v115())

    def fail(db_, uid, pc):
        raise user_bindings.BindingError("expired", "115 登录已失效")

    monkeypatch.setattr(user_bindings, "resolve_user115_copy_url", fail)
    with pytest.raises(HTTPException) as excinfo:
        _stream(monkeypatch, db)
    assert excinfo.value.status_code == 409
    assert excinfo.value.detail == tv.USERCOPY_QUOTA_DETAIL


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


def _pi(monkeypatch, db):
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


def test_pi_probe_ok_issues_user_copy(db, monkeypatch):
    monkeypatch.setattr(tv, "probe_user_copy_quota", lambda u, f: "ok")
    resp = _pi(monkeypatch, db)
    _assert_pi_safe(resp)
    src = resp["MediaSources"][0]
    assert COPY_FID in src["DirectStreamUrl"]
    assert "ErrorCode" not in resp


def test_pi_probe_error_issues_user_copy(db, monkeypatch):
    monkeypatch.setattr(tv, "probe_user_copy_quota", lambda u, f: "error")
    resp = _pi(monkeypatch, db)
    _assert_pi_safe(resp)
    assert COPY_FID in resp["MediaSources"][0]["DirectStreamUrl"]


def test_pi_quota_no_115_errors_and_strips_urls(db, monkeypatch):
    monkeypatch.setattr(tv, "probe_user_copy_quota", lambda u, f: "quota")
    monkeypatch.setattr(tv, "ready_user115_variant",
                        lambda db_, iid, uid: None)
    resp = _pi(monkeypatch, db)
    _assert_pi_safe(resp)
    assert resp["ErrorMessage"] == tv.USERCOPY_QUOTA_DETAIL
    assert resp["ErrorCode"]
    for src in resp["MediaSources"]:
        assert not src.get("DirectPlayUrl")
        assert not src.get("DirectStreamUrl")


def test_pi_quota_with_115_keeps_stream_url(db, monkeypatch):
    monkeypatch.setattr(tv, "probe_user_copy_quota", lambda u, f: "quota")
    monkeypatch.setattr(tv, "ready_user115_variant",
                        lambda db_, iid, uid: _v115())
    resp = _pi(monkeypatch, db)
    _assert_pi_safe(resp)
    assert "ErrorCode" not in resp
    src = resp["MediaSources"][0]
    # 清单不指向用户 Drive 副本，交给本站 /stream 走 115 现签 302。
    assert COPY_FID not in str(src.get("DirectStreamUrl") or "")
