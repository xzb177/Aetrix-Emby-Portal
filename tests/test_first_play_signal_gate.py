# -*- coding: utf-8 -*-
"""浏览预热信号门：只看不播的浏览，条目无真实播放信号则不预热。

信号口径（本地 DB，零 Drive 请求）：条目本身有观看记录（看过 /
有进度 / 有播放次数）、同剧任一集有观看记录、或有 active 的
追剧订阅。门关（FIRST_PLAY_WARM_SIGNAL_GATE=0）回旧行为全暖；
真实起播的 mode="next" 下一集预热不受门控。
"""
import os
import tempfile
import uuid
from datetime import datetime
from types import SimpleNamespace

os.environ.setdefault("DATABASE_TYPE", "sqlite")
os.environ.setdefault("REDIS_ENABLED", "false")

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend import models as core  # noqa: F401 — 注册 web_users
from backend.database import Base
from backend.emby_server import models as em
from backend.emby_server import first_play as fp


@pytest.fixture(autouse=True)
def _clear_inflight():
    fp._inflight.clear()
    yield
    fp._inflight.clear()


@pytest.fixture
def world(monkeypatch):
    path = os.path.join(tempfile.mkdtemp(), "signal-gate.db")
    engine = create_engine(
        f"sqlite:///{path}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    Session = sessionmaker(bind=engine, expire_on_commit=False)
    monkeypatch.setattr("backend.database.SessionLocal", Session)
    monkeypatch.delenv("FIRST_PLAY_WARM_SIGNAL_GATE", raising=False)
    with Session() as s:
        lib = em.Library(guid="S" * 32, name="库",
                         collection_type="tvshows", paths="")
        s.add(lib)
        s.flush()

        def _item(**kw):
            kw.setdefault("guid", uuid.uuid4().hex[:32])
            kw.setdefault("library_id", lib.id)
            kw.setdefault("is_hidden", False)
            kw.setdefault("date_added", datetime.now())
            kw.setdefault("file_path", "/mnt/x/" + kw["name"])
            kw.setdefault("sort_name", kw["name"])
            it = em.MediaItem(**kw)
            s.add(it)
            s.flush()
            return it

        series = _item(item_type="series", name="在看剧")
        ep1 = _item(item_type="episode", name="在看剧 S01E01",
                    series_id=series.id, parent_id=series.id,
                    season_number=1, episode_number=1)
        ep2 = _item(item_type="episode", name="在看剧 S01E02",
                    series_id=series.id, parent_id=series.id,
                    season_number=1, episode_number=2)
        fresh = _item(item_type="series", name="没碰过剧")
        fep1 = _item(item_type="episode", name="没碰过剧 S01E01",
                     series_id=fresh.id, parent_id=fresh.id,
                     season_number=1, episode_number=1)
        fep2 = _item(item_type="episode", name="没碰过剧 S01E02",
                     series_id=fresh.id, parent_id=fresh.id,
                     season_number=1, episode_number=2)
        movie = _item(item_type="movie", name="路过电影")
        user = core.WebUser(
            username="u_gate_" + uuid.uuid4().hex[:8], password_hash="x")
        s.add(user)
        s.flush()
        s.commit()
        yield SimpleNamespace(
            db=s, user=user, series=series, ep1=ep1, ep2=ep2,
            fresh=fresh, fep1=fep1, fep2=fep2, movie=movie)


class _WarmRec:
    def __init__(self):
        self.item_ids = []

    def __call__(self, db, item, user):
        self.item_ids.append(item.id)
        return {"ok": True, "reason": "warmed"}


@pytest.fixture
def rec(monkeypatch):
    r = _WarmRec()
    monkeypatch.setattr(fp, "warm_first_play_sync", r)
    return r


def _progress(db, user, item, *, played=False, ticks=0, count=0):
    db.add(em.UserMediaData(
        user_id=user.id, item_id=item.id, played=played,
        playback_position_ticks=ticks, play_count=count,
        hide_from_resume=False, last_played_at=datetime.now()))
    db.commit()


# ---- 无信号：浏览触发零预热（warm_first_play_sync 一次都不许打） ----

def test_detail_movie_without_signal_no_warm(world, rec):
    fp._warm_thread_body(world.movie.id, world.user.id, "detail")
    assert rec.item_ids == []


def test_detail_series_without_signal_no_warm(world, rec):
    fp._warm_thread_body(world.fresh.id, world.user.id, "detail")
    assert rec.item_ids == []


def test_detail_series_cancelled_follow_no_warm(world, rec):
    world.db.add(em.SeriesFollow(
        user_id=world.user.id, series_item_id=world.fresh.id,
        status="cancelled"))
    world.db.commit()
    fp._warm_thread_body(world.fresh.id, world.user.id, "detail")
    assert rec.item_ids == []


# ---- 有信号：保持预热 ----

def test_detail_movie_with_resume_progress_warms(world, rec):
    _progress(world.db, world.user, world.movie, ticks=5000)
    fp._warm_thread_body(world.movie.id, world.user.id, "detail")
    assert rec.item_ids == [world.movie.id]


def test_detail_series_watched_warms_first_unwatched(world, rec):
    _progress(world.db, world.user, world.ep1, played=True, count=1)
    fp._warm_thread_body(world.series.id, world.user.id, "detail")
    assert rec.item_ids == [world.ep2.id]


def test_detail_episode_of_watched_series_warms(world, rec):
    _progress(world.db, world.user, world.ep1, played=True, count=1)
    fp._warm_thread_body(world.ep2.id, world.user.id, "detail")
    assert rec.item_ids == [world.ep2.id]


def test_detail_series_followed_warms(world, rec):
    world.db.add(em.SeriesFollow(
        user_id=world.user.id, series_item_id=world.fresh.id,
        status="active"))
    world.db.commit()
    fp._warm_thread_body(world.fresh.id, world.user.id, "detail")
    assert rec.item_ids == [world.fep1.id]


# ---- 开关切回旧行为：浏览也预热 ----

def test_gate_off_restores_browse_warm(world, rec, monkeypatch):
    monkeypatch.setenv("FIRST_PLAY_WARM_SIGNAL_GATE", "0")
    fp._warm_thread_body(world.movie.id, world.user.id, "detail")
    assert rec.item_ids == [world.movie.id]


# ---- 播放主链路不受门控：真实起播的下一集照暖 ----

def test_next_mode_not_gated(world, rec):
    fp._warm_thread_body(world.fep1.id, world.user.id, "next")
    assert rec.item_ids == [world.fep2.id]
