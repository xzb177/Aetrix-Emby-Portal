# -*- coding: utf-8 -*-
"""苹果 CMS 集行结构回归（新媒体库类型一期·集行单，只预留结构）。

口径（2026-11-02 定案）：
- 一期只灌电影，剧集库不开、``sync_library`` 对 tvshows 库仍
  blocked；本单只把「剧行＋合成单季行＋集行＋集映射」的构建函数
  与行形约定先钉死，二期放行时直接接线，不许到时另起一套形状。
- CMS 无季概念：一部剧合成一张季行（第 1 季），多季混排是已知
  局限（设计稿明示）；集序号 = 直链组内 1 起序号，与 mapped
  取流口的 episode_index 同一口径。
- 集标题入库、播放地址绝不落库：detail 只用来数集与取集名，
  ``vod_play_url`` 的地址不写进任何行字段。
- 集映射 episode_index 非空，与电影映射（NULL）分开。
"""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend import models as base_models
from backend.emby_server import applecms_library_sync as als
from backend.emby_server import applecms_series as acser
from backend.emby_server import applecms_sync as acs
from backend.emby_server import models as em


DETAIL = {
    "vod_id": 202, "vod_name": "庆余年", "vod_year": "2019",
    "vod_play_from": "线路A$$$线路B",
    "vod_play_url": (
        "第1集$https://cdn.example.com/qyn/e1.m3u8#"
        "第2集$https://cdn.example.com/qyn/e2.m3u8#"
        "$https://cdn.example.com/qyn/e3.m3u8"
        "$$$https://parse.example.com/play?url=xyz"),
}


@pytest.fixture()
def db(monkeypatch):
    monkeypatch.setenv("UPSTREAM_ITEM_MAPPING_ENABLED", "1")
    engine = create_engine("sqlite:///:memory:")
    base_models.Base.metadata.create_all(engine)
    em.Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    try:
        yield session
    finally:
        session.close()


def _seed(db):
    db.add(em.UpstreamSource(
        id=7, name="非凡", source_type="applecms",
        base_url="http://s7.example", enabled=True))
    lib = em.Library(
        guid="lib-cms-tv", name="CMS剧集", collection_type="tvshows",
        is_enabled=True, kind="applecms", upstream_source_ids="7")
    db.add(lib)
    db.commit()
    return lib


def test_plan_episodes_uses_direct_group_titles():
    plan = acser.plan_episodes(DETAIL)
    assert [p["index"] for p in plan] == [1, 2, 3]
    assert plan[0]["title"] == "第1集"
    assert plan[2]["title"] == "第3集"  # 空标题回落「第N集」


def test_plan_episodes_no_direct_group_is_empty():
    detail = {"vod_play_from": "线路B",
              "vod_play_url": "第1集$https://parse.example.com/x"}
    assert acser.plan_episodes(detail) == []


def test_ensure_series_tree_shape_and_no_address_stored(db):
    lib = _seed(db)
    source = db.get(em.UpstreamSource, 7)
    record = acs.cms_entry_to_record(
        {"vod_id": 202, "vod_name": "庆余年", "vod_year": "2019"},
        source_id="7")
    stats = acser.ensure_series_tree(db, lib, source, record, DETAIL)
    db.commit()
    assert stats == {"series_created": 1, "season_created": 1,
                     "episodes_added": 3, "episodes_existing": 0,
                     "mapped": 3}
    series = (db.query(em.MediaItem)
              .filter(em.MediaItem.item_type == "series").one())
    assert series.upstream_source_id == 7
    assert series.enrich_status == "pending"
    season = (db.query(em.MediaItem)
              .filter(em.MediaItem.item_type == "season").one())
    assert season.series_id == series.id and season.parent_id == series.id
    assert season.season_number == 1 and season.name == "第 1 季"
    episodes = (db.query(em.MediaItem)
                .filter(em.MediaItem.item_type == "episode")
                .order_by(em.MediaItem.episode_number).all())
    assert [e.name for e in episodes] == ["第1集", "第2集", "第3集"]
    assert all(e.series_id == series.id and e.parent_id == season.id
               and e.season_number == 1 for e in episodes)
    # 播放地址绝不落库：任何行字段不许出现 detail 里的地址
    for row in [series, season] + episodes:
        blob = " ".join(str(v) for v in vars(row).values()
                        if isinstance(v, str))
        assert "cdn.example.com" not in blob
        assert "vod_play_url" not in blob
    maps = (db.query(em.UpstreamItemMapping)
            .filter(em.UpstreamItemMapping.source_id == 7)
            .order_by(em.UpstreamItemMapping.episode_index).all())
    assert [(m.upstream_item_id, m.episode_index) for m in maps] == [
        ("202", 1), ("202", 2), ("202", 3)]
    assert all(m.local_item_id in {e.id for e in episodes} for m in maps)


def test_ensure_series_tree_idempotent(db):
    lib = _seed(db)
    source = db.get(em.UpstreamSource, 7)
    record = acs.cms_entry_to_record(
        {"vod_id": 202, "vod_name": "庆余年", "vod_year": "2019"},
        source_id="7")
    acser.ensure_series_tree(db, lib, source, record, DETAIL)
    db.commit()
    stats2 = acser.ensure_series_tree(db, lib, source, record, DETAIL)
    db.commit()
    assert stats2["series_created"] == 0
    assert stats2["episodes_added"] == 0
    assert stats2["episodes_existing"] == 3
    assert db.query(em.MediaItem).count() == 5  # 剧+季+3集


def test_tvshows_library_still_blocked_in_phase1(db):
    lib = _seed(db)
    stats = als.sync_library(db, lib, http_get=None)
    assert stats["blocked"] == "unsupported_collection"
    assert db.query(em.MediaItem).count() == 0
