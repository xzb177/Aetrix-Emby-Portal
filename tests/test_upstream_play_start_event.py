# -*- coding: utf-8 -*-
"""上游播放口起播打点（#25-4 补记）。

钉死：
- 上游口成功下发 302 时记一条 play_start_events（provider=upstream，
  method=upstream，result=ok），走 play_events 既有口径与字段；
- 上游口没有本地条目：item_id 恒为空（upstream_item_id 是对方服的
  条目 id，绝不写进本地数值列），mount_id 为空，user_id 取自播放用户；
- 打点永不打断起播：note 本身抛错时路由仍返回 302；
- 失败路径不记（本批边界）：解析失败回结构化错误时不产生事件。
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

from backend import models
from backend.emby_server import api as emby_api
from backend.emby_server import async_db
from backend.emby_server import models as em
from backend.emby_server import play_events
from backend.emby_server import upstream_playback as up


@pytest.fixture(autouse=True)
def _isolate_play_events():
    play_events.reset_for_tests()
    yield
    play_events.reset_for_tests()


@pytest.fixture()
def db_factory(monkeypatch):
    """内存 SQLite + 把 play_events 落库的 SessionLocal 指向它。"""
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False},
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


def _events(db):
    return db.query(em.PlayStartEvent).order_by(em.PlayStartEvent.id).all()


def test_helper_notes_success_event(db):
    emby_api._note_upstream_play_start_safe(9, 123)
    assert play_events.flush_pending() == 1
    rows = _events(db)
    assert len(rows) == 1
    row = rows[0]
    assert row.provider == "upstream"
    assert row.method == "upstream"
    assert row.user_id == 9
    assert row.item_id is None
    assert row.mount_id is None
    assert row.result == "ok"
    assert row.error_kind is None
    assert row.resolve_ms == 123


def test_helper_never_raises(monkeypatch):
    def _bad_note(**kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(play_events, "note_play_start", _bad_note)
    emby_api._note_upstream_play_start_safe(9, 5)  # 不抛


async def _noop_run_db(fn, *args, **kwargs):
    return None


async def _sync_run(fn, *args, **kwargs):
    return fn(*args, **kwargs)


def test_route_success_notes_event(db, monkeypatch):
    monkeypatch.setattr(async_db, "run_db", _noop_run_db)
    monkeypatch.setattr(emby_api, "run_in_threadpool", _sync_run)
    monkeypatch.setattr(
        up, "resolve_upstream_stream",
        lambda *a, **k: "https://up.example.com/Videos/x/stream?static=true")
    resp = asyncio.run(emby_api.upstream_stream(
        1, "x", SimpleNamespace(query_params={}),
        SimpleNamespace(id=9), db))
    assert resp.status_code == 302
    assert play_events.flush_pending() == 1
    rows = _events(db)
    assert len(rows) == 1
    assert rows[0].provider == "upstream"
    assert rows[0].user_id == 9
    assert rows[0].result == "ok"
    assert rows[0].resolve_ms is not None and rows[0].resolve_ms >= 0


def test_route_failure_notes_nothing(db, monkeypatch):
    monkeypatch.setattr(async_db, "run_db", _noop_run_db)
    monkeypatch.setattr(emby_api, "run_in_threadpool", _sync_run)

    def _fail(*a, **k):
        raise up.UpstreamPlaybackError(
            404, "upstream_item_not_found", "上游已无此条目", source_id=1)

    monkeypatch.setattr(up, "resolve_upstream_stream", _fail)
    resp = asyncio.run(emby_api.upstream_stream(
        1, "x", SimpleNamespace(query_params={}),
        SimpleNamespace(id=9), db))
    assert resp.status_code == 404
    assert play_events.flush_pending() == 0
    assert _events(db) == []
