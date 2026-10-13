# -*- coding: utf-8 -*-
"""豆瓣兜底命中的条目也要有演员表（TMDB 没给演员时用豆瓣的）。

v2.56.0 起 TMDB 为主、豆瓣兜底：豆瓣 cast 在 TMDB 未命中后的兜底阶段获取，
写库阶段在 TMDB 没给演员表时落库（与旧"豆瓣优先"同口径）。

隔离临时 SQLite，豆瓣与 TMDB 全部 mock。
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


def _fake_douban():
    fake = mock.MagicMock()
    fake.search.return_value = {"id": "3700001", "title": "沦陷", "year": "2026",
                                "image": "", "type": "tv"}
    fake.get_details.return_value = {"title": "沦陷", "overview": "简介",
                                     "runtime_ticks": 0}
    fake.get_celebrities.return_value = list(_CELEBS)
    return fake


def _run(db, item, fake_dbn):
    """完整流程：pre（TMDB 规划）→ post（TMDB 未命中 → 豆瓣兜底）→ apply（写库）"""
    snap = enrich_worker._snapshot_item(item)
    fake_tmdb = mock.MagicMock()
    fake_tmdb.configured = True
    with mock.patch("backend.emby_server.tmdb.tmdb_client", fake_tmdb), \
            mock.patch.object(dbn, "client", fake_dbn), \
            mock.patch.object(enrich_worker, "_cached_douban_cfg",
                              return_value=(True, 2.5)), \
            mock.patch.object(enrich_worker, "_cached_altmeta_cfg",
                              return_value=(True, 2.5, 2.5)), \
            mock.patch.object(enrich_worker, "_alias_tmdb_id", return_value=None), \
            mock.patch("backend.emby_server.scanner._tmdb_work",
                        return_value=(None, None)):
        result, ctx = enrich_worker._enrich_fetch_pre(snap)
        # TMDB 为主：pre 阶段规划 TMDB 搜索，不再被豆瓣跳过
        assert ctx["tmdb_plan"]["op"] == "search"
        out = enrich_worker._enrich_fetch_tmdb_post(result, item, ctx)
    assert out.get("douban_fallback_hit"), "TMDB 未命中时豆瓣兜底必须命中"
    fresh = db.query(em.MediaItem).filter(em.MediaItem.id == item.id).first()
    enrich_worker._enrich_apply(db, fresh, out)
    db.commit()
    return fresh


def test_douban_fallback_hit_writes_cast(db):
    item = _series(db)
    fresh = _run(db, item, _fake_douban())
    people = (db.query(em.EmbyPerson).filter(em.EmbyPerson.item_id == item.id)
              .order_by(em.EmbyPerson.sort_order).all())
    assert [(p.name, p.role, p.image) for p in people] == [
        ("嘉羿", "展望", "https://img2.doubanio.com/p/jy.jpg"),
        ("与七", "展煦", ""),
    ]
    assert fresh.metadata_source == "douban"
    assert fresh.overview == "简介"


def test_douban_cast_does_not_duplicate_existing_people(db):
    item = _series(db)
    db.add(em.EmbyPerson(item_id=item.id, name="唐诚之", role="许愿", image="",
                         sort_order=0, person_tmdb_id=""))
    db.commit()
    _run(db, item, _fake_douban())
    names = [p.name for p in db.query(em.EmbyPerson).filter(
        em.EmbyPerson.item_id == item.id).all()]
    assert names == ["唐诚之"]


def test_display_name():
    assert dbn.display_name("嘉羿 Jia Yi") == "嘉羿"
    assert dbn.display_name("Tom Hanks") == "Tom Hanks"
    assert dbn.display_name("") == ""
