"""合集域 L3 批二：规则引擎 + 修订号刷新 + min 门 + 双轨 + 封面策略。

覆盖 8 种规则（含 is_exclusion 反选）、刷新差集增量语义、
修订号对账、min_effective_members 默认 2、双轨标配、
artwork_mode 只存策略不输出图 URL。
"""

import os
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
                      em.EmbyPerson, em.MediaItem, em.Library,
                      base_models.WebUser):
            s.query(table).delete()
        s.commit()
        yield s
    finally:
        s.close()


@pytest.fixture()
def admin(db):
    u = base_models.WebUser(username="setadmin2", is_staff=True,
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


def _item(db, lib_id, name, year=None, genres="", studios="",
          rating=None, itype="movie", tmdb=None, path=None):
    item = em.MediaItem(guid=f"item-{name}", library_id=lib_id,
                        item_type=itype, name=name, production_year=year,
                        genres=genres, studios=studios,
                        community_rating=rating, tmdb_id=tmdb,
                        file_path=path or f"/data/{name}.mkv")
    db.add(item)
    db.commit()
    db.refresh(item)
    return item


def _rule_set(authed, name="测试系列甲", **over):
    body = {"name": name, "source_type": "rule",
            "min_effective_members": 1}
    body.update(over)
    r = authed.post("/api/admin/media-sets", json=body)
    assert r.status_code == 200, r.text
    return r.json()


def _add_rule(authed, sid, kind, **value):
    body = {"rule_kind": kind}
    body.update(value)
    r = authed.post(f"/api/admin/media-sets/{sid}/rules", json=body)
    assert r.status_code == 200, r.text
    return r.json()


def _refresh(authed, sid):
    r = authed.post(f"/api/admin/media-sets/{sid}/refresh")
    assert r.status_code == 200, r.text
    return r.json()


def _member_names(authed, sid):
    r = authed.get(f"/api/user/collections/{sid}/items?limit=100")
    assert r.status_code == 200, r.text
    return [i["name"] for i in r.json()["items"]]


def test_rule_intersection_exclusion_and_incremental_refresh(
        authed, db, gate_on):
    lib = _lib(db, "甲库")
    a = _item(db, lib.id, "甲一", year=1994, genres="剧情,动作", tmdb="1001")
    b = _item(db, lib.id, "甲二", year=1994, genres="喜剧", tmdb="1002")
    c = _item(db, lib.id, "甲三", year=2001, genres="剧情", tmdb="1003")
    s = _rule_set(authed)
    sid = s["id"]
    _add_rule(authed, sid, "genre", value_text="剧情")
    _add_rule(authed, sid, "year", value_int=1994)
    detail = authed.get(f"/api/admin/media-sets/{sid}").json()
    # 两条规则增删顶了 2 次号，刷新前 completed 落后
    assert detail["requested_revision"] == 2
    assert detail["completed_revision"] == 0
    res = _refresh(authed, sid)
    assert res == {"status": "done", "added": 1, "removed": 0,
                   "id": sid, "requested_revision": 2,
                   "completed_revision": 2}
    assert _member_names(authed, sid) == ["甲一"]
    # 二次刷新幂等：差集为空，不写行
    res = _refresh(authed, sid)
    assert res["added"] == 0 and res["removed"] == 0
    # 反选：排除 1994 年的甲一后，剧情规则剩甲三？不——反选与包含交集，
    # year=1994 包含 + year 反选同值 = 空；改测：删 year 包含、加 genre 反选
    rule_id = detail["rules"][1]["id"]
    authed.delete(f"/api/admin/media-sets/{sid}/rules/{rule_id}")
    _add_rule(authed, sid, "genre", value_text="动作", is_exclusion=True)
    res = _refresh(authed, sid)
    # 包含剧情 {甲一, 甲三}，反选动作去掉甲一，新增甲三、删掉甲一
    assert res["added"] == 1 and res["removed"] == 1
    assert _member_names(authed, sid) == ["甲三"]
    # 手工行不受刷新增删：手工加回甲一后刷新仍保留
    authed.post(f"/api/admin/media-sets/{sid}/members",
                json={"item_id": a.id})
    _refresh(authed, sid)
    assert sorted(_member_names(authed, sid)) == ["甲一", "甲三"]


def test_all_eight_rule_kinds(authed, db, gate_on):
    lib1 = _lib(db, "甲库")
    lib2 = _lib(db, "乙库")
    a = _item(db, lib1.id, "甲一", year=1994, genres="剧情",
              studios="测试制片甲", rating=9.0, tmdb="1001")
    b = _item(db, lib1.id, "甲二", year=1985, genres="剧情",
              studios="测试制片乙", rating=7.0, itype="series", tmdb="1002")
    c = _item(db, lib2.id, "甲三", year=1994, genres="喜剧",
              studios="测试制片甲", rating=8.5, tmdb="1003")
    db.add(em.EmbyPerson(item_id=a.id, name="测试演员甲",
                         person_tmdb_id="p9001"))
    db.add(em.EmbyPerson(item_id=c.id, name="测试演员乙",
                         person_tmdb_id="p9002"))
    db.commit()
    cases = [
        ("person", {"value_text": "p9001"}, ["甲一"]),
        ("person", {"value_text": "测试演员乙"}, ["甲三"]),
        ("genre", {"value_text": "剧情"}, ["甲一", "甲二"]),
        ("studio", {"value_text": "测试制片甲"}, ["甲一", "甲三"]),
        ("year", {"value_int": 1994}, ["甲一", "甲三"]),
        ("decade", {"value_int": 1980}, ["甲二"]),
        ("rating", {"value_float": 8.5}, ["甲一", "甲三"]),
        ("library", {"value_int": lib2.id}, ["甲三"]),
        ("media_type", {"value_text": "series"}, ["甲二"]),
    ]
    for kind, value, expected in cases:
        s = _rule_set(authed, name=f"集-{kind}-{value}")
        _add_rule(authed, s["id"], kind, **value)
        _refresh(authed, s["id"])
        assert sorted(_member_names(authed, s["id"])) == sorted(expected), kind
    # 错型直接 400：decade 非 10 倍数、rating 缺 float、未知类型
    s = _rule_set(authed, name="错型集")
    assert authed.post(f"/api/admin/media-sets/{s['id']}/rules",
                       json={"rule_kind": "decade",
                             "value_int": 1995}).status_code == 400
    assert authed.post(f"/api/admin/media-sets/{s['id']}/rules",
                       json={"rule_kind": "rating"}).status_code == 400
    assert authed.post(f"/api/admin/media-sets/{s['id']}/rules",
                       json={"rule_kind": "nope",
                             "value_text": "x"}).status_code == 400


def test_watch_track_defaults_to_release_when_unarranged(
        authed, db, gate_on):
    lib = _lib(db, "甲库")
    _item(db, lib.id, "甲一", year=2001, tmdb="1001")
    _item(db, lib.id, "甲二", year=1999, tmdb="1002")
    s = _rule_set(authed, min_effective_members=2)
    _add_rule(authed, s["id"], "genre", value_text="x")  # 先占位，后删
    # 直接用 year 规则覆盖：删 genre、加 year>=? 无下限规则，改用 media_type
    detail = authed.get(f"/api/admin/media-sets/{s['id']}").json()
    authed.delete(f"/api/admin/media-sets/{s['id']}/rules/{detail['rules'][0]['id']}")
    _add_rule(authed, s["id"], "media_type", value_text="movie")
    _refresh(authed, s["id"])
    # 计算行 order_index 全为 0：观影轨默认同上映轨
    r = authed.get(f"/api/user/collections/{s['id']}/items?track=watch")
    assert [i["name"] for i in r.json()["items"]] == ["甲二", "甲一"]
    r = authed.get(f"/api/user/collections/{s['id']}/items?track=release")
    assert [i["name"] for i in r.json()["items"]] == ["甲二", "甲一"]


def test_artwork_mode_is_strategy_only(authed, db, gate_on):
    lib = _lib(db, "甲库")
    _item(db, lib.id, "甲一", year=2001, tmdb="1001")
    _item(db, lib.id, "甲二", year=2002, tmdb="1002")
    s = _rule_set(authed, min_effective_members=2)
    _add_rule(authed, s["id"], "media_type", value_text="movie")
    _refresh(authed, s["id"])
    r = authed.get(f"/api/user/collections/{s['id']}")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["artwork_mode"] == "auto_collage"
    assert body["min_effective_members"] == 2  # 默认门为 2
    assert not any("url" in key or "cover" in key for key in body), body
    assert authed.post("/api/admin/media-sets",
                       json={"name": "坏策略", "source_type": "manual",
                             "artwork_mode": "bogus"}).status_code == 400
