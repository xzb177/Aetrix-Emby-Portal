# -*- coding: utf-8 -*-
"""IntroDB 自动片头片尾（2026-10-17 用户拍板学 raisemby）。

覆盖：上游响应解析（多类型/空/坏字段）、缓存命中不外呼、负缓存、
开关关零外呼、手动优先于 IntroDB、超时/异常时照常返回 []。

全部离线：httpx.get 全部 mock 掉，不许真打 api.introdb.app。
"""
import os

os.environ.setdefault("DATABASE_TYPE", "sqlite")
os.environ.setdefault("REDIS_ENABLED", "false")

from datetime import datetime, timedelta
from types import SimpleNamespace

import httpx
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend import models as core_models
from backend.emby_server import introdb
from backend.emby_server import models as em
from backend.emby_server import playback_strategy as _ps
from backend.integrations import store


@pytest.fixture()
def db():
    # store 的进程内 TTL 缓存跨测试残留（开关写"false"会被后面的测试读到），
    # 每个测试前后都清掉，保证开关读的是本测试自己的库。
    store.invalidate(_ps.KEY_INTRODB_ENABLED)
    engine = create_engine("sqlite://")
    core_models.Base.metadata.create_all(engine)
    em.Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    yield session
    session.close()
    store.invalidate(_ps.KEY_INTRODB_ENABLED)


def _item(**kw):
    base = {"id": 1, "item_type": "episode", "imdb_id": "tt0903747",
            "season_number": 1, "episode_number": 1}
    base.update(kw)
    return SimpleNamespace(**base)


class _Resp:
    def __init__(self, status_code=200, payload=None):
        self.status_code = status_code
        self._payload = payload

    def json(self):
        return self._payload


# ---------------- 响应解析 ----------------


def test_parse_segments_multi_type_ms():
    payload = {"segments": [
        {"segment_type": "intro", "start_sec": 12.5, "end_sec": 72.0},
        {"segment_type": "recap", "start_sec": 0, "end_sec": 45.2},
        {"segment_type": "outro", "start_sec": 2500.0, "end_sec": 2600.5},
        {"segment_type": "post-credits", "start_sec": 2700, "end_sec": 2710},
        {"segment_type": "credits", "start_sec": 2600, "end_sec": 2650},
        {"segment_type": "ad", "start_sec": 100, "end_sec": 200},      # 未知类型丢掉
        {"segment_type": "intro", "start_sec": 100, "end_sec": 50},    # 坏区间丢掉
        {"segment_type": "intro", "start_sec": "x", "end_sec": 60},    # 非数字丢掉
        {"segment_type": "intro", "start_sec": 10},                   # 缺字段丢掉
        "not-a-dict",                                                  # 非字典丢掉
    ]}
    out = introdb._parse_segments(payload)
    by_type = {s["segment_type"]: s for s in out}
    assert set(by_type) == {"intro", "recap", "outro", "credits", "post-credits"}
    assert by_type["intro"]["start_ms"] == 12500
    assert by_type["intro"]["end_ms"] == 72000
    assert by_type["outro"]["start_ms"] == 2500000


def test_parse_segments_list_payload_and_empty():
    assert introdb._parse_segments(
        [{"segment_type": "intro", "start_sec": 5, "end_sec": 65}]) == [
        {"segment_type": "intro", "start_ms": 5000, "end_ms": 65000}]
    assert introdb._parse_segments({"segments": []}) == []
    assert introdb._parse_segments({}) == []
    assert introdb._parse_segments(None) == []


# ---------------- fetch 上游 ----------------


def test_fetch_segments_episode_params(monkeypatch):
    seen = {}

    def fake_get(url, params=None, headers=None, timeout=None):
        seen.update({"url": url, "params": params})
        return _Resp(200, {"segments": [{"segment_type": "intro", "start_sec": 1, "end_sec": 61}]})

    monkeypatch.setattr(httpx, "get", fake_get)
    out = introdb.fetch_segments("tt0903747", season=2, episode=3)
    assert seen["url"] == "https://api.introdb.app/segments"
    assert seen["params"] == {"imdb_id": "tt0903747", "season": "2", "episode": "3"}
    assert out == [{"segment_type": "intro", "start_ms": 1000, "end_ms": 61000}]


def test_fetch_segments_movie_params(monkeypatch):
    seen = {}

    def fake_get(url, params=None, headers=None, timeout=None):
        seen.update({"params": params})
        return _Resp(200, {"segments": []})

    monkeypatch.setattr(httpx, "get", fake_get)
    assert introdb.fetch_segments("tt0371746", is_movie=True) == []
    assert seen["params"] == {"imdb_id": "tt0371746", "is_movie": "true"}


def test_fetch_segments_episode_missing_numbers_no_call(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("不应外呼")

    monkeypatch.setattr(httpx, "get", boom)
    # 剧集缺季/集号：省一次外呼，直接 None
    assert introdb.fetch_segments("tt0903747") is None


def test_fetch_segments_timeout_and_non200_return_none(monkeypatch):
    def timeout(*a, **k):
        raise httpx.ConnectTimeout("boom")

    monkeypatch.setattr(httpx, "get", timeout)
    assert introdb.fetch_segments("tt0903747", season=1, episode=1) is None

    monkeypatch.setattr(httpx, "get", lambda *a, **k: _Resp(500, None))
    assert introdb.fetch_segments("tt0903747", is_movie=True) is None


# ---------------- 缓存与开关 ----------------


def _seed_row(db, item_id=1, stype="intro", days_ago=0, start_ms=1000, end_ms=61000):
    db.add(em.IntroSegment(
        item_id=item_id, segment_type=stype, start_ms=start_ms, end_ms=end_ms,
        source="introdb", fetched_at=datetime.now() - timedelta(days=days_ago)))
    db.commit()


def test_cache_hit_no_http(db, monkeypatch):
    _seed_row(db)
    _seed_row(db, stype="outro", start_ms=2500000, end_ms=2600000)

    def boom(*a, **k):
        raise AssertionError("缓存命中时不应外呼")

    monkeypatch.setattr(httpx, "get", boom)
    out = introdb.get_segments_for_item(db, _item())
    assert {s["segment_type"] for s in out} == {"intro", "outro"}
    assert out[0]["source"] == "introdb"


def test_stale_cache_refetch_and_replace(db, monkeypatch):
    _seed_row(db, days_ago=40)  # 超过 30 天正缓存期

    def fake_get(*a, **k):
        return _Resp(200, {"segments": [{"segment_type": "recap", "start_sec": 0, "end_sec": 30}]})

    monkeypatch.setattr(httpx, "get", fake_get)
    out = introdb.get_segments_for_item(db, _item())
    assert out == [{"segment_type": "recap", "start_ms": 0, "end_ms": 30000, "source": "introdb"}]
    # 旧行被替换：库里只剩 recap
    rows = db.query(em.IntroSegment).filter(em.IntroSegment.item_id == 1).all()
    assert [r.segment_type for r in rows] == ["recap"]


def test_negative_cache_hit_and_stale(db, monkeypatch):
    calls = []

    def fake_get(*a, **k):
        calls.append(1)
        return _Resp(200, {"segments": []})

    monkeypatch.setattr(httpx, "get", fake_get)
    # 新鲜负缓存：不外呼
    _seed_row(db, stype="none", start_ms=0, end_ms=0, days_ago=2)
    assert introdb.get_segments_for_item(db, _item()) == []
    assert calls == []
    # 过期负缓存（8 天 > 7 天）：重新拉，落哨兵行
    db.query(em.IntroSegment).delete()
    db.commit()
    _seed_row(db, stype="none", start_ms=0, end_ms=0, days_ago=8)
    assert introdb.get_segments_for_item(db, _item()) == []
    assert len(calls) == 1
    rows = db.query(em.IntroSegment).all()
    assert len(rows) == 1 and rows[0].segment_type == "none"


def test_switch_off_zero_http(db, monkeypatch):
    store.write_values(db, {_ps.KEY_INTRODB_ENABLED: "false"}, None)
    db.commit()

    def boom(*a, **k):
        raise AssertionError("开关关时不应外呼")

    monkeypatch.setattr(httpx, "get", boom)
    assert introdb.get_segments_for_item(db, _item()) == []


def test_switch_default_on(db, monkeypatch):
    def fake_get(*a, **k):
        return _Resp(200, {"segments": [{"segment_type": "intro", "start_sec": 1, "end_sec": 61}]})

    monkeypatch.setattr(httpx, "get", fake_get)
    out = introdb.get_segments_for_item(db, _item())
    assert out[0]["segment_type"] == "intro"


def test_no_imdb_id_no_http(db, monkeypatch):
    def boom(*a, **k):
        raise AssertionError("无 imdb_id 不应外呼")

    monkeypatch.setattr(httpx, "get", boom)
    assert introdb.get_segments_for_item(db, _item(imdb_id="")) == []
    assert introdb.get_segments_for_item(db, _item(item_type="season")) == []


def test_upstream_failure_keeps_playback_alive(db, monkeypatch):
    # 拉取抛异常 → 当无分段，不抛；旧缓存不动
    def boom(*a, **k):
        raise httpx.ReadTimeout("boom")

    monkeypatch.setattr(httpx, "get", boom)
    monkeypatch.setattr(introdb, "fetch_segments", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("x")))
    assert introdb.get_segments_for_item(db, _item()) == []


# ---------------- 合并：手动优先 ----------------


def test_merge_manual_wins_and_type_filter():
    manual = [{"marker_type": "intro", "start_ms": 0, "end_ms": 60000, "source": "manual"}]
    extra = [
        {"segment_type": "intro", "start_ms": 1000, "end_ms": 61000, "source": "introdb"},
        {"segment_type": "recap", "start_ms": 0, "end_ms": 30000, "source": "introdb"},
        {"segment_type": "outro", "start_ms": 2500000, "end_ms": 2600000, "source": "introdb"},
        {"segment_type": "post-credits", "start_ms": 2700000, "end_ms": 2710000, "source": "introdb"},
    ]
    merged = introdb.merge_segments(manual, extra, "episode")
    types = [m["marker_type"] for m in merged]
    # 手动 intro 赢（IntroDB 的 intro 被丢掉）；剧集只要 intro/recap/outro
    assert types == ["intro", "recap", "outro"]
    assert merged[0]["source"] == "manual"
    assert merged[1] == {"marker_type": "recap", "start_ms": 0, "end_ms": 30000, "source": "introdb"}

    merged_movie = introdb.merge_segments([], extra, "movie")
    # 电影只要 outro/post-credits
    assert [m["marker_type"] for m in merged_movie] == ["outro", "post-credits"]

    # 其他类型不处理
    assert introdb.merge_segments(manual, extra, "season") == manual


def test_chapters_pipeline_unchanged():
    from backend.emby_server import intro_marker as _im
    chapters = _im.to_chapters([
        {"marker_type": "recap", "start_ms": 0, "end_ms": 30000, "source": "introdb"}])
    assert chapters[0]["StartPositionTicks"] == 0
    assert chapters[0]["EndPositionTicks"] == 30000 * 10000
    assert chapters[0]["MarkerType"] == "recap"
