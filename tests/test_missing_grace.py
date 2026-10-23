"""缺失宽限（2026-10-24，FMBY 学习批一 #6 / 魂「对外部变化克制」）。

口径钉死：
- 单个文件确认缺失，先累计 missing_count，不当轮下架；
- 连续缺失达到 SCAN_MISSING_GRACE_SCANS（默认 3）轮才软删除；
- 文件重新出现 → 计数清零、missing_since 清空，旧账不连坐；
- 有条目挂着缺失计数时快速路径不许跳过遍历（否则清零/累计没人做）；
- SCAN_MISSING_GRACE_SCANS=1 时回到旧行为（确认缺失即下架）。

与删除保护（test_removal_guard.py）分工：保护管整库级事故，
宽限管单条级抖动，两者同时在场。
"""
import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend import models
from backend.emby_server import models as em
from backend.emby_server import scanner
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


def _lib(db):
    row = em.Library(guid="g-grace", name="电影库", collection_type="movies",
                     paths="", storage_backends="")
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def _item(db, lib, guid):
    row = em.MediaItem(guid=guid, library_id=lib.id, item_type="movie",
                       name=guid, file_path=None)
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def _state(db, item_id):
    return db.execute(text(
        "SELECT missing_count, missing_since, deleted_at FROM emby_items "
        "WHERE id = :i"), {"i": item_id}).first()


def _seed_library(db, n=30):
    """返回 (lib, [(id, guid), ...])——先把 id/guid 摘成普通值再跑清理：

    清理内部的 commit 会让 ORM 实例过期，软删除后实例刷新又会被全局
    可见性过滤挡掉（ObjectDeletedError），测试里不许再碰实例属性。
    """
    lib = _lib(db)
    items = []
    for i in range(n):
        row = _item(db, lib, f"m{i:02d}")
        items.append((row.id, row.guid))
        db.expunge(row)
    return lib, items


def test_first_miss_only_counts_not_removes(db):
    lib, items = _seed_library(db)
    seen = {g for _, g in items[1:]}          # m00 缺失，其余全在

    removed = scanner._remove_missing_items(db, lib, seen)

    assert removed == 0
    count, since, deleted = _state(db, items[0][0])
    assert count == 1 and since is not None and deleted is None
    assert scanner._CLEANUP_LAST["graced"] == 1


def test_third_consecutive_miss_removes(db):
    lib, items = _seed_library(db)
    seen = {g for _, g in items[1:]}

    assert scanner._remove_missing_items(db, lib, seen) == 0
    assert scanner._remove_missing_items(db, lib, seen) == 0
    count, since, deleted = _state(db, items[0][0])
    assert count == 2 and deleted is None

    assert scanner._remove_missing_items(db, lib, seen) == 1
    count, since, deleted = _state(db, items[0][0])
    assert deleted is not None, "连续缺失第 3 轮必须真正下架"


def test_return_resets_counter(db):
    lib, items = _seed_library(db)
    seen_without = {g for _, g in items[1:]}
    scanner._remove_missing_items(db, lib, seen_without)
    scanner._remove_missing_items(db, lib, seen_without)
    assert _state(db, items[0][0])[0] == 2

    seen_all = {g for _, g in items}          # 文件回来了
    assert scanner._remove_missing_items(db, lib, seen_all) == 0
    count, since, deleted = _state(db, items[0][0])
    assert count == 0 and since is None and deleted is None

    # 旧账清零后再缺失，必须从 1 重新累计，不许一轮就下架
    assert scanner._remove_missing_items(db, lib, seen_without) == 0
    assert _state(db, items[0][0])[0] == 1


def test_pending_grace_blocks_fast_path(db):
    lib, items = _seed_library(db)
    seen_all = {g for _, g in items}
    assert scanner._no_removals_possible(db, lib, seen_all) is True

    seen_without = {g for _, g in items[1:]}
    scanner._remove_missing_items(db, lib, seen_without)
    # 文件已全部回来（计数对得上）但有一条挂着缺失计数：
    # 快速路径必须让路给遍历去清零
    assert scanner._no_removals_possible(db, lib, seen_all) is False
    scanner._remove_missing_items(db, lib, seen_all)
    assert scanner._no_removals_possible(db, lib, seen_all) is True


def test_grace_one_is_legacy_behavior(db, monkeypatch):
    monkeypatch.setattr(scanner, "MISSING_GRACE_SCANS", 1)
    lib, items = _seed_library(db)
    seen = {g for _, g in items[1:]}

    assert scanner._remove_missing_items(db, lib, seen) == 1
    assert _state(db, items[0][0])[2] is not None
