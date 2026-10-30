"""360° 审计 R16：A-048（P1）＋A-049（P2）条目删除外键回归。

背景：生产是 PostgreSQL（FK 默认 NO ACTION），而 CI/冒烟的 SQLite 会话
从不设 PRAGMA foreign_keys=ON，导致管理端删条目、删库、软删回收三条
删除路径漏删 FK 子表的问题全程测不出来。本文件用 FK-ON 的 SQLite 会话
模拟 PG 强制语义，钉住：

- admin_delete_item 删条目时 9 张 FK 子表全清、子集级联、合并副本改指；
- delete_library 删库时 chunk 内 9 张 FK 子表全清；
- scanner._purge_items 补齐 5 张漏删子表；
- purge_expired 整批失败时逐行回落：毒单跳过记日志、其余行照常回收。

不碰网络、不碰生产库。旧代码上本文件用例必须全红（见回传红计数）。
"""

from __future__ import annotations

import os
import uuid
from datetime import datetime, timedelta

os.environ.setdefault("DATABASE_TYPE", "sqlite")
os.environ.setdefault("REDIS_ENABLED", "false")

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend import models
from backend.emby_server import models as em
from backend.emby_server import portal, scanner, soft_delete
from backend.emby_server import scan_queue as scan_queue_mod  # noqa: F401  (portal 侧门禁打点用)
from backend.integrations import store


@pytest.fixture()
def db():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False},
                           poolclass=StaticPool)

    @event.listens_for(engine, "connect")
    def _fk_on(dbapi_conn, _rec):
        # 模拟 PG：外键强制执行。仓库其余测试夹具不开，属本文件专属口径。
        dbapi_conn.execute("PRAGMA foreign_keys=ON")

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


def _user(db, name):
    row = models.WebUser(username=name, password_hash="x")
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def _mset(db, name="合集甲"):
    row = em.EmbyMediaSet(name=name)
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def _item(db, lib_id, item_type="series", name="剧甲", **kw):
    row = em.MediaItem(guid=_guid(), library_id=lib_id, item_type=item_type,
                       name=name, file_path="", **kw)
    db.add(row)
    db.commit()
    with soft_delete.include_deleted():
        db.refresh(row)
    return row


def _attach_all(db, item, user, mset):
    """给 item 挂上 9 张 FK 子表各一行（SeriesFollow 用 series_item_id）。"""
    db.add(em.MediaStream(item_id=item.id, stream_type="Video", codec="h264"))
    db.add(em.UserMediaData(user_id=user.id, item_id=item.id))
    db.add(em.ItemFacet(item_id=item.id, kind="genre", value="剧情"))
    db.add(em.EmbyPerson(item_id=item.id, name="演员甲"))
    db.add(em.PlaybackSession(user_id=user.id, item_id=item.id))
    db.add(em.IntroMarker(item_id=item.id, marker_type="intro",
                          start_ms=0, end_ms=1000))
    db.add(em.IntroSegment(item_id=item.id, segment_type="intro",
                           start_ms=0, end_ms=1000))
    db.add(em.EmbyMediaSetMember(set_id=mset.id, item_id=item.id))
    db.add(em.SeriesFollow(user_id=user.id, series_item_id=item.id))
    db.commit()


def _child_counts(db, item_id):
    with soft_delete.include_deleted():
        return {
            "stream": db.query(em.MediaStream).filter(
                em.MediaStream.item_id == item_id).count(),
            "umdata": db.query(em.UserMediaData).filter(
                em.UserMediaData.item_id == item_id).count(),
            "facet": db.query(em.ItemFacet).filter(
                em.ItemFacet.item_id == item_id).count(),
            "person": db.query(em.EmbyPerson).filter(
                em.EmbyPerson.item_id == item_id).count(),
            "session": db.query(em.PlaybackSession).filter(
                em.PlaybackSession.item_id == item_id).count(),
            "marker": db.query(em.IntroMarker).filter(
                em.IntroMarker.item_id == item_id).count(),
            "segment": db.query(em.IntroSegment).filter(
                em.IntroSegment.item_id == item_id).count(),
            "member": db.query(em.EmbyMediaSetMember).filter(
                em.EmbyMediaSetMember.item_id == item_id).count(),
            "follow": db.query(em.SeriesFollow).filter(
                em.SeriesFollow.series_item_id == item_id).count(),
        }


def test_fixture_really_enforces_foreign_keys(db):
    lib = _lib(db)
    item = _item(db, lib.id)
    db.add(em.MediaStream(item_id=item.id + 999999, stream_type="Video"))
    with pytest.raises(IntegrityError):
        db.commit()
    db.rollback()


def test_admin_delete_item_removes_all_child_rows(db):
    lib = _lib(db)
    user = _user(db, "u_a048_1")
    mset = _mset(db)
    item = _item(db, lib.id)
    item_id = item.id
    _attach_all(db, item, user, mset)
    assert all(v == 1 for v in _child_counts(db, item_id).values())

    portal.admin_delete_item(item.guid, staff=None, db=db)

    with soft_delete.include_deleted():
        assert db.query(em.MediaItem).filter(em.MediaItem.id == item_id).first() is None
    assert all(v == 0 for v in _child_counts(db, item_id).values())


def test_admin_delete_item_cascades_subtree(db):
    lib = _lib(db)
    user = _user(db, "u_a048_2")
    mset = _mset(db)
    series = _item(db, lib.id, "series", "剧乙")
    season = _item(db, lib.id, "season", "第 1 季",
                   series_id=series.id)
    episode = _item(db, lib.id, "episode", "第 1 集",
                    series_id=series.id, parent_id=season.id)
    series_id, season_id, episode_id = series.id, season.id, episode.id
    _attach_all(db, episode, user, mset)
    _attach_all(db, season, user, mset)

    portal.admin_delete_item(series.guid, staff=None, db=db)

    with soft_delete.include_deleted():
        for row_id in (series_id, season_id, episode_id):
            assert db.query(em.MediaItem).filter(
                em.MediaItem.id == row_id).first() is None
    assert all(v == 0 for v in _child_counts(db, episode_id).values())
    assert all(v == 0 for v in _child_counts(db, season_id).values())


def test_admin_delete_item_rehomes_merged_duplicate(db):
    lib = _lib(db)
    series = _item(db, lib.id, "series", "剧丙")
    dup = _item(db, lib.id, "series", "剧丙",
                merged_into_id=series.id)
    series_id, dup_id = series.id, dup.id

    portal.admin_delete_item(series.guid, staff=None, db=db)

    with soft_delete.include_deleted():
        assert db.query(em.MediaItem).filter(
            em.MediaItem.id == series_id).first() is None
        kept = db.query(em.MediaItem).filter(em.MediaItem.id == dup_id).first()
    assert kept is not None
    assert kept.merged_into_id is None


def test_delete_library_removes_all_child_rows(db, monkeypatch):
    lib = _lib(db)
    user = _user(db, "u_a048_3")
    mset = _mset(db)
    series = _item(db, lib.id, "series", "剧丁")
    episode = _item(db, lib.id, "episode", "第 1 集",
                    series_id=series.id)
    _attach_all(db, series, user, mset)
    _attach_all(db, episode, user, mset)
    dup = _item(db, lib.id, "series", "剧丁",
                merged_into_id=series.id)
    series_id, episode_id = series.id, episode.id

    monkeypatch.setattr(portal, "is_scan_active", lambda lib_id: False)
    monkeypatch.setattr(portal.scan_queue, "is_busy", lambda lib_id: False)
    result = portal.delete_library(lib.id, staff=None, db=db)

    assert result["success"] is True
    with soft_delete.include_deleted():
        assert db.query(em.MediaItem).filter(
            em.MediaItem.library_id == lib.id).count() == 0
        assert db.query(em.Library).filter(em.Library.id == lib.id).first() is None
    for row_id in (series_id, episode_id):
        assert all(v == 0 for v in _child_counts(db, row_id).values())


def test_purge_items_removes_all_child_rows(db):
    lib = _lib(db)
    user = _user(db, "u_a049_1")
    mset = _mset(db)
    item = _item(db, lib.id)
    item_id = item.id
    _attach_all(db, item, user, mset)

    scanner._purge_items(db, [item_id])
    db.commit()

    with soft_delete.include_deleted():
        assert db.query(em.MediaItem).filter(em.MediaItem.id == item_id).first() is None
    assert all(v == 0 for v in _child_counts(db, item_id).values())


def test_purge_expired_skips_poison_row_and_purges_rest(db, monkeypatch):
    lib = _lib(db)
    old = datetime.now() - timedelta(days=31)
    good = _item(db, lib.id, "movie", "片甲", deleted_at=old)
    good_id = good.id
    poison = _item(db, lib.id, "movie", "片乙", deleted_at=old)
    poison_id = poison.id
    real_purge = scanner._purge_items
    calls = {"n": 0}

    def _flaky(db_arg, ids):
        calls["n"] += 1
        if len(list(ids)) > 1:
            raise RuntimeError("bulk boom")
        if list(ids) == [poison_id]:
            raise RuntimeError("poison row boom")
        return real_purge(db_arg, ids)

    monkeypatch.setattr(scanner, "_purge_items", _flaky)
    purged = soft_delete.purge_expired(db, purge_days=30, library_id=lib.id)

    assert purged == 1
    with soft_delete.include_deleted():
        assert db.query(em.MediaItem).filter(
            em.MediaItem.id == good_id).first() is None
        assert db.query(em.MediaItem).filter(
            em.MediaItem.id == poison_id).first() is not None
