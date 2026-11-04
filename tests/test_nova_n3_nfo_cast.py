"""Nova N3-1：NFO 演员解析与落库（NovaEmby 全面学习立项 N3）。

Nova 实证口径（novaemby-deep-raw §3b）：自写 NFO 里的两名演员
（NFO Actor One/Lead Role、NFO Actor Two/Support Role）在 normal 与
readonly 两库都落进人物表（name/role 逐项一致）。

Aetrix 此前：``nfo.py`` 全文不解析 ``<actor>``，演员只来自 TMDB 详情
与豆瓣兜底。本文件钉住新口径：

- ``apply_nfo(..., db=db)`` 把 NFO 演员写进既有 ``emby_people`` 管道
  （item_id/name/role/image/sort_order/person_tmdb_id），不新造一套；
- 落库边界与既有演员管道（enrich ``_apply_cast``/``_apply_douban_cast``）
  同款幂等口径：条目已有演员行时不新增、不删、不重排，只按名字给
  已有行补空 role / 空头像（NFO 不与 TMDB/豆瓣演员表互相覆盖）；
- 不传 db 的旧调用（纯字段落库）行为逐字不变。
"""
import os

os.environ.setdefault("DATABASE_TYPE", "sqlite")
os.environ.setdefault("REDIS_ENABLED", "false")

import uuid
from types import SimpleNamespace

import pytest

from backend.database import SessionLocal, init_db
from backend.emby_server import models as em
from backend.emby_server import nfo as nfo_lib

init_db()

MOVIE_NFO_WITH_CAST = """<?xml version="1.0" encoding="UTF-8" standalone="yes" ?>
<movie>
  <title>NFO Custom Title Deep</title>
  <plot>Custom plot</plot>
  <year>2019</year>
  <rating>8.7</rating>
  <uniqueid type="tmdb" default="true">999002</uniqueid>
  <actor>
    <name>NFO Actor One</name>
    <role>Lead Role</role>
    <type>Actor</type>
  </actor>
  <actor>
    <name>NFO Actor Two</name>
    <role>Support Role</role>
  </actor>
</movie>
"""

TVSHOW_NFO_WITH_CAST = """<?xml version="1.0" encoding="UTF-8" standalone="yes" ?>
<tvshow>
  <title>NFO Series Deep</title>
  <plot>Series plot</plot>
  <actor>
    <name>NFO Actor One</name>
    <role>Lead Role</role>
  </actor>
</tvshow>
"""

MOVIE_NFO_NO_CAST = """<?xml version="1.0" encoding="UTF-8" standalone="yes" ?>
<movie>
  <title>No Cast Film</title>
  <plot>Nothing here</plot>
</movie>
"""


def _clear(db, item_ids):
    db.rollback()
    if item_ids:
        db.query(em.EmbyPerson).filter(
            em.EmbyPerson.item_id.in_(item_ids)).delete(
                synchronize_session=False)
        db.query(em.MediaItem).filter(
            em.MediaItem.id.in_(item_ids)).delete(synchronize_session=False)
    db.commit()


@pytest.fixture()
def db():
    s = SessionLocal()
    made = []
    try:
        yield s, made
        _clear(s, made)
    finally:
        s.close()


def _make_item(s, made, item_type="movie", name="测试条目"):
    it = em.MediaItem(
        guid=uuid.uuid4().hex, library_id=1, item_type=item_type,
        name=name, file_path="/tmp/nfo-cast.mp4", enrich_status="pending",
    )
    s.add(it)
    s.commit()
    made.append(it.id)
    return it


def _people(s, item_id):
    return s.query(em.EmbyPerson).filter(
        em.EmbyPerson.item_id == item_id).order_by(
            em.EmbyPerson.sort_order, em.EmbyPerson.id).all()


def test_apply_nfo_writes_cast_rows(db):
    """电影 NFO 演员落库：两行、name/role/顺序与 NFO 逐项一致"""
    s, made = db
    item = _make_item(s, made)
    data = nfo_lib.parse_nfo(MOVIE_NFO_WITH_CAST)
    nfo_lib.apply_nfo(item, data, "movie", db=s)
    s.commit()
    rows = _people(s, item.id)
    assert [(r.name, r.role, r.sort_order) for r in rows] == [
        ("NFO Actor One", "Lead Role", 0),
        ("NFO Actor Two", "Support Role", 1),
    ]
    assert all(r.person_tmdb_id == "" for r in rows)


def test_apply_nfo_series_cast_rows(db):
    """剧集（tvshow）NFO 演员同样落库"""
    s, made = db
    item = _make_item(s, made, item_type="series")
    data = nfo_lib.parse_nfo(TVSHOW_NFO_WITH_CAST)
    nfo_lib.apply_nfo(item, data, "series", db=s)
    s.commit()
    rows = _people(s, item.id)
    assert [(r.name, r.role) for r in rows] == [
        ("NFO Actor One", "Lead Role")]


def test_apply_nfo_cast_idempotent(db):
    """重扫再 apply 一次：不产生重复行（既有管道幂等口径）"""
    s, made = db
    item = _make_item(s, made)
    data = nfo_lib.parse_nfo(MOVIE_NFO_WITH_CAST)
    nfo_lib.apply_nfo(item, data, "movie", db=s)
    s.commit()
    nfo_lib.apply_nfo(item, data, "movie", db=s)
    s.commit()
    assert len(_people(s, item.id)) == 2


def test_apply_nfo_cast_keeps_existing_rows(db):
    """已有演员行（TMDB/豆瓣先写）时：NFO 不新增、不删、不重排"""
    s, made = db
    item = _make_item(s, made)
    s.add(em.EmbyPerson(
        item_id=item.id, name="TMDB Actor", role="主角",
        image="https://image.tmdb.org/t/p/w185/x.jpg", sort_order=0,
        person_tmdb_id="123"))
    s.commit()
    data = nfo_lib.parse_nfo(MOVIE_NFO_WITH_CAST)
    nfo_lib.apply_nfo(item, data, "movie", db=s)
    s.commit()
    rows = _people(s, item.id)
    assert [(r.name, r.role) for r in rows] == [("TMDB Actor", "主角")]


def test_apply_nfo_cast_fills_empty_role_by_name(db):
    """已有同名行但 role 为空：按名字补上 NFO 的 role，头像不覆盖"""
    s, made = db
    item = _make_item(s, made)
    s.add(em.EmbyPerson(
        item_id=item.id, name="NFO Actor One", role="",
        image="https://image.tmdb.org/t/p/w185/y.jpg", sort_order=0,
        person_tmdb_id=""))
    s.commit()
    data = nfo_lib.parse_nfo(MOVIE_NFO_WITH_CAST)
    nfo_lib.apply_nfo(item, data, "movie", db=s)
    s.commit()
    rows = _people(s, item.id)
    assert len(rows) == 1
    assert rows[0].role == "Lead Role"
    assert rows[0].image == "https://image.tmdb.org/t/p/w185/y.jpg"


def test_apply_nfo_no_cast_writes_nothing(db):
    """无演员 NFO 传 db 也不写人物行，文本字段照常落"""
    s, made = db
    item = _make_item(s, made)
    data = nfo_lib.parse_nfo(MOVIE_NFO_NO_CAST)
    nfo_lib.apply_nfo(item, data, "movie", db=s)
    s.commit()
    assert _people(s, item.id) == []
    assert item.name == "No Cast Film"


def test_apply_nfo_without_db_unchanged():
    """不传 db 的旧调用：纯字段落库，签名兼容、不报错"""
    item = SimpleNamespace(
        name="旧名", sort_name="旧名", original_title="", overview="",
        community_rating=None, official_rating="", genres="", studios="",
        tmdb_id=None, imdb_id=None, aliases="", production_year=None,
        last_scraped_at=None,
    )
    data = nfo_lib.parse_nfo(MOVIE_NFO_WITH_CAST)
    nfo_lib.apply_nfo(item, data, "movie")
    assert item.name == "NFO Custom Title Deep"
    assert data["actors"][0]["name"] == "NFO Actor One"
