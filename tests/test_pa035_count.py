"""PA-035：Query.count() 绕过软删全局钩子，计数把已下架条目算进去。

6 处计数点（R15-B15 清单）逐一钉：1 可见＋1 已下架的夹具下，
计数必须等于列表长度（只含可见条目），且合集上架门不被下架成员顶过去。

另补一条静态门禁：6 个已修函数的源码里不许再出现 `.count()`，
防止有人把 Query.count() 写回来（其余文件的非条目 .count() 不受影响）。

隔离 SQLite 内存库，不连生产库；软删钩子装在 Session 类上，
import soft_delete 即生效（默认开）。
"""

from __future__ import annotations

import inspect
import os
from datetime import datetime, timedelta

os.environ.setdefault("DATABASE_TYPE", "sqlite")
os.environ.setdefault("REDIS_ENABLED", "false")

import pytest
from sqlalchemy import create_engine, func
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from starlette.requests import Request

from backend import models as base_models
from backend.emby_server import models as em
from backend.emby_server import soft_delete  # noqa: F401 — 装全局软删钩子
from backend.emby_server import media_sets as ms


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


def _user(db, name="pa035user"):
    u = base_models.WebUser(username=name, password_hash="x", is_active=True)
    db.add(u)
    db.commit()
    db.refresh(u)
    return u


def _item(db, guid, deleted=False, **kw):
    kw.setdefault("name", guid)
    kw.setdefault("item_type", "movie")
    kw.setdefault("tmdb_id", "1")
    if "library_id" not in kw:
        lib = db.query(em.Library).filter(em.Library.guid == "pa035lib").first()
        if lib is None:
            lib = em.Library(guid="pa035lib", name="pa035lib")
            db.add(lib)
            db.commit()
            db.refresh(lib)
        kw["library_id"] = lib.id
    it = em.MediaItem(guid=guid, **kw)
    db.add(it)
    db.commit()
    db.refresh(it)
    item_id = it.id
    if deleted:
        # 注意：软删提交后该实例在 ORM 里即不可见（钩子会把它滤掉），
        # 之后再碰它会报 ObjectDeletedError，故只返回主键 id。
        it.deleted_at = datetime.now()
        db.commit()
    return item_id


def _request():
    scope = {
        "type": "http",
        "method": "GET",
        "path": "/",
        "headers": [],
        "query_string": b"",
        "server": ("testserver", 80),
        "scheme": "http",
    }
    return Request(scope)


def test_effective_count_excludes_soft_deleted(db):
    """合集有效数：1 可见＋1 下架 → 1；min 门 2 时不上架。"""
    vis = _item(db, "pa035-vis")
    dead = _item(db, "pa035-dead", deleted=True)
    s = em.EmbyMediaSet(name="pa035set", visibility_status="visible",
                        min_effective_members=2)
    db.add(s)
    db.commit()
    db.refresh(s)
    for i, it in enumerate((vis, dead)):
        db.add(em.EmbyMediaSetMember(set_id=s.id, item_id=it, order_index=i))
    db.commit()
    assert ms.effective_count(db, s) == 1
    assert ms.shelf_eligible(db, s) is False
    assert len(ms.visible_items_query(db, s.id).all()) == 1


def test_favorite_total_matches_list(db):
    """收藏 TotalRecordCount 与实返一致（1 可见＋1 下架 → 1）。"""
    from backend.emby_server import api as emby_api

    u = _user(db)
    vis = _item(db, "pa035-fav-vis")
    dead = _item(db, "pa035-fav-dead", deleted=True)
    db.add(em.UserMediaData(user_id=u.id, item_id=vis, is_favorite=True))
    db.add(em.UserMediaData(user_id=u.id, item_id=dead, is_favorite=True))
    db.commit()
    out = emby_api.get_favorites(_request(), u, db)
    assert out["TotalRecordCount"] == 1
    assert len(out["Items"]) == 1


def test_played_total_matches_list(db):
    """已看 TotalRecordCount 与实返一致（1 可见＋1 下架 → 1）。"""
    from backend.emby_server import api as emby_api

    u = _user(db)
    vis = _item(db, "pa035-played-vis")
    dead = _item(db, "pa035-played-dead", deleted=True)
    db.add(em.UserMediaData(user_id=u.id, item_id=vis, played=True))
    db.add(em.UserMediaData(user_id=u.id, item_id=dead, played=True))
    db.commit()
    out = emby_api.get_played_items(_request(), u, db)
    assert out["TotalRecordCount"] == 1
    assert len(out["Items"]) == 1


def test_admin_search_total_matches_list(db):
    """管理端条目搜索 total 与实返一致（1 可见＋1 下架 → 1）。"""
    from backend.emby_server import portal as portal_mod

    staff = _user(db, "pa035staff")
    staff.is_staff = True
    _item(db, "pa035-search-vis", name="pa035search")
    _item(db, "pa035-search-dead", name="pa035search", deleted=True)
    out = portal_mod.admin_search_items(staff, db, search="pa035search")
    assert out["total"] == 1
    assert len(out["items"]) == 1


def test_sample_hash_progress_excludes_soft_deleted(db):
    """采样诊断 pending/sampled 不含已下架行。"""
    from backend.api import admin_readonly as ro
    from backend.emby_server import sample_hash_worker as shw

    _item(db, "pa035-pend-vis", file_path="/x/a.mkv", content_hash=None)
    _item(db, "pa035-pend-dead", file_path="/x/b.mkv", content_hash=None,
          deleted=True)
    _item(db, "pa035-samp-vis", file_path="/x/c.mkv",
          content_hash="abc", content_hash_algo=shw.SAMPLE_ALGO)
    _item(db, "pa035-samp-dead", file_path="/x/d.mkv",
          content_hash="def", content_hash_algo=shw.SAMPLE_ALGO, deleted=True)
    out = ro.sample_hash_progress(None, db)
    assert out["pending"] == 1
    assert out["sampled"] == 1


def test_new_media_summary_total_excludes_soft_deleted(db):
    """本轮新增汇总 total 不含本轮内又被下架的行。"""
    from backend.emby_server import new_media_notify as nmn

    now = datetime.now()
    _item(db, "pa035-new-vis", date_added=now)
    _item(db, "pa035-new-dead", date_added=now, deleted=True)
    out = nmn._summarize_new_items(db, 1, now - timedelta(days=1))
    assert out is not None
    assert out["total"] == 1
    assert len(out["items"]) == 1


def test_count_virtual_items_excludes_soft_deleted(db):
    """虚拟库计数不含已下架行（潜伏死路径，一并钉住）。"""
    from backend.emby_server import scanner as scanner_mod

    class _Lib:
        platform = "pa035plat"

    _item(db, "pa035-virt-vis", platforms="pa035plat")
    _item(db, "pa035-virt-dead", platforms="pa035plat", deleted=True)
    assert scanner_mod.count_virtual_items(db, _Lib()) == 1


def test_no_query_count_in_fixed_functions():
    """静态门禁：6 个已修函数源码里不许再出现 Query.count() 调用。

    用 AST 判定（只认真正的 `.count()` 调用，docstring/注释里提到
    Query.count() 不算），其余文件的非条目 .count() 不受影响。
    """
    import ast

    from backend.api import admin_readonly as ro
    from backend.emby_server import api as emby_api
    from backend.emby_server import new_media_notify as nmn
    from backend.emby_server import portal as portal_mod
    from backend.emby_server import scanner as scanner_mod

    fns = [
        ms.effective_count,
        emby_api.get_favorites,
        emby_api.get_played_items,
        portal_mod.admin_search_items,
        ro.sample_hash_progress,
        nmn._summarize_new_items,
        scanner_mod.count_virtual_items,
    ]
    for fn in fns:
        tree = ast.parse(inspect.getsource(fn))
        bad = [n for n in ast.walk(tree)
               if isinstance(n, ast.Call)
               and isinstance(n.func, ast.Attribute)
               and n.func.attr == "count"
               # func.count(...) 是聚合函数本身，不是 Query.count()
               and not (isinstance(n.func.value, ast.Name)
                        and n.func.value.id == "func")]
        assert not bad, fn.__qualname__
