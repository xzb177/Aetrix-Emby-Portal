"""合集域 L3 批一：骨架 + 总闸。

覆盖：合集表/成员表（order_index）/手工成员接口/合集自身
visibility_status/sort_order/设序专用接口/与真实库并列成架。
全部在隔离临时 SQLite 里跑，不连生产库。
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
from backend.emby_server import models as em
from backend.emby_server import media_sets as ms

init_db()


@pytest.fixture()
def db():
    s = SessionLocal()
    try:
        for table in (em.EmbyMediaSetMember, em.EmbyMediaSetRule,
                      em.EmbyMediaSet, em.EmbyMediaSetTemplate,
                      em.MediaItem, em.Library, base_models.WebUser):
            s.query(table).delete()
        s.commit()
        yield s
    finally:
        s.close()


@pytest.fixture()
def admin(db):
    u = base_models.WebUser(username="setadmin", is_staff=True,
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
    yield


def _lib(db, name):
    lib = em.Library(guid=f"lib-{name}", name=name, is_enabled=True)
    db.add(lib)
    db.commit()
    db.refresh(lib)
    return lib


def _item(db, lib_id, name, year=None, tmdb=None):
    item = em.MediaItem(guid=f"item-{name}", library_id=lib_id,
                        item_type="movie", name=name,
                        production_year=year, tmdb_id=tmdb,
                        file_path=f"/data/{name}.mkv")
    db.add(item)
    db.commit()
    db.refresh(item)
    return item


def _make_set(authed, **over):
    body = {"name": "测试系列甲", "source_type": "manual"}
    body.update(over)
    r = authed.post("/api/admin/media-sets", json=body)
    assert r.status_code == 200, r.text
    return r.json()


def test_gate_off_new_endpoints_lazy_and_existing_unchanged(
        authed, db, monkeypatch):
    """总闸关：新接口全部 404；已有接口（只读观测口）响应逐字不变。"""
    monkeypatch.setattr(ms, "COLLECTIONS_ENABLED", 0)
    assert authed.post("/api/admin/media-sets",
                       json={"name": "测试系列甲"}).status_code == 404
    assert authed.get("/api/user/collections/shelves").status_code == 404
    before = authed.get("/api/admin/storage-bindings")
    assert before.status_code == 200
    monkeypatch.setattr(ms, "COLLECTIONS_ENABLED", 1)
    after = authed.get("/api/admin/storage-bindings")
    assert after.status_code == 200
    assert after.json() == before.json()


def test_manual_members_and_dual_tracks(authed, db, gate_on):
    lib = _lib(db, "甲库")
    a = _item(db, lib.id, "甲一", year=2001, tmdb="1001")
    b = _item(db, lib.id, "甲二", year=1999, tmdb="1002")
    c = _item(db, lib.id, "甲三", year=2005, tmdb="1003")
    s = _make_set(authed, min_effective_members=2)
    sid = s["id"]
    for item, order in ((a, 2), (b, 1), (c, 0)):
        r = authed.post(f"/api/admin/media-sets/{sid}/members",
                        json={"item_id": item.id, "order_index": order})
        assert r.status_code == 200, r.text
    # 上映轨按年份：甲二(1999) < 甲一(2001) < 甲三(2005)
    r = authed.get(f"/api/user/collections/{sid}/items?track=release")
    assert r.status_code == 200, r.text
    assert [i["name"] for i in r.json()["items"]] == ["甲二", "甲一", "甲三"]
    # 观影轨按 order_index：已编排的在前（甲二=1，甲一=2），未编排的按上映序在后
    r = authed.get(f"/api/user/collections/{sid}/items?track=watch")
    assert [i["name"] for i in r.json()["items"]] == ["甲二", "甲一", "甲三"]
    # 成员设序专用接口：把甲三排到最前（已编排行按 order_index 升序）
    r = authed.post(f"/api/admin/media-sets/{sid}/members/order",
                    json={"orders": [{"item_id": c.id, "order_index": 1},
                                    {"item_id": b.id, "order_index": 5},
                                    {"item_id": a.id, "order_index": 6}]})
    assert r.status_code == 200, r.text
    r = authed.get(f"/api/user/collections/{sid}/items?track=watch")
    assert [i["name"] for i in r.json()["items"]] == ["甲三", "甲二", "甲一"]


def test_shelf_lists_libraries_and_collections_by_sort_order(
        authed, db, gate_on):
    lib = _lib(db, "甲库")
    a = _item(db, lib.id, "甲一", year=2001, tmdb="1001")
    b = _item(db, lib.id, "甲二", year=2002, tmdb="1002")
    s1 = _make_set(authed, name="测试系列甲", sort_order=20)
    s2 = _make_set(authed, name="测试系列乙", sort_order=5)
    for s in (s1, s2):
        for item in (a, b):
            authed.post(f"/api/admin/media-sets/{s['id']}/members",
                        json={"item_id": item.id})
    r = authed.get("/api/user/collections/shelves")
    assert r.status_code == 200, r.text
    body = r.json()
    # 真实库在前、顺序不动（按 id）；合集区按 sort_order 出架
    assert [row["name"] for row in body["libraries"]] == ["甲库"]
    assert [row["name"] for row in body["collections"]] == ["测试系列乙", "测试系列甲"]
    # 合集设序专用接口：调换架序
    r = authed.post("/api/admin/media-sets/order",
                    json={"orders": [{"id": s1["id"], "sort_order": 1},
                                    {"id": s2["id"], "sort_order": 9}]})
    assert r.status_code == 200, r.text
    r = authed.get("/api/user/collections/shelves")
    assert [row["name"] for row in r.json()["collections"]] == ["测试系列甲", "测试系列乙"]


def test_min_gate_and_hidden_and_revision_bump(authed, db, gate_on):
    lib = _lib(db, "甲库")
    a = _item(db, lib.id, "甲一", year=2001, tmdb="1001")
    b = _item(db, lib.id, "甲二", year=2002, tmdb="1002")
    s = _make_set(authed, min_effective_members=3)
    sid = s["id"]
    assert s["requested_revision"] == 0
    authed.post(f"/api/admin/media-sets/{sid}/members",
                json={"item_id": a.id})
    detail = authed.get(f"/api/admin/media-sets/{sid}").json()
    # 手工成员增删顶 requested_revision
    assert detail["requested_revision"] == 1
    # 仅 1 个有效成员、门为 3：不上架、成员口空
    r = authed.get("/api/user/collections/shelves")
    assert r.json()["collections"] == []
    r = authed.get(f"/api/user/collections/{sid}/items")
    assert r.json()["items"] == [] and r.json()["shelf_eligible"] is False
    # 补到 2 个仍不够；改 min 门为 2（改门不顶号）即上架
    authed.post(f"/api/admin/media-sets/{sid}/members",
                json={"item_id": b.id})
    r = authed.patch(f"/api/admin/media-sets/{sid}",
                     json={"min_effective_members": 2})
    assert r.json()["requested_revision"] == 2
    r = authed.get("/api/user/collections/shelves")
    assert [row["name"] for row in r.json()["collections"]] == ["测试系列甲"]
    # hidden：用户端按不存在处理，架上也无
    authed.patch(f"/api/admin/media-sets/{sid}",
                 json={"visibility_status": "hidden"})
    assert authed.get(f"/api/user/collections/{sid}").status_code == 404
    r = authed.get("/api/user/collections/shelves")
    assert r.json()["collections"] == []
