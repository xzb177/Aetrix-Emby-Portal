"""补全「算不算完成」的判定：核心字段齐了就完成，不因简介缺失永久重试

生产实测（2026-10-05）：外语电影库 571 条待补全，全部有标题 + 年份、
104 条有 tmdb_id，却因为 ``overview`` 为空被判 ``_incomplete`` 退回 pending，
worker 反复重刮，队列永不下降。

本测试钉住判定边界，防止把**真正空白**的条目误判成已完成。
"""
import os
import tempfile
os.environ.setdefault("DATABASE_TYPE", "sqlite")
os.environ.setdefault("REDIS_ENABLED", "false")
_fd, _p = tempfile.mkstemp(suffix=".db"); os.close(_fd)
os.environ["DATABASE_URL"] = f"sqlite:///{_p}"
from backend import database as _dbmod
from sqlalchemy import create_engine as _ce
from sqlalchemy.orm import sessionmaker as _sm
_dbmod.engine = _ce(os.environ["DATABASE_URL"])
_dbmod.configure_session_local(_sm(bind=_dbmod.engine))

import uuid
from datetime import datetime
from unittest import mock

import pytest

from backend.database import SessionLocal, init_db
from backend.emby_server import models as em
from backend.emby_server import enrich_worker

init_db()


@pytest.fixture()
def db():
    s = SessionLocal()
    try:
        s.query(em.MediaStream).delete()
        s.query(em.MediaItem).delete()
        s.commit()
        yield s
    finally:
        s.close()


def _item(db, **kw):
    it = em.MediaItem(
        guid=uuid.uuid4().hex,
        library_id=kw.pop("library_id", 1),
        item_type=kw.pop("item_type", "movie"),
        name=kw.pop("name", "片名"),
        file_path=kw.pop("file_path", ""),
        enrich_status="enriching",
        **kw,
    )
    db.add(it)
    db.commit()
    return it


def _apply(db, it, fetched):
    with mock.patch("backend.emby_server.tmdb.tmdb_client") as tc:
        tc.configured = True
        enrich_worker._enrich_apply(db, it, fetched)
    db.commit()
    db.refresh(it)
    return it


BASE_OK = {"ok": True, "poster": None, "fanart": None, "external_subs": [],
           "nfo_data": None, "tmdb_hit": None, "tmdb_details": None,
           "error": None}


def test_completes_when_title_and_year_present_but_no_overview(db, monkeypatch):
    """核心复现：生产那 571 条——标题+年份齐、简介空，应判 done 而不是永久 pending"""
    # 2026-10-21 A 类开关摘面板：字段级完成口径缺省改为开（=生产现值），
    # 本用例验证的是「字段级口径关」时的核心完成线，按 env 显式关闭
    monkeypatch.setenv("ENRICH_FIELD_COMPLETENESS", "0")
    it = _item(db, name="007：女王密使", production_year=1969,
               tmdb_id="1642")
    _apply(db, it, dict(BASE_OK))
    assert it.enrich_status == "done", (
        "标题+年份+tmdb_id 齐了却因简介为空退回 pending，会让队列永不下降")


def test_completes_with_title_year_and_poster(db):
    """三者齐备：即使没有 tmdb_id 也算完成"""
    it = _item(db, name="逃避行", production_year=2023,
               poster_path="/img/p.jpg")
    _apply(db, it, dict(BASE_OK))
    assert it.enrich_status == "done"


def test_stays_pending_when_only_title_and_search_missed(db):
    """核心字段不足（只有标题）且**兜底源有命中** → 仍应可重试

    注意用 ``douban_hit`` 让 ``alt_hit`` 非空，从而绕开处方 5 的终态分支：
    否则会走进「搜过确实没命中 → 终态 done+none」那条既有路径，测的就不是
    本次改动的判定了。分开测是因为那两条语义不同：
    「搜过没有」是终态，「没搜过/搜不出来」才需要重试。
    """
    it = _item(db, name="只有标题", production_year=None)
    fetched = dict(BASE_OK)
    fetched["douban_hit"] = {"id": "x", "title": "只有标题"}   # alt_hit 非空
    with mock.patch("backend.emby_server.altmeta.apply"), \
            mock.patch("backend.emby_server.tmdb.tmdb_client") as tc:
        tc.configured = True
        enrich_worker._enrich_apply(db, it, fetched)
    db.commit()
    db.refresh(it)
    assert it.enrich_status == "pending", (
        "核心字段不足时不能被判完成，否则真正没刮干净的条目会被静默放过")


def test_searched_without_hit_is_still_terminal_none(db):
    """处方 5 终态化不被破坏：搜过没命中仍是 done + none"""
    it = _item(db, name="注定搜不到的剧", production_year=None)
    with mock.patch.object(enrich_worker, "_alias_tmdb_id", return_value=None), \
            mock.patch("backend.emby_server.tmdb.tmdb_client") as tc:
        tc.configured = True
        enrich_worker._enrich_apply(db, it, dict(BASE_OK))
    db.commit()
    db.refresh(it)
    assert it.enrich_status == "done"
    assert it.metadata_source == "none"
