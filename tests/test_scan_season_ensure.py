"""建集同建季行（缺集普查 B2，2026-10-19）。

``_ensure_season_row`` 回归：建集行时同事务内保证 (series, season_no) 的
季行存在——缺了就建；幂等（重复调用不建第二行）；并发安全（另一事务抢先
建了同 guid 季行时降级复用，不抛异常）；已软删/合并走的季行不算数。
"""
import os

os.environ.setdefault("DATABASE_TYPE", "sqlite")
os.environ.setdefault("REDIS_ENABLED", "false")

import uuid
from datetime import datetime
from types import SimpleNamespace

from backend.database import SessionLocal, init_db
from backend.emby_server import models as em
from backend.emby_server import mounts as _mounts  # noqa: F401 -- 必须先于 scanner 导入，避免循环导入
from backend.emby_server import scanner

init_db()


def _guid():
    return uuid.uuid4().hex


def _ctx():
    return SimpleNamespace(season_items={}, season_items_by_series={},
                                 new_series_ids=set())


def _make_lib(db):
    lib = em.Library(guid=_guid(), name="B2 Test", collection_type="tvshows",
                     paths="/tmp/b2", is_enabled=True)
    db.add(lib)
    db.flush()
    return lib


def _make_series(db, lib):
    s = em.MediaItem(guid=_guid(), library_id=lib.id, item_type="series",
                     name="B2 Show", date_added=datetime.now(),
                     probe_status="done")
    db.add(s)
    db.flush()
    return s


def _seasons(db, lib, series, season_no):
    return db.query(em.MediaItem).filter(
        em.MediaItem.library_id == lib.id,
        em.MediaItem.item_type == "season",
        em.MediaItem.series_id == series.id,
        em.MediaItem.season_number == season_no,
        em.MediaItem.deleted_at.is_(None),
    ).all()


def _cleanup(db, lib):
    ids = [r[0] for r in db.query(em.MediaItem.id).filter(
        em.MediaItem.library_id == lib.id).all()]
    if ids:
        db.query(em.MediaStream).filter(
            em.MediaStream.item_id.in_(ids)).delete(synchronize_session=False)
    db.query(em.MediaItem).filter(
        em.MediaItem.library_id == lib.id).delete(synchronize_session=False)
    db.query(em.Library).filter(em.Library.id == lib.id).delete()
    db.commit()


def test_ensure_creates_season_when_missing():
    """季行缺失时建出来，且与集行同属一个 series。"""
    db = SessionLocal()
    lib = _make_lib(db)
    series = _make_series(db, lib)
    try:
        ctx = _ctx()
        hint = "hint-b2-%s" % _guid()
        season = scanner._ensure_season_row(
            db, ctx, lib, series, 2, hint)
        db.commit()
        assert season.id is not None
        assert season.series_id == series.id
        assert season.season_number == 2
        assert season.parent_id == series.id
        rows = _seasons(db, lib, series, 2)
        assert len(rows) == 1 and rows[0].id == season.id
    finally:
        _cleanup(db, lib)
        db.close()


def test_ensure_soft_deleted_season_second_call_no_refresh_crash():
    """软删季行：同一 (剧, 季) 的第二集进来时不抛 InvalidRequestError。

    生产事故（2026-10-20）：动漫库（library 43，有 1255 个软删季行）
    连续三轮整库失败，错误均为 _ensure_season_row 里 db.refresh 抛
    sqlalchemy.exc.InvalidRequestError。
    复现链：_prepare_and_prefetch 用 include_deleted 把软删季行装进
    ctx.season_items；第一集走 refresh+expunge 分支（行脱离会话，但
    ctx.season_items 里的键还在）；第二集再从缓存里拿到这个脱离实例，
    走到 db.refresh(detached) 就炸。旧代码第二次调用必抛。
    """
    db = SessionLocal()
    lib = _make_lib(db)
    series = _make_series(db, lib)
    try:
        hint = "hint-softdel2-%s" % _guid()
        dead = em.MediaItem(
            guid=hint, library_id=lib.id, item_type="season",
            name="第 7 季", parent_id=series.id, series_id=series.id,
            season_number=7, date_added=datetime.now(), probe_status="done",
            deleted_at=datetime.now())
        db.add(dead)
        db.commit()

        # 模拟 _prepare_and_prefetch 的 _load_items(include_deleted)：
        # 软删季行被装进 guid 缓存。
        with scanner._soft_delete.include_deleted():
            cached = db.query(em.MediaItem).filter(
                em.MediaItem.guid == hint).first()
        assert cached is not None
        ctx = _ctx()
        ctx.season_items[hint] = cached

        s1 = scanner._ensure_season_row(db, ctx, lib, series, 7, hint)
        # 第二集：同一 (series, season_no)，缓存里还是第一集 expunge 后
        # 留下的脱离实例——旧代码在这里抛 InvalidRequestError。
        s2 = scanner._ensure_season_row(db, ctx, lib, series, 7, hint)
        db.commit()
        assert s1.id == s2.id == dead.id
        assert s1.season_number == 7
    finally:
        _cleanup(db, lib)
        db.close()


def test_ensure_is_idempotent():
    """重复调用不建第二行（幂等）。"""
    db = SessionLocal()
    lib = _make_lib(db)
    series = _make_series(db, lib)
    try:
        ctx = _ctx()
        hint = "hint-b2-%s" % _guid()
        s1 = scanner._ensure_season_row(db, ctx, lib, series, 1, hint)
        s2 = scanner._ensure_season_row(db, ctx, lib, series, 1, hint)
        db.commit()
        assert s1.id == s2.id
        assert len(_seasons(db, lib, series, 1)) == 1
    finally:
        _cleanup(db, lib)
        db.close()


def test_ensure_reuses_concurrent_insert():
    """另一事务抢先建了同 guid 季行：降级复用，不抛唯一键异常。"""
    db = SessionLocal()
    lib = _make_lib(db)
    series = _make_series(db, lib)
    try:
        hint3 = "hint-b2-%s" % _guid()
        other = em.MediaItem(
            guid=hint3, library_id=lib.id, item_type="season",
            name="第 3 季", parent_id=series.id, series_id=series.id,
            season_number=3, date_added=datetime.now(), probe_status="done")
        db.add(other)
        db.commit()

        ctx = _ctx()
        season = scanner._ensure_season_row(
            db, ctx, lib, series, 3, hint3)
        db.commit()
        assert season.id == other.id
        assert len(_seasons(db, lib, series, 3)) == 1
    finally:
        _cleanup(db, lib)
        db.close()


def test_ensure_ignores_soft_deleted_season_guid():
    """同 guid 的季行已被软删：可见查询不算数。

    注：guid 唯一键仍被软删行占着，建不出第二行——此时复用旧行（B1 死壳
    范畴，读侧虚拟季兜底），不断言新建，只断言不抛异常且返回一行。
    """
    db = SessionLocal()
    lib = _make_lib(db)
    series = _make_series(db, lib)
    try:
        hint4 = "hint-b2-%s" % _guid()
        dead = em.MediaItem(
            guid=hint4, library_id=lib.id, item_type="season",
            name="第 4 季", parent_id=series.id, series_id=series.id,
            season_number=4, date_added=datetime.now(), probe_status="done",
            deleted_at=datetime.now())
        db.add(dead)
        db.commit()

        ctx = _ctx()
        season = scanner._ensure_season_row(
            db, ctx, lib, series, 4, hint4)
        db.commit()
        assert season is not None and season.season_number == 4
    finally:
        _cleanup(db, lib)
        db.close()


def test_ensure_reuses_visible_season_with_other_guid():
    """同 (series, 季号) 已有可见季行（guid 不同，如历史目录 guid 漂移）：
    复用它，不建重复。"""
    db = SessionLocal()
    lib = _make_lib(db)
    series = _make_series(db, lib)
    try:
        old = em.MediaItem(
            guid="old-dir-derived-guid", library_id=lib.id, item_type="season",
            name="第 5 季", parent_id=series.id, series_id=series.id,
            season_number=5, date_added=datetime.now(), probe_status="done")
        db.add(old)
        db.commit()

        ctx = _ctx()
        hint5 = "hint-b2-%s" % _guid()
        season = scanner._ensure_season_row(
            db, ctx, lib, series, 5, hint5)
        db.commit()
        assert season.id == old.id
        assert len(_seasons(db, lib, series, 5)) == 1
    finally:
        _cleanup(db, lib)
        db.close()
