"""物理删除兜底：真删一行前先解开入向 merged_into，杜绝悬空合并。

背景（生产实账 2026-11-05）：全站 2339 行（series 1204 / movie 610 /
season 525）的 merged_into_id 指向已被物理删除的行，挂在下面的 10929 集
随之不可见。成因是几条真删路径删目标行时没处理入向合并引用——
``scanner._purge_items``（扫描清理软删关分支、byos 清理、软删回收共用）
与 ``upstream_import._delete_rows`` 的硬删分支（上游托管行下架/删源）。

语义定版（本文件钉住）：
- 真删前，把 merged_into_id 指向待删行的**存活行**一律置 NULL 解开，
  分身恢复为独立可见行；**不许改指**到别的行（改指是数据修复的活）；
- 删除本身照常进行，待删集合内部互相指向的行不解（一起消失）；
- 已软删的分身同样要解（它的指向同样不许悬空）。

不碰网络、不碰生产库。
"""
from __future__ import annotations

import os
import uuid
from datetime import datetime, timedelta

os.environ.setdefault("DATABASE_TYPE", "sqlite")
os.environ.setdefault("REDIS_ENABLED", "false")

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend import models
from backend.emby_server import models as em
from backend.emby_server import scanner, soft_delete, upstream_import
from backend.integrations import store


@pytest.fixture()
def db():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False},
                           poolclass=StaticPool)
    models.Base.metadata.create_all(engine)
    em.Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    store.invalidate()
    yield session
    store.invalidate()
    session.close()


def _guid():
    return uuid.uuid4().hex


def _lib(db, name="剧集库"):
    row = em.Library(guid=_guid(), name=name, collection_type="tvshows", paths="")
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def _series(db, lib_id, name, tmdb_id=None, merged_into_id=None, deleted_at=None):
    row = em.MediaItem(guid=_guid(), library_id=lib_id, item_type="series",
                       name=name, tmdb_id=tmdb_id, merged_into_id=merged_into_id,
                       deleted_at=deleted_at)
    db.add(row)
    db.commit()
    with soft_delete.include_deleted():
        db.refresh(row)
    return row


def _movie(db, lib_id, name, merged_into_id=None):
    row = em.MediaItem(guid=_guid(), library_id=lib_id, item_type="movie",
                       name=name, merged_into_id=merged_into_id)
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def _season(db, lib_id, series_id, number):
    row = em.MediaItem(guid=_guid(), library_id=lib_id, item_type="season",
                       name=f"第 {number} 季", series_id=series_id,
                       season_number=number)
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def _episode(db, lib_id, series_id, season_id, season_number, episode_number):
    row = em.MediaItem(guid=_guid(), library_id=lib_id, item_type="episode",
                       name=f"第 {episode_number} 集", series_id=series_id,
                       parent_id=season_id, season_number=season_number,
                       episode_number=episode_number)
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def _row(db, item_id):
    db.expire_all()
    with soft_delete.include_deleted():
        return db.query(em.MediaItem).filter(em.MediaItem.id == item_id).first()


def _merged_into(db, item_id):
    row = _row(db, item_id)
    return None if row is None else row.merged_into_id


# ==================== 1. 共用兜底函数本体 ====================

def test_detach_unmerges_children_and_keeps_them(db):
    lib = _lib(db)
    target = _series(db, lib.id, "Show", tmdb_id="100")
    child = _series(db, lib.id, "Show (2020)", tmdb_id="100",
                    merged_into_id=target.id)
    target_id, child_id = target.id, child.id

    n = soft_delete.detach_merged_children(db, [target_id])
    db.commit()

    assert n == 1
    assert _merged_into(db, child_id) is None, "分身必须被解开（置 NULL）"
    assert _row(db, child_id) is not None, "分身行本身不许被删"
    # 恢复显示：普通可见性口径（不带 include_deleted）能直接查到
    db.expire_all()
    visible = db.query(em.MediaItem).filter(em.MediaItem.id == child_id).first()
    assert visible is not None and visible.merged_into_id is None


def test_detach_does_not_repoint_to_other_root(db):
    """语义定版：只解开、不改指——哪怕同库有同 tmdb 的另一个 root，
    分身也只能变回独立行，不许被顺手改指过去。"""
    lib = _lib(db)
    target = _series(db, lib.id, "Show", tmdb_id="101")
    other_root = _series(db, lib.id, "Show 别名", tmdb_id="101")
    child = _series(db, lib.id, "Show (2020)", tmdb_id="101",
                    merged_into_id=target.id)
    target_id, other_id, child_id = target.id, other_root.id, child.id

    soft_delete.detach_merged_children(db, [target_id])
    db.commit()

    assert _merged_into(db, child_id) is None
    assert _merged_into(db, child_id) != other_id


def test_detach_covers_hidden_children_and_batch_internals(db):
    lib = _lib(db)
    target = _series(db, lib.id, "Show", tmdb_id="102")
    target_id = target.id
    hidden_child = _series(db, lib.id, "Show (2019)", tmdb_id="102",
                           merged_into_id=target_id,
                           deleted_at=datetime.now() - timedelta(days=1))
    # 隐藏行立刻快照主键：后续 commit 会过期实例，再取 id 时它在可见性
    # 过滤下刷不回来（与 test_soft_delete_orphan_guard 的口径一致）
    hidden_id = hidden_child.id
    in_batch = _series(db, lib.id, "Show (2020)", tmdb_id="102",
                       merged_into_id=target_id)
    batch_id = in_batch.id
    grandchild = _series(db, lib.id, "Show (2021)", tmdb_id="102",
                         merged_into_id=batch_id)
    grand_id = grandchild.id

    # 目标与其中一个分身同批删：集合外的分身（可见+已隐藏）与指向
    # 集合内行的孙行都要解开；集合内互相指向的不用管（一起消失）。
    n = soft_delete.detach_merged_children(db, [target_id, batch_id])
    db.commit()

    assert _merged_into(db, hidden_id) is None, "已软删分身的指向同样不许悬空"
    assert _merged_into(db, grand_id) is None, "孙行指向待删行必须解开"
    assert n == 2


def test_detach_leaves_unrelated_pointers_alone(db):
    lib = _lib(db)
    target = _series(db, lib.id, "Show A", tmdb_id="103")
    survivor = _series(db, lib.id, "Show B", tmdb_id="104")
    child = _series(db, lib.id, "Show B (2020)", tmdb_id="104",
                    merged_into_id=survivor.id)
    target_id, survivor_id, child_id = target.id, survivor.id, child.id

    n = soft_delete.detach_merged_children(db, [target_id])
    db.commit()

    assert n == 0
    assert _merged_into(db, child_id) == survivor_id


# ==================== 2. 真删路径一：scanner._purge_items ====================

def test_purge_items_detaches_children_before_delete(db):
    """构造「目标行 + 合并来源行」，走 _purge_items 真删目标：
    来源行 merged_into_id 必须已清空且行还在、其名下集行可达。"""
    lib = _lib(db)
    target = _series(db, lib.id, "Show", tmdb_id="110")
    child = _series(db, lib.id, "Show (2020)", tmdb_id="110",
                    merged_into_id=target.id)
    season = _season(db, lib.id, child.id, 1)
    ep = _episode(db, lib.id, child.id, season.id, 1, 1)
    target_id, child_id, ep_id = target.id, child.id, ep.id

    scanner._purge_items(db, [target_id])
    db.commit()

    assert _row(db, target_id) is None, "目标行应已被物理删除"
    assert _row(db, child_id) is not None, "来源行必须还在"
    assert _merged_into(db, child_id) is None, "来源行的 merged_into 必须已解开"
    assert _row(db, ep_id) is not None, "来源行名下的集不许跟着没"


def test_purge_items_movie_family(db):
    lib = _lib(db, name="电影库")
    target = _movie(db, lib.id, "Film")
    child = _movie(db, lib.id, "Film (2020)", merged_into_id=target.id)
    target_id, child_id = target.id, child.id

    scanner._purge_items(db, [target_id])
    db.commit()

    assert _row(db, target_id) is None
    assert _row(db, child_id) is not None
    assert _merged_into(db, child_id) is None


# ==================== 3. 真删路径二：upstream_import._delete_rows 硬删分支 =====

def test_upstream_delete_rows_hard_branch_detaches(db, monkeypatch):
    monkeypatch.setenv("MEDIA_SOFT_DELETE", "0")
    lib = _lib(db)
    target = _series(db, lib.id, "Show", tmdb_id="120")
    child = _series(db, lib.id, "Show (2020)", tmdb_id="120",
                    merged_into_id=target.id)
    target_id, child_id = target.id, child.id
    target_row = _row(db, target_id)

    n = upstream_import._delete_rows(db, [target_row])
    db.commit()

    assert n == 1
    assert _row(db, target_id) is None, "目标行应已被物理删除"
    assert _row(db, child_id) is not None, "来源行必须还在"
    assert _merged_into(db, child_id) is None, "来源行的 merged_into 必须已解开"
