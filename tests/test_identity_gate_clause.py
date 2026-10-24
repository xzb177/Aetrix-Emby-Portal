"""身份准入货架·判据单测（identity_gate.identity_clause）。

判据口径（2026-10-24 案 A 定版，backend/emby_server/identity_gate.py）：
- movie/series：自身 tmdb_id / imdb_id 任一 strip 后非空才算已绑定；
- season/episode：跟随所属 series 行；自身有编号亦算；
- 只有豆瓣身份（external_ids.douban）不算；空白串不算；
- series 行已软删时不能替它的集「绑定」（EXISTS 显式排除软删行）；
- 非四类（person 等）不在治理内，恒通过。
"""
import os

os.environ.setdefault("DATABASE_TYPE", "sqlite")
os.environ.setdefault("REDIS_ENABLED", "false")

import uuid
from datetime import datetime

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend import models  # noqa: F401  注册全部表
from backend.database import Base
from backend.emby_server import identity_gate
from backend.emby_server import models as em


@pytest.fixture()
def db(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path}/gate.db")
    Base.metadata.create_all(engine)
    s = sessionmaker(bind=engine)()
    try:
        yield s
    finally:
        s.close()


def _mk(db, lib, item_type, name, **kw):
    it = em.MediaItem(
        guid=uuid.uuid4().hex, library_id=lib.id, item_type=item_type,
        name=name, sort_name=name, file_path=f"/m/{uuid.uuid4().hex}.mkv",
        **kw)
    db.add(it)
    db.flush()
    return it


@pytest.fixture()
def scene(db):
    lib = em.Library(guid=uuid.uuid4().hex, name="库",
                     collection_type="mixed", is_enabled=True)
    db.add(lib)
    db.flush()
    m = {}
    m["m_tmdb"] = _mk(db, lib, "movie", "有TMDB", tmdb_id="123")
    m["m_imdb"] = _mk(db, lib, "movie", "只有IMDb", imdb_id="tt0000001")
    m["m_none"] = _mk(db, lib, "movie", "无身份")
    m["m_blank"] = _mk(db, lib, "movie", "空白TMDB", tmdb_id="   ")
    m["m_empty"] = _mk(db, lib, "movie", "空串TMDB", tmdb_id="")
    m["m_douban"] = _mk(db, lib, "movie", "只有豆瓣",
                        external_ids='{"douban": "1234567"}')
    m["s_bound"] = _mk(db, lib, "series", "已绑定剧", tmdb_id="999")
    m["s_unbound"] = _mk(db, lib, "series", "未绑定剧")
    m["season_bound"] = _mk(db, lib, "season", "季-已绑定剧",
                            series_id=m["s_bound"].id, season_number=1)
    m["season_unbound"] = _mk(db, lib, "season", "季-未绑定剧",
                              series_id=m["s_unbound"].id, season_number=1)
    m["ep_own"] = _mk(db, lib, "episode", "集-自身有号未绑定剧",
                      series_id=m["s_unbound"].id, tmdb_id="555",
                      season_number=1, episode_number=1)
    m["ep_follow"] = _mk(db, lib, "episode", "集-跟随已绑定剧",
                         series_id=m["s_bound"].id,
                         season_number=1, episode_number=1)
    m["ep_none"] = _mk(db, lib, "episode", "集-跟随未绑定剧",
                       series_id=m["s_unbound"].id,
                       season_number=1, episode_number=2)
    # series 已软删：它的集不能靠它「绑定」
    m["s_deleted"] = _mk(db, lib, "series", "已删剧", tmdb_id="777",
                         deleted_at=datetime(2026, 10, 24))
    m["ep_deleted_series"] = _mk(db, lib, "episode", "集-所属剧已删",
                                  series_id=m["s_deleted"].id,
                                  season_number=1, episode_number=1)
    m["person"] = _mk(db, lib, "person", "某演员")
    db.commit()
    return m


def _bound_ids(db):
    rows = (db.query(em.MediaItem.id)
            .filter(identity_gate.identity_clause()).all())
    return {r[0] for r in rows}


def test_gate_default_off():
    # env 未设时总开关默认关（关 = 全部出口旧口径，唯一的回滚手段）
    assert identity_gate.IDENTITY_SHELF_GATE == 0
    assert identity_gate.gate_enabled() is False


def test_movie_series_own_ids(db, scene):
    bound = _bound_ids(db)
    assert scene["m_tmdb"].id in bound
    assert scene["m_imdb"].id in bound, "只有 IMDb 也算已绑定"
    assert scene["s_bound"].id in bound
    assert scene["m_none"].id not in bound
    assert scene["s_unbound"].id not in bound


def test_blank_and_douban_only_not_bound(db, scene):
    bound = _bound_ids(db)
    assert scene["m_blank"].id not in bound, "纯空白 tmdb 不算"
    assert scene["m_empty"].id not in bound, "空串 tmdb 不算"
    assert scene["m_douban"].id not in bound, "只有豆瓣身份不算已绑定"


def test_children_follow_series(db, scene):
    bound = _bound_ids(db)
    assert scene["season_bound"].id in bound
    assert scene["season_unbound"].id not in bound
    assert scene["ep_follow"].id in bound, "集跟随已绑定剧"
    assert scene["ep_none"].id not in bound, "集跟随未绑定剧"
    assert scene["ep_own"].id in bound, "集自身有 tmdb 也算"


def test_deleted_series_does_not_bind_episode(db, scene):
    bound = _bound_ids(db)
    assert scene["ep_deleted_series"].id not in bound, \
        "已软删的 series 不能替集绑定（EXISTS 必须显式排除软删行）"


def test_other_types_not_governed(db, scene):
    bound = _bound_ids(db)
    assert scene["person"].id in bound, "person 等非四类不在货架治理内"
