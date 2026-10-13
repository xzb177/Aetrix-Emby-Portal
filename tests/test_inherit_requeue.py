"""空壳 inherit 单集联动翻转（v2.58.0）

覆盖：
1. 父级剧新拿 tmdb_id → 名下空壳 inherit 子集翻回 pending（已有数据的子集不动）
2. 存量补排 requeue_stuck_inherit_children：同一谓词、跨多剧
3. 竞态兜底：抓取时父级无 id 被跳过、写库时父级已有 id → 打回 pending 短退避；
   重跑（抓取跑过）不再打回 → 无死循环
4. _enrich_apply 的跃迁标记：series 新拿 id 才置位，已有 id 不置位
"""
import itertools
from datetime import datetime, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend import models as base_models
from backend.database import Base
from backend.emby_server import enrich_worker as ew
from backend.emby_server import models as em

_seq = itertools.count(1)


@pytest.fixture()
def db():
    engine = create_engine("sqlite:///:memory:")
    # web_users 等表在 base_models.Base 上（emby_user_media_data 有 FK 指向它），
    # 两个 metadata 都要建（test_bind_tmdb 同口径）
    base_models.Base.metadata.create_all(engine)
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


@pytest.fixture(autouse=True)
def _no_probe(monkeypatch):
    # _maybe_queue_probe 会调 scanner.needs_probe 碰文件系统；联动测试不关心探测
    monkeypatch.setattr(ew, "_maybe_queue_probe", lambda db, item: None)


def _mk(db, item_type, name, **kw):
    i = next(_seq)
    defaults = dict(
        guid="inherit-requeue-%06d" % i,
        library_id=1,
        item_type=item_type,
        name=name,
        enrich_status="pending",
    )
    defaults.update(kw)
    m = em.MediaItem(**defaults)
    db.add(m)
    db.flush()
    return m


def _series(db, tmdb_id=None, **kw):
    return _mk(db, "series", "测试剧", tmdb_id=tmdb_id,
               poster_path="/p.jpg", primary_image_url="http://img/p.jpg",
               enrich_status="done", **kw)


def _shell_child(db, series, **kw):
    """空壳 inherit 子集：done+inherit、无简介、图片=父级物化拷贝"""
    defaults = dict(
        series_id=series.id, parent_id=None, season_number=1, episode_number=1,
        enrich_status="done", metadata_source="inherit",
        poster_path=series.poster_path,
        primary_image_url=series.primary_image_url,
    )
    defaults.update(kw)
    return _mk(db, "episode", "测试剧 第 1 集 S01E01", **defaults)


class TestFlipShellChildren:
    def test_shell_child_flipped(self, db):
        s = _series(db, tmdb_id="123")
        ep = _shell_child(db, s)
        db.commit()

        n = ew._requeue_inherit_children_for_parent(db, s.id)
        db.commit()

        assert n == 1
        db.refresh(ep)
        assert ep.enrich_status == "pending"
        assert ep.enrich_attempts == 0
        assert ep.enrich_next_retry_at is None
        assert ep.enrich_priority == ew.ENRICH_PRIORITY_RETRY_UNMATCHED
        # 父级物化拷贝被清掉，重跑的单集剧照才能落下来
        assert ep.poster_path is None
        assert ep.primary_image_url is None

    def test_child_with_overview_not_flipped(self, db):
        s = _series(db, tmdb_id="123")
        ep = _shell_child(db, s, overview="本集简介")
        db.commit()

        assert ew._requeue_inherit_children_for_parent(db, s.id) == 0
        db.refresh(ep)
        assert ep.enrich_status == "done"

    def test_child_with_own_image_not_flipped(self, db):
        s = _series(db, tmdb_id="123")
        ep = _shell_child(db, s, poster_path="/own-still.jpg",
                          primary_image_url="http://img/still.jpg")
        db.commit()

        assert ew._requeue_inherit_children_for_parent(db, s.id) == 0
        db.refresh(ep)
        assert ep.enrich_status == "done"
        # 自己的图不动
        assert ep.poster_path == "/own-still.jpg"

    def test_locked_child_not_flipped(self, db):
        s = _series(db, tmdb_id="123")
        ep = _shell_child(db, s, metadata_locked=True)
        db.commit()

        assert ew._requeue_inherit_children_for_parent(db, s.id) == 0
        db.refresh(ep)
        assert ep.enrich_status == "done"

    def test_non_inherit_child_not_flipped(self, db):
        s = _series(db, tmdb_id="123")
        ep = _shell_child(db, s, metadata_source="tmdb")
        db.commit()

        assert ew._requeue_inherit_children_for_parent(db, s.id) == 0
        db.refresh(ep)
        assert ep.enrich_status == "done"

    def test_parent_without_tmdb_id_noop(self, db):
        s = _series(db, tmdb_id=None)
        ep = _shell_child(db, s)
        db.commit()

        assert ew._requeue_inherit_children_for_parent(db, s.id) == 0
        db.refresh(ep)
        assert ep.enrich_status == "done"

    def test_no_reflip_after_data_arrives(self, db):
        """无循环：子集拿到数据后，再次触发不再翻转"""
        s = _series(db, tmdb_id="123")
        ep = _shell_child(db, s)
        db.commit()
        assert ew._requeue_inherit_children_for_parent(db, s.id) == 1
        # 模拟重跑拿到了单集简介
        ep.overview = "本集简介"
        ep.enrich_status = "done"
        db.commit()

        assert ew._requeue_inherit_children_for_parent(db, s.id) == 0


class TestBulkRequeue:
    def test_bulk_same_predicate(self, db):
        s1 = _series(db, tmdb_id="111")
        e1 = _shell_child(db, s1)
        e2 = _shell_child(db, s1, overview="有简介")
        s2 = _series(db, tmdb_id="222")
        e3 = _shell_child(db, s2)
        s3 = _series(db, tmdb_id=None)  # 父级还没 id：不动
        e4 = _shell_child(db, s3)
        db.commit()

        n = ew.requeue_stuck_inherit_children(db)

        assert n == 2
        for ep, expect in ((e1, "pending"), (e2, "done"),
                           (e3, "pending"), (e4, "done")):
            db.refresh(ep)
            assert ep.enrich_status == expect, ep.name


class TestRaceRepend:
    def _episode(self, db, series):
        return _mk(db, "episode", "测试剧 第 1 集 S01E01", series_id=series.id,
                   season_number=1, episode_number=1,
                   enrich_status="enriching", metadata_source=None,
                   file_path="/strm/s01e01.strm")

    def test_skipped_then_parent_has_id_repends(self, db):
        s = _series(db, tmdb_id="123")
        ep = self._episode(db, s)
        db.commit()
        fetched = {"pure_inherit": True,
                   "episode_tmdb_skipped_no_parent_id": True, "ok": True}

        ew._enrich_apply(db, ep, fetched)
        db.commit()

        db.refresh(ep)
        assert ep.enrich_status == "pending"
        assert ep.enrich_attempts == 0
        assert ep.enrich_next_retry_at is not None
        assert ep.enrich_next_retry_at <= datetime.now() + timedelta(seconds=600)
        assert ep.metadata_source == "inherit"

    def test_fetch_ran_but_no_episode_does_not_repend(self, db):
        """抓取跑过但 TMDB 没这集（无 skipped 标记）→ 确定性结论，不重跑"""
        s = _series(db, tmdb_id="123")
        ep = self._episode(db, s)
        db.commit()
        fetched = {"pure_inherit": True, "ok": True}  # 无 skipped 标记

        ew._enrich_apply(db, ep, fetched)
        db.commit()

        db.refresh(ep)
        assert ep.enrich_status == "done"
        assert ep.metadata_source == "inherit"

    def test_parent_still_without_id_stays_done(self, db):
        """父级写库时依然没 id → 正常终态，不打回"""
        s = _series(db, tmdb_id=None)
        ep = self._episode(db, s)
        db.commit()
        fetched = {"pure_inherit": True,
                   "episode_tmdb_skipped_no_parent_id": True, "ok": True}

        ew._enrich_apply(db, ep, fetched)
        db.commit()

        db.refresh(ep)
        assert ep.enrich_status == "done"


class TestTransitionFlag:
    def _series_pending(self, db, tmdb_id=None):
        return _mk(db, "series", "测试剧", tmdb_id=tmdb_id,
                   enrich_status="enriching")

    def test_new_tmdb_id_sets_flag(self, db):
        s = self._series_pending(db, tmdb_id=None)
        db.commit()
        fetched = {"tmdb_id": "123", "ok": True}

        ew._enrich_apply(db, s, fetched)

        assert fetched.get("_parent_tmdb_newly_acquired") == s.id
        assert s.tmdb_id == "123"

    def test_existing_tmdb_id_no_flag(self, db):
        s = self._series_pending(db, tmdb_id="123")
        db.commit()
        fetched = {"tmdb_id": "123", "ok": True}

        ew._enrich_apply(db, s, fetched)

        assert "_parent_tmdb_newly_acquired" not in fetched

    def test_episode_never_sets_flag(self, db):
        s = _series(db, tmdb_id="123")
        ep = _mk(db, "episode", "测试剧 第 1 集 S01E01", series_id=s.id,
                 enrich_status="enriching", tmdb_id=None)
        db.commit()
        fetched = {"tmdb_id": "999", "ok": True}

        ew._enrich_apply(db, ep, fetched)

        assert "_parent_tmdb_newly_acquired" not in fetched


class TestMaybeFlipWrapper:
    def test_wrapper_flips_on_flag(self, db):
        s = _series(db, tmdb_id="123")
        ep = _shell_child(db, s)
        db.commit()

        ew._maybe_flip_inherit_children(
            db, {"_parent_tmdb_newly_acquired": s.id})

        db.refresh(ep)
        assert ep.enrich_status == "pending"

    def test_wrapper_noop_without_flag(self, db):
        s = _series(db, tmdb_id="123")
        ep = _shell_child(db, s)
        db.commit()

        ew._maybe_flip_inherit_children(db, {})

        db.refresh(ep)
        assert ep.enrich_status == "done"
