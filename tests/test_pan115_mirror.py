# -*- coding: utf-8 -*-
"""FMBY #21：115 增量镜像（pan115_mirror）回归测试。

只用假 lister（内存目录树），不碰真 115、不读生产库、不起调度
线程。DB 用测试 sqlite（init_db），每用例独立 scope 隔离。
诚实口径：115 无原生推送/增量接口，本模块的“增量”指增量应用
（只有指纹变化的节点写事件），不是增量枚举——枚举仍需逐目录
列一次，测试 assert 的正是这一点：未变化节点不产生事件。
"""
from __future__ import annotations

import os
import uuid

import pytest

os.environ.setdefault("DATABASE_TYPE", "sqlite")
os.environ.setdefault("REDIS_ENABLED", "false")

from backend.database import SessionLocal, init_db
from backend.emby_server import models as em
from backend.emby_server import pan115_mirror as mirror

init_db()


def _scope():
    return "test:%s" % uuid.uuid4().hex[:12]


def _tree(entries_by_cid):
    def lister(cid):
        return [dict(e) for e in entries_by_cid.get(str(cid), [])]
    return lister


def _rows(db, scope):
    return db.query(em.Pan115RawNode).filter(
        em.Pan115RawNode.scope == scope).order_by(em.Pan115RawNode.id).all()


def _events(db, scope):
    return db.query(em.Pan115LifeEvent).filter(
        em.Pan115LifeEvent.scope == scope).order_by(
        em.Pan115LifeEvent.id).all()


def _cleanup(db, scope):
    db.query(em.Pan115LifeEvent).filter(
        em.Pan115LifeEvent.scope == scope).delete(synchronize_session=False)
    db.query(em.Pan115RawNode).filter(
        em.Pan115RawNode.scope == scope).delete(synchronize_session=False)
    db.query(em.Pan115SyncCheckpoint).filter(
        em.Pan115SyncCheckpoint.scope == scope).delete(
        synchronize_session=False)
    db.commit()


def _base_tree():
    return {
        "0": [
            {"cid": "10", "name": "Movies", "is_dir": True, "size": 0,
             "pickcode": ""},
            {"fid": "f1", "cid": "f1", "name": "a.mkv", "is_dir": False,
             "size": 10, "pickcode": "pc-a", "sha1": "sha-a"},
        ],
        "10": [
            {"fid": "f3", "cid": "f3", "name": "film.mkv", "is_dir": False,
             "size": 100, "pickcode": "pc-film", "sha1": "sha-film"},
        ],
    }


# ---------------------------------------------------------------------------
# 指纹与身份
# ---------------------------------------------------------------------------

def test_fingerprint_changes_on_size():
    a = {"name": "a.mkv", "size": 10, "sha1": "", "pickcode": "pc",
         "is_dir": False}
    b = dict(a, size=11)
    assert mirror.entry_fingerprint(a) == mirror.entry_fingerprint(dict(a))
    assert mirror.entry_fingerprint(a) != mirror.entry_fingerprint(b)


def test_node_key_dir_fid_and_no_fid_fallback():
    assert mirror.node_key_for(
        {"is_dir": True, "cid": "10"}, "") == ("dir:10", True)
    assert mirror.node_key_for(
        {"is_dir": False, "fid": "f1", "cid": "f1"}, "dir:0") == ("fid:f1", True)
    key, has_fid = mirror.node_key_for(
        {"is_dir": False, "fid": "", "cid": "", "name": "x.mkv"}, "dir:0")
    assert has_fid is False
    # 无 fid 绝不拿父目录 cid 冒充身份：键里是父键 + 文件名
    assert key == "path:dir:0:x.mkv"


# ---------------------------------------------------------------------------
# 枚举：递归、环 guard、同轮去重
# ---------------------------------------------------------------------------

def test_walk_full_recursive_and_cycle_guard():
    tree = _base_tree()
    tree["10"].append({"cid": "0", "name": "loop", "is_dir": True, "size": 0,
                        "pickcode": ""})
    nodes, truncated = mirror.walk_full(_tree(tree), "0")
    assert truncated is False
    keys = {n["node_key"] for n in nodes}
    assert keys == {"dir:10", "fid:f1", "fid:f3", "dir:0"}
    film = [n for n in nodes if n["node_key"] == "fid:f3"][0]
    assert film["path"] == "/Movies/film.mkv"
    assert film["parent_key"] == "dir:10"


def test_walk_full_dedupes_same_node_twice():
    tree = {"0": [
        {"fid": "f1", "cid": "f1", "name": "a.mkv", "is_dir": False,
         "size": 10, "pickcode": "pc-a"},
        {"fid": "f1", "cid": "f1", "name": "a.mkv", "is_dir": False,
         "size": 10, "pickcode": "pc-a"},
    ]}
    nodes, _ = mirror.walk_full(_tree(tree), "0")
    assert len(nodes) == 1


def test_walk_full_truncation_flag(monkeypatch):
    monkeypatch.setattr(mirror, "MAX_SYNC_ENTRIES", 2)
    nodes, truncated = mirror.walk_full(_tree(_base_tree()), "0")
    assert truncated is True
    assert len(nodes) == 2


# ---------------------------------------------------------------------------
# 代际应用：新增 / 不变 / 变更 / 删除 / 复活
# ---------------------------------------------------------------------------

def test_full_first_run_then_unchanged_produces_no_new_events():
    db = SessionLocal()
    scope = _scope()
    try:
        r1 = mirror.run_sync(db, scope=scope, lister=_tree(_base_tree()),
                             root_cid="0", mode="full")
        assert r1 == {"scope": scope, "generation": 1, "mode": "full",
                      "added": 3, "changed": 0, "deleted": 0,
                      "unchanged": 0, "truncated": False}
        assert len(_rows(db, scope)) == 3
        assert len(_events(db, scope)) == 3
        r2 = mirror.run_sync(db, scope=scope, lister=_tree(_base_tree()),
                             root_cid="0", mode="full")
        assert r2["generation"] == 2
        assert r2["unchanged"] == 3
        assert r2["added"] == r2["changed"] == r2["deleted"] == 0
        assert len(_events(db, scope)) == 3  # 未变化不写新事件
        assert len(_rows(db, scope)) == 3  # 重复跑不产生重复行
    finally:
        _cleanup(db, scope)
        db.close()


def test_changed_and_deleted_and_resurrection():
    db = SessionLocal()
    scope = _scope()
    try:
        mirror.run_sync(db, scope=scope, lister=_tree(_base_tree()),
                        root_cid="0", mode="full")
        tree2 = _base_tree()
        tree2["0"][1]["size"] = 20  # a.mkv 变了
        tree2["10"] = []  # film.mkv 删了
        r = mirror.run_sync(db, scope=scope, lister=_tree(tree2),
                            root_cid="0", mode="full")
        assert r["changed"] == 1 and r["deleted"] == 1
        types = [(e.node_key, e.event_type) for e in _events(db, scope)
                 if e.generation == 2]
        assert ("fid:f1", "changed") in types
        assert ("fid:f3", "deleted") in types
        film = [x for x in _rows(db, scope)
                if x.node_key == "fid:f3"][0]
        assert film.is_deleted is True  # 只标记，不物理删行
        # 复活：删后又出现，同一行翻回，事件为 added
        r3 = mirror.run_sync(db, scope=scope, lister=_tree(_base_tree()),
                             root_cid="0", mode="full")
        assert r3["added"] == 1
        film = [x for x in _rows(db, scope)
                if x.node_key == "fid:f3"][0]
        assert film.is_deleted is False
        assert len(_rows(db, scope)) == 3
    finally:
        _cleanup(db, scope)
        db.close()


def test_checkpoint_not_advanced_on_failure():
    db = SessionLocal()
    scope = _scope()
    try:
        def boom(cid):
            raise RuntimeError("115 挂了")
        with pytest.raises(RuntimeError):
            mirror.run_sync(db, scope=scope, lister=boom,
                            root_cid="0", mode="full")
        db.rollback()
        row = db.query(em.Pan115SyncCheckpoint).filter(
            em.Pan115SyncCheckpoint.scope == scope).first()
        assert row is None or row.last_generation == 0
    finally:
        _cleanup(db, scope)
        db.close()


# ---------------------------------------------------------------------------
# 增量：只列 watched，未列到的不标 deleted
# ---------------------------------------------------------------------------

def test_incremental_partial_listing_never_marks_unlisted_deleted():
    db = SessionLocal()
    scope = _scope()
    try:
        mirror.run_sync(db, scope=scope, lister=_tree(_base_tree()),
                        root_cid="0", mode="full")
        # 只列根目录：Movies 里的 film.mkv 未被列到，绝不能标 deleted
        r = mirror.run_sync(db, scope=scope, lister=_tree(_base_tree()),
                            root_cid="0", mode="incremental",
                            watched_cids=[("0", "/")])
        assert r["deleted"] == 0
        film = [x for x in _rows(db, scope)
                if x.node_key == "fid:f3"][0]
        assert film.is_deleted is False
        # 列 Movies 目录且其内容空了：这才是真删除
        tree2 = _base_tree()
        tree2["10"] = []
        r2 = mirror.run_sync(db, scope=scope, lister=_tree(tree2),
                             root_cid="0", mode="incremental",
                             watched_cids=[("10", "/Movies")])
        assert r2["deleted"] == 1
    finally:
        _cleanup(db, scope)
        db.close()


def test_incremental_default_watches_all_known_dirs():
    db = SessionLocal()
    scope = _scope()
    try:
        mirror.run_sync(db, scope=scope, lister=_tree(_base_tree()),
                        root_cid="0", mode="full")
        tree2 = _base_tree()
        tree2["10"][0]["size"] = 101
        r = mirror.run_sync(db, scope=scope, lister=_tree(tree2),
                            root_cid="0", mode="incremental")
        assert r["changed"] == 1
        assert r["deleted"] == 0
    finally:
        _cleanup(db, scope)
        db.close()


# ---------------------------------------------------------------------------
# 下游消费口
# ---------------------------------------------------------------------------

def test_events_since_and_dirty_directories():
    db = SessionLocal()
    scope = _scope()
    try:
        mirror.run_sync(db, scope=scope, lister=_tree(_base_tree()),
                        root_cid="0", mode="full")
        tree2 = _base_tree()
        tree2["10"][0]["size"] = 101
        mirror.run_sync(db, scope=scope, lister=_tree(tree2),
                        root_cid="0", mode="full")
        gen2 = mirror.events_since(db, scope, 2)
        assert [(e.node_key, e.event_type) for e in gen2] == [
            ("fid:f3", "changed")]
        assert mirror.dirty_directories(db, scope, 2) == ["/Movies"]
        assert mirror.events_since(db, scope, 99) == []
    finally:
        _cleanup(db, scope)
        db.close()


def test_start_returns_false_when_disabled(monkeypatch):
    monkeypatch.setattr(mirror, "PAN115_MIRROR_ENABLED", False)
    assert mirror.start() is False
