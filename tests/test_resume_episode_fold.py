"""get_resume 单集折叠功能测试（r3）。

场景：用户看了一部剧的两集（S1E1、S1E2，先后有进度）和一部电影。
期望：Resume 返回 [剧集（1 次）, 电影]——没有 episode 条目；
同一部剧只出现一次（取最近一次播放的）。
"""
import os
import tempfile
import uuid
from datetime import datetime, timedelta
from types import SimpleNamespace

os.environ.setdefault("DATABASE_TYPE", "sqlite")
os.environ.setdefault("REDIS_ENABLED", "false")

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend import models as core  # noqa: F401
from backend.database import Base
from backend.emby_server import api, models as em


class _QP(dict):
    def getlist(self, key):
        v = self.get(key)
        return [v] if v else []


@pytest.fixture
def db():
    path = os.path.join(tempfile.mkdtemp(), "resumefold.db")
    engine = create_engine(f"sqlite:///{path}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    Session = sessionmaker(bind=engine, expire_on_commit=False)
    with Session() as s:
        yield s


@pytest.fixture
def scene(db):
    lib = em.Library(guid="F" * 32, name="剧库", collection_type="tvshows", paths="")
    db.add(lib)
    db.flush()
    now = datetime.now()

    def _item(**kw):
        kw.setdefault("guid", uuid.uuid4().hex[:32])
        kw.setdefault("library_id", lib.id)
        kw.setdefault("is_hidden", False)
        kw.setdefault("date_added", now)
        kw.setdefault("file_path", "/mnt/x/" + kw["name"])
        it = em.MediaItem(**kw)
        db.add(it)
        db.flush()
        return it

    series = _item(item_type="series", name="测试剧", sort_name="测试剧")
    ep1 = _item(item_type="episode", name="测试剧 S01E01", sort_name="测试剧 S01E01",
                series_id=series.id, parent_id=series.id)
    ep2 = _item(item_type="episode", name="测试剧 S01E02", sort_name="测试剧 S01E02",
                series_id=series.id, parent_id=series.id)
    movie = _item(item_type="movie", name="测试电影", sort_name="测试电影")
    user = core.WebUser(username="u_resumefold", password_hash="x")
    db.add(user)
    db.flush()

    def _progress(item, minutes_ago):
        db.add(em.UserMediaData(
            user_id=user.id, item_id=item.id,
            playback_position_ticks=1000, played=False,
            last_played_at=now - timedelta(minutes=minutes_ago),
        ))
    _progress(ep1, 60)
    _progress(ep2, 10)   # 最近一次看的是 E2
    _progress(movie, 30)
    db.commit()
    return SimpleNamespace(db=db, user=user, series=series, movie=movie)


def _call(db, user, **params):
    req = SimpleNamespace(query_params=_QP(params), base_url="http://test/")
    with pytest.MonkeyPatch().context() as mp:
        mp.setattr(api, "_library_scope", lambda db, user: None)
        mp.setattr(api, "_prefetch_list_data", lambda *a, **k: None)
        mp.setattr(api, "_item_dto",
                   lambda e, base, uid, db: {"Id": e.guid, "Name": e.name,
                                             "Type": e.item_type,
                                             # 调试字段：断言折叠时选中的源条目
                                             "_src_id": e.id})
        return api.get_resume(req, user, db)


def test_episodes_fold_into_series(scene):
    res = _call(scene.db, scene.user)
    types = [it["Type"] for it in res["Items"]]
    assert "episode" not in types, f"单集不应出现在 Resume：{types}"
    names = [it["Name"] for it in res["Items"]]
    assert names.count("测试剧") == 1, f"同一部剧只应出现一次：{names}"
    assert "测试电影" in names
    assert res["TotalRecordCount"] == len(res["Items"]) == 2


def test_episode_without_series_is_dropped(scene):
    """series_id 为空 / 剧集不可见的单集直接丢弃，不崩。"""
    orphan = em.MediaItem(guid=uuid.uuid4().hex[:32], library_id=1,
                          item_type="episode", name="孤儿单集",
                          sort_name="孤儿单集", is_hidden=False,
                          date_added=datetime.now(), file_path="/mnt/x/orphan.mkv",
                          series_id=None)
    scene.db.add(orphan)
    scene.db.flush()
    scene.db.add(em.UserMediaData(user_id=scene.user.id, item_id=orphan.id,
                                  playback_position_ticks=500, played=False,
                                  last_played_at=datetime.now()))
    scene.db.commit()
    res = _call(scene.db, scene.user)
    names = [it["Name"] for it in res["Items"]]
    assert "孤儿单集" not in names


def test_resume_order_by_last_played_desc(db):
    """折叠后整体顺序严格按 last_played_at 倒序：S2(10min) > M(30min) > S1(60min)。"""
    lib = em.Library(guid="E" * 32, name="剧库2", collection_type="tvshows", paths="")
    db.add(lib)
    db.flush()
    now = datetime.now()

    def _mk(**kw):
        kw.setdefault("guid", uuid.uuid4().hex[:32])
        kw.setdefault("library_id", lib.id)
        kw.setdefault("is_hidden", False)
        kw.setdefault("date_added", now)
        kw.setdefault("file_path", "/mnt/x/" + kw["name"])
        it = em.MediaItem(**kw)
        db.add(it)
        db.flush()
        return it

    s1 = _mk(item_type="series", name="S1", sort_name="S1")
    s1e1 = _mk(item_type="episode", name="S1E01", sort_name="S1E01",
               series_id=s1.id, parent_id=s1.id)
    s2 = _mk(item_type="series", name="S2", sort_name="S2")
    s2e1 = _mk(item_type="episode", name="S2E01", sort_name="S2E01",
               series_id=s2.id, parent_id=s2.id)
    movie = _mk(item_type="movie", name="M", sort_name="M")

    user = core.WebUser(username="u_resumeorder", password_hash="x")
    db.add(user)
    db.flush()

    def _prog(item, minutes_ago):
        db.add(em.UserMediaData(
            user_id=user.id, item_id=item.id,
            playback_position_ticks=1000, played=False,
            last_played_at=now - timedelta(minutes=minutes_ago),
        ))

    _prog(s1e1, 60)   # S1 最近一次播放：60 分钟前
    _prog(s2e1, 10)   # S2 最近一次播放：10 分钟前
    _prog(movie, 30)  # 电影 M：30 分钟前
    db.commit()

    res = _call(db, user)
    names = [it["Name"] for it in res["Items"]]
    src_ids = [it["_src_id"] for it in res["Items"]]
    assert names == ["S2", "M", "S1"], f"必须严格按 last_played_at 倒序：{names}"
    assert src_ids == [s2.id, movie.id, s1.id], f"折叠选中来源不对：{src_ids}"
    assert names.count("S1") == 1 and names.count("S2") == 1, "同一部剧只应出现一次"
    assert res["TotalRecordCount"] == len(res["Items"]) == 3
