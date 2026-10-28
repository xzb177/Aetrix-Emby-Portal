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
    # 人物匹配走 emby_people（生产迁移必有此表），夹具须同建，否则 EXISTS 报错。
    em.EmbyPerson.__table__.create(engine, checkfirst=True)
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


# ---- 虚词剥离 / 国产 / 最近 / 人物匹配（解析 bug 修复） ----

def test_parse_particle_stripped_from_keyword():
    p = nl.parse_query("周星驰的喜剧")
    assert p["genre"] == "喜剧" and p["keyword"] == "周星驰"
    p = nl.parse_query("宫崎骏的动画电影")
    assert p["genre"] == "动画" and p["keyword"] == "宫崎骏"
    p = nl.parse_query("2024年的国产剧")
    assert p["year"] == 2024 and p["region"] == "内地"
    assert p["keyword"] == ""
    # 只剥首尾虚词，词中“之”不动。
    p = nl.parse_query("月之恋的喜剧")
    assert p["genre"] == "喜剧" and p["keyword"] == "月之恋"
    # 纯虚词 token 丢弃，不进查询。
    p = nl.parse_query("的喜剧")
    assert p["genre"] == "喜剧" and p["keyword"] == ""


def test_parse_recent_maps_to_year_range():
    from datetime import date
    y = date.today().year
    p = nl.parse_query("最近的科幻片")
    assert p["year_range"] == (y - 2, y)
    assert p["genre"] == "科幻" and p["keyword"] == ""
    p = nl.parse_query("近期动画")
    assert p["year_range"] == (y - 2, y) and p["genre"] == "动画"
    # 显式年代优先，不被“最近”覆盖。
    p = nl.parse_query("最近1994年喜剧")
    assert p["year"] == 1994 and p["year_range"] is None


def _db_fix():
    from datetime import date
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False},
                           poolclass=StaticPool)
    em.MediaItem.__table__.create(engine, checkfirst=True)
    em.EmbyPerson.__table__.create(engine, checkfirst=True)
    session = sessionmaker(bind=engine)()
    y = date.today().year
    items = [
        # (id, name, year, countries, genres, library)
        (1, "喜剧之王", 1999, "香港", "喜剧", 1),
        (2, "大话西游", 1995, "香港", "喜剧", 1),
        (4, "千与千寻", 2001, "日本", "动画", 2),
        (6, "科幻新片", y, "内地", "科幻", 1),
        (7, "国产剧示例", 2024, "内地", "剧情", 1),
        (8, "星际穿越", 2014, "美国", "科幻", 1),
    ]
    for i, name, year, countries, genres, lib in items:
        session.add(em.MediaItem(id=i, guid=f"{i:032d}", library_id=lib,
                                 item_type="movie", name=name,
                                 production_year=year, countries=countries,
                                 genres=genres))
    # 人物行：只挂在对应条目下（2 号喜剧无周星驰行，防人物匹配误扩大）。
    session.add(em.EmbyPerson(item_id=1, name="周星驰", sort_order=0))
    session.add(em.EmbyPerson(item_id=4, name="宫崎骏", sort_order=0))
    session.commit()
    return session


def test_query_four_reported_queries_return_results(monkeypatch):
    _enable(monkeypatch)
    db = _db_fix()
    r = nl.search_items(db, "周星驰的喜剧", {1})
    assert [x["id"] for x in r["results"]] == [1]
    r = nl.search_items(db, "最近的科幻片", {1})
    assert [x["id"] for x in r["results"]] == [6]
    r = nl.search_items(db, "2024年的国产剧", {1})
    assert [x["id"] for x in r["results"]] == [7]
    r = nl.search_items(db, "宫崎骏的动画电影", {1, 2})
    assert [x["id"] for x in r["results"]] == [4]


def test_query_exact_search_no_regression(monkeypatch):
    _enable(monkeypatch)
    db = _db_fix()
    r = nl.search_items(db, "星际穿越", {1})
    assert r["fallback"] is True
    assert [x["id"] for x in r["results"]] == [8]
    r = nl.search_items(db, "不存在的片名xyz123", {1})
    assert r["results"] == []


# ---- R4 审查修复回归：地区 ISO 代码 / 最近残留 / LIKE 通配转义 ----

def _db_iso():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False},
                           poolclass=StaticPool)
    em.MediaItem.__table__.create(engine, checkfirst=True)
    em.EmbyPerson.__table__.create(engine, checkfirst=True)
    session = sessionmaker(bind=engine)()
    # 生产真实取值：countries 存 TMDB origin_country ISO 代码，不是中文名。
    items = [
        # (id, name, year, countries, genres, library)
        (1, "国产剧示例", 2024, "CN", "剧情", 1),
        (2, "喜剧之王", 1999, "HK", "喜剧", 1),
        (3, "千与千寻", 2001, "JP", "动画", 2),
    ]
    for i, name, year, countries, genres, lib in items:
        session.add(em.MediaItem(id=i, guid=f"{i:032d}", library_id=lib,
                                 item_type="movie", name=name,
                                 production_year=year, countries=countries,
                                 genres=genres))
    session.commit()
    return session


def test_region_matches_production_iso_codes(monkeypatch):
    _enable(monkeypatch)
    db = _db_iso()
    r = nl.search_items(db, "国产剧", {1})
    assert r["region"] == "内地" and [x["id"] for x in r["results"]] == [1]
    r = nl.search_items(db, "香港喜剧", {1})
    assert [x["id"] for x in r["results"]] == [2]
    r = nl.search_items(db, "日本动画", {1, 2})
    assert [x["id"] for x in r["results"]] == [3]


def test_recent_token_never_leaks_into_keyword():
    from datetime import date
    y = date.today().year
    # 显式年代已定时，“最近”须被清除，不能进 LIKE 误收窄。
    p = nl.parse_query("90年代最近的科幻")
    assert p["year_range"] == (1990, 1999) and p["keyword"] == ""
    # 两词并存时只清一处不够，两处都须清。
    p = nl.parse_query("最近近期科幻")
    assert p["year_range"] == (y - 2, y) and p["keyword"] == ""


def test_like_wildcards_in_keyword_are_literal(monkeypatch):
    _enable(monkeypatch)
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False},
                           poolclass=StaticPool)
    em.MediaItem.__table__.create(engine, checkfirst=True)
    em.EmbyPerson.__table__.create(engine, checkfirst=True)
    db = sessionmaker(bind=engine)()
    db.add(em.MediaItem(id=1, guid="00000000000000000000000000000001",
                        library_id=1, item_type="movie", name="100%纯爱",
                        production_year=2020, countries="CN", genres="爱情"))
    db.add(em.MediaItem(id=2, guid="00000000000000000000000000000002",
                        library_id=1, item_type="movie", name="100X纯爱",
                        production_year=2020, countries="CN", genres="爱情"))
    db.commit()
    # % 若当通配符，100X纯爱也会命中；按字面匹配只能命中 100%纯爱。
    r = nl.search_items(db, "100%纯", {1})
    assert r["fallback"] is True
    assert [x["id"] for x in r["results"]] == [1]
