"""刮削「标 done 但不干净」系统性修复（2026-10-31 用户截图亲抓）。

生产实证三类坏行：
1. 绑错条目：series《网球王子》tmdb_id 绑到年份相同、内容无关的条目，
   enrich_status=done、无海报——绑定时没有任何客观复核。
2. kind/id 错位：《原子少年》绑的 id 按 tv 查不到（回落 movie 也 404），
   details 根本不存在却照样落绑标 done。
3. 搜不中永久终态：tmdb_id=NULL + done + 无海报，enrich_none_reschedules
   达上限（6）后 enrich_next_retry_at=NULL、无人再碰，TMDB 以后上架
   也永不重查。

本文件覆盖：
- 纯复核函数 tmdb.binding_reject_reason（年份/kind/details 三查）；
- 取数后绑定复核门（_enrich_fetch_tmdb_post）：错年/无详情的命中被丢弃，
  走搜不中路径，不许落绑标 done；
- 两段式识别段同门（identify_two_stage.apply_identity）；
- none 长尾：达轮数上限的行过长尾周期（默认 30 天）后仍被回捞，
  不再有 next_retry=NULL 且无人再碰的永久终态；
- 干净度巡检 dirty_done_patrol：错绑清 id 重排、无海报重排、none 终态
  行捞回、好行不动、新鲜行（min-age 内）不动。
"""
import os
import tempfile

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
from datetime import datetime, timedelta
from unittest import mock

import pytest

from backend.database import SessionLocal, init_db
from backend.emby_server import models as em
from backend.emby_server import enrich_worker as ew
from backend.emby_server import identify_two_stage as ts
from backend.emby_server import tmdb as tmdb_mod
from backend.integrations import store

init_db()


def _clear_tuning_cache():
    ew._enrich_tuning_cache["at"] = 0.0
    ew._enrich_tuning_cache["cfg"] = None


def _set_cfg(key, value):
    _clear_tuning_cache()
    db = SessionLocal()
    try:
        store.write_values(db, {key: value}, {key: "dirty-gate 测试"})
        db.commit()
    finally:
        db.close()
    store.invalidate(key)
    _clear_tuning_cache()


@pytest.fixture(autouse=True)
def _flags():
    _set_cfg(ew.CFG_ENRICH_NONE_AUTO_RESCHEDULE, "1")
    _set_cfg("enrich_dirty_done_patrol", "1")
    yield


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
        item_type=kw.pop("item_type", "series"),
        name=kw.pop("name", "测试"),
        file_path=kw.pop("file_path", "/tmp/x.mp4"),
        enrich_status=kw.pop("enrich_status", "pending"),
        **kw,
    )
    db.add(it)
    db.commit()
    return it


# ---------------------------------------------------------------------------
# 1. 纯复核函数
# ---------------------------------------------------------------------------

def test_binding_reject_reason_year_mismatch():
    details = {"id": 111, "name": "Wrong Old Show",
               "first_air_date": "1990-05-01"}
    assert tmdb_mod.binding_reject_reason(details, "series", 2024) == \
        "year_mismatch"


def test_binding_reject_reason_details_missing():
    assert tmdb_mod.binding_reject_reason(None, "series", 2024) == \
        "details_not_found"
    assert tmdb_mod.binding_reject_reason({}, "movie", 2024) == \
        "details_not_found"
    assert tmdb_mod.binding_reject_reason(
        {"success": False, "status_code": 34}, "movie", 2024) == \
        "details_not_found"


def test_binding_reject_reason_kind_mismatch():
    details = {"id": 222, "title": "Some Movie",
               "release_date": "2024-01-01"}
    assert tmdb_mod.binding_reject_reason(details, "series", 2024) == \
        "kind_mismatch"


def test_binding_reject_reason_ok_and_unknown_year():
    details = {"id": 333, "name": "Right Show",
               "first_air_date": "2024-03-01"}
    assert tmdb_mod.binding_reject_reason(details, "series", 2024) is None
    assert tmdb_mod.binding_reject_reason(details, "series", 2025) is None
    assert tmdb_mod.binding_reject_reason(details, "series", None) is None
    nodate = {"id": 444, "name": "No Date Show"}
    assert tmdb_mod.binding_reject_reason(nodate, "series", 2024) is None


# ---------------------------------------------------------------------------
# 2. 取数后绑定复核门（批量路径与单条路径共用 _enrich_fetch_tmdb_post）
# ---------------------------------------------------------------------------

def _post_ctx():
    return {"kind": "series", "needs_repair": False,
            "tmdb_plan": {"op": "search", "name": "Test Show Alpha",
                          "year": 2024, "kind": "series",
                          "existing_id": None}}


def _run_post(db, item, batch_out, fake_details):
    fake_client = mock.MagicMock()
    fake_client.configured = True
    fake_client.details.side_effect = \
        lambda tid, kind: fake_details
    fake_client.images_with_language.return_value = None
    result = {"nfo_data": None}
    with mock.patch("backend.emby_server.tmdb.tmdb_client", fake_client), \
            mock.patch("backend.emby_server.tmdb.poster_language",
                       lambda: "system"), \
            mock.patch.object(ew, "_douban_ban_downgrade_active",
                              lambda: False), \
            mock.patch.object(ew, "_douban_fallback_on_tmdb_miss",
                              lambda *a: False), \
            mock.patch.object(ew, "_unified_fallback_flag_on",
                              lambda: False), \
            mock.patch.object(ew, "_should_multisource_on_tmdb_miss",
                              lambda *a: False):
        ew._enrich_fetch_tmdb_post(result, item, _post_ctx(),
                                   batch_out=batch_out)
    return result


def test_fetch_gate_rejects_year_mismatch_hit(db):
    """样本《网球王子》形态：命中年份与条目差 >1 年 → 丢弃命中不落绑。"""
    it = _make_item(db, name="Test Show Alpha", production_year=2024)
    details = {"id": 111, "name": "Wrong Old Show",
               "first_air_date": "1990-05-01"}
    out = {"hit": {"id": 111, "name": "Wrong Old Show",
                   "first_air_date": "1990-05-01"},
           "details": details, "transient": False}
    result = _run_post(db, it, out, details)
    assert result.get("tmdb_gate_rejected") == "year_mismatch"
    assert not result.get("tmdb_hit")
    assert not result.get("tmdb_id")


def test_fetch_gate_rejects_hit_without_details(db):
    """样本《原子少年》形态：hit 的 id 按 kind 查不到 details → 不落绑。"""
    it = _make_item(db, name="Test Show Alpha", production_year=2024)
    out = {"hit": {"id": 222, "name": "Ghost Show",
                   "first_air_date": "2024-01-01"},
           "details": None, "transient": False}
    result = _run_post(db, it, out, {})
    assert result.get("tmdb_gate_rejected") == "details_not_found"
    assert not result.get("tmdb_hit")
    assert not result.get("tmdb_id")


def test_fetch_gate_keeps_good_hit(db):
    it = _make_item(db, name="Test Show Alpha", production_year=2024)
    details = {"id": 333, "name": "Test Show Alpha",
               "first_air_date": "2024-03-01"}
    out = {"hit": {"id": 333, "name": "Test Show Alpha",
                   "first_air_date": "2024-03-01"},
           "details": details, "transient": False}
    result = _run_post(db, it, out, details)
    assert not result.get("tmdb_gate_rejected")
    assert result.get("tmdb_hit", {}).get("id") == 333


# ---------------------------------------------------------------------------
# 3. 两段式识别段同门
# ---------------------------------------------------------------------------

def test_two_stage_gate_rejects_year_mismatch(db):
    it = _make_item(db, name="Test Show Alpha", production_year=2024)
    fetched = {"tmdb_id": "111",
               "tmdb_details": {"id": 111, "first_air_date": "1990-05-01"},
               "nfo_data": None}
    fetcher = lambda tid, kind: {"id": 111,
                                 "first_air_date": "1990-05-01"}
    res = ts.apply_identity(db, it, fetched, tmdb_details_fetcher=fetcher)
    assert res is None
    db.refresh(it)
    assert not it.tmdb_id


# ---------------------------------------------------------------------------
# 4. none 长尾：达上限不再永久终态
# ---------------------------------------------------------------------------

def _none_terminal_row(db, name, **kw):
    kw.setdefault("enrich_status", "done")
    kw.setdefault("metadata_source", "none")
    kw.setdefault("file_fingerprint", "fp-" + name)
    kw.setdefault("enrich_none_reschedules", 6)
    return _make_item(db, name=name, **kw)


def test_none_longtail_requeues_after_longtail_period(db):
    """reschedules 已达上限 6、长尾周期（30 天）已过 → 仍被回捞。"""
    it = _none_terminal_row(
        db, "长尾老行",
        enrich_next_retry_at=datetime.now() - timedelta(days=31))
    n = ew.enrich_none_auto_reschedule(db)
    assert n >= 1
    db.refresh(it)
    assert it.enrich_status == "pending"


def test_none_longtail_not_yet_due_stays(db):
    """达上限且长尾周期未到 → 不动（防高频空转）。"""
    it = _none_terminal_row(
        db, "长尾未到行",
        enrich_next_retry_at=datetime.now() - timedelta(days=2))
    ew.enrich_none_auto_reschedule(db)
    db.refresh(it)
    assert it.enrich_status == "done"


def test_none_terminal_null_stamp_legacy_row_requeued(db):
    """存量形态：reschedules=6 且 next_retry=NULL → 视为早已终态，本轮捞回。"""
    it = _none_terminal_row(db, "存量终态行", enrich_next_retry_at=None)
    n = ew.enrich_none_auto_reschedule(db)
    assert n >= 1
    db.refresh(it)
    assert it.enrich_status == "pending"


# ---------------------------------------------------------------------------
# 5. 干净度巡检
# ---------------------------------------------------------------------------

_FAKE_DETAILS = {
    "111": {"id": 111, "name": "Wrong Old Show",
            "first_air_date": "1990-05-01"},
    "333": {"id": 333, "name": "Right Show",
            "first_air_date": "2024-03-01"},
}


def _fake_fetcher(tid, kind):
    return _FAKE_DETAILS.get(str(tid))


def _done_row(db, name, **kw):
    kw.setdefault("enrich_status", "done")
    kw.setdefault("file_fingerprint", "fp-" + name)
    kw.setdefault("last_scraped_at", datetime.now() - timedelta(days=30))
    return _make_item(db, name=name, **kw)


def test_patrol_requeues_wrong_bind_and_clears_tmdb(db):
    it = _done_row(db, "错绑行", tmdb_id="111", production_year=2024,
                   poster_path="p.jpg", metadata_source="tmdb")
    stats = ew.dirty_done_patrol(db, details_fetcher=_fake_fetcher)
    db.refresh(it)
    assert it.enrich_status == "pending"
    assert not it.tmdb_id
    assert stats["by_reason"].get("bind_year_mismatch") == 1


def test_patrol_requeues_no_poster_keeps_good_bind(db):
    it = _done_row(db, "无海报行", tmdb_id="333", production_year=2024,
                   metadata_source="tmdb")
    stats = ew.dirty_done_patrol(db, details_fetcher=_fake_fetcher)
    db.refresh(it)
    assert it.enrich_status == "pending"
    assert it.tmdb_id == "333"
    assert stats["by_reason"].get("no_poster") == 1


def test_patrol_requeues_none_terminal_row(db):
    it = _done_row(db, "终态无绑行", tmdb_id=None, metadata_source="none",
                   enrich_none_reschedules=6)
    stats = ew.dirty_done_patrol(db, details_fetcher=_fake_fetcher)
    db.refresh(it)
    assert it.enrich_status == "pending"
    assert stats["by_reason"].get("no_tmdb_no_poster") == 1


def test_patrol_leaves_good_and_fresh_rows(db):
    good = _done_row(db, "好行", tmdb_id="333", production_year=2024,
                     poster_path="p.jpg", metadata_source="tmdb")
    fresh = _done_row(db, "新鲜错绑行", tmdb_id="111",
                      production_year=2024, poster_path="p.jpg",
                      metadata_source="tmdb",
                      last_scraped_at=datetime.now())
    stats = ew.dirty_done_patrol(db, details_fetcher=_fake_fetcher)
    db.refresh(good)
    db.refresh(fresh)
    assert good.enrich_status == "done"
    assert fresh.enrich_status == "done"
    assert stats["requeued"] == 0
