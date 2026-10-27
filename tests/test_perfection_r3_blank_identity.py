"""完美审查第三轮第 2 路：#1383 / #1377 空白身份残留复现。

背景：#1383 只在 enrich_worker 的 4 处 done 终态点清 tmdb_id / imdb_id
的纯空白；#1377 只把 extract_binding 的身份取值换成 strip 归一。
本文件钉死同一家族在其它路径上的残留——每个测试先在修前跑红、
修后跑绿；只断言真实代码行为，不测实现细节。
"""
import os
import tempfile
import uuid
from datetime import datetime
from types import SimpleNamespace

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

import pytest

from backend.database import SessionLocal, init_db
from backend.emby_server import models as em
from backend.emby_server import enrich_worker
from backend.emby_server import identify_two_stage as ts

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
        name=kw.pop("name", "测试片"),
        file_path=kw.pop("file_path", "/tmp/x.mp4"),
        enrich_status=kw.pop("enrich_status", "pending"),
        **kw,
    )
    db.add(it)
    db.commit()
    return it


# --- R1：现行写库裸 or 吞掉本轮真实 tmdb_id，done 清理再补刀清成 NULL ---
def _complete_minor_fields(db, it):
    """Step 5 次要字段（简介/类型/演员）补齐，否则本轮进低优先级补刮队列
    而非 done，断言不到终态清理。"""
    it.genres = "剧情"
    db.add(em.EmbyPerson(item_id=it.id, name="演员甲"))
    db.commit()


def test_enrich_apply_blank_item_tmdb_does_not_swallow_fetched_id(db):
    it = _make_item(
        db, tmdb_id="  ", production_year=2020, poster_path="/x.jpg",
        overview="简介已齐",
    )
    _complete_minor_fields(db, it)
    fetched = {"tmdb_id": "12345"}
    enrich_worker._enrich_apply(db, it, fetched)
    # _enrich_apply 本身不提交（调用方短事务提交），先 commit 再 refresh。
    db.commit()
    db.refresh(it)
    assert it.enrich_status == "done"
    assert it.tmdb_id == "12345"


# --- R1b：同函数的 had_tmdb_id 裸 bool，把空白当成“此前已有” ---
def test_enrich_apply_blank_series_marks_parent_newly_acquired(db):
    it = _make_item(
        db, item_type="series", tmdb_id="  ", production_year=2020,
        poster_path="/x.jpg", overview="简介已齐",
    )
    _complete_minor_fields(db, it)
    fetched = {"tmdb_id": "999"}
    enrich_worker._enrich_apply(db, it, fetched)
    db.commit()
    db.refresh(it)
    assert it.enrich_status == "done"
    assert it.tmdb_id == "999"
    assert fetched.get("_parent_tmdb_newly_acquired") == it.id


# --- R2：resolve_imdb_id 裸 or：空白 imdb 被当成“条目已有” ---
def test_resolve_imdb_blank_item_imdb_is_not_a_binding():
    item = SimpleNamespace(id=1, item_type="movie", tmdb_id="12345",
                           imdb_id="  ")
    imdb_id, source = ts.resolve_imdb_id(item, {}, None)
    assert (imdb_id, source) == (None, None)


# --- R2b：resolve_imdb_id 裸 or：空白 item tmdb 压住 fetched 真实 tmdb ---
def test_resolve_imdb_uses_fetched_tmdb_when_item_tmdb_blank():
    item = SimpleNamespace(id=1, item_type="movie", tmdb_id="  ",
                           imdb_id=None)
    seen = []

    def fetcher(tmdb_id, kind):
        seen.append((tmdb_id, kind))
        return {"imdb_id": "tt999"}

    imdb_id, source = ts.resolve_imdb_id(
        item, {"tmdb_id": "12345"}, fetcher)
    assert seen == [("12345", "movie")]
    assert (imdb_id, source) == ("tt999", "tmdb_details")


# --- R3：donor 绑定裸 or：纯空白 donor 行算 donor、还能毒化歧义判定 ---
def test_donor_binding_blank_row_is_not_a_donor():
    row = SimpleNamespace(tmdb_id="  ", imdb_id=None)
    assert ts._donor_binding([row]) == (None, False)


def test_donor_binding_blank_row_does_not_poison_ambiguity():
    blank = SimpleNamespace(tmdb_id="  ", imdb_id=None)
    real = SimpleNamespace(tmdb_id="123", imdb_id=None)
    pair, ambiguous = ts._donor_binding([blank, real])
    assert ambiguous is False
    assert pair == ("123", None)


class _FakeQuery:
    def __init__(self, rows):
        self._rows = rows

    def filter(self, *a, **k):
        return self

    def all(self):
        return self._rows

    def first(self):
        return self._rows[0] if self._rows else None


class _FakeDb:
    """只够 identify 两段用的替身：按模型分发行。

    EnrichStageRecord 查询返回空（走新建分支）；其余查询返回 donor 行。
    """

    def __init__(self, donor_rows):
        self._donor_rows = donor_rows
        self.added = []

    def query(self, model):
        if model is em.EnrichStageRecord:
            return _FakeQuery([])
        return _FakeQuery(self._donor_rows)

    def add(self, obj):
        self.added.append(obj)


# --- R3b：donor 继承门禁裸 or：空白身份条目被当成“已有号”挡在门外 ---
def test_inherit_binding_blank_item_is_eligible():
    donor = SimpleNamespace(id=7, tmdb_id="123", imdb_id=None)
    item = SimpleNamespace(id=1, item_type="movie", tmdb_id="  ",
                           imdb_id=None, drive_file_id="fid-1",
                           content_hash=None, content_hash_algo=None,
                           size=0, name="测试片")
    binding = ts.inherit_binding_from_remote_identity(_FakeDb([donor]), item)
    assert binding is not None
    assert binding["tmdb_id"] == "123"
    assert binding["confidence"] == "inherited"


# --- R4：补资料段记录裸 or：空白 tmdb 压住真实 imdb ---
def test_apply_fill_stage_record_prefers_real_imdb_over_blank_tmdb():
    item = SimpleNamespace(id=1, item_type="movie", name="测试片",
                           tmdb_id="  ", imdb_id="tt123")
    fake = _FakeDb([])
    ts.apply_fill(fake, item, {})
    assert len(fake.added) == 1
    record = fake.added[0]
    assert record.provider == "imdb"
    assert record.external_id == "tt123"


# --- R5：_suppress_flood 批量 done 旁路 _clear_blank_identity ---
def test_suppress_flood_clears_blank_identity(db):
    it = _make_item(db, tmdb_id="  ", imdb_id="  ",
                    last_scraped_at=datetime.now())
    n = enrich_worker._suppress_flood(db)
    assert n == 1
    db.refresh(it)
    assert it.enrich_status == "done"
    assert it.tmdb_id is None
    assert it.imdb_id is None
