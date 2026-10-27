"""第三批第二单：自然语言搜片。解析 + 查询 + 路由门禁测试。

口径：本地 Python 3.12（CI 以 3.11 全量为准）；查询测试用内存 SQLite
+ 显式传入可见库集合（路由层真实 library_scope 判定由既有测试覆盖，
这里不冒充它，只验“集合外条目绝不出现”这一查询层契约）。
"""
from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.emby_server import nl_search as nl
from backend.emby_server import models as em


def _enable(monkeypatch):
    monkeypatch.setenv("NL_SEARCH_ENABLED", "1")


def _disable(monkeypatch):
    monkeypatch.setenv("NL_SEARCH_ENABLED", "0")


# ---- 解析 ----

def test_parse_decade_and_region_combo():
    p = nl.parse_query("90年代港片")
    assert p["year_range"] == (1990, 1999)
    assert p["region"] == "香港"
    assert p["keyword"] == ""
    assert p["fallback"] is False


def test_parse_full_decade_and_year():
    assert nl.parse_query("1990年代喜剧")["year_range"] == (1990, 1999)
    assert nl.parse_query("1994年喜剧")["year"] == 1994
    assert nl.parse_query("00年代动画")["year_range"] == (2000, 2009)


def test_parse_genre_and_keyword_kept():
    p = nl.parse_query("喜剧 周星驰")
    assert p["genre"] == "喜剧"
    assert p["keyword"] == "周星驰"
    assert p["fallback"] is False


def test_parse_failure_falls_back_to_keyword():
    p = nl.parse_query("一部讲登月的片子")
    assert p["fallback"] is True
    assert p["keyword"] == "一部讲登月的片子"
    assert p["year_range"] is None and p["region"] is None and p["genre"] is None


def test_parse_empty_falls_back():
    p = nl.parse_query("   ")
    assert p["fallback"] is True and p["keyword"] == ""


# ---- 查询（内存 SQLite + 显式可见集合） ----

def _db():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False},
                           poolclass=StaticPool)
    em.MediaItem.__table__.create(engine, checkfirst=True)
    session = sessionmaker(bind=engine)()
    items = [
        # (id, name, year, countries, genres, library)
        (1, "喜剧之王", 1999, "香港", "喜剧", 1),
        (2, "大话西游", 1995, "香港", "喜剧", 1),
        (3, "泰坦尼克号", 1997, "美国", "爱情", 1),
        (4, "千与千寻", 2001, "日本", "动画", 2),
        (5, "无间道", 2002, "香港", "犯罪", 1),
    ]
    for i, name, year, countries, genres, lib in items:
        session.add(em.MediaItem(id=i, guid=f"{i:032d}", library_id=lib,
                                 item_type="movie", name=name,
                                 production_year=year, countries=countries,
                                 genres=genres))
    session.commit()
    return session


def test_query_multi_condition_and_combination(monkeypatch):
    _enable(monkeypatch)
    db = _db()
    r = nl.search_items(db, "90年代港片", {1})
    assert [x["id"] for x in r["results"]] == [1, 2]
    r = nl.search_items(db, "90年代香港喜剧", {1})
    assert [x["id"] for x in r["results"]] == [1, 2]
    r = nl.search_items(db, "1995年喜剧", {1})
    assert [x["id"] for x in r["results"]] == [2]


def test_query_no_results_is_empty_not_fabricated(monkeypatch):
    _enable(monkeypatch)
    db = _db()
    r = nl.search_items(db, "80年代法国科幻", {1})
    assert r["results"] == [] and r["fallback"] is False


def test_query_fallback_keyword_search(monkeypatch):
    _enable(monkeypatch)
    db = _db()
    r = nl.search_items(db, "无间道", {1})
    assert r["fallback"] is True
    assert [x["id"] for x in r["results"]] == [5]


def test_query_out_of_scope_items_never_appear(monkeypatch):
    _enable(monkeypatch)
    db = _db()
    # 库 2 的条目（千与千寻）在可见集合 {1} 外，即使关键词命中也不出现
    r = nl.search_items(db, "千与千寻", {1})
    assert r["results"] == []
    r = nl.search_items(db, "千与千寻", {1, 2})
    assert [x["id"] for x in r["results"]] == [4]


def test_query_fail_closed_empty_scope(monkeypatch):
    _enable(monkeypatch)
    db = _db()
    r = nl.search_items(db, "喜剧", set())
    assert r["results"] == []


def test_query_disabled_refuses(monkeypatch):
    _disable(monkeypatch)
    db = _db()
    with pytest.raises(nl.NlSearchDisabled):
        nl.search_items(db, "喜剧", {1})


def test_query_results_not_recommendation_ordered(monkeypatch):
    _enable(monkeypatch)
    db = _db()
    r = nl.search_items(db, "喜剧", {1})
    ids = [x["id"] for x in r["results"]]
    assert ids == sorted(ids)


# ---- 路由门禁 ----

def _client(monkeypatch, allowed):
    from backend.api import nl_search as router_mod
    from backend.api.user import get_current_user
    from backend.database import get_db
    from backend import library_scope as _ls
    monkeypatch.setattr(_ls, "effective_ids_safe", lambda db, user: allowed)
    app = FastAPI()
    app.include_router(router_mod.router)
    app.dependency_overrides[get_current_user] = lambda: type(
        "U", (), {"id": 1, "is_staff": False})()
    app.dependency_overrides[get_db] = lambda: _db()
    return TestClient(app)


def test_router_disabled_returns_403(monkeypatch):
    _disable(monkeypatch)
    client = _client(monkeypatch, {1})
    r = client.get("/api/user/nl-search", params={"q": "喜剧"})
    assert r.status_code == 403


def test_router_happy_path_marks_no_fallback(monkeypatch):
    _enable(monkeypatch)
    client = _client(monkeypatch, {1})
    r = client.get("/api/user/nl-search", params={"q": "90年代港片"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["fallback"] is False
    assert [x["id"] for x in body["results"]] == [1, 2]
