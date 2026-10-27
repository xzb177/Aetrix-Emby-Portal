# -*- coding: utf-8 -*-
"""播放变体清单下发（FMBY 批二 #11 PR-1，backend/emby_server/play_variants.py；
2026-10-24 转码删除后版）。

钉住的口径：
- 开关关（默认）：PlaybackInfo 单源、Id=guid、URL 不带 variant 参数、
  SupportsTranscoding 恒为假且无 TranscodingUrl；
- 开关开：按条目真实可用变体多源下发，第一条恒为现行主线变体，
  Id 带 ``:{variant}`` 后缀、URL 带 variant 参数双保险；
- 变体集合按条目类型正确（转码删除后不再有 transcode_hls）：
  Drive 条目（双开+挂载可定位）中继+直链 / 中继关只剩直链；
  挂载条目中继；本地条目本地；
- 申报诚实且两面同源：每条 SupportsTranscoding 为假，且 _media_source
  （详情 DTO 同函数）同步为假——不许 PlaybackInfo 面与详情面各说各话；
- 缓存不串用户：同一条目两名用户各自的签名 uid 互不串。

全部离线：DB 用内存 SQLite，可见性/付费墙/探测入队/域名改写/Drive
挂载定位全部桩掉，开关读数走 monkeypatch（与 test_drive_direct_play
的 store 桩同口径）。
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

from backend.database import Base
from backend.emby_server import api, play_variants, probe_worker, stream_accel
from backend.emby_server import models as em
from backend.emby_server import playback_strategy

FID = "1eXUpWksxfCCrIZUye8lo24JGXyr_2ZQO"


@pytest.fixture()
def db():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    try:
        yield session
    finally:
        session.rollback()
        session.close()
        engine.dispose()


def _request():
    scope = {"type": "http", "method": "GET",
             "path": "/Items/x/PlaybackInfo",
             "raw_path": b"/Items/x/PlaybackInfo", "query_string": b"",
             "headers": [], "scheme": "http", "server": ("testserver", 80),
             "client": ("testclient", 50000), "root_path": ""}
    return Request(scope)


def _user(uid=101, **kw):
    base = {"id": uid, "is_staff": False}
    base.update(kw)
    return SimpleNamespace(**base)


def _item(guid, *, file_path="/data/movie.mkv", drive_file_id="", **kw):
    item = em.MediaItem()
    item.guid = guid
    item.name = kw.pop("name", "测试片")
    item.file_path = file_path
    item.drive_file_id = drive_file_id
    item.item_type = kw.pop("item_type", "movie")
    item.size = kw.pop("size", 1_000_000_000)
    item.duration_ticks = kw.pop("duration_ticks", 36_000_000_000)
    item.bitrate = kw.pop("bitrate", 2_000_000)
    for k, v in kw.items():
        setattr(item, k, v)
    return item


def _call_pi(monkeypatch, db, user, item, *, variants_on,
             dd="true", relay="true", mount_ok=True):
    monkeypatch.setattr(api, "_require_visible_item",
                        lambda db_, u_, iid: item)
    monkeypatch.setattr(api, "ensure_playback_allowed",
                        lambda db_, u_: None)
    # 播放门禁桩（与付费墙桩同口径；本文件测变体逻辑，不测门禁）。
    monkeypatch.setattr(
        "backend.emby_server.playback_gate.ensure_binding_playback_allowed",
        lambda db_, u_, r=None: None)
    monkeypatch.setattr(probe_worker, "maybe_enqueue",
                        lambda db_, it: None)
    monkeypatch.setattr(
        stream_accel, "rewrite_playback_urls_unified",
        lambda db_, s, base: (s, None))
    monkeypatch.setattr(api, "_drive_mount_id_for_item",
                        lambda db_, it: "drive-x" if mount_ok else None)
    monkeypatch.setattr(play_variants, "_ENABLED", variants_on)
    real_get = playback_strategy.store.get_value

    def fake_get(db_, key, default="", **kwargs):
        if key == playback_strategy.KEY_DRIVE_DIRECT_PLAY:
            return dd
        if key == playback_strategy.KEY_DRIVE_RELAY:
            return relay
        return real_get(db_, key, default, **kwargs)

    monkeypatch.setattr(playback_strategy.store, "get_value", fake_get)
    return asyncio.run(api.playback_info(
        item.guid, _request(), user_id="", user=user, db=db))


def _drive_item(guid):
    return _item(guid, file_path="mount://3/电影/测试片.mkv",
                 drive_file_id=FID)


# ---------------------------------------------------------------------------
# 开关关：与改前逐字一致（对照组）
# ---------------------------------------------------------------------------

def test_switch_off_single_source_legacy_shape(db, monkeypatch):
    item = _drive_item("guid-off-1")
    user = _user()
    resp = _call_pi(monkeypatch, db, user, item, variants_on=False)
    sources = resp["MediaSources"]
    assert len(sources) == 1
    src = sources[0]
    assert src["Id"] == item.guid
    assert "variant=" not in src["DirectStreamUrl"]
    assert f"MediaSourceId={item.guid}&" in src["DirectStreamUrl"]
    # 转码删除后：申报恒为假且无转码地址
    assert src["SupportsTranscoding"] is False
    assert "TranscodingUrl" not in src
    assert src["SupportsDirectStream"] is True


def test_switch_off_cache_hit_same_and_users_isolated(db, monkeypatch):
    item = _drive_item("guid-off-2")
    r1 = _call_pi(monkeypatch, db, _user(101), item, variants_on=False)
    r2 = _call_pi(monkeypatch, db, _user(101), item, variants_on=False)
    r3 = _call_pi(monkeypatch, db, _user(202), item, variants_on=False)
    s1 = dict(r1["MediaSources"][0])
    s2 = dict(r2["MediaSources"][0])
    assert s1 == s2  # 缓存命中不改基础体与 URL（同用户同签名输入）
    url3 = r3["MediaSources"][0]["DirectStreamUrl"]
    assert "uid=202" in url3 and "uid=101" not in url3  # 签名不串用户


# ---------------------------------------------------------------------------
# 开关开：变体集合按条目类型正确、主线第一
# ---------------------------------------------------------------------------

def test_drive_item_variants_direct_first(db, monkeypatch):
    # 直链为主线（2026-10-27 用户改令）：双开关都开时第一条恒为直链，
    # 中继列在之后作兜底；此前“中继第一”的口径已作废。
    item = _drive_item("guid-on-drive")
    resp = _call_pi(monkeypatch, db, _user(), item, variants_on=True)
    sources = resp["MediaSources"]
    assert [s["Id"] for s in sources] == [
        f"{item.guid}:drive_direct",
        f"{item.guid}:drive_relay",
    ]
    first = sources[0]
    assert "variant=drive_direct" in first["DirectStreamUrl"]
    assert f"MediaSourceId={item.guid}:drive_direct" in first["DirectStreamUrl"]
    assert first["Name"].endswith("Drive 直链")
    assert first["IsRemote"] is True
    relay = sources[1]
    assert "variant=drive_relay" in relay["DirectStreamUrl"]
    for src in sources:
        assert src["SupportsTranscoding"] is False
        assert "TranscodingUrl" not in src


def test_drive_item_variants_relay_off(db, monkeypatch):
    # 转码删除后只剩直链一条变体 → 保持原单源下发不变样（Id 不带后缀）
    item = _drive_item("guid-on-drive-norelay")
    resp = _call_pi(monkeypatch, db, _user(), item, variants_on=True,
                    relay="false")
    sources = resp["MediaSources"]
    assert [s["Id"] for s in sources] == [item.guid]
    assert sources[0]["SupportsTranscoding"] is False
    assert "TranscodingUrl" not in sources[0]


def test_drive_item_variants_mount_not_locatable(db, monkeypatch):
    # 同上：单变体时保持原单源下发不变样
    item = _drive_item("guid-on-drive-nomount")
    resp = _call_pi(monkeypatch, db, _user(), item, variants_on=True,
                    mount_ok=False)
    sources = resp["MediaSources"]
    assert [s["Id"] for s in sources] == [item.guid]
    assert sources[0]["SupportsTranscoding"] is False
    assert "TranscodingUrl" not in sources[0]


def test_mount_item_variants(db, monkeypatch):
    # 同上：单变体时保持原单源下发不变样（清单层仍只有 mount_relay，见
    # test_transcode_removed.py 的 list_variants 断言）
    item = _item("guid-on-mount", file_path="mount://7/剧集/a.mkv")
    resp = _call_pi(monkeypatch, db, _user(), item, variants_on=True)
    sources = resp["MediaSources"]
    assert [s["Id"] for s in sources] == [item.guid]
    assert sources[0]["SupportsTranscoding"] is False
    assert "TranscodingUrl" not in sources[0]


def test_local_item_variants(db, monkeypatch):
    # 同上：单变体时保持原单源下发不变样
    item = _item("guid-on-local", file_path="/data/movie.mkv")
    resp = _call_pi(monkeypatch, db, _user(), item, variants_on=True)
    sources = resp["MediaSources"]
    assert [s["Id"] for s in sources] == [item.guid]
    assert sources[0]["Protocol"] == "File"
    assert "variant=" not in sources[0]["DirectStreamUrl"]
    assert sources[0]["SupportsTranscoding"] is False
    assert "TranscodingUrl" not in sources[0]


# ---------------------------------------------------------------------------
# 转码删除后：清单里永远没有转码变体 + 两面申报同步为假
# ---------------------------------------------------------------------------

def test_no_transcode_variant_ever(db, monkeypatch):
    item = _drive_item("guid-off-transcode")
    user = _user()
    resp = _call_pi(monkeypatch, db, user, item, variants_on=True)
    sources = resp["MediaSources"]
    assert [s["Id"] for s in sources] == [
        f"{item.guid}:drive_direct", f"{item.guid}:drive_relay"]
    for src in sources:
        assert src["SupportsTranscoding"] is False
        assert "TranscodingUrl" not in src
    # 详情面（_item_dto 嵌的就是 _media_source）同源同值：同步为假
    detail_src = api._media_source(item, "http://testserver", "", db,
                                   user=user)
    assert detail_src["SupportsTranscoding"] is False


def test_media_source_always_reports_no_transcoding(db, monkeypatch):
    # 转码删除后：staff 与普通用户一律报假，不再有 staff 豁免口径
    item = _item("guid-mouth", file_path="/data/m.mkv")
    src = api._media_source(item, "http://testserver", "", db,
                            user=_user(is_staff=True))
    assert src["SupportsTranscoding"] is False
    assert "TranscodingUrl" not in src
    src2 = api._media_source(item, "http://testserver", "", db,
                             user=_user())
    assert src2["SupportsTranscoding"] is False
    assert "TranscodingUrl" not in src2


# ---------------------------------------------------------------------------
# list_variants / variantize_url 单元口径
# ---------------------------------------------------------------------------

def test_list_variants_drive_direct_off_falls_back_to_path(db, monkeypatch):
    real_get = playback_strategy.store.get_value

    def fake_get(db_, key, default="", **kwargs):
        if key == playback_strategy.KEY_DRIVE_DIRECT_PLAY:
            return "false"
        return real_get(db_, key, default, **kwargs)

    monkeypatch.setattr(playback_strategy.store, "get_value", fake_get)
    mount_item = _drive_item("guid-fallback-mount")
    names = [v.name for v in play_variants.list_variants(
        db, mount_item, drive_mount_locatable=True)]
    assert names == ["mount_relay"]


def test_variantize_url():
    url = ("http://h/emby/Videos/g1/stream?static=true"
           "&MediaSourceId=g1&uid=1&exp=2&sign=abc")
    out = play_variants.variantize_url(url, "g1", "drive_relay")
    assert "MediaSourceId=g1:drive_relay" in out
    assert out.endswith("&variant=drive_relay")
    assert "sign=abc" in out  # 签名串原样保留
