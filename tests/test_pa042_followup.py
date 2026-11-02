"""PA-042：PA-037 残留的同家族两处。

① _query_items 的 DateLastContentAdded 关联子查询（_ChildItem 别名）：
   软删钩子只给外层语句补 deleted_at，标量子查询里的已下架单集仍参与
   max(date_added)，剧集会被已下架的新集抬高最新入库时间。
② _projection_page 的 _live_items_query(db, lib).count()：Query.count()
   包子查询后钩子看不到内层表名，活条目数把已下架算进去，与投影行数
   对不上 → 本可走投影的页被误判覆盖不全、多余回落现算。

隔离 SQLite 内存库，不连生产库；软删钩子装在 Session 类上，
import soft_delete 即生效（默认开）。
"""

from __future__ import annotations

import os
from datetime import datetime

os.environ.setdefault("DATABASE_TYPE", "sqlite")
os.environ.setdefault("REDIS_ENABLED", "false")

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from starlette.requests import Request

from backend import models as base_models
from backend.emby_server import models as em
from backend.emby_server import soft_delete  # noqa: F401 — 装全局软删钩子
from backend.emby_server import api as emby_api
from backend.emby_server import item_sort_projection as proj


@pytest.fixture()
def db():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    base_models.Base.metadata.create_all(engine)
    em.Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    try:
        yield session
    finally:
        session.close()

def _lib(db):
    lib = db.query(em.Library).filter(em.Library.guid == "pa042lib").first()
    if lib is None:
        lib = em.Library(guid="pa042lib", name="pa042lib")
        db.add(lib)
        db.commit()
        db.refresh(lib)
    return lib


def _user(db):
    u = base_models.WebUser(username="pa042user", password_hash="x", is_active=True)
    db.add(u)
    db.commit()
    db.refresh(u)
    return u


def _item(db, guid, **kw):
    deleted = kw.pop("deleted", False)
    kw.setdefault("name", guid)
    kw.setdefault("sort_name", guid)
    kw.setdefault("item_type", "movie")
    kw.setdefault("library_id", _lib(db).id)
    it = em.MediaItem(guid=guid, **kw)
    db.add(it)
    db.commit()
    db.refresh(it)
    item_id = it.id
    if deleted:
        # 软删提交后该实例在 ORM 里即不可见（钩子会把它滤掉），
        # 之后再碰它会报 ObjectDeletedError，故只返回主键 id。
        it.deleted_at = datetime.now()
        db.commit()
    return item_id


def _request(query_string: str):
    scope = {
        "type": "http",
        "method": "GET",
        "path": "/",
        "headers": [],
        "query_string": query_string.encode(),
        "server": ("testserver", 80),
        "scheme": "http",
    }
    return Request(scope)

def test_date_last_content_added_excludes_soft_deleted_episodes(db):
    """① 剧集 A：可见单集 2020＋已下架单集 2025；剧集 B：可见单集 2023。

    按 DateLastContentAdded 倒序，正确顺序是 B（2023）在前、A（2020）
    在后；修前 A 会被已下架的 2025 顶到前面。
    """
    a = _item(db, "pa042-a", item_type="series", date_added=datetime(2019, 1, 1))
    _item(db, "pa042-a-e1", item_type="episode", series_id=a,
          date_added=datetime(2020, 1, 1))
    _item(db, "pa042-a-e2", item_type="episode", series_id=a,
          date_added=datetime(2025, 1, 1), deleted=True)
    b = _item(db, "pa042-b", item_type="series", date_added=datetime(2019, 1, 1))
    _item(db, "pa042-b-e1", item_type="episode", series_id=b,
          date_added=datetime(2023, 1, 1))
    req = _request("SortBy=DateLastContentAdded&SortOrder=Descending&Limit=100")
    out = emby_api._query_items(req, _user(db), db, "http://x")
    ids = [it["Id"] for it in out["Items"]]
    assert ids == ["pa042-b", "pa042-a"]


def test_projection_page_not_fallback_on_soft_deleted(db):
    """② 1 可见＋1 已下架：重建投影后投影行只有可见那 1 行。

    修前 _projection_page 的 Query.count() 把已下架算进去（2≠1），
    误判覆盖不全返回 None（多余回落）；修后应返回正常页。
    """
    lib = _lib(db)
    _item(db, "pa042-p-vis", date_added=datetime(2020, 1, 1))
    _item(db, "pa042-p-dead", date_added=datetime(2020, 1, 2), deleted=True)
    proj.rebuild_projection(db)
    db.commit()
    page = proj._projection_page(db, lib.id, "title", None, None, 10)
    assert page is not None
    rows, _has_more = page
    assert [row.item_id for row, _it in rows] == [
        db.query(em.MediaItem).filter(em.MediaItem.guid == "pa042-p-vis").first().id
    ]
