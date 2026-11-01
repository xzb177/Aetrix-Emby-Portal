"""媒体库学习端点：L1 五件套 / L5 游标分页 / L6 分区 / L9 藏 /
管理端覆盖与库设序 / Views 设序。

真实 app 的 TestClient + 隔离临时 SQLite；鉴权逐用例覆盖（门户 JWT
用户、管理 staff、Emby 用户、门户 resume 口各一套）。
"""

import os
import tempfile
from datetime import datetime

os.environ.setdefault("DATABASE_TYPE", "sqlite")
os.environ.setdefault("REDIS_ENABLED", "false")
_fd, _tmppath = tempfile.mkstemp(suffix=".db")
os.close(_fd)
os.environ["DATABASE_URL"] = f"sqlite:///{_tmppath}"

from backend import database as _dbmod
from sqlalchemy import create_engine as _ce
from sqlalchemy.orm import sessionmaker as _sm

_dbmod.engine = _ce(os.environ["DATABASE_URL"])
_dbmod.configure_session_local(_sm(bind=_dbmod.engine))

import pytest

from backend import models as base_models
from backend.database import SessionLocal, init_db
from backend.emby_server import identity_gate, item_artwork, item_sort_projection
from backend.emby_server import library_covers
from backend.emby_server import models as em
from backend.api import media_library as ml_api

init_db()


@pytest.fixture()
def db():
    s = SessionLocal()
    try:
        for table in (em.HomeSection, em.MediaItemSortProjection,
                      em.MediaItemArtworkOverride, em.UserMediaData,
                      em.MediaItem, em.Library, base_models.WebUser,
                      base_models.SystemConfig):
            s.query(table).delete()
        s.commit()
        yield s
    finally:
        # L65：teardown 必须与 setup 清同一批表，否则测试残留会漏进下一个文件
        # （#1287 的 system_configs 行泄漏即由此而来）。
        try:
            for table in (em.HomeSection, em.MediaItemSortProjection,
                          em.MediaItemArtworkOverride, em.UserMediaData,
                          em.MediaItem, em.Library, base_models.WebUser,
                          base_models.SystemConfig):
                s.query(table).delete()
            s.commit()
        except Exception:
            s.rollback()
        finally:
            s.close()


@pytest.fixture()
def user(db):
    u = base_models.WebUser(username="mediauser", is_staff=True,
                            is_active=True, password_hash="test")
    db.add(u)
    db.commit()
    db.refresh(u)
    return u


@pytest.fixture()
def client(db):
    from fastapi.testclient import TestClient

    from backend.database import get_db
    from backend.main import app

    app.dependency_overrides[get_db] = lambda: SessionLocal()
    try:
        yield TestClient(app, raise_server_exceptions=False)
    finally:
        app.dependency_overrides.clear()


@pytest.fixture()
def user_client(client, user):
    from backend.api.user import get_current_user
    from backend.main import app

    app.dependency_overrides[get_current_user] = lambda: user
    try:
        yield client
    finally:
        del app.dependency_overrides[get_current_user]


@pytest.fixture()
def admin_client(client, user):
    from backend.emby_server.portal import require_staff
    from backend.main import app

    app.dependency_overrides[require_staff] = lambda: user
    try:
        yield client
    finally:
        del app.dependency_overrides[require_staff]


@pytest.fixture()
def emby_client(client, user):
    from backend.emby_server.auth import get_emby_user
    from backend.main import app

    app.dependency_overrides[get_emby_user] = lambda: user
    try:
        yield client
    finally:
        del app.dependency_overrides[get_emby_user]


@pytest.fixture()
def resume_client(client, user):
    from backend.emby_server.portal import get_admin_or_emby_user
    from backend.main import app

    app.dependency_overrides[get_admin_or_emby_user] = lambda: user
    try:
        yield client
    finally:
        del app.dependency_overrides[get_admin_or_emby_user]


def _lib(db, name, **kw):
    lib = em.Library(name=name, guid=f"lib-{name}-{os.urandom(4).hex()}", **kw)
    db.add(lib)
    db.commit()
    db.refresh(lib)
    return lib


def _movie(db, lib, name, year=None, tmdb=None, remote=None, added=None):
    it = em.MediaItem(
        name=name, guid=f"item-{os.urandom(6).hex()}",
        library_id=lib.id, item_type="movie", production_year=year,
        tmdb_id=tmdb, primary_image_url=remote,
        date_added=added or datetime(2024, 1, 1),
    )
    db.add(it)
    db.commit()
    db.refresh(it)
    return it


# ---------- L1 ----------

def test_cover_kind_legacy_and_logo(db, user_client):
    lib = _lib(db, "旧行为")
    lib.cover_path = "library-covers/x.webp"
    db.commit()
    r = user_client.get(f"/api/user/media-library/libraries/{lib.id}/cover/poster")
    assert r.status_code == 200
    assert r.json()["source"] == "uploaded"
    r = user_client.get(f"/api/user/media-library/libraries/{lib.id}/cover/logo")
    assert r.json() == {"url": None, "source": "none"}
    assert user_client.get(f"/api/user/media-library/libraries/{lib.id}/cover/nope").status_code == 400
    assert user_client.get("/api/user/media-library/libraries/99999/cover/poster").status_code == 404


def test_cover_borrow_on(db, user_client, monkeypatch):
    monkeypatch.setattr(library_covers, "BORROW_ENABLED", 1)
    lib = _lib(db, "借代表")
    _movie(db, lib, "A", tmdb="42", remote="https://img.example/a.jpg")
    r = user_client.get(f"/api/user/media-library/libraries/{lib.id}/cover/poster")
    assert r.status_code == 200
    assert r.json() == {"url": "https://img.example/a.jpg", "source": "borrow"}


def test_cover_svg_placeholder(db, user_client):
    lib = _lib(db, "占位")
    r = user_client.get(f"/api/user/media-library/libraries/{lib.id}/cover-placeholder.svg")
    assert r.status_code == 200
    assert "image/svg+xml" in r.headers["content-type"]
    assert r.content.startswith(b"<svg")


# ---------- L5 ----------

def _seed_five(db):
    lib = _lib(db, "游标")
    _movie(db, lib, "流浪地球", year=2023)
    _movie(db, lib, "Apple", year=2024)
    _movie(db, lib, "香蕉", year=None)
    _movie(db, lib, "长津湖", year=2020)
    _movie(db, lib, "banana", year=2024)
    return lib


def test_cursor_off_404(db, user_client):
    lib = _seed_five(db)
    r = user_client.get(f"/api/user/media-library/libraries/{lib.id}/items?sort=title")
    assert r.status_code == 404


def test_cursor_paging_title_and_year(db, user_client, monkeypatch):
    monkeypatch.setattr(item_sort_projection, "ENABLED", 1)
    lib = _seed_five(db)
    item_sort_projection.rebuild_projection(db)
    db.commit()

    def walk(sort):
        seen, cursor = [], None
        for _ in range(10):
            url = f"/api/user/media-library/libraries/{lib.id}/items?sort={sort}&limit=2"
            if cursor:
                url += f"&cursor={cursor}"
            r = user_client.get(url)
            assert r.status_code == 200, r.text
            body = r.json()
            seen.extend(body["items"])
            cursor = body["next_cursor"]
            if cursor is None:
                break
        return seen

    titles = [it["name"] for it in walk("title")]
    assert len(titles) == 5 and len(set(titles)) == 5
    years = [it["production_year"] for it in walk("year")]
    assert years == [2024, 2024, 2023, 2020, None]


def test_cursor_bad_cursor_and_sort_mismatch(db, user_client, monkeypatch):
    monkeypatch.setattr(item_sort_projection, "ENABLED", 1)
    lib = _seed_five(db)
    item_sort_projection.rebuild_projection(db)
    db.commit()
    base = f"/api/user/media-library/libraries/{lib.id}/items"
    assert user_client.get(f"{base}?sort=title&cursor=garbage!!").status_code == 400
    cursor = item_sort_projection.encode_cursor("year", 2024, 1)
    assert user_client.get(f"{base}?sort=title&cursor={cursor}").status_code == 400
    assert user_client.get(f"{base}?sort=rating").status_code == 400


# ---------- L6 ----------

def test_sections_off_404_and_on_seed_order(db, user_client, monkeypatch):
    r = user_client.get("/api/user/media-library/home/sections")
    assert r.status_code == 404
    monkeypatch.setattr(ml_api, "SECTIONS_ENABLED", 1)
    lib = _lib(db, "分区")
    _movie(db, lib, "新片", added=datetime(2024, 5, 1))
    _movie(db, lib, "旧片", added=datetime(2024, 1, 1))
    r = user_client.get("/api/user/media-library/home/sections")
    assert r.status_code == 200
    keys = [s["key"] for s in r.json()["sections"]]
    assert keys == ["continue_watching", "recently_updated"]
    recent = r.json()["sections"][1]["items"]
    assert [it["name"] for it in recent] == ["新片", "旧片"]


def test_sections_gate_filters_unbound(db, user_client, monkeypatch):
    monkeypatch.setattr(ml_api, "SECTIONS_ENABLED", 1)
    monkeypatch.setattr(identity_gate, "IDENTITY_SHELF_GATE", 1)
    lib = _lib(db, "门控")
    _movie(db, lib, "已绑定", tmdb="123")
    _movie(db, lib, "未绑定")
    r = user_client.get("/api/user/media-library/home/sections")
    recent = r.json()["sections"][1]["items"]
    assert [it["name"] for it in recent] == ["已绑定"]


# ---------- L9 ----------

def test_resume_hide_flow(db, user_client, resume_client, user):
    lib = _lib(db, "继续")
    movie = _movie(db, lib, "片")
    umd = em.UserMediaData(user_id=user.id, item_id=movie.id,
                           playback_position_ticks=1000, played=False,
                           hide_from_resume=False)
    db.add(umd)
    db.commit()
    resume_url = "/api/user/emby/resume"
    assert any(it["id"] == movie.guid for it in resume_client.get(resume_url).json()["items"])
    r = user_client.post(f"/api/user/media-library/resume/{movie.guid}/hide")
    assert r.status_code == 200
    assert not any(it["id"] == movie.guid for it in resume_client.get(resume_url).json()["items"])
    hidden = user_client.get("/api/user/media-library/resume/hidden").json()["items"]
    assert [it["item_guid"] for it in hidden] == [movie.guid]
    r = user_client.post(f"/api/user/media-library/resume/{movie.guid}/unhide")
    assert r.status_code == 200
    assert any(it["id"] == movie.guid for it in resume_client.get(resume_url).json()["items"])


def test_season_poster_override_via_resume(db, resume_client, user, monkeypatch):
    """有覆盖无自有图时，门户继续观看不再出占位（L2 season_poster_url 插槽）。"""
    monkeypatch.setattr(item_artwork, "OVERRIDE_ENABLED", 1)
    lib = _lib(db, "剧集")
    series = em.MediaItem(name="剧", guid=f"item-{os.urandom(6).hex()}",
                          library_id=lib.id, item_type="series")
    db.add(series)
    db.commit()
    db.refresh(series)
    season = em.MediaItem(name="Season 01", guid=f"item-{os.urandom(6).hex()}",
                          library_id=lib.id, item_type="season",
                          series_id=series.id, season_number=1)
    db.add(season)
    db.commit()
    db.refresh(season)
    ep = em.MediaItem(name="S01E01", guid=f"item-{os.urandom(6).hex()}",
                      library_id=lib.id, item_type="episode",
                      series_id=series.id, parent_id=season.id,
                      season_number=1, episode_number=1)
    db.add(ep)
    db.commit()
    db.refresh(ep)
    db.add(em.UserMediaData(user_id=user.id, item_id=ep.id,
                            playback_position_ticks=1000, played=False,
                            hide_from_resume=False))
    db.commit()
    items = resume_client.get("/api/user/emby/resume").json()["items"]
    assert len(items) == 1 and items[0]["poster_url"] is None
    item_artwork.set_override(db, series.id, "Primary", "https://img.example/cover.jpg")
    items = resume_client.get("/api/user/emby/resume").json()["items"]
    assert items[0]["poster_url"] == f"/emby/Items/{series.guid}/Images/Primary"


# ---------- 管理端：L2 覆盖 / L4 设序 ----------

def test_admin_artwork_flow(db, admin_client):
    lib = _lib(db, "管理覆盖")
    movie = _movie(db, lib, "片")
    base = "/api/admin/media-library/artwork-overrides"
    r = admin_client.post(base, json={"item_id": movie.id, "kind": "Primary",
                                      "image_url": "https://img.example/x.jpg"})
    assert r.status_code == 200, r.text
    rows = admin_client.get(base).json()["items"]
    assert len(rows) == 1 and rows[0]["item_name"] == "片"
    r = admin_client.post(base, json={"item_id": movie.id, "kind": "Primary",
                                      "image_url": "/tmp/x.jpg"})
    assert r.status_code == 400
    r = admin_client.post(base, json={"item_id": 99999, "kind": "Primary",
                                      "image_url": "https://img.example/x.jpg"})
    assert r.status_code == 404
    r = admin_client.delete(f"{base}/{movie.id}/Primary")
    assert r.status_code == 200
    assert admin_client.delete(f"{base}/{movie.id}/Primary").status_code == 404


def test_library_order_flow(db, admin_client):
    a = _lib(db, "A")
    b = _lib(db, "B")
    c = _lib(db, "C")
    base = "/api/admin/emby/libraries/order"
    r = admin_client.put(base, json={"orders": [{"library_id": b.id, "sort_order": 2},
                                                {"library_id": c.id, "sort_order": 1}]})
    assert r.status_code == 200, r.text
    names = [it["name"] for it in admin_client.get(base).json()["items"]]
    assert names == ["C", "B", "A"]  # 已设序在前（按值），未设序的 A 排最后
    r = admin_client.put(base, json={"orders": [{"library_id": 99999, "sort_order": 1}]})
    assert r.status_code == 400


# ---------- L4 Views ----------

def test_views_order_with_sort_order(db, emby_client, user):
    a = _lib(db, "A")
    b = _lib(db, "B")
    c = _lib(db, "C")
    b.sort_order = 5
    c.sort_order = 2
    db.commit()
    r = emby_client.get(f"/emby/Users/{user.id}/Views")
    assert r.status_code == 200, r.text
    ids = [it.get("Id") for it in r.json()["Items"]]
    mine = [g for g in ids if g in (a.guid, b.guid, c.guid)]
    assert mine == [c.guid, b.guid, a.guid]


# ---------- PA-011（R7 审计） ----------

def test_sections_recently_updated_excludes_hidden(db, user_client, monkeypatch):
    """PA-011：首页“最近更新”不得出现 is_hidden 行。"""
    monkeypatch.setattr(ml_api, "SECTIONS_ENABLED", 1)
    lib = _lib(db, "藏")
    _movie(db, lib, "可见", added=datetime(2024, 5, 1))
    hidden = _movie(db, lib, "已藏", added=datetime(2024, 5, 2))
    hidden.is_hidden = True
    db.commit()
    r = user_client.get("/api/user/media-library/home/sections")
    assert r.status_code == 200, r.text
    recent = r.json()["sections"][1]["items"]
    assert [it["name"] for it in recent] == ["可见"]
