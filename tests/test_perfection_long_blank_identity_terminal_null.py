"""后端完美审查第二轮第 2 项：终态时纯空白身份列清成 NULL。

口径（parent 已定，不再回问）：
- 只清纯空白（strip 后为空，含 "" / "  " / 制表符）的 tmdb_id / imdb_id，
  真实身份一律不动；person_tmdb_id / proposed_* / resolved_* 不在本项范围。
- 清 NULL 后该行仍为 done 终态，不再被抢单、不再空转。
- 只覆盖 done 终态点：现行 success done、锁定 done、资料不全超限
  done+none、两段 fill done；pending 重试 / failed 一律不动（不在本文件断言，
  以免把非终态语义扩进本项）。
- 与 #1374 分工：#1374 修抢单/bound 口径（空白不再空转），本项修终态
  残留（空白不再污染后续匹配）；本文件不碰 identify_two_stage.extract_binding。
"""
import os
import tempfile
from types import SimpleNamespace
from unittest import mock

os.environ.setdefault("DATABASE_TYPE", "sqlite")
os.environ.setdefault("REDIS_ENABLED", "false")
_fd, _tmppath = tempfile.mkstemp(suffix=".db")
os.close(_fd)
os.environ["DATABASE_URL"] = "sqlite:///" + _tmppath
from backend import database as _dbmod
from sqlalchemy import create_engine as _ce
from sqlalchemy.orm import sessionmaker as _sm

_dbmod.engine = _ce(os.environ["DATABASE_URL"])
_dbmod.configure_session_local(_sm(bind=_dbmod.engine))

import uuid

import pytest

from backend.database import SessionLocal, init_db
from backend.emby_server import enrich_worker
from backend.emby_server import identify_two_stage as ts
from backend.emby_server import models as em

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


def _make_item(db, **kw):
    it = em.MediaItem(
        guid=uuid.uuid4().hex,
        library_id=kw.pop("library_id", 1),
        item_type=kw.pop("item_type", "movie"),
        name=kw.pop("name", "测试"),
        file_path=kw.pop("file_path", "/tmp/x.mp4"),
        enrich_status=kw.pop("enrich_status", "pending"),
        **kw,
    )
    db.add(it)
    db.commit()
    return it


def _pin_classic(db):
    enrich_worker.save_enrich_tuning_config(db, {
        enrich_worker.CFG_ENRICH_FIELD_COMPLETENESS: False,
        enrich_worker.CFG_ENRICH_NONE_AUTO_RESCHEDULE: False,
    })


@pytest.fixture(autouse=True)
def _drop_tuning():
    yield
    from backend import models as _models
    from backend.integrations import store as _store
    s = SessionLocal()
    try:
        s.query(_models.SystemConfig).filter(
            _models.SystemConfig.key.in_([
                enrich_worker.CFG_ENRICH_FIELD_COMPLETENESS,
                enrich_worker.CFG_ENRICH_NONE_AUTO_RESCHEDULE,
            ])).delete(synchronize_session=False)
        s.commit()
    except Exception:
        s.rollback()
    finally:
        s.close()
    enrich_worker._enrich_tuning_cache["at"] = 0.0
    enrich_worker._enrich_tuning_cache["cfg"] = None


def test_helper_clears_only_blank():
    blank = SimpleNamespace(tmdb_id="  ", imdb_id="\t")
    enrich_worker._clear_blank_identity(blank)
    assert blank.tmdb_id is None
    assert blank.imdb_id is None
    empty = SimpleNamespace(tmdb_id="", imdb_id=None)
    enrich_worker._clear_blank_identity(empty)
    assert empty.tmdb_id is None
    assert empty.imdb_id is None
    real = SimpleNamespace(tmdb_id="60300", imdb_id="tt0903747")
    enrich_worker._clear_blank_identity(real)
    assert real.tmdb_id == "60300"
    assert real.imdb_id == "tt0903747"
    missing = SimpleNamespace()
    enrich_worker._clear_blank_identity(missing)  # 缺字段不抛


def test_success_done_clears_blank_and_stays_done_not_reclaimed(db):
    _pin_classic(db)
    it = _make_item(db, tmdb_id="  ", imdb_id="\t", overview="已有简介",
                    production_year=2021)
    fetched = {"poster": None, "fanart": None, "external_subs": [],
               "nfo_data": None, "tmdb_hit": None, "tmdb_details": None,
               "ok": True}
    with mock.patch("backend.emby_server.tmdb.tmdb_client"):
        enrich_worker._enrich_apply(db, it, fetched)
    db.commit()
    db.refresh(it)
    assert it.enrich_status == "done"
    assert it.tmdb_id is None
    assert it.imdb_id is None
    # done 不复活：两队列抢单都不捞它
    assert it.id not in {r.id for r in enrich_worker._claim_batch(db, 10, queue="identify")}
    assert it.id not in {r.id for r in enrich_worker._claim_batch(db, 10, queue="fill")}


def test_success_done_keeps_real_identity(db):
    _pin_classic(db)
    it = _make_item(db, tmdb_id="60300", imdb_id="tt0903747",
                    overview="已有简介", production_year=2021)
    fetched = {"poster": None, "fanart": None, "external_subs": [],
               "nfo_data": None, "tmdb_hit": None, "tmdb_details": None,
               "ok": True}
    with mock.patch("backend.emby_server.tmdb.tmdb_client"):
        enrich_worker._enrich_apply(db, it, fetched)
    db.commit()
    db.refresh(it)
    assert it.enrich_status == "done"
    assert it.tmdb_id == "60300"
    assert it.imdb_id == "tt0903747"


def test_locked_done_clears_blank(db):
    it = _make_item(db, tmdb_id="  ", imdb_id=" ", metadata_locked=True)
    enrich_worker._enrich_apply(db, it, {})
    db.commit()
    db.refresh(it)
    assert it.enrich_status == "done"
    assert it.tmdb_id is None
    assert it.imdb_id is None


def test_none_overlimit_done_clears_blank_imdb(db, monkeypatch):
    # 资料不全 5 次 + none 回捞 flag 开 -> done+none；imdb 空白清 NULL。
    enrich_worker.save_enrich_tuning_config(db, {
        enrich_worker.CFG_ENRICH_FIELD_COMPLETENESS: False,
        enrich_worker.CFG_ENRICH_NONE_AUTO_RESCHEDULE: True,
    })
    it = _make_item(db, tmdb_id=None, imdb_id="  ", production_year=None,
                    enrich_attempts=4)
    fetched = {"ok": True, "poster": None, "fanart": None, "external_subs": [],
               "nfo_data": None, "tmdb_hit": {"id": 999, "title": "x"},
               "tmdb_details": None, "error": None}
    with mock.patch("backend.emby_server.tmdb.tmdb_client") as fake_client:
        fake_client.configured = True
        enrich_worker._enrich_apply(db, it, fetched)
    db.commit()
    db.refresh(it)
    assert it.enrich_status == "done"
    assert it.metadata_source == "none"
    assert it.tmdb_id is None
    assert it.imdb_id is None


def test_two_stage_fill_done_clears_blank_imdb_keeps_real_tmdb(db, monkeypatch):
    it = _make_item(db, item_type="series", tmdb_id="60300", imdb_id="  ",
                    production_year=2021)
    monkeypatch.setattr(ts, "apply_fill", lambda _db, _item, _fetched: None)
    enrich_worker._enrich_apply_two_stage(db, it, {}, identify=False, fill=True)
    db.commit()
    db.refresh(it)
    assert it.enrich_status == "done"
    assert it.tmdb_id == "60300"
    assert it.imdb_id is None
