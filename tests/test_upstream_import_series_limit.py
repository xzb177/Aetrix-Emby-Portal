"""上游独有剧进库 · 剧数限额 series_limit 回归（2026-11-01）。

背景：二期整轮官方入口 run_manual_aggregation 的 import_limit 是
行限额（剧/季/集每个新建行各耗 1），且聚合按 WINDOW_RECORDS 分
窗、每窗先建剧行再处理集行——任何行限额取值都做不出「约 10 部
完整剧树」的小批量试点（小了只有光秃剧行、大了上百部）。

本文件锁住新口径（series_limit / import_series_limit）：

- 只统计**新建的独有剧树**（未配对 Series 新建行）：达到上限后
  不再接纳新剧，其季/集一行不建；已存在剧行不占额；
- 完整性：一部剧一旦被接纳，它在本轮后续任何窗出现的季/集照常
  落库，**不受行限额 _Limit 拦截**（接纳集合跨窗存活，由
  _SeriesBudget 线程化，run 层整轮共用一个）；
- 被拒新剧的集靠上游 SeriesId 确定性跳过——绝不落标题回落误挂
  同名本地剧（O-08 防错红线），跨窗同理；
- 已配对剧补集不消耗 series_limit，其受行限额的口径维持现状；
- series_limit=None（默认）＝功能不介入，既有行为逐字不变
  （由 phase1/phase2 既有套件全过兜底，本文件只补新语义）。

DB 纯逻辑用内存 SQLite（照 phase2 形态）；run 接线用假 HTTP。
"""

from __future__ import annotations

import hashlib
import os

os.environ.setdefault("DATABASE_TYPE", "sqlite")
os.environ.setdefault("REDIS_ENABLED", "false")

import uuid

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import backend.models as models
from backend.emby_server import models as em
from backend.emby_server import upstream_aggregation as agg


# ---------------------------------------------------------------------------
# 内存 DB 夹具（与 phase2 同形态）
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


def _source(db, *, name="src-series-limit", enabled=True,
            import_enabled=True):
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


def _hosted(db, src_id, item_type=None):
    q = db.query(em.MediaItem).filter(
        em.MediaItem.upstream_source_id == src_id)
    if item_type:
        q = q.filter(em.MediaItem.item_type == item_type)
    return q.all()


# ---------------------------------------------------------------------------
# ① series_limit=2、候选 3 部、跨窗分布 → 恰好 2 部完整树，第 3 部零行
# ---------------------------------------------------------------------------

def test_series_limit_two_full_trees_across_windows(db):
    from backend.emby_server import upstream_import as uimp
    src = _source(db)
    _library(db, name="国产剧")
    budget = uimp._SeriesBudget(2)

    # 第一窗：只有 3 部剧的剧行——甲、乙接纳，丙被拒
    stats1 = uimp.import_unpaired_series_tree(
        db, src,
        [_agg_series(src.id, "up-ser-a", "试点剧甲", "970001"),
         _agg_series(src.id, "up-ser-b", "试点剧乙", "970002"),
         _agg_series(src.id, "up-ser-c", "试点剧丙", "970003")],
        [], upstream_library_name="国产剧", series_budget=budget)
    db.commit()
    assert stats1["series_imported"] == 2
    assert stats1["series_limited"] == 1

    # 第二窗：只有集——甲/乙的集照常落库，丙的集按 SeriesId 跳过
    stats2 = uimp.import_unpaired_series_tree(
        db, src,
        [_agg_episode(src.id, "up-ep-a1", "试点剧甲", 1, 1, "甲一",
                      series_upstream_id="up-ser-a"),
         _agg_episode(src.id, "up-ep-b1", "试点剧乙", 1, 1, "乙一",
                      series_upstream_id="up-ser-b"),
         _agg_episode(src.id, "up-ep-c1", "试点剧丙", 1, 1, "丙一",
                      series_upstream_id="up-ser-c"),
         _agg_episode(src.id, "up-ep-a2", "试点剧甲", 1, 2, "甲二",
                      series_upstream_id="up-ser-a")],
        [], upstream_library_name="国产剧", series_budget=budget)
    db.commit()
    assert stats2["episodes_imported"] == 3
    assert stats2["seasons_created"] == 2
    assert stats2["series_limited"] == 1  # 丙的集

    # 恰好 2 部完整树（剧+季+集齐），丙一行没有（剧/集 guid 皆无）
    assert len(_hosted(db, src.id, "series")) == 2
    assert len(_hosted(db, src.id, "season")) == 2
    assert len(_hosted(db, src.id, "episode")) == 3
    for up_id in ("up-ser-c", "up-ep-c1"):
        assert db.query(em.MediaItem).filter(
            em.MediaItem.guid == _hosted_guid(src.id, up_id)
        ).first() is None
    ser_a = db.query(em.MediaItem).filter(
        em.MediaItem.guid == _hosted_guid(src.id, "up-ser-a")).one()
    ep_a = db.query(em.MediaItem).filter(
        em.MediaItem.item_type == "episode",
        em.MediaItem.series_id == ser_a.id).all()
    assert len(ep_a) == 2  # 甲的树是完整的：两集都在
    assert budget.admitted_count == 2


def test_series_limit_param_only_counts_new_series(db):
    """series_limit 直接传参（无共享 budget）：只数新建，已存在不占额。"""
    from backend.emby_server import upstream_import as uimp
    src = _source(db)
    _library(db, name="国产剧")
    items = [_agg_series(src.id, "up-ser-a", "试点剧甲", "970001"),
             _agg_series(src.id, "up-ser-b", "试点剧乙", "970002")]
    first = uimp.import_unpaired_series_tree(
        db, src, items, [], upstream_library_name="国产剧",
        series_limit=1)
    db.commit()
    # 一次调用内：第 1 部接纳、第 2 部被拒
    assert first["series_imported"] == 1
    assert first["series_limited"] == 1
    # 已进库的那部重跑不再占额（existing），计数口径不重复
    second = uimp.import_unpaired_series_tree(
        db, src, [_agg_series(src.id, "up-ser-a", "试点剧甲", "970001")],
        [], upstream_library_name="国产剧", series_limit=1)
    db.commit()
    assert second["series_imported"] == 0
    assert second["existing"] == 1
    assert second["series_limited"] == 0


# ---------------------------------------------------------------------------
# ② 接纳剧的集行在行限额已耗尽后到达仍落库；补集仍受行限额（现状）
# ---------------------------------------------------------------------------

def test_admitted_tree_episodes_bypass_exhausted_row_limit(db):
    from backend.emby_server import upstream_import as uimp
    src = _source(db)
    lib = _library(db, name="国产剧")
    # 已配对本地剧戊：补集对象（不消耗 series_limit）
    local_series = em.MediaItem(
        guid="guid-local-wu", library_id=lib.id, item_type="series",
        name="本地剧戊", tmdb_id="970102", probe_status="done")
    db.add(local_series)
    db.flush()
    pairs = [{"local_item_id": local_series.id,
              "upstream_item_id": "up-ser-p", "item_type": "Series",
              "confidence": 1.0, "evidence": "tmdb",
              "episode_index": None}]
    items = [
        _agg_series(src.id, "up-ser-p", "本地剧戊", "970102"),
        _agg_series(src.id, "up-ser-a", "试点剧甲", "970101"),
        _agg_episode(src.id, "up-ep-a1", "试点剧甲", 1, 1, "甲一",
                     series_upstream_id="up-ser-a"),
        _agg_episode(src.id, "up-ep-a2", "试点剧甲", 1, 2, "甲二",
                     series_upstream_id="up-ser-a"),
        _agg_episode(src.id, "up-ep-p2", "本地剧戊", 1, 2, "戊二",
                     series_upstream_id="up-ser-p"),
    ]
    # 行限额只够建 1 行：被剧甲的剧行吃掉后即耗尽
    stats = uimp.import_unpaired_series_tree(
        db, src, items, pairs, upstream_library_name="国产剧",
        limit=1, series_limit=1)
    db.commit()
    assert stats["series_imported"] == 1
    # 接纳剧的季/集不受行限额拦截：整棵落齐
    assert stats["seasons_created"] == 1
    assert stats["episodes_imported"] == 2
    # 补集维持现状：季行要占行限额，限额已耗尽 → limited，不落
    assert stats["limited"] == 1
    assert stats["imported"] == 4  # 剧 1 + 季 1 + 集 2
    ep_rows = _hosted(db, src.id, "episode")
    assert len(ep_rows) == 2
    assert all(r.series_id != local_series.id for r in ep_rows)


# ---------------------------------------------------------------------------
# 被拒新剧的集跨窗按 SeriesId 跳过：绝不标题回落误挂同名本地剧
# ---------------------------------------------------------------------------

def test_rejected_series_episode_never_title_attaches(db):
    from backend.emby_server import upstream_import as uimp
    src = _source(db)
    lib = _library(db, name="国产剧")
    clash = em.MediaItem(
        guid="guid-local-clash", library_id=lib.id, item_type="series",
        name="撞名剧", tmdb_id="970201", probe_status="done")
    db.add(clash)
    db.flush()
    budget = uimp._SeriesBudget(0)  # 一部都不接纳

    # 第一窗：同名独有剧（tmdb 不同）被 series_limit 拒
    stats1 = uimp.import_unpaired_series_tree(
        db, src, [_agg_series(src.id, "up-ser-clash", "撞名剧",
                              "970202")],
        [], upstream_library_name="国产剧", series_budget=budget)
    db.commit()
    assert stats1["series_limited"] == 1
    assert stats1["imported"] == 0

    # 第二窗：它的集到达——SeriesId 命中拒绝登记，确定性跳过；
    # 若没有这道拦截，标题回落会把集误挂到本地「撞名剧」下
    stats2 = uimp.import_unpaired_series_tree(
        db, src,
        [_agg_episode(src.id, "up-ep-clash", "撞名剧", 1, 1, "第一集",
                      series_upstream_id="up-ser-clash")],
        [], upstream_library_name="国产剧", series_budget=budget)
    db.commit()
    assert stats2["series_limited"] == 1
    assert stats2["imported"] == 0
    assert db.query(em.MediaItem).filter(
        em.MediaItem.series_id == clash.id).count() == 0
    assert db.query(em.MediaItem).count() == 1  # 只有本地剧本身


# ---------------------------------------------------------------------------
# run 接线：import_series_limit 整轮线程化（官方入口、真跨窗）
# ---------------------------------------------------------------------------

class _FakeMulti:
    """一个剧库、3 部剧 + 每部 2 集；记录顺序让窗口从树中间切开。"""

    def get(self, url, headers, timeout):
        if url.split("?")[0].endswith("/Library/MediaFolders"):
            return 200, {"Items": [
                {"Id": "tv-1", "Name": "剧库",
                 "CollectionType": "tvshows"}]}
        items = []
        for ser_id, name, tmdb in (
                ("r-serA", "试点剧A", "970001"),
                ("r-serB", "试点剧B", "970002"),
                ("r-serC", "试点剧C", "970003")):
            items.append({
                "Id": ser_id, "Name": name, "Type": "Series",
                "ProductionYear": 2023,
                "ProviderIds": {"Tmdb": tmdb},
                "MediaSources": [{"Id": f"ms-{ser_id}",
                                  "Name": "1080p"}]})
        for ser_id, name in (("r-serA", "试点剧A"),
                             ("r-serB", "试点剧B"),
                             ("r-serC", "试点剧C")):
            for ep_no in (1, 2):
                up_id = f"{ser_id}-ep{ep_no}"
                items.append({
                    "Id": up_id, "Name": f"第{ep_no}集",
                    "Type": "Episode", "SeriesId": ser_id,
                    "SeriesName": name, "ParentIndexNumber": 1,
                    "IndexNumber": ep_no, "ProviderIds": {},
                    "MediaSources": [{"Id": f"ms-{up_id}",
                                      "Name": "1080p"}]})
        # 窗口按 3 条切：[3 部剧][A1 B1 C1][A2 B2 C2]
        order = [0, 1, 2, 3, 5, 7, 4, 6, 8]
        items = [items[i] for i in order]
        return 200, {"Items": items, "TotalRecordCount": len(items)}


def test_run_import_series_limit_threads_across_windows(db, monkeypatch):
    from backend.emby_server import upstream_aggregate_run as uar
    monkeypatch.setattr(uar, "WINDOW_RECORDS", 3)
    src = _source(db, name="src-run-series-limit")
    _library(db, name="剧库")
    result = uar.run_manual_aggregation(
        db, src, http_get=_FakeMulti().get, import_series_limit=2)
    db.commit()
    assert result["series_imported"] == 2
    assert result["episodes_imported"] == 4
    # 丙：剧行 1 次 + 集 2 次被剧数限额拦下
    assert result["import_series_limited"] == 3
    assert len(_hosted(db, src.id, "series")) == 2
    assert len(_hosted(db, src.id, "episode")) == 4
    for up_id in ("r-serC", "r-serC-ep1", "r-serC-ep2"):
        assert db.query(em.MediaItem).filter(
            em.MediaItem.guid == _hosted_guid(src.id, up_id)
        ).first() is None
