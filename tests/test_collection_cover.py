"""合集拼贴封面：出图、collage 镜像、关闸回落、backfill 入队。

隔离临时 SQLite，不连生产库。拼图复用库封面渲染器，缓存落在
临时图片目录。
"""

import os
import sys
import tempfile

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
from PIL import Image

from backend import models as base_models
from backend.database import SessionLocal, init_db
from backend.emby_server import collection_cover, cover_mirror, library_covers
from backend.emby_server import media_sets as ms
from backend.emby_server import models as em
from scripts.backfill_cover_mirrors import main as backfill_main
from scripts.backfill_cover_mirrors import scan as backfill_scan

init_db()


@pytest.fixture()
def db():
    s = SessionLocal()
    try:
        for table in (em.CoverMirror, em.EmbyMediaSetMember, em.EmbyMediaSetRule,
                      em.EmbyMediaSet, em.EmbyMediaSetTemplate,
                      em.MediaItem, em.Library, base_models.WebUser):
            s.query(table).delete()
        s.commit()
        yield s
    finally:
        s.close()


@pytest.fixture()
def imgdir(tmp_path, monkeypatch):
    monkeypatch.setenv("EMBY_IMAGE_DIR", str(tmp_path))
    return tmp_path


@pytest.fixture()
def admin(db):
    u = base_models.WebUser(username="coveradmin", is_staff=True,
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

    def _override_get_db():
        _s = SessionLocal()
        try:
            yield _s
        finally:
            _s.close()

    app.dependency_overrides[get_db] = _override_get_db
    try:
        yield TestClient(app, raise_server_exceptions=False)
    finally:
        app.dependency_overrides.clear()


@pytest.fixture()
def authed(client, admin):
    from backend.api.admin_core import get_current_admin
    from backend.api.user import get_current_user
    from backend.main import app

    app.dependency_overrides[get_current_admin] = lambda: admin
    app.dependency_overrides[get_current_user] = lambda: admin
    try:
        yield client
    finally:
        del app.dependency_overrides[get_current_admin]
        del app.dependency_overrides[get_current_user]


@pytest.fixture()
def gate_on(monkeypatch):
    monkeypatch.setattr(ms, "COLLECTIONS_ENABLED", 1)
    monkeypatch.setattr(ms, "COLLECTION_SHOW_UNBOUND", 0)
    monkeypatch.setattr(cover_mirror, "COVER_MIRROR_ENABLED", 0)
    yield


def _lib(db, name):
    lib = em.Library(guid=f"lib-{name}-{os.urandom(3).hex()}", name=name,
                     is_enabled=True)
    db.add(lib)
    db.commit()
    db.refresh(lib)
    return lib


def _poster(imgdir, name, color):
    path = imgdir / f"{name}.jpg"
    Image.new("RGB", (200, 300), color).save(path, "JPEG")
    return str(path)


def _item(db, lib_id, name, poster, year=2001, tmdb="1"):
    item = em.MediaItem(
        guid=f"item-{name}-{os.urandom(3).hex()}", library_id=lib_id,
        item_type="movie", name=name, production_year=year, tmdb_id=tmdb,
        file_path=f"/data/{name}.mkv", poster_path=poster,
    )
    db.add(item)
    db.commit()
    db.refresh(item)
    return item


def _collection(authed, name="测试合集", artwork_mode="auto_collage", **over):
    body = {"name": name, "source_type": "manual", "artwork_mode": artwork_mode,
            "min_effective_members": 1}
    body.update(over)
    r = authed.post("/api/admin/media-sets", json=body)
    assert r.status_code == 200, r.text
    return r.json()


def _add_member(authed, set_id, item_id):
    r = authed.post(f"/api/admin/media-sets/{set_id}/members",
                    json={"item_id": item_id})
    assert r.status_code == 200, r.text


def test_cover_request_returns_webp_when_artwork_on(authed, db, gate_on, imgdir):
    lib = _lib(db, "甲库")
    a = _item(db, lib.id, "甲一", _poster(imgdir, "a", (20, 90, 140)), tmdb="11")
    b = _item(db, lib.id, "甲二", _poster(imgdir, "b", (140, 60, 40)),
              year=2004, tmdb="12")
    created = _collection(authed)
    sid = created["id"]
    _add_member(authed, sid, a.id)
    _add_member(authed, sid, b.id)

    # 出图路由不挂 JWT：<img> 带不了门户令牌
    resp = authed.get(f"/api/user/collections/{sid}/cover")
    assert resp.status_code == 200, resp.text
    assert resp.headers["content-type"].startswith("image/webp")
    assert resp.content[:4] == b"RIFF" and resp.content[8:12] == b"WEBP"
    img = Image.open(__import__("io").BytesIO(resp.content))
    assert img.size == (1920, 1080)

    detail = authed.get(f"/api/user/collections/{sid}")
    assert detail.status_code == 200, detail.text
    assert detail.json()["artwork_mode"] == "auto_collage"
    assert detail.json()["cover_url"] == f"/api/user/collections/{sid}/cover"
    shelves = authed.get("/api/user/collections/shelves").json()
    row = next(c for c in shelves["collections"] if c["id"] == sid)
    assert row["cover_url"] == f"/api/user/collections/{sid}/cover"
    assert row["artwork_mode"] == "auto_collage"


def test_collage_mirror_rewrites_when_enabled(authed, db, gate_on, imgdir, monkeypatch):
    lib = _lib(db, "乙库")
    a = _item(db, lib.id, "乙一", _poster(imgdir, "c", (30, 80, 30)), tmdb="21")
    b = _item(db, lib.id, "乙二", _poster(imgdir, "d", (80, 30, 80)),
              year=1998, tmdb="22")
    sid = _collection(authed, name="镜像合集")["id"]
    _add_member(authed, sid, a.id)
    _add_member(authed, sid, b.id)

    warm = authed.get(f"/api/user/collections/{sid}/cover")
    assert warm.status_code == 200
    assert warm.headers["content-type"].startswith("image/webp")
    local = collection_cover._webp_path(sid)
    assert os.path.isfile(local)
    url = f"/api/user/collections/{sid}/cover"

    # 镜像关：即使下面会有成功行，读口也必须保持门户路径
    assert authed.get(f"/api/user/collections/{sid}").json()["cover_url"] == url

    monkeypatch.setattr(cover_mirror, "COVER_MIRROR_ENABLED", 1)
    monkeypatch.setenv("COVER_MIRROR_BACKEND", "local")
    # 缓存已在、尚无成功行：入队，地址仍是原路径
    again = authed.get(f"/api/user/collections/{sid}").json()
    assert again["cover_url"] == url
    h = cover_mirror.source_hash(url, local)
    row = db.query(em.CoverMirror).filter(em.CoverMirror.source_hash == h).one()
    assert row.source == "collage"
    assert row.status == "pending"
    assert row.local_path_hint == local

    row.status = "success"
    row.mirror_rel = f"{h}.webp"
    row.content_type = "image/webp"
    db.commit()
    mirror_path = os.path.join(cover_mirror.mirror_dir(), row.mirror_rel)
    os.makedirs(os.path.dirname(mirror_path), exist_ok=True)
    with open(mirror_path, "wb") as handle:
        handle.write(warm.content)

    mirrored = f"/cdn/covers/{h}"
    assert authed.get(f"/api/user/collections/{sid}").json()["cover_url"] == mirrored
    shelves = authed.get("/api/user/collections/shelves").json()
    row_out = next(c for c in shelves["collections"] if c["id"] == sid)
    assert row_out["cover_url"] == mirrored

    # 关闸后原路径回来，不继续指向镜像
    monkeypatch.setattr(cover_mirror, "COVER_MIRROR_ENABLED", 0)
    assert authed.get(f"/api/user/collections/{sid}").json()["cover_url"] == url
    shelves = authed.get("/api/user/collections/shelves").json()
    assert next(c for c in shelves["collections"] if c["id"] == sid)["cover_url"] == url


def test_artwork_none_and_gate_off_do_not_break_image_contract(
        authed, db, gate_on, imgdir, monkeypatch):
    lib = _lib(db, "丙库")
    a = _item(db, lib.id, "丙一", _poster(imgdir, "e", (10, 10, 10)), tmdb="31")
    b = _item(db, lib.id, "丙二", _poster(imgdir, "f", (200, 200, 200)),
              year=2010, tmdb="32")
    sid = _collection(authed, name="无封面", artwork_mode="none")["id"]
    _add_member(authed, sid, a.id)
    _add_member(authed, sid, b.id)

    detail = authed.get(f"/api/user/collections/{sid}").json()
    assert detail["artwork_mode"] == "none"
    assert detail["cover_url"] is None
    shelves = authed.get("/api/user/collections/shelves").json()
    assert next(c for c in shelves["collections"] if c["id"] == sid)["cover_url"] is None

    img = authed.get(f"/api/user/collections/{sid}/cover")
    assert img.status_code == 200
    assert img.headers["content-type"].startswith("image/svg+xml")
    assert img.content == library_covers.SVG_PLACEHOLDER_BYTES
    assert not img.content.startswith(b"RIFF")

    monkeypatch.setattr(ms, "COLLECTIONS_ENABLED", 0)
    assert authed.get(f"/api/user/collections/{sid}/cover").status_code == 404
    assert authed.get(f"/api/user/collections/{sid}").status_code == 404
    assert authed.get("/api/user/collections/shelves").status_code == 404


def test_backfill_enqueues_auto_collage_only(db, imgdir, monkeypatch):
    monkeypatch.setattr(cover_mirror, "COVER_MIRROR_ENABLED", 1)
    monkeypatch.setenv("COVER_MIRROR_BACKEND", "local")
    monkeypatch.setattr(ms, "COLLECTIONS_ENABLED", 1)
    monkeypatch.setattr(ms, "COLLECTION_SHOW_UNBOUND", 0)
    monkeypatch.setattr(library_covers, "BORROW_ENABLED", 0)

    lib = _lib(db, "丁库")
    poster = _poster(imgdir, "g", (40, 40, 160))
    item = _item(db, lib.id, "丁一", poster, tmdb="41")
    bare = _item(db, lib.id, "丁二", None, tmdb="42")
    bare.poster_path = None
    db.commit()

    ready = em.EmbyMediaSet(name="可拼", source_type="manual",
                            artwork_mode="auto_collage", min_effective_members=1)
    empty = em.EmbyMediaSet(name="无海报", source_type="manual",
                            artwork_mode="auto_collage", min_effective_members=1)
    silent = em.EmbyMediaSet(name="关封面", source_type="manual",
                             artwork_mode="none", min_effective_members=1)
    db.add_all([ready, empty, silent])
    db.commit()
    db.add(em.EmbyMediaSetMember(set_id=ready.id, item_id=item.id,
                                 member_source="manual"))
    db.add(em.EmbyMediaSetMember(set_id=empty.id, item_id=bare.id,
                                 member_source="manual"))
    db.add(em.EmbyMediaSetMember(set_id=silent.id, item_id=item.id,
                                 member_source="manual"))
    db.commit()

    dry = backfill_scan(db, apply=False)
    assert dry["collections"] == 2  # none 不扫
    assert dry["enqueue"] == 1
    assert db.query(em.CoverMirror).count() == 0

    applied = backfill_scan(db, apply=True)
    assert applied["collections"] == 2
    assert applied["enqueue"] == 1
    rows = db.query(em.CoverMirror).all()
    assert len(rows) == 1
    assert rows[0].source == "collage"
    assert rows[0].status == "pending"
    assert rows[0].source_url == f"/api/user/collections/{ready.id}/cover"
    assert rows[0].source_url != f"/api/user/collections/{silent.id}/cover"
    assert rows[0].source_url != f"/api/user/collections/{empty.id}/cover"
    assert rows[0].local_path_hint and os.path.isfile(rows[0].local_path_hint)

    again = backfill_scan(db, apply=True)
    assert again["enqueue"] == 0
    assert again["exists"] == 1
    assert db.query(em.CoverMirror).count() == 1

    monkeypatch.setattr(cover_mirror, "COVER_MIRROR_ENABLED", 0)
    monkeypatch.setattr(sys, "argv", ["backfill_cover_mirrors.py", "--apply"])
    assert backfill_main() == 2
