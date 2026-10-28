"""302 Next F3：回滚演练（可执行）。

演练三步，全部在测试里跑通才算 F3 落地：
1. 总闸开 → 新路由活（搜索 200）。
2. 总闸关 → 新路由 404，且旧链（自然语言搜片，自身开关开）仍 200。
3. 关闸不删数据：数据集行与转存任务行原样留着，再开闸即恢复。
"""

import os

os.environ.setdefault("DATABASE_TYPE", "sqlite")
os.environ.setdefault("REDIS_ENABLED", "false")

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend import library_scope, models
from backend.api.next302 import router as next302_router
from backend.api.nl_search import router as nl_search_router
from backend.api.user import get_current_user
from backend.database import get_db
from backend.emby_server import models as em
from backend.emby_server import tmdb_dataset as dataset


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
    try:
        yield session
    finally:
        session.close()


@pytest.fixture()
def client(engine):
    application = FastAPI()
    application.include_router(next302_router)
    application.include_router(nl_search_router)
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


def test_rollback_drill(monkeypatch, client, db):
    """F3 演练：开闸 → 关闸 → 再开闸，旧链全程不受影响、数据不丢。"""
    user = models.WebUser(username="u1", password_hash="x", is_active=True,
                          is_staff=True)
    db.add(user)
    db.commit()
    db.refresh(user)
    client.app.dependency_overrides[get_current_user] = lambda: user
    monkeypatch.setattr(library_scope, "effective_ids",
                        lambda db_, u: None)  # 工作人员豁免：不过滤
    dataset.upsert_items(db, [
        {"tmdb_id": "550", "media_type": "movie", "title": "搏击俱乐部",
         "original_title": "", "year": 1999, "overview": "", "genres": [],
         "poster_path": "", "vote_average": None, "release_date": "",
         "source": "popular"},
    ])
    import uuid
    item = em.MediaItem(name="搏击俱乐部", item_type="movie", library_id=7,
                        tmdb_id="550", guid="test-" + uuid.uuid4().hex)
    db.add(item)
    db.commit()
    db.refresh(item)
    job = em.TransferJob(item_id=item.id, owner_scope="user",
                         owner_ref=str(user.id), status="pending",
                         trigger="test")
    db.add(job)
    db.commit()

    # 1. 总闸开 + 搜索开关开：新路由活。
    monkeypatch.setenv("NEXT302_ENABLED", "1")
    monkeypatch.setenv("NEXT302_SEARCH_ENABLED", "1")
    monkeypatch.setenv("NL_SEARCH_ENABLED", "1")
    res = client.get("/api/user/next302/search", params={"q": "搏击"})
    assert res.status_code == 200, res.text
    assert res.json()["total"] == 1
    legacy = client.get("/api/user/nl-search", params={"q": "搏击"})
    assert legacy.status_code == 200, legacy.text

    # 2. 总闸关：新路由 404，旧链仍 200。
    monkeypatch.setenv("NEXT302_ENABLED", "0")
    res = client.get("/api/user/next302/search", params={"q": "搏击"})
    assert res.status_code == 404
    legacy = client.get("/api/user/nl-search", params={"q": "搏击"})
    assert legacy.status_code == 200, legacy.text

    # 3. 关闸不删数据：数据集行与任务行原样。
    assert db.query(em.TmdbDatasetItem).count() == 1
    assert db.query(em.TransferJob).count() == 1

    # 再开闸即恢复，不用重建任何数据。
    monkeypatch.setenv("NEXT302_ENABLED", "1")
    res = client.get("/api/user/next302/search", params={"q": "搏击"})
    assert res.status_code == 200
    assert res.json()["total"] == 1
