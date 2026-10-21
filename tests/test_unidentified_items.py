"""按库未识别工作台（GET /libraries/{id}/unidentified 后端逻辑）。

口径：顶层 movie/series + 无 tmdb_id + 可见（未软删、未合并），按 id 倒序分页。
排除项逐个立条目验证：已绑 tmdb_id 的、别的库的、季/集、软删行、已合并行。
"""
import os
import tempfile
from types import SimpleNamespace

os.environ.setdefault("DATABASE_TYPE", "sqlite")
os.environ.setdefault("REDIS_ENABLED", "false")
_fd, _tmp = tempfile.mkstemp(suffix=".db")
os.close(_fd)
os.environ["DATABASE_URL"] = f"sqlite:///{_tmp}"

from backend import database as _dbmod  # noqa: E402
from sqlalchemy import create_engine as _ce  # noqa: E402
from sqlalchemy.orm import sessionmaker as _sm  # noqa: E402

_dbmod.engine = _ce(os.environ["DATABASE_URL"])
_dbmod.configure_session_local(_sm(bind=_dbmod.engine))

from backend.database import init_db  # noqa: E402

init_db()

import pytest  # noqa: E402
from fastapi import HTTPException  # noqa: E402

from backend.api import admin_scrape  # noqa: E402
from backend.emby_server import models as em  # noqa: E402


@pytest.fixture()
def own_db():
    """本用例自建库并设为当前 factory（用完还原），不依赖测试执行顺序"""
    from backend import models as base_models
    engine = _ce("sqlite:///:memory:")
    original = _dbmod.SessionLocal
    _dbmod.configure_session_local(_sm(bind=engine))
    base_models.Base.metadata.create_all(bind=engine)
    em.Base.metadata.create_all(bind=engine)
    db = _dbmod.SessionLocal()
    try:
        lib = em.Library(guid="L" * 32, name="目标库", collection_type="tvshows", paths="")
        other = em.Library(guid="M" * 32, name="别的库", collection_type="movies", paths="")
        db.add_all([lib, other])
        db.flush()
        yield db, lib, other
    finally:
        db.close()
        _dbmod.configure_session_local(original)
        engine.dispose()


_seq = [0]


def _item(db, lib, name, **kw):
    _seq[0] += 1
    base = dict(guid=f"{_seq[0]:032d}", library_id=lib.id, item_type="series",
                name=name, production_year=2024, tmdb_id=None,
                metadata_source="none", enrich_status="done")
    base.update(kw)
    it = em.MediaItem(**base)
    db.add(it)
    db.flush()
    return it


def _list(db, lib_id, **kw):
    return admin_scrape.list_unidentified_items(
        lib_id, kw.get("page", 1), kw.get("page_size", 20),
        SimpleNamespace(id=1), db)


def test_lists_only_unbound_top_level_visible(own_db):
    db, lib, other = own_db
    keep1 = _item(db, lib, "未识别剧")
    keep2 = _item(db, lib, "未识别电影", item_type="movie")
    _item(db, lib, "已绑定剧", tmdb_id="123", metadata_source="tmdb")
    _item(db, other, "别库的剧")
    _item(db, lib, "某一集", item_type="episode")
    _item(db, lib, "某一季", item_type="season")
    from datetime import datetime
    _item(db, lib, "被软删的剧", deleted_at=datetime.now())
    anchor = _item(db, lib, "合并目标", tmdb_id="999", metadata_source="tmdb")
    _item(db, lib, "被合并的副本", merged_into_id=anchor.id)
    db.commit()

    res = _list(db, lib.id)
    assert res["total"] == 2
    assert [r["name"] for r in res["items"]] == ["未识别电影", "未识别剧"]  # id 倒序
    row = res["items"][1]
    assert row["id"] == keep1.id and row["tmdb_id"] is None
    assert row["year"] == 2024 and row["item_type"] == "series"
    assert row["metadata_source"] == "none" and row["enrich_status"] == "done"
    assert keep2.id in [r["id"] for r in res["items"]]


def test_pagination(own_db):
    db, lib, _other = own_db
    for i in range(5):
        _item(db, lib, f"剧{i}")
    db.commit()

    p1 = _list(db, lib.id, page=1, page_size=2)
    p2 = _list(db, lib.id, page=2, page_size=2)
    p3 = _list(db, lib.id, page=3, page_size=2)
    assert p1["total"] == 5 and len(p1["items"]) == 2
    assert len(p2["items"]) == 2 and len(p3["items"]) == 1
    ids = [r["id"] for r in p1["items"] + p2["items"] + p3["items"]]
    assert len(set(ids)) == 5  # 分页不重不漏


def test_missing_library_404(own_db):
    db, _lib, _other = own_db
    with pytest.raises(HTTPException) as ei:
        _list(db, 999999)
    assert ei.value.status_code == 404


def test_bound_item_leaves_list(own_db):
    """绑定后（有了 tmdb_id）从未识别列表消失——工作台的闭环口径"""
    db, lib, _other = own_db
    it = _item(db, lib, "待绑剧")
    db.commit()
    assert _list(db, lib.id)["total"] == 1
    it.tmdb_id = "777"
    it.metadata_source = "tmdb"
    db.commit()
    assert _list(db, lib.id)["total"] == 0
