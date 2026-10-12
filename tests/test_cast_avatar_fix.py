"""演员头像 / 角色名修复：People 带 Id、按 Id 取头像、单集回退剧集演员表、
拼音角色名不再冒充角色、缺头像的演员后台补全（TMDB 人物搜索 + 豆瓣兜底）。

隔离临时 SQLite，不连网络（TMDB / 豆瓣全部 mock）。
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
# 测试隔离：backend.database 可能已被之前的测试文件导入，
# 此时模块级的 DATABASE_TYPE 已定死，os.environ.setdefault 改不动它。
# 强制覆盖，保证 init_db() 走 SQLite 分支（fcntl 锁）而非 pg_advisory_lock。
_dbmod.DATABASE_TYPE = "sqlite"
_dbmod.engine = _ce(os.environ["DATABASE_URL"])
_dbmod.configure_session_local(_sm(bind=_dbmod.engine))

import uuid
from unittest import mock

import pytest

from backend.database import SessionLocal, init_db
from backend.emby_server import models as em
from backend.emby_server import api as emby_api
from backend.emby_server import enrich_worker
from backend.emby_server import refresh_person_worker as rpw
from backend.emby_server import douban as dbn
from backend.emby_server.tmdb import cast_list, is_romanized_role

init_db()


@pytest.fixture()
def db():
    s = SessionLocal()
    try:
        s.query(em.EmbyPerson).delete()
        s.query(em.MediaStream).delete()
        s.query(em.MediaItem).delete()
        s.commit()
        rpw._tried_rows.clear()
        rpw._tried_items.clear()
        yield s
    finally:
        s.close()


def _item(db, **kw):
    it = em.MediaItem(guid=uuid.uuid4().hex, library_id=kw.pop("library_id", 1),
                      item_type=kw.pop("item_type", "series"),
                      name=kw.pop("name", "沦陷"),
                      file_path=kw.pop("file_path", None),
                      enrich_status="done", **kw)
    db.add(it)
    db.commit()
    return it


def _person(db, item, name, role="", image="", tmdb="", order=0):
    p = em.EmbyPerson(item_id=item.id, name=name, role=role, image=image,
                      person_tmdb_id=tmdb, sort_order=order)
    db.add(p)
    db.commit()
    return p


# ---------- 拼音角色名 ----------

def test_is_romanized_role():
    assert is_romanized_role("嘉羿", "Zhan Wang")
    assert not is_romanized_role("嘉羿", "展望")
    assert not is_romanized_role("Tom Hanks", "Forrest Gump")
    assert not is_romanized_role("嘉羿", "")


def test_cast_list_drops_pinyin_role_for_chinese_title():
    details = {"original_language": "zh", "credits": {"cast": [
        {"name": "嘉羿", "character": "Zhan Wang", "id": 1},
        {"name": "唐诚之", "character": "许愿", "id": 2},
    ]}}
    got = cast_list(details)
    assert got[0]["name"] == "嘉羿" and got[0]["role"] == ""
    assert got[1]["role"] == "许愿"


def test_cast_list_keeps_latin_role_for_english_title():
    details = {"original_language": "en", "credits": {"cast": [
        {"name": "成龙", "character": "Lee", "id": 1}]}}
    assert cast_list(details)[0]["role"] == "Lee"


# ---------- People DTO ----------

def test_people_dto_has_id_and_episode_falls_back_to_series(db):
    series = _item(db)
    ep = _item(db, item_type="episode", series_id=series.id,
               file_path="/strm/x/S01E01.strm", name="第 1 集")
    p = _person(db, series, "嘉羿", "展望", "https://img/a.jpg")
    got = emby_api._people_dto(ep, db, {})
    assert got == [{"Name": "嘉羿", "Id": str(p.id), "Role": "展望",
                    "Type": "Actor", "PrimaryImageTag": str(p.id)}]


def test_people_prefetch_covers_series_of_episodes(db):
    series = _item(db)
    ep = _item(db, item_type="episode", series_id=series.id,
               file_path="/strm/x/S01E01.strm", name="第 1 集")
    _person(db, series, "唐诚之")
    emby_api._prefetch_list_data(db, 1, [ep])
    prefetch = emby_api._prefetched(db)
    with mock.patch.object(db, "query", side_effect=AssertionError("N+1")):
        got = emby_api._people_dto(ep, db, prefetch)
    assert [g["Name"] for g in got] == ["唐诚之"]


def test_person_item_dto(db):
    series = _item(db)
    p = _person(db, series, "嘉羿", image="https://img/a.jpg", tmdb="42")
    dto = emby_api._person_item_dto(p, db)
    assert dto["Type"] == "Person" and dto["Id"] == str(p.id)
    assert dto["ImageTags"] == {"Primary": str(p.id)}
    assert dto["ProviderIds"] == {"Tmdb": "42"}


def _fake_request():
    from starlette.requests import Request
    return Request({"type": "http", "method": "GET", "headers": []})


def test_item_image_resolves_person_id(db, tmp_path):
    from backend.emby_server import media_routes
    series = _item(db)
    p = _person(db, series, "嘉羿", image="https://img/a.jpg")
    avatar = tmp_path / "a.jpg"
    avatar.write_bytes(b"\xff\xd8x")
    with mock.patch.object(media_routes.image_store, "localize",
                           return_value=str(avatar)), \
            mock.patch.object(media_routes, "_serve_sized",
                              return_value="SERVED") as m_serve:
        got = media_routes.item_image(str(p.id), "Primary", _fake_request(), db)
    assert got == "SERVED"
    m_serve.assert_called_once_with(str(avatar), None, None)


def test_item_image_person_without_avatar_404(db):
    from backend.emby_server import media_routes
    from fastapi import HTTPException
    series = _item(db)
    p = _person(db, series, "无头像")
    with pytest.raises(HTTPException) as e:
        media_routes.item_image(str(p.id), "Primary", _fake_request(), db)
    assert e.value.status_code == 404


# ---------- enrich：已有演员行补空头像 ----------

def test_apply_cast_backfills_existing_rows_and_kicks(db):
    series = _item(db, tmdb_id="316407")
    _person(db, series, "嘉羿")
    details = {"original_language": "zh", "credits": {"cast": [
        {"name": "嘉羿", "character": "Zhan Wang", "id": 7, "profile_path": "/j.jpg"},
        {"name": "无图", "character": "", "id": 8},
    ]}}
    with mock.patch.object(rpw, "kick") as m_kick:
        enrich_worker._apply_cast(db, series, details)
    db.commit()
    rows = db.query(em.EmbyPerson).filter(em.EmbyPerson.item_id == series.id).all()
    assert len(rows) == 1  # 不重复写
    assert rows[0].image.endswith("/w185/j.jpg")
    assert rows[0].person_tmdb_id == "7"
    m_kick.assert_not_called()  # 全都有头像了


def test_apply_cast_new_rows_missing_image_kicks(db):
    series = _item(db, tmdb_id="1")
    details = {"credits": {"cast": [{"name": "无图", "character": "x", "id": 8}]}}
    with mock.patch.object(rpw, "kick") as m_kick:
        enrich_worker._apply_cast(db, series, details)
    db.commit()
    m_kick.assert_called_once()


# ---------- 后台补全 ----------

class _FakeTmdb:
    configured = True

    def __init__(self, responses):
        self.responses = responses
        self.calls = []

    def _get(self, path, params):
        self.calls.append((path, dict(params)))
        r = self.responses.get(path)
        if callable(r):
            return r(params)
        return r


def test_refresh_searches_tmdb_by_name_when_no_person_id(db):
    series = _item(db, name="Some Show")
    a = _person(db, series, "嘉羿")
    b = _person(db, series, "查无此人", order=1)
    fake = _FakeTmdb({"/search/person": lambda p: {"results": [
        {"id": 99, "name": "嘉羿", "profile_path": "/jy.jpg"}]} if p["query"] == "嘉羿"
        else {"results": [{"id": 5, "name": "别人", "profile_path": "/x.jpg"}]}})
    with mock.patch("backend.emby_server.tmdb.tmdb_client", fake), \
            mock.patch.object(rpw, "fill_from_douban", return_value=0):
        n = rpw.refresh_person_images(db)
    db.refresh(a); db.refresh(b)
    assert n == 1
    assert a.image.endswith("/w185/jy.jpg") and a.person_tmdb_id == "99"
    # 名字不完全一致的结果不采用，打标记不再搜
    assert not b.image and b.person_tmdb_id == rpw.NO_TMDB_MARK
    # 第二轮：同一批不再重复打
    fake.calls.clear()
    with mock.patch("backend.emby_server.tmdb.tmdb_client", fake), \
            mock.patch.object(rpw, "fill_from_douban", return_value=0):
        rpw.refresh_person_images(db)
    assert fake.calls == []


def test_dedupe_ignores_no_tmdb_mark(db):
    series = _item(db)
    _person(db, series, "甲", tmdb=rpw.NO_TMDB_MARK)
    _person(db, series, "乙", tmdb=rpw.NO_TMDB_MARK, order=1)
    rpw.deduplicate_persons(db)
    assert db.query(em.EmbyPerson).filter(em.EmbyPerson.item_id == series.id).count() == 2


_CELEB_HTML = """
<ul>
<li class="celebrity">
  <a href="https://www.douban.com/personage/1/" title="薛朗 Xue Lang" class="">
    <div class="avatar" style="background-image: url(https://img1.doubanio.com/view/personage/m/public/d.jpg)"></div></a>
  <div class="info"><span class="name"><a href="#" class="name">薛朗 Xue Lang</a></span>
  <span class="role" title="导演 Director">导演 Director</span></div>
</li>
<li class="celebrity">
  <a href="https://www.douban.com/personage/2/" title="嘉羿 Jia Yi" class="">
    <div class="avatar" style="background-image: url(https://img2.doubanio.com/view/personage/m/public/jy.jpg)"></div></a>
  <div class="info"><span class="name"><a href="#" class="name">嘉羿 Jia Yi</a></span>
  <span class="role" title="演员 Actor (饰 展望)">演员 Actor (饰 展望)</span></div>
</li>
<li class="celebrity">
  <a href="#" title="与七 Yu Qi" class="">
    <div class="avatar" style="background-image: url(https://img9.doubanio.com/f/movie/celebrity-default-medium.png)"></div></a>
  <div class="info"><span class="name"><a href="#" class="name">与七 Yu Qi</a></span>
  <span class="role" title="演员 Actress (饰 展煦)">演员 Actress (饰 展煦)</span></div>
</li>
</ul>
"""


def test_parse_celebrities_and_name_match():
    got = dbn.parse_celebrities(_CELEB_HTML)
    assert got == [
        {"name": "嘉羿 Jia Yi",
         "image": "https://img2.doubanio.com/view/personage/m/public/jy.jpg",
         "role": "展望"},
        {"name": "与七 Yu Qi", "image": "", "role": "展煦"},
    ]
    assert dbn.name_matches("嘉羿", "嘉羿 Jia Yi")
    assert not dbn.name_matches("嘉", "嘉羿 Jia Yi")


def test_fill_from_douban_fills_avatar_and_chinese_role(db):
    series = _item(db, name="沦陷", production_year=2026)
    a = _person(db, series, "嘉羿", role="Zhan Wang", tmdb="11")
    b = _person(db, series, "与七", role="", tmdb="12", order=1)
    c = _person(db, series, "唐诚之", role="许愿", image="https://img/t.jpg", order=2)
    fake = mock.MagicMock()
    fake.search.return_value = {"id": "3700001", "title": "沦陷"}
    fake.get_celebrities.return_value = dbn.parse_celebrities(_CELEB_HTML)
    with mock.patch.object(dbn, "client", fake), \
            mock.patch.object(dbn, "enabled", return_value=True), \
            mock.patch.object(dbn, "min_interval", return_value=0.0):
        n = rpw.fill_from_douban(db)
    for r in (a, b, c):
        db.refresh(r)
    assert n == 1
    assert a.image.endswith("/jy.jpg") and a.role == "展望"
    assert not b.image and b.role == "展煦"   # 默认占位头像不算头像
    assert c.role == "许愿" and c.image == "https://img/t.jpg"  # 已有的不动
    fake.search.assert_called_once_with("沦陷", 2026, "series")
    # 同一条目不会每轮都去打豆瓣
    fake.search.reset_mock()
    with mock.patch.object(dbn, "client", fake), \
            mock.patch.object(dbn, "enabled", return_value=True), \
            mock.patch.object(dbn, "min_interval", return_value=0.0):
        rpw.fill_from_douban(db)
    fake.search.assert_not_called()
