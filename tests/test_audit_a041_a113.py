"""审计 A-041 + A-113 同批：用户端在库判定与求片 lookup 须认隐藏/删除/可见范围。

A-113：store.find_library_item、media_seek.library_hit（tmdb/片名两分支）、
  store_requests._library_hit/_sync_row 三路在库判定，隐藏与已删条目不得判在库。
A-041：GET /media-seek/lookup（lookup_media）须转义 LIKE 通配符、过滤
  deleted_at/is_hidden、按 _library_scope 可见库过滤。

不连生产库；sqlite 内存库 + StaticPool。
"""

import os

os.environ.setdefault("DATABASE_TYPE", "sqlite")
os.environ.setdefault("REDIS_ENABLED", "false")

from datetime import datetime

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend import library_scope, media_seek, models
from backend.api.store import find_library_item
from backend.api.store import router as store_router
from backend.api.store_requests import _library_hit, _sync_row
from backend.api.user import lookup_media
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
    try:
        yield session
    finally:
        session.close()


def _library(db, tag):
    lib = em.Library(guid=f"lib-a041-{tag}", name=f"库{tag}")
    db.add(lib)
    db.commit()
    return lib


def _item(db, lib, *, tmdb_id, name, hidden=False, deleted=False, item_type="movie"):
    row = em.MediaItem(
        guid=f"item-a041-{tmdb_id}-{name}",
        library_id=lib.id,
        item_type=item_type,
        name=name,
        tmdb_id=tmdb_id,
        file_path="mount://1/x.mp4",
        is_hidden=hidden,
        deleted_at=datetime.now() if deleted else None,
        poster_path="/poster.jpg",
    )
    db.add(row)
    db.commit()
    return row


def _user(db):
    user = models.WebUser(username="u1", password_hash="x", is_active=True, is_staff=False)
    db.add(user)
    db.commit()
    return user


# ---------------- A-113：store.find_library_item ----------------

def test_store_find_library_item_ignores_hidden_and_deleted(db):
    lib = _library(db, "s")
    _item(db, lib, tmdb_id="1001", name="藏片", hidden=True)
    _item(db, lib, tmdb_id="1002", name="删片", deleted=True)
    visible = _item(db, lib, tmdb_id="1003", name="可见片")
    assert find_library_item(db, "1001") is None
    assert find_library_item(db, "1002") is None
    assert find_library_item(db, "1003") is not None
    assert find_library_item(db, "1003").id == visible.id


def test_store_get_item_hidden_returns_in_library_false(db, engine, monkeypatch):
    # 访客口（无鉴权）：隐藏条目不得回 in_library=true，更不得回内部 id。
    monkeypatch.setenv("STORE_ENABLED", "1")
    lib = _library(db, "g")
    _item(db, lib, tmdb_id="2001", name="藏片", hidden=True)
    _item(db, lib, tmdb_id="2002", name="可见片")
    app = FastAPI()
    app.include_router(store_router)
    sessions = sessionmaker(bind=engine)

    def _override_get_db():
        session = sessions()
        try:
            yield session
        finally:
            session.close()

    app.dependency_overrides[get_db] = _override_get_db
    with TestClient(app) as client:
        hidden = client.get("/api/user/store/items/2001")
        assert hidden.status_code == 200
        assert hidden.json()["in_library"] is False
        assert hidden.json()["library_item_id"] is None
        visible = client.get("/api/user/store/items/2002")
        assert visible.json()["in_library"] is True
        assert visible.json()["library_item_id"] is not None
    app.dependency_overrides.clear()


# ---------------- A-113：media_seek.library_hit ----------------

def test_library_hit_tmdb_branch_ignores_hidden_and_deleted(db):
    lib = _library(db, "t")
    _item(db, lib, tmdb_id="3001", name="藏片", hidden=True)
    _item(db, lib, tmdb_id="3002", name="删片", deleted=True)
    _item(db, lib, tmdb_id="3003", name="可见片")
    assert media_seek.library_hit(db, tmdb_id="3001") is None
    assert media_seek.library_hit(db, tmdb_id="3002") is None
    assert media_seek.library_hit(db, tmdb_id="3003") is not None


def test_library_hit_name_branch_ignores_hidden_and_deleted(db):
    lib = _library(db, "n")
    _item(db, lib, tmdb_id="4001", name="同名藏片", hidden=True)
    _item(db, lib, tmdb_id="4002", name="同名删片", deleted=True)
    assert media_seek.library_hit(db, name="同名藏片") is None
    assert media_seek.library_hit(db, name="同名删片") is None
    _item(db, lib, tmdb_id="4003", name="同名可见片")
    assert media_seek.library_hit(db, name="同名可见片") is not None


# ---------------- A-113：store_requests 复用点 ----------------

def test_store_requests_library_hit_and_sync_row_skip_hidden(db):
    lib = _library(db, "r")
    _item(db, lib, tmdb_id="5001", name="藏片", hidden=True)
    _item(db, lib, tmdb_id="5002", name="可见片")
    assert _library_hit(db, "5001") == (False, None)
    assert _library_hit(db, "5002")[0] is True
    user = _user(db)
    row_hidden = models.MovieRequest(user_id=user.id, movie_name="藏片",
                                     tmdb_id="5001", status="pending")
    row_visible = models.MovieRequest(user_id=user.id, movie_name="可见片",
                                      tmdb_id="5002", status="pending")
    db.add_all([row_hidden, row_visible])
    db.commit()
    # 读时同步：隐藏条目的求片行不得当场翻 completed。
    assert _sync_row(db, row_hidden) is False
    assert row_hidden.status == "pending"
    assert _sync_row(db, row_visible) is True
    assert row_visible.status == "completed"


# ---------------- A-041：lookup ----------------

def test_lookup_ignores_hidden_and_deleted(db):
    lib = _library(db, "l")
    _item(db, lib, tmdb_id="6001", name="精确藏片", hidden=True)
    _item(db, lib, tmdb_id="6002", name="精确删片", deleted=True)
    _item(db, lib, tmdb_id="6003", name="精确可见片")
    user = _user(db)
    assert lookup_media(name="精确藏片", current_user=user, db=db)["items"] == []
    assert lookup_media(name="精确删片", current_user=user, db=db)["items"] == []
    hit = lookup_media(name="精确可见片", current_user=user, db=db)
    assert len(hit["items"]) == 1
    assert hit["items"][0]["name"] == "精确可见片"
    assert hit["items"][0]["poster_url"] is not None


def test_lookup_wildcards_are_escaped_not_enumerated(db):
    lib = _library(db, "w")
    _item(db, lib, tmdb_id="7001", name="精确可见片")
    _item(db, lib, tmdb_id="7002", name="100%纯爱")
    user = _user(db)
    # % / _ 只命中名字含该字面字符的行，不得把全部行枚举出来。
    pct = lookup_media(name="%", current_user=user, db=db)["items"]
    assert [i["name"] for i in pct] == ["100%纯爱"]
    assert lookup_media(name="_", current_user=user, db=db)["items"] == []
    # 含字面 % 的片名仍可用含 % 的关键字精确命中（转义正确，不是粗暴禁 %）。
    hit = lookup_media(name="100%", current_user=user, db=db)
    assert [i["name"] for i in hit["items"]] == ["100%纯爱"]


def test_lookup_respects_library_scope(db, monkeypatch):
    lib_visible = _library(db, "v")
    lib_hidden = _library(db, "h")
    _item(db, lib_visible, tmdb_id="8001", name="可见库片")
    _item(db, lib_hidden, tmdb_id="8002", name="不可见库片")
    user = _user(db)
    # 只放行可见库：monkeypatch 作用域口径本身，测 lookup 是否挂了过滤。
    monkeypatch.setattr(
        library_scope, "effective_ids_safe",
        lambda _db, _user: {lib_visible.id},
    )
    assert lookup_media(name="不可见库片", current_user=user, db=db)["items"] == []
    hit = lookup_media(name="可见库片", current_user=user, db=db)
    assert len(hit["items"]) == 1
