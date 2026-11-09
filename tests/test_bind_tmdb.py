"""手动绑定 TMDB ID：给刮不上的条目一个出口。

TMDB 对中文剧集/综艺收录偏少，生产实测 158 条怎么搜都搜不到，反复重试白试。
管理员手动指定 ID 比再接第四个数据源更治本——但必须防「绑错」和「绑到不存在的 ID」。
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


import pytest  # noqa: E402

from backend.api import admin_scrape  # noqa: E402
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
    """本用例自建库并设为当前 factory（用完还原），不依赖测试执行顺序"""
    from backend import models as base_models
    engine = _ce("sqlite:///:memory:")
    original = _dbmod.SessionLocal
    _dbmod.configure_session_local(_sm(bind=engine))
    base_models.Base.metadata.create_all(bind=engine)
    em.Base.metadata.create_all(bind=engine)
    db = _dbmod.SessionLocal()
    try:
        lib = em.Library(guid="L" * 32, name="库", collection_type="tvshows", paths="")
        db.add(lib)
        db.flush()
        yield db, lib
    finally:
        db.close()
        _dbmod.configure_session_local(original)
        engine.dispose()


def _series(db, lib, name="刮不上的剧", **kw):
    base = dict(guid="s" * 32, item_type="series", name=name, overview="",
                tmdb_id=None, metadata_source="none", enrich_status="failed",
                enrich_attempts=5, enrich_next_retry_at=None, last_scraped_at=None)
    base.update(kw)
    it = em.MediaItem(library_id=lib.id, **base)
    db.add(it)
    db.flush()
    return it


def test_bind_sets_tmdb_id_and_fills(own_db, monkeypatch):
    db, lib = own_db
    it = _series(db, lib)
    user = SimpleNamespace(id=1)
    details = {"id": 12345, "name": "正确的剧名", "overview": "简介",
               "vote_average": 8.0, "poster_path": "/p.jpg",
               "external_ids": {"imdb_id": "tt1"}, "alternative_titles": {}}
    class _T:
        configured = True

        def details(self, *a, **k):
            return details

        def apply_details(self, item, d):
            item.imdb_id = "tt1"

        def apply_images(self, item, d):
            return True
    monkeypatch.setattr(admin_scrape, "tmdb_client", _T())

    res = admin_scrape.bind_tmdb_id(it.id, admin_scrape.TmdbBindRequest(tmdb_id="12345"),
                                    user, db)
    assert res["success"] is True
    assert it.tmdb_id == "12345"
    assert it.metadata_source == "tmdb"
    assert it.enrich_status == "done", "绑定了权威 ID 就不该再排进补全队列"


def test_bind_rejects_unknown_tmdb_id(own_db, monkeypatch):
    """TMDB 上查无此 ID 时必须拒绝绑定——否则会绑上一个不存在的条目"""
    from fastapi import HTTPException
    db, lib = own_db
    it = _series(db, lib)
    user = SimpleNamespace(id=1)
    class _T:
        configured = True

        def details(self, *a, **k):
            return None
    monkeypatch.setattr(admin_scrape, "tmdb_client", _T())
    with pytest.raises(HTTPException) as e:
        admin_scrape.bind_tmdb_id(it.id, admin_scrape.TmdbBindRequest(tmdb_id="99999999"),
                                  user, db)
    assert e.value.status_code == 404
    assert it.tmdb_id is None, "校验失败时不能写入 tmdb_id"


def test_bind_rejects_non_numeric(own_db):
    from fastapi import HTTPException
    db, lib = own_db
    it = _series(db, lib)
    with pytest.raises(HTTPException) as e:
        admin_scrape.bind_tmdb_id(it.id, admin_scrape.TmdbBindRequest(tmdb_id="abc"),
                                  SimpleNamespace(id=1), db)
    assert e.value.status_code == 400


def test_bind_transient_returns_503(own_db, monkeypatch):
    """TMDB 瞬态不可用：校验返 503——而不是 404 误报「找不到」或裸 500"""
    from fastapi import HTTPException
    from backend.emby_server.tmdb import TmdbTransientError
    db, lib = own_db
    it = _series(db, lib)

    class _T:
        configured = True

        def details(self, *a, **k):
            raise TmdbTransientError("TMDB 网络请求失败（重试耗尽）: /tv/1")

    monkeypatch.setattr(admin_scrape, "tmdb_client", _T())
    with pytest.raises(HTTPException) as e:
        admin_scrape.bind_tmdb_id(it.id, admin_scrape.TmdbBindRequest(tmdb_id="123"),
                                  SimpleNamespace(id=1), db)
    assert e.value.status_code == 503
    assert it.tmdb_id is None, "校验没通过就不能写入 tmdb_id"


def test_preview_transient_returns_503(own_db, monkeypatch):
    """tmdb-preview 校验：瞬态失败 → 503（404 语义只留给真没有）"""
    from fastapi import HTTPException
    from backend.emby_server.tmdb import TmdbTransientError
    db, lib = own_db
    it = _series(db, lib)

    class _T:
        configured = True

        def details(self, *a, **k):
            raise TmdbTransientError("TMDB 服务端错误 HTTP 503: /tv/1")

    monkeypatch.setattr(admin_scrape, "tmdb_client", _T())
    user = SimpleNamespace(id=1)
    with pytest.raises(HTTPException) as e:
        admin_scrape.preview_tmdb_id(it.id, "1", user, db)
    assert e.value.status_code == 503


def test_unbind_clears_and_requeues(own_db, monkeypatch):
    """解绑后应重新排入补全队列，让自动刮削有机会再试"""
    db, lib = own_db
    it = _series(db, lib, tmdb_id="12345", metadata_source="tmdb",
                 enrich_status="done", last_scraped_at=__import__("datetime").datetime.now())
    res = admin_scrape.bind_tmdb_id(it.id, admin_scrape.TmdbBindRequest(tmdb_id=""),
                                    SimpleNamespace(id=1), db)
    assert res["unbound"] is True
    assert it.tmdb_id is None
    assert it.enrich_status == "pending"
    assert it.metadata_source is None


def test_preview_flags_name_mismatch(own_db, monkeypatch):
    """预览要能提示「这条和你手上的剧名对不上」，减少绑错"""
    db, lib = own_db
    it = _series(db, lib, name="完全不同的名字")
    user = SimpleNamespace(id=1)
    class _T:
        configured = True

        def details(self, *a, **k):
            return {"id": 1, "name": "某剧", "first_air_date": "2020-01-01"}
    monkeypatch.setattr(admin_scrape, "tmdb_client", _T())
    res = admin_scrape.preview_tmdb_id(it.id, "1", user, db)
    assert res["title"] == "某剧"
    assert res["matches_current"] is False, res
