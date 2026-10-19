"""r51 新合并代码审查补洞回归测试。

F1（enrich_worker._enrich_season_posters_io）：season_number 为 None 的季行
必须跳过——不能调 tmdb_client.season_poster(tmdb_id, None)（内部会退化成
/tv/{id}/season/0，specials 海报错配落到无季号行上）。

F2（media_routes._virtual_season_series）：虚拟季 Id 只能解析到 series 行；
数字 id 属于电影/单集等非 series 行时必须返回 None（不把 guid 寻址降级成
数字 id 枚举）。

全部在隔离临时 SQLite 里跑，不连生产库；不测真实 TMDB 网络。
"""
import os
import tempfile

os.environ.setdefault("DATABASE_TYPE", "sqlite")
os.environ.setdefault("REDIS_ENABLED", "false")
_fd, _tmppath = tempfile.mkstemp(suffix=".db"); os.close(_fd)
os.environ["DATABASE_URL"] = "sqlite:///" + _tmppath
# r51 注：不要在这里设置 EMBY_TMDB_CACHE_DIR——import 时设置是进程全局的，
# 本文件字母排序在 test_image_season_poster_ws5.py 之后，会把它 fixture 要清
# 的缓存目录劫持走，导致其 TestSeasonPosterClient 两个用例在全量运行时因
# 缓存未清而失败（CI 实证）。本文件测试 mock 掉 season_poster，根本用不到
# 磁盘缓存。
from backend import database as _dbmod
from sqlalchemy import create_engine as _ce
from sqlalchemy.orm import sessionmaker as _sm
_dbmod.engine = _ce(os.environ["DATABASE_URL"])
_dbmod.configure_session_local(_sm(bind=_dbmod.engine))

import pytest
from types import SimpleNamespace

from backend.database import SessionLocal, init_db
from backend.emby_server import models as em
from backend.emby_server import enrich_worker
from backend.emby_server import tmdb as tmdb_mod

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


def _mk_item(db, **kw):
    import uuid as _uuid
    kw.setdefault("guid", _uuid.uuid4().hex)
    it = em.MediaItem(**kw)
    db.add(it)
    db.commit()
    return it


class TestNullSeasonNumberSkipped:
    def test_none_season_number_not_requested(self, monkeypatch, db):
        """F1：无季号的季行不触发 season_poster 请求。"""
        series = _mk_item(db, item_type="series", name="测试剧",
                          tmdb_id="87187", library_id=1)
        _mk_item(db, item_type="season", name="无季号季",
                 series_id=series.id, parent_id=series.id,
                 season_number=None, library_id=1)
        s_ok = _mk_item(db, item_type="season", name="第 1 季",
                        series_id=series.id, parent_id=series.id,
                        season_number=1, library_id=1)
        seen = []
        monkeypatch.setattr(
            tmdb_mod.tmdb_client, "season_poster",
            lambda tid, sn: seen.append((tid, sn)) or "https://img.example/w500/s1.jpg")
        monkeypatch.setattr("backend.emby_server.image_store.prewarm",
                            lambda urls: 0)
        snapshot = SimpleNamespace(id=series.id, item_type="series",
                                   tmdb_id="87187")
        fetched = {"tmdb_id": "87187", "ok": True}
        enrich_worker._enrich_season_posters_io(db, snapshot, fetched)
        # 只给有季号的第 1 季发了请求；无季号行被跳过
        assert seen == [("87187", 1)]
        assert fetched["season_posters"] == {s_ok.id: "https://img.example/w500/s1.jpg"}


class TestVirtualSeasonSeries:
    def test_series_row_resolves(self, db):
        from backend.emby_server import media_routes
        series = _mk_item(db, item_type="series", name="测试剧2", library_id=1)
        got = media_routes._virtual_season_series(db, f"vseason-{series.id}-3")
        assert got is not None and got.id == series.id

    def test_non_series_row_rejected(self, db):
        """F2：数字 id 属于电影行时必须返回 None。"""
        from backend.emby_server import media_routes
        movie = _mk_item(db, item_type="movie", name="测试电影", library_id=1)
        assert media_routes._virtual_season_series(
            db, f"vseason-{movie.id}-1") is None

    def test_malformed_id_returns_none(self, db):
        from backend.emby_server import media_routes
        assert media_routes._virtual_season_series(db, "vseason-abc") is None
        assert media_routes._virtual_season_series(db, "not-a-virtual-id") is None
        assert media_routes._virtual_season_series(db, "vseason-999999999-1") is None
