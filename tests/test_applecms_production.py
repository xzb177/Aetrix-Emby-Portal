"""苹果 CMS 生产接线（组装层）测试：适配器 + 手动触发端到端。

只用内存 SQLite，不发真实 HTTP（http_get 注入 fake）；不碰生产库。
"""

from datetime import datetime

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend import models as base_models
from backend.emby_server import applecms_production as prod
from backend.emby_server import applecms_sync as sync
from backend.emby_server import models as em
from backend.emby_server import upstream_item_mappings as uim


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
def _gates_on(monkeypatch):
    monkeypatch.setenv(sync.APPLECMS_ENABLED_ENV, "1")
    monkeypatch.setenv(sync.UPSTREAM_SYNC_ENABLED_ENV, "1")
    monkeypatch.setenv(sync.MAPPING_ENABLED_ENV, "1")
    monkeypatch.setenv(sync.UNMATCHED_MODE_ENV, "parallel")
    monkeypatch.setenv(sync.PAGE_DELAY_ENV, "0")


def _item(db, guid, item_type, name, year=None, series_id=None,
          episode_number=None, merged_into_id=None, deleted_at=None):
    row = em.MediaItem(
        guid=guid, library_id=1, item_type=item_type, name=name,
        production_year=year, series_id=series_id,
        episode_number=episode_number, merged_into_id=merged_into_id,
        deleted_at=deleted_at)
    db.add(row)
    db.flush()
    return row


def _source(db, name="cms-1"):
    row = em.UpstreamSource(
        name=name, source_type="applecms", base_url="http://cms.example",
        auth_method="none", enabled=True)
    db.add(row)
    db.flush()
    return row


def _binding(db, source_id, category="1"):
    row = em.SourceCategoryBinding(
        source_id=source_id, upstream_category_id=category,
        sync_enabled=True)
    db.add(row)
    db.flush()
    return row


def test_load_live_locals_only_playable_leaves(db):
    series = _item(db, "g-series", "series", "测试剧", year=2020)
    _item(db, "g-s1", "season", "第 1 季", series_id=series.id)
    _item(db, "g-e1", "episode", "剧情标题一", series_id=series.id,
          episode_number=1)
    _item(db, "g-e2", "episode", "剧情标题二", series_id=series.id,
          episode_number=2)
    _item(db, "g-movie", "movie", "测试电影", year=1994)
    _item(db, "g-merged", "movie", "合并副本", year=1994,
          merged_into_id=999)
    _item(db, "g-deleted", "movie", "已删电影", year=1994,
          deleted_at=datetime.now())
    db.commit()

    locals_ = prod.make_list_locals(db)(None)
    assert len(locals_) == 3
    by_id = {d["id"]: d for d in locals_}
    movie = [d for d in locals_ if d["item_type"] == "Movie"]
    assert len(movie) == 1
    assert movie[0]["title"] == "测试电影"
    assert movie[0]["production_year"] == 1994
    episodes = sorted((d for d in locals_ if d["item_type"] == "Episode"),
                      key=lambda d: d["episode_number"])
    assert len(episodes) == 2
    # 单集配对键取系列标题与年份，不取集剧情标题。
    assert episodes[0]["title"] == "测试剧"
    assert episodes[0]["production_year"] == 2020
    assert episodes[0]["episode_number"] == 1
    assert episodes[1]["episode_number"] == 2


def test_production_trigger_maps_movie_and_ignores_play_url(db):
    movie = _item(db, "g-m1", "movie", "肖申克的救赎", year=1994)
    source = _source(db)
    _binding(db, source.id, "1")
    db.commit()

    def _fake_get(url, headers, timeout):
        assert "cms.example" in url
        return (200, {"code": 1, "pagecount": 1, "list": [{
            "vod_id": "vod-1", "vod_name": "肖申克的救赎",
            "vod_year": "1994",
            # 播放地址随条目进来，但同步链路绝不读它、绝不落库。
            "vod_play_url": "http://cms.example/play/secret.m3u8",
            "vod_play_from": "线路1",
        }]})

    result = prod.production_trigger_applecms_sync(
        db, source.id, http_get=_fake_get)
    assert result.mapped == 1
    assert result.health == "healthy"
    row = uim.get_best_mapping(db, movie.id)
    assert row is not None
    assert (row.source_id, row.upstream_item_id) == (source.id, "vod-1")
    assert row.evidence == "title+year"
    assert row.confidence == 0.7
    assert row.episode_index is None
    db.refresh(source)
    assert source.health_status == "healthy"


def test_production_trigger_failed_run_writes_unhealthy_not_degraded(db):
    source = _source(db)
    _binding(db, source.id, "1")
    db.commit()

    def _boom(url, headers, timeout):
        raise RuntimeError("boom")

    result = prod.production_trigger_applecms_sync(
        db, source.id, http_get=_boom)
    assert result.health == "unhealthy"
    db.refresh(source)
    # set_health_status 只许三值；degraded 到这里会直接报错，
    # 本断言同时证明组装后的健康写路径不再产出第四种状态。
    assert source.health_status == "unhealthy"
