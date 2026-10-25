"""阶段 8 投递层测试：SendCommand 经 websocket 推送到播放器（不做轮询）。

夹具照 tests/test_playback_bus.py 的内存 SQLite 模式：同一 StaticPool
会话同时供 HTTP 依赖与 websocket 握手（握手 DB 会话用 monkeypatch
注入夹具会话，模拟生产短命会话“查完即关”）。

握手用 api_key=真实 token 行（EmbyApiToken 存 SHA256 哈希，expires_at
为空 = 历史 token 永不过期），与生产 resolve_token 口径一致。
"""

import os

os.environ.setdefault("DATABASE_TYPE", "sqlite")
os.environ.setdefault("REDIS_ENABLED", "false")

from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.database import Base, get_db
from backend.emby_server import models as em
from backend.emby_server import playback_bus as bus
from backend.emby_server import session_push as _push
from backend.emby_server import session_routes as _sr
from backend.emby_server.auth import get_emby_user, hash_emby_token
from backend.models import WebUser

RAW_TOKEN = "test-token-1"


@pytest.fixture
def db():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    yield session
    session.close()
    engine.dispose()


@pytest.fixture
def app(db, monkeypatch):
    monkeypatch.setattr(bus, "_ENABLED", True)

    def _owner():
        return SimpleNamespace(id=1, username="push_user", is_staff=True)

    application = FastAPI()
    application.post("/Sessions/{session_key}/Command")(_sr.send_session_command)
    application.post("/emby/Sessions/{session_key}/Command")(_sr.send_session_command)
    application.websocket("/embywebsocket")(_push.emby_websocket)
    application.dependency_overrides[get_emby_user] = _owner
    application.dependency_overrides[get_db] = lambda: db
    monkeypatch.setattr(_push, "_db_session", lambda: db)
    yield application


@pytest.fixture
def client(app):
    return TestClient(app)


def _session_row(db, key, user_id=1, device_id="dev-1", variant="drive_relay"):
    row = em.PlaybackSession(session_key=key, server_session_id=key,
                             user_id=user_id, item_id=1, variant=variant,
                             device_id=device_id)
    db.add(row)
    db.commit()
    return row


def _user_and_token(db, user_id=1, device_id="dev-1", raw=RAW_TOKEN):
    db.add(WebUser(id=user_id, username=f"push_user_{user_id}",
                   password_hash="x", is_active=True))
    db.add(em.EmbyApiToken(token=hash_emby_token(raw), user_id=user_id,
                           device_id=device_id, is_revoked=False,
                           expires_at=None))
    db.commit()


def test_push_pause_reaches_connected_player(db, client):
    _user_and_token(db)
    _session_row(db, "s1")
    with client.websocket_connect(
            f"/embywebsocket?api_key={RAW_TOKEN}&deviceId=dev-1") as ws:
        r = client.post("/emby/Sessions/s1/Command", json={"Name": "Pause"})
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["delivery"]["pushed_to_player_connections"] == 1
        assert body["delivery"]["player_connected"] is True
        assert body["outcome"] == bus.OUTCOME_RECORDED
        frame = ws.receive_json()
        assert frame == {"MessageType": "GeneralCommand",
                         "Data": {"Name": "Pause", "Arguments": {}}}
    row = db.query(em.PlaybackSession).filter_by(session_key="s1").first()
    assert row.is_paused is True


def test_push_stop_reaches_connected_player(db, client):
    _user_and_token(db)
    _session_row(db, "s1")
    with client.websocket_connect(
            f"/embywebsocket?api_key={RAW_TOKEN}&deviceId=dev-1") as ws:
        r = client.post("/Sessions/s1/Command", json={"Name": "Stop"})
        assert r.status_code == 200, r.text
        assert r.json()["delivery"]["pushed_to_player_connections"] == 1
        frame = ws.receive_json()
        assert frame["Data"]["Name"] == "Stop"
    row = db.query(em.PlaybackSession).filter_by(session_key="s1").first()
    assert row.ended_at is not None


def test_push_seek_forwards_emby_arguments(db, client):
    _user_and_token(db)
    _session_row(db, "s1")
    with client.websocket_connect(
            f"/embywebsocket?api_key={RAW_TOKEN}&deviceId=dev-1") as ws:
        r = client.post("/emby/Sessions/s1/Command",
                        json={"Name": "Seek", "Arguments": {"SeekPositionTicks": 98765}})
        assert r.status_code == 200, r.text
        assert r.json()["delivery"]["pushed_to_player_connections"] == 1
        frame = ws.receive_json()
        assert frame["Data"] == {"Name": "Seek",
                                 "Arguments": {"SeekPositionTicks": 98765}}
    row = db.query(em.PlaybackSession).filter_by(session_key="s1").first()
    assert row.position_ticks == 98765


def test_push_without_player_reports_zero_but_still_recorded(db, client):
    _user_and_token(db)
    _session_row(db, "s1")
    r = client.post("/emby/Sessions/s1/Command", json={"Name": "Pause"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["delivery"] == {"pushed_to_player_connections": 0,
                                "player_connected": False}
    assert body["status"] == bus.STATUS_COMPLETED
    assert body["outcome"] == bus.OUTCOME_RECORDED
    assert "未直接作用于播放器" in body["note"]


def test_push_note_states_pushed_count_when_connected(db, client):
    _user_and_token(db)
    _session_row(db, "s1")
    with client.websocket_connect(
            f"/embywebsocket?api_key={RAW_TOKEN}&deviceId=dev-1"):
        r = client.post("/emby/Sessions/s1/Command", json={"Name": "Pause"})
        assert r.status_code == 200, r.text
        assert "1 个播放器连接" in r.json()["note"]
        assert "尚无回执" in r.json()["note"]


def test_websocket_rejects_bad_token(db, client):
    _user_and_token(db)
    _session_row(db, "s1")
    with pytest.raises(Exception):
        with client.websocket_connect("/embywebsocket?api_key=wrong-token"):
            pass


def test_websocket_session_key_binding(db, client):
    _user_and_token(db)
    _session_row(db, "s1")
    # sessionKey 绑定：以会话行 device_id 为准，建连成功且能收到投递。
    with client.websocket_connect(
            f"/embywebsocket?api_key={RAW_TOKEN}&sessionKey=s1") as ws:
        r = client.post("/emby/Sessions/s1/Command", json={"Name": "Pause"})
        assert r.status_code == 200, r.text
        assert r.json()["delivery"]["pushed_to_player_connections"] == 1
        assert ws.receive_json()["Data"]["Name"] == "Pause"


def test_websocket_rejects_foreign_session_key(db, client):
    _user_and_token(db)
    _session_row(db, "other", user_id=2, device_id="dev-2")
    with pytest.raises(Exception):
        with client.websocket_connect(
                f"/embywebsocket?api_key={RAW_TOKEN}&sessionKey=other"):
            pass


def test_push_after_disconnect_reports_zero(db, client):
    _user_and_token(db)
    _session_row(db, "s1")
    with client.websocket_connect(
            f"/embywebsocket?api_key={RAW_TOKEN}&deviceId=dev-1"):
        assert _push.connection_count(1, "dev-1") == 1
    assert _push.connection_count(1, "dev-1") == 0
    r = client.post("/emby/Sessions/s1/Command", json={"Name": "Pause"})
    assert r.status_code == 200, r.text
    assert r.json()["delivery"]["pushed_to_player_connections"] == 0
