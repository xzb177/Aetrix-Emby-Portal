"""P3：NextUp 批量化（窗口函数 + NOT EXISTS）后与旧的逐剧查询结果逐条一致，且 SQL 条数不随剧数增长。"""
import os
import random
import uuid
from datetime import datetime, timedelta
from types import SimpleNamespace
from urllib.parse import urlencode

import pytest
from sqlalchemy import create_engine, event, or_
from sqlalchemy.orm import sessionmaker
from starlette.requests import Request

os.environ.setdefault("DATABASE_TYPE", "sqlite")
os.environ.setdefault("REDIS_ENABLED", "false")

from backend import models
from backend.database import Base
from backend.emby_server import api
from backend.emby_server import models as em


def _legacy_next_up_ids(db, user_id, allowed, limit):
    """升级前 get_next_up 的逐剧实现（原样搬来做对照基准）"""
    watched = (
        db.query(em.UserMediaData.item_id, em.UserMediaData.last_played_at)
        .filter(em.UserMediaData.user_id == user_id,
                or_(em.UserMediaData.played == True,  # noqa: E712
                    em.UserMediaData.playback_position_ticks > 0,
                    em.UserMediaData.play_count > 0))
        .all()
    )
    if not watched:
        return []
    watched_ids = {w.item_id for w in watched}
    last_played = {}
    for w in watched:
        if w.last_played_at and w.item_id not in last_played:
            last_played[w.item_id] = w.last_played_at
    ep_series = api._scope_items(
        db.query(em.MediaItem.id, em.MediaItem.series_id)
        .filter(em.MediaItem.id.in_(watched_ids), em.MediaItem.series_id.isnot(None)),
        allowed).all()
    series_ids = sorted({r.series_id for r in ep_series})
    series_recent = {}
    for eid, sid in ep_series:
        ts = last_played.get(eid)
        if ts and (sid not in series_recent or ts > series_recent[sid]):
            series_recent[sid] = ts
    ranked = []
    for sid in series_ids:
        nxt = (
            db.query(em.MediaItem)
            .filter(em.MediaItem.item_type == "episode", em.MediaItem.series_id == sid,
                    em.MediaItem.is_hidden == False,  # noqa: E712
                    ~em.MediaItem.id.in_(watched_ids))
            .order_by(em.MediaItem.season_number.asc().nullslast(),
                      em.MediaItem.episode_number.asc().nullslast(), em.MediaItem.id.asc())
            .first()
        )
        if nxt is not None:
            ranked.append((series_recent.get(sid), nxt))
    ranked.sort(key=lambda x: x[0] or datetime.min, reverse=True)
    return [e.id for _, e in ranked[:limit]]


@pytest.fixture(scope="module")
def seeded(tmp_path_factory):
    path = tmp_path_factory.mktemp("nextup") / "n.db"
    engine = create_engine(f"sqlite:///{path}")
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine)()
    rnd = random.Random(11)
    lib = em.Library(guid=uuid.uuid4().hex, name="剧", collection_type="tvshows", is_enabled=True)
    lib2 = em.Library(guid=uuid.uuid4().hex, name="剧2", collection_type="tvshows", is_enabled=True)
    db.add_all([lib, lib2])
    db.flush()
    users = []
    for u in range(3):
        wu = models.WebUser(username=f"nu{u}", password_hash="x", is_active=True)
        db.add(wu)
        users.append(wu)
    db.flush()
    t0 = datetime(2026, 3, 1)
    all_eps = []
    for s in range(60):
        L = lib if s % 5 else lib2
        series = em.MediaItem(guid=uuid.uuid4().hex, library_id=L.id, item_type="series", name=f"S{s}")
        db.add(series)
        db.flush()
        n_seasons = rnd.randint(1, 3)
        for sn in range(1, n_seasons + 1):
            season = em.MediaItem(guid=uuid.uuid4().hex, library_id=L.id, item_type="season",
                                  name=f"Season {sn}", series_id=series.id, season_number=sn)
            db.add(season)
            db.flush()
            for e in range(1, rnd.randint(2, 8)):
                ep = em.MediaItem(guid=uuid.uuid4().hex, library_id=L.id, item_type="episode",
                                  name=f"S{s} {sn}x{e}", series_id=series.id, parent_id=season.id,
                                  season_number=rnd.choice([sn, sn, None]) if s % 7 == 0 else sn,
                                  episode_number=rnd.choice([e, None]) if s % 11 == 0 else e,
                                  is_hidden=(rnd.random() < 0.05))
                db.add(ep)
                all_eps.append(ep)
    db.flush()
    for wu in users:
        for ep in rnd.sample(all_eps, 150):
            kind = rnd.choice(["played", "pos", "count", "none", "fav"])
            db.add(em.UserMediaData(
                user_id=wu.id, item_id=ep.id,
                played=(kind == "played"),
                playback_position_ticks=(10_000 if kind == "pos" else 0),
                play_count=(1 if kind == "count" else 0),
                is_favorite=(kind == "fav"),
                last_played_at=(t0 + timedelta(hours=rnd.choice([1, 2, 2, 3, 50, 100]))
                                if kind != "none" and rnd.random() < 0.85 else None),
            ))
    db.commit()
    yield SimpleNamespace(db=db, engine=engine, users=users, lib=lib, lib2=lib2)
    db.close()
    engine.dispose()


def _req(params):
    return Request({"type": "http", "method": "GET",
                    "query_string": urlencode(params).encode(), "headers": []})


@pytest.mark.parametrize("limit", [1, 3, 24, 100])
def test_next_up_matches_legacy(seeded, limit, monkeypatch):
    for wu in seeded.users:
        for allowed in (None, {seeded.lib.id}, {seeded.lib2.id}):
            monkeypatch.setattr(api, "_library_scope", lambda db, user, a=allowed: a)
            user = SimpleNamespace(id=wu.id, is_staff=False)
            res = api.get_next_up(_req({"Limit": limit}), user, seeded.db)
            got = [seeded.db.query(em.MediaItem.id).filter(em.MediaItem.guid == i["Id"]).scalar()
                   for i in res["Items"]]
            want = _legacy_next_up_ids(seeded.db, wu.id, allowed, limit)
            assert got == want, (wu.id, allowed, limit)
            # r50：TotalRecordCount 取全部可用下一集数（真实 Emby QueryResult 口径，
            # 与 F-c/F-d/F-e/F-f 一致；此前是当前页条数）。legacy 对照取全量做总数基准。
            full = _legacy_next_up_ids(seeded.db, wu.id, allowed, 10 ** 9)
            assert res["TotalRecordCount"] == len(full)


def test_next_up_sql_count_independent_of_series_count(seeded, monkeypatch):
    monkeypatch.setattr(api, "_library_scope", lambda db, user: None)
    n = {"c": 0}

    def count(*a, **k):
        n["c"] += 1

    event.listen(seeded.engine, "before_cursor_execute", count)
    try:
        api.get_next_up(_req({"Limit": 24}), SimpleNamespace(id=seeded.users[0].id, is_staff=False),
                        seeded.db)
    finally:
        event.remove(seeded.engine, "before_cursor_execute", count)
    # 旧实现：每部已开看的剧 1 条（这里 ~50 部）+ 若干；新实现与剧数无关
    assert n["c"] < 25, n["c"]


def test_series_id_branch_uses_same_unwatched_rule(seeded, monkeypatch):
    monkeypatch.setattr(api, "_library_scope", lambda db, user: None)
    db = seeded.db
    wu = seeded.users[1]
    for series in db.query(em.MediaItem).filter(em.MediaItem.item_type == "series").limit(15):
        res = api.get_next_up(_req({"SeriesId": series.guid}), SimpleNamespace(id=wu.id, is_staff=False), db)
        mp = api._next_up_first_unwatched(db, wu.id, [series.id])
        expect = [mp[series.id].guid] if series.id in mp else []
        assert [i["Id"] for i in res["Items"]] == expect
