# -*- coding: utf-8 -*-
"""LRU 腾地方保护“正在追的剧”与“最近播过的副本”（2026-10-29）。

全部离线：Drive 客户端用桩替身，不碰真实 Drive、不碰生产库。
覆盖：受保护行第一轮不删；空间够时只删非受保护行；空间不够时
第二轮删受保护行并记 warning；正在播 / 本条永不删；保护集计算
异常退回原行为；两个环境变量可调；追剧取当前集 + 后 N 集。
"""
import os

os.environ.setdefault("DATABASE_TYPE", "sqlite")
os.environ.setdefault("REDIS_ENABLED", "false")

from datetime import datetime, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend import models as base_models
from backend.emby_server import models as em
from backend.emby_server import transfer_cleanup as tc

NOW = datetime(2026, 10, 29, 12, 0, 0)
UID = 435
LOGGER = "backend.emby_server.transfer_cleanup"


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    monkeypatch.delenv("TRANSFER_EVICT_RECENT_HOURS", raising=False)
    monkeypatch.delenv("TRANSFER_EVICT_NEXT_EPISODES", raising=False)


@pytest.fixture()
def db():
    engine = create_engine("sqlite://",
                           connect_args={"check_same_thread": False},
                           poolclass=StaticPool)
    base_models.Base.metadata.create_all(engine)
    em.Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    yield session
    session.close()


class _NotFound(Exception):
    pass


class FakeDriveClient:
    """桩 Drive 客户端：about/get/delete 只记调用，不碰网络。"""

    def __init__(self, existing=None, free=10 ** 9):
        self.existing = set(existing or [])
        self.deleted = []
        self.free = free

    def about_storage(self, drive_marker):
        return {"limit": 10 ** 12, "usage": 10 ** 12 - self.free,
                "free": self.free}

    def get_file(self, drive_marker, fid):
        if fid not in self.existing:
            raise _NotFound("404")
        return {"id": fid}

    def delete_file(self, drive_marker, fid):
        self.deleted.append(fid)
        self.existing.discard(fid)


def _factory(client):
    def factory(db, user_id):
        return client, f"myDrive:{user_id}"
    return factory


def _variant(db, item_id, fid, size=3000, last_played=None, created=None):
    row = em.TransferVariant(item_id=item_id, source_file_id="origfid",
                             variant_file_id=fid,
                             variant_drive_id=f"userdrive:{UID}",
                             owner_scope="user", owner_ref=str(UID),
                             status="ready", size_bytes=size,
                             last_played_at=last_played,
                             created_at=created or datetime(2026, 10, 1))
    db.add(row)
    db.commit()
    return row


def _library(db):
    lib = em.Library(guid="lib-guid-1", name="剧集库")
    db.add(lib)
    db.commit()
    return lib


def _item(db, lib, item_id, item_type, *, series_id=None, season=None,
          episode=None, merged_into=None):
    row = em.MediaItem(id=item_id, guid=f"guid-{item_id}",
                       library_id=lib.id, item_type=item_type,
                       name=f"条目{item_id}", series_id=series_id,
                       season_number=season, episode_number=episode,
                       merged_into_id=merged_into)
    db.add(row)
    db.commit()
    return row


def _series_with_episodes(db, count=6, season=1):
    """建一部剧（id 1000）+ 第 season 季 1..count 集（id 1001..）。"""
    lib = _library(db)
    _item(db, lib, 1000, "series")
    for n in range(1, count + 1):
        _item(db, lib, 1000 + n, "episode", series_id=1000,
              season=season, episode=n)
    return lib


def _watched(db, item_id, when):
    db.add(em.UserMediaData(user_id=UID, item_id=item_id,
                            last_played_at=when))
    db.commit()


def _status(db, row):
    db.refresh(row)
    return row.status


def test_recent_played_protected_in_round_one(db):
    # 2 小时前播过（超出 600 秒正在播窗口，但在 6 小时保护窗口内）。
    recent = _variant(db, 101, "a", last_played=NOW - timedelta(hours=2),
                      created=datetime(2026, 10, 1))
    other = _variant(db, 102, "b", created=datetime(2026, 10, 20))
    client = FakeDriveClient(existing={"a", "b"}, free=1000)
    result = tc.ensure_space_for_transfer(
        db, _factory(client), UID, file_size=4000, now=NOW)
    # 原 LRU 会先删 a（last_played 更早于 b 的 created? 否——a 的键
    # 是 2 小时前，b 是 10-20，b 更旧）；这里要看的是 a 进了保护集、
    # 只删 b 就够，a 不动。
    assert result["reason"] == "evicted_enough"
    assert result["evicted"] == [other.id]
    assert result["protected"] == [recent.id]
    assert result["evicted_protected"] == []
    assert _status(db, recent) == "ready"


def test_watching_series_protects_current_and_next_two(db):
    _series_with_episodes(db, count=6)
    # 当前在看第 3 集（三天前），不在 6 小时近播窗口内，靠追剧保护。
    _watched(db, 1002, NOW - timedelta(days=4))
    _watched(db, 1003, NOW - timedelta(days=3))
    ep2 = _variant(db, 1002, "e2", created=datetime(2026, 10, 2))
    ep3 = _variant(db, 1003, "e3", created=datetime(2026, 10, 3))
    ep4 = _variant(db, 1004, "e4", created=datetime(2026, 10, 4))
    ep5 = _variant(db, 1005, "e5", created=datetime(2026, 10, 5))
    ep6 = _variant(db, 1006, "e6", created=datetime(2026, 10, 6))
    protected = tc.protected_variant_ids(db, UID, NOW)
    assert protected == {ep3.id, ep4.id, ep5.id}
    assert ep2.id not in protected and ep6.id not in protected


def test_space_enough_only_non_protected_deleted(db):
    _series_with_episodes(db, count=6)
    _watched(db, 1003, NOW - timedelta(days=1))
    # 受保护的 e3/e4/e5 都比非受保护的更旧（LRU 原本会先删它们）。
    ep3 = _variant(db, 1003, "e3", created=datetime(2026, 10, 1))
    ep4 = _variant(db, 1004, "e4", created=datetime(2026, 10, 2))
    ep5 = _variant(db, 1005, "e5", created=datetime(2026, 10, 3))
    movie1 = _variant(db, 501, "m1", created=datetime(2026, 10, 10))
    movie2 = _variant(db, 502, "m2", created=datetime(2026, 10, 11))
    client = FakeDriveClient(existing={"e3", "e4", "e5", "m1", "m2"},
                             free=1000)
    result = tc.ensure_space_for_transfer(
        db, _factory(client), UID, file_size=7000, now=NOW)
    # need = 6000：m1 + m2 = 6000 即够，受保护行一条不动。
    assert result["reason"] == "evicted_enough"
    assert result["evicted"] == [movie1.id, movie2.id]
    assert client.deleted == ["m1", "m2"]
    assert result["evicted_protected"] == []
    for row in (ep3, ep4, ep5):
        assert _status(db, row) == "ready"


def test_space_short_round_two_evicts_protected_with_warning(db, caplog):
    _series_with_episodes(db, count=6)
    _watched(db, 1003, NOW - timedelta(days=1))
    ep3 = _variant(db, 1003, "e3", created=datetime(2026, 10, 2))
    ep4 = _variant(db, 1004, "e4", created=datetime(2026, 10, 1))
    ep5 = _variant(db, 1005, "e5", created=datetime(2026, 10, 3))
    movie = _variant(db, 501, "m1", created=datetime(2026, 10, 10))
    client = FakeDriveClient(existing={"e3", "e4", "e5", "m1"}, free=1000)
    with caplog.at_level("WARNING", logger=LOGGER):
        result = tc.ensure_space_for_transfer(
            db, _factory(client), UID, file_size=7000, now=NOW)
    # need = 6000：第一轮只有 m1（3000）不够；第二轮按原 LRU 顺序
    # 在受保护行里腾：e4（10-01）最旧先删，3000 + 3000 够了即停。
    assert result["reason"] == "evicted_enough"
    assert result["proceed"] is True
    assert result["evicted"] == [movie.id, ep4.id]
    assert result["evicted_protected"] == [ep4.id]
    assert client.deleted == ["m1", "e4"]
    assert _status(db, ep3) == "ready" and _status(db, ep5) == "ready"
    warns = [r for r in caplog.records if r.levelname == "WARNING"
             and "受保护" in r.getMessage()]
    assert len(warns) == 1 and str(ep4.id) in warns[0].getMessage()


def test_playing_and_current_item_never_evicted_even_in_round_two(db):
    _series_with_episodes(db, count=6)
    _watched(db, 1003, NOW - timedelta(days=1))
    # 正在播：活跃会话（30 秒前上报）播的是一部电影（条目 888）。
    db.add(em.PlaybackSession(session_key="sk-play", user_id=UID,
                              item_id=888,
                              last_update_at=NOW - timedelta(seconds=30)))
    db.commit()
    ep3 = _variant(db, 1003, "e3", created=datetime(2026, 10, 1))
    playing = _variant(db, 888, "e4", created=datetime(2026, 9, 1))
    current = _variant(db, 777, "cur", created=datetime(2026, 9, 1))
    client = FakeDriveClient(existing={"e3", "e4", "cur"}, free=1000)
    result = tc.ensure_space_for_transfer(
        db, _factory(client), UID, file_size=100000, now=NOW,
        exclude_item_ids=[777])
    # 两轮都腾完仍不足：明确失败；正在播的 e4、本条 cur 一律不动，
    # 受保护的 e3 在第二轮被腾。
    assert result["reason"] == "insufficient"
    assert result["proceed"] is False
    assert client.deleted == ["e3"]
    assert result["evicted_protected"] == [ep3.id]
    assert _status(db, playing) == "ready"
    assert _status(db, current) == "ready"


def test_protected_failure_falls_back_to_plain_lru(db, monkeypatch, caplog):
    recent = _variant(db, 101, "a", last_played=NOW - timedelta(hours=1),
                      created=datetime(2026, 10, 1))
    other = _variant(db, 102, "b", created=datetime(2026, 10, 20))
    # 注意：b 的 LRU 键（10-20 创建）本就比 a（1 小时前播）旧。

    def boom(*args, **kwargs):
        raise RuntimeError("protected boom")

    monkeypatch.setattr(tc, "protected_variant_ids", boom)
    client = FakeDriveClient(existing={"a", "b"}, free=1000)
    with caplog.at_level("WARNING", logger=LOGGER):
        result = tc.ensure_space_for_transfer(
            db, _factory(client), UID, file_size=4000, now=NOW)
    # 退回原行为：按原 LRU（b 的创建时间 10-20 早于 a 的播放时间）
    # 先删 b，够了即停；转存不被阻断。
    assert result["reason"] == "evicted_enough"
    assert result["evicted"] == [other.id]
    assert result["protected"] == []
    assert _status(db, recent) == "ready"
    assert any("追剧保护集计算失败" in r.getMessage()
               for r in caplog.records)


def test_protected_failure_still_evicts_what_old_lru_would(db, monkeypatch):
    # 无保护时被保护的 a 会因 LRU 更旧而先被删——证明确实退回原行为。
    a = _variant(db, 101, "a", last_played=NOW - timedelta(hours=1),
                 created=datetime(2026, 10, 1))
    b = _variant(db, 102, "b", created=datetime(2026, 10, 29, 11, 30))
    monkeypatch.setattr(tc, "protected_variant_ids",
                        lambda *a, **k: (_ for _ in ()).throw(ValueError()))
    client = FakeDriveClient(existing={"a", "b"}, free=1000)
    result = tc.ensure_space_for_transfer(
        db, _factory(client), UID, file_size=4000, now=NOW)
    assert result["evicted"] == [a.id]
    assert _status(db, b) == "ready"


def test_env_recent_hours_tunable(db, monkeypatch):
    row = _variant(db, 101, "a", last_played=NOW - timedelta(hours=3))
    assert tc.protected_variant_ids(db, UID, NOW) == {row.id}
    monkeypatch.setenv("TRANSFER_EVICT_RECENT_HOURS", "2")
    assert tc.protected_variant_ids(db, UID, NOW) == set()
    monkeypatch.setenv("TRANSFER_EVICT_RECENT_HOURS", "0")
    assert tc.protected_variant_ids(db, UID, NOW) == set()
    monkeypatch.setenv("TRANSFER_EVICT_RECENT_HOURS", "abc")
    assert tc.evict_recent_hours() == 6.0
    monkeypatch.setenv("TRANSFER_EVICT_RECENT_HOURS", "-1")
    assert tc.evict_recent_hours() == 6.0


def test_env_next_episodes_tunable(db, monkeypatch):
    _series_with_episodes(db, count=6)
    _watched(db, 1002, NOW - timedelta(days=2))
    rows = {n: _variant(db, 1000 + n, f"e{n}") for n in range(1, 7)}
    monkeypatch.setenv("TRANSFER_EVICT_NEXT_EPISODES", "3")
    assert tc.protected_variant_ids(db, UID, NOW) == {
        rows[n].id for n in (2, 3, 4, 5)}
    monkeypatch.setenv("TRANSFER_EVICT_NEXT_EPISODES", "0")
    assert tc.protected_variant_ids(db, UID, NOW) == {rows[2].id}
    monkeypatch.setenv("TRANSFER_EVICT_NEXT_EPISODES", "x")
    assert tc.evict_next_episodes() == 2


def test_watching_crosses_season_and_uses_session_reports(db):
    lib = _library(db)
    _item(db, lib, 1000, "series")
    _item(db, lib, 1101, "episode", series_id=1000, season=1, episode=9)
    _item(db, lib, 1102, "episode", series_id=1000, season=1, episode=10)
    _item(db, lib, 1201, "episode", series_id=1000, season=2, episode=1)
    _item(db, lib, 1202, "episode", series_id=1000, season=2, episode=2)
    # 没有 UserMediaData，只有已结束的会话上报（一天前）。
    db.add(em.PlaybackSession(session_key="sk-old", user_id=UID,
                              item_id=1102,
                              last_update_at=NOW - timedelta(days=1),
                              ended_at=NOW - timedelta(days=1)))
    db.commit()
    rows = {iid: _variant(db, iid, f"f{iid}")
            for iid in (1101, 1102, 1201, 1202)}
    assert tc.protected_variant_ids(db, UID, NOW) == {
        rows[1102].id, rows[1201].id, rows[1202].id}


def test_watching_ignores_other_users_and_stale_lookback(db):
    _series_with_episodes(db, count=4)
    # 别的用户在追、本用户 30 天前看过（超出回看范围）：都不保护。
    db.add(em.UserMediaData(user_id=999, item_id=1001,
                            last_played_at=NOW - timedelta(hours=1)))
    db.commit()
    _watched(db, 1002, NOW - timedelta(days=30))
    for n in range(1, 5):
        _variant(db, 1000 + n, f"e{n}")
    assert tc.protected_variant_ids(db, UID, NOW) == set()


def test_no_need_still_deletes_nothing(db):
    _variant(db, 101, "a")
    client = FakeDriveClient(existing={"a"}, free=10 ** 9)
    result = tc.ensure_space_for_transfer(
        db, _factory(client), UID, file_size=4000, now=NOW)
    assert result["reason"] == "sufficient"
    assert client.deleted == []
