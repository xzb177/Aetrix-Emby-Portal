# -*- coding: utf-8 -*-
"""用户副本直链探测第二腿：整拉形态（2026-10-30 事故）。

生产实证（variant 627，user=1 的 173MB 副本）：同一 usercontent
直链，带 Range 分段请求两次回 206 视频，整段 GET / HEAD 却回 200
text/html「Google Drive - Quota exceeded」。播放器起播含整拉形态
请求，吃到额度页 HTML 即判死；服务端旧探测只有 Range 一条腿，
看到 206 就判 ok 发裸 302，探测口径与播放器形态脱节。

钉死：
- probe 两腿组合：Range 206 + 整拉额度页 → quota 信号（事故
  形态，detail shape=get——信号归信号，出法见下面矩阵）；
  Range 206 + 整拉扫描警告页 / 其它 HTML 插页 → interstitial；
  Range 206 + 整拉视频 → ok；整拉第二腿失败不推翻 Range 的 ok；
  Range 已判 quota 时不跑第二腿。
- 判定与明细（shape/page）同 TTL 缓存，缓存命中不重复外呼。
- 第二腿 plain GET 不带 Range、不带凭据，只读前段字节即断开。
- 出法矩阵（2026-10-30 01:40 证据修订，用户「直链优先、中继
  第二位」定版）：发直链只看 Range 腿——GET 腿信号（quota /
  interstitial）单独出现时，两判定点照常发直链、只打信号日志
  （verdict=direct）；只有 Range 腿判 quota（shape=range）才走
  本人令牌中继 → 本人 115 → 409 明示的改道链。专门钉这条规矩
  的回归测试见下方 test_rule_direct_first_relay_second_*。
- 出法矩阵修订（2026-10-31，用户亲点「起播很慢」定案，生产实证
  item 859178）：公开链已被探针证明回额度页时（Range 腿判 quota
  或 GET 腿整拉回额度页，detail page=quota），该链对播放器任何
  形态的请求都是死的——首播不再发这条直链、缓存期内不重复探，
  一律直接走本人令牌中继 → 本人 115 → 409 明示。GET 腿的
  scan / html 插页仍只是信号（不改道）。已知 quota（含缓存命中）
  的收窄口径见下方 test_rule_known_quota_*；08:29 的「判定零
  缓存」只保留给非 quota 的缓存裁决（见 decision 段）。
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
from backend.emby_server import play_sign, play_variants, playback_strategy
from backend.emby_server import probe_worker, stream_accel, user_bindings
from backend.emby_server import models as em
from backend.emby_server import transfer_variant as tv

SHARED_FID = "SHAREDorigFID000000000000000000"
COPY_FID = "USERcopyFID11111111111111111111"
USER_TOKEN = "user-token-xyz"

QUOTA_HTML = (b"<!DOCTYPE html><html><head><title>Google Drive - Quota "
              b"exceeded</title></head><body>quota</body></html>")
SCAN_HTML = (b"<!DOCTYPE html><html><head><title>Google Drive - Virus "
             b"scan warning</title></head><body>This file is too large "
             b"for Google to scan. Download anyway?</body></html>")
GENERIC_HTML = b"<!DOCTYPE html><html><head><title>Google Drive</title></head></html>"
VIDEO_HEAD = b"\x00\x00\x00 ftypisom\x00\x00\x00\x00"


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


def _gate_proceed(monkeypatch):
    gate = {"verdict": "proceed", "reason": "has_ready_variant",
            "variant": _copy_variant()}
    monkeypatch.setattr(tv, "ensure_user_copy_gate",
                        lambda db_, it, u, **k: dict(gate))


def _drive_item(guid="item-g"):
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


def _stub_legs(monkeypatch, range_resp, get_resp):
    calls = {"range": 0, "get": 0}

    def fake_range(url):
        calls["range"] += 1
        if isinstance(range_resp, Exception):
            raise range_resp
        return range_resp

    def fake_get(url):
        calls["get"] += 1
        if isinstance(get_resp, Exception):
            raise get_resp
        return get_resp

    monkeypatch.setattr(tv, "_usercopy_probe_http", fake_range)
    monkeypatch.setattr(tv, "_usercopy_probe_http_get", fake_get)
    return calls


def test_probe_range206_get_quota_is_ok_with_get_signal(monkeypatch):
    """事故形态：Range 206 视频、整拉回额度页 → probe 返 ok
    （probe 自身口径不变），GET 信号进 detail（shape=get、
    page=quota）。判定点按 detail 改道中继，见
    test_rule_known_quota_*。"""
    calls = _stub_legs(monkeypatch,
                       (206, "video/mp4", VIDEO_HEAD),
                       (200, "text/html; charset=utf-8", QUOTA_HTML))
    assert tv.probe_user_copy_quota(7, COPY_FID) == "ok"
    assert tv.usercopy_quota_probe_detail(7, COPY_FID) == {
        "shape": "get", "page": "quota"}
    assert tv.probe_user_copy_quota(7, COPY_FID) == "ok"
    assert calls == {"range": 1, "get": 1}
    assert tv.usercopy_quota_probe_cached(7, COPY_FID)


def test_probe_range206_get_scan_is_ok_with_get_signal(monkeypatch):
    _stub_legs(monkeypatch,
               (206, "video/mp4", VIDEO_HEAD),
               (200, "text/html; charset=utf-8", SCAN_HTML))
    assert tv.probe_user_copy_quota(7, COPY_FID) == "ok"
    assert tv.usercopy_quota_probe_detail(7, COPY_FID) == {
        "shape": "get", "page": "scan"}
    assert tv.usercopy_quota_probe_cached(7, COPY_FID)


def test_probe_range206_get_generic_html_is_ok_with_get_signal(monkeypatch):
    _stub_legs(monkeypatch,
               (206, "video/mp4", VIDEO_HEAD),
               (200, "text/html", GENERIC_HTML))
    assert tv.probe_user_copy_quota(7, COPY_FID) == "ok"
    assert tv.usercopy_quota_probe_detail(7, COPY_FID) == {
        "shape": "get", "page": "html"}


def test_probe_range206_get_video_is_ok_and_cached(monkeypatch):
    calls = _stub_legs(monkeypatch,
                       (206, "video/mp4", VIDEO_HEAD),
                       (200, "video/mp4", VIDEO_HEAD))
    assert tv.probe_user_copy_quota(7, COPY_FID) == "ok"
    assert tv.probe_user_copy_quota(7, COPY_FID) == "ok"
    assert calls == {"range": 1, "get": 1}


def test_probe_range_quota_short_circuits_get_leg(monkeypatch):
    calls = _stub_legs(monkeypatch,
                       (200, "text/html", QUOTA_HTML),
                       AssertionError("Range 已判 quota 不许跑第二腿"))
    assert tv.probe_user_copy_quota(7, COPY_FID) == "quota"
    assert tv.usercopy_quota_probe_detail(7, COPY_FID) == {
        "shape": "range", "page": "quota"}
    assert calls["get"] == 0


def test_probe_get_leg_failure_keeps_range_ok(monkeypatch):
    _stub_legs(monkeypatch,
               (206, "video/mp4", VIDEO_HEAD),
               RuntimeError("timeout"))
    assert tv.probe_user_copy_quota(7, COPY_FID) == "ok"


def test_probe_range200_html_scan_is_ok_with_get_signal(monkeypatch):
    """Range 形态回 200 HTML（非额度页）：第二腿分类出 scan 信号进
    detail，probe 返 ok（出法裁决口径）；旧口径连信号都没有。"""
    _stub_legs(monkeypatch,
               (200, "text/html", SCAN_HTML),
               (200, "text/html", SCAN_HTML))
    assert tv.probe_user_copy_quota(7, COPY_FID) == "ok"
    assert tv.usercopy_quota_probe_detail(7, COPY_FID) == {
        "shape": "get", "page": "scan"}


class _FakeResp:
    def __init__(self, status, ctype, chunks):
        self.status_code = status
        self.headers = {"content-type": ctype}
        self._chunks = chunks

    def iter_bytes(self):
        yield from self._chunks

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_probe_http_get_no_range_header_reads_head_only(monkeypatch):
    seen = {}

    def _chunks():
        yield b"x" * 1024
        raise AssertionError("读够 512 字节就该停")

    def fake_stream(method, url, headers=None, **kw):
        seen["method"] = method
        seen["headers"] = headers
        return _FakeResp(206, "video/mp4", _chunks())

    monkeypatch.setattr(tv, "_shared_probe_client",
                        lambda: SimpleNamespace(stream=fake_stream))
    status, ctype, head = tv._usercopy_probe_http_get("https://x/u")
    assert status == 206
    assert seen["method"] == "GET"
    assert not seen["headers"] or "Range" not in seen["headers"]
    assert len(head) == 1024


def test_probe_http_get_html_reads_up_to_4096(monkeypatch):
    def _chunks():
        yield b"a" * 2048
        yield b"b" * 2048
        yield b"c" * 2048
        raise AssertionError("读够 4096 字节就该停")

    def fake_stream(method, url, headers=None, **kw):
        return _FakeResp(200, "text/html; charset=utf-8", _chunks())

    monkeypatch.setattr(tv, "_shared_probe_client",
                        lambda: SimpleNamespace(stream=fake_stream))
    status, ctype, head = tv._usercopy_probe_http_get("https://x/u")
    assert status == 200
    assert len(head) == 4096


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


def _token_ok(monkeypatch):
    monkeypatch.setattr(drive_oauth, "get_user_drive_access_token",
                        lambda db_, uid: USER_TOKEN)


def _token_missing(monkeypatch):
    def _raise(db_, uid):
        raise drive_oauth.DriveOAuthError(
            "not_found", "当前用户没有可用的 Drive 绑定。")
    monkeypatch.setattr(drive_oauth, "get_user_drive_access_token", _raise)


def _stub_detail(monkeypatch, shape, page):
    monkeypatch.setattr(tv, "usercopy_quota_probe_detail",
                        lambda u, f: {"shape": shape, "page": page})


def test_stream_get_interstitial_issues_direct_not_relay(db, monkeypatch):
    """GET 腿 interstitial 信号（shape=get）：照常发直链，中继不许动。

    即使本人令牌可用也一样——插页信号不改道（规矩全程版见
    test_rule_direct_first_relay_second_stream_get_scan_*，这里是
    桩判定版）。"""
    monkeypatch.setattr(tv, "probe_user_copy_quota",
                        lambda u, f: "interstitial")
    _stub_detail(monkeypatch, "get", "scan")
    monkeypatch.setattr(tv, "ready_user115_variant",
                        lambda db_, iid, uid: None)
    _token_ok(monkeypatch)
    record = {}

    async def fake_relay(request, drive_id, file_id, **kwargs):
        record.update(drive_id=drive_id, file_id=file_id, kwargs=kwargs)
        return Response(b"vid", status_code=206)

    item = _drive_item()
    _stub_stream(monkeypatch, item)
    monkeypatch.setattr(api, "serve_drive_relay_async", fake_relay)
    resp = asyncio.run(api.video_stream(
        item.guid, _stream_request(), user=_user(), db=db))
    assert resp.status_code == 302
    assert resp.headers["location"] == f"https://usercontent.test/{COPY_FID}"
    assert record == {}


def test_stream_get_interstitial_no_115_still_direct(db, monkeypatch):
    """GET 腿 interstitial 信号 + 无令牌 + 无 115：仍然直链，不许 409。"""
    monkeypatch.setattr(tv, "probe_user_copy_quota",
                        lambda u, f: "interstitial")
    _stub_detail(monkeypatch, "get", "scan")
    monkeypatch.setattr(tv, "ready_user115_variant",
                        lambda db_, iid, uid: None)
    _token_missing(monkeypatch)
    resp = _stream(monkeypatch, db)
    assert resp.status_code == 302
    assert resp.headers["location"] == f"https://usercontent.test/{COPY_FID}"


def test_stream_get_interstitial_with_115_still_direct(db, monkeypatch):
    """GET 腿 interstitial 信号 + 有本人 115 副本：直链优先，不许跳 115。"""
    monkeypatch.setattr(tv, "probe_user_copy_quota",
                        lambda u, f: "interstitial")
    _stub_detail(monkeypatch, "get", "scan")
    v115 = SimpleNamespace(id=10, owner_scope="user", owner_ref="7",
                           variant_drive_id="pan115:user-7",
                           variant_file_id="115fid", status="ready",
                           pickcode="pc115")
    monkeypatch.setattr(tv, "ready_user115_variant",
                        lambda db_, iid, uid: v115)
    monkeypatch.setattr(user_bindings, "resolve_user115_copy_url",
                        lambda db_, uid, pc: f"https://cdn.115.test/{pc}")
    _token_missing(monkeypatch)
    resp = _stream(monkeypatch, db)
    assert resp.status_code == 302
    assert resp.headers["location"] == f"https://usercontent.test/{COPY_FID}"
    assert SHARED_FID not in resp.headers["location"]


def _pi_request():
    scope = {"type": "http", "method": "GET",
             "path": "/Items/x/PlaybackInfo",
             "raw_path": b"/Items/x/PlaybackInfo", "query_string": b"",
             "headers": [], "scheme": "http", "server": ("testserver", 80),
             "client": ("testclient", 50000), "root_path": ""}
    return Request(scope)


def _pi(monkeypatch, db):
    item = _drive_item("guid-pi-g")
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


def test_pi_get_interstitial_keeps_direct_manifest(db, monkeypatch):
    """GET 腿 interstitial 信号（shape=get）：清单直链保留、不报错。"""
    monkeypatch.setattr(tv, "probe_user_copy_quota",
                        lambda u, f: "interstitial")
    _stub_detail(monkeypatch, "get", "scan")
    monkeypatch.setattr(tv, "ready_user115_variant",
                        lambda db_, iid, uid: None)
    resp = _pi(monkeypatch, db)
    assert not resp.get("ErrorMessage")
    urls = [(src.get("DirectPlayUrl") or "")
            + (src.get("DirectStreamUrl") or "")
            for src in resp["MediaSources"]]
    assert any(COPY_FID in u for u in urls), urls
    assert all(SHARED_FID not in u for u in urls)


# ------------------------------------------------------------------
# 规矩兜底（2026-10-30 01:40 用户亲点立规矩，2026-10-31 修订）：
# 直链优先、中继第二位——Range 腿与 GET 腿都健康时，两个判定点
# 的最终出法必须是直链；GET 腿的 scan / html 插页信号仍只是
# 信号，绝不许触发中继改道、绝不许清 PlaybackInfo 清单。
# 2026-10-31 修订（用户亲点，生产实证 item 859178）：GET 腿整拉
# 回额度页（page=quota）不再是「只是信号」——公开链已被证明对
# 播放器的 GET 形首请求回额度页，首播发出去就是约 45 秒黑屏；
# page=quota（无论 Range 腿还是 GET 腿探到）一律改道中继，且
# 已知 quota（含缓存命中）在缓存期内不重复探、任何形态的流
# 请求直接中继。谁改判定，先红这一组。
# 本组全部走真实 probe（只桩两条 HTTP 腿），不桩判定本身。
# ------------------------------------------------------------------

def _record_byos(monkeypatch):
    events = []
    monkeypatch.setattr(tv, "log_byos",
                        lambda event, **kw: events.append((event, kw)))
    return events


def test_rule_known_quota_stream_get_quota_diverts_to_relay(
        db, monkeypatch):
    """2026-10-31 修订：Range 206 视频 + 整拉额度页 → 改道中继。

    probe 仍返 ok（probe 自身口径不变），但 GET 腿已证明公开链对
    整拉形态回额度页（detail shape=get、page=quota）：video_stream
    不许再把这条链发给客户端——本人令牌中继出流（206）；判定
    日志 verdict=quota、shape=get、page=quota。
    """
    _stub_legs(monkeypatch,
               (206, "video/mp4", VIDEO_HEAD),
               (200, "text/html; charset=utf-8", QUOTA_HTML))
    _token_ok(monkeypatch)
    record = {}

    async def fake_relay(request, drive_id, file_id, **kwargs):
        record.update(file_id=file_id, kwargs=kwargs)
        return Response(b"vid", status_code=206)

    item = _drive_item()
    _stub_stream(monkeypatch, item)
    monkeypatch.setattr(api, "serve_drive_relay_async", fake_relay)
    events = _record_byos(monkeypatch)
    resp = asyncio.run(api.video_stream(
        item.guid, _stream_request(), user=_user(), db=db))
    assert resp.status_code == 206
    assert record["file_id"] == COPY_FID
    assert record["kwargs"]["access_token"] == USER_TOKEN
    sig = [kw for ev, kw in events if ev == "byos_usercopy_quota"]
    assert sig and sig[0]["verdict"] == "quota"
    assert sig[0]["shape"] == "get" and sig[0]["page"] == "quota"


def test_rule_direct_first_relay_second_stream_get_scan_keeps_direct(
        db, monkeypatch):
    """GET 腿扫描页（interstitial 信号）同样不许改道：直链 302。"""
    _stub_legs(monkeypatch,
               (206, "video/mp4", VIDEO_HEAD),
               (200, "text/html; charset=utf-8", SCAN_HTML))
    events = _record_byos(monkeypatch)
    resp = _stream(monkeypatch, db)
    assert resp.status_code == 302
    assert resp.headers["location"] == f"https://usercontent.test/{COPY_FID}"
    sig = [kw for ev, kw in events if ev == "byos_usercopy_interstitial"]
    assert sig and sig[0]["verdict"] == "direct"
    assert sig[0]["shape"] == "get" and sig[0]["page"] == "scan"


def test_rule_known_quota_pi_get_quota_no_direct_manifest(
        db, monkeypatch):
    """2026-10-31 修订：PlaybackInfo GET 腿额度页 → 不发直链清单。

    无本人 115 副本、无可保留的 /stream 后路（测试用户无 active
    Drive 绑定行）时，清单里的副本直链必须被清、ErrorMessage 为
    额度明示文案；判定日志 verdict=quota、shape=get、page=quota。
    """
    _stub_legs(monkeypatch,
               (206, "video/mp4", VIDEO_HEAD),
               (200, "text/html; charset=utf-8", QUOTA_HTML))
    monkeypatch.setattr(tv, "ready_user115_variant",
                        lambda db_, iid, uid: None)
    events = _record_byos(monkeypatch)
    resp = _pi(monkeypatch, db)
    assert resp.get("ErrorMessage") == tv.USERCOPY_QUOTA_DETAIL
    urls = [(src.get("DirectPlayUrl") or "")
            + (src.get("DirectStreamUrl") or "")
            for src in resp["MediaSources"]]
    assert all(COPY_FID not in u for u in urls), urls
    assert all(SHARED_FID not in u for u in urls)
    sig = [kw for ev, kw in events if ev == "byos_usercopy_quota"]
    assert sig and sig[0]["verdict"] == "quota"
    assert sig[0]["shape"] == "get" and sig[0]["page"] == "quota"


def test_rule_direct_first_relay_second_stream_range_quota_diverts(
        db, monkeypatch):
    """规矩的另一半：Range 腿本身判 quota（shape=range）才轮到中继。

    本人令牌中继成功 → 出中继流（206）；日志 verdict=quota、
    shape=range；GET 腿不许被跑（Range 已判死，短路）。
    """
    calls = _stub_legs(monkeypatch,
                       (200, "text/html; charset=utf-8", QUOTA_HTML),
                       AssertionError("Range 已判 quota 不许跑第二腿"))
    _token_ok(monkeypatch)
    record = {}

    async def fake_relay(request, drive_id, file_id, **kwargs):
        record.update(file_id=file_id, kwargs=kwargs)
        return Response(b"vid", status_code=206)

    item = _drive_item()
    _stub_stream(monkeypatch, item)
    monkeypatch.setattr(api, "serve_drive_relay_async", fake_relay)
    events = _record_byos(monkeypatch)
    resp = asyncio.run(api.video_stream(
        item.guid, _stream_request(), user=_user(), db=db))
    assert resp.status_code == 206
    assert record["file_id"] == COPY_FID
    assert record["kwargs"]["access_token"] == USER_TOKEN
    assert calls["get"] == 0
    sig = [kw for ev, kw in events if ev == "byos_usercopy_quota"]
    assert sig and sig[0]["verdict"] == "quota"
    assert sig[0]["shape"] == "range"


# ---------- 改道判定与缓存（08:29 事故兜底 + 2026-10-31 收窄） ----------
# 08:29 事故：user 1 播《大唐妖探》（本人副本 fid 当场 Range 实测
# 已回 206 真字节），日志却连续 byos_usercopy_quota verdict=quota
# cached=True——几分钟前间歇额度页留下的缓存裁决（TTL 300 秒）被
# 两判定点反复当成实时裁决改道中继，播放器 8 次重连播不出。当时
# 的修法是判定零缓存、每请求新鲜两腿。
# 2026-10-31 收窄（用户亲点，生产实证 item 859178）：额度是持续
# 状态时，每请求新鲜两腿让每个分段请求先白付一发注定 quota 的
# 探测（约 0.85 秒）才轮到中继，起播约 45 秒。现口径：已知
# quota（缓存裁决 quota，或缓存 detail 的 page=quota）→ 判定
# 直接吃缓存、零外呼，判定点据此直接中继；缓存到期后下一请求
# 才重新探一次，谷歌解除额度即可恢复直链。非 quota 的缓存
# （ok 且无 quota 信号 / interstitial）仍沿 08:29 口径：判定先
# 失效再新鲜探测——08:29 防的「间歇页残留裁决」只在 quota 方向
# 被本次定案有意让位，其余方向不变。

def _seed_verdict_cache(verdict, detail="range:quota"):
    from backend.database import CacheManager
    CacheManager.set(tv._usercopy_quota_cache_key(7, COPY_FID),
                     verdict, 300, domain="playback")
    CacheManager.set(tv._usercopy_quota_detail_key(7, COPY_FID),
                     detail, 300, domain="playback")


def test_rule_decision_honors_cached_quota_no_reprobe(monkeypatch):
    """缓存预置 quota 裁决 → 判定直接吃缓存、两腿零外呼。

    两腿桩成一调就炸：判定仍返回 quota 且缓存/detail 原样保留
    （判定点随后按 detail=range/quota 改道，日志 cached=True
    名副其实）。这正是 2026-10-31 定案要的：已知 quota 不重复探。
    """
    _seed_verdict_cache("quota")
    calls = _stub_legs(monkeypatch,
                       AssertionError("已知 quota 不许再探 Range 腿"),
                       AssertionError("已知 quota 不许再探 GET 腿"))
    assert tv.probe_user_copy_quota_decision(7, COPY_FID) == "quota"
    assert calls == {"range": 0, "get": 0}
    assert tv.usercopy_quota_probe_detail(7, COPY_FID) == {
        "shape": "range", "page": "quota"}
    # 非判定调用同样吃缓存（复用口径保留）。
    assert tv.probe_user_copy_quota(7, COPY_FID) == "quota"
    assert calls == {"range": 0, "get": 0}


def test_rule_decision_honors_cached_get_quota_signal_no_reprobe(
        monkeypatch):
    """缓存是 GET 腿额度页信号（verdict=ok + detail get/quota）：
    同样算已知 quota——判定零外呼、detail 原样保留，判定点按
    detail 改道。否则 Range 健康但整拉持续回额度页的副本，每个
    分段请求仍要白付两腿探测才轮到中继（variant 627 形态）。"""
    _seed_verdict_cache("ok", "get:quota")
    calls = _stub_legs(monkeypatch,
                       AssertionError("已知 quota 不许再探 Range 腿"),
                       AssertionError("已知 quota 不许再探 GET 腿"))
    assert tv.probe_user_copy_quota_decision(7, COPY_FID) == "ok"
    assert calls == {"range": 0, "get": 0}
    assert tv.usercopy_quota_probe_detail(7, COPY_FID) == {
        "shape": "get", "page": "quota"}


def test_rule_decision_reprobes_once_after_cache_expiry(monkeypatch):
    """缓存到期（键消失）后判定重新探一次：谷歌已解除额度
    （两腿回视频）→ 判定 ok、新裁决回写缓存，直链自动恢复。"""
    _seed_verdict_cache("quota")
    from backend.database import CacheManager
    CacheManager.delete(tv._usercopy_quota_cache_key(7, COPY_FID),
                        domain="playback")
    CacheManager.delete(tv._usercopy_quota_detail_key(7, COPY_FID),
                        domain="playback")
    calls = _stub_legs(monkeypatch,
                       (206, "video/mp4", VIDEO_HEAD),
                       (200, "video/mp4", VIDEO_HEAD))
    assert tv.probe_user_copy_quota_decision(7, COPY_FID) == "ok"
    assert calls == {"range": 1, "get": 1}
    assert tv.probe_user_copy_quota(7, COPY_FID) == "ok"
    assert calls == {"range": 1, "get": 1}


def test_rule_decision_fresh_probe_range_quota_still_diverts(monkeypatch):
    """新鲜 Range 回额度页 → fresh 判定才返回 quota（改道成立）。

    缓存里预置 ok 也盖不过去：判定当场 Range 判 quota、短路不跑
    第二腿、detail 同步刷新为 range/quota，判定点据此改道。
    """
    _seed_verdict_cache("ok", "get:")
    calls = _stub_legs(
        monkeypatch,
        (200, "text/html; charset=utf-8", QUOTA_HTML),
        AssertionError("Range 已判 quota 不许跑第二腿"))
    assert tv.probe_user_copy_quota_decision(7, COPY_FID) == "quota"
    assert calls == {"range": 1, "get": 0}
    assert tv.usercopy_quota_probe_detail(7, COPY_FID) == {
        "shape": "range", "page": "quota"}


# ---------- 已知 quota 全程（2026-10-31 生产实证 item 859178） ----------
# 缓存里已有 quota 裁决时，后续每个分段流请求必须零探测直接
# 中继——旧行为是判定先失效缓存再新鲜两腿，每请求白付约 0.85 秒
# 注定 quota 的探测、ttfb 0.9–1.7 秒。两腿桩成一调就炸：还探
# 就是红。

def test_rule_known_quota_stream_cached_quota_relays_without_probing(
        db, monkeypatch):
    """video_stream：缓存 quota 在效 → 不探、直接本人令牌中继。"""
    _seed_verdict_cache("quota")
    calls = _stub_legs(monkeypatch,
                       AssertionError("已知 quota 不许再探 Range 腿"),
                       AssertionError("已知 quota 不许再探 GET 腿"))
    _token_ok(monkeypatch)
    record = {}

    async def fake_relay(request, drive_id, file_id, **kwargs):
        record.update(file_id=file_id, kwargs=kwargs)
        return Response(b"vid", status_code=206)

    item = _drive_item()
    _stub_stream(monkeypatch, item)
    monkeypatch.setattr(api, "serve_drive_relay_async", fake_relay)
    events = _record_byos(monkeypatch)
    resp = asyncio.run(api.video_stream(
        item.guid, _stream_request(), user=_user(), db=db))
    assert resp.status_code == 206
    assert record["file_id"] == COPY_FID
    assert record["kwargs"]["access_token"] == USER_TOKEN
    assert calls == {"range": 0, "get": 0}
    sig = [kw for ev, kw in events if ev == "byos_usercopy_quota"]
    assert sig and sig[0]["verdict"] == "quota"
    assert sig[0]["cached"] is True


def test_rule_known_quota_pi_cached_quota_no_direct_manifest(
        db, monkeypatch):
    """PlaybackInfo：缓存 quota 在效 → 不探、不发副本直链清单。"""
    _seed_verdict_cache("quota")
    calls = _stub_legs(monkeypatch,
                       AssertionError("已知 quota 不许再探 Range 腿"),
                       AssertionError("已知 quota 不许再探 GET 腿"))
    monkeypatch.setattr(tv, "ready_user115_variant",
                        lambda db_, iid, uid: None)
    events = _record_byos(monkeypatch)
    resp = _pi(monkeypatch, db)
    assert resp.get("ErrorMessage") == tv.USERCOPY_QUOTA_DETAIL
    urls = [(src.get("DirectPlayUrl") or "")
            + (src.get("DirectStreamUrl") or "")
            for src in resp["MediaSources"]]
    assert all(COPY_FID not in u for u in urls), urls
    assert all(SHARED_FID not in u for u in urls)
    assert calls == {"range": 0, "get": 0}
    sig = [kw for ev, kw in events if ev == "byos_usercopy_quota"]
    assert sig and sig[0]["verdict"] == "quota"
    assert sig[0]["cached"] is True
