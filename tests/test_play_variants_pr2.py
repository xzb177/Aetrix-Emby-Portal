# -*- coding: utf-8 -*-
"""播放按变体取流（FMBY 批二 #11 PR-2，阶段 0 + 阶段 1）。

阶段 0（基线锁死，不改行为）：钉住现状的决策矩阵——四家真实源
（Drive / 115 挂载 / 本机 / 挂载中继）的变体清单、PlaybackInfo
多源金响应、#1207 租约「同条目两变体只占一额」、#1208 打点基线
（无变体调用时事件 variant 为 NULL、method 不变）。

阶段 1（按变体取流）：video_stream 入口读 variant 参数 /
MediaSourceId 后缀，按变体直达对应分支；变体未知报 404、已知
但该条目不可用报 409，两种情况都不静默改走默认条；无变体时
老路径逐字不变，第一条恒为现行主线。

全部离线：DB 用内存 SQLite，可见性/付费墙/referer/限流/Drive
存在性校验/中继取流全部桩掉；开关读数走 monkeypatch（与
test_play_variants.py 的 store 桩同口径）。打点落库走
backend.database.SessionLocal 桩（与 test_play_start_events.py
的 db_factory 同口径），断言 method 与 variant 双维度。
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

from backend import models
from backend.database import Base
from backend.emby_server import api, mounts as mount_lib
from backend.emby_server import models as em
from backend.emby_server import play_events, play_sign, play_variants
from backend.emby_server import playback_strategy, stream_limits
from backend.emby_server import streaming as streaming_mod

FID = "1eXUpWksxfCCrIZUye8lo24JGXyr_2ZQO"


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


def _request(query=b""):
    scope = {"type": "http", "method": "GET",
             "path": "/emby/Videos/x/stream",
             "raw_path": b"/emby/Videos/x/stream", "query_string": query,
             "headers": [], "scheme": "http", "server": ("testserver", 80),
             "client": ("testclient", 50000), "root_path": ""}
    return Request(scope)


def _user(uid=101, **kw):
    base = {"id": uid, "is_staff": False, "enable_video_transcoding": True}
    base.update(kw)
    return SimpleNamespace(**base)


def _item(guid, *, file_path="/data/movie.mkv", drive_file_id="", **kw):
    item = em.MediaItem()
    item.guid = guid
    item.name = kw.pop("name", "测试片")
    item.file_path = file_path
    item.drive_file_id = drive_file_id
    item.item_type = kw.pop("item_type", "movie")
    item.container = kw.pop("container", "mkv")
    item.size = kw.pop("size", 1_000_000_000)
    for k, v in kw.items():
        setattr(item, k, v)
    return item


def _drive_item(guid):
    return _item(guid, file_path="mount://3/电影/测试片.mkv",
                 drive_file_id=FID)


def _stub_switches(monkeypatch, *, dd="true", relay="true",
                   mount_ok=True):
    monkeypatch.setattr(api, "_drive_mount_id_for_item",
                        lambda db_, it: "drive-x" if mount_ok else None)
    real_get = playback_strategy.store.get_value

    def fake_get(db_, key, default="", **kwargs):
        if key == playback_strategy.KEY_DRIVE_DIRECT_PLAY:
            return dd
        if key == playback_strategy.KEY_DRIVE_RELAY:
            return relay
        return real_get(db_, key, default, **kwargs)

    monkeypatch.setattr(playback_strategy.store, "get_value", fake_get)


def _stub_gates(monkeypatch, item):
    monkeypatch.setattr(api, "_require_visible_item",
                        lambda db_, u_, iid: item)
    monkeypatch.setattr(api, "ensure_playback_allowed",
                        lambda db_, u_: None)
    # 播放门禁桩（与付费墙桩同口径；本文件测变体逻辑，不测门禁）。
    monkeypatch.setattr(
        "backend.emby_server.playback_gate.ensure_binding_playback_allowed",
        lambda db_, u_, r=None: None)
    monkeypatch.setattr(play_sign, "check_referer",
                        lambda request, db_: None)
    monkeypatch.setattr(api, "_enforce_stream_limit",
                        lambda db_, req, u_, it, iid: None)


def _call_stream(monkeypatch, db, user, item, query=b""):
    _stub_gates(monkeypatch, item)
    return asyncio.run(api.video_stream(
        item.guid, _request(query), user=user, db=db))


def _events(db):
    play_events.flush_pending()
    return db.query(em.PlayStartEvent).order_by(em.PlayStartEvent.id).all()


# ---------------------------------------------------------------------------
# 阶段 0：变体清单决策矩阵（list_variants，与 PR-1 清单下发同口径）
# ---------------------------------------------------------------------------

def test_phase0_drive_dual_on_lists_relay_first(db, monkeypatch):
    _stub_switches(monkeypatch)
    names = [v.name for v in play_variants.list_variants(
        db, _drive_item("g0-drive"), drive_mount_locatable=True)]
    assert names == ["drive_relay", "drive_direct"]


def test_phase0_drive_relay_off_or_unlocatable_lists_direct_first(
        db, monkeypatch):
    _stub_switches(monkeypatch, relay="false")
    names = [v.name for v in play_variants.list_variants(
        db, _drive_item("g0-norelay"), drive_mount_locatable=True)]
    assert names == ["drive_direct"]
    names2 = [v.name for v in play_variants.list_variants(
        db, _drive_item("g0-nomount"), drive_mount_locatable=False)]
    assert names2 == ["drive_direct"]


def test_phase0_mount_and_local_lists(db, monkeypatch):
    _stub_switches(monkeypatch)
    mount_names = [v.name for v in play_variants.list_variants(
        db, _item("g0-mount", file_path="mount://7/剧集/a.mkv"))]
    assert mount_names == ["mount_relay"]
    local_names = [v.name for v in play_variants.list_variants(
        db, _item("g0-local"))]
    assert local_names == ["local_file"]
    # 转码删除后：任何条目的清单里都不再有 transcode_hls
    assert "transcode_hls" not in mount_names + local_names


# ---------------------------------------------------------------------------
# 阶段 0：变体解析纯函数（参数 / 后缀 / 不一致 / 未知 / 不可用）
# ---------------------------------------------------------------------------

def test_parse_variant_param_wins_and_suffix_fallback():
    name, mismatch = play_variants.parse_requested_variant(
        {"variant": "drive_direct"})
    assert (name, mismatch) == ("drive_direct", False)
    name, mismatch = play_variants.parse_requested_variant(
        {"MediaSourceId": "guid-1:mount_relay"})
    assert (name, mismatch) == ("mount_relay", False)
    name, mismatch = play_variants.parse_requested_variant({})
    assert (name, mismatch) == (None, False)
    name, mismatch = play_variants.parse_requested_variant(
        {"variant": "drive_direct",
         "MediaSourceId": "guid-1:drive_relay"})
    assert (name, mismatch) == (None, True)


def test_resolve_variant_statuses():
    available = ["drive_relay", "drive_direct"]
    assert play_variants.resolve_requested_variant({}, available) == (None, None)
    assert play_variants.resolve_requested_variant(
        {"variant": "drive_direct"}, available) == ("drive_direct", None)
    assert play_variants.resolve_requested_variant(
        {"variant": "nope"}, available) == (None, 404)
    assert play_variants.resolve_requested_variant(
        {"variant": "mount_relay"}, available) == (None, 409)
    assert play_variants.resolve_requested_variant(
        {"variant": "drive_direct",
         "MediaSourceId": "g:drive_relay"}, available) == (None, 404)


# ---------------------------------------------------------------------------
# 阶段 0：#1207 租约——同条目两变体只占一额
# ---------------------------------------------------------------------------

class _FakeRedis:
    """只实现 stream_limits 用到的 eval / zrem，语义照抄 Lua 脚本。"""

    def __init__(self):
        self.sets = {}

    def eval(self, script, numkeys, key, now, ttl, lease_id, max_streams):
        members = self.sets.setdefault(key, {})
        for member in [m for m, score in members.items() if score <= now]:
            del members[member]
        if lease_id in members:
            members[lease_id] = now + ttl
            return 1
        if len(members) >= max_streams:
            return 0
        members[lease_id] = now + ttl
        return 1

    def zrem(self, key, member):
        self.sets.get(key, {}).pop(member, None)


def test_phase0_same_item_two_variants_one_lease(db, monkeypatch):
    fake = _FakeRedis()
    monkeypatch.setattr(stream_limits, "load_policies",
                        lambda db_: [stream_limits.StreamPathPolicy(
                            3, "/电影", 1)])
    monkeypatch.setattr(stream_limits, "_redis", lambda: fake)
    # 同设备同条目：第一次占位，第二次（换变体再请求）只刷新不新增
    assert stream_limits.acquire(
        db, "dev-1", "guid-x", "mount://3/电影/a.mkv") is None
    assert stream_limits.acquire(
        db, "dev-1", "guid-x", "mount://3/电影/b.mkv") is None
    assert len(list(fake.sets.values())[0]) == 1
    # 不同条目同设备：第二路被拒（上限 1 已占满）
    assert stream_limits.acquire(
        db, "dev-1", "guid-y", "mount://3/电影/c.mkv") is not None


# ---------------------------------------------------------------------------
# 阶段 0：#1208 打点基线——无变体调用时 variant 为 NULL
# ---------------------------------------------------------------------------

def test_phase0_play_event_without_variant_has_null_variant(db):
    play_events.note_play_start(provider="drive", method="relay",
                                user_id=9, item_id=42, result="ok")
    rows = _events(db)
    assert len(rows) == 1
    assert rows[0].method == "relay"
    assert rows[0].variant is None


def test_phase0_play_event_with_variant_round_trips(db):
    play_events.note_play_start(provider="drive", method="relay",
                                variant="drive_relay", user_id=9,
                                item_id=42, result="ok")
    rows = _events(db)
    assert rows[0].variant == "drive_relay"


# ---------------------------------------------------------------------------
# 阶段 1：无变体老路径逐字不变
# ---------------------------------------------------------------------------

def test_legacy_no_variant_drive_goes_relay_first(db, monkeypatch):
    _stub_switches(monkeypatch)
    item = _drive_item("g1-legacy")
    calls = []

    async def fake_relay(request, drive_id, file_id, media_type="video/mp4",
                         pt=None):
        calls.append(("relay", drive_id, file_id))
        return Response(content=b"relay-bytes", media_type=media_type)

    monkeypatch.setattr(api, "serve_drive_relay_async", fake_relay)
    resp = _call_stream(monkeypatch, db, _user(), item)
    assert calls == [("relay", "drive-x", FID)]
    assert resp.status_code == 200
    rows = _events(db)
    assert len(rows) == 1 and rows[0].method == "relay"
    assert rows[0].variant == "drive_relay"


def test_legacy_no_variant_mount_goes_mount_relay(db, monkeypatch):
    _stub_switches(monkeypatch)
    item = _item("g1-legacy-mount", file_path="mount://7/剧集/a.mkv")
    monkeypatch.setattr(
        mount_lib, "resolve_play_target",
        lambda path, db_, library=None: mount_lib.PlayTarget(
            "url", "https://115.example/v.mkv", {}))
    calls = []

    async def fake_mount(url, request, headers, media_type, pt=None, sniff_quota_page=False):
        calls.append(url)
        return Response(content=b"mount-bytes", media_type=media_type)

    monkeypatch.setattr(streaming_mod, "serve_mount_relay_async", fake_mount)
    resp = _call_stream(monkeypatch, db, _user(), item)
    assert calls == ["https://115.example/v.mkv"]
    assert resp.status_code == 200


# ---------------------------------------------------------------------------
# 阶段 1：按变体直达——五变体各一条
# ---------------------------------------------------------------------------

def test_forced_drive_relay_skips_direct(db, monkeypatch):
    _stub_switches(monkeypatch)
    item = _drive_item("g1-relay")
    calls = []

    async def fake_relay(request, drive_id, file_id, media_type="video/mp4",
                         pt=None):
        calls.append("relay")
        return Response(content=b"relay-bytes", media_type=media_type)

    monkeypatch.setattr(api, "serve_drive_relay_async", fake_relay)
    resp = _call_stream(monkeypatch, db, _user(), item,
                        query=b"variant=drive_relay")
    assert calls == ["relay"]
    assert resp.status_code == 200


def test_forced_drive_direct_via_param_and_via_suffix(db, monkeypatch):
    _stub_switches(monkeypatch)
    item = _drive_item("g1-direct")

    async def _exists_ok(db_, it, fid, force_recheck=False):
        return True

    monkeypatch.setattr(api, "_ensure_drive_file_playable", _exists_ok)
    resp = _call_stream(monkeypatch, db, _user(), item,
                        query=b"variant=drive_direct")
    assert resp.status_code == 302
    assert FID in resp.headers["location"]
    resp2 = _call_stream(
        monkeypatch, db, _user(), item,
        query=f"MediaSourceId={item.guid}:drive_direct".encode())
    assert resp2.status_code == 302
    assert FID in resp2.headers["location"]
    rows = _events(db)
    assert [r.method for r in rows] == ["direct_302", "direct_302"]
    assert [r.variant for r in rows] == ["drive_direct", "drive_direct"]


def test_forced_mount_relay_records_variant_event(db, monkeypatch):
    _stub_switches(monkeypatch)
    item = _item("g1-mount", file_path="mount://7/剧集/a.mkv")
    monkeypatch.setattr(
        mount_lib, "resolve_play_target",
        lambda path, db_, library=None: mount_lib.PlayTarget(
            "url", "https://115.example/v.mkv", {}))

    async def fake_mount(url, request, headers, media_type, pt=None, sniff_quota_page=False):
        return Response(content=b"mount-bytes", media_type=media_type)

    monkeypatch.setattr(streaming_mod, "serve_mount_relay_async", fake_mount)
    resp = _call_stream(monkeypatch, db, _user(), item,
                        query=b"variant=mount_relay")
    assert resp.status_code == 200
    rows = _events(db)
    assert len(rows) == 1
    assert rows[0].method == "mount_relay"
    assert rows[0].variant == "mount_relay"


def test_forced_local_file_records_variant_event(db, monkeypatch, tmp_path):
    _stub_switches(monkeypatch)
    # serve_file 真查磁盘：用一张真实存在的临时文件当本机目标
    real_file = tmp_path / "movie.mkv"
    real_file.write_bytes(b"x" * 100)
    item = _item("g1-local", file_path=str(real_file))
    monkeypatch.setattr(
        mount_lib, "resolve_play_target",
        lambda path, db_, library=None: mount_lib.PlayTarget(
            "local", str(real_file), {}))
    resp = _call_stream(monkeypatch, db, _user(), item,
                        query=b"variant=local_file")
    assert resp.status_code == 200
    rows = _events(db)
    assert len(rows) == 1
    assert rows[0].method == "local_file"
    assert rows[0].variant == "local_file"


def test_forced_transcode_now_404(db, monkeypatch):
    # 转码删除后：transcode_hls 已不是合法变体名，点它 → 404，不再 302 到 HLS
    _stub_switches(monkeypatch)
    item = _drive_item("g1-transcode")
    with pytest.raises(HTTPException) as excinfo:
        _call_stream(monkeypatch, db, _user(), item,
                     query=b"variant=transcode_hls&uid=9")
    assert excinfo.value.status_code == 404


# ---------------------------------------------------------------------------
# 阶段 1：404 / 409，不静默改走
# ---------------------------------------------------------------------------

def test_unknown_variant_404(db, monkeypatch):
    _stub_switches(monkeypatch)
    item = _drive_item("g1-unknown")
    with pytest.raises(HTTPException) as excinfo:
        _call_stream(monkeypatch, db, _user(), item,
                     query=b"variant=nope")
    assert excinfo.value.status_code == 404


def test_known_but_unavailable_variant_409(db, monkeypatch):
    _stub_switches(monkeypatch)
    # 本地条目点 Drive 中继：名字已知、清单里没有 → 409，不回落本机文件
    item = _item("g1-unavail", file_path="/data/movie.mkv")
    with pytest.raises(HTTPException) as excinfo:
        _call_stream(monkeypatch, db, _user(), item,
                     query=b"variant=drive_relay")
    assert excinfo.value.status_code == 409


def test_variant_suffix_mismatch_404(db, monkeypatch):
    _stub_switches(monkeypatch)
    item = _drive_item("g1-mismatch")
    with pytest.raises(HTTPException) as excinfo:
        _call_stream(monkeypatch, db, _user(), item,
                     query=b"variant=drive_direct&MediaSourceId="
                           b"g1-mismatch:drive_relay")
    assert excinfo.value.status_code == 404


def test_forced_variant_wrong_branch_409(db, monkeypatch):
    _stub_switches(monkeypatch)
    # 挂载条目点本机文件：清单里有 local_file 吗？没有（挂载清单只有
    # mount_relay），所以这里先是 409；下面这条是另一层：
    # 挂载条目点 mount_relay，但解析回的却是本机文件目标 → 409
    item = _item("g1-wrongbranch", file_path="mount://7/剧集/a.mkv")
    monkeypatch.setattr(
        mount_lib, "resolve_play_target",
        lambda path, db_, library=None: mount_lib.PlayTarget(
            "local", "/data/movie.mkv", {}))
    with pytest.raises(HTTPException) as excinfo:
        _call_stream(monkeypatch, db, _user(), item,
                     query=b"variant=mount_relay")
    assert excinfo.value.status_code == 409
