"""季海报回退：季无图时门户 API 返回父剧集海报 URL

覆盖 ``backend.emby_server.api.season_poster_url``：
- 季有自身图 → 自身 poster_url（行为不变）
- 季无图、父剧集有图 → 父剧集的 poster_url
- 季无图、父剧集也无图 → None（前端保持文字占位）
- movie / series / episode 无图 → None（不误伤，只对 season 回退）
- 父条目被软删除 → 视为无父，返回 None
- parent_id 为空时用 series_id 兜底
"""
from __future__ import annotations

import os

os.environ.setdefault("DATABASE_TYPE", "sqlite")
os.environ.setdefault("REDIS_ENABLED", "false")

from datetime import datetime  # noqa: E402

from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

from backend.emby_server import api  # noqa: E402
from backend.emby_server import models as em  # noqa: E402


def _session():
    engine = create_engine("sqlite://")
    em.Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    s = Session()
    lib = em.Library(guid="lib", name="剧集", collection_type="tvshows", paths="/media/tv")
    s.add(lib)
    s.flush()

    def add(**kw):
        kw.setdefault("library_id", lib.id)
        kw.setdefault("date_added", datetime.now())
        it = em.MediaItem(**kw)
        s.add(it)
        s.flush()
        return it

    # 有图的剧
    series_ok = add(guid="series-ok", name="有图剧", item_type="series",
                    primary_image_url="http://img/series-ok.jpg")
    # 无图的剧
    series_bare = add(guid="series-bare", name="无图剧", item_type="series")
    # 被软删除的剧（有图但不可见）
    series_deleted = add(guid="series-del", name="已删剧", item_type="series",
                         primary_image_url="http://img/series-del.jpg",
                         deleted_at=datetime.now())

    # 季：有自身图
    s1 = add(guid="s1", name="第 1 季", item_type="season", season_number=1,
             parent_id=series_ok.id, series_id=series_ok.id,
             primary_image_url="http://img/s1.jpg")
    # 季：无图，父有图 → 应回退父
    s2 = add(guid="s2", name="第 2 季", item_type="season", season_number=2,
             parent_id=series_ok.id, series_id=series_ok.id)
    # 季：无图，父也无图 → None
    s3 = add(guid="s3", name="第 1 季", item_type="season", season_number=1,
             parent_id=series_bare.id, series_id=series_bare.id)
    # 季：无图，父被软删除 → None
    s4 = add(guid="s4", name="第 1 季", item_type="season", season_number=1,
             parent_id=series_deleted.id, series_id=series_deleted.id)
    # 季：无图，parent_id 为空，series_id 有图 → series_id 兜底
    s5 = add(guid="s5", name="第 3 季", item_type="season", season_number=3,
             series_id=series_ok.id)

    movie = add(guid="m1", name="无图电影", item_type="movie")
    ep = add(guid="e1", name="第 1 集", item_type="episode", season_number=1,
             episode_number=1, parent_id=s2.id, series_id=series_ok.id)
    s.commit()
    return s, dict(s1=s1, s2=s2, s3=s3, s4=s4, s5=s5, movie=movie, ep=ep,
                   series_ok=series_ok)


def test_season_with_own_image_returns_own_url():
    s, d = _session()
    assert api.season_poster_url(d["s1"], s) == "/emby/Items/s1/Images/Primary"


def test_season_without_image_falls_back_to_parent():
    s, d = _session()
    assert api.season_poster_url(d["s2"], s) == "/emby/Items/series-ok/Images/Primary"


def test_season_without_image_and_bare_parent_returns_none():
    s, d = _session()
    assert api.season_poster_url(d["s3"], s) is None


def test_season_with_soft_deleted_parent_returns_none():
    s, d = _session()
    assert api.season_poster_url(d["s4"], s) is None


def test_season_falls_back_via_series_id_when_no_parent_id():
    s, d = _session()
    assert api.season_poster_url(d["s5"], s) == "/emby/Items/series-ok/Images/Primary"


def test_movie_and_episode_never_fall_back():
    s, d = _session()
    assert api.season_poster_url(d["movie"], s) is None
    assert api.season_poster_url(d["ep"], s) is None
    # series 自身无图也不回退（没有更上层）
    bare = s.query(em.MediaItem).filter_by(guid="series-bare").one()
    assert api.season_poster_url(bare, s) is None
