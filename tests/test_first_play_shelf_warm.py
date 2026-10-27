# -*- coding: utf-8 -*-
"""首页货架预热 + 预热写入 exists 正缓存。

Resume / Latest / NextUp 各最多暖 1 条；spawn 抛错不影响列表。
预热 206 且有 fid 时写 exists 正缓存（本进程 put 或跨进程 publish）。
不打谷歌、不按播放器名分支。
"""
import json
import os
import tempfile
import time
import uuid
from datetime import datetime, timedelta
from types import SimpleNamespace

os.environ.setdefault("DATABASE_TYPE", "sqlite")
os.environ.setdefault("REDIS_ENABLED", "false")

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend import models as core  # noqa: F401 — 注册 web_users
from backend.database import Base
from backend.emby_server import api, models as em
from backend.emby_server import first_play as fp


class _Rec:
    def __init__(self):
        self.calls = []

    def __call__(self, item_id, user_id, mode="detail", *, source="", self_target=False):
        self.calls.append({
            "item_id": item_id,
            "user_id": user_id,
            "mode": mode,
            "source": source,
            "self_target": self_target,
        })
        return True


@pytest.fixture
def db():
    path = os.path.join(tempfile.mkdtemp(), "shelf-warm.db")
    engine = create_engine(
        f"sqlite:///{path}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    Session = sessionmaker(bind=engine, expire_on_commit=False)
    with Session() as s:
        yield s


@pytest.fixture
def shelf(db):
    lib = em.Library(guid="S" * 32, name="库", collection_type="tvshows", paths="")
    db.add(lib)
    db.flush()
    now = datetime.now()

    def _item(**kw):
        kw.setdefault("guid", uuid.uuid4().hex[:32])
        kw.setdefault("library_id", lib.id)
        kw.setdefault("is_hidden", False)
        kw.setdefault("date_added", now)
        kw.setdefault("file_path", "/mnt/x/" + kw["name"])
        kw.setdefault("sort_name", kw["name"])
        it = em.MediaItem(**kw)
        db.add(it)
        db.flush()
        return it

    series = _item(item_type="series", name="测试剧")
    ep = _item(item_type="episode", name="测试剧 S01E02", series_id=series.id,
               parent_id=series.id, season_number=1, episode_number=2)
    ep_next = _item(item_type="episode", name="测试剧 S01E03", series_id=series.id,
                    parent_id=series.id, season_number=1, episode_number=3)
    series_b = _item(item_type="series", name="另一剧",
                     date_added=now - timedelta(days=2))
    ep_b1 = _item(item_type="episode", name="另一剧 S01E01", series_id=series_b.id,
                  parent_id=series_b.id, season_number=1, episode_number=1,
                  date_added=now - timedelta(days=2))
    ep_b2 = _item(item_type="episode", name="另一剧 S01E02", series_id=series_b.id,
                  parent_id=series_b.id, season_number=1, episode_number=2,
                  date_added=now - timedelta(days=2))
    movie = _item(item_type="movie", name="测试电影",
                  date_added=now - timedelta(hours=3))
    user = core.WebUser(username="u_shelf_" + uuid.uuid4().hex[:8], password_hash="x")
    db.add(user)
    db.flush()
    db.commit()
    return SimpleNamespace(
        db=db, user=user, series=series, ep=ep, ep_next=ep_next,
        series_b=series_b, ep_b1=ep_b1, ep_b2=ep_b2, movie=movie, now=now)


def _req(**params):
    return SimpleNamespace(query_params=params, base_url="http://test/")


def _quiet(monkeypatch):
    monkeypatch.setattr(api, "_library_scope", lambda db, user: None)
    monkeypatch.setattr(api, "_prefetch_list_data", lambda *a, **k: None)
    monkeypatch.setattr(api, "_base_url", lambda request: "http://test/")
    monkeypatch.setattr(
        api, "_item_dto",
        lambda e, base, uid, db, **kw: {
            "Id": e.guid, "Name": e.name, "Type": e.item_type})


def _progress(db, user, item, *, minutes_ago=0, played=False, ticks=1000):
    db.add(em.UserMediaData(
        user_id=user.id, item_id=item.id,
        playback_position_ticks=0 if played else ticks,
        played=played, play_count=1 if played else 0,
        hide_from_resume=False,
        last_played_at=datetime.now() - timedelta(minutes=minutes_ago),
    ))
    db.commit()


def test_resume_warms_in_progress_episode_not_the_series(shelf, monkeypatch):
    _quiet(monkeypatch)
    rec = _Rec()
    monkeypatch.setattr(fp, "spawn_first_play_warm", rec)
    _progress(shelf.db, shelf.user, shelf.ep, minutes_ago=1)
    _progress(shelf.db, shelf.user, shelf.movie, minutes_ago=30)
    res = api.get_resume(_req(), shelf.user, shelf.db)
    names = [it["Name"] for it in res["Items"]]
    assert names[0] == "测试剧"
    assert "episode" not in [it["Type"] for it in res["Items"]]
    assert len(rec.calls) == 1
    call = rec.calls[0]
    assert call["item_id"] == shelf.ep.id
    assert call["item_id"] != shelf.series.id
    assert call["user_id"] == shelf.user.id
    assert call["mode"] == "detail"
    assert call["source"] == "resume"
    assert call["self_target"] is True


def test_resume_warms_movie_when_it_is_the_first_card(shelf, monkeypatch):
    _quiet(monkeypatch)
    rec = _Rec()
    monkeypatch.setattr(fp, "spawn_first_play_warm", rec)
    _progress(shelf.db, shelf.user, shelf.movie, minutes_ago=1)
    _progress(shelf.db, shelf.user, shelf.ep, minutes_ago=40)
    res = api.get_resume(_req(), shelf.user, shelf.db)
    assert res["Items"][0]["Name"] == "测试电影"
    assert len(rec.calls) == 1
    assert rec.calls[0]["item_id"] == shelf.movie.id
    assert rec.calls[0]["source"] == "resume"
    assert rec.calls[0]["self_target"] is True


def test_resume_empty_does_not_warm(db, monkeypatch):
    _quiet(monkeypatch)
    rec = _Rec()
    monkeypatch.setattr(fp, "spawn_first_play_warm", rec)
    user = core.WebUser(username="u_empty_" + uuid.uuid4().hex[:8], password_hash="x")
    db.add(user)
    db.commit()
    res = api.get_resume(_req(), user, db)
    assert res["Items"] == []
    assert rec.calls == []


def test_resume_spawn_error_still_returns(shelf, monkeypatch):
    _quiet(monkeypatch)
    _progress(shelf.db, shelf.user, shelf.movie, minutes_ago=1)

    def boom(*a, **k):
        raise RuntimeError("warm down")

    monkeypatch.setattr(fp, "spawn_first_play_warm", boom)
    res = api.get_resume(_req(), shelf.user, shelf.db)
    assert res["Items"][0]["Name"] == "测试电影"
    assert res["TotalRecordCount"] == 1


def test_latest_warms_only_first_series_as_detail(shelf, monkeypatch):
    _quiet(monkeypatch)
    rec = _Rec()
    monkeypatch.setattr(fp, "spawn_first_play_warm", rec)
    res = api.get_latest(_req(), shelf.user, shelf.db)
    assert isinstance(res, list) and len(res) > 1
    assert res[0]["Name"] == "测试剧"
    assert len(rec.calls) == 1
    assert rec.calls[0]["item_id"] == shelf.series.id
    assert rec.calls[0]["mode"] == "detail"
    assert rec.calls[0]["source"] == "latest"
    assert rec.calls[0]["self_target"] is False


def test_latest_movie_first_warms_self(db, monkeypatch):
    _quiet(monkeypatch)
    lib = em.Library(guid="M" * 32, name="电影", collection_type="movies", paths="")
    db.add(lib)
    db.flush()
    now = datetime.now()
    movie = em.MediaItem(
        guid=uuid.uuid4().hex[:32], library_id=lib.id, item_type="movie",
        name="新电影", sort_name="新电影", is_hidden=False,
        date_added=now, file_path="/mnt/x/m.mkv")
    older = em.MediaItem(
        guid=uuid.uuid4().hex[:32], library_id=lib.id, item_type="series",
        name="旧剧", sort_name="旧剧", is_hidden=False,
        date_added=now - timedelta(days=1), file_path="/mnt/x/s")
    user = core.WebUser(username="u_lat_" + uuid.uuid4().hex[:8], password_hash="x")
    db.add_all([movie, older, user])
    db.commit()
    rec = _Rec()
    monkeypatch.setattr(fp, "spawn_first_play_warm", rec)
    res = api.get_latest(_req(), user, db)
    assert res[0]["Name"] == "新电影"
    assert len(rec.calls) == 1
    assert rec.calls[0]["item_id"] == movie.id
    assert rec.calls[0]["source"] == "latest"
    assert rec.calls[0]["self_target"] is True
    assert rec.calls[0]["mode"] == "detail"


def test_latest_spawn_error_still_returns(shelf, monkeypatch):
    _quiet(monkeypatch)

    def boom(*a, **k):
        raise RuntimeError("warm down")

    monkeypatch.setattr(fp, "spawn_first_play_warm", boom)
    res = api.get_latest(_req(), shelf.user, shelf.db)
    assert isinstance(res, list) and res


def test_next_up_warms_only_first_episode(shelf, monkeypatch):
    _quiet(monkeypatch)
    rec = _Rec()
    monkeypatch.setattr(fp, "spawn_first_play_warm", rec)
    _progress(shelf.db, shelf.user, shelf.ep, minutes_ago=5, played=True, ticks=0)
    _progress(shelf.db, shelf.user, shelf.ep_b1, minutes_ago=50, played=True, ticks=0)
    res = api.get_next_up(_req(), shelf.user, shelf.db)
    assert [it["Name"] for it in res["Items"]][:1] == ["测试剧 S01E03"]
    assert len(res["Items"]) >= 2
    assert len(rec.calls) == 1
    assert rec.calls[0]["item_id"] == shelf.ep_next.id
    assert rec.calls[0]["source"] == "next_up"
    assert rec.calls[0]["self_target"] is True
    assert rec.calls[0]["mode"] == "detail"
    assert rec.calls[0]["user_id"] == shelf.user.id


def test_next_up_series_id_warms_that_episode(shelf, monkeypatch):
    _quiet(monkeypatch)
    rec = _Rec()
    monkeypatch.setattr(fp, "spawn_first_play_warm", rec)
    res = api.get_next_up(
        _req(SeriesId=shelf.series_b.guid), shelf.user, shelf.db)
    assert res["Items"][0]["Name"] == "另一剧 S01E01"
    assert len(rec.calls) == 1
    assert rec.calls[0]["item_id"] == shelf.ep_b1.id
    assert rec.calls[0]["source"] == "next_up"
    assert rec.calls[0]["self_target"] is True


def test_next_up_empty_does_not_warm(db, monkeypatch):
    _quiet(monkeypatch)
    rec = _Rec()
    monkeypatch.setattr(fp, "spawn_first_play_warm", rec)
    user = core.WebUser(username="u_nu_" + uuid.uuid4().hex[:8], password_hash="x")
    db.add(user)
    db.commit()
    res = api.get_next_up(_req(), user, db)
    assert res["Items"] == []
    assert rec.calls == []


def test_next_up_spawn_error_still_returns(shelf, monkeypatch):
    _quiet(monkeypatch)
    _progress(shelf.db, shelf.user, shelf.ep, minutes_ago=1, played=True, ticks=0)

    def boom(*a, **k):
        raise RuntimeError("warm down")

    monkeypatch.setattr(fp, "spawn_first_play_warm", boom)
    res = api.get_next_up(_req(), shelf.user, shelf.db)
    assert res["Items"][0]["Name"] == "测试剧 S01E03"


def test_note_exists_unknown_drive_publishes(monkeypatch):
    published = []
    puts = []
    monkeypatch.setattr(
        "backend.emby_server.transfer_variant.publish_drive_exists_ok",
        lambda fid, ttl_sec=None: published.append(fid) or True)
    monkeypatch.setattr(
        "backend.emby_server.api._drive_exists_cache_put",
        lambda drive_id, file_id: puts.append((drive_id, file_id)))
    fp.note_drive_exists_from_warm("fid-1")
    fp.note_drive_exists_from_warm("  ")
    fp.note_drive_exists_from_warm("")
    assert published == ["fid-1"]
    assert puts == []


def test_note_exists_known_drive_puts_without_publish(monkeypatch):
    published = []
    puts = []
    monkeypatch.setattr(
        "backend.emby_server.api._drive_exists_cache_put",
        lambda drive_id, file_id: puts.append((drive_id, file_id)))
    monkeypatch.setattr(
        "backend.emby_server.transfer_variant.publish_drive_exists_ok",
        lambda fid, ttl_sec=None: published.append(fid) or True)
    fp.note_drive_exists_from_warm("fid-2", drive_id="drv")
    assert puts == [("drv", "fid-2")]
    assert published == []


def test_note_exists_put_failure_falls_back_to_publish(monkeypatch):
    published = []

    def boom(drive_id, file_id):
        raise RuntimeError("put down")

    monkeypatch.setattr("backend.emby_server.api._drive_exists_cache_put", boom)
    monkeypatch.setattr(
        "backend.emby_server.transfer_variant.publish_drive_exists_ok",
        lambda fid, ttl_sec=None: published.append(fid) or True)
    fp.note_drive_exists_from_warm("fid-3", drive_id="drv")
    assert published == ["fid-3"]


def test_note_exists_publish_failure_is_swallowed(monkeypatch):
    def boom(fid, ttl_sec=None):
        raise RuntimeError("redis down")

    monkeypatch.setattr(
        "backend.emby_server.transfer_variant.publish_drive_exists_ok", boom)
    fp.note_drive_exists_from_warm("fid-4")


def _stub_probe_stack(monkeypatch, status=206):
    monkeypatch.setattr(fp, "alt_media_url", lambda fid: "https://example.invalid/f")
    monkeypatch.setattr(
        "backend.emby_server.playback_strategy.drive_direct_play_enabled",
        lambda db: True)
    monkeypatch.setattr(
        "backend.emby_server.transfer_variant.resolve_play_file_id",
        lambda db, item, user: ("fid-warm", None))
    monkeypatch.setattr(
        "backend.emby_server.drive_auth.get_drive_bearer_token",
        lambda key: "tok")
    monkeypatch.setattr(
        "backend.emby_server.drive_auth.sa_email_for_token",
        lambda token: None)
    monkeypatch.setattr(
        "backend.emby_server.drive_auth.get_drive_bearer_token_for_email",
        lambda email: None)

    class _CM:
        @staticmethod
        def get(k, domain=None):
            return None

        @staticmethod
        def set(k, v, ttl, domain=None):
            return None

    monkeypatch.setattr("backend.database.CacheManager", _CM)
    monkeypatch.setattr(
        fp, "probe_alt_media",
        lambda *a, **k: {
            "ok": status == 206, "status": status,
            "content_type": "video/mp4",
            "content_range": "bytes 0-0/100", "ttfb_ms": 1})


def test_warm_sync_206_notes_exists_failure_does_not(monkeypatch):
    noted = []
    monkeypatch.setattr(
        fp, "note_drive_exists_from_warm",
        lambda fid, drive_id=None: noted.append(fid))
    _stub_probe_stack(monkeypatch, status=206)
    item = SimpleNamespace(id=1, guid="g1", item_type="movie")
    user = SimpleNamespace(id=7)
    out = fp.warm_first_play_sync(None, item, user)
    assert out["ok"] is True and out["reason"] == "warmed"
    assert noted == ["fid-warm"]

    noted.clear()
    _stub_probe_stack(monkeypatch, status=403)
    out = fp.warm_first_play_sync(None, item, user)
    assert out["ok"] is False
    assert noted == []


def test_warm_hit_does_not_rewrite_exists(monkeypatch):
    noted = []
    monkeypatch.setattr(
        fp, "note_drive_exists_from_warm",
        lambda fid, drive_id=None: noted.append(fid))
    _stub_probe_stack(monkeypatch, status=206)
    payload = json.dumps({
        "url": "https://example.invalid/f", "token": "tok",
        "fid": "fid-warm", "warmed_at": time.time(),
    })

    class _Hit:
        @staticmethod
        def get(k, domain=None):
            return payload

        @staticmethod
        def set(k, v, ttl, domain=None):
            raise AssertionError("warm hit 不应再写预热缓存")

    monkeypatch.setattr("backend.database.CacheManager", _Hit)
    out = fp.warm_first_play_sync(
        None, SimpleNamespace(id=1, guid="g1", item_type="movie"),
        SimpleNamespace(id=7))
    assert out["reason"] == "warm_hit"
    assert noted == []


def test_resolve_for_playback_notes_exists(monkeypatch):
    noted = []
    monkeypatch.setattr(
        fp, "note_drive_exists_from_warm",
        lambda fid, drive_id=None: noted.append(fid))
    _stub_probe_stack(monkeypatch, status=206)
    out = fp._resolve_for_playback(
        None, SimpleNamespace(id=1, guid="g1"), SimpleNamespace(id=7), "fid-cold")
    assert out["ok"] is True
    assert noted == ["fid-cold"]
