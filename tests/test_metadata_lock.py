"""元数据锁定（P3，Emby 式手动识别）：锁定防自动覆盖，手动优先。

- 锁定的条目：enrich worker 抢单（_claim_batch）必须跳过；
- lock / unlock API 置位，GET 详情返回 metadata_locked；
- 手动绑定 bind_tmdb_id 不受锁定影响（手动永远优先于锁定）；
- _auto_migrate 幂等：老表补上 metadata_locked 列。
"""
import os
import tempfile
from types import SimpleNamespace

os.environ.setdefault("DATABASE_TYPE", "sqlite")
os.environ.setdefault("REDIS_ENABLED", "false")
_fd, _tmp = tempfile.mkstemp(suffix=".db")
os.close(_fd)

from backend import database as _dbmod  # noqa: E402
from sqlalchemy import create_engine as _ce  # noqa: E402
from sqlalchemy.orm import sessionmaker as _sm  # noqa: E402


from backend.database import init_db  # noqa: E402


import uuid  # noqa: E402

import pytest  # noqa: E402
from fastapi import HTTPException  # noqa: E402

from backend.api import admin_scrape  # noqa: E402
from backend.emby_server import enrich_worker  # noqa: E402
from backend.emby_server import models as em  # noqa: E402


@pytest.fixture(scope="module", autouse=True)
def _hygiene_module_engine():
    """测试卫生：本文件独占临时库在模块夹具内换绑、模块结束原样还原。

    导入期顶层换绑会让全局 engine/SessionLocal 在 collection 后停在本文件
    的临时库上，后面的文件/用例撞 no such table 或读错库（#1668 同族）；
    换绑只在本模块用例期间生效。
    """
    _hyg_saved_engine = _dbmod.engine
    _hyg_saved_factory = _dbmod.SessionLocal._factory
    _hyg_saved_url = os.environ.get("DATABASE_URL")
    os.environ["DATABASE_URL"] = f"sqlite:///{_tmp}"
    _dbmod.engine = _ce(os.environ["DATABASE_URL"])
    _dbmod.configure_session_local(_sm(bind=_dbmod.engine))
    init_db()
    _hyg_engine = _dbmod.engine
    try:
        yield
    finally:
        _dbmod.engine = _hyg_saved_engine
        _dbmod.configure_session_local(_hyg_saved_factory)
        if _hyg_saved_url is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = _hyg_saved_url
        _hyg_engine.dispose()

@pytest.fixture()
def own_db():
    """本用例自建内存库（create_all 会带上 metadata_locked 列），用完还原"""
    from backend import models as base_models
    engine = _ce("sqlite:///:memory:")
    original = _dbmod.SessionLocal
    _dbmod.configure_session_local(_sm(bind=engine))
    base_models.Base.metadata.create_all(bind=engine)
    em.Base.metadata.create_all(bind=engine)
    db = _dbmod.SessionLocal()
    try:
        lib = em.Library(guid="L" * 32, name="库", collection_type="movies", paths="")
        db.add(lib)
        db.flush()
        yield db, lib
    finally:
        db.close()
        _dbmod.configure_session_local(original)
        engine.dispose()


def _movie(db, lib, name="锁定的电影", **kw):
    base = dict(guid=uuid.uuid4().hex, item_type="movie", name=name,
                enrich_status="pending", enrich_attempts=0,
                enrich_next_retry_at=None, metadata_locked=False)
    base.update(kw)
    it = em.MediaItem(library_id=lib.id, **base)
    db.add(it)
    db.commit()
    return it


def _staff():
    return SimpleNamespace(id=1)


# ---------- enrich 抢单跳过锁定条目 ----------

def test_claim_batch_skips_locked_items(own_db):
    """pending 的锁定条目不能被 _claim_batch 抢走；未锁定的正常抢到"""
    db, lib = own_db
    locked = _movie(db, lib, name="锁定的", metadata_locked=True)
    unlocked = _movie(db, lib, name="未锁定的", metadata_locked=False)

    claimed = enrich_worker._claim_batch(db, 10)
    claimed_ids = {c.id for c in claimed}

    assert unlocked.id in claimed_ids, "未锁定的 pending 条目应被抢单"
    assert locked.id not in claimed_ids, "锁定的 pending 条目不应被抢单"
    db.refresh(locked)
    db.refresh(unlocked)
    assert locked.enrich_status == "pending", "锁定的条目状态不应被改动"
    assert unlocked.enrich_status == "enriching"


def test_claim_batch_all_locked_returns_empty(own_db):
    """全锁定的队列抢单应返回空，不报错"""
    db, lib = own_db
    _movie(db, lib, name="只锁定这一条", metadata_locked=True)
    assert enrich_worker._claim_batch(db, 10) == []


# ---------- lock / unlock / GET 详情 ----------

def test_lock_and_unlock_roundtrip(own_db):
    db, lib = own_db
    it = _movie(db, lib)
    assert it.metadata_locked is False

    res = admin_scrape.lock_item_metadata(it.id, _staff(), db)
    assert res["success"] is True
    assert res["item"]["metadata_locked"] is True
    db.refresh(it)
    assert it.metadata_locked is True

    res = admin_scrape.get_item_lock_status(it.id, _staff(), db)
    assert res["success"] is True
    assert res["item"]["metadata_locked"] is True
    assert res["item"]["id"] == it.id

    res = admin_scrape.unlock_item_metadata(it.id, _staff(), db)
    assert res["success"] is True
    assert res["item"]["metadata_locked"] is False
    db.refresh(it)
    assert it.metadata_locked is False


def test_lock_nonexistent_item_404(own_db):
    db, _lib = own_db
    with pytest.raises(HTTPException) as e:
        admin_scrape.lock_item_metadata(999999, _staff(), db)
    assert e.value.status_code == 404
    with pytest.raises(HTTPException) as e:
        admin_scrape.unlock_item_metadata(999999, _staff(), db)
    assert e.value.status_code == 404
    with pytest.raises(HTTPException) as e:
        admin_scrape.get_item_lock_status(999999, _staff(), db)
    assert e.value.status_code == 404


# ---------- 手动绑定不受锁定影响 ----------

def test_bind_tmdb_ignores_lock(own_db):
    """锁定的条目手动绑定 TMDB 不被拦截（手动永远优先于锁定）"""
    db, lib = own_db
    it = _movie(db, lib, metadata_locked=True, enrich_status="pending")
    res = admin_scrape.bind_tmdb_id(
        it.id, admin_scrape.TmdbBindRequest(tmdb_id="12345", verify=False),
        _staff(), db)
    assert res["success"] is True
    assert it.tmdb_id == "12345"
    assert it.enrich_status == "done"
    # 锁定态保持：绑定是手动操作，不顺手解锁
    db.refresh(it)
    assert it.metadata_locked is True


def test_unbind_unlocks_bind_lock(own_db):
    """解绑时解锁：绑定自动加的锁随解绑解除，条目回到自动刮削队列。

    绑定-锁定生命周期（Hark 刮削第 3 项）：bind 自动锁定 → unbind 自动解锁。
    解绑的语义是"交还给自动刮削"；锁定的条目自动刮削永不认领，
    保留锁会制造 pending 僵尸（一直 pending 但永远不被处理）。
    通过锁定 API 手动加的锁，解绑时同样解除——如需继续保护，重新锁定即可。
    """
    db, lib = own_db
    it = _movie(db, lib, metadata_locked=True, tmdb_id="12345",
                enrich_status="done")
    res = admin_scrape.bind_tmdb_id(
        it.id, admin_scrape.TmdbBindRequest(tmdb_id=""),
        _staff(), db)
    assert res["unbound"] is True
    db.refresh(it)
    assert it.metadata_locked is False
    assert it.enrich_status == "pending"


# ---------- _auto_migrate 幂等补列 ----------

def test_auto_migrate_adds_metadata_locked_column():
    """_auto_migrate：老表缺 metadata_locked 列时补上（幂等：已有时跳过）。

    桩库只建一张只有 id 的 emby_items 表，模拟「升级上来的老库」；
    _auto_migrate 里其它表的缺失会被跳过（表不存在 → continue）。
    """
    from sqlalchemy import inspect, text

    from backend import database as dbmod

    engine = _ce("sqlite:///:memory:")
    with engine.begin() as conn:
        conn.execute(text("CREATE TABLE emby_items (id INTEGER PRIMARY KEY)"))
    original_engine = dbmod.engine
    dbmod.engine = engine
    try:
        dbmod._auto_migrate()
        cols = {c["name"] for c in inspect(engine).get_columns("emby_items")}
        assert "metadata_locked" in cols, "老库升级后必须补上 metadata_locked 列"
        # 幂等：再跑一次不报错、不重复加列
        dbmod._auto_migrate()
        cols2 = {c["name"] for c in inspect(engine).get_columns("emby_items")}
        assert cols2 == cols
    finally:
        dbmod.engine = original_engine
        engine.dispose()
