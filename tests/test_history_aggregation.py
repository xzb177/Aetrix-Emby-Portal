"""观看历史聚合回归测试：单集观看记录按剧聚合，顶层只出现 movie/series。

用户要求（2026-09-25）：所有顶层页面只允许 Movie/Series；观看记录
（/media?tab=history）里的单集行也要收进剧集行，标注"第X季第X集 · 集名"，
点行进剧集详情并定位到该集。
"""
import os

os.environ.setdefault("DATABASE_TYPE", "sqlite")
os.environ.setdefault("REDIS_ENABLED", "false")

import uuid
from datetime import datetime, timedelta

import pytest

from backend.database import SessionLocal, init_db
from backend.emby_server import models as em
from backend.emby_server import portal
from backend.models import WebUser

init_db()


def _guid():
    return uuid.uuid4().hex


@pytest.fixture()
def db():
    s = SessionLocal()
    try:
        yield s
    finally:
        s.close()


@pytest.fixture()
def seed(db):
    """用户 u：看过 剧A S1E1（旧）、剧A S2E3（新）、电影M。返回 guid 字典。"""
    u = WebUser(username="hist_" + _guid()[:8], password_hash="x")
    db.add(u)
    db.flush()
    # xdist 并行下新用户可能复用刚被删用户的 id、继承其孤儿会话行，
    # 聚合结果会多出别人的条目；先清掉本 id 名下已有会话再造数。
    db.query(em.PlaybackSession).filter(
        em.PlaybackSession.user_id == u.id).delete()

    lib = em.Library(guid=_guid(), name="测试库", collection_type="tvshows")
    db.add(lib)
    db.flush()

    def mk(item_type, name, **kw):
        it = em.MediaItem(guid=_guid(), library_id=lib.id,
                          item_type=item_type, name=name, **kw)
        db.add(it)
        db.flush()
        return it

    series = mk("series", "测试剧A", production_year=2024,
                poster_path="/p/a.jpg")
    s1 = mk("season", "第 1 季", series_id=series.id, parent_id=series.id,
            season_number=1)
    s2 = mk("season", "第 2 季", series_id=series.id, parent_id=series.id,
            season_number=2)
    e11 = mk("episode", "第一集", series_id=series.id, parent_id=s1.id,
             season_number=1, episode_number=1, duration_ticks=24000000000)
    e23 = mk("episode", "第三集", series_id=series.id, parent_id=s2.id,
             season_number=2, episode_number=3, duration_ticks=24000000000)
    movie = mk("movie", "测试电影M", duration_ticks=60000000000)

    base = datetime(2026, 9, 25, 12, 0, 0)

    def sess(item, minutes_ago, pos=0):
        ps = em.PlaybackSession(
            session_key=_guid(), user_id=u.id, item_id=item.id,
            device_name="测试设备", client_name="测试客户端",
            last_update_at=base - timedelta(minutes=minutes_ago),
            position_ticks=pos)
        db.add(ps)
        db.flush()
        return ps

    sess(e11, 120)                       # 旧：剧A S1E1
    sess(e23, 10, pos=12000000000)       # 新：剧A S2E3（看到一半）
    sess(movie, 60, pos=6000000000)      # 电影M
    db.commit()
    return {"u": u, "series": series, "e11": e11, "e23": e23, "movie": movie}


def _history(db, seed, **kw):
    return portal.get_watch_history(seed["u"], db, **kw)


def test_episodes_aggregate_to_series(db, seed):
    res = _history(db, seed, limit=30, offset=0, item_type="")
    types = {i["type"] for i in res["items"]}
    assert types <= {"movie", "series"}, types
    # 剧A 只出现一行（最近看的 S2E3），电影一行
    assert len(res["items"]) == 2
    row = next(i for i in res["items"] if i["type"] == "series")
    assert row["id"] == seed["series"].guid
    assert row["name"] == "测试剧A"
    assert row["episode_id"] == seed["e23"].guid
    assert row["episode_name"] == "第三集"
    assert row["season_number"] == 2
    assert row["episode_number"] == 3
    # 进度取的是单集那次播放的位置
    assert row["position_ticks"] == 12000000000
    # 剧集海报来自剧集本身
    assert row["poster_url"] == f"/emby/Items/{seed['series'].guid}/Images/Primary"


def test_type_filter_series_includes_episodes_case_insensitive(db, seed):
    # 前端传首字母大写（Emby 惯例）；此前直接 == 比对小写库字段，筛选恒为空
    res = _history(db, seed, item_type="Series")
    assert len(res["items"]) == 1
    assert res["items"][0]["type"] == "series"
    res = _history(db, seed, item_type="Movie")
    assert len(res["items"]) == 1
    assert res["items"][0]["type"] == "movie"


def test_movie_row_unchanged(db, seed):
    res = _history(db, seed, item_type="")
    row = next(i for i in res["items"] if i["type"] == "movie")
    assert row["id"] == seed["movie"].guid
    assert "episode_id" not in row
    assert row["position_ticks"] == 6000000000


def test_series_row_prefers_session_position_over_zeroed_umd(db, seed):
    """回归：umd 因「标为已看」被清零时，聚合行的进度必须取会话的实际位置。

    PR #376 之前：`umd.playback_position_ticks if umd else session.position_ticks`
    —— umd 存在但被清零（0）时，进度显示 0，CI 报 `assert 0 == 12000000000`。
    修复后：会话有位置时优先用会话的。
    """
    # 给 e23 建一条被清零的 umd（模拟播完/标为已看）
    umd = em.UserMediaData(user_id=seed["u"].id, item_id=seed["e23"].id,
                           playback_position_ticks=0, played=True, play_count=1)
    db.add(umd)
    db.commit()

    res = _history(db, seed, limit=30, offset=0, item_type="")
    row = next(i for i in res["items"] if i["type"] == "series")
    # 会话里看到一半（12000000000），不能因为 umd 被清零就显示 0
    assert row["position_ticks"] == 12000000000
