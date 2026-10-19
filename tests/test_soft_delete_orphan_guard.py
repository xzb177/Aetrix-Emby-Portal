"""软删孤儿守卫：防治缺集普查 2026-10-19 桶 C2 的「孤儿集」。

背景：分身的合并目标 root 行被软删后，分身名下可见的季/集没有任何可见 root
的 family 能触达——整剧在库里不可见，集在 limbo（普查：265 集 / 20 系列，
14/15 抽样同根因）。

本文件钉住的不变量：**任何可见集行，必须能经 merged_into 反向链触达某个
可见的剧 root**（merged_into_id 为空的可见 series 行）。软删/回收某行之前，
rehome_merged_children 先把指向它的分身改指到链上其它可见行。

不碰网络、不碰生产库。
"""
from __future__ import annotations

import os
import uuid
from datetime import datetime, timedelta

os.environ.setdefault("DATABASE_TYPE", "sqlite")
os.environ.setdefault("REDIS_ENABLED", "false")

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend import models
from backend.emby_server import models as em
from backend.emby_server import soft_delete
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


def _series(db, lib_id, name, tmdb_id=None, merged_into_id=None,
            poster=None, overview=None, deleted_at=None):
    row = em.MediaItem(guid=_guid(), library_id=lib_id, item_type="series",
                       name=name, tmdb_id=tmdb_id, merged_into_id=merged_into_id,
                       poster_path=poster, overview=overview,
                       deleted_at=deleted_at)
    db.add(row)
    db.commit()
    # commit 会过期实例（含主键），随后取属性要走一次 refresh；
    # 生来就隐藏的行必须在 include_deleted 里刷，否则全局可见性过滤
    # 让 refresh 报「行已不存在」
    with soft_delete.include_deleted():
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


def _reachable_series_ids(db) -> set:
    """可见 root 经反向 merged_into 边能触达的可见 series id 集合。

    口径对齐播放器 family 语义的核心：family = {可见露面行} ∪
    {merged_into 链能走到的可见分身}。这里不复刻「准入」的名字/tmdb 判定，
    只验链不断——链不断是认亲的前提。
    """
    with soft_delete.include_deleted():
        rows = db.query(em.MediaItem.id, em.MediaItem.merged_into_id,
                        em.MediaItem.deleted_at).filter(
            em.MediaItem.item_type == "series").all()
    visible = {r[0] for r in rows if r[2] is None}
    merged = {r[0]: r[1] for r in rows if r[2] is None and r[1]}
    reached = {i for i in visible if i not in merged}
    changed = True
    while changed:
        changed = False
        for cid, pid in merged.items():
            if cid not in reached and pid in reached:
                reached.add(cid)
                changed = True
    return reached


def _orphan_episode_ids(db) -> list:
    """可见但没有任何可见 root 能触达的集 id 列表（应恒为空）。"""
    reachable = _reachable_series_ids(db)
    q = db.query(em.MediaItem.id).filter(em.MediaItem.item_type == "episode")
    if reachable:
        q = q.filter(~em.MediaItem.series_id.in_(reachable))
    return [r[0] for r in q.all()]


def _is_hidden(db, item_id) -> bool:
    # expire_all：mark_soft_deleted 用 synchronize_session=False 批量写，
    # session 里旧实例的 deleted_at 还是内存旧值，必须先过期再读库
    db.expire_all()
    with soft_delete.include_deleted():
        row = db.query(em.MediaItem).filter(em.MediaItem.id == item_id).first()
        return row is not None and row.deleted_at is not None


def _merged_into(db, item_id):
    db.expire_all()
    with soft_delete.include_deleted():
        return db.query(em.MediaItem.merged_into_id).filter(
            em.MediaItem.id == item_id).scalar()


# ==================== 1. 复现：旧路径确实造出孤儿 ====================

def test_repro_orphan_without_guard(db):
    """证据先行：绕过守卫直接 UPDATE（旧 mark_soft_deleted 行为），
    root 软删后分身名下的集变成孤儿——这正是普查里 14/20 例的根因。"""
    lib = _lib(db)
    root = _series(db, lib.id, "Wild 'N Out", tmdb_id="940241")
    clone = _series(db, lib.id, "Wild 'N Out (2020)", tmdb_id="940241",
                    merged_into_id=root.id)
    season = _season(db, lib.id, clone.id, 1)
    ep = _episode(db, lib.id, clone.id, season.id, 1, 1)
    assert _orphan_episode_ids(db) == []

    # 旧行为：直接标 deleted_at，不处理合并链
    db.query(em.MediaItem).filter(em.MediaItem.id == root.id).update(
        {"deleted_at": datetime.now()}, synchronize_session=False)
    db.commit()

    assert _orphan_episode_ids(db) == [ep.id], \
        "旧路径下软删 root 必须复现出孤儿集，否则复现不成立"


# ==================== 2. 守卫：改指到家族内另一个可见 root ====================

def test_guard_repoints_to_sibling_root(db):
    lib = _lib(db)
    root = _series(db, lib.id, "Wild 'N Out", tmdb_id="940241")
    sibling = _series(db, lib.id, "Wild N Out", tmdb_id="940241")
    clone = _series(db, lib.id, "Wild 'N Out (2020)", tmdb_id="940241",
                    merged_into_id=root.id)
    season = _season(db, lib.id, clone.id, 1)
    ep = _episode(db, lib.id, clone.id, season.id, 1, 1)
    # 先快照主键：_is_hidden/_merged_into 内部会 expire_all，
    # 之后再碰 row.id 会触发实例刷新（已隐藏的行在可见性过滤下刷不回来）
    root_id, sibling_id, clone_id = root.id, sibling.id, clone.id

    n = soft_delete.mark_soft_deleted(db, [root_id])
    assert n == 1
    assert _is_hidden(db, root_id)
    assert _merged_into(db, clone_id) == sibling_id
    assert _orphan_episode_ids(db) == []


def test_guard_admits_tmdb_and_name_candidates(db):
    """家族 root 准入（与 api._family_series_ids 一致）：tmdb 相同**或**
    归一化名相同都算。两个候选都命中时 pick_primary 按评分/id 取最优。"""
    lib = _lib(db)
    root = _series(db, lib.id, "Show", tmdb_id="111")
    by_tmdb = _series(db, lib.id, "Completely Different", tmdb_id="111")
    by_name = _series(db, lib.id, "Show")  # 同名但无 tmdb
    clone = _series(db, lib.id, "Show (2021)", tmdb_id="111",
                    merged_into_id=root.id)
    _season(db, lib.id, clone.id, 1)
    root_id, by_tmdb_id, clone_id = root.id, by_tmdb.id, clone.id

    soft_delete.mark_soft_deleted(db, [root_id])
    # 两个候选都准入；pick_primary 评分：by_tmdb 有 tmdb_id 占优
    assert _merged_into(db, clone_id) == by_tmdb_id
    assert _orphan_episode_ids(db) == []


# ==================== 3. 守卫：无家族 root 时提升最合适的分身 ====================

def test_guard_promotes_best_child(db):
    lib = _lib(db)
    root = _series(db, lib.id, "Show", tmdb_id="200")
    best = _series(db, lib.id, "Show (2020)", tmdb_id="200",
                   merged_into_id=root.id, poster="p.jpg", overview="简介")
    other = _series(db, lib.id, "Show 2020", tmdb_id="200",
                    merged_into_id=root.id)
    season = _season(db, lib.id, other.id, 1)
    ep = _episode(db, lib.id, other.id, season.id, 1, 1)
    root_id, best_id, other_id = root.id, best.id, other.id

    soft_delete.mark_soft_deleted(db, [root_id])

    assert _is_hidden(db, root_id)
    assert _merged_into(db, best_id) is None, "评分最高的分身应被提升为可见 root"
    assert _merged_into(db, other_id) == best_id
    assert _orphan_episode_ids(db) == []


# ==================== 4. 守卫：链中间节点被删，改指链顶 ====================

def test_guard_midchain_repoints_to_chain_top(db):
    lib = _lib(db)
    root = _series(db, lib.id, "Show", tmdb_id="300")
    mid = _series(db, lib.id, "Show (2020)", tmdb_id="300",
                  merged_into_id=root.id)
    leaf = _series(db, lib.id, "Show 2020", tmdb_id="300",
                   merged_into_id=mid.id)
    season = _season(db, lib.id, leaf.id, 1)
    ep = _episode(db, lib.id, leaf.id, season.id, 1, 1)
    root_id, mid_id, leaf_id = root.id, mid.id, leaf.id

    soft_delete.mark_soft_deleted(db, [mid_id])

    assert _is_hidden(db, mid_id)
    assert _merged_into(db, leaf_id) == root_id
    assert _orphan_episode_ids(db) == []


# ==================== 5. 批量：顺序无关，不造孤儿 ====================

def _build_chain(db, lib):
    root = _series(db, lib.id, "Show", tmdb_id="400")
    child = _series(db, lib.id, "Show (2020)", tmdb_id="400",
                    merged_into_id=root.id)
    grand = _series(db, lib.id, "Show 2020", tmdb_id="400",
                    merged_into_id=child.id)
    season = _season(db, lib.id, grand.id, 1)
    ep = _episode(db, lib.id, grand.id, season.id, 1, 1)
    return root, child, grand, ep


@pytest.mark.parametrize("order", ["rc", "cr"])
def test_guard_batch_order_independent(db, order):
    lib = _lib(db)
    root, child, grand, ep = _build_chain(db, lib)
    root_id, child_id, grand_id = root.id, child.id, grand.id
    ids = [root_id, child_id] if order == "rc" else [child_id, root_id]

    n = soft_delete.mark_soft_deleted(db, ids)
    assert n == 2
    assert _is_hidden(db, root_id) and _is_hidden(db, child_id)
    # 孙行必须仍有落点：指向可见行或自己成为 root
    g_target = _merged_into(db, grand_id)
    assert g_target is None or not _is_hidden(db, g_target), \
        f"孙行悬空指向已删行 id={g_target}"
    assert _orphan_episode_ids(db) == []


def test_guard_batch_no_dangling_merge_pointers(db):
    """同批里 root 和它的分身都被删（整棵子树文件都没了，扫描器会同批带走
    集行）：改指后随批删除，merged_into 不悬空指向已删行。"""
    lib = _lib(db)
    root = _series(db, lib.id, "Show", tmdb_id="500")
    clone = _series(db, lib.id, "Show (2020)", tmdb_id="500",
                    merged_into_id=root.id)
    season = _season(db, lib.id, clone.id, 1)
    ep = _episode(db, lib.id, clone.id, season.id, 1, 1)
    root_id, clone_id, ep_id = root.id, clone.id, ep.id

    n = soft_delete.mark_soft_deleted(db, [root_id, clone_id, ep_id])
    assert n == 3
    assert _is_hidden(db, root_id) and _is_hidden(db, clone_id)
    # 没有任何行的 merged_into 指向已隐藏/已删除的行
    with soft_delete.include_deleted():
        rows = db.query(em.MediaItem.id, em.MediaItem.merged_into_id,
                        em.MediaItem.deleted_at).all()
    by_id = {r[0]: (r[1], r[2]) for r in rows}
    for rid, (target, _dt) in by_id.items():
        if target is not None:
            assert target in by_id, f"行 {rid} 的 merged_into 悬空"
            assert by_id[target][1] is None, \
                f"行 {rid} 的 merged_into 指向已删行 {target}"


# ==================== 6. fail-closed：守卫失败则本批跳过该行 ====================

def test_guard_fail_closed_skips_row(db, monkeypatch):
    lib = _lib(db)
    bad = _series(db, lib.id, "Bad", tmdb_id="600")
    good = _series(db, lib.id, "Good", tmdb_id="601")
    bad_id, good_id = bad.id, good.id

    real = soft_delete.rehome_merged_children

    def boom(db_, item, skip_ids=frozenset()):
        if item.id == bad_id:
            raise RuntimeError("模拟守卫异常")
        return real(db_, item, skip_ids)

    monkeypatch.setattr(soft_delete, "rehome_merged_children", boom)
    n = soft_delete.mark_soft_deleted(db, [bad_id, good_id])

    assert n == 1
    assert not _is_hidden(db, bad_id), "守卫失败的行必须留在可见态"
    assert _is_hidden(db, good_id)


# ==================== 7. 回收：物理删除前同样改指 ====================

def test_purge_rehomes_before_hard_delete(db):
    lib = _lib(db)
    root = _series(db, lib.id, "Show", tmdb_id="700",
                   deleted_at=datetime.now() - timedelta(days=40))
    # 立刻快照：随后 commit 会过期实例，隐藏行的主键再取会刷不回来
    root_id = root.id
    clone = _series(db, lib.id, "Show (2020)", tmdb_id="700",
                    merged_into_id=root_id)
    season = _season(db, lib.id, clone.id, 1)
    ep = _episode(db, lib.id, clone.id, season.id, 1, 1)
    clone_id = clone.id

    n = soft_delete.purge_expired(db, purge_days=30)
    assert n == 1
    # root 物理消失
    with soft_delete.include_deleted():
        assert db.query(em.MediaItem).filter(
            em.MediaItem.id == root_id).first() is None
    # 分身被提升为可见 root，不悬空
    assert _merged_into(db, clone_id) is None
    assert _orphan_episode_ids(db) == []


def test_purge_skips_row_when_guard_fails(db, monkeypatch):
    lib = _lib(db)
    root = _series(db, lib.id, "Show", tmdb_id="701",
                   deleted_at=datetime.now() - timedelta(days=40))
    root_id = root.id  # 立刻快照：随后 commit 会过期实例，隐藏行刷不回来
    clone = _series(db, lib.id, "Show (2020)", tmdb_id="701",
                    merged_into_id=root_id)
    _season(db, lib.id, clone.id, 1)

    def boom(db_, item, skip_ids=frozenset()):
        raise RuntimeError("模拟守卫异常")

    monkeypatch.setattr(soft_delete, "rehome_merged_children", boom)
    n = soft_delete.purge_expired(db, purge_days=30)
    assert n == 0
    with soft_delete.include_deleted():
        assert db.query(em.MediaItem).filter(
            em.MediaItem.id == root_id).first() is not None
