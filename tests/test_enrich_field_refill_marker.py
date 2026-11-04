"""P5 恢复补齐：字段补齐标记改独立列 + 存量回捞巡检。

背景（2026-11-04）：Step 5 用 enrich_priority=-10 当"已跑过一轮"标记，
与 CMS 入库优先级（同为 -10）撞值，CMS 条目首轮缺次要字段时被误判
已补过、直接终态，从未得到设计中的那一轮补齐。

钉死：
① CMS 入库优先级（-10）首轮缺次要字段 → 进补齐队列（pending + -10 +
   refill_done=True），不再被误判已补过直接终态；
② 补齐轮（refill_done=True 入场）仍缺次要字段 → 直接终态化，每条最多补一轮；
③ 补齐轮拿到字段 → 正常 done；
④ 回捞巡检：CMS 库 done + 核心齐 + 缺次要 + 未补过 → 翻回 pending/-10/
   refill_done=True；单轮上限生效；再跑一轮翻回 0（幂等）；
⑤ 回捞巡检不碰：非 CMS 库、已补过、字段全齐、metadata_source='none'、
   已锁定、已合并、非 done 的行。
"""
import uuid
from unittest import mock

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend import models as base_models
from backend.emby_server import enrich_worker as ew
from backend.emby_server import models as em


@pytest.fixture()
def db_factory():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False},
                           poolclass=StaticPool)
    base_models.Base.metadata.create_all(engine)
    em.Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)


@pytest.fixture()
def db(db_factory):
    session = db_factory()
    yield session
    session.close()


@pytest.fixture(autouse=True)
def _clear_tuning_cache():
    ew._enrich_tuning_cache["at"] = 0.0
    ew._enrich_tuning_cache["cfg"] = None
    yield
    ew._enrich_tuning_cache["at"] = 0.0
    ew._enrich_tuning_cache["cfg"] = None


def _lib(db, name, upstream=""):
    row = em.Library(guid=uuid.uuid4().hex, name=name,
                     collection_type="movies", upstream_source_ids=upstream)
    db.add(row)
    db.flush()
    return row


def _item(db, lib, name, **kw):
    kw.setdefault("enrich_status", "pending")
    row = em.MediaItem(
        guid=uuid.uuid4().hex, library_id=lib.id, item_type="movie",
        name=name, sort_name=name,
        file_path=f"/m/{uuid.uuid4().hex}.mkv", **kw)
    db.add(row)
    db.flush()
    return row


def _fetched_minimal():
    return {"ok": True, "poster": None, "fanart": None, "external_subs": [],
            "nfo_data": None, "tmdb_hit": None, "tmdb_details": None,
            "error": None}


def _apply(db, item, fetched=None):
    with mock.patch("backend.emby_server.tmdb.tmdb_client") as fake_client:
        fake_client.configured = True
        ew._enrich_apply(db, item, fetched or _fetched_minimal())
    db.commit()
    db.refresh(item)


def _core_missing_minor(db, lib, name, **kw):
    """核心齐（tmdb_id/年份/海报）但缺次要字段（简介/类型/演员）的条目。"""
    kw.setdefault("tmdb_id", "123")
    kw.setdefault("production_year", 2020)
    kw.setdefault("poster_path", "/tmp/p.jpg")
    kw.setdefault("overview", None)
    kw.setdefault("genres", None)
    return _item(db, lib, name, **kw)


# ---------- ①②③ Step 5 新标记语义 ----------

def test_cms_priority_first_round_enters_refill_not_terminal(db):
    """★ P5 核心回归：CMS 优先级（-10）首轮缺次要字段必须进补齐队列，
    不能再被误判已补过直接终态。"""
    lib = _lib(db, "库")
    it = _core_missing_minor(db, lib, "CMS 首轮剧",
                             enrich_priority=-10)  # CMS 入库优先级即 -10
    _apply(db, it)
    assert it.enrich_status == "pending", "首轮缺次要字段应进补齐队列"
    assert it.enrich_priority == -10
    assert it.enrich_field_refill_done is True, "入队即置补齐标记"


def test_refill_round_terminalizes_no_repeat(db):
    """补齐轮（标记已置）仍缺次要字段 → 直接终态化，每条最多补一轮。"""
    lib = _lib(db, "库")
    it = _core_missing_minor(db, lib, "补齐轮剧")
    _apply(db, it)
    assert it.enrich_status == "pending"
    assert it.enrich_field_refill_done is True
    _apply(db, it)
    assert it.enrich_status == "done", "补过一轮必须终态化，不许反复"
    assert it.enrich_priority == 0, "终态化后优先级清零"


def test_refill_round_got_fields_goes_done(db):
    """补齐轮拿到字段 → 正常 done。"""
    lib = _lib(db, "库")
    it = _core_missing_minor(db, lib, "补到字段剧",
                             enrich_field_refill_done=True)
    it.overview = "本轮拿到的简介"
    it.genres = "剧情"
    p = em.EmbyPerson(item_id=it.id, name="演员甲", sort_order=0)
    db.add(p)
    db.commit()
    _apply(db, it)
    assert it.enrich_status == "done"
    assert it.enrich_priority == 0


def test_field_refill_done_defaults_false(db):
    lib = _lib(db, "库")
    it = _item(db, lib, "新行")
    assert it.enrich_field_refill_done is False


# ---------- ④⑤ 回捞巡检 ----------

def _done_cms_missing(db, lib, name, **kw):
    kw.setdefault("enrich_status", "done")
    return _core_missing_minor(db, lib, name, **kw)


def test_recovery_flips_stock_with_cap_and_idempotent(db):
    cms = _lib(db, "无界库", upstream="7,9")
    local = _lib(db, "本地库", upstream="")
    a = _done_cms_missing(db, cms, "可捞A")
    a2 = _done_cms_missing(db, cms, "可捞B")
    _done_cms_missing(db, cms, "可捞C")
    complete = _done_cms_missing(db, cms, "全齐", overview="简介",
                                genres="剧情")
    db.add(em.EmbyPerson(item_id=complete.id, name="演员甲", sort_order=0))
    _done_cms_missing(db, cms, "已补过", enrich_field_refill_done=True)
    _done_cms_missing(db, local, "非CMS库")
    _done_cms_missing(db, cms, "搜过没有", metadata_source="none")
    _done_cms_missing(db, cms, "已锁定", metadata_locked=True)
    _done_cms_missing(db, cms, "已合并", merged_into_id=a.id)
    _core_missing_minor(db, cms, "还是pending",
                        enrich_status="pending")
    db.commit()

    n = ew.enrich_field_refill_recovery(db, limit=2)
    assert n == 2, "单轮上限 2 应恰好翻回 2 条"
    db.expire_all()
    for row in (a, a2):
        r = db.query(em.MediaItem).filter(em.MediaItem.id == row.id).first()
        assert r.enrich_status == "pending"
        assert r.enrich_priority == -10
        assert r.enrich_field_refill_done is True
        assert r.enrich_attempts == 0
        assert r.enrich_next_retry_at is None

    n2 = ew.enrich_field_refill_recovery(db, limit=2000)
    assert n2 == 1, "上一轮上限剩下的 1 条应在本轮捞回"
    n3 = ew.enrich_field_refill_recovery(db, limit=2000)
    assert n3 == 0, "全部捞回后不再有候选（幂等）"


def test_recovery_ignores_non_cms_and_complete_and_none(db):
    cms = _lib(db, "无界库", upstream="7,9")
    local = _lib(db, "本地库", upstream="")
    _done_cms_missing(db, local, "非CMS库")
    complete = _done_cms_missing(db, cms, "全齐", overview="简介",
                                genres="剧情")
    db.add(em.EmbyPerson(item_id=complete.id, name="演员甲", sort_order=0))
    _done_cms_missing(db, cms, "搜过没有", metadata_source="none")
    _done_cms_missing(db, cms, "已补过", enrich_field_refill_done=True)
    db.commit()
    assert ew.enrich_field_refill_recovery(db) == 0


def test_recovery_no_cms_libraries_returns_zero(db):
    lib = _lib(db, "本地库", upstream="")
    _done_cms_missing(db, lib, "本地done缺字段")
    db.commit()
    assert ew.enrich_field_refill_recovery(db) == 0
