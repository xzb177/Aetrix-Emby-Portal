# -*- coding: utf-8 -*-
"""暖地址重做批 0 · 仪表先行（goal-35）。

钉死口径（零行为变化，只加字段/写入/打点）：
1. 起播事件暖归因：note_play_start 新增 warm_hits（命中哪几步预热，
   逗号分隔的步骤名子集：plan/quota/exists）与 warm_in_signal
   （是否信号内目标）。本批还没有工人，缺省一律未暖口径：不传即
   NULL（与 variant 老事件 NULL 同口径）；显式传 False 是「已判定
   非信号内」，与 NULL 的「未判定」可区分。
2. total_ms 真落库：note 传入即落；play_timing.PlayTimer.elapsed_ms
   给出请求内已耗毫秒（与 [play-timing] 同一计时器、同一开关：
   关闭时 no-op 返回 None，total_ms 留空，不许拿分支内 resolve 冒充）。
3. PlaybackInfo 尾段分段打点：[play-timing] 行里新增 plan_spawn /
   variants / copy_gate / upstream_lines 段；调用返回值与改前同形
   （MediaSources / PlaySessionId 契约不变）。
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from types import SimpleNamespace

os.environ.setdefault("DATABASE_TYPE", "sqlite")
os.environ.setdefault("REDIS_ENABLED", "false")

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from starlette.requests import Request

from backend import models
from backend.database import Base
from backend.emby_server import api, play_events, play_timing, play_variants
from backend.emby_server import models as em
from backend.emby_server import probe_worker, stream_accel
from backend.emby_server import playback_strategy


# ------------------------------------------------------------ play_events


@pytest.fixture(autouse=True)
def _isolate_play_events():
    play_events.reset_for_tests()
    yield
    play_events.reset_for_tests()


@pytest.fixture()
def ev_db_factory(monkeypatch):
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False},
                           poolclass=StaticPool)
    models.Base.metadata.create_all(engine)
    em.Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    monkeypatch.setattr("backend.database.SessionLocal", factory)
    return factory


@pytest.fixture()
def ev_db(ev_db_factory):
    session = ev_db_factory()
    yield session
    session.close()


def _events(db):
    return db.query(em.PlayStartEvent).order_by(em.PlayStartEvent.id).all()


def test_note_default_warm_attribution_is_unwarmed(ev_db):
    """缺省（本批全部现行调用方）：暖归因两列与 total_ms 均为 NULL。"""
    play_events.note_play_start(
        provider="mount", method="relay", user_id=9, item_id=42,
        result="ok", resolve_ms=123)
    assert play_events.flush_pending() == 1
    row = _events(ev_db)[0]
    assert row.warm_hits is None
    assert row.warm_in_signal is None
    assert row.total_ms is None
    assert row.resolve_ms == 123


def test_note_records_warm_attribution_and_total(ev_db):
    """批 1/2 的读写口径先行：显式传参原样落库。"""
    play_events.note_play_start(
        provider="mount", method="relay", user_id=9, item_id=42,
        result="ok", resolve_ms=123, total_ms=980,
        warm_hits="plan,quota,exists", warm_in_signal=True)
    assert play_events.flush_pending() == 1
    row = _events(ev_db)[0]
    assert row.warm_hits == "plan,quota,exists"
    assert row.warm_in_signal is True
    assert row.total_ms == 980


def test_note_warm_hits_accepts_step_iterable(ev_db):
    """warm_hits 传步骤名序列时按逗号拼存；warm_in_signal=False 落 False。"""
    play_events.note_play_start(
        provider="mount", method="relay", result="ok",
        warm_hits=["plan", "exists"], warm_in_signal=False)
    assert play_events.flush_pending() == 1
    row = _events(ev_db)[0]
    assert row.warm_hits == "plan,exists"
    assert row.warm_in_signal is False


# ------------------------------------------------------------ play_timing


def test_play_timer_elapsed_ms():
    t = play_timing.PlayTimer("stream item=1")
    time.sleep(0.05)
    elapsed = t.elapsed_ms()
    assert isinstance(elapsed, int)
    assert elapsed >= 40


def test_noop_timer_elapsed_is_none():
    assert play_timing._NoopTimer().elapsed_ms() is None


def test_elapsed_ms_helper_tolerates_missing_and_stubs():
    assert play_timing.elapsed_ms(None) is None
    assert play_timing.elapsed_ms(object()) is None  # 无 elapsed_ms 的桩
    t = play_timing.PlayTimer("x")
    assert isinstance(play_timing.elapsed_ms(t), int)


# ------------------------------------------------- PlaybackInfo 尾段打点


@pytest.fixture()
def pi_db():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False},
        poolclass=StaticPool)
    Base.metadata.create_all(engine)
    em.Base.metadata.create_all(engine)
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


def _local_item(guid):
    item = em.MediaItem()
    item.guid = guid
    item.name = "仪表测试片"
    item.file_path = "/data/movie.mkv"
    item.drive_file_id = ""
    item.item_type = "movie"
    item.size = 1_000_000_000
    item.duration_ticks = 36_000_000_000
    item.bitrate = 2_000_000
    return item


def _call_pi(monkeypatch, db, item):
    user = SimpleNamespace(id=101, is_staff=False)
    monkeypatch.setattr(api, "_require_visible_item",
                        lambda db_, u_, iid: item)
    monkeypatch.setattr(api, "ensure_playback_allowed",
                        lambda db_, u_: None)
    monkeypatch.setattr(
        "backend.emby_server.playback_gate.ensure_binding_playback_allowed",
        lambda db_, u_, r=None: None)
    monkeypatch.setattr(probe_worker, "maybe_enqueue",
                        lambda db_, it: None)
    monkeypatch.setattr(
        stream_accel, "rewrite_playback_urls_unified",
        lambda db_, s, base: (s, None))
    monkeypatch.setattr(play_variants, "_ENABLED", True)
    return asyncio.run(api.playback_info(
        item.guid, _request(), user_id="", user=user, db=db))


def test_playback_info_tail_timing_marks_and_response_shape(
        monkeypatch, pi_db, caplog):
    item = _local_item("guid-warm-b0")
    with caplog.at_level(logging.INFO,
                         logger="backend.emby_server.play_timing"):
        result = _call_pi(monkeypatch, pi_db, item)
    # 返回值契约与改前同形（打点不改变返回值）
    assert result["MediaSources"], result
    assert result["MediaSources"][0]["Id"] == "guid-warm-b0"
    assert result["PlaySessionId"]
    # 尾段分段打点在场
    lines = [r.getMessage() for r in caplog.records
             if r.getMessage().startswith("[play-timing] playbackinfo")]
    assert lines, [r.getMessage() for r in caplog.records]
    line = lines[-1]
    for segment in ("plan_spawn=", "variants=", "copy_gate=",
                    "upstream_lines=", "media_source=", "total="):
        assert segment in line, line
