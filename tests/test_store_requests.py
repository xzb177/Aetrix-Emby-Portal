"""想看商店求片 R1：登记 + 榜单 + 入库联动。

不连生产库。总闸关时四个口都是 404（路由级依赖先于鉴权，未登录
也是 404）。总闸开时登记与列表限会员；库外 tmdb 登记或附议（同一
tmdb 只建一行处理中行，人数 = 附议数）；库内 tmdb 不登记、指去想看；
读时同步把已入库的行翻 completed，榜单随即掉出该行（应去想看）。

复用 movie_requests / movie_request_votes，不另建表；管理端榜单只读，
改状态走既有 MediaSeek 入口。
"""

import os

os.environ.setdefault("DATABASE_TYPE", "sqlite")
os.environ.setdefault("REDIS_ENABLED", "false")

from datetime import datetime, timedelta

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend import models, subscriptions
from backend.api.admin_core import get_current_admin
from backend.api.store_requests import admin_router, router
from backend.api.user import get_current_user
from backend.database import get_db
from backend.emby_server import models as em


@pytest.fixture()
def engine():
    eng = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    models.Base.metadata.create_all(eng)
    yield eng
    eng.dispose()


@pytest.fixture()
def db(engine):
    session = sessionmaker(bind=engine)()
    # 全量 pytest 里别的用例可能装过「本进程的服」。想看会员闸不按服过滤，
    # 这里清掉解析器，结束再装回去，避免订阅行被服条件滤掉。
    previous = subscriptions._realm_resolver
    subscriptions.set_process_realm_resolver(None)
    try:
        yield session
    finally:
        subscriptions.set_process_realm_resolver(previous)
        session.close()


@pytest.fixture()
def client(engine):
    application = FastAPI()
    application.include_router(router)
    application.include_router(admin_router)
    sessions = sessionmaker(bind=engine)

    def _override_get_db():
        session = sessions()
        try:
            yield session
        finally:
            session.close()

    application.dependency_overrides[get_db] = _override_get_db
    with TestClient(application) as test_client:
        yield test_client
    application.dependency_overrides.clear()


def _user(db, username, staff=False):
    user = models.WebUser(
        username=username, password_hash="x", is_active=True, is_staff=staff,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def _subscribe(db, user):
    plan = models.SubscriptionPlan(name="月卡", price=1, duration_days=30, is_active=True)
    db.add(plan)
    db.commit()
    now = datetime.now()
    sub = models.UserSubscription(
        user_id=user.id,
        plan_id=plan.id,
        realm_id=None,
        start_date=now,
        end_date=now + timedelta(days=30),
        status="active",
    )
    db.add(sub)
    db.commit()
    return sub


def _media_item(db, tmdb_id, name="已在库"):
    library = em.Library(guid=f"lib-r1-{tmdb_id}", name="R1库")
    db.add(library)
    db.commit()
    item = em.MediaItem(
        guid=f"item-r1-{tmdb_id}",
        library_id=library.id,
        item_type="movie",
        name=name,
        tmdb_id=tmdb_id,
        file_path=f"/media/{tmdb_id}.mkv",
    )
    db.add(item)
    db.commit()
    return item


def _as(client, user):
    client.app.dependency_overrides[get_current_user] = lambda: user
    return client


def _as_admin(client, admin):
    client.app.dependency_overrides[get_current_admin] = lambda: admin
    return client


def _request_row(db, tmdb_id):
    return (
        db.query(models.MovieRequest)
        .filter(models.MovieRequest.tmdb_id == tmdb_id)
        .first()
    )


def test_flag_off_all_r1_routes_404(client, monkeypatch):
    monkeypatch.setenv("STORE_ENABLED", "0")
    assert client.post("/api/user/store/requests", json={"tmdb_id": "123"}).status_code == 404
    assert client.get("/api/user/store/requests").status_code == 404
    assert client.get("/api/user/store/requests/board").status_code == 404
    assert client.get("/api/admin/store/requests").status_code == 404


def test_flag_on_non_member_post_403(client, db, monkeypatch):
    monkeypatch.setenv("STORE_ENABLED", "1")
    user = _user(db, "plain")
    resp = _as(client, user).post("/api/user/store/requests", json={"tmdb_id": "123"})
    assert resp.status_code == 403


def test_member_registers_out_of_library(client, db, monkeypatch):
    monkeypatch.setenv("STORE_ENABLED", "1")
    user = _user(db, "seeker")
    _subscribe(db, user)
    resp = _as(client, user).post(
        "/api/user/store/requests", json={"tmdb_id": "123", "title": "星际穿越"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["state"] == "registered"
    assert body["vote_count"] == 1
    assert body["request_id"] is not None
    assert body["in_library"] is False
    row = _request_row(db, "123")
    assert row is not None and row.status == "pending"
    assert row.movie_name == "星际穿越"
    votes = (
        db.query(models.MovieRequestVote)
        .filter(models.MovieRequestVote.request_id == row.id)
        .count()
    )
    assert votes == 1


def test_register_without_title_uses_tmdb_placeholder(client, db, monkeypatch):
    monkeypatch.setenv("STORE_ENABLED", "1")
    user = _user(db, "seeker")
    _subscribe(db, user)
    resp = _as(client, user).post("/api/user/store/requests", json={"tmdb_id": "124"})
    assert resp.status_code == 200
    assert resp.json()["movie_name"] == "124"


def test_same_tmdb_votes_then_idempotent(client, db, monkeypatch):
    monkeypatch.setenv("STORE_ENABLED", "1")
    first = _user(db, "first")
    second = _user(db, "second")
    _subscribe(db, first)
    _subscribe(db, second)
    _as(client, first).post("/api/user/store/requests", json={"tmdb_id": "125"})
    resp = _as(client, second).post("/api/user/store/requests", json={"tmdb_id": "125"})
    assert resp.status_code == 200
    assert resp.json()["vote_count"] == 2
    assert resp.json()["state"] == "registered"
    resp = _as(client, second).post("/api/user/store/requests", json={"tmdb_id": "125"})
    assert resp.json()["vote_count"] == 2
    assert resp.json()["message"] == "你已经登记过这部片子了，人数没有重复算。"
    rows = (
        db.query(models.MovieRequest)
        .filter(models.MovieRequest.tmdb_id == "125")
        .all()
    )
    assert len(rows) == 1


def test_register_in_library_no_row(client, db, monkeypatch):
    monkeypatch.setenv("STORE_ENABLED", "1")
    user = _user(db, "seeker")
    _subscribe(db, user)
    _media_item(db, "456")
    resp = _as(client, user).post("/api/user/store/requests", json={"tmdb_id": "456"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["state"] == "in_library"
    assert body["request_id"] is None
    assert body["in_library"] is True
    assert _request_row(db, "456") is None


def test_register_bad_tmdb_400(client, db, monkeypatch):
    monkeypatch.setenv("STORE_ENABLED", "1")
    user = _user(db, "seeker")
    _subscribe(db, user)
    resp = _as(client, user).post("/api/user/store/requests", json={"tmdb_id": "abc"})
    assert resp.status_code == 400


def test_my_list_syncs_to_stored(client, db, monkeypatch):
    monkeypatch.setenv("STORE_ENABLED", "1")
    user = _user(db, "seeker")
    _subscribe(db, user)
    _as(client, user).post("/api/user/store/requests", json={"tmdb_id": "789"})
    _media_item(db, "789")
    resp = _as(client, user).get("/api/user/store/requests")
    assert resp.status_code == 200
    items = resp.json()["items"]
    assert len(items) == 1
    assert items[0]["state"] == "stored"
    assert items[0]["in_library"] is True
    row = _request_row(db, "789")
    assert row.status == "completed"


def test_board_orders_by_votes_and_drops_stored(client, db, monkeypatch):
    monkeypatch.setenv("STORE_ENABLED", "1")
    users = [_user(db, f"u{i}") for i in range(4)]
    for user in users:
        _subscribe(db, user)
    _as(client, users[0]).post("/api/user/store/requests", json={"tmdb_id": "201"})
    _as(client, users[0]).post("/api/user/store/requests", json={"tmdb_id": "202"})
    _as(client, users[1]).post("/api/user/store/requests", json={"tmdb_id": "202"})
    _as(client, users[2]).post("/api/user/store/requests", json={"tmdb_id": "202"})
    resp = _as(client, users[3]).get("/api/user/store/requests/board")
    assert resp.status_code == 200
    items = resp.json()["items"]
    assert [item["tmdb_id"] for item in items] == ["202", "201"]
    assert items[0]["vote_count"] == 3
    assert items[0]["voted"] is False
    _media_item(db, "202")
    resp = _as(client, users[3]).get("/api/user/store/requests/board")
    items = resp.json()["items"]
    assert [item["tmdb_id"] for item in items] == ["201"]


def test_admin_board_sees_all_and_guest_401(client, db, monkeypatch):
    monkeypatch.setenv("STORE_ENABLED", "1")
    admin = _user(db, "boss", staff=True)
    seeker = _user(db, "seeker")
    _subscribe(db, seeker)
    _as(client, seeker).post("/api/user/store/requests", json={"tmdb_id": "301"})
    rejected = models.MovieRequest(
        user_id=seeker.id, movie_name="拒片", tmdb_id="302", status="rejected",
        vote_count=5,
    )
    db.add(rejected)
    db.commit()
    resp = _as_admin(client, admin).get("/api/admin/store/requests")
    assert resp.status_code == 200
    items = resp.json()["items"]
    assert [item["tmdb_id"] for item in items] == ["302", "301"]
    assert items[1]["state"] == "registered"
    assert items[0]["state"] == "not_found"
    client.app.dependency_overrides.pop(get_current_admin, None)
    assert client.get("/api/admin/store/requests").status_code == 401


def test_guest_board_401(client, monkeypatch):
    monkeypatch.setenv("STORE_ENABLED", "1")
    assert client.get("/api/user/store/requests/board").status_code == 401
