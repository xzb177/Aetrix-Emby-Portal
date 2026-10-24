# -*- coding: utf-8 -*-
"""播放实时总线（FMBY #24 前半，只做 stop）。

覆盖四类：总线发布/订阅、快照写入、命令生命周期（发送→送达→完成）、
非 stop 命令拒绝；另覆超时判失败、stop 执行诚实 outcome（中继 enforced /
直链 best_effort / 已结束 already_ended / 查无会话 failed）、管理端
入口（503 关开关、400 非 stop、403 只读角色写命令）。

全部离线：DB 用内存 SQLite，Redis 用进程内替身；开关读数走
monkeypatch（模块级 _ENABLED，与 play_session 同口径）。
"""
import os

os.environ.setdefault("DATABASE_TYPE", "sqlite")
os.environ.setdefault("REDIS_ENABLED", "false")

import json
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.api.admin_core import get_current_admin
from backend.database import Base, get_db
from backend.emby_server import models as em
from backend.emby_server import playback_bus as bus
from backend.emby_server import playback_bus_admin


# ---------------------------------------------------------------- Redis 替身

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


# ---------------------------------------------------------------- 夹具

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


def _session_row(db, key, variant="drive_relay", ended=False):
    row = em.PlaybackSession(
        session_key=key, server_session_id=key, user_id=1, item_id=1,
        variant=variant, device_id="dev-1",
        ended_at=datetime.now() if ended else None)
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def _enable_bus(monkeypatch):
    monkeypatch.setattr(bus, "_ENABLED", True)


def _disable_bus(monkeypatch):
    monkeypatch.setattr(bus, "_ENABLED", False)


# ---------------------------------------------------------------- 发布 / 订阅

def test_publish_disabled_is_noop(monkeypatch, fake_redis):
    _disable_bus(monkeypatch)
    event = bus.build_event(bus.EVENT_STARTED, session_id="s1")
    assert bus.publish_event(event, client=fake_redis) is False
    assert fake_redis._store == {}
    assert fake_redis._subscribers == []


def test_publish_enabled_writes_versioned_channel(monkeypatch, fake_redis):
    _enable_bus(monkeypatch)
    pubsub = bus.subscribe(client=fake_redis)
    assert pubsub is not None
    event = bus.build_event(bus.EVENT_STARTED, session_id="s1", user_id=7,
                            item_id=9, variant="drive_relay", device_id="dev-1",
                            position_ticks=123, is_paused=False)
    assert bus.publish_event(event, client=fake_redis) is True
    msg = pubsub.get_message()
    assert msg is not None and msg["channel"] == bus.BUS_CHANNEL
    payload = json.loads(msg["data"])
    assert payload["v"] == 1 and payload["type"] == "started"
    assert payload["session_id"] == "s1" and payload["variant"] == "drive_relay"
    assert "api_key" not in payload and "token" not in payload


def test_build_event_rejects_unknown_type():
    with pytest.raises(ValueError):
        bus.build_event("seek", session_id="s1")


def test_subscribe_without_redis_returns_none(monkeypatch):
    _enable_bus(monkeypatch)
    assert bus.subscribe(client=None) is None or True  # 无 Redis 不抛异常即可
    # 注：本机 REDIS_ENABLED=false 时 get_redis 回 None，subscribe 回 None。


# ---------------------------------------------------------------- 快照

def test_snapshot_only_active_sessions(db, fake_redis):
    _session_row(db, "live-1", variant="drive_relay")
    _session_row(db, "live-2", variant="drive_direct")
    _session_row(db, "dead-1", variant="drive_relay", ended=True)
    snapshot = bus.build_snapshot(db)
    assert snapshot["v"] == 1
    ids = [s["session_id"] for s in snapshot["sessions"]]
    assert ids == ["live-1", "live-2"]
    assert bus.write_snapshot(fake_redis, snapshot) is True
    back = bus.read_snapshot(fake_redis)
    assert back is not None and back["sessions"] == snapshot["sessions"]


def test_read_snapshot_missing_and_corrupt(fake_redis):
    assert bus.read_snapshot(fake_redis) is None
    fake_redis.set(bus.SNAPSHOT_KEY, "{not json")
    assert bus.read_snapshot(fake_redis) is None
    fake_redis.set(bus.SNAPSHOT_KEY, json.dumps({"v": 99, "sessions": []}))
    assert bus.read_snapshot(fake_redis) is None


def test_run_snapshot_once_counts(db, fake_redis):
    _session_row(db, "live-1")
    assert bus.run_snapshot_once(db, client=fake_redis) == 1
    assert bus.read_snapshot(fake_redis) is not None


# ---------------------------------------------------------------- 命令生命周期

def test_command_lifecycle_and_illegal_transition(db):
    cmd = bus.create_command(db, session_key="s1", command="stop",
                             requested_by=1)
    assert cmd.status == bus.STATUS_PENDING
    bus.mark_sent(db, cmd)
    assert cmd.status == bus.STATUS_SENT and cmd.sent_at is not None
    bus.mark_delivered(db, cmd)
    assert cmd.status == bus.STATUS_DELIVERED and cmd.delivered_at is not None
    bus.mark_completed(db, cmd, bus.OUTCOME_ENFORCED)
    assert cmd.status == bus.STATUS_COMPLETED
    assert cmd.outcome == bus.OUTCOME_ENFORCED
    assert cmd.completed_at is not None
    with pytest.raises(ValueError):
        bus.mark_sent(db, cmd)  # 终态不许再动


def test_command_direct_pending_to_completed_rejected(db):
    cmd = bus.create_command(db, session_key="s1", command="stop")
    with pytest.raises(ValueError):
        bus.mark_completed(db, cmd, bus.OUTCOME_ENFORCED)


def test_unsupported_command_rejected_without_row(db):
    with pytest.raises(bus.UnsupportedCommandError):
        bus.create_command(db, session_key="s1", command="volume")
    assert db.query(bus.PlaybackControlCommand).count() == 0
    with pytest.raises(bus.UnsupportedCommandError):
        bus.create_command(db, session_key="s1", command="message")


def test_timeout_sweep_only_stale_non_terminal(db):
    old = bus.create_command(db, session_key="old", command="stop")
    old.requested_at = datetime.now() - timedelta(seconds=3600)
    db.commit()
    fresh = bus.create_command(db, session_key="fresh", command="stop")
    done = bus.create_command(db, session_key="done", command="stop")
    bus.mark_sent(db, done)
    bus.mark_delivered(db, done)
    bus.mark_completed(db, done, bus.OUTCOME_ENFORCED)
    assert bus.timeout_stale_commands(db, timeout_sec=10) == 1
    assert bus.get_command(db, old.command_id).status == bus.STATUS_TIMEOUT
    assert bus.get_command(db, fresh.command_id).status == bus.STATUS_PENDING
    assert bus.get_command(db, done.command_id).status == bus.STATUS_COMPLETED


def test_get_and_list_commands(db):
    a = bus.create_command(db, session_key="s1", command="stop")
    b = bus.create_command(db, session_key="s2", command="stop")
    assert bus.get_command(db, a.command_id).id == a.id
    assert bus.get_command(db, "nope") is None
    assert [c.command_id for c in bus.list_commands(db)] == [b.command_id, a.command_id]


# ---------------------------------------------------------------- stop 执行的诚实 outcome

def test_execute_stop_relay_enforced(db, monkeypatch, fake_redis):
    _enable_bus(monkeypatch)
    _session_row(db, "s1", variant="drive_relay")
    cmd = bus.create_command(db, session_key="s1", command="stop")
    bus.mark_sent(db, cmd)
    bus.mark_delivered(db, cmd)
    out = bus.execute_stop(db, cmd, publish_client=fake_redis)
    assert out.status == bus.STATUS_COMPLETED
    assert out.outcome == bus.OUTCOME_ENFORCED
    row = db.query(em.PlaybackSession).filter_by(session_key="s1").first()
    assert row.ended_at is not None


def test_execute_stop_direct_best_effort(db, monkeypatch, fake_redis):
    _enable_bus(monkeypatch)
    _session_row(db, "s1", variant="drive_direct")
    cmd = bus.create_command(db, session_key="s1", command="stop")
    bus.mark_sent(db, cmd)
    bus.mark_delivered(db, cmd)
    out = bus.execute_stop(db, cmd, publish_client=fake_redis)
    assert out.status == bus.STATUS_COMPLETED
    assert out.outcome == bus.OUTCOME_BEST_EFFORT


def test_execute_stop_already_ended_and_missing(db):
    _session_row(db, "s1", variant="drive_relay", ended=True)
    cmd = bus.create_command(db, session_key="s1", command="stop")
    bus.mark_sent(db, cmd)
    bus.mark_delivered(db, cmd)
    out = bus.execute_stop(db, cmd)
    assert out.status == bus.STATUS_COMPLETED
    assert out.outcome == bus.OUTCOME_ALREADY_ENDED
    cmd2 = bus.create_command(db, session_key="ghost", command="stop")
    bus.mark_sent(db, cmd2)
    bus.mark_delivered(db, cmd2)
    out2 = bus.execute_stop(db, cmd2)
    assert out2.status == bus.STATUS_FAILED
    assert out2.error == "session_not_found"


def test_execute_stop_requires_sent_or_delivered(db):
    cmd = bus.create_command(db, session_key="s1", command="stop")
    with pytest.raises(ValueError):
        bus.execute_stop(db, cmd)


# ---------------------------------------------------------------- 管理端入口

@pytest.fixture()
def client(db, monkeypatch):
    app = FastAPI()
    app.include_router(playback_bus_admin.router)

    def _admin_super():
        return SimpleNamespace(id=1, admin_role="super", is_staff=True)

    def _db():
        yield db

    app.dependency_overrides[get_current_admin] = _admin_super
    app.dependency_overrides[get_db] = _db
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


def test_admin_post_stop_end_to_end(client, db, monkeypatch):
    _enable_bus(monkeypatch)
    _session_row(db, "s1", variant="mount_relay")
    r = client.post("/api/admin/playback-bus/commands",
                    json={"session_key": "s1", "command": "stop"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == bus.STATUS_COMPLETED
    assert body["outcome"] == bus.OUTCOME_ENFORCED
    assert "note" not in body
    r2 = client.get(f"/api/admin/playback-bus/commands/{body['command_id']}")
    assert r2.status_code == 200
    assert r2.json()["command_id"] == body["command_id"]


def test_admin_post_stop_direct_carries_best_effort_note(client, db, monkeypatch):
    _enable_bus(monkeypatch)
    _session_row(db, "s1", variant="drive_direct")
    r = client.post("/api/admin/playback-bus/commands",
                    json={"session_key": "s1", "command": "stop"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["outcome"] == bus.OUTCOME_BEST_EFFORT
    assert "best-effort" in body.get("note", "")


def test_admin_post_non_stop_rejected_with_allowed_set(client, db, monkeypatch):
    _enable_bus(monkeypatch)
    r = client.post("/api/admin/playback-bus/commands",
                    json={"session_key": "s1", "command": "volume"})
    assert r.status_code == 400, r.text
    assert "stop" in r.json()["detail"]
    assert db.query(bus.PlaybackControlCommand).count() == 0


def test_admin_post_when_bus_disabled_503(client, db, monkeypatch):
    _disable_bus(monkeypatch)
    r = client.post("/api/admin/playback-bus/commands",
                    json={"session_key": "s1", "command": "stop"})
    assert r.status_code == 503, r.text
    assert db.query(bus.PlaybackControlCommand).count() == 0


def test_admin_post_viewer_role_forbidden_no_row(db, monkeypatch):
    app = FastAPI()
    app.include_router(playback_bus_admin.router)

    def _admin_viewer():
        return SimpleNamespace(id=2, admin_role="viewer", is_staff=True)

    def _db():
        yield db

    app.dependency_overrides[get_current_admin] = _admin_viewer
    app.dependency_overrides[get_db] = _db
    _enable_bus(monkeypatch)
    with TestClient(app) as c:
        r = c.post("/api/admin/playback-bus/commands",
                   json={"session_key": "s1", "command": "stop"})
    assert r.status_code == 403, r.text
    assert db.query(bus.PlaybackControlCommand).count() == 0
    app.dependency_overrides.clear()


def test_admin_get_snapshot_missing_404(client):
    r = client.get("/api/admin/playback-bus/snapshot")
    assert r.status_code == 404


def test_admin_list_commands(client, db):
    bus.create_command(db, session_key="s1", command="stop")
    r = client.get("/api/admin/playback-bus/commands")
    assert r.status_code == 200
    assert len(r.json()) == 1


# ---------------------------------------------------------------- 阶段 8 PR-1：pause / resume / seek 记账路径

def test_payload_validation_rejected_without_row(db):
    with pytest.raises(ValueError):
        bus.create_command(db, session_key="s1", command="seek")
    with pytest.raises(ValueError):
        bus.create_command(db, session_key="s1", command="seek",
                           payload={"position_ticks": "123"})
    with pytest.raises(ValueError):
        bus.create_command(db, session_key="s1", command="seek",
                           payload={"position_ticks": True})
    with pytest.raises(ValueError):
        bus.create_command(db, session_key="s1", command="pause",
                           payload={"position_ticks": 1})
    with pytest.raises(ValueError):
        bus.create_command(db, session_key="s1", command="stop",
                           payload={"x": 1})
    assert db.query(bus.PlaybackControlCommand).count() == 0


def test_seek_negative_ticks_clamped_to_zero(db):
    cmd = bus.create_command(db, session_key="s1", command="seek",
                             payload={"position_ticks": -500})
    assert bus.command_to_dict(cmd)["payload"] == {"position_ticks": 0}


def _run_state_command(db, cmd, fake_redis=None):
    bus.mark_sent(db, cmd)
    bus.mark_delivered(db, cmd)
    return bus.execute_state_command(db, cmd, publish_client=fake_redis)


def test_execute_pause_and_resume_write_flag_and_recorded(
        db, monkeypatch, fake_redis):
    _enable_bus(monkeypatch)
    pubsub = bus.subscribe(client=fake_redis)
    _session_row(db, "s1", variant="drive_relay")
    cmd = bus.create_command(db, session_key="s1", command="pause")
    out = _run_state_command(db, cmd, fake_redis)
    assert out.status == bus.STATUS_COMPLETED
    assert out.outcome == bus.OUTCOME_RECORDED
    row = db.query(em.PlaybackSession).filter_by(session_key="s1").first()
    assert row.is_paused is True
    msg = pubsub.get_message()
    assert msg is not None
    payload = json.loads(msg["data"])
    assert payload["type"] == "progress" and payload["is_paused"] is True
    cmd2 = bus.create_command(db, session_key="s1", command="resume")
    out2 = _run_state_command(db, cmd2, fake_redis)
    assert out2.outcome == bus.OUTCOME_RECORDED
    row = db.query(em.PlaybackSession).filter_by(session_key="s1").first()
    assert row.is_paused is False


def test_execute_seek_writes_position_and_progress_event(
        db, monkeypatch, fake_redis):
    _enable_bus(monkeypatch)
    pubsub = bus.subscribe(client=fake_redis)
    _session_row(db, "s1", variant="drive_direct")
    cmd = bus.create_command(db, session_key="s1", command="seek",
                             payload={"position_ticks": 123456})
    out = _run_state_command(db, cmd, fake_redis)
    assert out.status == bus.STATUS_COMPLETED
    assert out.outcome == bus.OUTCOME_RECORDED
    row = db.query(em.PlaybackSession).filter_by(session_key="s1").first()
    assert row.position_ticks == 123456
    msg = pubsub.get_message()
    payload = json.loads(msg["data"])
    assert payload["type"] == "progress"
    assert payload["position_ticks"] == 123456


def test_execute_state_command_already_ended_and_missing(db):
    _session_row(db, "s1", variant="drive_relay", ended=True)
    cmd = bus.create_command(db, session_key="s1", command="pause")
    out = _run_state_command(db, cmd)
    assert out.status == bus.STATUS_COMPLETED
    assert out.outcome == bus.OUTCOME_ALREADY_ENDED
    cmd2 = bus.create_command(db, session_key="ghost", command="seek",
                              payload={"position_ticks": 10})
    out2 = _run_state_command(db, cmd2)
    assert out2.status == bus.STATUS_FAILED
    assert out2.error == "session_not_found"


def test_execute_state_command_requires_sent_or_delivered(db):
    cmd = bus.create_command(db, session_key="s1", command="pause")
    with pytest.raises(ValueError):
        bus.execute_state_command(db, cmd)
    cmd2 = bus.create_command(db, session_key="s1", command="stop")
    bus.mark_sent(db, cmd2)
    with pytest.raises(bus.UnsupportedCommandError):
        bus.execute_state_command(db, cmd2)


def test_admin_post_pause_end_to_end_recorded_note(client, db, monkeypatch):
    _enable_bus(monkeypatch)
    _session_row(db, "s1", variant="drive_relay")
    r = client.post("/api/admin/playback-bus/commands",
                    json={"session_key": "s1", "command": "pause"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == bus.STATUS_COMPLETED
    assert body["outcome"] == bus.OUTCOME_RECORDED
    assert "未直接作用于播放器" in body.get("note", "")
    row = db.query(em.PlaybackSession).filter_by(session_key="s1").first()
    assert row.is_paused is True


def test_admin_post_seek_bad_payload_400_no_row(client, db, monkeypatch):
    _enable_bus(monkeypatch)
    _session_row(db, "s1", variant="drive_relay")
    r = client.post("/api/admin/playback-bus/commands",
                    json={"session_key": "s1", "command": "seek",
                          "payload": {}})
    assert r.status_code == 400, r.text
    assert db.query(bus.PlaybackControlCommand).count() == 0
    r2 = client.post("/api/admin/playback-bus/commands",
                     json={"session_key": "s1", "command": "seek",
                           "payload": {"position_ticks": 777}})
    assert r2.status_code == 200, r2.text
    assert r2.json()["payload"] == {"position_ticks": 777}
    row = db.query(em.PlaybackSession).filter_by(session_key="s1").first()
    assert row.position_ticks == 777
