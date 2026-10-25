"""软删除：条目「下架」而不是消失（v2.48.0）

要钉住的是四件事：

1. **隐藏**：清理阶段只写 ``deleted_at``，条目行、播放进度、收藏都还在，只是所有 ORM
   查询都看不见它（全局可见性过滤器）；
2. **复活**：文件重新出现时扫描器把它放回来——不是插一条新行（那会撞 guid 唯一键，
   也会丢掉用户数据），而是清空 ``deleted_at``；
3. **回收**：隐藏够久（默认 30 天）才物理删除，表不会无限涨；
4. **可回滚**：``MEDIA_SOFT_DELETE=0`` 回到硬删，并且启动时把已隐藏的行放回来。

不碰网络、不碰生产库。

注意：被软删的行在 ORM 里**取不回来**（连 refresh 都会报「行已不存在」），所以用例里
一律先记住主键 id，再做隐藏动作。
"""
from __future__ import annotations

import os
import uuid
from datetime import datetime, timedelta

os.environ.setdefault("DATABASE_TYPE", "sqlite")
os.environ.setdefault("REDIS_ENABLED", "false")

import pytest
from sqlalchemy import create_engine, func, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend import models
from backend.emby_server import models as em
from backend.emby_server import scanner, soft_delete
from backend.integrations import store


@pytest.fixture(autouse=True)
def _grace_single_pass(monkeypatch):
    # 本文件测软删除主体，钉的是「确认缺失 → 下架」这一环；
    # 缺失宽限（批一 #6，默认 3 轮）会让单轮清理不下架，与本文件无关，
    # 压到 1 轮隔离变量（宽限语义在 test_missing_grace.py 里钉）
    monkeypatch.setattr(scanner, "MISSING_GRACE_SCANS", 1)


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


def _lib(db, name="电影库", paths=""):
    row = em.Library(guid=_guid(), name=name, collection_type="movies", paths=paths)
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def _item(db, lib_id, guid=None, path=None):
    guid = guid or _guid()
    row = em.MediaItem(guid=guid, library_id=lib_id, item_type="movie",
                       name=guid, file_path=path, size=4096)
    db.add(row)
    db.commit()
    return row.id


def _physical_count(db, **where) -> int:
    """绕过 ORM 过滤器数物理行：软删除之后行还在，删干净才不在"""
    sql = "SELECT COUNT(*) FROM emby_items"
    params = {}
    if where:
        sql += " WHERE " + " AND ".join(f"{k} = :{k}" for k in where)
        params = where
    return int(db.execute(text(sql), params).scalar() or 0)


def _visible_ids(db, lib_id) -> list:
    return [i.id for i in db.query(em.MediaItem.id).filter(
        em.MediaItem.library_id == lib_id).all()]


def _all_ids(db, lib_id) -> list:
    with soft_delete.include_deleted():
        return [i.id for i in db.query(em.MediaItem.id).filter(
            em.MediaItem.library_id == lib_id).all()]


# ==================== 开关与列 ====================

def test_column_exists_and_defaults_to_visible():
    assert em.MediaItem.__table__.c.deleted_at.default is None


def test_switch_defaults_to_on(monkeypatch):
    monkeypatch.delenv("MEDIA_SOFT_DELETE", raising=False)
    assert soft_delete.soft_delete_enabled() is True


@pytest.mark.parametrize("raw", ["0", "false", "no", "off"])
def test_switch_can_be_turned_off(monkeypatch, raw):
    monkeypatch.setenv("MEDIA_SOFT_DELETE", raw)
    assert soft_delete.soft_delete_enabled() is False


def test_empty_switch_value_means_default(monkeypatch):
    """环境变量写成空值（如 compose 里的 MEDIA_SOFT_DELETE:）按默认开启算"""
    monkeypatch.setenv("MEDIA_SOFT_DELETE", "")
    assert soft_delete.soft_delete_enabled() is True


def test_purge_days_are_env_tunable(monkeypatch):
    monkeypatch.setenv("MEDIA_SOFT_DELETE_PURGE_DAYS", "7")
    assert soft_delete.soft_delete_purge_days() == 7
    monkeypatch.setenv("MEDIA_SOFT_DELETE_PURGE_DAYS", "abc")
    assert soft_delete.soft_delete_purge_days() == 30, "非法值要回落，不能把功能打挂"


# ==================== 可见性：全局过滤 ====================

def test_soft_deleted_rows_disappear_from_orm_queries(db):
    lib = _lib(db)
    lib_id = lib.id
    kept = _item(db, lib_id)
    gone = _item(db, lib_id)

    soft_delete.mark_soft_deleted(db, [gone])
    db.commit()

    assert _visible_ids(db, lib_id) == [kept]
    assert db.query(func.count(em.MediaItem.id)).scalar() == 1


def test_include_deleted_reveals_them_again(db):
    """复活 / 恢复 / 删库这些路径要看得见隐藏行，否则它们会漏掉它们"""
    lib = _lib(db)
    lib_id = lib.id
    gone = _item(db, lib_id)

    soft_delete.mark_soft_deleted(db, [gone])
    db.commit()
    assert _visible_ids(db, lib_id) == []

    assert _all_ids(db, lib_id) == [gone]
    assert _visible_ids(db, lib_id) == [], "出了块就必须重新隐藏"


def test_count_visible_ignores_deleted_rows(db):
    """计数口径要跟着可见性走

    ``Query.count()`` 会把语句包成子查询，全局过滤器下不到那里去——所以数条目的地方
    必须用 ``count_visible``，否则会拿一个数不出下架条目的数字去填界面。
    """
    lib = _lib(db)
    lib_id = lib.id
    _item(db, lib_id, guid="m0")
    gone = _item(db, lib_id, guid="m1")
    db.commit()
    assert soft_delete.count_visible(db) == 2

    soft_delete.mark_soft_deleted(db, [gone])
    db.commit()

    assert soft_delete.count_visible(db) == 1
    assert soft_delete.count_visible(db, em.MediaItem.library_id == lib_id) == 1
    assert soft_delete.count_visible(db, em.MediaItem.library_id == 9999) == 0


def test_library_item_count_follows_visibility(db):
    """客户端拿到的 ChildCount / 卡片上的条目数不能比实际能浏览到的多"""
    from backend.emby_server import api

    lib = _lib(db)
    lib_id = lib.id
    _item(db, lib_id, guid="m0")
    gone = _item(db, lib_id, guid="m1")
    db.commit()
    lib.item_count = None          # 未扫完 / 失败过 → 走实时计数分支

    assert api._library_item_count(db, lib) == 2

    soft_delete.mark_soft_deleted(db, [gone])
    db.commit()

    assert api._library_item_count(db, lib) == 1


def test_bulk_update_is_not_filtered(db):
    """回收与批量 UPDATE 不能被过滤器挡掉，否则「删自己的软删行」会变成死循环"""
    lib = _lib(db)
    gone = _item(db, lib.id)

    soft_delete.mark_soft_deleted(db, [gone])
    db.commit()

    touched = db.query(em.MediaItem).filter(
        em.MediaItem.id == gone).update({"name": "改个名"}, synchronize_session=False)
    db.commit()

    assert touched == 1
    with soft_delete.include_deleted():
        assert db.query(em.MediaItem).filter(
            em.MediaItem.id == gone).one().name == "改个名"


# ==================== 清理阶段：标记而不是删 ====================

def test_cleanup_marks_instead_of_deleting(db):
    lib = _lib(db)
    lib_id = lib.id
    kept = _item(db, lib_id, guid="m0")
    gone = _item(db, lib_id, guid="m1")

    removed = scanner._remove_missing_items(db, lib, {"m0"})

    assert removed == 1
    assert _visible_ids(db, lib_id) == [kept]
    assert _physical_count(db, library_id=lib_id) == 2, "行必须还在，只是被隐藏"
    assert gone not in _visible_ids(db, lib_id)


def test_cleanup_keeps_user_data_for_a_coming_back_file(db):
    """下架一部片不该把用户的播放进度与收藏一起带走"""
    lib = _lib(db)
    lib_id = lib.id
    _item(db, lib_id, guid="m0")
    gone = _item(db, lib_id, guid="m1")
    db.add(em.UserMediaData(item_id=gone, user_id=1, playback_position_ticks=12345))
    db.commit()

    scanner._remove_missing_items(db, lib, {"m0"})

    assert db.query(em.UserMediaData).filter(
        em.UserMediaData.item_id == gone).count() == 1


def test_deleted_at_is_stamped(db):
    lib = _lib(db)
    lib_id = lib.id
    _item(db, lib_id, guid="m0")
    gone = _item(db, lib_id, guid="m1")

    before = datetime.now()
    scanner._remove_missing_items(db, lib, {"m0"})

    with soft_delete.include_deleted():
        row = db.query(em.MediaItem).filter(em.MediaItem.id == gone).one()
    assert row.deleted_at is not None and row.deleted_at >= before


def test_already_deleted_rows_are_not_walked_again(db):
    """已经下架的行不该每轮清理都再遍历、再统计一次"""
    lib = _lib(db)
    lib_id = lib.id
    _item(db, lib_id, guid="m0")
    _item(db, lib_id, guid="m1")

    scanner._remove_missing_items(db, lib, {"m0"})

    assert scanner._no_removals_possible(db, lib, {"m0"}) is True, \
        "有软删行时也必须走快速路径，否则每次扫描都要遍历整库"


def test_soft_deleted_rows_do_not_block_later_cleanups(db):
    """下架过的行不能当删除保护的分母

    否则一个删过 90 部的库，本轮正常看到剩下 10 部也会被判定成“来源整体不可用”，
    后面真正要清理的场景跟着一起被拦住。
    """
    lib = _lib(db)
    lib_id = lib.id
    kept = _item(db, lib_id, guid="m0")
    for i in range(1, 41):
        _item(db, lib_id, guid=f"g{i}")
    seen = {f"g{i}" for i in range(1, 41)} | {"m0"}

    soft_delete.mark_soft_deleted(db, [
        i.id for i in db.query(em.MediaItem.id).filter(
            em.MediaItem.guid.in_({f"g{i}" for i in range(1, 41)})).all()])
    db.commit()

    allowed, why, total = scanner._removal_budget(db, lib, seen)
    assert allowed is True, f"不该被拦住：{why}"
    assert total == 1, "预算只看可见行"

    # 再下一部：正常清理必须能发生（不被软删行拖累成「来源不可用」）
    gone = _item(db, lib_id, guid="m9")
    allowed, why, _total = scanner._removal_budget(db, lib, seen)
    assert allowed is True
    assert scanner._remove_missing_items(db, lib, seen - {"m9"}) == 1
    assert gone not in _visible_ids(db, lib_id)
    assert kept in _visible_ids(db, lib_id)


def test_soft_delete_does_not_loosen_the_removal_guard(db):
    """软删除不能变成「绕过删除保护的后门」：零结果仍然一条都不动"""
    lib = _lib(db)
    lib_id = lib.id
    for i in range(30):
        _item(db, lib_id, guid=f"m{i:02d}")

    assert scanner._remove_missing_items(db, lib, set()) == 0
    assert _physical_count(db, library_id=lib_id) == 30
    assert scanner._CLEANUP_LAST["skipped"]


# ==================== 回收：过期才物理删 ====================

def test_expired_rows_are_purged(db):
    lib = _lib(db)
    lib_id = lib.id
    old = _item(db, lib_id)
    fresh = _item(db, lib_id)
    db.add(em.UserMediaData(item_id=old, user_id=1, playback_position_ticks=1))
    db.commit()
    soft_delete.mark_soft_deleted(db, [old, fresh])
    with soft_delete.include_deleted():
        db.query(em.MediaItem).filter(em.MediaItem.id == old).update(
            {"deleted_at": datetime.now() - timedelta(days=40)}, synchronize_session=False)
    db.commit()

    assert soft_delete.purge_expired(db, purge_days=30, library_id=lib_id) == 1

    # 从属数据跟着走：回收的是「这条记录彻底不要了」，不是「藏起来」
    assert db.query(em.UserMediaData).filter(
        em.UserMediaData.item_id == old).count() == 0
    assert _physical_count(db, library_id=lib_id) == 1
    assert _all_ids(db, lib_id) == [fresh], "没到期的仍然只是隐藏"
    assert _visible_ids(db, lib_id) == [], "没到期的依然对用户隐藏"


def test_purge_can_be_disabled(db):
    lib = _lib(db)
    lib_id = lib.id
    gone = _item(db, lib_id)
    soft_delete.mark_soft_deleted(db, [gone])
    with soft_delete.include_deleted():
        db.query(em.MediaItem).filter(em.MediaItem.id == gone).update(
            {"deleted_at": datetime.now() - timedelta(days=3650)}, synchronize_session=False)
    db.commit()

    assert soft_delete.purge_expired(db, purge_days=0) == 0
    assert _physical_count(db, library_id=lib_id) == 1


def test_purge_does_not_touch_visible_rows(db):
    lib = _lib(db)
    lib_id = lib.id
    alive = _item(db, lib_id)

    assert soft_delete.purge_expired(db, purge_days=30, library_id=lib_id) == 0
    assert _visible_ids(db, lib_id) == [alive]


# ==================== 复活：文件回来 ====================

def test_resurrect_makes_the_row_visible_again(db):
    lib = _lib(db)
    lib_id = lib.id
    gone = _item(db, lib_id, guid="m1")

    soft_delete.mark_soft_deleted(db, [gone])
    db.commit()

    assert soft_delete.resurrect(db, [gone]) == 1

    assert _visible_ids(db, lib_id) == [gone]
    assert db.query(em.MediaItem).filter(em.MediaItem.id == gone).one().deleted_at is None


def test_scan_resurrects_instead_of_inserting_a_duplicate(db, tmp_path, monkeypatch):
    """文件重新出现：必须复用原来那一行（连播放进度一起），不能撞 guid 唯一键"""
    monkeypatch.setattr(scanner, "PROBE_BACKGROUND", True)
    monkeypatch.setattr(scanner, "SCAN_LAYERED", False)
    gone_movie = tmp_path / "Come Back (2024).mp4"
    kept_movie = tmp_path / "Still Here (2023).mp4"
    for path in (gone_movie, kept_movie):
        path.write_bytes(b"\x00" * 64)
    present = {"files": [gone_movie, kept_movie]}

    def fake_sources(snap, library, db, failed_roots, report=None):
        yield "test", iter([
            scanner.ScanFile(stored_path=str(p), name=p.name, local_dir=str(p.parent),
                             size=64, container="mp4")
            for p in present["files"]
        ])

    monkeypatch.setattr(scanner, "iter_scan_sources", fake_sources)
    lib = _lib(db, name="复活库", paths=str(tmp_path))
    lib_id = lib.id

    scanner.scan_library_sync(db, lib)
    item_id = next(i.id for i in db.query(em.MediaItem).filter(
        em.MediaItem.file_path == str(gone_movie)).all())
    db.add(em.UserMediaData(item_id=item_id, user_id=1, playback_position_ticks=999))
    db.commit()

    # 那一部真的从磁盘上撤了（库里还有另一部，所以清理阶段照常进行）
    gone_movie.unlink()
    present["files"] = [kept_movie]
    scanner.scan_library_sync(db, lib)
    assert item_id not in _visible_ids(db, lib_id)
    assert _physical_count(db, library_id=lib_id) == 2, "下架不是删除"

    # 补种回来了 → 同一条记录复活，播放进度还在
    gone_movie.write_bytes(b"\x00" * 64)
    present["files"] = [gone_movie, kept_movie]
    stats = scanner.scan_library_sync(db, lib)

    assert item_id in _visible_ids(db, lib_id), "必须是原来那一行，不是新插一条"
    assert _physical_count(db, library_id=lib_id) == 2, "不能多出一行"
    assert db.query(em.UserMediaData).filter(
        em.UserMediaData.item_id == item_id).count() == 1
    assert stats.get("resurrected") == 1


def test_resurrected_item_is_never_fast_skipped(db):
    """秒跳的前提是「这一行是可见的」：下架过的条目即使指纹没变也要走完整路径"""
    ctx = type("Ctx", (), {"snap": type("S", (), {"scrape_policy": "missing_only"})(),
                           "lib_id": 1})()
    monkeypatch_env = os.environ.get("SCAN_INCREMENTAL", "1")
    os.environ["SCAN_INCREMENTAL"] = "1"
    try:
        item = type("Row", (), {"deleted_at": datetime.now(), "repair_requested_at": None,
                                "size": 1, "duration_ticks": 10, "probe_status": "done",
                                "file_path": "/x.mkv"})()
        pending = type("P", (), {"item_type": "movie", "series_guid": None})()
        scan_file = type("F", (), {"stored_path": "/x.mkv", "size": 1})()

        assert scanner._can_skip_file(ctx, item, pending, "fp", "fp") is False
    finally:
        os.environ["SCAN_INCREMENTAL"] = monkeypatch_env


# ==================== 后台队列不该再碰下架的条目 ====================

def test_cleanup_hard_deletes_when_disabled(db, monkeypatch):
    monkeypatch.setenv("MEDIA_SOFT_DELETE", "0")
    lib = _lib(db)
    lib_id = lib.id
    _item(db, lib_id, guid="m0")
    gone = _item(db, lib_id, guid="m1")

    removed = scanner._remove_missing_items(db, lib, {"m0"})

    assert removed == 1
    assert _physical_count(db, library_id=lib_id) == 1
    assert gone not in _all_ids(db, lib_id)


def test_startup_restores_hidden_rows_when_disabled(db, monkeypatch):
    """关掉开关的那一刻必须先把隐藏行放回来，否则它们变成看不见也删不掉的僵尸"""
    from backend import database as db_mod

    lib = _lib(db)
    lib_id = lib.id
    hidden = _item(db, lib_id)
    soft_delete.mark_soft_deleted(db, [hidden])
    db.commit()
    assert _visible_ids(db, lib_id) == []

    monkeypatch.setenv("MEDIA_SOFT_DELETE", "0")
    monkeypatch.setattr(db_mod, "engine", db.get_bind())
    db_mod._resurrect_soft_deleted({"emby_items"})

    assert _visible_ids(db, lib_id) == [hidden]
    assert _physical_count(db, library_id=lib_id) == 1


def test_startup_resurrect_is_a_noop_while_enabled(db, monkeypatch):
    from backend import database as db_mod

    lib = _lib(db)
    lib_id = lib.id
    hidden = _item(db, lib_id)
    soft_delete.mark_soft_deleted(db, [hidden])
    db.commit()

    monkeypatch.setenv("MEDIA_SOFT_DELETE", "1")
    monkeypatch.setattr(db_mod, "engine", db.get_bind())
    db_mod._resurrect_soft_deleted({"emby_items"})

    assert _visible_ids(db, lib_id) == [], "开关还开着就不能擅自把下架的条目放回来"


def test_restore_all_puts_everything_back(db, monkeypatch):
    lib = _lib(db)
    lib_id = lib.id
    a = _item(db, lib_id)
    b = _item(db, lib_id)
    soft_delete.mark_soft_deleted(db, [a, b])
    db.commit()

    monkeypatch.setenv("MEDIA_SOFT_DELETE", "0")
    assert soft_delete.restore_all(db) == 2
    assert sorted(_visible_ids(db, lib_id)) == sorted([a, b])