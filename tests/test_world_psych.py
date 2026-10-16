import os

os.environ.setdefault("DATABASE_TYPE", "sqlite")
os.environ.setdefault("REDIS_ENABLED", "false")

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend import models
from backend.api import panel as panel_api
from backend.emby_server import models as emby_models
from backend.integrations import world_copy, world_streak

_POOL_TEXT = world_copy.SPEC["fields"][0]["default"]


def _now_local_naive():
    return datetime.now(ZoneInfo("Asia/Shanghai")).replace(tzinfo=None)


@pytest.fixture()
def db():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    models.Base.metadata.create_all(engine)
    emby_models.Base.metadata.create_all(engine)
    TestingSessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    session = TestingSessionLocal()
    try:
        yield session
    finally:
        session.close()


@pytest.fixture()
def user(db):
    u = models.WebUser(username="psych_test", password_hash="x", is_active=True)
    db.add(u)
    db.commit()
    db.refresh(u)
    return u


def _make_app(db_session, current_user):
    app = FastAPI()
    app.include_router(panel_api.router)

    def _get_db():
        return db_session

    def _get_current_user():
        return current_user

    app.dependency_overrides[panel_api.get_db] = _get_db
    app.dependency_overrides[panel_api.get_current_user] = _get_current_user
    return app


def test_parse_milestones():
    assert world_streak.parse_milestones("7,30,100") == [7, 30, 100]
    assert world_streak.parse_milestones(" 7, 7 ,30") == [7, 30]
    assert world_streak.parse_milestones("7,x,-5,0,30") == [7, 30]
    assert world_streak.parse_milestones("") == []
    assert world_streak.parse_milestones("abc") == []


def test_next_milestone():
    milestones = [7, 30, 100, 200, 365]
    assert world_streak.next_milestone(6, milestones) == (7, 1)
    assert world_streak.next_milestone(7, milestones) == (30, 23)
    assert world_streak.next_milestone(365, milestones) is None
    assert world_streak.next_milestone(400, milestones) is None
    assert world_streak.next_milestone(5, []) is None


def test_parse_and_pick_line():
    pool = world_copy.parse_lines(_POOL_TEXT)
    for daypart in ("dawn", "day", "dusk", "night"):
        assert len(pool[daypart]) == 3

    first = world_copy.pick_line(_POOL_TEXT, "dawn", 0)
    second = world_copy.pick_line(_POOL_TEXT, "dawn", 0)
    assert first == second
    assert first in pool["dawn"]

    other = world_copy.pick_line(_POOL_TEXT, "dawn", 1)
    assert other in pool["dawn"]

    assert world_copy.pick_line("", "dawn", 0) is None

    skipped = world_copy.parse_lines("dawn | a\nbad | b\nnight | c")
    assert "bad" not in skipped
    assert skipped["dawn"] == ["a"]
    assert skipped["night"] == ["c"]


def test_today_streak_milestone_critical(db, user):
    today = _now_local_naive().replace(hour=0, minute=0, second=0, microsecond=0)
    db.add(
        models.CheckinRecord(
            user_id=user.id,
            checkin_date=today,
            streak=6,
        )
    )
    db.commit()

    app = _make_app(db, user)
    client = TestClient(app)
    resp = client.get("/api/panel/today")
    assert resp.status_code == 200
    body = resp.json()
    assert body["code"] == 0
    data = body["data"]
    assert data["streak_days"] == 6
    assert data["streak_milestone"] == {"next": 7, "remaining": 1, "warn_within": 1}


def test_today_streak_milestone_none_when_no_record(db, user):
    app = _make_app(db, user)
    client = TestClient(app)
    resp = client.get("/api/panel/today")
    assert resp.status_code == 200
    body = resp.json()
    assert body["code"] == 0
    data = body["data"]
    assert data["streak_milestone"]["next"] == 7
    assert data["streak_milestone"]["remaining"] == 7


def test_today_viewers_counts_distinct_users_today(db, user):
    user2 = models.WebUser(username="psych_test2", password_hash="x", is_active=True)
    db.add(user2)
    db.commit()
    db.refresh(user2)

    now = _now_local_naive()
    today = now.replace(hour=12, minute=0, second=0, microsecond=0)
    yesterday = today - timedelta(days=1)

    def add_session(uid, key, start):
        db.add(
            emby_models.PlaybackSession(
                user_id=uid,
                item_id=999,
                session_key=key,
                start_time=start,
                last_update_at=start,
            )
        )

    add_session(user.id, "s1", today)
    add_session(user.id, "s2", today + timedelta(minutes=1))
    add_session(user2.id, "s3", today + timedelta(minutes=2))
    add_session(user.id, "s4", yesterday)
    db.commit()

    app = _make_app(db, user)
    client = TestClient(app)
    resp = client.get("/api/panel/today")
    assert resp.status_code == 200
    body = resp.json()
    assert body["code"] == 0
    data = body["data"]
    assert data["today_viewers"] == 2


def test_today_daypart_line_present(db, user):
    app = _make_app(db, user)
    client = TestClient(app)
    resp = client.get("/api/panel/today")
    assert resp.status_code == 200
    body = resp.json()
    assert body["code"] == 0
    data = body["data"]

    line = data["daypart_line"]
    assert isinstance(line, str)
    assert line

    pool = world_copy.parse_lines(_POOL_TEXT)
    all_lines = {item for lines in pool.values() for item in lines}
    assert line in all_lines
