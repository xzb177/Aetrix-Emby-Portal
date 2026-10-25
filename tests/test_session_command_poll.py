"""阶段 8 消费端：播放器轮询口（GET Sessions/{id}/Commands）。

只读口：轮询不改任何命令状态（不把“被轮询到”写成 sent/delivered）；
鉴权与遥控口同口径（只能拉自己的会话，管理员可拉任意会话）；
总线关时 503；会话不存在 404。
"""
from datetime import datetime
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
from backend.emby_server import session_routes
from backend.emby_server.auth import get_emby_user


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


def _session_row(db, key, user_id=1, ended=False):
    row = em.PlaybackSession(
        session_key=key, server_session_id=key, user_id=user_id, item_id=1,
        variant="drive_relay", device_id="dev-1",
        ended_at=datetime.now() if ended else None)
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def _enable_bus(monkeypatch):
    monkeypatch.setattr(bus, "_ENABLED", True)


def _disable_bus(monkeypatch):
    monkeypatch.setattr(bus, "_ENABLED", False)


def _make_app(db, user):
    app = FastAPI()
    app.get("/Sessions/{session_key}/Commands")(session_routes.poll_session_commands)
    app.get("/emby/Sessions/{session_key}/Commands")(session_routes.poll_session_commands)
    app.dependency_overrides[get_emby_user] = lambda: user

    def _db():
        yield db

    app.dependency_overrides[get_db] = _db
    return app


@pytest.fixture()
def poll_client(db):
    owner = SimpleNamespace(id=1, admin_role="super", is_staff=True)
    app = _make_app(db, owner)
    with TestClient(app) as c:
        yield SimpleNamespace(client=c, app=app)
    app.dependency_overrides.clear()


def _three_commands(db):
    c1 = bus.create_command(db, session_key="s1", command="pause",
                            requested_by=1, device_id="dev-1")
    c2 = bus.create_command(db, session_key="s1", command="resume",
                            requested_by=1, device_id="dev-1")
    bus.mark_sent(db, c2)
    bus.mark_delivered(db, c2)
    bus.mark_completed(db, c2, bus.OUTCOME_RECORDED)
    c3 = bus.create_command(db, session_key="s1", command="seek",
                            requested_by=1, device_id="dev-1",
                            payload={"position_ticks": 98765})
    return c1, c2, c3


def test_poll_bus_disabled_503(poll_client, monkeypatch):
    _disable_bus(monkeypatch)
    r = poll_client.client.get("/Sessions/s1/Commands")
    assert r.status_code == 503, r.text


def test_poll_unknown_session_404(poll_client, monkeypatch):
    _enable_bus(monkeypatch)
    r = poll_client.client.get("/Sessions/nope/Commands")
    assert r.status_code == 404, r.text


def test_poll_other_user_forbidden(db, monkeypatch):
    _enable_bus(monkeypatch)
    _session_row(db, "s1", user_id=1)
    other = SimpleNamespace(id=2, admin_role=None, is_staff=False)
    app = _make_app(db, other)
    with TestClient(app) as c:
        r = c.get("/Sessions/s1/Commands")
    assert r.status_code == 403, r.text
    app.dependency_overrides.clear()


def test_poll_staff_can_view_others(poll_client, db, monkeypatch):
    _enable_bus(monkeypatch)
    _session_row(db, "s2", user_id=2)
    bus.create_command(db, session_key="s2", command="pause",
                       requested_by=1, device_id="dev-1")
    r = poll_client.client.get("/Sessions/s2/Commands")
    assert r.status_code == 200, r.text
    assert r.json()["TotalRecordCount"] == 1


def test_poll_returns_commands_in_issue_order(poll_client, db, monkeypatch):
    _enable_bus(monkeypatch)
    _session_row(db, "s1")
    _three_commands(db)
    r = poll_client.client.get("/Sessions/s1/Commands")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["TotalRecordCount"] == 3
    assert body["StartIndex"] == 0
    assert [i["command"] for i in body["Items"]] == ["pause", "resume", "seek"]
    assert [i["status"] for i in body["Items"]] == [
        bus.STATUS_PENDING, bus.STATUS_COMPLETED, bus.STATUS_PENDING]
    assert body["Items"][2]["payload"] == {"position_ticks": 98765}
    assert body["Items"][1]["outcome"] == bus.OUTCOME_RECORDED


def test_poll_is_read_only(poll_client, db, monkeypatch):
    _enable_bus(monkeypatch)
    _session_row(db, "s1")
    c1, c2, _ = _three_commands(db)
    r = poll_client.client.get("/Sessions/s1/Commands")
    assert r.status_code == 200, r.text
    db.refresh(c1)
    db.refresh(c2)
    assert c1.status == bus.STATUS_PENDING
    assert c2.status == bus.STATUS_COMPLETED


def test_poll_pagination(poll_client, db, monkeypatch):
    _enable_bus(monkeypatch)
    _session_row(db, "s1")
    _three_commands(db)
    r = poll_client.client.get("/Sessions/s1/Commands?Limit=2")
    assert r.status_code == 200, r.text
    assert r.json()["TotalRecordCount"] == 3
    assert len(r.json()["Items"]) == 2
    r2 = poll_client.client.get("/Sessions/s1/Commands?StartIndex=2&Limit=2")
    assert r2.status_code == 200, r2.text
    assert len(r2.json()["Items"]) == 1
    assert r2.json()["Items"][0]["command"] == "seek"


def test_poll_emby_prefix_same_shape(poll_client, db, monkeypatch):
    _enable_bus(monkeypatch)
    _session_row(db, "s1")
    _three_commands(db)
    r = poll_client.client.get("/emby/Sessions/s1/Commands")
    assert r.status_code == 200, r.text
    assert r.json()["TotalRecordCount"] == 3


def test_poll_does_not_leak_other_session(poll_client, db, monkeypatch):
    _enable_bus(monkeypatch)
    _session_row(db, "s1")
    _session_row(db, "s2")
    bus.create_command(db, session_key="s1", command="pause",
                       requested_by=1, device_id="dev-1")
    bus.create_command(db, session_key="s2", command="pause",
                       requested_by=1, device_id="dev-1")
    r = poll_client.client.get("/Sessions/s1/Commands")
    assert r.status_code == 200, r.text
    assert r.json()["TotalRecordCount"] == 1
    assert r.json()["Items"][0]["session_key"] == "s1"
