# -*- coding: utf-8 -*-
"""Nova N4-1：多版本合并键收紧为同 TMDB 硬条件（NovaEmby 全面学习立项 N4）。

Nova 实证口径：同片多版本合并的硬条件只有一条——同一个 TMDB 编号；
没有 TMDB 编号就不合，绝不用 imdb / 片名+年份这类模糊条件猜着合。

Aetrix 此前（merge_versions_worker._provider_key）：tmdb 优先、imdb 兜底
单独成键——两行都无 tmdb、仅 imdb 相同时会被自动合并（imdb 编号在不同
片源/不同版本间复用与误绑并不罕见，猜合风险实存）。episode_versions 则
完全不看 series 的 tmdb 身份：series 无 tmdb 时同季同集照样自动合并。

本文件钉住新口径：
- _provider_key：只有 tmdb 能成键；仅 imdb → None（不参与自动合并）；
- find_duplicate_groups：无 tmdb 的行不进组（imdb 相同也不进）；
  同 tmdb 仍成组、仍可 merge_group 合并（回归）；
  tmdb 不同（哪怕同名同年同 imdb）不成组；
- merge_episode_versions：所属 series 行存在且无 tmdb_id → 整组跳过，
  计数 stats["skipped_no_tmdb"]、不写 merged_into_id；
  series 有 tmdb → 维持现行合并口径。
  （series 行缺失属异常/旧夹具形态：身份未知、不在本门拦截范围，
  维持既有行为——孤儿回收等存量路径的测试口径不因此改变。）
"""

import os

os.environ.setdefault("DATABASE_TYPE", "sqlite")
os.environ.setdefault("REDIS_ENABLED", "false")

import sys
from types import SimpleNamespace

sys.path.insert(0, ".")

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend.database import Base
from backend.emby_server import episode_versions as ev
from backend.emby_server import merge_versions_worker as mvw
from backend.emby_server import models as em
import backend.models  # noqa: F401 — 注册 web_users 等表，供 create_all 建表


@pytest.fixture()
def db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


def _movie(db, **kw):
    defaults = dict(
        guid=kw.pop("guid", f"m{kw.get('id', 0)}"),
        library_id=1,
        item_type="movie",
        name="同片",
        production_year=2020,
        file_path="/x/movie - 1080p.mkv",
    )
    defaults.update(kw)
    item = em.MediaItem(**defaults)
    db.add(item)
    db.commit()
    return item


def _series(db, **kw):
    defaults = dict(
        guid=kw.pop("guid", f"s{kw.get('id', 0)}"),
        library_id=1,
        item_type="series",
        name="测试剧",
        file_path="/x/show",
    )
    defaults.update(kw)
    item = em.MediaItem(**defaults)
    db.add(item)
    db.commit()
    return item


def _episode(db, **kw):
    defaults = dict(
        guid=kw.pop("guid", f"e{kw.get('id', 0)}"),
        library_id=1,
        item_type="episode",
        name="S01E01",
        series_id=100,
        season_number=1,
        episode_number=1,
        file_path="/x/S01E01.strm",
    )
    defaults.update(kw)
    item = em.MediaItem(**defaults)
    db.add(item)
    db.commit()
    return item


def _group_id_sets(db):
    return [sorted(it.id for it in g) for g in mvw.find_duplicate_groups(db)]


# ---------- _provider_key：只有 tmdb 能成键 ----------

def test_provider_key_tmdb_still_forms_key():
    item = SimpleNamespace(library_id=1, item_type="movie",
                           tmdb_id="123", imdb_id="tt456")
    assert mvw._provider_key(item) == (1, "movie", "tmdb", "123")


def test_provider_key_imdb_only_returns_none():
    item = SimpleNamespace(library_id=1, item_type="movie",
                           tmdb_id="", imdb_id="tt456")
    assert mvw._provider_key(item) is None


def test_provider_key_no_ids_returns_none():
    item = SimpleNamespace(library_id=1, item_type="movie",
                           tmdb_id=None, imdb_id=None)
    assert mvw._provider_key(item) is None


# ---------- find_duplicate_groups：分组键收紧 ----------

def test_no_tmdb_same_imdb_not_grouped(db):
    """旧毛病正例：两行都无 tmdb、仅 imdb 相同——旧键会成组自动合并。"""
    a = _movie(db, id=1, guid="m1", tmdb_id=None, imdb_id="tt1111111")
    b = _movie(db, id=2, guid="m2", tmdb_id=None, imdb_id="tt1111111")
    assert _group_id_sets(db) == []
    db.refresh(a)
    db.refresh(b)
    assert a.merged_into_id is None and b.merged_into_id is None


def test_imdb_bridge_chain_not_grouped(db):
    """imdb 桥接变体：无 tmdb 两行靠 imdb 成组、第三行同 imdb 但有 tmdb。

    收紧后 imdb 边彻底不存在：无 tmdb 两行不进组，有 tmdb 的行单独不成组。
    """
    _movie(db, id=1, guid="m1", tmdb_id=None, imdb_id="tt2222222")
    _movie(db, id=2, guid="m2", tmdb_id="", imdb_id="tt2222222")
    _movie(db, id=3, guid="m3", tmdb_id="777", imdb_id="tt2222222")
    assert _group_id_sets(db) == []


def test_same_tmdb_still_merges(db):
    """回归：同 tmdb 两行仍成组、仍可合并。"""
    a = _movie(db, id=1, guid="m1", tmdb_id="555", imdb_id="tt3333333",
               file_path="/x/movie - 720p.mkv")
    b = _movie(db, id=2, guid="m2", tmdb_id="555", imdb_id=None,
               file_path="/x/movie - 2160p.mkv")
    groups = mvw.find_duplicate_groups(db)
    assert [sorted(it.id for it in g) for g in groups] == [[1, 2]]
    merged = mvw.merge_group(db, groups[0])
    db.commit()
    assert merged == 1
    db.refresh(a)
    db.refresh(b)
    primary_ids = [it.id for it in (a, b) if it.merged_into_id is None]
    assert len(primary_ids) == 1
    other = b if a.merged_into_id is None else a
    assert other.merged_into_id == primary_ids[0]


def test_different_tmdb_same_title_year_imdb_not_grouped(db):
    """同名同年、同 imdb，但 tmdb 不同 → 两部不同的片，绝不猜合。"""
    _movie(db, id=1, guid="m1", name="同片", production_year=2020,
           tmdb_id="101", imdb_id="tt4444444")
    _movie(db, id=2, guid="m2", name="同片", production_year=2020,
           tmdb_id="102", imdb_id="tt4444444")
    assert _group_id_sets(db) == []


# ---------- episode_versions：series 无 tmdb 不自动合 ----------

def test_episode_series_without_tmdb_skipped(db):
    series = _series(db, id=100, guid="s100", tmdb_id=None)
    v1 = _episode(db, id=1, guid="e1", series_id=series.id, height=720,
                  size=100, file_path="/x/S01E01 - 720p.strm")
    v2 = _episode(db, id=2, guid="e2", series_id=series.id, height=2160,
                  size=200, file_path="/x/S01E01 - 2160p.strm")
    stats = ev.merge_episode_versions(db, library_id=1)
    db.commit()
    assert stats["merged"] == 0
    assert stats["groups"] == 0
    assert stats["skipped_no_tmdb"] == 1
    db.refresh(v1)
    db.refresh(v2)
    assert v1.merged_into_id is None and v2.merged_into_id is None


def test_episode_series_with_tmdb_still_merges(db):
    series = _series(db, id=100, guid="s100", tmdb_id="888")
    v1 = _episode(db, id=1, guid="e1", series_id=series.id, height=720,
                  size=100, file_path="/x/S01E01 - 720p.strm")
    v2 = _episode(db, id=2, guid="e2", series_id=series.id, height=2160,
                  size=200, file_path="/x/S01E01 - 2160p.strm")
    stats = ev.merge_episode_versions(db, library_id=1)
    db.commit()
    assert stats["merged"] == 1
    assert stats["skipped_no_tmdb"] == 0
    db.refresh(v1)
    db.refresh(v2)
    assert v2.merged_into_id is None
    assert v1.merged_into_id == v2.id
