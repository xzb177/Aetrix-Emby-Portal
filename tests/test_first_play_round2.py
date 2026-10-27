# -*- coding: utf-8 -*-
"""首次播放第二轮：持久 SA 亲和 + 预热目标扩展 + 裸地址一跳。

不碰真实谷歌、不碰真实 SA：probe 与令牌函数全部注入；
剧集目标选择用内存 SQLite + 真实模型表测，不用桩 DB。
不按播放器名分流：新 API 签名里没有任何客户端名参数。
"""
import json
import time

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend.database import Base
from backend.emby_server import drive_auth as da
from backend.emby_server import first_play as fp
from backend.emby_server import models as em


@pytest.fixture()
def db():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    yield session
    session.close()


def _probe(status):
    def _fn(url, token):
        return {"ok": status == 206, "status": status,
                "content_type": "video/x-matroska" if status == 206 else "text/html",
                "content_range": "bytes 0-0/1000" if status == 206 else None,
                "ttfb_ms": 5}
    return _fn


def _cache():
    store = {}

    def cache_set(k, v, ttl):
        store[k] = v

    def cache_get(k):
        return store.get(k)

    return store, cache_get, cache_set


def _setup_affinity(monkeypatch, store, email, age_sec=0):
    store[fp.sa_affinity_key("fid1")] = json.dumps(
        {"email": email, "updated_at": time.time() - age_sec})


def test_persistent_affinity_preferred_sa_used_first(monkeypatch):
    monkeypatch.setattr(fp, "alt_media_url", lambda fid: "https://example.invalid/f")
    store, cache_get, cache_set = _cache()
    _setup_affinity(monkeypatch, store, "good@x")
    pref_calls = []
    monkeypatch.setattr(
        da, "get_drive_bearer_token_for_email",
        lambda email: pref_calls.append(email) or "pref-token")
    token_calls = []

    def token_fn(attempt):
        token_calls.append(attempt)
        return "rot-token"

    result = fp._resolve_verified(
        "fid1", token_fn, cache_get=cache_get, cache_set=cache_set,
        probe=lambda u, t: _probe(206)(u, t))
    assert result["ok"] is True
    assert result["token"] == "pref-token"
    assert result["rotations"] == 0
    assert pref_calls == ["good@x"]
    assert token_calls == []  # 亲和命中时不走轮换、不抽奖


def test_persistent_affinity_403_falls_through_and_overwrites(monkeypatch):
    monkeypatch.setattr(fp, "alt_media_url", lambda fid: "https://example.invalid/f")
    store, cache_get, cache_set = _cache()
    _setup_affinity(monkeypatch, store, "bad@x")
    monkeypatch.setattr(
        da, "get_drive_bearer_token_for_email", lambda email: "pref-token")
    monkeypatch.setattr(
        da, "sa_email_for_token",
        lambda token: "new@x" if token == "good-token" else None)

    def token_fn(attempt):
        return "good-token"

    result = fp._resolve_verified(
        "fid1", token_fn, cache_get=cache_get, cache_set=cache_set,
        probe=lambda u, t: _probe(403 if t == "pref-token" else 206)(u, t))
    assert result["ok"] is True
    assert result["token"] == "good-token"
    # 轮换成功后亲和记录被新 SA 覆盖，不会钉死在坏 SA 上。
    record = json.loads(store[fp.sa_affinity_key("fid1")])
    assert record["email"] == "new@x"


def test_persistent_affinity_expired_record_is_ignored(monkeypatch):
    monkeypatch.setattr(fp, "alt_media_url", lambda fid: "https://example.invalid/f")
    store, cache_get, cache_set = _cache()
    _setup_affinity(monkeypatch, store, "old@x", age_sec=7 * 3600)
    pref_calls = []
    monkeypatch.setattr(
        da, "get_drive_bearer_token_for_email",
        lambda email: pref_calls.append(email) or "pref-token")
    monkeypatch.setattr(da, "sa_email_for_token", lambda token: None)
    result = fp._resolve_verified(
        "fid1", lambda attempt: "good-token", cache_get=cache_get,
        cache_set=cache_set, probe=lambda u, t: _probe(206)(u, t))
    assert result["ok"] is True
    assert pref_calls == []  # 过期记录不触发亲和路径
    assert result["token"] == "good-token"


def test_sa_affinity_store_load_roundtrip_and_expiry():
    store, cache_get, cache_set = _cache()
    assert fp.store_sa_affinity(cache_set, "fid9", "a@x") is True
    assert fp.load_sa_affinity(cache_get, "fid9") == "a@x"
    # 只存邮箱，不存令牌。
    assert "token" not in store[fp.sa_affinity_key("fid9")]


def test_drive_auth_find_sa_and_token_lookup(monkeypatch):
    from types import SimpleNamespace

    monkeypatch.setattr(
        da, "_load_drive_changes",
        lambda: SimpleNamespace(_discover_sa_files=lambda: [
            {"client_email": "a@x", "private_key": "k1"},
            {"client_email": "b@x", "private_key": "k2"}]))
    assert da.find_sa_by_email("b@x")["private_key"] == "k2"
    assert da.find_sa_by_email("nope@x") is None
    da._token_cache["a@x"] = ("tok123", time.monotonic() + 9999)
    try:
        assert da.sa_email_for_token("tok123") == "a@x"
        assert da.sa_email_for_token("unknown") is None
    finally:
        da._token_cache.pop("a@x", None)


def _series_with_episodes(db, watched_ids=()):
    lib = em.Library(guid="g-round2", name="剧库", collection_type="tvshows",
                     paths="", storage_backends="")
    db.add(lib)
    db.commit()
    series = em.MediaItem(guid="s1", library_id=lib.id, item_type="series",
                          name="剧", file_path="")
    db.add(series)
    db.commit()
    season = em.MediaItem(guid="se1", library_id=lib.id, item_type="season",
                          name="第 1 季", file_path="", series_id=series.id,
                          season_number=1, parent_id=series.id)
    db.add(season)
    db.commit()
    eps = []
    for n in (1, 2, 3):
        ep = em.MediaItem(guid=f"e{n}", library_id=lib.id, item_type="episode",
                          name=f"第 {n} 集", file_path=f"/data/e{n}.mkv",
                          series_id=series.id, parent_id=season.id,
                          season_number=1, episode_number=n,
                          drive_file_id=f"fid{n}")
        db.add(ep)
        eps.append(ep)
    db.commit()
    for ep in eps:
        if ep.id in watched_ids:
            db.add(em.UserMediaData(user_id=7, item_id=ep.id, played=True))
    db.commit()
    return series, season, eps


def test_first_unwatched_and_next_episode(db):
    series, season, eps = _series_with_episodes(db, watched_ids=[])
    # 先标记第 1 集已看（用 id 查，避免 fixture 顺序问题）。
    db.add(em.UserMediaData(user_id=7, item_id=eps[0].id, played=True))
    db.commit()
    assert fp.first_unwatched_episode(db, 7, series.id).id == eps[1].id
    assert fp.first_unwatched_episode(db, 7, series.id, season_number=1).id == eps[1].id
    assert fp.next_episode_after(db, eps[0]).id == eps[1].id
    assert fp.next_episode_after(db, eps[1]).id == eps[2].id
    assert fp.next_episode_after(db, eps[2]) is None
    assert fp.next_episode_after(db, series) is None  # 非单集无下一集


def test_first_unwatched_all_watched_returns_none(db):
    series, _season, eps = _series_with_episodes(db)
    for ep in eps:
        db.add(em.UserMediaData(user_id=7, item_id=ep.id, played=True))
    db.commit()
    assert fp.first_unwatched_episode(db, 7, series.id) is None


def test_warm_target_for_detail(db):
    series, season, eps = _series_with_episodes(db)
    db.add(em.UserMediaData(user_id=7, item_id=eps[0].id, played=True))
    db.commit()
    assert fp.warm_target_for_detail(db, 7, series).id == eps[1].id
    assert fp.warm_target_for_detail(db, 7, season).id == eps[1].id
    assert fp.warm_target_for_detail(db, 7, eps[0]).id == eps[0].id  # 单集暖自身
    movie = em.MediaItem(guid="m1", library_id=series.library_id,
                         item_type="movie", name="电影", file_path="/data/m.mkv",
                         drive_file_id="fidm")
    db.add(movie)
    db.commit()
    assert fp.warm_target_for_detail(db, 7, movie).id == movie.id


def test_no_client_name_in_round2_api():
    import inspect

    sigs = [inspect.signature(fp._resolve_verified),
            inspect.signature(fp.first_unwatched_episode),
            inspect.signature(fp.next_episode_after),
            inspect.signature(fp.warm_target_for_detail),
            inspect.signature(fp.spawn_first_play_warm)]
    joined = " ".join(str(s) for s in sigs).lower()
    for name in ("rex", "senplayer", "vidhub", "client_name", "device_name"):
        assert name not in joined
