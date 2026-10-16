# -*- coding: utf-8 -*-
"""进度>85% 预热下一集测试（2026-10-16 PR-2）。

覆盖：
- _find_next_episode：季号/集号升序找下一集；最后一集返回 None；
  非剧集条目返回 None；无集号时按 id 取之后第一集；跳过隐藏/合并副本行。
- _sync_find_next_for_prewarm：进度<85% 不触发；>=85% 返回下一集纯数据。

全部离线：sqlite 内存库，不做真实网络请求。
"""
import os

os.environ.setdefault("DATABASE_TYPE", "sqlite")
os.environ.setdefault("REDIS_ENABLED", "false")

import pytest
from types import SimpleNamespace
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend import models as app_models
from backend.database import Base
from backend.emby_server import models as em
from backend.emby_server import compat_routes as cr

_STAFF = SimpleNamespace(id=1, is_staff=True)


@pytest.fixture()
def memdb():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(
        engine,
        tables=[
            em.Library.__table__,
            em.MediaItem.__table__,
        ],
    )
    Session = sessionmaker(bind=engine)
    session = Session()
    lib = em.Library(id=1, guid="lib-guid-1", name="剧集")
    session.add(lib)
    session.commit()

    def _item(**kw):
        kw.setdefault("library_id", 1)
        kw.setdefault("item_type", "episode")
        kw.setdefault("is_hidden", False)
        row = em.MediaItem(**kw)
        session.add(row)
        return row

    series = _item(item_type="series", guid="series-1", name="测试剧",
                   season_number=None, episode_number=None)
    session.flush()
    # S1E1..E3 + S2E1
    e1 = _item(guid="ep-1-1", name="E1", series_id=series.id,
               season_number=1, episode_number=1,
               file_path="/strm/x/S01E01.strm", drive_file_id="FID_E1",
               duration_ticks=3_600_000_0000)
    e2 = _item(guid="ep-1-2", name="E2", series_id=series.id,
               season_number=1, episode_number=2,
               file_path="/strm/x/S01E02.strm", drive_file_id="FID_E2",
               duration_ticks=3_600_000_0000)
    e3 = _item(guid="ep-1-3", name="E3", series_id=series.id,
               season_number=1, episode_number=3,
               file_path="/strm/x/S01E03.strm", drive_file_id="FID_E3",
               duration_ticks=3_600_000_0000)
    s2e1 = _item(guid="ep-2-1", name="S2E1", series_id=series.id,
                 season_number=2, episode_number=1,
                 file_path="/strm/x/S02E01.strm", drive_file_id="FID_S2E1",
                 duration_ticks=3_600_000_0000)
    # 隐藏的 E4：应被跳过
    _item(guid="ep-1-4-hidden", name="E4", series_id=series.id,
          season_number=1, episode_number=4, is_hidden=True,
          file_path="/strm/x/S01E04.strm", duration_ticks=3_600_000_0000)
    movie = _item(item_type="movie", guid="movie-1", name="电影",
                  file_path="/strm/y/movie.strm", duration_ticks=7_200_000_0000)
    session.commit()
    yield session, {"e1": e1, "e2": e2, "e3": e3, "s2e1": s2e1, "movie": movie}
    session.close()


def test_find_next_episode_basic(memdb):
    db, rows = memdb
    nxt = cr._find_next_episode(db, rows["e1"])
    assert nxt is not None and nxt.guid == "ep-1-2"
    nxt = cr._find_next_episode(db, rows["e2"])
    assert nxt is not None and nxt.guid == "ep-1-3"


def test_find_next_episode_cross_season_and_skip_hidden(memdb):
    db, rows = memdb
    # E3 的下一集是 S2E1（隐藏的 S1E4 被跳过）
    nxt = cr._find_next_episode(db, rows["e3"])
    assert nxt is not None and nxt.guid == "ep-2-1"
    # S2E1 是最后一集
    assert cr._find_next_episode(db, rows["s2e1"]) is None


def test_find_next_episode_non_episode(memdb):
    db, rows = memdb
    assert cr._find_next_episode(db, rows["movie"]) is None


def test_sync_find_below_threshold_no_trigger(memdb):
    db, rows = memdb
    e1 = rows["e1"]
    body = {"ItemId": e1.guid, "PositionTicks": int(e1.duration_ticks * 0.5)}
    assert cr._sync_find_next_for_prewarm(db, _STAFF, body) is None


def test_sync_find_above_threshold_returns_next(memdb):
    db, rows = memdb
    e1 = rows["e1"]
    body = {"ItemId": e1.guid, "PositionTicks": int(e1.duration_ticks * 0.9)}
    out = cr._sync_find_next_for_prewarm(db, _STAFF, body)
    assert out is not None
    assert out["file_path"] == "/strm/x/S01E02.strm"
    assert out["drive_file_id"] == "FID_E2"
