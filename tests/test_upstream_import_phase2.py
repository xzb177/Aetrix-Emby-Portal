"""上游独有资源进库展示 · 二期（剧全量＋生命周期＋自动建库）回归。

锁住的口径（方案 reports/upstream-only-import-proposal-20261101.md
第五节二期，用户 2026-11-01 批准，任务书 2026-11-01 10:57 下达）：

- 独有剧整棵进库：剧/季/集三级行（托管标记 upstream_source_id、
  guid 确定性派生、身份落 tmdb_id），集靠上游 SeriesId 回查已建
  剧行（跨窗可达）；已配对本地剧的缺集补进库（托管单集挂本地剧
  行下，季行复用或新建）；36 部无身份剧按拍板 7 不进（计数）；
- 生命周期对账：整轮可信（无失败/跳过库、开关开）才对账——
  连续 2 轮不见隐藏、再 2 轮不见软删；重现清零恢复；剧隐藏/删除
  级联其托管季/集；单集单独消失只动单集；
- 自动建库：上游分类在本地无同名真实库时自动新建同名真实库
  （非虚拟），落「上游库 id ↔ 本地库 id」对应关系，后续按关系
  找库（管理员改名不重复建）；建库只在带上游库 id 的调用下发生；
- 按源开关 import_enabled 关→剧/季/集托管行全隐藏，开→恢复
  （一期 _set_import_visibility 按 upstream_source_id 全量翻转，
  二期行同口径纳入）；
- 合并让位：托管剧与本地剧合并时集跟随（同集合并＋映射迁移、
  孤儿集 reparent 到存活剧），不许双行并存；
- 播放：托管单集详情有上游线路、PlaybackInfo 只回上游一条源；
  托管剧 PlaybackInfo（IsPlayback=true）落到托管集出上游源。

DB 纯逻辑用内存 SQLite（照一期形态）；播放面用真 TestClient。
"""

from __future__ import annotations

import hashlib
import os

os.environ.setdefault("DATABASE_TYPE", "sqlite")
os.environ.setdefault("REDIS_ENABLED", "false")

import re
import uuid
from types import SimpleNamespace
from unittest import mock

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import backend.models as models
from backend.emby_server import models as em
from backend.emby_server import upstream_aggregation as agg
from backend.emby_server import upstream_sources as ups


# ---------------------------------------------------------------------------
# 内存 DB 夹具（与一期同形态）
# ---------------------------------------------------------------------------

@pytest.fixture()
def db_factory():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False},
        poolclass=StaticPool)
    models.Base.metadata.create_all(engine)
    em.Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)


@pytest.fixture()
def db(db_factory):
    session = db_factory()
    yield session
    session.close()


def _source(db, *, name="src-import2", enabled=True, import_enabled=True):
    row = em.UpstreamSource(
        name=name, source_type="emby",
        base_url="https://up.example.com/emby", auth_method="none",
        enabled=enabled, credential_scope="source",
        health_status="unknown", import_enabled=import_enabled)
    db.add(row)
    db.flush()
    return row


def _library(db, *, name, is_virtual=False, collection_type="tvshows"):
    row = em.Library(guid=uuid.uuid4().hex, name=name,
                     collection_type=collection_type, is_enabled=True,
                     is_virtual=is_virtual)
    db.add(row)
    db.flush()
    return row


def _line(source_id, upstream_id, episode_index=None):
    return agg.UpstreamLine(
        line_index=0, line_key=f"{source_id}:{upstream_id}",
        source_id=str(source_id), upstream_item_id=upstream_id,
        episode_index=episode_index)


def _agg_series(source_id, upstream_id, name, tmdb, year=2022):
    return agg.UpstreamAggregateItem(
        aggregate_key=(f"tmdb:{tmdb}" if tmdb else f"title:{name}:{year}"),
        name=name, item_type="Series", production_year=year,
        tmdb_id=tmdb, lines=[_line(source_id, upstream_id)])


def _agg_episode(source_id, upstream_id, series_name, season, episode,
                 ep_name, tmdb=None, series_upstream_id=""):
    return agg.UpstreamAggregateItem(
        aggregate_key=f"title:{series_name}::s{season}:e{episode}",
        name=series_name, item_type="Episode", tmdb_id=tmdb,
        season_number=season, series_name=series_name,
        series_upstream_id=series_upstream_id, episode_name=ep_name,
        lines=[_line(source_id, upstream_id, episode_index=episode)])


def _hosted_guid(source_id, upstream_item_id):
    return hashlib.md5(
        f"upstream:{source_id}:{upstream_item_id}".encode()).hexdigest()


@pytest.fixture(autouse=True)
def _mapping_on(monkeypatch):
    monkeypatch.setenv("UPSTREAM_ITEM_MAPPING_ENABLED", "1")


def _row(db, guid):
    return db.query(em.MediaItem).filter(em.MediaItem.guid == guid).one()


# ---------------------------------------------------------------------------
# 剧整棵进库
# ---------------------------------------------------------------------------

def _tree_items(src_id):
    return [
        _agg_series(src_id, "up-ser-1", "独有剧甲", "960001"),
        _agg_episode(src_id, "up-ep-1", "独有剧甲", 1, 1, "第一集",
                     series_upstream_id="up-ser-1"),
        _agg_episode(src_id, "up-ep-2", "独有剧甲", 1, 2, "第二集",
                     series_upstream_id="up-ser-1"),
        _agg_episode(src_id, "up-ep-3", "独有剧甲", 2, 1, "归来",
                     series_upstream_id="up-ser-1"),
    ]


def test_series_tree_import_builds_full_tree(db):
    from backend.emby_server import upstream_import as uimp
    src = _source(db)
    lib = _library(db, name="国产剧")
    stats = uimp.import_unpaired_series_tree(
        db, src, _tree_items(src.id), [],
        upstream_library_name="国产剧")
    db.commit()
    assert stats["blocked"] is None
    assert stats["series_imported"] == 1
    assert stats["episodes_imported"] == 3
    assert stats["seasons_created"] == 2
    assert stats["imported"] == 6  # 剧 1 + 季 2 + 集 3

    series = _row(db, _hosted_guid(src.id, "up-ser-1"))
    assert series.library_id == lib.id
    assert series.item_type == "series"
    assert series.tmdb_id == "960001"
    assert series.upstream_source_id == src.id
    assert series.file_path is None
    assert series.probe_status == "done"

    seasons = db.query(em.MediaItem).filter(
        em.MediaItem.item_type == "season",
        em.MediaItem.series_id == series.id).order_by(
            em.MediaItem.season_number).all()
    assert [s.season_number for s in seasons] == [1, 2]
    for season in seasons:
        assert season.parent_id == series.id
        assert season.upstream_source_id == src.id
        assert season.name == f"第 {season.season_number} 季"

    ep1 = _row(db, _hosted_guid(src.id, "up-ep-1"))
    assert ep1.item_type == "episode"
    assert ep1.series_id == series.id
    assert ep1.parent_id == seasons[0].id
    assert ep1.season_number == 1 and ep1.episode_number == 1
    assert ep1.name == "第一集"  # 单集本名，不是剧名
    assert ep1.library_id == lib.id
    assert ep1.upstream_source_id == src.id
    ep3 = _row(db, _hosted_guid(src.id, "up-ep-3"))
    assert ep3.parent_id == seasons[1].id

    # 映射：剧一条、集三条（集带 episode_index）
    mappings = db.query(em.UpstreamItemMapping).filter(
        em.UpstreamItemMapping.source_id == src.id).all()
    by_up = {m.upstream_item_id: m for m in mappings}
    assert set(by_up) == {"up-ser-1", "up-ep-1", "up-ep-2", "up-ep-3"}
    assert by_up["up-ser-1"].local_item_id == series.id
    assert by_up["up-ep-1"].local_item_id == ep1.id
    assert by_up["up-ep-1"].episode_index == 1
    assert by_up["up-ep-3"].episode_index == 1


def test_series_import_idempotent_second_run(db):
    from backend.emby_server import upstream_import as uimp
    src = _source(db)
    _library(db, name="国产剧")
    first = uimp.import_unpaired_series_tree(
        db, src, _tree_items(src.id), [], upstream_library_name="国产剧")
    db.commit()
    second = uimp.import_unpaired_series_tree(
        db, src, _tree_items(src.id), [], upstream_library_name="国产剧")
    db.commit()
    assert first["imported"] == 6
    assert second["imported"] == 0
    assert second["existing"] == 4  # 剧 1 + 集 3（季按 (剧,季号) 复用）
    assert db.query(em.MediaItem).count() == 6
    assert db.query(em.UpstreamItemMapping).count() == 4


def test_episodes_resolve_series_across_windows(db):
    """跨窗：第二窗只有单集（无剧聚合），靠 SeriesId 回查已建剧行。"""
    from backend.emby_server import upstream_import as uimp
    src = _source(db)
    _library(db, name="国产剧")
    uimp.import_unpaired_series_tree(
        db, src, [_agg_series(src.id, "up-ser-1", "独有剧甲", "960001")],
        [], upstream_library_name="国产剧")
    db.commit()
    stats = uimp.import_unpaired_series_tree(
        db, src, [_agg_episode(src.id, "up-ep-9", "独有剧甲", 1, 9,
                               "第九集", series_upstream_id="up-ser-1")],
        [], upstream_library_name="国产剧")
    db.commit()
    assert stats["episodes_imported"] == 1
    series = _row(db, _hosted_guid(src.id, "up-ser-1"))
    ep = _row(db, _hosted_guid(src.id, "up-ep-9"))
    assert ep.series_id == series.id


def test_no_identity_series_skipped_and_counted(db):
    """36 部无身份剧口径：不进库、单独计数；其单集无处可挂一并跳过。"""
    from backend.emby_server import upstream_import as uimp
    src = _source(db)
    _library(db, name="国产剧")
    items = [
        _agg_series(src.id, "up-ser-x", "无身份剧", None),
        _agg_episode(src.id, "up-ep-x", "无身份剧", 1, 1, "第一集",
                     series_upstream_id="up-ser-x"),
    ]
    stats = uimp.import_unpaired_series_tree(
        db, src, items, [], upstream_library_name="国产剧")
    db.commit()
    assert stats["no_identity"] == 1
    assert stats["no_series"] == 1
    assert stats["imported"] == 0
    assert db.query(em.MediaItem).count() == 0


def test_episode_without_position_skipped(db):
    from backend.emby_server import upstream_import as uimp
    src = _source(db)
    _library(db, name="国产剧")
    uimp.import_unpaired_series_tree(
        db, src, [_agg_series(src.id, "up-ser-1", "独有剧甲", "960001")],
        [], upstream_library_name="国产剧")
    db.commit()
    orphan = _agg_episode(src.id, "up-ep-np", "独有剧甲", None, None,
                          "无号集", series_upstream_id="up-ser-1")
    stats = uimp.import_unpaired_series_tree(
        db, src, [orphan], [], upstream_library_name="国产剧")
    db.commit()
    assert stats["no_position"] == 1
    assert stats["imported"] == 0


# ---------------------------------------------------------------------------
# 补集：已配对本地剧挂托管单集
# ---------------------------------------------------------------------------

def test_episode_fills_into_paired_local_series(db):
    from backend.emby_server import upstream_import as uimp
    src = _source(db)
    lib = _library(db, name="国产剧")
    local_series = em.MediaItem(
        guid="guid-local-series", library_id=lib.id, item_type="series",
        name="本地剧乙", tmdb_id="960002", file_path=None,
        probe_status="done")
    db.add(local_series)
    db.flush()
    local_ep1 = em.MediaItem(
        guid="guid-local-ep1", library_id=lib.id, item_type="episode",
        name="本地第一集", series_id=local_series.id, season_number=1,
        episode_number=1, file_path="mount://3/乙/e1.mp4",
        probe_status="done")
    db.add(local_ep1)
    db.flush()
    pairs = [
        {"local_item_id": local_series.id, "upstream_item_id": "up-ser-2",
         "item_type": "Series", "confidence": 1.0, "evidence": "tmdb",
         "episode_index": None},
        {"local_item_id": local_ep1.id, "upstream_item_id": "up-ep2-1",
         "item_type": "Episode", "confidence": 1.0, "evidence": "tmdb",
         "episode_index": 1},
    ]
    items = [
        _agg_series(src.id, "up-ser-2", "本地剧乙", "960002"),
        _agg_episode(src.id, "up-ep2-1", "本地剧乙", 1, 1, "第一集",
                     series_upstream_id="up-ser-2"),
        _agg_episode(src.id, "up-ep2-2", "本地剧乙", 1, 2, "缺集第二集",
                     series_upstream_id="up-ser-2"),
    ]
    stats = uimp.import_unpaired_series_tree(
        db, src, items, pairs, upstream_library_name="国产剧")
    db.commit()
    # 剧已配对不重建；已配对的集不重建；只补缺的第二集
    assert stats["series_imported"] == 0
    assert stats["episodes_imported"] == 1
    ep2 = _row(db, _hosted_guid(src.id, "up-ep2-2"))
    assert ep2.series_id == local_series.id
    assert ep2.upstream_source_id == src.id  # 托管集（本地剧行不是托管）
    assert local_series.upstream_source_id is None
    season = db.query(em.MediaItem).filter(
        em.MediaItem.item_type == "season",
        em.MediaItem.series_id == local_series.id).one()
    assert ep2.parent_id == season.id
    mapping = db.query(em.UpstreamItemMapping).filter(
        em.UpstreamItemMapping.local_item_id == ep2.id).one()
    assert mapping.upstream_item_id == "up-ep2-2"
    assert mapping.episode_index == 2


# ---------------------------------------------------------------------------
# 自动建库
# ---------------------------------------------------------------------------

def test_auto_create_library_and_link_reuse_after_rename(db):
    from backend.emby_server import upstream_import as uimp
    src = _source(db)
    items = [_agg_series(src.id, "up-ser-1", "独有剧甲", "960001")]
    stats = uimp.import_unpaired_series_tree(
        db, src, items, [], upstream_library_name="独有分类库",
        upstream_library_id="uplib-9", upstream_collection_type="tvshows")
    db.commit()
    assert stats["libraries_created"] == 1
    assert stats["imported"] == 1
    lib = db.query(em.Library).filter(
        em.Library.name == "独有分类库").one()
    assert not lib.is_virtual  # 真实库，不是虚拟库
    assert lib.collection_type == "tvshows"
    link = db.query(em.UpstreamLibraryLink).filter(
        em.UpstreamLibraryLink.source_id == src.id,
        em.UpstreamLibraryLink.upstream_library_id == "uplib-9").one()
    assert link.local_library_id == lib.id

    # 管理员改名后按关系找库：不重复建库，行仍落这个库
    lib.name = "改名后的库"
    db.commit()
    stats2 = uimp.import_unpaired_series_tree(
        db, src, [_agg_series(src.id, "up-ser-2", "独有剧丙", "960003")],
        [], upstream_library_name="独有分类库",
        upstream_library_id="uplib-9", upstream_collection_type="tvshows")
    db.commit()
    assert stats2["libraries_created"] == 0
    assert db.query(em.Library).count() == 1
    row2 = _row(db, _hosted_guid(src.id, "up-ser-2"))
    assert row2.library_id == lib.id

    # 幂等：整轮重跑不建第二个库、不多一条关系
    uimp.import_unpaired_series_tree(
        db, src, items, [], upstream_library_name="独有分类库",
        upstream_library_id="uplib-9", upstream_collection_type="tvshows")
    db.commit()
    assert db.query(em.Library).count() == 1
    assert db.query(em.UpstreamLibraryLink).count() == 1


def test_no_auto_create_without_upstream_library_id(db):
    """一期兼容：不带上游库 id 的调用无同名库仍只跳过计数，不建库。"""
    from backend.emby_server import upstream_import as uimp
    src = _source(db)
    stats = uimp.import_unpaired_series_tree(
        db, src, [_agg_series(src.id, "up-ser-1", "独有剧甲", "960001")],
        [], upstream_library_name="不存在的库")
    db.commit()
    assert stats["no_library"] == 1
    assert stats["imported"] == 0
    assert db.query(em.Library).count() == 0


# ---------------------------------------------------------------------------
# 生命周期对账
# ---------------------------------------------------------------------------

def _import_tree(db, src):
    from backend.emby_server import upstream_import as uimp
    _library(db, name="国产剧")
    uimp.import_unpaired_series_tree(
        db, src, _tree_items(src.id), [], upstream_library_name="国产剧")
    uimp.import_unpaired_movies(
        db, src, [], set(), upstream_library_name="国产剧")
    db.commit()


def test_lifecycle_hide_then_delete_and_restore(db):
    from backend.emby_server import upstream_import as uimp
    src = _source(db)
    _import_tree(db, src)
    all_ids = {"up-ser-1", "up-ep-1", "up-ep-2", "up-ep-3"}

    # 第 1 轮不见：只计数，不隐藏
    s = uimp.reconcile_hosted_lifecycle(db, src, set())
    db.commit()
    assert s["hidden"] == 0 and s["deleted"] == 0
    series = _row(db, _hosted_guid(src.id, "up-ser-1"))
    assert series.is_hidden in (False, None)

    # 第 2 轮不见：剧隐藏，级联季/集
    s = uimp.reconcile_hosted_lifecycle(db, src, set())
    db.commit()
    assert s["hidden"] >= 1
    db.expire_all()
    series = _row(db, _hosted_guid(src.id, "up-ser-1"))
    assert series.is_hidden is True
    ep1 = _row(db, _hosted_guid(src.id, "up-ep-1"))
    assert ep1.is_hidden is True

    # 重现：清零 + 恢复可见（剧与集）
    s = uimp.reconcile_hosted_lifecycle(db, src, all_ids)
    db.commit()
    assert s["restored"] >= 1
    db.expire_all()
    series = _row(db, _hosted_guid(src.id, "up-ser-1"))
    assert series.is_hidden is False
    ep1 = _row(db, _hosted_guid(src.id, "up-ep-1"))
    assert ep1.is_hidden is False

    # 连续 4 轮不见：第 4 轮软删（行带 deleted_at，常规查询不可见）
    for _ in range(3):
        uimp.reconcile_hosted_lifecycle(db, src, set())
        db.commit()
    s = uimp.reconcile_hosted_lifecycle(db, src, set())
    db.commit()
    assert s["deleted"] >= 1
    assert db.query(em.MediaItem).filter(
        em.MediaItem.guid == _hosted_guid(src.id, "up-ser-1")).first() is None
    from backend.emby_server import soft_delete
    with soft_delete.include_deleted():
        dead = db.query(em.MediaItem).filter(
            em.MediaItem.guid == _hosted_guid(src.id, "up-ser-1")).one()
    assert dead.deleted_at is not None

    # 软删后上游重现：进库复活同一行（不撞 guid 唯一键、不建双行）
    stats = uimp.import_unpaired_series_tree(
        db, src, _tree_items(src.id), [], upstream_library_name="国产剧")
    db.commit()
    assert db.query(em.MediaItem).filter(
        em.MediaItem.guid == _hosted_guid(src.id, "up-ser-1")).count() == 1
    series = _row(db, _hosted_guid(src.id, "up-ser-1"))
    assert series.deleted_at is None


def test_lifecycle_single_episode_missing_only_hides_episode(db):
    from backend.emby_server import upstream_import as uimp
    src = _source(db)
    _import_tree(db, src)
    seen = {"up-ser-1", "up-ep-1", "up-ep-3"}  # 只缺 up-ep-2
    uimp.reconcile_hosted_lifecycle(db, src, seen)
    db.commit()
    uimp.reconcile_hosted_lifecycle(db, src, seen)
    db.commit()
    db.expire_all()
    ep2 = _row(db, _hosted_guid(src.id, "up-ep-2"))
    assert ep2.is_hidden is True
    series = _row(db, _hosted_guid(src.id, "up-ser-1"))
    assert series.is_hidden in (False, None)  # 剧还在，不许跟着藏
    ep1 = _row(db, _hosted_guid(src.id, "up-ep-1"))
    assert ep1.is_hidden in (False, None)


def test_import_switch_hides_and_restores_series_tree(db):
    src = _source(db)
    _import_tree(db, src)
    ups.update_source(db, src, import_enabled=False)
    db.commit()
    db.expire_all()
    rows = db.query(em.MediaItem).filter(
        em.MediaItem.upstream_source_id == src.id).all()
    assert rows and all(r.is_hidden is True for r in rows)
    ups.update_source(db, src, import_enabled=True)
    db.commit()
    db.expire_all()
    rows = db.query(em.MediaItem).filter(
        em.MediaItem.upstream_source_id == src.id).all()
    assert rows and all(r.is_hidden is False for r in rows)


# ---------------------------------------------------------------------------
# 合并让位
# ---------------------------------------------------------------------------

def test_series_merge_follows_episodes_and_mappings(db):
    from backend.emby_server import merge_versions_worker as mvw
    from backend.emby_server import upstream_import as uimp
    src = _source(db)
    lib = _library(db, name="国产剧")
    # 本地剧：有海报（选主胜出）+ 本地第 1 集（有文件）
    local_series = em.MediaItem(
        guid="guid-loc-ser", library_id=lib.id, item_type="series",
        name="同剧", tmdb_id="960010", probe_status="done",
        primary_image_url="https://img.example/s.jpg")
    db.add(local_series)
    db.flush()
    local_ep1 = em.MediaItem(
        guid="guid-loc-ep1", library_id=lib.id, item_type="episode",
        name="本地一", series_id=local_series.id, season_number=1,
        episode_number=1, file_path="mount://3/同剧/e1.mp4",
        probe_status="done")
    db.add(local_ep1)
    db.flush()
    # 托管剧整棵（同 tmdb）：第 1 集与本地同集、第 2 集本地没有
    items = [
        _agg_series(src.id, "up-ser-9", "同剧", "960010"),
        _agg_episode(src.id, "up-ep9-1", "同剧", 1, 1, "托管一",
                     series_upstream_id="up-ser-9"),
        _agg_episode(src.id, "up-ep9-2", "同剧", 1, 2, "托管二",
                     series_upstream_id="up-ser-9"),
    ]
    uimp.import_unpaired_series_tree(
        db, src, items, [], upstream_library_name="国产剧")
    db.commit()
    hosted_series = _row(db, _hosted_guid(src.id, "up-ser-9"))

    merged = mvw.merge_group(db, [local_series, hosted_series])
    db.commit()
    assert merged == 1
    db.expire_all()
    hosted_series = _row(db, _hosted_guid(src.id, "up-ser-9"))
    assert hosted_series.merged_into_id == local_series.id
    # 同集：托管第 1集合并进本地第 1 集，映射跟随迁移
    hosted_ep1 = _row(db, _hosted_guid(src.id, "up-ep9-1"))
    assert hosted_ep1.merged_into_id == local_ep1.id
    mig = db.query(em.UpstreamItemMapping).filter(
        em.UpstreamItemMapping.local_item_id == local_ep1.id,
        em.UpstreamItemMapping.upstream_item_id == "up-ep9-1").one()
    assert mig.source_id == src.id
    # 孤儿集：第 2 集本地没有 → reparent 到存活剧，不合并、仍可播
    hosted_ep2 = _row(db, _hosted_guid(src.id, "up-ep9-2"))
    assert hosted_ep2.merged_into_id is None
    assert hosted_ep2.series_id == local_series.id


# ---------------------------------------------------------------------------
# run 接线：整轮进库 + 生命周期随轮次推进
# ---------------------------------------------------------------------------

class _FakeTV:
    def __init__(self, episodes):
        self.episodes = episodes  # [(upstream_id, season, episode)]

    def get(self, url, headers, timeout):
        if url.split("?")[0].endswith("/Library/MediaFolders"):
            return 200, {"Items": [
                {"Id": "tv-1", "Name": "剧库", "CollectionType": "tvshows"}]}
        items = [{
            "Id": "r-ser", "Name": "独有剧丁", "Type": "Series",
            "ProductionYear": 2023, "ProviderIds": {"Tmdb": "960020"},
            "MediaSources": [{"Id": "ms-r-ser", "Name": "1080p"}]}]
        for up_id, season, ep_no in self.episodes:
            items.append({
                "Id": up_id, "Name": f"第{ep_no}集", "Type": "Episode",
                "SeriesId": "r-ser", "SeriesName": "独有剧丁",
                "ParentIndexNumber": season, "IndexNumber": ep_no,
                "ProviderIds": {},
                "MediaSources": [{"Id": f"ms-{up_id}", "Name": "1080p"}]})
        return 200, {"Items": items, "TotalRecordCount": len(items)}


def test_run_hook_imports_series_tree_then_lifecycle(db):
    from backend.emby_server import upstream_aggregate_run as uar
    src = _source(db, name="src-run2")
    _library(db, name="剧库")
    fake = _FakeTV([("r-ep1", 1, 1), ("r-ep2", 1, 2)])
    result = uar.run_manual_aggregation(db, src, http_get=fake.get)
    db.commit()
    assert result["series_imported"] == 1
    assert result["episodes_imported"] == 2
    rows = db.query(em.MediaItem).filter(
        em.MediaItem.upstream_source_id == src.id).all()
    assert {r.item_type for r in rows} == {"series", "season", "episode"}

    # 上游下架第 2 集：连续两轮不见后该集被对账隐藏，剧与第 1 集不动
    fake2 = _FakeTV([("r-ep1", 1, 1)])
    uar.run_manual_aggregation(db, src, http_get=fake2.get)
    db.commit()
    uar.run_manual_aggregation(db, src, http_get=fake2.get)
    db.commit()
    db.expire_all()
    ep2 = _row(db, _hosted_guid(src.id, "r-ep2"))
    assert ep2.is_hidden is True
    ep1 = _row(db, _hosted_guid(src.id, "r-ep1"))
    assert ep1.is_hidden in (False, None)


# ---------------------------------------------------------------------------
# 播放三环（真 TestClient）：托管剧/集
# ---------------------------------------------------------------------------

from backend.database import SessionLocal, init_db  # noqa: E402
from backend.security import hash_password  # noqa: E402

init_db()

PW = "ImportP2#2026"
BOUND = "importp2_bound"
FREE = "importp2_free"
LIB3_NAME = "进库二期播放库"
SRC3_NAME = "进库二期测试源"
SERIES_NAME = "托管剧戊"
EP_NAME = "托管第一集"
SER_UP = "up2-ser-1"
EP_UP = "up2-ep-1"


def _cleanup_client_seed(db):
    rows = db.query(em.MediaItem).filter(
        em.MediaItem.name.in_([SERIES_NAME, EP_NAME])).all()
    ids = [r.id for r in rows]
    if ids:
        db.query(em.UpstreamItemMapping).filter(
            em.UpstreamItemMapping.local_item_id.in_(ids)
        ).delete(synchronize_session=False)
        db.query(em.MediaItem).filter(
            em.MediaItem.id.in_(ids)).delete(synchronize_session=False)
    db.query(em.UpstreamSource).filter(
        em.UpstreamSource.name == SRC3_NAME).delete(
            synchronize_session=False)
    db.query(em.Library).filter(em.Library.name == LIB3_NAME).delete(
        synchronize_session=False)
    for uname in (BOUND, FREE):
        u = db.query(models.WebUser).filter(
            models.WebUser.username == uname).first()
        if u is not None:
            db.query(em.UserStorageBinding).filter(
                em.UserStorageBinding.user_id == u.id).delete(
                    synchronize_session=False)
            db.query(em.MountCredential).filter(
                em.MountCredential.owner_user_id == u.id).delete(
                    synchronize_session=False)
            db.delete(u)
    db.commit()


@pytest.fixture(scope="module")
def pseed():
    db = SessionLocal()
    _cleanup_client_seed(db)
    users = {}
    for uname in (BOUND, FREE):
        u = models.WebUser(
            username=uname, password_hash=hash_password(PW), is_active=True,
            is_staff=True, emby_username=uname,
            emby_password=hash_password(PW))
        db.add(u)
        db.flush()
        users[uname] = u.id
    cred = em.MountCredential(
        mount_id=None, scope="user", owner_user_id=users[BOUND],
        sealed_credential="", key_id="fernet-v1", status="active")
    db.add(cred)
    db.flush()
    db.add(em.UserStorageBinding(
        user_id=users[BOUND], provider="drive", account_label="种子盘",
        credential_id=cred.id, status="active"))
    src = ups.create_source(
        db, name=SRC3_NAME, source_type="emby",
        base_url="https://up3.example.com", auth_method="api_key")
    ups.enable_source(db, src)
    lib = em.Library(guid=uuid.uuid4().hex, name=LIB3_NAME,
                     collection_type="tvshows", is_enabled=True)
    db.add(lib)
    db.flush()
    series = em.MediaItem(
        guid=_hosted_guid(src.id, SER_UP), library_id=lib.id,
        item_type="series", name=SERIES_NAME, production_year=2023,
        tmdb_id="960030", file_path=None, upstream_source_id=src.id,
        probe_status="done")
    db.add(series)
    db.flush()
    season = em.MediaItem(
        guid=hashlib.md5(
            f"upstream:{src.id}:season:{series.id}:1".encode()).hexdigest(),
        library_id=lib.id, item_type="season", name="第 1 季",
        parent_id=series.id, series_id=series.id, season_number=1,
        file_path=None, upstream_source_id=src.id, probe_status="done")
    db.add(season)
    db.flush()
    episode = em.MediaItem(
        guid=_hosted_guid(src.id, EP_UP), library_id=lib.id,
        item_type="episode", name=EP_NAME, production_year=2023,
        parent_id=season.id, series_id=series.id, season_number=1,
        episode_number=1, file_path=None, upstream_source_id=src.id,
        probe_status="done")
    db.add(episode)
    db.flush()
    db.add(em.UpstreamItemMapping(
        local_item_id=series.id, source_id=src.id, upstream_item_id=SER_UP,
        confidence=1.0, evidence="tmdb"))
    db.add(em.UpstreamItemMapping(
        local_item_id=episode.id, source_id=src.id,
        upstream_item_id=EP_UP, confidence=1.0, evidence="tmdb",
        episode_index=1))
    db.commit()
    data = {"series_guid": series.guid, "ep_guid": episode.guid,
            "src": src.id}
    db.close()
    yield data
    db = SessionLocal()
    try:
        _cleanup_client_seed(db)
    finally:
        db.close()


@pytest.fixture(scope="module")
def pclient():
    from backend.main import app
    from fastapi.testclient import TestClient
    return TestClient(app, raise_server_exceptions=False)


def _auth_header():
    q = chr(34)
    return (f"MediaBrowser Client={q}ImportP2{q}, Device={q}iOS{q}, "
            f"DeviceId={q}importp2-dev{q}, Version={q}1.0{q}")


def _token(client, uname):
    r = client.post(
        "/emby/Users/AuthenticateByName",
        json={"Username": uname, "Pw": PW},
        headers={"X-Emby-Authorization": _auth_header()})
    assert r.status_code == 200, r.text
    return r.json()["AccessToken"]


@pytest.fixture(scope="module")
def bound_token(pclient, pseed):
    return _token(pclient, BOUND)


@pytest.fixture(scope="module")
def free_token(pclient, pseed):
    return _token(pclient, FREE)


def _upstream_line(body):
    return [s for s in body.get("MediaSources") or []
            if str(s.get("Id", "")).endswith(":upstream")]


def test_episode_detail_has_upstream_line(pclient, bound_token, pseed):
    r = pclient.get(f"/emby/Items/{pseed['ep_guid']}",
                    headers={"X-Emby-Token": bound_token})
    assert r.status_code == 200, r.text
    lines = _upstream_line(r.json())
    assert len(lines) == 1, r.json().get("MediaSources")
    assert lines[0]["Name"] == f"{EP_NAME} · 上游 · {SRC3_NAME}"


def test_episode_playbackinfo_only_upstream_source(
        pclient, bound_token, pseed, monkeypatch):
    from backend.emby_server import upstream_playback as up
    monkeypatch.setattr(
        up, "fetch_upstream_media_params",
        lambda source, upstream_item_id, **kw: {
            "size": 1_800_000_000, "run_time_ticks": 2_700_000_000,
            "container": "mkv", "bitrate": 5_000_000, "media_streams": []})
    r = pclient.post(f"/emby/Items/{pseed['ep_guid']}/PlaybackInfo",
                     headers={"X-Emby-Token": bound_token}, json={})
    assert r.status_code == 200, r.text
    sources = r.json().get("MediaSources") or []
    assert len(sources) == 1, sources
    assert sources[0]["Id"] == f"{pseed['ep_guid']}:upstream"
    assert sources[0]["Size"] == 1_800_000_000


def test_series_playbackinfo_resolves_hosted_episode(
        pclient, bound_token, pseed, monkeypatch):
    """剧级点播（IsPlayback=true）落到托管集出上游源，不回空源。"""
    from backend.emby_server import upstream_playback as up
    monkeypatch.setattr(
        up, "fetch_upstream_media_params",
        lambda source, upstream_item_id, **kw: None)
    r = pclient.post(
        f"/emby/Items/{pseed['series_guid']}/PlaybackInfo",
        params={"IsPlayback": "true"},
        headers={"X-Emby-Token": bound_token}, json={})
    assert r.status_code == 200, r.text
    sources = r.json().get("MediaSources") or []
    assert len(sources) == 1, sources
    assert sources[0]["Id"] == f"{pseed['ep_guid']}:upstream"


def test_episode_playbackinfo_unbound_user_blocked(
        pclient, free_token, pseed):
    r = pclient.post(f"/emby/Items/{pseed['ep_guid']}/PlaybackInfo",
                     headers={"X-Emby-Token": free_token}, json={})
    assert r.status_code == 403, r.text


# ---------------------------------------------------------------------------
# enrich 衔接（不改 enrich_worker：只验二期行身份字段齐、托管分支认）
# ---------------------------------------------------------------------------

def test_enrich_hosted_episode_without_file_path_not_a_failure():
    from backend.emby_server import enrich_worker
    item = SimpleNamespace(
        id=1, item_type="episode", name="托管第一集",
        production_year=2023, file_path=None, size=0, container="",
        library_id=1, tmdb_id="960031", poster_path=None,
        primary_image_url=None, imdb_id=None, aliases=None,
        repair_requested_at=None, upstream_source_id=7,
        series_id=10, season_number=1, episode_number=1)
    with mock.patch.object(
            enrich_worker, "_scanfile_from_item", return_value=None), \
            mock.patch("backend.emby_server.scanner._tmdb_work",
                       return_value=({"id": 960031}, {"id": 960031})) as m, \
            mock.patch("backend.emby_server.tmdb.tmdb_client") as m_client, \
            mock.patch("backend.emby_server.douban.enabled",
                       return_value=False):
        m_client.configured = True
        res = enrich_worker._enrich_fetch(item)
    assert res["ok"] is True, f"托管集不该被判失败：{res}"
    assert res["error"] is None
