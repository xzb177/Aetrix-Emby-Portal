"""302 Next A1：本地 TMDB 数据集。

不连生产库。覆盖：upsert 去重、本地搜索排序、库内混排与三态范围、
同步 stub（榜单页 + 类型名）、无 key 干净跳过、搜索零 TMDB 现调。
"""

import os

os.environ.setdefault("DATABASE_TYPE", "sqlite")
os.environ.setdefault("REDIS_ENABLED", "false")

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend import models
from backend.emby_server import models as em
from backend.emby_server import tmdb_dataset as dataset
from backend.emby_server.tmdb import TmdbClient


@pytest.fixture()
def engine():
    eng = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    models.Base.metadata.create_all(eng)
    yield eng
    eng.dispose()


@pytest.fixture()
def db(engine):
    session = sessionmaker(bind=engine)()
    try:
        yield session
    finally:
        session.close()


def _row(tmdb_id, title, media_type="movie", **kw):
    row = {"tmdb_id": tmdb_id, "media_type": media_type, "title": title,
           "original_title": kw.get("original_title", ""),
           "year": kw.get("year"), "overview": kw.get("overview", ""),
           "genres": kw.get("genres", []),
           "poster_path": kw.get("poster_path", ""),
           "vote_average": kw.get("vote_average"),
           "release_date": kw.get("release_date", ""),
           "source": kw.get("source", "popular")}
    return row


def _item(db, name, tmdb_id, library_id, item_type="movie"):
    import uuid
    item = em.MediaItem(name=name, item_type=item_type,
                        library_id=library_id, tmdb_id=tmdb_id,
                        guid="test-" + uuid.uuid4().hex)
    db.add(item)
    db.commit()
    db.refresh(item)
    return item


def test_upsert_dedupes_on_tmdb_and_type(db):
    ins, upd = dataset.upsert_items(db, [_row("550", "搏击俱乐部")])
    assert (ins, upd) == (1, 0)
    ins, upd = dataset.upsert_items(db, [_row("550", "搏击俱乐部·新标题")])
    assert (ins, upd) == (0, 1)
    ins, upd = dataset.upsert_items(db, [_row("550", "搏击俱乐部", "tv")])
    assert (ins, upd) == (1, 0)
    assert db.query(em.TmdbDatasetItem).count() == 2
    row = db.query(em.TmdbDatasetItem).filter(
        em.TmdbDatasetItem.tmdb_id == "550",
        em.TmdbDatasetItem.media_type == "movie").first()
    assert row.title == "搏击俱乐部·新标题"


def test_search_ranking_exact_prefix_contains(db):
    dataset.upsert_items(db, [
        _row("1", "星际穿越者", vote_average=9.0),
        _row("2", "星际穿越", vote_average=5.0),
        _row("3", "星际", vote_average=9.9),
    ])
    db.commit()
    res = dataset.search_local(db, "星际穿越", library_known=True)
    titles = [r["title"] for r in res["results"]]
    # 全等 > 前缀 > 包含；同档库内在前；再按评分。
    assert titles == ["星际穿越", "星际穿越者"]


def test_search_library_mix_and_scope(db):
    dataset.upsert_items(db, [_row("550", "搏击俱乐部"),
                              _row("551", "搏击俱乐部 2")])
    _item(db, "搏击俱乐部", "550", library_id=7)
    _item(db, "搏击俱乐部", "999", library_id=7)  # 数据集里没有的库内条目
    _item(db, "搏击俱乐部", "550", library_id=8)  # 另一库的同名条目
    db.commit()
    res = dataset.search_local(db, "搏击俱乐部",
                               allowed_library_ids={7}, library_known=True)
    by_tmdb = {r["tmdb_id"]: r for r in res["results"]}
    assert by_tmdb["550"]["in_library"] is True
    assert by_tmdb["550"]["library_known"] is True
    assert by_tmdb["551"]["in_library"] is False
    # 库内但不在数据集里：混排进来，且标库内。
    assert by_tmdb["999"]["in_library"] is True
    assert by_tmdb["999"]["dataset_only"] is False
    # 另一库的同 tmdb 不影响本库标记（550 在 7 库有行即标库内）。
    res_other = dataset.search_local(db, "搏击俱乐部",
                                     allowed_library_ids={9},
                                     library_known=True)
    by_other = {r["tmdb_id"]: r for r in res_other["results"]}
    assert by_other["550"]["in_library"] is False
    # 999 行属于 7 库：范围外连出现都不出现（不是标成库外）。
    assert "999" not in by_other


def test_search_scope_unknown_never_marks_outside(db):
    """范围读失败：数据集行照常回，但一律 library_known=False，不谎报库外。"""
    dataset.upsert_items(db, [_row("550", "搏击俱乐部")])
    _item(db, "搏击俱乐部", "550", library_id=7)
    db.commit()
    res = dataset.search_local(db, "搏击俱乐部", library_known=False)
    assert len(res["results"]) == 1
    assert res["results"][0]["library_known"] is False
    assert res["results"][0]["in_library"] is False


def test_search_never_calls_tmdb(db, monkeypatch):
    """搜索路径零 TMDB 现调：把榜单方法换成必炸的桩，搜索照常工作。"""
    dataset.upsert_items(db, [_row("550", "搏击俱乐部")])
    db.commit()

    def _boom(*args, **kwargs):
        raise AssertionError("搜索路径不许调 TMDB")

    monkeypatch.setattr(TmdbClient, "list_page", _boom)
    monkeypatch.setattr(TmdbClient, "genre_names", _boom)
    res = dataset.search_local(db, "搏击俱乐部", library_known=True)
    assert [r["title"] for r in res["results"]] == ["搏击俱乐部"]


class _StubClient:
    """榜单桩：trending/popular/top_rated 各回一页，含 genre_ids。"""

    key_source = "env"
    api_keys = ["stub"]

    def list_page(self, list_name, media_type, page=1):
        if page > 1:
            return []
        return [{"id": 550, "title": "搏击俱乐部",
                 "release_date": "1999-10-15", "genre_ids": [18, 53],
                 "overview": "失眠症患者", "vote_average": 8.4,
                 "poster_path": "/p.jpg"}]

    def genre_names(self, media_type):
        return {18: "剧情", 53: "惊悚"}


def test_sync_once_upserts_and_freshness(db):
    result = dataset.sync_once(db, client=_StubClient(), pages_per_list=1)
    assert result["status"] == "ok"
    assert result["fetched"] == 6  # 3 榜单 × 2 类型
    # 桩每榜都回同一 tmdb_id：按 (tmdb_id, media_type) 去重后只剩 2 行。
    assert result["inserted"] == 2
    assert result["updated"] == 4
    row = db.query(em.TmdbDatasetItem).filter(
        em.TmdbDatasetItem.tmdb_id == "550",
        em.TmdbDatasetItem.media_type == "movie").first()
    assert row is not None
    assert row.genres_json == '["剧情", "惊悚"]'
    assert row.year == 1999
    fresh = dataset.get_freshness(db)
    assert fresh["synced"] is True
    assert fresh["total_rows"] == 2
    assert fresh["last_error"] is None
    # 再跑一轮：同一键 upsert，不翻倍。
    result2 = dataset.sync_once(db, client=_StubClient(), pages_per_list=1)
    assert result2["inserted"] == 0
    assert result2["updated"] == 6


def test_sync_skips_cleanly_without_key(db):
    class _NoKeyClient:
        key_source = "none"
        api_keys = []

        def list_page(self, *args, **kwargs):
            raise AssertionError("无 key 时不许发请求")

    result = dataset.sync_once(db, client=_NoKeyClient())
    assert result["status"] == "skipped"
    assert result["reason"] == "no_tmdb_key"
    assert db.query(em.TmdbDatasetItem).count() == 0
    fresh = dataset.get_freshness(db)
    assert fresh["synced"] is False
    assert "no_tmdb_key" in (fresh["last_error"] or "")


def test_sync_partial_failure_does_not_kill_round(db):
    class _FlakyClient(_StubClient):
        def list_page(self, list_name, media_type, page=1):
            if list_name == "popular":
                raise RuntimeError("上游抖了一下")
            return super().list_page(list_name, media_type, page)

    result = dataset.sync_once(db, client=_FlakyClient(), pages_per_list=1)
    assert result["status"] == "partial"
    assert len(result["errors"]) == 2  # popular × movie/tv
    # 剩下 4 个组合仍按 (tmdb_id, media_type) 去重成 2 行。
    assert db.query(em.TmdbDatasetItem).count() == 2
    fresh = dataset.get_freshness(db)
    assert fresh["synced"] is True  # 至少一页成功即算本轮成功
    assert fresh["last_error"]


def test_library_mixin_respects_media_type(db):
    """库内混排受 type 约束：movie 不混剧集、tv 不混电影，季/集行永不混排。

    回归：混排查询之前没按 media_type 过滤，type=movie 也会返回剧集行，
    季/集行还会被标成 movie（media_type 映射只有 series→tv、其余→movie）。
    """
    _item(db, "搏击俱乐部", "999", library_id=7, item_type="series")
    _item(db, "搏击俱乐部", "998", library_id=7, item_type="episode")
    _item(db, "搏击俱乐部", "997", library_id=7, item_type="movie")
    db.commit()
    by_movie = {r["tmdb_id"]: r for r in
                dataset.search_local(db, "搏击俱乐部", media_type="movie",
                                     library_known=True)["results"]}
    assert set(by_movie) == {"997"}
    assert by_movie["997"]["media_type"] == "movie"
    by_tv = {r["tmdb_id"]: r for r in
             dataset.search_local(db, "搏击俱乐部", media_type="tv",
                                  library_known=True)["results"]}
    assert set(by_tv) == {"999"}
    assert by_tv["999"]["media_type"] == "tv"
    by_all = {r["tmdb_id"]: r for r in
              dataset.search_local(db, "搏击俱乐部", media_type=None,
                                   library_known=True)["results"]}
    assert set(by_all) == {"997", "999"}
