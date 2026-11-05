# -*- coding: utf-8 -*-
"""无界起播「暖地址」保鲜工人（一期）回归测试。

口径钉死（2026-11-05 用户亲批）：
- 慢的大头是每播必做的同步探活；PR #1856 给复播加了已验活地址
  快路（窗口默认 300 秒），首播仍全额付探活。本工人把探活挪到
  点播之前：按信号集（追剧新集 / 继续观看含下一集 / 新入库热门）
  在后台走与起播逐字相同的解析＋探活，成功即由 #1856 的同一
  写入点落进同一份已验活缓存——不另起缓存、不写业务数据。
- 资源纪律：低并发、轮次双预算（时长 + 请求数）、起播繁忙或
  系统高负载整轮跳过、源健康判不可用跳过不硬探、失败只记账
  不冒泡、多轮幂等（已新鲜的映射不重复验）。
"""
import os

os.environ.setdefault("DATABASE_TYPE", "sqlite")
os.environ.setdefault("REDIS_ENABLED", "false")

from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.database import Base, CacheManager
from backend.emby_server import models as em
from backend.emby_server import upstream_playback as up
from backend.emby_server import upstream_warm_worker as uw
import backend.models  # noqa: F401 — 注册 web_users 等表

NOW = datetime(2026, 11, 5, 12, 0, 0)


@pytest.fixture()
def factory():
    # 内存库 + StaticPool：全体会话共用一条连接（用例内并发已钉
    # 死为 1），免得测试盘 I/O 争用把整轮拖进磁盘等待。
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False, "timeout": 30},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)


@pytest.fixture()
def db(factory):
    s = factory()
    try:
        yield s
    finally:
        s.close()


@pytest.fixture(autouse=True)
def _clean_state(monkeypatch):
    monkeypatch.setenv("UPSTREAM_WARM_JITTER_SEC", "0")
    monkeypatch.setenv("UPSTREAM_WARM_CONCURRENCY", "1")
    for key in list(os.environ):
        if key.startswith("UPSTREAM_WARM_") and key not in (
                "UPSTREAM_WARM_JITTER_SEC", "UPSTREAM_WARM_CONCURRENCY"):
            monkeypatch.delenv(key, raising=False)
    # 负载比默认贴地（高负载用例自己改回 0.99），免得测试机
    # 真实负载偶发越线把别的用例整轮让掉
    monkeypatch.setattr(uw, "_load_ratio", lambda: 0.1)
    uw._warmed_at.clear()
    uw._last_round.clear()
    _key = getattr(up, "cms_verified_play_key", None)
    if _key is not None:
        for mid in range(92001, 92060):
            CacheManager.delete(_key(mid))
    yield
    uw._warmed_at.clear()
    uw._last_round.clear()


def _item(db, name, item_type="movie", *, series=None, season=None,
          ep=None, added=None, merged_into=None, library_id=1):
    it = em.MediaItem(
        guid=f"g-{name}-{os.urandom(4).hex()}",
        library_id=library_id, item_type=item_type, name=name,
        file_path="/x",
        series_id=series.id if series is not None else None,
        season_number=season, episode_number=ep,
        date_added=added if added is not None else NOW,
        merged_into_id=merged_into,
    )
    db.add(it)
    db.commit()
    return it


def _cfg(**kw):
    cfg = uw.warm_config()
    cfg.update(kw)
    return cfg


# ---------------------------------------------------------------------------
# 配置
# ---------------------------------------------------------------------------

def test_config_defaults_and_env_override(monkeypatch):
    cfg = uw.warm_config()
    assert cfg["enabled"] is True
    assert cfg["concurrency"] == 1  # autouse 夹具钉死为 1（生产默认 2）
    assert cfg["max_targets"] == 120
    assert cfg["mappings_per_item"] == 2
    assert cfg["follow_per_series"] == 3
    assert cfg["hot_min_plays"] == 3
    monkeypatch.setenv("UPSTREAM_WARM_MAX_TARGETS_PER_ROUND", "7")
    monkeypatch.setenv("UPSTREAM_WARM_ENABLED", "0")
    monkeypatch.setenv("UPSTREAM_WARM_INTERVAL_SEC", "not-a-number")
    cfg = uw.warm_config()
    assert cfg["max_targets"] == 7
    assert cfg["enabled"] is False
    assert cfg["interval_sec"] == 240.0  # 脏值回默认


# ---------------------------------------------------------------------------
# 信号一：追剧新集
# ---------------------------------------------------------------------------

def _follow(db, series, created_at, user_id=1):
    row = em.SeriesFollow(user_id=user_id, series_item_id=series.id,
                          status="active", created_at=created_at)
    db.add(row)
    db.commit()
    return row


def test_follow_signal_new_episodes_only(db):
    series = _item(db, "追的剧", "series")
    other = _item(db, "没追的剧", "series")
    _follow(db, series, NOW - timedelta(days=1))
    fresh = _item(db, "新集", "episode", series=series, season=1, ep=9,
                  added=NOW - timedelta(hours=2))
    _item(db, "订阅前到的集", "episode", series=series, season=1, ep=8,
          added=NOW - timedelta(days=2))
    _item(db, "别家的集", "episode", series=other, season=1, ep=1,
          added=NOW - timedelta(hours=1))
    _item(db, "合并副本集", "episode", series=series, season=1, ep=10,
          added=NOW - timedelta(hours=1), merged_into=999)
    got = uw.collect_follow_episode_ids(db, _cfg(), NOW)
    assert got == [fresh.id]


def test_follow_signal_per_series_cap_and_lookback(db):
    series = _item(db, "长剧", "series")
    _follow(db, series, NOW - timedelta(days=30))
    ids = []
    for i in range(5):
        ids.append(_item(db, f"第{i}集", "episode", series=series,
                         season=1, ep=i + 1,
                         added=NOW - timedelta(hours=i + 1)).id)
    _item(db, "太老的集", "episode", series=series, season=1, ep=99,
          added=NOW - timedelta(hours=100))
    got = uw.collect_follow_episode_ids(
        db, _cfg(follow_per_series=2, follow_lookback_hours=72), NOW)
    assert got == ids[:2]  # 最新两集，老集被回望窗挡下


# ---------------------------------------------------------------------------
# 信号二：继续观看（含下一集）
# ---------------------------------------------------------------------------

def _watched(db, item, when, *, user_id=1, played=False, position=100):
    row = em.UserMediaData(
        user_id=user_id, item_id=item.id,
        playback_position_ticks=position, played=played,
        last_played_at=when, updated_at=when)
    db.add(row)
    db.commit()
    return row


def test_resume_signal_and_next_episode(db):
    series = _item(db, "在看的剧", "series")
    eps = {}
    for season, count in ((1, 3), (2, 2)):
        for ep in range(1, count + 1):
            eps[(season, ep)] = _item(
                db, f"S{season}E{ep}", "episode", series=series,
                season=season, ep=ep)
    movie = _item(db, "在看的电影", "movie")
    stale = _item(db, "十天前看的", "movie")
    _watched(db, eps[(1, 3)], NOW - timedelta(days=1))   # 季终集 → 下一集跨季
    _watched(db, movie, NOW - timedelta(days=2))
    _watched(db, stale, NOW - timedelta(days=10))
    got = uw.collect_resume_item_ids(db, _cfg(), NOW)
    assert movie.id in got
    assert eps[(1, 3)].id in got
    assert eps[(2, 1)].id in got       # 下一集（跨季首集）
    assert stale.id not in got
    assert eps[(1, 2)].id not in got   # 没看过的上一集不进


def test_resume_signal_session_only_item(db):
    movie = _item(db, "只有会话的片", "movie")
    ses = em.PlaybackSession(
        session_key="warm-test-session", user_id=1, item_id=movie.id,
        start_time=NOW - timedelta(hours=3),
        last_update_at=NOW - timedelta(hours=3))
    db.add(ses)
    db.commit()
    got = uw.collect_resume_item_ids(db, _cfg(), NOW)
    assert got == [movie.id]


def test_next_episode_none_without_number(db):
    series = _item(db, "无号剧", "series")
    ep = _item(db, "无号集", "episode", series=series, season=1, ep=None)
    assert uw.next_episode_id(db, ep) is None


# ---------------------------------------------------------------------------
# 信号三：新入库热门
# ---------------------------------------------------------------------------

def _play(db, item, when, result="ok"):
    row = em.PlayStartEvent(
        user_id=1, item_id=item.id, provider="upstream", method="upstream",
        result=result, resolve_ms=3000, created_at=when)
    db.add(row)
    db.commit()
    return row


def test_hot_signal_threshold_and_newness(db):
    hot = _item(db, "热门新片", "movie", added=NOW - timedelta(days=1))
    warm = _item(db, "温吞新片", "movie", added=NOW - timedelta(days=1))
    old = _item(db, "老片翻红", "movie", added=NOW - timedelta(days=30))
    err = _item(db, "播不出的新片", "movie", added=NOW - timedelta(days=1))
    for i in range(3):
        _play(db, hot, NOW - timedelta(hours=2 + i))
    for i in range(2):
        _play(db, warm, NOW - timedelta(hours=2 + i))
    for i in range(5):
        _play(db, old, NOW - timedelta(hours=2 + i))
    for i in range(4):
        _play(db, err, NOW - timedelta(hours=2 + i), result="error")
    got = uw.collect_hot_item_ids(db, _cfg(), NOW)
    assert got == [hot.id]


# ---------------------------------------------------------------------------
# 目标构建：去重 / 上限 / 跳过口径
# ---------------------------------------------------------------------------

def _fake_pairs(mapping_specs):
    """mapping_specs: {item_id: [(mapping_id, source_id, source_type)]}"""
    def fake(db, item_id):
        out = []
        for mid, sid, stype in mapping_specs.get(item_id, []):
            mapping = SimpleNamespace(id=mid, source_id=sid)
            source = SimpleNamespace(id=sid, source_type=stype,
                                     enabled=True)
            out.append((mapping, source))
        return out
    return fake


def test_build_targets_dedup_caps_and_skips(db, monkeypatch):
    a = _item(db, "甲片", "movie")
    b = _item(db, "乙片", "movie")
    monkeypatch.setattr(uw, "collect_follow_episode_ids",
                        lambda db_, cfg, now: [a.id])
    monkeypatch.setattr(uw, "collect_resume_item_ids",
                        lambda db_, cfg, now: [a.id, b.id])
    monkeypatch.setattr(uw, "collect_hot_item_ids",
                        lambda db_, cfg, now: [])
    monkeypatch.setattr(uw, "_displayable_mappings", _fake_pairs({
        a.id: [(92001, 21, "applecms"), (92002, 22, "applecms"),
               (92003, 23, "applecms")],
        b.id: [(92004, 24, "emby"), (92005, 99, "applecms")],
    }))
    monkeypatch.setattr(uw, "_source_unavailable",
                        lambda sid: sid == 99)
    targets, stats = uw.build_targets(db, _cfg(), NOW)
    # 甲片按每条目上限 2 取前两条；乙片 emby 型跳过、源 99 不可用跳过
    assert [t["mapping_id"] for t in targets] == [92001, 92002]
    assert stats["skipped_not_cms"] == 1
    assert stats["skipped_source_unavailable"] == 1
    assert stats["follow_items"] == 1
    assert stats["resume_items"] == 2


def test_build_targets_global_cap(db, monkeypatch):
    items = [_item(db, f"片{i}", "movie") for i in range(4)]
    monkeypatch.setattr(uw, "collect_follow_episode_ids",
                        lambda db_, cfg, now: [i.id for i in items])
    monkeypatch.setattr(uw, "collect_resume_item_ids",
                        lambda db_, cfg, now: [])
    monkeypatch.setattr(uw, "collect_hot_item_ids",
                        lambda db_, cfg, now: [])
    monkeypatch.setattr(uw, "_displayable_mappings", _fake_pairs({
        it.id: [(92100 + it.id, 21, "applecms")] for it in items}))
    monkeypatch.setattr(uw, "_source_unavailable", lambda sid: False)
    targets, _stats = uw.build_targets(db, _cfg(max_targets=3), NOW)
    assert len(targets) == 3


# ---------------------------------------------------------------------------
# 整轮：让路 / 幂等 / 失败隔离 / 预算
# ---------------------------------------------------------------------------

def _one_target(monkeypatch, item_id, mapping_id=92001):
    monkeypatch.setattr(uw, "build_targets", lambda db_, cfg, now=None: (
        [{"item_id": item_id, "mapping_id": mapping_id, "source_id": 21}],
        {"follow_items": 0, "resume_items": 1, "hot_items": 0,
         "skipped_not_cms": 0, "skipped_source_unavailable": 0}))


def test_warm_once_busy_round_skips(factory, db, monkeypatch):
    movie = _item(db, "繁忙时段", "movie")
    for i in range(3):
        _play(db, movie, datetime.now())
    _one_target(monkeypatch, movie.id)
    calls = []
    monkeypatch.setattr(uw, "_resolve_for_warm",
                        lambda *a, **k: calls.append(1) or "x")
    monkeypatch.setenv("UPSTREAM_WARM_BUSY_PLAYS_5MIN", "3")
    stats = uw.warm_once(factory)
    assert stats["ran"] is False
    assert stats["reason"] == "busy"
    assert calls == []


def test_warm_once_load_round_skips(factory, db, monkeypatch):
    movie = _item(db, "高负载", "movie")
    _one_target(monkeypatch, movie.id)
    calls = []
    monkeypatch.setattr(uw, "_resolve_for_warm",
                        lambda *a, **k: calls.append(1) or "x")
    monkeypatch.setattr(uw, "_load_ratio", lambda: 0.99)
    stats = uw.warm_once(factory)
    assert stats["ran"] is False
    assert stats["reason"] == "load"
    assert calls == []


def test_warm_once_fastpath_off_skips(factory, db, monkeypatch):
    movie = _item(db, "快路关", "movie")
    _one_target(monkeypatch, movie.id)
    calls = []
    monkeypatch.setattr(uw, "_resolve_for_warm",
                        lambda *a, **k: calls.append(1) or "x")
    monkeypatch.setenv("APPLECMS_VERIFIED_PLAY_TTL_SEC", "0")
    stats = uw.warm_once(factory)
    assert stats["ran"] is False
    assert stats["reason"] == "fastpath_off"
    assert calls == []


def test_warm_once_warms_then_second_round_fresh(factory, db, monkeypatch):
    movie = _item(db, "保鲜片", "movie")
    _one_target(monkeypatch, movie.id)
    calls = []

    def fake_resolve(db_, item_id, mapping_id, http):
        calls.append(mapping_id)
        # 与真实解析同口径：成功下发即经 #1856 写入点落已验活缓存
        up.store_verified_play_address(mapping_id,
                                       "https://cdn.example.com/a.m3u8")
        return "https://cdn.example.com/a.m3u8"

    monkeypatch.setattr(uw, "_resolve_for_warm", fake_resolve)
    first = uw.warm_once(factory)
    assert first["ran"] is True
    assert first["warmed"] == 1
    assert up.read_verified_play_address(92001) == \
        "https://cdn.example.com/a.m3u8"
    second = uw.warm_once(factory)
    assert second["warmed"] == 0
    assert second["fresh"] == 1
    assert calls == [92001]  # 第二轮没再验


def test_warm_once_failure_isolated(factory, db, monkeypatch):
    a = _item(db, "会失败的片", "movie")
    b = _item(db, "会成功的片", "movie")
    monkeypatch.setattr(uw, "build_targets", lambda db_, cfg, now=None: (
        [{"item_id": a.id, "mapping_id": 92011, "source_id": 21},
         {"item_id": b.id, "mapping_id": 92012, "source_id": 21}],
        {"follow_items": 0, "resume_items": 2, "hot_items": 0,
         "skipped_not_cms": 0, "skipped_source_unavailable": 0}))

    def fake_resolve(db_, item_id, mapping_id, http):
        if mapping_id == 92011:
            raise up.UpstreamPlaybackError(
                502, "upstream_unavailable", "上游不可用", source_id=21)
        up.store_verified_play_address(mapping_id,
                                       "https://cdn.example.com/b.m3u8")
        return "https://cdn.example.com/b.m3u8"

    monkeypatch.setattr(uw, "_resolve_for_warm", fake_resolve)
    stats = uw.warm_once(factory)
    assert stats["warmed"] == 1
    assert stats["failed"] == 1


def test_warm_once_request_budget(factory, db, monkeypatch):
    a = _item(db, "预算甲", "movie")
    b = _item(db, "预算乙", "movie")
    monkeypatch.setattr(uw, "build_targets", lambda db_, cfg, now=None: (
        [{"item_id": a.id, "mapping_id": 92021, "source_id": 21},
         {"item_id": b.id, "mapping_id": 92022, "source_id": 21}],
        {"follow_items": 0, "resume_items": 2, "hot_items": 0,
         "skipped_not_cms": 0, "skipped_source_unavailable": 0}))

    def fake_resolve(db_, item_id, mapping_id, http):
        http.probe("https://cdn.example.com/x.m3u8", {}, 1.0)  # 记 1 次请求
        up.store_verified_play_address(mapping_id,
                                       "https://cdn.example.com/x.m3u8")
        return "https://cdn.example.com/x.m3u8"

    monkeypatch.setattr(uw, "_resolve_for_warm", fake_resolve)
    monkeypatch.setattr(uw, "_make_http", lambda counter: SimpleNamespace(
        get=lambda *a: counter.add(1),
        probe=lambda *a: counter.add(1),
        get_text=lambda *a: counter.add(1)))
    monkeypatch.setenv("UPSTREAM_WARM_MAX_REQUESTS_PER_ROUND", "1")
    stats = uw.warm_once(factory)
    assert stats["requests"] == 1
    assert stats["warmed"] == 1
    assert stats["budget"] == 1


def test_status_snapshot_after_round(factory, db, monkeypatch):
    movie = _item(db, "状态片", "movie")
    _one_target(monkeypatch, movie.id, mapping_id=92031)
    monkeypatch.setattr(uw, "_resolve_for_warm",
                        lambda *a, **k: "https://cdn.example.com/s.m3u8")
    uw.warm_once(factory)
    snap = uw.status_snapshot()
    assert snap["last_round"]["warmed"] == 1
    assert snap["last_round"]["targets"] == 1


# ---------------------------------------------------------------------------
# 集成：保鲜走真实解析 → 点播命中快路免探活（依赖 PR #1856 已合入）
# ---------------------------------------------------------------------------

BASE = "https://cms.example.com"
ADDR1 = "https://cdn.example.com/v1e1.m3u8"

DETAIL = {"code": 1, "page": 1, "pagecount": 1, "list": [{
    "vod_id": 4242, "vod_play_from": "组一",
    "vod_play_url": f"第01集${ADDR1}"}]}


def test_warm_then_play_skips_probe(factory, db, monkeypatch):
    monkeypatch.setenv("UPSTREAM_PLAYBACK_ENABLED", "1")
    monkeypatch.setenv("APPLECMS_ENABLED", "1")
    monkeypatch.setenv("APPLECMS_DETAIL_CACHE_ENABLED", "1")
    monkeypatch.delenv("APPLECMS_TIER_GATE", raising=False)
    monkeypatch.setattr(up, "_capture_cms_quality_async",
                        lambda *a, **k: None)
    monkeypatch.setattr(uw, "_source_unavailable", lambda sid: False)

    source = em.UpstreamSource(
        name="warm-it-src", source_type="applecms", base_url=BASE,
        enabled=True)
    db.add(source)
    db.commit()
    movie = _item(db, "集成片", "movie", added=datetime.now())
    mapping = em.UpstreamItemMapping(
        local_item_id=movie.id, source_id=source.id,
        upstream_item_id="4242", episode_index=None,
        confidence=1.0, evidence="tmdb")
    db.add(mapping)
    db.commit()
    # 信号：新入库热门（48 小时内 3 次成功起播）
    for i in range(3):
        _play(db, movie, datetime.now() - timedelta(hours=1 + i))

    get_calls, probe_calls = [], []

    def http_get(url, headers, timeout):
        get_calls.append(url)
        return 200, DETAIL

    def http_probe(url, headers, timeout):
        probe_calls.append(url)
        return 206, None

    monkeypatch.setattr(uw, "_make_http", lambda counter: SimpleNamespace(
        get=http_get, probe=http_probe,
        get_text=lambda *a: (200, "")))

    stats = uw.warm_once(factory)
    assert stats["warmed"] == 1
    assert probe_calls == [ADDR1]  # 保鲜验了一次
    assert up.read_verified_play_address(mapping.id) == ADDR1

    # 模拟点播：同映射解析应命中快路、不再探活
    play_probe_calls = []

    def play_probe(url, headers, timeout):
        play_probe_calls.append(url)
        return 206, None

    url = up.resolve_mapped_upstream_stream(
        db, movie.id, mapping_id=mapping.id,
        http_get=http_get, http_probe=play_probe)
    assert url == ADDR1
    assert play_probe_calls == []
