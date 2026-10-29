# -*- coding: utf-8 -*-
"""用户副本直链 confirm=t 契约（2026-10-30 紧急修复，回归兜底）。

生产实证（2026-10-30 01:11 用户亲点）：用户副本《王爷遭朝廷谋杀》
（item 753935，variant 627，fid 1wEmQDpwZK2M9VcCEoEsrC1Z_sPQrr0y2，
173,736,122 字节）转存 job 四秒 done、门禁 proceed、stream 发 302，
但无令牌实拉该 fid 的 usercontent 直链回 HTTP 200 + text/html，
页面标题「Google Drive - Virus scan warning」——大文件触发谷歌
病毒扫描确认页，播放器拿到网页当然播不了；同 URL 追加 confirm=t
后实测 HTTP 206 + video/mp4 真字节。凡走 usercontent 直链的用户
副本、大到触发确认页的全部播不了，是通病不是个例。

钉死口径：
- drive_usercontent_download_url 是全站 usercontent 地址的唯一
  构造点，输出恒带、且只带一次 confirm=t（小文件谷歌忽略该
  参数，无条件携带；构造点已有处理，下发点不许重复拼）。
- 每个客户端下发点都只许经构造点出地址，逐个行为级钉死：
  PlaybackInfo 用户副本直链已由 tests/test_usercopy_direct.py
  精确钉死（本文件不重复）；本文件补齐其余各点——video_stream
  直链优先 302、video_stream 通用 302、/Items/{id}/Download、
  /Items/{id}/File。此前变体切换/直链测试把构造点桩掉了，这些
  302 的 Location 从没被真实输出验过，本文件补上。
- 额度探测与播放同口径：probe_user_copy_quota 请求的地址与
  下发地址逐字一致（同出构造点、自带 confirm=t）。
  is_quota_page_bytes 语义不动：额度页（200/html/quota 标记）
  仍判 quota；病毒扫描警告页（200/html、无 quota 标记）不判
  quota——两页靠标记区分。探测不负责识别警告页（警告页漏过
  探测只会按状态判 ok），防线在下发侧 confirm=t，这正是
  confirm=t 必须在构造点钉死、不许靠探测兜底的原因。
- drive_api_media_url 把 usercontent 转成 API alt=media 形态
  时不携带 confirm（API 形态凭 Bearer 取流、没有确认页）——
  这是设计，不许「顺手」把 confirm 拼进 API 地址。

全部离线：网络/DB/鉴权全桩，monkeypatch 只打类/模块属性。
"""
import os

os.environ.setdefault("DATABASE_TYPE", "sqlite")
os.environ.setdefault("REDIS_ENABLED", "false")

import asyncio
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from starlette.requests import Request

from backend import models
from backend.emby_server import api, drive_auth, media_routes, mount_routes
from backend.emby_server import models as em
from backend.emby_server import play_events, play_sign, playback_strategy
from backend.emby_server import transfer_variant as tv
from backend.emby_server.mounts import PlayTarget

# 生产实证的那条副本 fid（只是文件 id，不是凭据）。
FID = "1wEmQDpwZK2M9VcCEoEsrC1Z_sPQrr0y2"

QUOTA_HTML = (b"<!DOCTYPE html><html><head><title>Google Drive - Quota "
              b"exceeded</title>")
VIRUS_HTML = (b"<!DOCTYPE html><html><head><title>Google Drive - Virus "
              b"scan warning</title></head><body>Google Drive can't "
              b"scan this file for viruses.</body></html>")


def _request(query=b""):
    scope = {"type": "http", "method": "GET", "path": "/Videos/item/stream",
             "raw_path": b"/Videos/item/stream", "query_string": query,
             "headers": [], "scheme": "http", "server": ("testserver", 80),
             "client": ("testclient", 50000), "root_path": ""}
    return Request(scope)


# ---------------------------------------------------------------------------
# 构造点本体
# ---------------------------------------------------------------------------

def test_builder_exact_form_single_confirm():
    url = drive_auth.drive_usercontent_download_url(FID)
    assert url == ("https://drive.usercontent.google.com/download?id="
                   + FID + "&export=download&confirm=t")
    assert url.count("confirm=t") == 1


def test_builder_empty_fid_returns_empty():
    assert drive_auth.drive_usercontent_download_url("") == ""
    assert drive_auth.drive_usercontent_download_url(None) == ""


def test_api_media_conversion_drops_confirm_by_design():
    """转 alt=media（中继/预热凭 Bearer 取流）不带 confirm：设计如此。"""
    url = drive_auth.drive_api_media_url(
        drive_auth.drive_usercontent_download_url(FID))
    assert url == ("https://www.googleapis.com/drive/v3/files/" + FID
                   + "?alt=media&supportsAllDrives=true")
    assert "confirm" not in url


# ---------------------------------------------------------------------------
# video_stream 通用 302 分支（api.py drive_switch_on）
# ---------------------------------------------------------------------------

def test_video_stream_generic_302_has_confirm_t(monkeypatch):
    item = SimpleNamespace(container="mkv", drive_file_id=FID)
    target = PlayTarget(
        "url", f"https://drive.google.com/uc?export=download&id={FID}",
        {"Authorization": "Bearer <redacted>"})
    monkeypatch.setattr(api, "_require_item", lambda db, item_id: item)
    monkeypatch.setattr(api, "_library_scope", lambda db_, user_: None)
    monkeypatch.setattr(api, "ensure_playback_allowed",
                        lambda db, user: None)
    monkeypatch.setattr(
        "backend.emby_server.playback_gate.ensure_binding_playback_allowed",
        lambda db, u, r=None: None)
    monkeypatch.setattr(api, "_play_target", lambda db, it: target)
    # 开关走真实 reader：假 db 读失败 → fail-safe 默认开（与
    # test_drive_direct_play.py 同口径）。
    resp = asyncio.run(api.video_stream("item", _request(), object(),
                                        object()))
    assert resp.status_code == 302
    loc = resp.headers["location"]
    assert loc == drive_auth.drive_usercontent_download_url(FID)
    assert "confirm=t" in loc


# ---------------------------------------------------------------------------
# video_stream 直链优先 302 分支（api.py _fast302 / drive_direct 变体）
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _isolate_play_events():
    play_events.reset_for_tests()
    yield
    play_events.reset_for_tests()


@pytest.fixture()
def db_factory(monkeypatch):
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False},
        poolclass=StaticPool)
    models.Base.metadata.create_all(engine)
    em.Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    monkeypatch.setattr("backend.database.SessionLocal", factory)
    return factory


@pytest.fixture()
def db(db_factory):
    session = db_factory()
    yield session
    session.close()


def test_video_stream_fast302_has_confirm_t(monkeypatch, db):
    """直链优先选中 drive_direct → _ensure 通过 → 302 Location 必带
    confirm=t。注意：不桩 drive_usercontent_download_url——验的
    就是构造点真实输出（变体切换旧测试把它桩掉了，本条补上）。"""
    item = em.MediaItem()
    item.guid = "confirm-t-fast302"
    item.name = "测试片"
    item.file_path = "mount://3/电影/测试片.mkv"
    item.drive_file_id = FID
    item.item_type = "movie"
    item.container = "mkv"
    item.size = 173_736_122

    monkeypatch.setattr(api, "_drive_mount_id_for_item",
                        lambda db_, it: "drive-x")
    real_get = playback_strategy.store.get_value

    def fake_get(db_, key, default="", **kwargs):
        if key == playback_strategy.KEY_DRIVE_DIRECT_PLAY:
            return "true"
        if key == playback_strategy.KEY_DRIVE_RELAY:
            return "true"
        return real_get(db_, key, default, **kwargs)

    monkeypatch.setattr(playback_strategy.store, "get_value", fake_get)
    monkeypatch.setattr(api, "_require_visible_item",
                        lambda db_, u_, iid: item)
    monkeypatch.setattr(api, "ensure_playback_allowed",
                        lambda db_, u_: None)
    monkeypatch.setattr(
        "backend.emby_server.playback_gate.ensure_binding_playback_allowed",
        lambda db_, u_, r=None: None)
    monkeypatch.setattr(play_sign, "check_referer",
                        lambda request, db_: None)
    monkeypatch.setattr(api, "_enforce_stream_limit",
                        lambda db_, req, u_, it, iid: None)

    async def fake_relay(request, drive_id, fid, **kwargs):
        raise AssertionError("直链优先命中时不许试中继")

    async def fake_ensure(db_, it, fid, **kwargs):
        return True

    monkeypatch.setattr(api, "serve_drive_relay_async", fake_relay)
    monkeypatch.setattr(api, "_ensure_drive_file_playable", fake_ensure)
    monkeypatch.setattr(api, "drive_exists_rapid_retry",
                        lambda request, user_, fid: False)

    user = SimpleNamespace(id=101, is_staff=False)
    resp = asyncio.run(api.video_stream(item.guid, _request(), user=user,
                                        db=db))
    assert resp.status_code == 302
    loc = resp.headers["location"]
    assert loc == drive_auth.drive_usercontent_download_url(FID)
    assert "confirm=t" in loc


# ---------------------------------------------------------------------------
# /Items/{id}/Download 与 /Items/{id}/File 两口
# ---------------------------------------------------------------------------

def test_download_item_302_has_confirm_t(monkeypatch):
    item = SimpleNamespace(drive_file_id=FID)
    target = PlayTarget("url", "https://example.invalid/placeholder", {})
    monkeypatch.setattr(media_routes, "_require_visible_item",
                        lambda db, user, iid: item)
    monkeypatch.setattr(media_routes, "ensure_playback_allowed",
                        lambda db, user: None)
    monkeypatch.setattr(media_routes, "ensure_download_allowed",
                        lambda db, user, request=None: None)
    monkeypatch.setattr(media_routes, "_play_target",
                        lambda db, it: target)
    resp = media_routes.download_item(
        "item", _request(), user=SimpleNamespace(id=1), db=object())
    assert resp.status_code == 302
    loc = resp.headers["location"]
    assert loc == drive_auth.drive_usercontent_download_url(FID)
    assert "confirm=t" in loc


def test_mounted_item_file_302_has_confirm_t(monkeypatch):
    item = SimpleNamespace(drive_file_id=FID)
    target = PlayTarget("url", "https://example.invalid/placeholder", {})
    monkeypatch.setattr(mount_routes, "_require_visible_item",
                        lambda db, user, iid: item)
    monkeypatch.setattr(mount_routes, "ensure_playback_allowed",
                        lambda db, user: None)
    monkeypatch.setattr(mount_routes, "_play_target",
                        lambda db, it: target)
    resp = mount_routes.mounted_item_file(
        "item", _request(), user=SimpleNamespace(id=1), db=object())
    assert resp.status_code == 302
    loc = resp.headers["location"]
    assert loc == drive_auth.drive_usercontent_download_url(FID)
    assert "confirm=t" in loc


# ---------------------------------------------------------------------------
# 额度探测：与播放同口径 + 两页区分（is_quota_page_bytes 语义不动）
# ---------------------------------------------------------------------------

def test_probe_url_matches_issued_url(monkeypatch):
    """探测打的地址与下发给播放器的地址逐字一致（同出构造点）。"""
    seen = {}

    def fake_http(url):
        seen["url"] = url
        return 206, "video/mp4", b"\x00\x00\x00\x18ftypisom"

    monkeypatch.setattr(tv, "_usercopy_probe_http", fake_http)
    # 专用 user id 避开探测结果缓存与其它用例串扰。
    assert tv.probe_user_copy_quota(7919, FID) == "ok"
    assert seen["url"] == drive_auth.drive_usercontent_download_url(FID)
    assert "confirm=t" in seen["url"]


def test_probe_quota_page_still_quota_on_confirm_url(monkeypatch):
    """confirm=t 不改变额度语义：谷歌额度满时带 confirm 仍回额度页，
    探测照样判 quota（桩测钉死这层关系）。"""

    def fake_http(url):
        assert "confirm=t" in url
        return 200, "text/html; charset=utf-8", QUOTA_HTML

    monkeypatch.setattr(tv, "_usercopy_probe_http", fake_http)
    assert tv.probe_user_copy_quota(7920, FID) == "quota"


def test_virus_scan_page_is_not_quota_page():
    """两页区分：病毒扫描警告页不是额度页，不许误判 quota（否则
    大文件会被当额度满误拦）；额度页照常判 quota；视频字节不判。"""
    assert tv.is_quota_page_bytes(
        200, "text/html; charset=utf-8", VIRUS_HTML) is False
    assert tv.is_quota_page_bytes(
        200, "text/html; charset=utf-8", QUOTA_HTML) is True
    assert tv.is_quota_page_bytes(206, "video/mp4", b"\x00\x00") is False
