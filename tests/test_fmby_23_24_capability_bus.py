# -*- coding: utf-8 -*-
"""FMBY #23 能力门控执法 + #24 总线消费端（2026-10-27 第 5 路）。

#23 之前的状态：五能力位只声明不执法——can_list（扫描）、
can_refresh_credentials（刷新重试）、can_generate_play_target（变体
清单）三处有门，can_random_read 与 can_read_sidecar 全仓零调用方
执法，换链直访（无变体参数的老客户端）也不查能力。本文件钉：
声明 False 的提供者对应接口根本不被调（间谍计数为 0），而不是
调了之后才失败。

#24 之前的状态：总线只有生产没有消费——publish_event 发频道，
subscribe() 在生产代码里零调用方。本文件钉：工人消费端订阅频道
→ 事件尾巴 → 管理端读口，整条生产→消费链真实走通；远程控制
整环（命令→websocket 投递→帧到达）已有 test_session_push.py 的
TestClient 实测，本文件不重复，只补消费端与读口。

全部离线：DB 用内存 SQLite，Redis 用进程内替身；开关读数走
monkeypatch（模块级 _ENABLED，与 test_playback_bus.py 同口径）。
"""

import base64
import json
import os
import threading

os.environ.setdefault("DATABASE_TYPE", "sqlite")
os.environ.setdefault("REDIS_ENABLED", "false")

import pytest
from fastapi import Request
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from types import SimpleNamespace

from backend import models
from backend.database import Base
from backend.emby_server import compat_routes
from backend.emby_server import mounts as mnt
from backend.emby_server import playback_bus as bus
from backend.emby_server import playback_bus_admin as bus_admin
from backend.emby_server import models as em
from backend.emby_server.mounts import (
    MountProvider,
    Pan115Mount,
    ProviderCapabilities,
)
from backend.emby_server.mount_sidecar import SidecarMountProvider


def _all_false():
    return ProviderCapabilities()


def _all_true_no_refresh():
    return ProviderCapabilities(
        can_list=True, can_random_read=True, can_read_sidecar=True,
        can_generate_play_target=True, can_refresh_credentials=False,
    )


# ---------------------------------------------------------------- #23：按段读门控

def test_random_read_gate_base_provider_never_touches_disk(tmp_path):
    (tmp_path / "f.mkv").write_bytes(b"0123456789")

    class _NoCap(mnt.LocalMount):
        def capabilities(self):
            return _all_false()

    prov = _NoCap(SimpleNamespace(
        mount_type="local", path=str(tmp_path), config="{}", name="t"))
    with pytest.raises(mnt.MountError, match="can_random_read"):
        prov.open_range("f.mkv", 0, 2)


def test_random_read_gate_remote_provider_never_requests():
    seen = []

    class _NoCapRemote(mnt.RemoteMount):
        def __init__(self):
            pass

        def capabilities(self):
            return _all_false()

        def resolve_play_target(self, rel):
            return mnt.PlayTarget("url", "http://x/f", {})

        def _request(self, method, url, headers=None, **kw):
            seen.append((method, url))
            return SimpleNamespace(status_code=206, content=b"xy")

    with pytest.raises(mnt.MountError, match="can_random_read"):
        _NoCapRemote().open_range("f.mkv", 0, 2)
    assert seen == []


def test_random_read_gate_pan115_never_resolves():
    calls = []

    class _NoCap115(Pan115Mount):
        def __init__(self):
            pass

        def capabilities(self):
            return _all_false()

        def resolve_play_target(self, rel):
            calls.append(rel)
            return mnt.PlayTarget("url", "http://x/115", {})

    with pytest.raises(mnt.MountError, match="can_random_read"):
        _NoCap115().open_range("f.mkv", 0, 2)
    assert calls == []


def test_random_read_gate_sidecar_never_calls_runtime():
    calls = []

    class _FakeRuntimeClient:
        def open_range(self, session_id, path, offset, length):
            calls.append((session_id, path))
            return {"data_b64": base64.b64encode(b"xy").decode(),
                    "byte_offset": 0, "byte_length": 2}

    prov = SidecarMountProvider.__new__(SidecarMountProvider)
    prov._capabilities = _all_false()
    prov.client = _FakeRuntimeClient()
    prov.session_id = "s-1"
    with pytest.raises(mnt.MountError, match="can_random_read"):
        prov.open_range("f.mkv", 0, 2)
    assert calls == []

    prov._capabilities = _all_true_no_refresh()
    assert prov.open_range("f.mkv", 0, 2) == b"xy"
    assert calls == [("s-1", "f.mkv")]


# ---------------------------------------------------------------- #23：旁挂门控

def test_sidecar_gate_method_and_function_refuse_false_provider():
    calls = []

    class _NoCap(MountProvider):
        def __init__(self):
            pass

        def capabilities(self):
            return _all_false()

        def read_text(self, rel):
            calls.append(rel)
            return "text"

    prov = _NoCap()
    with pytest.raises(mnt.MountError, match="can_read_sidecar"):
        prov.read_sidecar_text("a.nfo")
    with pytest.raises(mnt.MountError, match="can_read_sidecar"):
        mnt.read_sidecar_text_from(prov, "a.nfo")
    assert calls == []


def test_sidecar_gate_duck_provider_still_falls_back():
    class _Duck:
        def __init__(self):
            self.read_text_calls = []

        def read_text(self, rel):
            self.read_text_calls.append(rel)
            return "duck-text"

    duck = _Duck()
    assert mnt.read_sidecar_text_from(duck, "a.nfo") == "duck-text"
    assert duck.read_text_calls == ["a.nfo"]


# ---------------------------------------------------------------- #23：换链直访门控

@pytest.fixture()
def mount_db():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False},
        poolclass=StaticPool)
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    yield session
    session.close()
    engine.dispose()


def test_generate_gate_module_resolve_refuses_false_provider(
        mount_db, monkeypatch, tmp_path):
    calls = []

    class _NoCapProvider(MountProvider):
        def capabilities(self):
            return _all_false()

        def resolve_play_target(self, rel):
            calls.append(rel)
            return mnt.PlayTarget("url", "http://x/f", {})

    monkeypatch.setitem(mnt._PROVIDERS, "testnocap", _NoCapProvider)
    mount = em.StorageMount(name="nocap-mount", mount_type="testnocap",
                            path="", config="{}", is_enabled=True)
    mount_db.add(mount)
    mount_db.commit()
    with pytest.raises(mnt.MountError, match="can_generate_play_target"):
        mnt.resolve_play_target(f"mount://{mount.id}/f.mkv", mount_db)
    assert calls == []


def test_generate_gate_module_resolve_strm_uses_sidecar_gate(
        mount_db, monkeypatch):
    """模块换链口对 .strm 按旁挂门控：旁挂 False 即使换链 True
    也挡，且 read_text 根本不被调；反之旁挂 True、换链 False
    的 .strm 提供者不被错杀（见 test_resolve_target_cache）。"""
    reads = []

    class _NoSidecarProvider(MountProvider):
        def capabilities(self):
            return ProviderCapabilities(can_generate_play_target=True)

        def read_text(self, rel):
            reads.append(rel)
            return "http://x/target.mkv"

        def resolve_play_target(self, rel):
            raise AssertionError(".strm 不许调换链口")

    monkeypatch.setitem(mnt._PROVIDERS, "testnoside",
                        _NoSidecarProvider)
    mount = em.StorageMount(name="noside-mount", mount_type="testnoside",
                            path="", config="{}", is_enabled=True)
    mount_db.add(mount)
    mount_db.commit()
    with pytest.raises(mnt.MountError, match="can_read_sidecar"):
        mnt.resolve_play_target(f"mount://{mount.id}/x.strm", mount_db)
    assert reads == []


def test_generate_gate_module_resolve_allows_true_provider(
        mount_db, tmp_path):
    (tmp_path / "f.mkv").write_bytes(b"0123456789")
    mount = em.StorageMount(name="local-mount", mount_type="local",
                            path=str(tmp_path), config="{}", is_enabled=True)
    mount_db.add(mount)
    mount_db.commit()
    target = mnt.resolve_play_target(f"mount://{mount.id}/f.mkv", mount_db)
    assert target.kind == "local"
    assert target.value.endswith("f.mkv")


# ---------------------------------------------------------------- #24：消费端替身与夹具

class _FakePubSub:
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


class _FakeRedis:
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
        return _FakePubSub(self)

    def rpush(self, key, value):
        self._store.setdefault(key, []).append(value)
        return len(self._store[key])

    def lrange(self, key, start, end):
        items = self._store.get(key, [])
        return items[start:end + 1 if end >= 0 else None]

    def ltrim(self, key, start, end):
        items = self._store.get(key, [])
        self._store[key] = items[start:] if end == -1 else items[start:end + 1]


@pytest.fixture()
def fake_redis():
    return _FakeRedis()


@pytest.fixture()
def bus_on(monkeypatch, fake_redis):
    monkeypatch.setattr(bus, "_ENABLED", True)
    monkeypatch.setattr(bus, "_redis_client",
                        lambda client=None: client if client is not None
                        else fake_redis)
    return fake_redis


def _publish_started(fake_redis, session_id):
    event = bus.build_event(bus.EVENT_STARTED, session_id=session_id,
                            user_id=1, item_id=2)
    assert bus.publish_event(event, client=fake_redis) is True
    return event


def test_bus_consume_roundtrip_and_drain_is_at_most_once(
        bus_on, fake_redis):
    pubsub = bus.subscribe(client=fake_redis)
    assert pubsub is not None
    _publish_started(fake_redis, "s-1")
    assert bus.drain_bus_pubsub(pubsub, client=fake_redis) == 1
    # 第二拍已空：at-most-once，同一事件绝不重复进尾巴。
    assert bus.drain_bus_pubsub(pubsub, client=fake_redis) == 0
    events = bus.read_bus_events(client=fake_redis)
    assert [e["type"] for e in events] == ["started"]
    assert events[0]["session_id"] == "s-1"


def test_bus_consume_upsert_session_end_to_end(bus_on, fake_redis, mount_db):
    """生产→消费整环：_upsert_session（真实发布点）发的事件，
    消费端确实收到并写进尾巴——不是只测 publish 本身。"""
    lib = em.Library(guid="lib-bus-consume", name="电影",
                     collection_type="movies", is_enabled=True)
    mount_db.add(lib)
    mount_db.commit()
    item = em.MediaItem(guid="g-bus-consume-0001", library_id=lib.id,
                        item_type="movie", name="消费测试片")
    mount_db.add(item)
    user = models.WebUser(username="bus-consume-user", password_hash="x",
                          is_active=True)
    mount_db.add(user)
    mount_db.commit()
    scope = {
        "type": "http", "http_version": "1.1", "method": "POST",
        "scheme": "http", "path": "/emby/Sessions/Playing",
        "headers": [], "client": ("127.0.0.1", 5000),
        "server": ("t", 80), "query_string": b"",
    }
    # 订阅必须发生在发布之前（频道 at-most-once，事后订阅收不到旧事件）。
    pubsub = bus.subscribe(client=fake_redis)
    session, wrote = compat_routes._upsert_session(
        mount_db, Request(scope), user, item,
        {"PlaySessionId": "bus-consume-1", "PositionTicks": 0,
         "IsPaused": False})
    assert wrote is True
    assert bus.drain_bus_pubsub(pubsub, client=fake_redis) == 1
    events = bus.read_bus_events(client=fake_redis)
    assert [e["type"] for e in events] == ["started"]
    assert events[0]["session_id"] == "bus-consume-1"


def test_bus_consume_skips_invalid_messages(bus_on, fake_redis):
    pubsub = bus.subscribe(client=fake_redis)
    pubsub._queue.append({"type": "message", "channel": bus.BUS_CHANNEL,
                          "data": "not-json"})
    pubsub._queue.append({"type": "message", "channel": "other-channel",
                          "data": json.dumps(
                              bus.build_event(bus.EVENT_STARTED,
                                              session_id="s-x"))})
    pubsub._queue.append({"type": "subscribe", "channel": bus.BUS_CHANNEL,
                          "data": 1})
    assert bus.drain_bus_pubsub(pubsub, client=fake_redis) == 0
    assert bus.read_bus_events(client=fake_redis) == []


def test_bus_consume_caps_tail_at_100(bus_on, fake_redis):
    pubsub = bus.subscribe(client=fake_redis)
    for i in range(105):
        _publish_started(fake_redis, f"s-{i}")
    assert bus.drain_bus_pubsub(pubsub, client=fake_redis) == 105
    events = bus.read_bus_events(client=fake_redis, limit=200)
    assert len(events) == 100
    ids = [e["session_id"] for e in events]
    assert "s-0" not in ids  # 最旧的 5 条被裁掉
    assert ids[0] == "s-104"  # 最新在前


def test_bus_events_endpoint_gates_and_reads(bus_on, fake_redis,
                                            monkeypatch):
    pubsub = bus.subscribe(client=fake_redis)
    _publish_started(fake_redis, "s-ev-1")
    assert bus.drain_bus_pubsub(pubsub, client=fake_redis) == 1
    events = bus_admin.get_events(limit=10, current_admin=SimpleNamespace())
    assert [e["session_id"] for e in events] == ["s-ev-1"]
    monkeypatch.setattr(bus, "_ENABLED", False)
    with pytest.raises(HTTPException) as excinfo:
        bus_admin.get_events(limit=10, current_admin=SimpleNamespace())
    assert excinfo.value.status_code == 503


def test_bus_consumer_loop_exits_on_preset_stop(bus_on, fake_redis):
    stop = threading.Event()
    stop.set()
    # 预置 stop：循环一拍都不跑就退出，不挂起测试。
    bus.run_event_consumer_loop(client=fake_redis, stop_event=stop)


def test_bus_consumer_worker_start_gating(monkeypatch):
    import backend.worker as worker_mod

    monkeypatch.setattr(bus, "_ENABLED", False)
    assert worker_mod.start_playback_bus_consumer() is False
    monkeypatch.setattr(bus, "_ENABLED", True)
    stop = threading.Event()
    stop.set()
    assert worker_mod.start_playback_bus_consumer(stop_event=stop) is True
    worker_mod._playback_bus_consumer_thread.join(timeout=5)
