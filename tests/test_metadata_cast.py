"""演员数据刮削与 EA 返回（v2.51.0）：TMDB credits → emby_people → EA People。

- 全部在隔离临时 SQLite 里跑，不连生产库；
- 不测真实 TMDB 网络（mock ``TmdbClient._get`` / ``details``）。
"""
import os
import tempfile

os.environ.setdefault("DATABASE_TYPE", "sqlite")
os.environ.setdefault("REDIS_ENABLED", "false")
_fd, _tmppath = tempfile.mkstemp(suffix=".db"); os.close(_fd)
os.environ["DATABASE_URL"] = f"sqlite:///{_tmppath}"
from backend import database as _dbmod
from sqlalchemy import create_engine as _ce
from sqlalchemy.orm import sessionmaker as _sm
_dbmod.engine = _ce(os.environ["DATABASE_URL"])
# 必须走 configure_session_local：直接赋值会把 SessionLocal 代理顶掉，
# 之后各模块 import 到的是被冻结的真 factory，又会查旧库。
_dbmod.configure_session_local(_sm(bind=_dbmod.engine))

import uuid
from unittest import mock

import pytest
from sqlalchemy import inspect as _inspect
from sqlalchemy import text as _text

from backend.database import SessionLocal, init_db
from backend.emby_server import models as em
from backend.emby_server import enrich_worker
from backend.emby_server import api as emby_api
from backend.emby_server.tmdb import TmdbClient, cast_list, _has_credits

init_db()


def _clear_media_tables(s):
    """清空本文件用到的三张表（进出各一次，避免跨文件污染共享库）。"""
    s.rollback()
    s.query(em.EmbyPerson).delete()
    s.query(em.MediaStream).delete()
    s.query(em.MediaItem).delete()
    s.commit()


@pytest.fixture()
def db():
    s = SessionLocal()
    try:
        # 每个测试前后都清空（隔离，避免测试间污染）。
        # 只在测试前清不够：文件内最后一个用例的残留行会漏进共享库，
        # 污染后续文件的全库计数断言（#1034 冒烟实证：
        # test_person_image_404_cases 残留的 pending 行让
        # test_probe_enrich_excludes_merged_copies 的 backlog 被多算 1）。
        _clear_media_tables(s)
        yield s
        try:
            _clear_media_tables(s)
        except Exception:
            # teardown 清理绝不能掩盖用例本身的失败
            s.rollback()
    finally:
        s.close()


def _make_item(db, **kw):
    it = em.MediaItem(
        guid=uuid.uuid4().hex,
        library_id=kw.pop("library_id", 1),
        item_type=kw.pop("item_type", "movie"),
        name=kw.pop("name", "测试电影"),
        file_path=kw.pop("file_path", "/tmp/x.mp4"),
        enrich_status=kw.pop("enrich_status", "pending"),
        **kw,
    )
    db.add(it)
    db.commit()
    return it


def _cast_payload(n, with_profile=True):
    cast = []
    for i in range(n):
        c = {"name": f"演员{i}", "character": f"角色{i}", "order": i}
        if with_profile:
            c["profile_path"] = f"/p{i}.jpg"
        cast.append(c)
    return {"credits": {"cast": cast}}


# ---------- cast_list ----------

def test_cast_list_shape_and_limit():
    got = cast_list(_cast_payload(15))
    assert len(got) == 10  # 只取前 10
    first = got[0]
    assert first["name"] == "演员0"
    assert first["role"] == "角色0"
    assert first["image"].endswith("/w185/p0.jpg")
    assert [c["sort_order"] for c in got] == list(range(10))


def test_cast_list_skips_empty_names_and_missing_credits():
    assert cast_list({}) == []
    assert cast_list(None) == []
    assert cast_list({"credits": {"cast": []}}) == []
    payload = _cast_payload(3)
    payload["credits"]["cast"].insert(1, {"name": "  ", "character": "x"})
    got = cast_list(payload)
    assert [c["name"] for c in got] == ["演员0", "演员1", "演员2"]
    # sort_order 是返回列表里的位置（被跳过的空名不占位）
    assert [c["sort_order"] for c in got] == [0, 1, 2]


def test_cast_list_no_profile_path_gives_empty_image():
    got = cast_list(_cast_payload(2, with_profile=False))
    assert [c["image"] for c in got] == ["", ""]


def test_has_credits():
    assert _has_credits({"credits": {"cast": []}}) is True
    assert _has_credits({"title": "x"}) is False  # v2.51.0 前的老缓存载荷
    assert _has_credits(None) is True  # 阴性缓存：保持 TTL 内不再重打


# ---------- credits() / details()：不新增请求 ----------

def test_credits_reuses_details_single_request():
    """credits() 走 details() 的 append_to_response，只发一次请求。"""
    client = TmdbClient()
    payload = _cast_payload(3)
    payload["title"] = "测试"
    with mock.patch.object(
        TmdbClient, "_get",
        return_value=dict(payload),
    ) as m_get, mock.patch(
        "backend.emby_server.tmdb.tmdb_cache"
    ) as m_cache:
        m_cache.load_details.return_value = (False, None)
        got = client.credits("123", "movie")
    assert m_get.call_count == 1
    _, params = m_get.call_args[0][0], m_get.call_args[0][1]
    assert "credits" in params["append_to_response"]
    assert [c["name"] for c in got] == ["演员0", "演员1", "演员2"]


def test_details_refetches_stale_cache_without_credits():
    """老缓存（无 credits 键）视为过期重拉一次；新缓存直接命中不再请求。"""
    client = TmdbClient()
    stale = {"title": "老载荷"}  # v2.51.0 前缓存的形状
    fresh = dict(_cast_payload(2))
    fresh["title"] = "新载荷"
    with mock.patch.object(
        TmdbClient, "_get", return_value=fresh
    ) as m_get, mock.patch(
        "backend.emby_server.tmdb.tmdb_cache"
    ) as m_cache:
        # 磁盘与内存都没有 → 第一次真的请求
        m_cache.load_details.return_value = (False, None)
        first = client.details("42", "movie")
        assert m_get.call_count == 1
        assert _has_credits(first)
        # L1 内存里现在是带 credits 的新载荷 → 第二次不再请求
        second = client.details("42", "movie")
        assert m_get.call_count == 1
        assert second is first


# ---------- apply_details：国家/语言 ----------

def test_apply_details_fills_countries_languages():
    client = TmdbClient()
    item = em.MediaItem(name="x")
    data = {
        "origin_country": ["US", "GB"],
        "spoken_languages": [
            {"english_name": "English", "iso_639_1": "en"},
            {"english_name": "普通话", "iso_639_1": "zh"},
        ],
        "original_language": "en",
    }
    client.apply_details(item, data)
    assert item.countries == "US,GB"
    assert item.languages == "English,普通话"


def test_apply_details_languages_falls_back_to_original_language():
    client = TmdbClient()
    item = em.MediaItem(name="x")
    client.apply_details(item, {"original_language": "ja"})
    assert item.languages == "ja"


def test_apply_details_does_not_overwrite_existing():
    client = TmdbClient()
    item = em.MediaItem(name="x", countries="CN", languages="中文")
    client.apply_details(item, {
        "origin_country": ["US"],
        "spoken_languages": [{"english_name": "English"}],
    })
    assert item.countries == "CN"
    assert item.languages == "中文"


# ---------- enrich：写 emby_people（幂等） ----------

def _fetched_with_cast(n=3):
    details = _cast_payload(n)
    details["title"] = "测试电影"
    details["origin_country"] = ["US"]
    # 真实包里 details 总是配着 tmdb_id（NFO 路径）或 tmdb_hit（搜索路径）来的
    return {"tmdb_id": "123", "tmdb_hit": None, "tmdb_details": details, "ok": True}


def test_enrich_apply_writes_people(db):
    item = _make_item(db, tmdb_id="123")
    enrich_worker._enrich_apply(db, item, _fetched_with_cast(3))
    db.commit()
    rows = db.query(em.EmbyPerson).filter(
        em.EmbyPerson.item_id == item.id).order_by(em.EmbyPerson.sort_order).all()
    assert len(rows) == 3
    assert rows[0].name == "演员0"
    assert rows[0].role == "角色0"
    assert rows[0].image.endswith("/w185/p0.jpg")
    assert [r.sort_order for r in rows] == [0, 1, 2]
    # 同一轮里国家/语言也落库了（apply_details 路径）
    assert item.countries == "US"


def test_enrich_apply_people_idempotent(db):
    """已有演员行时不再写：补全重跑不产生重复行。"""
    item = _make_item(db, tmdb_id="123")
    enrich_worker._enrich_apply(db, item, _fetched_with_cast(3))
    db.commit()
    enrich_worker._enrich_apply(db, item, _fetched_with_cast(3))
    db.commit()
    n = db.query(em.EmbyPerson).filter(em.EmbyPerson.item_id == item.id).count()
    assert n == 3


def test_enrich_apply_without_credits_writes_nothing(db):
    item = _make_item(db, tmdb_id="123")
    enrich_worker._enrich_apply(db, item, {"tmdb_details": {"title": "x"}, "ok": True})
    db.commit()
    assert db.query(em.EmbyPerson).count() == 0


def test_enrich_apply_cast_failure_does_not_break_main_flow(db, monkeypatch):
    """演员落库抛异常也不该影响主流程（条目照常 done）。"""
    # 2026-10-21 A 类开关摘面板：字段级完成口径缺省改为开（=生产现值），
    # 本用例验证的是「字段级口径关」时的核心完成线，按 env 显式关闭
    monkeypatch.setenv("ENRICH_FIELD_COMPLETENESS", "0")
    # 刮削调优有 60s 进程缓存：清掉，让本用例的 env 立即生效
    monkeypatch.setitem(enrich_worker._enrich_tuning_cache, "cfg", None)
    item = _make_item(db, tmdb_id="123", name="主流程")
    with mock.patch(
        "backend.emby_server.tmdb.cast_list", side_effect=RuntimeError("boom")):
        enrich_worker._enrich_apply(db, item, _fetched_with_cast(2))
    db.commit()
    assert item.enrich_status == "done"


# ---------- EA：People / Countries / Languages ----------

def test_people_dto_format(db):
    item = _make_item(db)
    db.add(em.EmbyPerson(item_id=item.id, name="张三", role="主角",
                         image="https://img/p.jpg", sort_order=1))
    db.add(em.EmbyPerson(item_id=item.id, name="李四", role="配角",
                         image="", sort_order=0))
    db.commit()
    got = emby_api._people_dto(item, db, {})
    assert got == [
        {"Name": "李四", "Id": mock.ANY, "Role": "配角", "Type": "Actor"},
        {"Name": "张三", "Id": mock.ANY, "Role": "主角", "Type": "Actor",
         "PrimaryImageTag": mock.ANY},
    ]
    # 有头像才给 PrimaryImageTag（客户端只对有标记的发图片请求）
    assert "PrimaryImageTag" not in got[0]
    assert got[1]["PrimaryImageTag"]


def test_people_dto_uses_prefetch(db):
    """列表页走预取：_people_dto 不再发 SQL（用预取里的行）。

    包括「预取跑过、但该条目没有演员」的情况——这时也不能回退单查，
    否则列表页每个没演员的条目都多一次查询，预取就白做了。
    """
    item = _make_item(db)
    db.add(em.EmbyPerson(item_id=item.id, name="王五", role="", image="",
                         sort_order=0))
    naked = _make_item(db, name="没演员")
    db.commit()
    emby_api._prefetch_list_data(db, 1, [item, naked])
    prefetch = emby_api._prefetched(db)
    assert item.id in prefetch["people"]
    with mock.patch.object(db, "query", side_effect=AssertionError("N+1")):
        got = emby_api._people_dto(item, db, prefetch)
        got_naked = emby_api._people_dto(naked, db, prefetch)
    assert got == [{"Name": "王五", "Id": mock.ANY, "Role": "", "Type": "Actor"}]
    assert got_naked == []


def test_item_dto_people_countries_languages(db):
    item = _make_item(db, countries="US,GB", languages="English,中文")
    db.add(em.EmbyPerson(item_id=item.id, name="张三", role="主角",
                         image="https://img/p.jpg", sort_order=0))
    db.commit()
    dto = emby_api._item_dto(item, "http://x", 1, db)
    assert dto["Countries"] == ["US", "GB"]
    assert dto["Languages"] == ["English", "中文"]
    assert dto["People"][0]["Name"] == "张三"
    assert dto["People"][0]["Type"] == "Actor"


def test_item_dto_empty_cast_stays_empty_array(db):
    """没刮到演员的条目：People 仍是 []（客户端对 [] 做 .length 不会崩）。"""
    item = _make_item(db)
    dto = emby_api._item_dto(item, "http://x", 1, db)
    assert dto["People"] == []
    assert dto["Countries"] == []
    assert dto["Languages"] == []


# ---------- /emby/Persons 与演员头像 ----------

def test_persons_list_returns_scraped_people(db):
    from backend.emby_server import compat_routes
    item = _make_item(db)
    other = _make_item(db, name="另一部")
    # 同名两条（不同条目）：合并为一条，Id 取最早的
    db.add(em.EmbyPerson(item_id=item.id, name="张三", role="主角",
                         image="https://img/z.jpg", sort_order=0))
    db.add(em.EmbyPerson(item_id=other.id, name="张三", role="客串",
                         image="", sort_order=5))
    db.add(em.EmbyPerson(item_id=item.id, name="李四", role="配角",
                         image="", sort_order=1))
    db.commit()
    got = compat_routes.persons_list(None, None, db)
    assert got["TotalRecordCount"] == 2
    assert got["StartIndex"] == 0
    by_name = {p["Name"]: p for p in got["Items"]}
    # 张三：同名里有一条有头像 → 给 PrimaryImageTag
    assert by_name["张三"]["ImageTags"] == {"Primary": mock.ANY}
    # 李四：没头像 → 不给
    assert by_name["李四"]["ImageTags"] == {}
    assert by_name["李四"]["BackdropImageTags"] == []


def test_persons_list_pagination(db):
    from backend.emby_server import compat_routes
    item = _make_item(db)
    for i in range(5):
        db.add(em.EmbyPerson(item_id=item.id, name=f"演员{i:02d}",
                             role="", image="", sort_order=i))
    db.commit()
    page1 = compat_routes.persons_list(None, None, db, Limit=2, StartIndex=0)
    page2 = compat_routes.persons_list(None, None, db, Limit=2, StartIndex=2)
    assert page1["TotalRecordCount"] == 5
    assert [p["Name"] for p in page1["Items"]] == ["演员00", "演员01"]
    assert [p["Name"] for p in page2["Items"]] == ["演员02", "演员03"]


def _fake_request():
    from starlette.requests import Request
    return Request({"type": "http", "method": "GET", "headers": []})


def test_person_image_serves_localized_file(db, tmp_path):
    from backend.emby_server import media_routes
    from fastapi import HTTPException
    item = _make_item(db)
    db.add(em.EmbyPerson(item_id=item.id, name="张三", role="主角",
                         image="https://img/z.jpg", sort_order=0))
    db.commit()
    avatar = tmp_path / "z.jpg"
    avatar.write_bytes(b"\xff\xd8fakejpeg")
    with mock.patch.object(
        media_routes.image_store, "localize", return_value=str(avatar)
    ), mock.patch.object(
        media_routes, "_serve_sized", return_value="SERVED") as m_serve:
        got = media_routes.person_image("张三", _fake_request(), db)
    assert got == "SERVED"
    m_serve.assert_called_once_with(str(avatar), None, None)


def test_person_image_404_cases(db):
    from backend.emby_server import media_routes
    from fastapi import HTTPException
    item = _make_item(db)
    db.add(em.EmbyPerson(item_id=item.id, name="无头像", role="",
                         image="", sort_order=0))
    db.add(em.EmbyPerson(item_id=item.id, name="坏地址", role="",
                         image="ftp://x/y.jpg", sort_order=1))
    db.commit()
    # 查无此人
    with pytest.raises(HTTPException) as e1:
        media_routes.person_image("不存在", _fake_request(), db)
    assert e1.value.status_code == 404
    # 有行但没头像
    with pytest.raises(HTTPException) as e2:
        media_routes.person_image("无头像", _fake_request(), db)
    assert e2.value.status_code == 404
    # 非 http(s) 地址（防 SSRF）
    with pytest.raises(HTTPException) as e3:
        media_routes.person_image("坏地址", _fake_request(), db)
    assert e3.value.status_code == 404
    # 远程图本地化失败 → 404 而不是 5xx
    db.add(em.EmbyPerson(item_id=item.id, name="挂了的图", role="",
                         image="https://img/dead.jpg", sort_order=2))
    db.commit()
    with mock.patch.object(
        media_routes.image_store, "localize", return_value=""), \
        pytest.raises(HTTPException) as e4:
        media_routes.person_image("挂了的图", _fake_request(), db)
    assert e4.value.status_code == 404

# ---------- 迁移 ----------

def test_migration_adds_countries_languages_columns():
    """_auto_migrate 给老库补上 countries / languages 列（幂等）。"""
    cols = {c["name"] for c in _inspect(_dbmod.engine).get_columns("emby_items")}
    assert "countries" in cols
    assert "languages" in cols
    # 新表由 create_all 建
    tables = set(_inspect(_dbmod.engine).get_table_names())
    assert "emby_people" in tables


def test_auto_migrate_is_idempotent_for_new_columns():
    """删掉两列再跑一次 _auto_migrate：能补回来，且重复跑不报错。"""
    with _dbmod.engine.begin() as conn:
        conn.execute(_text("ALTER TABLE emby_items DROP COLUMN countries"))
        conn.execute(_text("ALTER TABLE emby_items DROP COLUMN languages"))
    _dbmod._auto_migrate()
    _dbmod._auto_migrate()  # 第二次：列已存在，跳过
    cols = {c["name"] for c in _inspect(_dbmod.engine).get_columns("emby_items")}
    assert "countries" in cols
    assert "languages" in cols
