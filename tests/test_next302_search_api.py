"""302 Next A2/E4：搜索、详情、播放状态口。

不连生产库。覆盖：总闸关 → 全部 404（先于鉴权）；搜索子开关关 →
404；搜索结果与库内标记；范围隔离；详情库内/库外；播放状态口
六态映射（只读：不建任务、不写库）。
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
from backend.api.next302 import router
from backend.api.user import get_current_user
from backend.database import get_db
from backend.emby_server import models as em
from backend.emby_server import playback_gate, tmdb_dataset as dataset


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


@pytest.fixture()
def user(db):
    u = models.WebUser(username="u1", password_hash="x", is_active=True,
                       is_staff=False)
    db.add(u)
    db.commit()
    db.refresh(u)
    return u


def _as(client, user):
    client.app.dependency_overrides[get_current_user] = lambda: user
    return client


def _gates_on(monkeypatch):
    monkeypatch.setenv("NEXT302_ENABLED", "1")
    monkeypatch.setenv("NEXT302_SEARCH_ENABLED", "1")


def _seed(db):
    dataset.upsert_items(db, [
        {"tmdb_id": "550", "media_type": "movie", "title": "搏击俱乐部",
         "original_title": "Fight Club", "year": 1999, "overview": "失眠症",
         "genres": ["剧情"], "poster_path": "/p.jpg", "vote_average": 8.4,
         "release_date": "1999-10-15", "source": "popular"},
    ])
    import uuid
    item = em.MediaItem(name="搏击俱乐部", item_type="movie", library_id=7,
                        tmdb_id="550", guid="test-" + uuid.uuid4().hex)
    db.add(item)
    db.commit()
    db.refresh(item)
    return item


def test_master_gate_off_all_404(client, monkeypatch, user):
    """总闸关：搜索、详情、状态口全部 404，未登录也不会先 401。"""
    monkeypatch.setenv("NEXT302_ENABLED", "0")
    monkeypatch.setenv("NEXT302_SEARCH_ENABLED", "1")
    anon = client.get("/api/user/next302/search", params={"q": "搏击"})
    assert anon.status_code == 404
    _as(client, user)
    assert client.get("/api/user/next302/search",
                      params={"q": "搏击"}).status_code == 404
    assert client.get("/api/user/next302/details/movie/550").status_code == 404
    assert client.get("/api/user/next302/play-status",
                      params={"item_id": 1}).status_code == 404


def test_search_sub_switch_off_404(client, monkeypatch, user):
    monkeypatch.setenv("NEXT302_ENABLED", "1")
    monkeypatch.setenv("NEXT302_SEARCH_ENABLED", "0")
    _as(client, user)
    assert client.get("/api/user/next302/search",
                      params={"q": "搏击"}).status_code == 404


def test_search_marks_library_and_isolates_scope(client, monkeypatch, db, user):
    _gates_on(monkeypatch)
    _seed(db)
    _as(client, user)
    monkeypatch.setattr(library_scope, "effective_ids",
                        lambda db_, u: {7})
    res = client.get("/api/user/next302/search", params={"q": "搏击"})
    assert res.status_code == 200
    row = res.json()["results"][0]
    assert row["in_library"] is True
    assert row["library_known"] is True
    assert row["library_item_id"] is not None
    # 换一个看不到 7 库的用户：标记消失，但数据集行还在。
    monkeypatch.setattr(library_scope, "effective_ids",
                        lambda db_, u: {9})
    res2 = client.get("/api/user/next302/search", params={"q": "搏击"})
    row2 = res2.json()["results"][0]
    assert row2["in_library"] is False
    assert row2["library_known"] is True


def test_details_library_and_outside(client, monkeypatch, db, user):
    _gates_on(monkeypatch)
    item = _seed(db)
    _as(client, user)
    monkeypatch.setattr(library_scope, "effective_ids",
                        lambda db_, u: {7})
    res = client.get("/api/user/next302/details/movie/550")
    assert res.status_code == 200
    assert res.json()["in_library"] is True
    assert res.json()["library_item_id"] == item.id
    # 数据集里没有的 tmdb：404，不编一条。
    assert client.get("/api/user/next302/details/movie/999999").status_code == 404
    # 库外详情：响应里绝没有播放地址字段。
    dataset.upsert_items(db, [
        {"tmdb_id": "551", "media_type": "movie", "title": "库外片",
         "original_title": "", "year": 2020, "overview": "", "genres": [],
         "poster_path": "", "vote_average": None, "release_date": "",
         "source": "popular"},
    ])
    db.commit()
    res2 = client.get("/api/user/next302/details/movie/551")
    assert res2.status_code == 200
    assert res2.json()["in_library"] is False
    assert "play_url" not in res2.json()
    assert "direct_url" not in res2.json()


def _variant(db, item_id, user_id, status="ready", stale=0):
    v = em.TransferVariant(item_id=item_id, source_file_id="src",
                           variant_file_id="copy", owner_scope="user",
                           owner_ref=str(user_id), status=status,
                           stale_count=stale)
    db.add(v)
    db.commit()
    db.refresh(v)
    return v


def _job(db, item_id, user_id, status="pending"):
    job = em.TransferJob(item_id=item_id, owner_scope="user",
                         owner_ref=str(user_id), status=status,
                         trigger="test")
    db.add(job)
    db.commit()
    db.refresh(job)
    return job


def _play_status(client, item_id):
    res = client.get("/api/user/next302/play-status",
                     params={"item_id": item_id})
    assert res.status_code == 200, res.text
    return res.json()


def test_play_status_matrix(client, monkeypatch, db, user):
    """E4 六态：只读映射，不建任务。"""
    _gates_on(monkeypatch)
    item = _seed(db)
    _as(client, user)
    monkeypatch.setattr(library_scope, "effective_ids",
                        lambda db_, u: {7})

    # 未绑定 → bind。
    monkeypatch.setattr(playback_gate, "has_active_binding",
                        lambda db_, uid: False)
    out = _play_status(client, item.id)
    assert out["route"] == "bind_guide" and out["action"] == "bind"

    monkeypatch.setattr(playback_gate, "has_active_binding",
                        lambda db_, uid: True)
    # 绑定、无副本、无任务 → no_copy_guide。
    out = _play_status(client, item.id)
    assert out["route"] == "no_copy_guide" and out["action"] == "want"
    # 有任务进行中 → preparing。
    _job(db, item.id, user.id, status="running")
    out = _play_status(client, item.id)
    assert out["route"] == "preparing" and out["action"] == "wait"
    # 任务失败 → failed（失败不播共享原件）。
    db.query(em.TransferJob).delete()
    db.commit()
    _job(db, item.id, user.id, status="failed")
    out = _play_status(client, item.id)
    assert out["route"] == "failed" and out["action"] == "failed"
    # 有 ready 副本 → user_copy（任务失败行还在也不影响：副本优先）。
    _variant(db, item.id, user.id, status="ready")
    out = _play_status(client, item.id)
    assert out["route"] == "user_copy" and out["action"] == "play"
    assert out["has_user_ready_copy"] is True
    # 副本失效（stale_count 满 2）→ stale_copy。
    db.query(em.TransferVariant).delete()
    db.commit()
    _variant(db, item.id, user.id, status="ready", stale=2)
    out = _play_status(client, item.id)
    assert out["route"] == "stale_copy" and out["action"] == "want"

    # 全程没多建任务行（只读口径）。
    assert db.query(em.TransferJob).count() == 1


def test_play_status_out_of_scope_404(client, monkeypatch, db, user):
    _gates_on(monkeypatch)
    item = _seed(db)
    _as(client, user)
    monkeypatch.setattr(library_scope, "effective_ids",
                        lambda db_, u: {9})
    res = client.get("/api/user/next302/play-status",
                     params={"item_id": item.id})
    assert res.status_code == 404
