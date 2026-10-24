# -*- coding: utf-8 -*-
"""播放阶段 6：按可用性自动换线（清单第 6 项）。

钉死三条（开关开时）：
- 可用变体优先：中继在可用性表中不可用时，起播直接走直链，不试死线；
- 失败重试点：中继上游 502 时改走直链（上限一次），凭据错
  （403/503）与 404 不换线；
- 全部不可用时明确失败（409 + 变体名与原因），不静默播死线。
关时选路与今天逐字一致（中继 502 原样抛）。

全部离线：DB 用内存 SQLite，可见性/付费墙/referer/限流/Drive
存在性校验/中继取流全部桩掉（与 test_play_variants_pr2.py 同口径）；
开关读数走 monkeypatch env（switch_enabled 每次现读）。
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
from backend.emby_server import playback_strategy, variant_switch
from backend.emby_server import drive_auth

FID = "1eXUpWksxfCCrIZUye8lo24JGXyr_2ZQO"
SWITCH_ENV = "PLAYBACK_VARIANT_SWITCH_ENABLED"


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


def _user(uid=101):
    return SimpleNamespace(id=uid, is_staff=False)


def _drive_item(guid):
    item = em.MediaItem()
    item.guid = guid
    item.name = "测试片"
    item.file_path = "mount://3/电影/测试片.mkv"
    item.drive_file_id = FID
    item.item_type = "movie"
    item.container = "mkv"
    item.size = 1_000_000_000
    return item


def _set_availability(db, key, status):
    row = em.VariantAvailability(
        variant_key=key, availability_status=status,
        consecutive_failures=3 if status == "unavailable" else 0)
    db.add(row)
    db.commit()


def _events(db):
    play_events.flush_pending()
    return db.query(em.PlayStartEvent).order_by(em.PlayStartEvent.id).all()


def _stub_switches(monkeypatch):
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


def _stub_gates(monkeypatch, item):
    monkeypatch.setattr(api, "_require_visible_item",
                        lambda db_, u_, iid: item)
    monkeypatch.setattr(api, "ensure_playback_allowed",
                        lambda db_, u_: None)
    monkeypatch.setattr(play_sign, "check_referer",
                        lambda request, db_: None)
    monkeypatch.setattr(api, "_enforce_stream_limit",
                        lambda db_, req, u_, it, iid: None)


def _call_stream(monkeypatch, db, user, item, query=b"", *,
                 relay=None, ensure=None, switch_on=True):
    """relay/ensure 是桩行为：relay 为 HTTPException 时中继抛错，
    为 Response 时中继成功返回；ensure 为 True/None/HTTPException。
    返回 (response_or_None, calls)，calls 记两桩各被调几次。
    """
    _stub_switches(monkeypatch)
    _stub_gates(monkeypatch, item)
    if switch_on:
        monkeypatch.setenv(SWITCH_ENV, "1")
    else:
        monkeypatch.delenv(SWITCH_ENV, raising=False)
    calls = {"relay": 0, "ensure": 0}

    async def fake_relay(request, drive_id, fid, **kwargs):
        calls["relay"] += 1
        if isinstance(relay, HTTPException):
            raise relay
        return relay

    async def fake_ensure(db_, it, fid, **kwargs):
        calls["ensure"] += 1
        if isinstance(ensure, HTTPException):
            raise ensure
        return ensure

    monkeypatch.setattr(api, "serve_drive_relay_async", fake_relay)
    monkeypatch.setattr(api, "_ensure_drive_file_playable", fake_ensure)
    monkeypatch.setattr(api, "drive_exists_rapid_retry",
                        lambda request, user_, fid: False)
    monkeypatch.setattr(drive_auth, "drive_usercontent_download_url",
                        lambda fid: f"https://usercontent.test/{fid}")
    return asyncio.run(api.video_stream(
        item.guid, _request(query), user=user, db=db)), calls


def _ok_relay():
    return Response(content=b"x", status_code=206,
                    media_type="video/x-matroska")


# ---------------------------------------------------------------------------
# 决策层：开关 / 换线资格 / 可用性排序
# ---------------------------------------------------------------------------

def test_switch_enabled_default_off_and_on(monkeypatch):
    monkeypatch.delenv(SWITCH_ENV, raising=False)
    assert variant_switch.switch_enabled() is False
    monkeypatch.setenv(SWITCH_ENV, "1")
    assert variant_switch.switch_enabled() is True
    monkeypatch.setenv(SWITCH_ENV, "0")
    assert variant_switch.switch_enabled() is False


def test_is_switch_eligible_only_502():
    assert variant_switch.is_switch_eligible_relay_status(502) is True
    for status in (401, 403, 404, 429, 500, 503):
        assert variant_switch.is_switch_eligible_relay_status(status) is False


def test_split_available_no_rows_all_available(db):
    avail, unavail = variant_switch.split_available(db, [
        ("drive_relay", None), ("drive_direct", None)])
    assert avail == [("drive_relay", None), ("drive_direct", None)]
    assert unavail == []


def test_split_available_relay_unavailable_direct_first(db):
    _set_availability(db, "drive_relay", "unavailable")
    avail, unavail = variant_switch.split_available(db, [
        ("drive_relay", None), ("drive_direct", None)])
    assert avail == [("drive_direct", None)]
    assert unavail == [("drive_relay", None)]
    first, _, _ = variant_switch.drive_route_choice(db)
    assert first == ("drive_direct", None)


def test_split_available_mount_keys_independent(db):
    _set_availability(db, "mount_relay:1", "unavailable")
    avail, unavail = variant_switch.split_available(db, [
        ("mount_relay", 1), ("mount_relay", 2)])
    assert avail == [("mount_relay", 2)]
    assert unavail == [("mount_relay", 1)]
    ordered = variant_switch.order_candidates(db, [
        ("mount_relay", 1), ("mount_relay", 2)])
    assert ordered == [("mount_relay", 2), ("mount_relay", 1)]


def test_drive_route_choice_all_unavailable(db):
    _set_availability(db, "drive_relay", "unavailable")
    _set_availability(db, "drive_direct", "unavailable")
    first, avail, unavail = variant_switch.drive_route_choice(db)
    assert first is None
    assert avail == []
    assert unavail == [("drive_relay", None), ("drive_direct", None)]


# ---------------------------------------------------------------------------
# video_stream 层：开关关时逐字不变；开时换线三条钉死
# ---------------------------------------------------------------------------

def test_stream_switch_off_relay_502_propagates(db, monkeypatch):
    with pytest.raises(HTTPException) as excinfo:
        _call_stream(monkeypatch, db, _user(), _drive_item("s-off"),
                     relay=HTTPException(status_code=502, detail="源站不可达"),
                     ensure=True, switch_on=False)
    assert excinfo.value.status_code == 502


def test_stream_switch_on_relay_502_falls_to_direct(db, monkeypatch):
    resp, calls = _call_stream(
        monkeypatch, db, _user(), _drive_item("s-502"),
        relay=HTTPException(status_code=502, detail="源站不可达"),
        ensure=True, switch_on=True)
    assert resp.status_code == 302
    assert resp.headers["location"] == f"https://usercontent.test/{FID}"
    assert calls == {"relay": 1, "ensure": 1}
    evs = [(e.method, e.variant, e.result, e.error_kind)
           for e in _events(db)]
    assert ("relay", "drive_relay", "error", "upstream_502") in evs
    assert ("direct_302", "drive_direct", "ok", None) in evs


def test_stream_switch_on_relay_unavailable_skips_relay(db, monkeypatch):
    _set_availability(db, "drive_relay", "unavailable")
    resp, calls = _call_stream(
        monkeypatch, db, _user(), _drive_item("s-skip"),
        relay=_ok_relay(), ensure=True, switch_on=True)
    assert resp.status_code == 302
    assert calls["relay"] == 0
    assert calls["ensure"] == 1


def test_stream_switch_on_all_unavailable_409(db, monkeypatch):
    _set_availability(db, "drive_relay", "unavailable")
    _set_availability(db, "drive_direct", "unavailable")
    with pytest.raises(HTTPException) as excinfo:
        _call_stream(monkeypatch, db, _user(), _drive_item("s-all"),
                     relay=_ok_relay(), ensure=True, switch_on=True)
    assert excinfo.value.status_code == 409
    assert "均不可用" in excinfo.value.detail


def test_stream_switch_on_relay_403_no_switch(db, monkeypatch):
    with pytest.raises(HTTPException) as excinfo:
        _call_stream(monkeypatch, db, _user(), _drive_item("s-403"),
                     relay=HTTPException(status_code=403, detail="forbidden"),
                     ensure=True, switch_on=True)
    assert excinfo.value.status_code == 403


def test_stream_switch_on_relay_503_no_switch(db, monkeypatch):
    with pytest.raises(HTTPException) as excinfo:
        _call_stream(monkeypatch, db, _user(), _drive_item("s-503"),
                     relay=HTTPException(status_code=503, detail="no credential"),
                     ensure=True, switch_on=True)
    assert excinfo.value.status_code == 503


def test_stream_switch_on_relay_502_direct_unavailable_409(db, monkeypatch):
    _set_availability(db, "drive_direct", "unavailable")
    with pytest.raises(HTTPException) as excinfo:
        _call_stream(monkeypatch, db, _user(), _drive_item("s-502u"),
                     relay=HTTPException(status_code=502, detail="源站不可达"),
                     ensure=True, switch_on=True)
    assert excinfo.value.status_code == 409
    assert "变体切换失败" in excinfo.value.detail


def test_stream_switch_on_relay_502_direct_ensure_raises_wrapped(
        db, monkeypatch):
    with pytest.raises(HTTPException) as excinfo:
        _call_stream(monkeypatch, db, _user(), _drive_item("s-502e"),
                     relay=HTTPException(status_code=502, detail="源站不可达"),
                     ensure=HTTPException(status_code=503, detail="暂不可用"),
                     switch_on=True)
    assert excinfo.value.status_code == 503
    assert "变体切换后仍失败" in excinfo.value.detail


def test_stream_switch_on_relay_502_direct_ensure_none_409(db, monkeypatch):
    with pytest.raises(HTTPException) as excinfo:
        _call_stream(monkeypatch, db, _user(), _drive_item("s-502n"),
                     relay=HTTPException(status_code=502, detail="源站不可达"),
                     ensure=None, switch_on=True)
    assert excinfo.value.status_code == 409
    assert "变体切换后仍失败" in excinfo.value.detail


def test_stream_switch_on_explicit_variant_502_no_switch(db, monkeypatch):
    with pytest.raises(HTTPException) as excinfo:
        _call_stream(monkeypatch, db, _user(), _drive_item("s-explicit"),
                     query=b"variant=drive_relay",
                     relay=HTTPException(status_code=502, detail="源站不可达"),
                     ensure=True, switch_on=True)
    assert excinfo.value.status_code == 502
