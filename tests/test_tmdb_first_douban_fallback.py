# -*- coding: utf-8 -*-
"""TMDB 为主、豆瓣兜底：优先级逻辑测试（v2.56.0）。

隔离临时 SQLite，豆瓣与 TMDB 全部 mock。

覆盖：
1. TMDB 命中 → 不打豆瓣（豆瓣 search 未被调用）
2. TMDB 未命中 + 中文标题 → 豆瓣 rich 兜底被调用（search/details/cast）
3. TMDB 未命中 + 非中文标题 → 不打豆瓣
4. 兜底阶段豆瓣被封禁 → 整条标记重试（ok=False, douban_banned=True）
5. 豆瓣兜底命中的演员表能写库（TMDB 没给演员时）
"""
import os
import tempfile

os.environ.setdefault("DATABASE_TYPE", "sqlite")
os.environ.setdefault("REDIS_ENABLED", "false")
_fd, _tmppath = tempfile.mkstemp(suffix=".db"); os.close(_fd)
os.environ["DATABASE_URL"] = "sqlite:///" + _tmppath
from backend import database as _dbmod
from sqlalchemy import create_engine as _ce
from sqlalchemy.orm import sessionmaker as _sm
_dbmod.engine = _ce(os.environ["DATABASE_URL"])
_dbmod.configure_session_local(_sm(bind=_dbmod.engine))

import uuid
from unittest import mock

import pytest

from backend.database import SessionLocal, init_db
from backend.emby_server import models as em
from backend.emby_server import enrich_worker
from backend.emby_server import douban as dbn

init_db()

_CELEBS = [
    {"name": "嘉羿 Jia Yi", "image": "https://img2.doubanio.com/p/jy.jpg", "role": "展望"},
    {"name": "与七 Yu Qi", "image": "", "role": "展煦"},
]


@pytest.fixture()
def db():
    s = SessionLocal()
    try:
        s.query(em.EmbyPerson).delete()
        s.query(em.MediaItem).delete()
        s.commit()
        yield s
    finally:
        s.close()


def _series(db, **kw):
    it = em.MediaItem(guid=uuid.uuid4().hex, library_id=1, item_type="series",
                      name=kw.pop("name", "沦陷"), production_year=2026,
                      enrich_status="enriching", **kw)
    db.add(it)
    db.commit()
    return it


def _fake_douban(hit=True, celebs=True):
    """返回 (fake_client, search_mock)"""
    fake = mock.MagicMock()
    if hit:
        fake.search.return_value = {"id": "3700001", "title": "沦陷", "year": "2026",
                                    "image": "", "type": "tv"}
        fake.get_details.return_value = {"title": "沦陷", "overview": "简介",
                                         "runtime_ticks": 0}
    else:
        fake.search.return_value = None
        fake.get_details.return_value = None
    fake.get_celebrities.return_value = list(_CELEBS) if celebs else []
    return fake


def _fake_tmdb(hit=True):
    """mock backend.emby_server.tmdb.tmdb_client 用的 MagicMock"""
    fake = mock.MagicMock()
    fake.configured = True
    return fake


def _run_pre(item):
    """只跑 _enrich_fetch_pre，返回 (result, tmdb_ctx)"""
    snap = enrich_worker._snapshot_item(item)
    with mock.patch.object(enrich_worker, "_alias_tmdb_id", return_value=None):
        result, ctx = enrich_worker._enrich_fetch_pre(snap)
    return result, ctx


def test_tmdb_hit_skips_douban(db):
    """1. TMDB 命中 → 豆瓣 search 完全不被调用"""
    item = _series(db, name="沦陷")
    fake_dbn = _fake_douban()
    fake_tmdb = _fake_tmdb()
    with mock.patch("backend.emby_server.tmdb.tmdb_client", fake_tmdb), \
            mock.patch.object(dbn, "client", fake_dbn), \
            mock.patch.object(enrich_worker, "_cached_douban_cfg",
                              return_value=(True, 2.5)):
        # pre 阶段：TMDB 计划应该是 search（不再被豆瓣优先跳过）
        result, ctx = _run_pre(item)
        assert ctx["tmdb_plan"]["op"] == "search", \
            "TMDB 为主：pre 阶段必须规划 TMDB 搜索"
        assert not result.get("douban_fallback_hit")
        # 模拟 TMDB 搜中：post 阶段走 tmdb_hit 分支
        result["tmdb_hit"] = {"id": 12345, "title": "沦陷"}
        result["tmdb_details"] = {"overview": "TMDB 简介"}
        with mock.patch("backend.emby_server.scanner._tmdb_work",
                        return_value=({"id": 12345}, {"overview": "TMDB 简介"})):
            out = enrich_worker._enrich_fetch_tmdb_post(result, item, ctx)
    fake_dbn.search.assert_not_called()
    assert not out.get("douban_fallback_hit")


def test_tmdb_miss_cjk_triggers_douban_fallback(db):
    """2. TMDB 未命中 + 中文标题 → 豆瓣 rich 兜底被调用"""
    item = _series(db, name="沦陷")
    fake_dbn = _fake_douban()
    fake_tmdb = _fake_tmdb()
    with mock.patch("backend.emby_server.tmdb.tmdb_client", fake_tmdb), \
            mock.patch.object(dbn, "client", fake_dbn), \
            mock.patch.object(enrich_worker, "_cached_douban_cfg",
                              return_value=(True, 2.5)), \
            mock.patch.object(enrich_worker, "_cached_altmeta_cfg",
                              return_value=(True, 2.5, 2.5)), \
            mock.patch.object(enrich_worker, "_alias_tmdb_id", return_value=None), \
            mock.patch("backend.emby_server.scanner._tmdb_work",
                        return_value=(None, None)):
        result, ctx = _run_pre(item)
        out = enrich_worker._enrich_fetch_tmdb_post(result, item, ctx)
    # 豆瓣 rich 兜底三件套都被调用
    fake_dbn.search.assert_called_once()
    fake_dbn.get_details.assert_called_once_with("3700001")
    fake_dbn.get_celebrities.assert_called_once_with("3700001")
    assert out.get("douban_fallback_hit", {}).get("id") == "3700001"
    assert out.get("douban_details", {}).get("overview") == "简介"
    assert len(out.get("douban_cast", [])) == 2


def test_tmdb_miss_non_cjk_skips_douban(db):
    """3. TMDB 未命中 + 非中文标题 → 不打豆瓣"""
    item = _series(db, name="Breaking Bad")
    fake_dbn = _fake_douban()
    fake_tmdb = _fake_tmdb()
    with mock.patch("backend.emby_server.tmdb.tmdb_client", fake_tmdb), \
            mock.patch.object(dbn, "client", fake_dbn), \
            mock.patch.object(enrich_worker, "_cached_douban_cfg",
                              return_value=(True, 2.5)), \
            mock.patch.object(enrich_worker, "_cached_altmeta_cfg",
                              return_value=(True, 2.5, 2.5)), \
            mock.patch.object(enrich_worker, "_alias_tmdb_id", return_value=None), \
            mock.patch("backend.emby_server.scanner._tmdb_work",
                        return_value=(None, None)):
        result, ctx = _run_pre(item)
        out = enrich_worker._enrich_fetch_tmdb_post(result, item, ctx)
    fake_dbn.search.assert_not_called()
    assert not out.get("douban_fallback_hit")


def test_douban_banned_on_fallback_marks_retry(db):
    """4. 兜底阶段豆瓣被封禁 → 整条标记重试（不终态化）"""
    item = _series(db, name="沦陷")
    fake_dbn = _fake_douban()
    fake_dbn.search.side_effect = dbn.DoubanBannedError("test banned")
    fake_tmdb = _fake_tmdb()
    with mock.patch("backend.emby_server.tmdb.tmdb_client", fake_tmdb), \
            mock.patch.object(dbn, "client", fake_dbn), \
            mock.patch.object(enrich_worker, "_cached_douban_cfg",
                              return_value=(True, 2.5)), \
            mock.patch.object(enrich_worker, "_alias_tmdb_id", return_value=None), \
            mock.patch("backend.emby_server.scanner._tmdb_work",
                        return_value=(None, None)):
        result, ctx = _run_pre(item)
        out = enrich_worker._enrich_fetch_tmdb_post(result, item, ctx)
    assert out.get("ok") is False
    assert out.get("douban_banned") is True


def test_douban_fallback_cast_writes_to_db(db):
    """5. 豆瓣兜底的演员表能写库（TMDB 没给演员时）"""
    item = _series(db, name="沦陷")
    fake_dbn = _fake_douban()
    fake_tmdb = _fake_tmdb()
    with mock.patch("backend.emby_server.tmdb.tmdb_client", fake_tmdb), \
            mock.patch.object(dbn, "client", fake_dbn), \
            mock.patch.object(enrich_worker, "_cached_douban_cfg",
                              return_value=(True, 2.5)), \
            mock.patch.object(enrich_worker, "_cached_altmeta_cfg",
                              return_value=(True, 2.5, 2.5)), \
            mock.patch.object(enrich_worker, "_alias_tmdb_id", return_value=None), \
            mock.patch("backend.emby_server.scanner._tmdb_work",
                        return_value=(None, None)):
        result, ctx = _run_pre(item)
        out = enrich_worker._enrich_fetch_tmdb_post(result, item, ctx)
        assert out.get("douban_fallback_hit")
        # 写库
        fresh = db.query(em.MediaItem).filter(em.MediaItem.id == item.id).first()
        enrich_worker._enrich_apply(db, fresh, out)
        db.commit()
    people = (db.query(em.EmbyPerson).filter(em.EmbyPerson.item_id == item.id)
              .order_by(em.EmbyPerson.sort_order).all())
    assert [(p.name, p.role) for p in people] == [
        ("嘉羿", "展望"),
        ("与七", "展煦"),
    ]
    fresh = db.query(em.MediaItem).filter(em.MediaItem.id == item.id).first()
    assert fresh.metadata_source == "douban"
    assert fresh.overview == "简介"


def test_display_name():
    assert dbn.display_name("嘉羿 Jia Yi") == "嘉羿"
    assert dbn.display_name("Tom Hanks") == "Tom Hanks"
    assert dbn.display_name("") == ""
