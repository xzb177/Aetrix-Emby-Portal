"""从热播榜生成手工合集。

覆盖：日榜/周榜成员与榜单同一套 item_id 与名次；总闸关时与其它
合集管理口一样 404；空榜或条目都缺失时不建行、不 500。
每次调用都新建一份合集（没有稳定键）。隔离临时 SQLite，不连生产库。
"""

import os
import tempfile
from datetime import datetime, timedelta

os.environ.setdefault("DATABASE_TYPE", "sqlite")
os.environ.setdefault("REDIS_ENABLED", "false")
_fd, _tmppath = tempfile.mkstemp(suffix=".db")
os.close(_fd)
os.environ["DATABASE_URL"] = f"sqlite:///{_tmppath}"

from backend import database as _dbmod
from sqlalchemy import create_engine as _ce
from sqlalchemy.orm import sessionmaker as _sm

_dbmod.engine = _ce(os.environ["DATABASE_URL"])
_dbmod.configure_session_local(_sm(bind=_dbmod.engine))

import pytest

from backend import models as base_models
from backend.database import SessionLocal, init_db
from backend.emby_server import models as em
from backend.emby_server import media_sets as ms
from backend.emby_server import portal

init_db()

_URL = "/api/admin/media-sets/from-hot-leaderboard"


@pytest.fixture()
def db():
    s = SessionLocal()
    try:
        for table in (em.EmbyMediaSetMember, em.EmbyMediaSetRule,
                      em.EmbyMediaSet, em.EmbyMediaSetTemplate,
                      em.PlaybackSession, em.MediaItem, em.Library,
                      base_models.WebUser):
            s.query(table).delete()
        s.commit()
        yield s
    finally:
        s.close()


@pytest.fixture()
def admin(db):
    u = base_models.WebUser(username="hotsetadmin", is_staff=True,
                            is_active=True, password_hash="test")
    db.add(u)
    db.commit()
    db.refresh(u)
    return u


@pytest.fixture()
def client(db):
    from fastapi.testclient import TestClient

    from backend.database import get_db
    from backend.main import app

    def _override_get_db():
        _s = SessionLocal()
        try:
            yield _s
        finally:
            _s.close()

    app.dependency_overrides[get_db] = _override_get_db
    try:
        yield TestClient(app, raise_server_exceptions=False)
    finally:
        app.dependency_overrides.clear()


@pytest.fixture()
def authed(client, admin):
    from backend.api.admin_core import get_current_admin
    from backend.main import app

    app.dependency_overrides[get_current_admin] = lambda: admin
    try:
        yield client
    finally:
        del app.dependency_overrides[get_current_admin]


@pytest.fixture()
def gate_on(monkeypatch):
    monkeypatch.setattr(ms, "COLLECTIONS_ENABLED", 1)
    yield


def _lib(db):
    lib = em.Library(guid="lib-hot-board", name="热播库", is_enabled=True)
    db.add(lib)
    db.commit()
    db.refresh(lib)
    return lib


def _item(db, lib_id, name, item_type="movie", series_id=None):
    item = em.MediaItem(guid=f"item-{name}", library_id=lib_id,
                        item_type=item_type, name=name, series_id=series_id,
                        file_path=f"/data/{name}.mkv")
    db.add(item)
    db.commit()
    db.refresh(item)
    return item


def _play(db, user, item_id, start, n=1):
    for i in range(n):
        db.add(em.PlaybackSession(
            user_id=user.id, item_id=item_id,
            session_key=f"hot-{item_id}-{start.isoformat()}-{i}",
            start_time=start, last_update_at=start,
        ))
    db.commit()


def _member_ids(payload):
    members = sorted(payload["members"], key=lambda m: m["order_index"])
    return [m["item_id"] for m in members]


def test_hot_day_and_week_snapshot_matches_leaderboard(authed, db, admin, gate_on):
    """日榜/周榜成员的 item_id 与名次等于热播榜本身，剧集按整剧聚合。"""
    lib = _lib(db)
    series = _item(db, lib.id, "剧甲", item_type="series")
    episode = _item(db, lib.id, "剧甲E1", item_type="episode",
                    series_id=series.id)
    jia = _item(db, lib.id, "甲")
    yi = _item(db, lib.id, "乙")
    bing = _item(db, lib.id, "丙")
    ding = _item(db, lib.id, "丁")

    _, day_utc = portal.leaderboard_window("day")
    _, week_utc = portal.leaderboard_window("week")
    inside_day = day_utc + timedelta(hours=1)
    # 周窗口起点：周榜计入；只有周一（周起点=日起点）才同时落入日榜。
    _play(db, admin, episode.id, inside_day, n=4)
    _play(db, admin, jia.id, inside_day, n=3)
    _play(db, admin, bing.id, inside_day, n=2)
    _play(db, admin, yi.id, inside_day, n=1)
    _play(db, admin, ding.id, week_utc, n=5)

    monday = week_utc == day_utc
    day_expected = []
    if monday:
        day_expected.append(ding.id)
    day_expected.extend([series.id, jia.id, bing.id, yi.id])
    week_expected = [ding.id, series.id, jia.id, bing.id, yi.id]

    day = authed.post(_URL, json={"period": "day"})
    assert day.status_code == 200, day.text
    day_body = day.json()
    assert day_body["created"] is True
    assert day_body["source_type"] == "manual"
    assert day_body["board"] == "hot"
    assert day_body["period"] == "day"
    assert day_body["name"] == f"热播日榜 {day_body['window_start'][:10]}"
    assert _member_ids(day_body) == day_expected
    assert [m["order_index"] for m in sorted(
        day_body["members"], key=lambda m: m["order_index"])] == list(
            range(1, len(day_expected) + 1))
    assert all(m["member_source"] == "manual" for m in day_body["members"])

    board = portal.get_leaderboard(
        board="hot", period="day", request_user=admin, db=db)
    assert [row["item_id"] for row in board["items"]] == day_expected
    detail = authed.get(f"/api/admin/media-sets/{day_body['id']}")
    assert detail.status_code == 200, detail.text
    assert _member_ids(detail.json()) == day_expected

    week = authed.post(_URL, json={"period": "week"})
    assert week.status_code == 200, week.text
    week_body = week.json()
    assert week_body["name"] == f"热播周榜 {week_body['window_start'][:10]}"
    assert _member_ids(week_body) == week_expected
    week_board = portal.get_leaderboard(
        board="hot", period="week", request_user=admin, db=db)
    assert [row["item_id"] for row in week_board["items"]] == week_expected
    # 单集不入集，入集的是整剧。
    assert episode.id not in week_expected
    if not monday:
        assert ding.id not in day_expected


def test_snapshot_keeps_leaderboard_top20(authed, db, admin, gate_on):
    lib = _lib(db)
    _, day_utc = portal.leaderboard_window("day")
    start = day_utc + timedelta(hours=1)
    items = []
    for n in range(1, 22):
        item = _item(db, lib.id, f"片{n:02d}")
        _play(db, admin, item.id, start, n=n)
        items.append(item)
    # 次数 21 最高，次数 1 落在 Top20 之外。
    r = authed.post(_URL, json={"period": "day"})
    assert r.status_code == 200, r.text
    got = _member_ids(r.json())
    assert len(got) == 20
    assert got[0] == items[-1].id
    assert items[0].id not in got
    ranked, _totals = portal.hot_leaderboard_items(db, day_utc)
    assert got == [row["item_id"] for row in ranked]


def test_gate_off_matches_other_collection_admin_routes(authed, db, monkeypatch):
    monkeypatch.setattr(ms, "COLLECTIONS_ENABLED", 0)
    blocked = authed.post(_URL, json={"period": "day"})
    neighbor = authed.post("/api/admin/media-sets", json={"name": "不该建成"})
    assert blocked.status_code == 404
    assert neighbor.status_code == 404
    assert blocked.json()["detail"] == neighbor.json()["detail"]
    assert db.query(em.EmbyMediaSet).count() == 0


def test_empty_board_and_missing_items_do_not_crash(authed, db, admin, gate_on):
    empty = authed.post(_URL, json={"period": "week"})
    assert empty.status_code == 200, empty.text
    body = empty.json()
    assert body["created"] is False
    assert body["reason"] == "leaderboard_empty"
    assert body["members"] == []
    assert body["window_start"].endswith("T00:00:00")
    assert db.query(em.EmbyMediaSet).count() == 0

    lib = _lib(db)
    _, day_utc = portal.leaderboard_window("day")
    start = day_utc + timedelta(hours=2)
    # 先建完再标删除：中途 commit 会过期对象，软删钩子随后让 .id 刷新失败。
    primary = _item(db, lib.id, "主片")
    dead = _item(db, lib.id, "下架片")
    merged = _item(db, lib.id, "副本")
    series = _item(db, lib.id, "已删剧", item_type="series")
    episode = _item(db, lib.id, "已删剧E1", item_type="episode",
                    series_id=series.id)
    dead_id, merged_id, episode_id = dead.id, merged.id, episode.id
    dead.deleted_at = datetime.now()
    merged.merged_into_id = primary.id
    series.deleted_at = datetime.now()
    db.commit()
    _play(db, admin, dead_id, start, n=3)
    _play(db, admin, merged_id, start, n=2)
    _play(db, admin, episode_id, start, n=4)
    _play(db, admin, None, start, n=1)

    missing = authed.post(_URL, json={})
    assert missing.status_code == 200, missing.text
    assert missing.json()["created"] is False
    assert missing.json()["reason"] == "leaderboard_empty"
    assert db.query(em.EmbyMediaSet).count() == 0

    kept = _item(db, lib.id, "还在")
    _play(db, admin, kept.id, start, n=1)
    ok = authed.post(_URL, json={"period": "day", "name": " 只留还在 "})
    assert ok.status_code == 200, ok.text
    assert ok.json()["created"] is True
    assert ok.json()["name"] == "只留还在"
    assert _member_ids(ok.json()) == [kept.id]


def test_repeat_creates_new_and_rejects_bad_input(authed, db, admin, gate_on):
    lib = _lib(db)
    item = _item(db, lib.id, "丙片")
    _, day_utc = portal.leaderboard_window("day")
    _play(db, admin, item.id, day_utc + timedelta(minutes=5), n=1)

    first = authed.post(_URL, json={"period": "day"}).json()
    second = authed.post(_URL, json={"period": "day"}).json()
    assert first["created"] is True and second["created"] is True
    assert first["id"] != second["id"]
    assert db.query(em.EmbyMediaSet).count() == 2

    blank = authed.post(_URL, json={"name": "   "})
    assert blank.status_code == 400
    bad = authed.post(_URL, json={"period": "month"})
    assert bad.status_code == 400
    assert "period" in bad.json()["detail"]
    assert db.query(em.EmbyMediaSet).count() == 2
