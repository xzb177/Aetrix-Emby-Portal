"""想看商店骨架：总闸、会员闸、R0 missing_source。

不连生产库。总闸关时四个口都是 404（路由级依赖先于鉴权，未登录也是 404）。
总闸开时非会员想看 403；会员对未入库 tmdb 得到 missing_source，且不写转存任务。
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
from backend.api.store import router
from backend.api.user import get_current_user
from backend.database import get_db
from backend.emby_server import models as em
from backend.emby_server.store_contract import (
    MISSING_SOURCE_MESSAGE,
    STORE_DISABLED_DETAIL,
    STORE_MEMBERSHIP_REQUIRED_DETAIL,
    TRANSFER_NOT_WIRED_DETAIL,
)


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


def _user(db, username):
    user = models.WebUser(
        username=username, password_hash="x", is_active=True, is_staff=False,
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


def _as(client, user):
    client.app.dependency_overrides[get_current_user] = lambda: user
    return client


def _jobs(engine) -> int:
    session = sessionmaker(bind=engine)()
    try:
        return session.query(em.TransferJob).count()
    finally:
        session.close()


def test_flag_off_all_store_routes_404(client, monkeypatch):
    """总闸关：目录、详情、想看、清单都是 404，未登录也不会先 401。"""
    monkeypatch.setenv("STORE_ENABLED", "0")
    catalog = client.get("/api/user/store/catalog")
    item = client.get("/api/user/store/items/550")
    want = client.post("/api/user/store/want", json={"tmdb_id": "550"})
    wants = client.get("/api/user/store/wants")
    for response in (catalog, item, want, wants):
        assert response.status_code == 404, response.text
        assert response.json()["detail"] == STORE_DISABLED_DETAIL


def test_flag_on_guest_browses_empty_catalog(client, monkeypatch):
    """总闸开：访客只读目录和详情；写口没有登录凭证则 401。"""
    monkeypatch.setenv("STORE_ENABLED", "1")
    catalog = client.get("/api/user/store/catalog")
    assert catalog.status_code == 200, catalog.text
    assert catalog.json() == {"items": [], "total": 0}
    item = client.get("/api/user/store/items/550")
    assert item.status_code == 200, item.text
    body = item.json()
    assert body["tmdb_id"] == "550"
    assert body["in_library"] is False
    assert body["library_item_id"] is None
    assert client.post("/api/user/store/want", json={"tmdb_id": "550"}).status_code == 401
    assert client.get("/api/user/store/wants").status_code == 401


def test_flag_on_non_member_want_403(client, db, engine, monkeypatch):
    """总闸开、已登录但没有有效订阅：想看和清单都是 403，不入队。"""
    monkeypatch.setenv("STORE_ENABLED", "1")
    user = _user(db, "guest-memberless")
    _as(client, user)
    want = client.post("/api/user/store/want", json={"tmdb_id": "550"})
    assert want.status_code == 403, want.text
    assert want.json()["detail"] == STORE_MEMBERSHIP_REQUIRED_DETAIL
    wants = client.get("/api/user/store/wants")
    assert wants.status_code == 403, wants.text
    assert wants.json()["detail"] == STORE_MEMBERSHIP_REQUIRED_DETAIL
    assert _jobs(engine) == 0


def test_flag_on_member_unmapped_want_missing_source(client, db, engine, monkeypatch):
    """会员想看一个库里没有的 tmdb：missing_source，转存任务表仍为空。"""
    monkeypatch.setenv("STORE_ENABLED", "1")
    user = _user(db, "member")
    _subscribe(db, user)
    _as(client, user)
    want = client.post("/api/user/store/want", json={"tmdb_id": "424242"})
    assert want.status_code == 200, want.text
    body = want.json()
    assert body["state"] == "missing_source"
    assert body["tmdb_id"] == "424242"
    assert body["in_library"] is False
    assert body["library_item_id"] is None
    assert body["message"] == MISSING_SOURCE_MESSAGE
    assert _jobs(engine) == 0
    wants = client.get("/api/user/store/wants")
    assert wants.status_code == 200, wants.text
    assert wants.json() == {"items": []}


def test_flag_on_member_mapped_want_does_not_enqueue(client, db, engine, monkeypatch):
    """库内已有条目时 501，仍然不写 emby_transfer_jobs。"""
    monkeypatch.setenv("STORE_ENABLED", "1")
    user = _user(db, "member-mapped")
    _subscribe(db, user)
    library = em.Library(guid="lib-store", name="商店库")
    db.add(library)
    db.commit()
    db.add(em.MediaItem(
        guid="item-store-603",
        library_id=library.id,
        item_type="movie",
        name="已在库",
        tmdb_id="603",
    ))
    db.commit()
    _as(client, user)
    want = client.post("/api/user/store/want", json={"tmdb_id": "603"})
    assert want.status_code == 501, want.text
    assert want.json()["detail"] == TRANSFER_NOT_WIRED_DETAIL
    assert _jobs(engine) == 0
    detail = client.get("/api/user/store/items/603")
    assert detail.status_code == 200, detail.text
    assert detail.json()["in_library"] is True
    assert detail.json()["library_item_id"] is not None
