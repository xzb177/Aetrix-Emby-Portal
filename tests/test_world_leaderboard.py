# -*- coding: utf-8 -*-
"""探险世界 · 观影碑榜单接口回归测试。

隔离：内存 SQLite + 直接调用路由函数，不碰开发库/生产库。

覆盖：
1. 热播榜：剧集按整剧聚合（单集 → series_id），已删除/已合并条目排除。
2. 时长榜：按用户总秒数排名；关掉上榜的用户不出现在榜上，但自己查 my_rank 仍返回真实名次。
3. 参数非法 → 400。
"""
import os

os.environ.setdefault("DATABASE_TYPE", "sqlite")
os.environ.setdefault("REDIS_ENABLED", "false")

from datetime import datetime, timedelta

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend import models
from backend.emby_server import models as em
from backend.emby_server import portal


@pytest.fixture()
def db():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    models.Base.metadata.create_all(engine)
    em.Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    try:
        yield session
    finally:
        session.rollback()
        session.close()
        engine.dispose()


def _user(db, name, show=True):
    u = models.WebUser(username=name, password_hash="x", is_active=True,
                       show_on_leaderboard=show)
    db.add(u)
    db.commit()
    db.refresh(u)
    return u


import uuid


def _lib(db):
    lib = em.Library(guid=uuid.uuid4().hex[:32], name="测试库")
    db.add(lib)
    db.commit()
    db.refresh(lib)
    return lib


def _item(db, name, item_type="Movie", series_id=None, deleted=False, merged=None):
    it = em.MediaItem(guid=uuid.uuid4().hex[:32], library_id=_lib(db).id,
                      name=name, item_type=item_type, series_id=series_id)
    db.add(it)
    db.commit()
    db.refresh(it)
    it_id = it.id
    if deleted or merged is not None:
        # 注意：软删除全局钩子会给所有 ORM 查询拼 deleted_at IS NULL，
        # 必须先建行 refresh，再标删除/合并，否则 refresh 查不到行。
        if deleted:
            it.deleted_at = datetime.now()
        if merged is not None:
            it.merged_into_id = merged
        db.commit()
    return it, it_id


def _session(db, user, item_id, start, dur_min=30, ended=True):
    if not isinstance(item_id, int):
        item_id = item_id.id
    s = em.PlaybackSession(user_id=user.id, item_id=item_id,
                           session_key=f"{user.id}-{item_id}-{start.timestamp()}",
                           start_time=start,
                           last_update_at=start + timedelta(minutes=dur_min))
    if ended:
        s.ended_at = start + timedelta(minutes=dur_min)
    db.add(s)
    db.commit()
    return s


def _today(hour=10):
    return datetime.now().replace(hour=hour, minute=0, second=0, microsecond=0)


def test_hot_board_aggregates_series_and_filters_dead(db):
    u = _user(db, "hot_u1")
    show, show_id = _item(db, "剧A", item_type="Series")
    ep1, _ = _item(db, "剧A E1", item_type="Episode", series_id=show_id)
    ep2, _ = _item(db, "剧A E2", item_type="Episode", series_id=show_id)
    movie, movie_id = _item(db, "电影B")
    _, dead_id = _item(db, "下架片", deleted=True)
    _, merged_id = _item(db, "副本", merged=movie_id)

    _session(db, u, ep1, _today(9))
    _session(db, u, ep2, _today(10))
    _session(db, u, ep1, _today(11))
    _session(db, u, movie, _today(12))
    _session(db, u, dead_id, _today(13))
    _session(db, u, merged_id, _today(14))

    res = portal.get_leaderboard("hot", "day", request_user=u, db=db)
    by_id = {r["item_id"]: r for r in res["items"]}
    # 剧A 的 3 次单集播放聚合成一条
    assert by_id[show_id]["count"] == 3
    assert by_id[movie_id]["count"] == 1
    # 下架/合并的不上榜
    assert dead_id not in by_id and merged_id not in by_id
    assert res["my_rank"] == 1


def test_duration_board_privacy_and_my_rank(db):
    a = _user(db, "dur_a", show=True)
    b = _user(db, "dur_b", show=False)  # 关掉上榜
    c = _user(db, "dur_c", show=True)
    movie, _ = _item(db, "电影C")
    _session(db, a, movie, _today(9), dur_min=60)
    _session(db, b, movie, _today(10), dur_min=120)
    _session(db, c, movie, _today(11), dur_min=30)

    res = portal.get_leaderboard("duration", "day", request_user=a, db=db)
    names = [r["nickname"] for r in res["items"]]
    assert "dur_b" not in names  # 关掉上榜的不出现
    assert names[0] == "dur_a"  # 60 分钟排第一（b 被排除）
    assert res["my_rank"] == 1

    # b 自己查：榜上没有他，但 my_rank 返回真实名次（120 分钟 > a 的 60 分钟 → 第 1）
    res_b = portal.get_leaderboard("duration", "day", request_user=b, db=db)
    assert "dur_b" not in [r["nickname"] for r in res_b["items"]]
    assert res_b["my_rank"] == 1


def test_leaderboard_bad_params_400(db):
    u = _user(db, "bad_u")
    with pytest.raises(HTTPException) as e:
        portal.get_leaderboard("nope", "day", request_user=u, db=db)
    assert e.value.status_code == 400
    with pytest.raises(HTTPException) as e:
        portal.get_leaderboard("hot", "month", request_user=u, db=db)
    assert e.value.status_code == 400
