# -*- coding: utf-8 -*-
"""取流 provider 分流回归（2026-11-02 夸克副本串线实锤）。

现场：user 829 播条目 696679《Running Man 第690集》，跨 provider
转存单 done、变体 ready（variant_drive_id=quark:user-829、
variant_file_id 是夸克 fid）；客户端走通用签名地址 stream.mkv，
play_sign 已下发链接消费让门禁直接 proceed（不看变体），选路
（交付计划 / resolve_play_file_id）已采用夸克变体行，但
video_stream 后段只认 Drive：夸克 fid 落进 Drive 快路 302 分支
被喂给 Google files.get，404 原样出门，播放器显示自家文案
「资源类型无效」。EA 24h 内 quark.cn 请求数为 0——夸克取流
分支从没被进过。

本文件钉死：
- quark ready 变体 + play_sign 消费请求 → 必须走夸克中继
  （resolve_user_quark_copy_url + serve_mount_relay_async），
  全程零次 Drive files.get（_ensure_drive_file_playable）、
  零次 Drive 中继、零次 usercontent 302。
- pan115 ready 变体 + play_sign 消费请求 → 必须走 serve_115_copy
  现签 302，同款零 Drive 断言（防串线）。
- 吃过死链 strike 的夸克变体不许被分流直用，也不许落进 Drive
  分支喂 Google：响亮 409（护栏）。
- userdrive ready 变体（正常 Drive 副本）行为不变：仍走 Drive
  直链 302（防误伤哨兵）。

全部离线：内存 SQLite + 模块边界桩；monkeypatch 一律 patch
类/模块属性（仓库纪律）。门禁本体走真实实现（play_sign 消费
短路是本 bug 的触发点，不许桩掉）。
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
from backend.emby_server import api, play_events, play_sign
from backend.emby_server import models as em
from backend.emby_server import playback_strategy
from backend.emby_server import transfer_variant as tv
from backend.emby_server import user_bindings as ub
from backend.emby_server import streaming as streaming_mod
from backend.emby_server import drive_auth

ORIG_FID = "1eXUpWksxfCCrIZUye8lo24JGXyr_2ZQO"
QUARK_FID = "quarkfid-696679-ep690"
PAN115_FID = "115fid-696679-ep690"


@pytest.fixture(autouse=True)
def _isolate_play_events():
    play_events.reset_for_tests()
    yield
    play_events.reset_for_tests()


@pytest.fixture(autouse=True)
def _enable_transfer(monkeypatch):
    monkeypatch.setenv("TRANSFER_VARIANT_ENABLED", "1")


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
             "path": "/emby/Videos/x/stream.mkv",
             "raw_path": b"/emby/Videos/x/stream.mkv",
             "query_string": query,
             "headers": [], "scheme": "http", "server": ("testserver", 80),
             "client": ("testclient", 50000), "root_path": ""}
    return Request(scope)


def _item(item_id=696679):
    item = em.MediaItem()
    item.id = item_id
    item.guid = f"guid-{item_id}"
    item.name = "Running Man 第690集"
    item.file_path = "mount://3/综艺/RunningMan690.mkv"
    item.drive_file_id = ORIG_FID
    item.item_type = "episode"
    item.container = "mkv"
    item.size = 1_000_000_000
    return item


def _variant(item_id, user_id, drive_id, fid, **kw):
    row = em.TransferVariant()
    row.item_id = item_id
    row.owner_scope = "user"
    row.owner_ref = str(user_id)
    row.status = "ready"
    row.variant_drive_id = drive_id
    row.variant_file_id = fid
    row.stale_count = kw.pop("stale_count", 0)
    for key, value in kw.items():
        setattr(row, key, value)
    return row


class _Probes:
    """Drive 侧全部出口的计数桩：任一被调用即说明串线。"""

    def __init__(self):
        self.ensure_calls = []       # _ensure_drive_file_playable 的 fid
        self.drive_relay_calls = []  # serve_drive_relay_async 入参
        self.usercontent_calls = []  # drive_usercontent_download_url 的 fid


def _stub_common(monkeypatch, item, probes, *, ensure_ok_fids=()):
    """桩通 video_stream 前置链路 + Drive 出口计数。

    门禁本体不桩（真实短路）；选路两口由各用例按场景装配。
    """
    monkeypatch.setattr(api, "_require_visible_item",
                        lambda db_, u_, iid: item)
    monkeypatch.setattr(api, "ensure_playback_allowed",
                        lambda db_, u_: None)
    monkeypatch.setattr(
        "backend.emby_server.playback_gate.ensure_binding_playback_allowed",
        lambda db_, u_, r=None: None)
    # 本 bug 的触发点：已下发签名链接消费（真实门禁据此 proceed）。
    monkeypatch.setattr(
        "backend.emby_server.playback_gate._request_uses_issued_play_sign",
        lambda request, db_: True)
    monkeypatch.setattr(play_sign, "check_referer",
                        lambda request, db_: None)
    monkeypatch.setattr(api, "_enforce_stream_limit",
                        lambda db_, req, u_, it, iid: None)
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

    async def fake_ensure(db_, it, fid, **kwargs):
        probes.ensure_calls.append(fid)
        if fid in ensure_ok_fids:
            return True
        # 生产形态：别家 fid 喂 Google files.get 直接 404 原样抛。
        raise HTTPException(status_code=404, detail="Not Found")

    monkeypatch.setattr(api, "_ensure_drive_file_playable", fake_ensure)

    async def fake_drive_relay(request, drive_id, file_id, **kwargs):
        probes.drive_relay_calls.append((drive_id, file_id))
        return Response(content=b"drive-relay-bytes", status_code=200)

    monkeypatch.setattr(api, "serve_drive_relay_async", fake_drive_relay)
    monkeypatch.setattr(api, "drive_exists_rapid_retry",
                        lambda request, user_, fid: False)

    def fake_usercontent(fid):
        probes.usercontent_calls.append(fid)
        return f"https://usercontent.test/{fid}"

    monkeypatch.setattr(drive_auth, "drive_usercontent_download_url",
                        fake_usercontent)


def _call(db, user, item):
    return asyncio.run(api.video_stream(
        item.guid, _request(), user=user, db=db))


# ---------------------------------------------------------------------------
# 夸克 ready 变体 + 签名消费 → 夸克中继，零 Drive 调用
# ---------------------------------------------------------------------------

def test_quark_ready_variant_sign_consume_goes_quark_relay(
        db, monkeypatch):
    item = _item()
    user = SimpleNamespace(id=829, is_staff=False)
    row = _variant(item.id, 829, "quark:user-829", QUARK_FID)
    probes = _Probes()
    _stub_common(monkeypatch, item, probes)
    # 选路走交付计划口（生产热路径）：采用夸克变体行。
    monkeypatch.setattr(
        "backend.emby_server.delivery_plan.plan_ready_leg",
        lambda db_, it, u_: (QUARK_FID, row))
    monkeypatch.setattr(
        tv, "resolve_play_file_id",
        lambda db_, it, u_: (ORIG_FID, None))

    quark_calls = {}

    def fake_resolve_quark(db_, user_id, fid):
        quark_calls["args"] = (user_id, fid)
        return "https://quark.test/dl/RunningMan690.mkv", "cookie-xyz"

    monkeypatch.setattr(ub, "resolve_user_quark_copy_url",
                        fake_resolve_quark)

    relay_calls = []

    async def fake_mount_relay(url, request, headers=None, media_type=None,
                               pt=None, **kwargs):
        relay_calls.append((url, headers))
        return Response(content=b"quark-bytes", status_code=206)

    monkeypatch.setattr(streaming_mod, "serve_mount_relay_async",
                        fake_mount_relay)

    resp = _call(db, user, item)
    assert resp.status_code == 206
    assert quark_calls["args"] == (829, QUARK_FID)
    assert len(relay_calls) == 1
    assert relay_calls[0][0] == "https://quark.test/dl/RunningMan690.mkv"
    assert relay_calls[0][1]["Cookie"] == "cookie-xyz"
    # 防串线核心断言：别家 fid 全程没喂过 Google 任一出口。
    assert probes.ensure_calls == []
    assert probes.drive_relay_calls == []
    assert probes.usercontent_calls == []


# ---------------------------------------------------------------------------
# 115 ready 变体 + 签名消费 → serve_115_copy 现签 302，零 Drive 调用
# ---------------------------------------------------------------------------

def test_pan115_ready_variant_sign_consume_goes_115_copy(
        db, monkeypatch):
    item = _item()
    user = SimpleNamespace(id=7, is_staff=False)
    row = _variant(item.id, 7, "pan115:user-7", PAN115_FID,
                   pickcode="PC999")
    probes = _Probes()
    _stub_common(monkeypatch, item, probes)
    # 选路走 resolve 口（无交付计划时）：采用 115 变体行。
    monkeypatch.setattr(
        "backend.emby_server.delivery_plan.plan_ready_leg",
        lambda db_, it, u_: None)
    monkeypatch.setattr(
        tv, "resolve_play_file_id",
        lambda db_, it, u_: (PAN115_FID, row))

    seen = {}

    def fake_resolve_115(db_, user_id, pickcode, ua=""):
        seen["pickcode"] = pickcode
        return "https://115.test/dl/RunningMan690.mkv"

    monkeypatch.setattr(ub, "resolve_user115_copy_url", fake_resolve_115)
    monkeypatch.setattr(ub, "check_115_download_alive",
                        lambda url: "alive")

    resp = _call(db, user, item)
    assert resp.status_code == 302
    assert resp.headers["location"] == \
        "https://115.test/dl/RunningMan690.mkv"
    assert seen["pickcode"] == "PC999"
    assert probes.ensure_calls == []
    assert probes.drive_relay_calls == []
    assert probes.usercontent_calls == []


# ---------------------------------------------------------------------------
# 护栏：吃过 strike 的夸克变体——不直用、不喂 Google，响亮 409
# ---------------------------------------------------------------------------

def test_stale_quark_variant_blocked_not_fed_to_google(
        db, monkeypatch):
    item = _item()
    user = SimpleNamespace(id=829, is_staff=False)
    row = _variant(item.id, 829, "quark:user-829", QUARK_FID,
                   stale_count=1)
    probes = _Probes()
    _stub_common(monkeypatch, item, probes)
    monkeypatch.setattr(
        "backend.emby_server.delivery_plan.plan_ready_leg",
        lambda db_, it, u_: (QUARK_FID, row))
    monkeypatch.setattr(
        tv, "resolve_play_file_id",
        lambda db_, it, u_: (ORIG_FID, None))

    with pytest.raises(HTTPException) as excinfo:
        _call(db, user, item)
    assert excinfo.value.status_code == 409
    assert probes.ensure_calls == []
    assert probes.drive_relay_calls == []
    assert probes.usercontent_calls == []


# ---------------------------------------------------------------------------
# 哨兵：userdrive ready 变体仍走 Drive 直链（行为不变）
# ---------------------------------------------------------------------------

def test_userdrive_ready_variant_still_goes_drive_direct(
        db, monkeypatch):
    item = _item()
    user = SimpleNamespace(id=7, is_staff=False)
    copy_fid = "drivecopyfid-7"
    row = _variant(item.id, 7, "userdrive:7", copy_fid)
    probes = _Probes()
    _stub_common(monkeypatch, item, probes, ensure_ok_fids=(copy_fid,))
    monkeypatch.setattr(
        "backend.emby_server.delivery_plan.plan_ready_leg",
        lambda db_, it, u_: (copy_fid, row))
    monkeypatch.setattr(
        tv, "resolve_play_file_id",
        lambda db_, it, u_: (ORIG_FID, None))
    # 本人副本额度探测三口桩成“无异常”（本用例不测额度链）。
    monkeypatch.setattr(tv, "usercopy_quota_probe_cached",
                        lambda user_id, fid: False)
    monkeypatch.setattr(tv, "probe_user_copy_quota_decision",
                        lambda user_id, fid: None)
    monkeypatch.setattr(tv, "usercopy_quota_probe_detail",
                        lambda user_id, fid: {})

    resp = _call(db, user, item)
    assert resp.status_code == 302
    assert resp.headers["location"] == \
        f"https://usercontent.test/{copy_fid}"
    assert probes.ensure_calls == [copy_fid]
