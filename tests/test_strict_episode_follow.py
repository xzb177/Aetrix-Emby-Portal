# -*- coding: utf-8 -*-
"""双份治理病根修复回归测试。

背景（生产实锤）：
1. 严格通道（同 tmdb）合并 series 时只搬剧不搬集——merge_group 之后从不调
   集跟随，集被永久滞留在被合并 series 名下（全库 8 万+ 滞留集的来源）。
   修复：集跟随收敛进 merge_group 末尾，严格/宽松两通道共用同一收尾。
2. 跟随集指向的主集被 episode_versions 按画质选主换人合并掉后，孤儿回收
   见目标"不存活"直接清标（merged_into_id=None），跟随集永久再滞留
   （治理窗口内实测 531 集）。修复：孤儿回收先沿 merged_into 链找存活根
   改指，链断才清标。

覆盖：
- 严格通道 merge_group 后集跟随到位：重复型（同集成版本）+ 孤儿型（reparent）
- 重复执行/双调幂等，不产生重复 (季,集)
- 531 全序复现：跟随 → 主集换人 → 孤儿回收改指存活根
- 链断（目标已删且无下游）仍清标恢复独立；链中行已删但有下游时穿透改指
"""
import os

os.environ.setdefault("DATABASE_TYPE", "sqlite")
os.environ.setdefault("REDIS_ENABLED", "false")

from datetime import datetime

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend.database import Base
from backend.emby_server import episode_versions as ev
from backend.emby_server import merge_versions_worker as mvw
from backend.emby_server import models as em
import backend.models  # noqa: F401 — 注册 web_users 等表，供 create_all 建表


@pytest.fixture()
def db(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path}/strict_follow.db",
        connect_args={"check_same_thread": False, "timeout": 30},
    )
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, expire_on_commit=False)
    s = Session()
    try:
        yield s
    finally:
        s.close()
        engine.dispose()


def _series(db, name, tmdb=None, year=None, lib=1, poster=None):
    it = em.MediaItem(
        guid=f"s-{name}-{tmdb}-{os.urandom(3).hex()}",
        library_id=lib, item_type="series", name=name,
        tmdb_id=tmdb, production_year=year, file_path="/x",
        poster_path=poster,
    )
    db.add(it)
    db.commit()
    return it


def _episode(db, series_id, season, ep, lib=1, height=None, size=None):
    it = em.MediaItem(
        guid=f"e-{series_id}-{season}-{ep}-{os.urandom(3).hex()}",
        library_id=lib, item_type="episode", name=f"S{season:02d}E{ep:02d}",
        series_id=series_id, season_number=season, episode_number=ep,
        file_path=f"/x/{series_id}/S{season:02d}E{ep:02d}-{os.urandom(2).hex()}.strm",
        height=height, size=size,
    )
    db.add(it)
    db.commit()
    return it


def _pair(db, tmdb="111"):
    """正主（有海报，pick_primary 必胜）+ 同 tmdb 分身。"""
    p = _series(db, "同部剧", tmdb=tmdb, poster="/p.jpg")
    d = _series(db, "同部剧", tmdb=tmdb)
    return p, d


# ---------------- 1. 严格通道集跟随 ----------------

def test_strict_merge_follows_duplicate_episode(db):
    """重复型：分身的同集在 merge_group 后成版本指主集（严格通道原缺口）。"""
    p, d = _pair(db)
    p_e1 = _episode(db, p.id, 1, 1)
    d_e1 = _episode(db, d.id, 1, 1)
    # _merge_once 严格分支正是这样调用：只有 merge_group，没有额外跟随调用
    merged = mvw.merge_group(db, [p, d])
    assert merged == 1
    db.commit()
    db.refresh(d)
    db.refresh(d_e1)
    assert d.merged_into_id == p.id
    assert d_e1.merged_into_id == p_e1.id
    assert d_e1.series_id == d.id  # 成版本不改 series_id（与既有跟随语义一致）


def test_strict_merge_reparents_orphan_episode(db):
    """孤儿型：主 series 没有的集，merge_group 后 reparent 到主 series 名下。"""
    p, d = _pair(db, tmdb="222")
    _episode(db, p.id, 1, 1)
    d_e2 = _episode(db, d.id, 1, 2)
    merged = mvw.merge_group(db, [p, d])
    assert merged == 1
    db.commit()
    db.refresh(d_e2)
    assert d_e2.series_id == p.id
    assert d_e2.merged_into_id is None


def test_movie_group_does_not_follow(db):
    """电影组不触发集跟随（类型门）：merge_group 行为与此前一致。"""
    a = em.MediaItem(
        guid=f"m-a-{os.urandom(2).hex()}", library_id=1, item_type="movie",
        name="电影", tmdb_id="999", file_path="/x", poster_path="/p.jpg",
    )
    b = em.MediaItem(
        guid=f"m-b-{os.urandom(2).hex()}", library_id=1, item_type="movie",
        name="电影", tmdb_id="999", file_path="/x",
    )
    db.add_all([a, b])
    db.commit()
    assert mvw.merge_group(db, [a, b]) == 1
    db.commit()
    db.refresh(b)
    assert b.merged_into_id == a.id


# ---------------- 2. 幂等 ----------------

def test_strict_follow_idempotent_no_dup_keys(db):
    """重复 merge_group + 显式双调 _follow_series_episodes 均无副作用，
    主 series 下可见 (季,集) 键不重复。"""
    p, d = _pair(db, tmdb="333")
    p_e1 = _episode(db, p.id, 1, 1)
    d_e1 = _episode(db, d.id, 1, 1)
    _episode(db, d.id, 1, 2)  # 孤儿集
    assert mvw.merge_group(db, [p, d]) == 1
    db.commit()
    # 第二轮：组已合并，merge_group 返回 0；再显式双调跟随（模拟旧宽松路径）
    assert mvw.merge_group(db, [p, d]) == 0
    mvw._follow_series_episodes(db, [p, d])
    db.commit()
    visible = (
        db.query(em.MediaItem)
        .filter(
            em.MediaItem.series_id == p.id,
            em.MediaItem.item_type == "episode",
            em.MediaItem.deleted_at.is_(None),
            em.MediaItem.merged_into_id.is_(None),
        )
        .all()
    )
    keys = sorted((e.season_number, e.episode_number) for e in visible)
    assert keys == [(1, 1), (1, 2)]
    db.refresh(d_e1)
    assert d_e1.merged_into_id == p_e1.id


# ---------------- 3. 531 全序复现 ----------------

def test_followed_episode_repoints_after_quality_reselect(db):
    """跟随集 → 主集被画质选主换人合并 → 孤儿回收改指存活根，不再滞留。"""
    p, d = _pair(db, tmdb="444")
    main_ep = _episode(db, p.id, 1, 1, height=1080, size=100)
    follow_ep = _episode(db, d.id, 1, 1, height=720, size=50)
    orphan_ep = _episode(db, d.id, 1, 2, height=1080, size=100)
    assert mvw.merge_group(db, [p, d]) == 1
    db.commit()
    db.refresh(follow_ep)
    db.refresh(orphan_ep)
    assert follow_ep.merged_into_id == main_ep.id
    assert orphan_ep.series_id == p.id

    # 正主 series 下新入库 2160p 版本：episode_versions 按画质换人
    new_main = _episode(db, p.id, 1, 1, height=2160, size=200)
    stats = ev.merge_episode_versions(db, library_id=1)
    db.commit()
    db.refresh(main_ep)
    db.refresh(follow_ep)
    assert main_ep.merged_into_id == new_main.id  # 旧主集被换人合并
    # 旧行为：follow_ep 被清标永久再滞留；新行为：改指存活根
    assert follow_ep.merged_into_id == new_main.id
    assert stats["repointed_orphans"] == 1
    assert stats["unmerged_orphans"] == 0


def test_orphan_chain_broken_still_unmerges(db):
    """链断（目标已删且无下游）：仍清标恢复独立，不误改指。"""
    lo = _episode(db, 100, 1, 1, height=720, size=100)
    hi = _episode(db, 100, 1, 1, height=2160, size=200)
    stats = ev.merge_episode_versions(db, library_id=1)
    db.commit()
    db.refresh(lo)
    assert lo.merged_into_id == hi.id
    hi.deleted_at = datetime.now()
    db.commit()
    stats = ev.merge_episode_versions(db, library_id=1)
    db.commit()
    db.refresh(lo)
    assert lo.merged_into_id is None
    assert stats["unmerged_orphans"] == 1
    assert stats["repointed_orphans"] == 0


def test_merge_once_commits_repoint_only_round(tmp_path, monkeypatch):
    """worker 提交口径回归：纯改指轮（groups=0、unmerged_orphans=0、
    repointed_orphans>0）也必须 commit——否则改指在 db.close() 时被回滚，
    跟随集继续指向已合并行（与 F2 纯孤儿回收同类坑）。"""
    import backend.database as _dbmod

    engine = create_engine(
        f"sqlite:///{tmp_path}/repoint_commit.db",
        connect_args={"check_same_thread": False, "timeout": 30},
    )
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, expire_on_commit=False)
    monkeypatch.setattr(_dbmod, "SessionLocal", Session)

    s = Session()
    root = _episode(s, 100, 1, 1, height=2160, size=200)
    mid = _episode(s, 100, 1, 1, height=1080, size=100)
    leaf = _episode(s, 100, 1, 1, height=720, size=50)
    mid.merged_into_id = root.id
    leaf.merged_into_id = mid.id
    s.commit()
    leaf_id, root_id = leaf.id, root.id
    s.close()

    mvw._merge_once()

    check = Session()
    try:
        row = check.get(em.MediaItem, leaf_id)
        assert row.merged_into_id == root_id, (
            "纯改指轮的修改必须被提交：提交口径不含 repointed_orphans 时，"
            "改指会在会话关闭时被回滚")
    finally:
        check.close()
        engine.dispose()


def test_orphan_repoint_follows_through_deleted_intermediate(db):
    """链中行已删但改指了下游存活根：穿透改指，不清标。"""
    root = _episode(db, 100, 1, 1, height=2160, size=200)
    mid = _episode(db, 100, 1, 1, height=1080, size=100)
    leaf = _episode(db, 100, 1, 1, height=720, size=50)
    mid.merged_into_id = root.id
    leaf.merged_into_id = mid.id
    db.commit()
    mid.deleted_at = datetime.now()
    db.commit()
    stats = ev.merge_episode_versions(db, library_id=1)
    db.commit()
    db.refresh(leaf)
    assert leaf.merged_into_id == root.id
    assert stats["repointed_orphans"] == 1
    assert stats["unmerged_orphans"] == 0
