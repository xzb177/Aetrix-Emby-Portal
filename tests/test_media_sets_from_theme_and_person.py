"""从类型主题周、从人物生成合集。

主题周是手工快照（对齐热播榜入口的空结果与总闸）；人物是一条
person 规则加一次同步刷新（对齐模板实例化）。隔离临时 SQLite，
不连生产库。
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
from backend.emby_server import portal

init_db()

_THEME = "/api/admin/media-sets/from-genre-theme"
_PERSON = "/api/admin/media-sets/from-person"


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
    u = base_models.WebUser(username="themesetadmin", is_staff=True,
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
    from backend.main import app

    app.dependency_overrides[get_current_admin] = lambda: admin
    try:
        yield client
    finally:
        del app.dependency_overrides[get_current_admin]


@pytest.fixture()
def gate_on(monkeypatch):
    monkeypatch.setattr(ms, "COLLECTIONS_ENABLED", 1)
    yield


def _lib(db):
    lib = em.Library(guid="lib-theme-person", name="主题库", is_enabled=True)
    db.add(lib)
    db.commit()
    db.refresh(lib)
    return lib


def _item(db, lib_id, name, *, item_type="movie", genres="", year=None,
          rating=None, premiere=None, tmdb=None, hidden=False,
          merged_into=None):
    item = em.MediaItem(
        guid=f"item-{name}", library_id=lib_id, item_type=item_type,
        name=name, genres=genres, production_year=year,
        community_rating=rating, premiere_date=premiere, tmdb_id=tmdb,
        is_hidden=hidden, merged_into_id=merged_into,
        file_path=f"/data/{name}.mkv")
    db.add(item)
    db.commit()
    db.refresh(item)
    return item


def _member_ids(payload):
    members = sorted(payload["members"], key=lambda m: m["order_index"])
    return [m["item_id"] for m in members]


def test_genre_theme_empty_does_not_create(authed, db, gate_on):
    lib = _lib(db)
    _item(db, lib.id, "喜剧片", genres="喜剧", rating=9, tmdb="2001")
    window_naive, _utc = portal.leaderboard_window("day")

    empty = authed.post(_THEME, json={"genre": " 科幻 "})
    assert empty.status_code == 200, empty.text
    body = empty.json()
    assert body["created"] is False
    assert body["reason"] == "theme_empty"
    assert body["board"] == "theme"
    assert body["genre"] == "科幻"
    assert body["members"] == []
    assert body["window_start"] == window_naive.isoformat()
    assert body["window_start"].endswith("T00:00:00")
    assert db.query(em.EmbyMediaSet).count() == 0


def test_genre_theme_ranks_visible_titles(authed, db, gate_on):
    """评分序、人数上限、类型定界，以及隐藏/合并/下架/季集不进片单。"""
    lib = _lib(db)
    low = _item(db, lib.id, "低分", genres="科幻", year=2010,
                rating=6.0, tmdb="3001")
    top = _item(db, lib.id, "高分", genres="科幻, 冒险", year=2024,
                rating=9.2, tmdb="3002")
    mid = _item(db, lib.id, "中分", genres=" 科幻 ", year=2001,
                rating=8.0, tmdb="3003")
    tie = _item(db, lib.id, "同分靠后", genres="科幻", year=2002,
                rating=8.0, tmdb="3004")
    unrated = _item(db, lib.id, "无评分", genres="科幻", year=2024,
                    rating=None, tmdb="3005")
    _item(db, lib.id, "类型子串", genres="科幻片", rating=10, tmdb="3006")
    _item(db, lib.id, "隐藏", genres="科幻", rating=10, tmdb="3007",
          hidden=True)
    _item(db, lib.id, "副本", genres="科幻", rating=10, tmdb="3008",
          merged_into=top.id)
    # 先建完再标删除：commit 后软删钩子会让 refresh 读不到这行。
    deleted = _item(db, lib.id, "下架", genres="科幻", rating=10, tmdb="3009")
    deleted.deleted_at = datetime.now()
    db.commit()
    _item(db, lib.id, "单集", genres="科幻", rating=10, tmdb="3010",
          item_type="episode")
    _item(db, lib.id, "季", genres="科幻", rating=10, tmdb="3011",
          item_type="season")
    series = _item(db, lib.id, "剧", genres="科幻", year=2022,
                   rating=8.5, tmdb="3012", item_type="series")
    old = _item(db, lib.id, "老片", genres="科幻", year=1990,
                rating=9.5, tmdb="3013")
    _item(db, lib.id, "喜剧", genres="喜剧", rating=10, tmdb="3014")

    window_naive, _utc = portal.leaderboard_window("day")
    date_label = window_naive.strftime("%Y-%m-%d")
    full_order = [old.id, top.id, series.id, mid.id, tie.id, low.id,
                  unrated.id]

    full = authed.post(_THEME, json={"genre": "科幻"})
    assert full.status_code == 200, full.text
    body = full.json()
    assert body["created"] is True
    assert body["source_type"] == "manual"
    assert body["board"] == "theme"
    assert body["genre"] == "科幻"
    assert body["window_start"] == window_naive.isoformat()
    assert body["name"] == f"主题周 · 科幻 · {date_label}"
    assert _member_ids(body) == full_order
    assert [m["order_index"] for m in body["members"]] == list(
        range(1, len(full_order) + 1))
    assert all(m["member_source"] == "manual" for m in body["members"])
    assert body["member_count"] == len(full_order)
    assert body["requested_revision"] == 1
    detail = authed.get(f"/api/admin/media-sets/{body['id']}")
    assert detail.status_code == 200, detail.text
    assert _member_ids(detail.json()) == full_order

    capped = authed.post(_THEME, json={"genre": "科幻", "limit": 2,
                                       "name": " 只留两部 "})
    assert capped.status_code == 200, capped.text
    capped_body = capped.json()
    assert capped_body["name"] == "只留两部"
    assert _member_ids(capped_body) == [old.id, top.id]
    assert capped_body["member_count"] == 2

    recent = authed.post(_THEME, json={"genre": "科幻", "min_year": 2000,
                                       "media_type": "Movie"})
    assert recent.status_code == 200, recent.text
    assert _member_ids(recent.json()) == [top.id, mid.id, tie.id, low.id,
                                          unrated.id]

    only_series = authed.post(
        _THEME, json={"genre": "科幻", "media_type": "series"})
    assert only_series.status_code == 200, only_series.text
    assert _member_ids(only_series.json()) == [series.id]

    # 两次调用是两份快照，没有稳定键。
    assert full.json()["id"] != capped_body["id"]
    assert db.query(em.EmbyMediaSet).count() == 4


def test_genre_theme_premiere_and_random(authed, db, gate_on):
    lib = _lib(db)
    older = _item(db, lib.id, "较早", genres="悬疑",
                  premiere=datetime(2018, 5, 1), tmdb="4001")
    newer = _item(db, lib.id, "较新", genres="悬疑",
                  premiere=datetime(2024, 1, 2), tmdb="4002")
    undated = _item(db, lib.id, "无档期", genres="悬疑",
                    premiere=None, tmdb="4003")

    premiered = authed.post(_THEME, json={"genre": "悬疑", "sort": "premiere"})
    assert premiered.status_code == 200, premiered.text
    assert _member_ids(premiered.json()) == [newer.id, older.id, undated.id]

    picked = authed.post(_THEME, json={"genre": "悬疑", "sort": "random",
                                       "limit": 2})
    assert picked.status_code == 200, picked.text
    got = _member_ids(picked.json())
    assert len(got) == 2
    assert set(got) <= {older.id, newer.id, undated.id}
    assert [m["order_index"] for m in picked.json()["members"]] == [1, 2]


def test_genre_theme_rejects_bad_input(authed, db, gate_on):
    lib = _lib(db)
    _item(db, lib.id, "有片", genres="科幻", rating=8, tmdb="5001")
    assert authed.post(_THEME, json={}).status_code == 422
    assert authed.post(_THEME, json={"genre": "   "}).status_code == 400
    assert authed.post(_THEME, json={"genre": "科幻", "limit": 0}
                       ).status_code == 400
    assert authed.post(_THEME, json={"genre": "科幻", "limit": 51}
                       ).status_code == 400
    assert authed.post(_THEME, json={"genre": "科幻", "sort": "hot"}
                       ).status_code == 400
    assert authed.post(_THEME, json={"genre": "科幻", "media_type": "Episode"}
                       ).status_code == 400
    assert authed.post(_THEME, json={"genre": "科幻", "name": "  "}
                       ).status_code == 400
    assert db.query(em.EmbyMediaSet).count() == 0


def test_person_create_refreshes_rule(authed, db, gate_on):
    """person_key 走人物规则；刷新把同人条目写入，默认名用这个键。"""
    lib = _lib(db)
    a = _item(db, lib.id, "甲片", tmdb="6001")
    b = _item(db, lib.id, "乙片", tmdb="6002")
    _item(db, lib.id, "丙片", tmdb="6003")
    db.add(em.EmbyPerson(item_id=a.id, name="测试演员甲",
                         person_tmdb_id="p9001"))
    db.add(em.EmbyPerson(item_id=b.id, name="测试演员甲",
                         person_tmdb_id="p9001"))
    db.commit()

    r = authed.post(_PERSON, json={"person_key": " p9001 "})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["source_type"] == "rule"
    assert body["name"] == "p9001 出演"
    assert body["bound_person_id"] is None
    assert body["min_effective_members"] == 2
    assert body["refresh"]["status"] == "done"
    assert body["refresh"]["added"] == 2
    assert body["refresh"]["removed"] == 0
    assert body["member_count"] == 2
    assert body["effective_count"] == 2
    assert body["shelf_eligible"] is True
    assert body["requested_revision"] == body["completed_revision"] == 1

    detail = authed.get(f"/api/admin/media-sets/{body['id']}")
    assert detail.status_code == 200, detail.text
    rules = detail.json()["rules"]
    assert len(rules) == 1
    assert rules[0]["rule_kind"] == "person"
    assert rules[0]["is_exclusion"] is False
    assert rules[0]["value_text"] == "p9001"
    assert sorted(m["item_id"] for m in detail.json()["members"]) == [
        a.id, b.id]
    assert all(m["member_source"] == "rule" for m in detail.json()["members"])

    # 姓名键同样命中，且再调一次是另一份合集。
    named = authed.post(_PERSON, json={"person_key": "测试演员甲",
                                      "name": " 演员专辑 "})
    assert named.status_code == 200, named.text
    assert named.json()["name"] == "演员专辑"
    assert named.json()["id"] != body["id"]
    assert named.json()["refresh"]["added"] == 2


def test_person_bound_id_and_short_stock(authed, db, gate_on):
    """绑定行必须存在；不够 min 门也建，上架资格按读时有效数，不补库存。"""
    lib = _lib(db)
    a = _item(db, lib.id, "规则片", tmdb="7001")
    b = _item(db, lib.id, "绑定片", tmdb="7002")
    loose = _item(db, lib.id, "未绑定片", tmdb=None)
    db.add(em.EmbyPerson(item_id=a.id, name="规则演员",
                         person_tmdb_id="p-rule"))
    db.add(em.EmbyPerson(item_id=b.id, name="绑定演员",
                         person_tmdb_id="p-bound"))
    db.add(em.EmbyPerson(item_id=loose.id, name="规则演员",
                         person_tmdb_id="p-rule"))
    db.commit()
    bound_row = db.query(em.EmbyPerson).filter(
        em.EmbyPerson.item_id == b.id).first()

    missing = authed.post(_PERSON, json={"person_key": "p-rule",
                                        "bound_person_id": 999999})
    assert missing.status_code == 400, missing.text
    assert "不存在" in missing.json()["detail"]
    assert db.query(em.EmbyMediaSet).count() == 0

    both = authed.post(_PERSON, json={"person_key": "p-rule",
                                      "bound_person_id": bound_row.id})
    assert both.status_code == 200, both.text
    body = both.json()
    assert body["name"] == "绑定演员 出演"
    assert body["bound_person_id"] == bound_row.id
    assert body["refresh"]["added"] == 3
    assert body["member_count"] == 3
    # 未绑定片在成员里，但不算有效库存，min 默认 2 时仍够上架（另两部已绑定）。
    assert body["effective_count"] == 2
    assert body["shelf_eligible"] is True
    detail = authed.get(f"/api/admin/media-sets/{body['id']}").json()
    assert sorted(m["item_id"] for m in detail["members"]) == [
        a.id, b.id, loose.id]

    only = _item(db, lib.id, "仅一部", tmdb="7003")
    db.add(em.EmbyPerson(item_id=only.id, name="独角",
                         person_tmdb_id="p-solo"))
    db.commit()
    short = authed.post(_PERSON, json={"person_key": "p-solo",
                                       "min_effective_members": 2})
    assert short.status_code == 200, short.text
    short_body = short.json()
    assert short_body["refresh"]["added"] == 1
    assert short_body["member_count"] == 1
    assert short_body["effective_count"] == 1
    assert short_body["shelf_eligible"] is False
    assert short_body["name"] == "p-solo 出演"


def test_person_rejects_bad_input(authed, db, gate_on):
    assert authed.post(_PERSON, json={}).status_code == 422
    assert authed.post(_PERSON, json={"person_key": "  "}
                       ).status_code == 400
    assert authed.post(_PERSON, json={"person_key": "某人",
                                      "min_effective_members": -1}
                       ).status_code == 400
    assert authed.post(_PERSON, json={"person_key": "某人", "name": ""}
                       ).status_code == 400
    assert db.query(em.EmbyMediaSet).count() == 0


def test_missing_credentials_are_401(client, db, gate_on):
    assert client.post(_THEME, json={"genre": "科幻"}).status_code == 401
    assert client.post(_PERSON, json={"person_key": "p1"}).status_code == 401
    assert db.query(em.EmbyMediaSet).count() == 0
    paths = client.app.openapi()["paths"]
    assert "/api/admin/media-sets/from-genre-theme" in paths
    assert "/api/admin/media-sets/from-person" in paths
    assert not any(path.startswith("/api/user/") and (
        path.endswith("/from-genre-theme") or path.endswith("/from-person"))
        for path in paths)


def test_gate_off_matches_other_collection_admin_routes(authed, db, monkeypatch):
    monkeypatch.setattr(ms, "COLLECTIONS_ENABLED", 0)
    blocked_theme = authed.post(_THEME, json={"genre": "科幻"})
    blocked_person = authed.post(_PERSON, json={"person_key": "p1"})
    neighbor = authed.post("/api/admin/media-sets", json={"name": "不该建成"})
    assert blocked_theme.status_code == 404
    assert blocked_person.status_code == 404
    assert blocked_theme.json()["detail"] == neighbor.json()["detail"]
    assert blocked_person.json()["detail"] == neighbor.json()["detail"]
    assert db.query(em.EmbyMediaSet).count() == 0
