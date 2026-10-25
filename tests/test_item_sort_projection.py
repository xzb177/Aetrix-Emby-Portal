"""媒体库学习 L5：条目排序投影（title_key / year_key + 游标分页）。

隔离临时 SQLite。覆盖：拼音键、年份降序 nulls last、游标全量遍历
无丢无重、过期/覆盖不全回落现算、增量重建。
"""

import os
import tempfile
from datetime import datetime, timedelta

os.environ.setdefault("DATABASE_TYPE", "sqlite")
os.environ.setdefault("REDIS_ENABLED", "false")
_fd, _tmppath = tempfile.mkstemp(suffix=".db")
os.close(_fd)
os.environ["DATABASE_URL"] = f"sqlite:///{_tmppath}"

from backend import database as _dbmod
from sqlalchemy import create_engine as _ce
from sqlalchemy.orm import sessionmaker as _sm

_dbmod.engine = _ce(os.environ["DATABASE_URL"])
_dbmod.configure_session_local(_sm(bind=_dbmod.engine))

import pytest

from backend.database import SessionLocal, init_db
from backend.emby_server import item_sort_projection as isp
from backend.emby_server import models as em

init_db()


@pytest.fixture()
def db():
    s = SessionLocal()
    try:
        for table in (em.MediaItemSortProjection, em.MediaItem, em.Library):
            s.query(table).delete()
        s.commit()
        yield s
    finally:
        s.close()


def _seed(db):
    lib = em.Library(name="库", guid=f"lib-{os.urandom(4).hex()}")
    db.add(lib)
    db.commit()
    items = [
        ("流浪地球", 2023),
        ("Apple", 2024),
        ("香蕉", None),
        ("长津湖", 2020),
        ("banana", 2024),
    ]
    rows = []
    for name, year in items:
        it = em.MediaItem(
            name=name, guid=f"item-{os.urandom(6).hex()}",
            library_id=lib.id, item_type="movie", production_year=year,
            date_added=datetime(2024, 1, 1),
        )
        db.add(it)
        rows.append(it)
    db.commit()
    for it in rows:
        db.refresh(it)
    return lib, rows


def _walk(db, lib_id, sort, limit=2):
    """游标翻完整库：返回按序取到的全部条目（断言无丢无重用）。"""
    seen, cursor_key, cursor_id = [], None, None
    for _ in range(20):
        items, nxt = isp.load_page(db, lib_id, sort, cursor_key, cursor_id, limit)
        seen.extend(items)
        if nxt is None:
            break
        _s, cursor_key, cursor_id = isp.decode_cursor(nxt)
    return seen


def test_title_key_uses_pinyin(db):
    lib, rows = _seed(db)
    isp.rebuild_projection(db)
    db.commit()
    proj = {r.item_id: r for r in db.query(em.MediaItemSortProjection).all()}
    assert proj[rows[0].id].title_key == "liulangdiqiu"  # 流浪地球
    assert proj[rows[1].id].title_key == "apple"
    assert proj[rows[2].id].year_key is None


def test_title_cursor_walk_covers_all_without_dup(db):
    lib, rows = _seed(db)
    isp.rebuild_projection(db)
    db.commit()
    seen = _walk(db, lib.id, "title")
    assert len(seen) == 5
    assert len({it.id for it in seen}) == 5
    keys = [isp.compute_keys(it)[0] for it in seen]
    assert keys == sorted(keys)


def test_year_cursor_walk_desc_nulls_last(db):
    lib, rows = _seed(db)
    isp.rebuild_projection(db)
    db.commit()
    seen = _walk(db, lib.id, "year")
    years = [it.production_year for it in seen]
    assert years == [2024, 2024, 2023, 2020, None]
    assert len({it.id for it in seen}) == 5


def test_stale_row_falls_back_to_live(db, monkeypatch):
    lib, rows = _seed(db)
    isp.rebuild_projection(db)
    db.commit()
    row = db.query(em.MediaItemSortProjection).first()
    row.computed_at = datetime.now() - timedelta(hours=2)
    db.commit()
    monkeypatch.setattr(isp, "MAX_STALE_SEC", 60)
    items, _nxt = isp.load_page(db, lib.id, "title", None, None, 50)
    assert len(items) == 5  # 回落现算，全量仍在


def test_coverage_gap_falls_back_to_live(db):
    lib, rows = _seed(db)
    isp.rebuild_projection(db)
    db.commit()
    db.query(em.MediaItemSortProjection).filter(
        em.MediaItemSortProjection.item_id == rows[0].id).delete()
    db.commit()
    items, _nxt = isp.load_page(db, lib.id, "title", None, None, 50)
    assert len(items) == 5  # 投影缺行 → 回落现算，不静默丢条目


def test_rebuild_incremental_updates_and_cleans(db):
    lib, rows = _seed(db)
    stats = isp.rebuild_projection(db)
    db.commit()
    assert stats["updated"] == 5
    # 改名 → updated_at 推进 → 重建更新键
    rows[0].name = "流浪地球2"
    db.commit()
    stats = isp.rebuild_projection(db)
    db.commit()
    assert stats["updated"] == 1
    proj = db.query(em.MediaItemSortProjection).filter(
        em.MediaItemSortProjection.item_id == rows[0].id).first()
    assert proj.title_key == "liulangdiqiu2"
    # 删条目 → 重建清孤行
    db.delete(rows[1])
    db.commit()
    stats = isp.rebuild_projection(db)
    db.commit()
    assert stats["deleted"] == 1
    assert db.query(em.MediaItemSortProjection).filter(
        em.MediaItemSortProjection.item_id == rows[1].id).first() is None


def test_cursor_decode_rejects_garbage():
    with pytest.raises(ValueError):
        isp.decode_cursor("not-a-cursor!!")
    with pytest.raises(ValueError):
        isp.decode_cursor(isp.encode_cursor("bogus", "x", 1))
