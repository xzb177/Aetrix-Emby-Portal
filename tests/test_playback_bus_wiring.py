# -*- coding: utf-8 -*-
"""播放实时总线接线（FMBY #24 接线 PR）。

覆盖接线四项，不碰 #1227 已覆的骨架逻辑：
- 管理路由挂载：backend/main.py 注册了 playback-bus 路由；
- 发布点：compat_routes._upsert_session 写库后发 started / progress /
  ended，节流未写库不发、开关关时不发；起播打点处不发（本文件不
  测打点，只钉唯一发布点在 _upsert_session）；
- 快照工人开关门控：worker.start_playback_bus_snapshot 关时 False
  不起线程、开时起 daemon 线程跑 run_snapshot_loop；
- 转码分支已清：transcode_hls 不在 enforced 集，stop 回 best_effort。

全部离线：DB 用内存 SQLite，Redis 用进程内替身；开关读数走
monkeypatch（模块级 _ENABLED，与 test_playback_bus.py 同口径）。
"""
import os

os.environ.setdefault("DATABASE_TYPE", "sqlite")
os.environ.setdefault("REDIS_ENABLED", "false")

import json
import threading

import pytest
from fastapi import Request
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend import models
from backend.database import Base
from backend.emby_server import models as em
from backend.emby_server import compat_routes
from backend.emby_server import playback_bus as bus


# ---------------------------------------------------------------- Redis 替身（与 test_playback_bus.py 同款行为）

class FakePubSub:
    def __init__(self, redis):
        self._redis = redis
        self._channels = set()
        self._queue = []

    def subscribe(self, channel):
        self._channels.add(channel)
        self._redis._subscribers.append((channel, self))

    def get_message(self, timeout=None):
        if self._queue:
            return self._queue.pop(0)
        return None


class FakeRedis:
    def __init__(self):
        self._store = {}
        self._subscribers = []

    def get(self, key):
        return self._store.get(key)

    def set(self, key, value, ex=None):
        self._store[key] = value
        return True

    def publish(self, channel, message):
        for ch, sub in self._subscribers:
            if ch == channel:
                sub._queue.append({"type": "message", "channel": channel,
                                   "data": message})
        return 1

    def pubsub(self):
        return FakePubSub(self)


@pytest.fixture()
def db():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False},
        poolclass=StaticPool)
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    session = factory()
    yield session
    session.close()


@pytest.fixture()
def fake_redis():
    return FakeRedis()


def _setup_play(db):
    lib = em.Library(guid="lib-bus-wiring", name="电影",
                     collection_type="movies", is_enabled=True)
    db.add(lib)
    db.commit()
    item = em.MediaItem(guid="g-bus-wiring-0001", library_id=lib.id,
                        item_type="movie", name="接线测试片")
    db.add(item)
    user = models.WebUser(username="bus-wiring-user", password_hash="x",
                          is_active=True)
    db.add(user)
    db.commit()
    return user, item


def _request():
    scope = {
        "type": "http", "http_version": "1.1", "method": "POST",
        "scheme": "http", "path": "/emby/Sessions/Playing",
        "headers": [], "client": ("127.0.0.1", 5000),
        "server": ("t", 80), "query_string": b"",
    }
    return Request(scope)


def _drain(pubsub):
    out = []
    while True:
        msg = pubsub.get_message()
        if msg is None:
            return out
        out.append(json.loads(msg["data"]))


# ---------------------------------------------------------------- 挂载与建表

def test_command_table_on_base_metadata():
    # 路由挂载即导入 playback_bus 模型；init_db create_all 随既有表
    # 建出 playback_control_commands（部署验收：生产 PG 亲验新表）。
    assert "playback_control_commands" in Base.metadata.tables


def _all_paths(routes):
    # 主应用的 include_router 结果包在 _IncludedRouter 里（original_router
    # 才是原 APIRouter），需递归展开，否则只能看到 4 条直挂路由。
    out = set()
    for r in routes:
        if hasattr(r, "path"):
            out.add(r.path)
        inner = getattr(r, "routes", None)
        if inner:
            out |= _all_paths(inner)
        orig = getattr(r, "original_router", None)
        if orig is not None and hasattr(orig, "routes"):
            out |= _all_paths(orig.routes)
    return out


def test_main_app_mounts_playback_bus_router():
    from backend.main import app

    paths = _all_paths(app.routes)
    assert "/api/admin/playback-bus/commands" in paths
    assert "/api/admin/playback-bus/commands/{command_id}" in paths
    assert "/api/admin/playback-bus/snapshot" in paths


# ---------------------------------------------------------------- 发布点：写库后发

def test_upsert_publishes_started_progress_ended(db, monkeypatch,
                                                 fake_redis):
    monkeypatch.setattr(bus, "_ENABLED", True)
    monkeypatch.setattr(bus, "_redis_client",
                        lambda client=None: fake_redis)
    pubsub = bus.subscribe(client=fake_redis)
    assert pubsub is not None
    user, item = _setup_play(db)
    req = _request()

    # 新会话 → started
    session, wrote = compat_routes._upsert_session(
        db, req, user, item,
        {"PlaySessionId": "bus-wiring-1", "PositionTicks": 0,
         "IsPaused": False})
    assert wrote is True
    events = _drain(pubsub)
    assert [e["type"] for e in events] == ["started"]
    assert events[0]["session_id"] == "bus-wiring-1"
    assert events[0]["user_id"] == user.id
    assert events[0]["item_id"] == item.id
    assert "api_key" not in events[0] and "token" not in events[0]

    # 位置跳变（≥30 秒 seek 阈值）→ progress
    session, wrote = compat_routes._upsert_session(
        db, req, user, item,
        {"PlaySessionId": "bus-wiring-1", "PositionTicks": 400_000_000,
         "IsPaused": False})
    assert wrote is True
    events = _drain(pubsub)
    assert [e["type"] for e in events] == ["progress"]
    assert events[0]["position_ticks"] == 400_000_000

    # 节流内同位置重复上报 → 未写库 → 不发
    session, wrote = compat_routes._upsert_session(
        db, req, user, item,
        {"PlaySessionId": "bus-wiring-1", "PositionTicks": 400_000_000,
         "IsPaused": False})
    assert wrote is False
    assert _drain(pubsub) == []

    # 结束上报 → ended，会话行置 ended_at
    session, wrote = compat_routes._upsert_session(
        db, req, user, item,
        {"PlaySessionId": "bus-wiring-1", "PositionTicks": 400_000_000,
         "IsPaused": False}, ended=True)
    assert wrote is True
    events = _drain(pubsub)
    assert [e["type"] for e in events] == ["ended"]
    assert session.ended_at is not None


def test_upsert_publishes_nothing_when_bus_disabled(db, monkeypatch,
                                                    fake_redis):
    monkeypatch.setattr(bus, "_ENABLED", False)
    monkeypatch.setattr(bus, "_redis_client",
                        lambda client=None: fake_redis)
    pubsub = bus.subscribe(client=fake_redis)
    user, item = _setup_play(db)
    session, wrote = compat_routes._upsert_session(
        db, _request(), user, item,
        {"PlaySessionId": "bus-wiring-off", "PositionTicks": 0,
         "IsPaused": False})
    assert wrote is True  # 会话照常写库，只是不发总线
    assert _drain(pubsub) == []


# ---------------------------------------------------------------- 转码分支已清

def test_transcode_variant_not_enforced(db, monkeypatch, fake_redis):
    assert "transcode_hls" not in bus._ENFORCED_VARIANTS
    assert bus._ENFORCED_VARIANTS == {"drive_relay", "mount_relay",
                                      "local_file"}
    monkeypatch.setattr(bus, "_ENABLED", True)
    row = em.PlaybackSession(
        session_key="s-trans", server_session_id="s-trans", user_id=1,
        item_id=1, variant="transcode_hls", device_id="dev-1")
    db.add(row)
    db.commit()
    cmd = bus.create_command(db, session_key="s-trans", command="stop")
    bus.mark_sent(db, cmd)
    bus.mark_delivered(db, cmd)
    out = bus.execute_stop(db, cmd, publish_client=fake_redis)
    assert out.status == bus.STATUS_COMPLETED
    assert out.outcome == bus.OUTCOME_BEST_EFFORT


# ---------------------------------------------------------------- 快照工人开关门控

def test_worker_snapshot_gate(monkeypatch):
    import backend.worker as worker_mod

    # 关时：返回 False，不起线程
    monkeypatch.setattr(bus, "_ENABLED", False)
    worker_mod._playback_bus_snapshot_thread = None
    assert worker_mod.start_playback_bus_snapshot(
        stop_event=threading.Event()) is False
    assert worker_mod._playback_bus_snapshot_thread is None

    # 开时：起 daemon 线程，目标是 run_snapshot_loop，传 SessionLocal
    monkeypatch.setattr(bus, "_ENABLED", True)
    calls = []
    release = threading.Event()

    def _fake_loop(db_factory, interval_sec=None, stop_event=None):
        from backend.database import SessionLocal

        calls.append((db_factory, stop_event))
        release.wait(timeout=10)

    monkeypatch.setattr(bus, "run_snapshot_loop", _fake_loop)
    stop_ev = threading.Event()
    try:
        assert worker_mod.start_playback_bus_snapshot(
            stop_event=stop_ev) is True
        t = worker_mod._playback_bus_snapshot_thread
        assert t is not None and t.daemon is True
        assert calls and calls[0][1] is stop_ev
        from backend.database import SessionLocal

        assert calls[0][0] is SessionLocal
    finally:
        release.set()
        t = worker_mod._playback_bus_snapshot_thread
        if t is not None:
            t.join(timeout=5)
        worker_mod._playback_bus_snapshot_thread = None
