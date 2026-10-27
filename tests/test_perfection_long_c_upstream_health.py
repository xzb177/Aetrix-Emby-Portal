# -*- coding: utf-8 -*-
"""后端完美审查 C 批（上游播放与健康）：证据确凿的三项小修。

① next_probe_due 退避指数溢出：长死源连续失败约 1020 次后
   interval * 2**extra 抛 OverflowError，异常在 set_source 之前
   炸掉 probe_source、连带 abort 整轮 probe_sources_once（后排源
   当轮全被跳过）。修法是指数先封顶（2**60 已远超任何退避上限），
   实际退避值不变。
② 脏 Redis 健康值：_record_from_json 只拦了 JSON 解析与非 dict，
   数值字段写坏（如 failures 存成 "abc"）时 int/float 转换照样抛，
   且 get_source 只包了 Redis 读本身，异常穿出 probe_source 同样
   abort 整轮。修法是数值字段坏掉即整条按无记录计（fail-open，
   与 get_line 的坏 JSON 同口径）。
③ record_mapping 的 episode_index：模块与模型 docstring 定死
   “播放组内 1 起序号、电影为 None”，此前 0 / 负数直接入库。
   修法是非 None 时走 _as_pos_int 校验，不静默截断。

健康过期问题（本批重点查项）不在此修：30 天未探活的 Active
记录经 sync_health_to_pg 仍原样落 healthy、is_source_available
仍判可用——过期阈值与降级目标（unknown 还是 unhealthy）是
产品语义，须用户定，不在本批悄悄定阈值。见回传 backlog。
全部离线：Redis 用内存/替身，DB 用 sqlite 内存库。
"""

import os

os.environ.setdefault("DATABASE_TYPE", "sqlite")
os.environ.setdefault("REDIS_ENABLED", "false")

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend import models as base_models
from backend.emby_server import models as em
from backend.emby_server import upstream_health as uh
from backend.emby_server import upstream_item_mappings as uim


@pytest.fixture()
def db_factory():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False},
                           poolclass=StaticPool)
    base_models.Base.metadata.create_all(engine)
    em.Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)


@pytest.fixture()
def db(db_factory):
    session = db_factory()
    yield session
    session.close()


@pytest.fixture(autouse=True)
def _probe_on(monkeypatch):
    monkeypatch.setenv("UPSTREAM_HEALTH_PROBE_ENABLED", "1")
    monkeypatch.setenv("PROVIDER_BREAKER_ENABLED", "0")
    monkeypatch.setenv(uim.MAPPING_ENABLED_ENV, "1")


# ---------- ① 退避指数溢出 ----------

def test_next_probe_due_huge_failures_does_not_overflow():
    rec = uh.SourceHealthRecord(
        source_key="dead", availability_state=uh.STATE_UNAVAILABLE,
        consecutive_unavailable_failures=3 + 2000)
    assert uh.next_probe_due(rec, interval_sec=60.0, backoff_max_sec=600.0,
                             threshold=3, now=1000.0) == 1600.0


def test_next_probe_due_normal_values_unchanged():
    rec = uh.SourceHealthRecord(
        source_key="x", availability_state=uh.STATE_UNAVAILABLE,
        consecutive_unavailable_failures=3)
    assert uh.next_probe_due(rec, interval_sec=60.0, backoff_max_sec=600.0,
                             threshold=3, now=1000.0) == 1060.0
    rec.consecutive_unavailable_failures = 13  # 2**10 * 60 远超上限
    assert uh.next_probe_due(rec, interval_sec=60.0, backoff_max_sec=600.0,
                             threshold=3, now=1000.0) == 1600.0
    rec2 = uh.SourceHealthRecord(
        source_key="y", availability_state=uh.STATE_ACTIVE,
        consecutive_unavailable_failures=2)
    assert uh.next_probe_due(rec2, interval_sec=60.0, backoff_max_sec=600.0,
                             threshold=3, now=1000.0) == 1060.0


def test_dead_source_does_not_starve_later_sources():
    """长死源在前、活源在后：一轮探活后两源都有结论（修前整轮 abort）。"""
    store = uh.InMemoryHealthStore()
    store.set_source(uh.SourceHealthRecord(
        source_key="dead", availability_state=uh.STATE_UNAVAILABLE,
        consecutive_unavailable_failures=3 + 2000, next_probe_at=0.0))
    dead = uh.UpstreamSource(key="dead", kind="emby",
                             base_url="https://dead.example.com")
    live = uh.UpstreamSource(key="live", kind="emby",
                             base_url="https://live.example.com")

    def http_get(url, headers, timeout):
        if "dead" in url:
            return None, None
        return 200, {"Id": "live-server", "ServerName": "live"}

    results = uh.probe_sources_once([dead, live], store, http_get=http_get,
                                    now=1000.0)
    assert [r["key"] for r in results] == ["dead", "live"]
    assert results[1]["outcome"] == "success"
    assert store.get_source("live").availability_state == uh.STATE_ACTIVE


# ---------- ② 脏 Redis 健康值 ----------

@pytest.mark.parametrize("payload", [
    '{"availability_state":"Active","consecutive_unavailable_failures":"abc"}',
    '{"availability_state":"Active","next_probe_at":"xyz"}',
    '{"availability_state":"Active","consecutive_unavailable_failures":[1]}',
    '{"availability_state":"Active","next_probe_at":null,"consecutive_unavailable_failures":null}',
])
def test_dirty_record_json_is_treated_as_no_record(payload):
    # 最后一项是合法的 null 口径：or 0.0 兜底，应正常解析而非按坏值计。
    rec = uh._record_from_json("1", payload)
    if "null" in payload:
        assert rec is not None and rec.next_probe_at == 0.0
    else:
        assert rec is None


def test_redis_store_dirty_value_does_not_raise():
    class FakeRedis:
        def get(self, key):
            return (b'{"availability_state":"Active",'
                    b'"consecutive_unavailable_failures":"abc"}')
        def set(self, key, value):
            raise AssertionError("本用例不应写 Redis")

    store = uh.RedisHealthStore(client=FakeRedis())
    assert store.get_source("1") is None


def test_redis_store_valid_value_still_parses():
    class FakeRedis:
        def get(self, key):
            return (b'{"availability_state":"Unavailable",'
                    b'"consecutive_unavailable_failures":5,"next_probe_at":123.0}')
        def set(self, key, value):
            raise AssertionError("本用例不应写 Redis")

    store = uh.RedisHealthStore(client=FakeRedis())
    rec = store.get_source("1")
    assert rec is not None
    assert rec.availability_state == uh.STATE_UNAVAILABLE
    assert rec.consecutive_unavailable_failures == 5
    assert rec.next_probe_at == 123.0


# ---------- ③ episode_index 域校验 ----------

@pytest.mark.parametrize("bad_episode", [0, -1, -3])
def test_record_mapping_rejects_non_positive_episode(db, bad_episode):
    with pytest.raises(ValueError):
        uim.record_mapping(db, local_item_id=10, source_id=3,
                           upstream_item_id="ep-bad", confidence=0.9,
                           episode_index=bad_episode)
    assert db.query(em.UpstreamItemMapping).count() == 0


def test_record_mapping_accepts_positive_episode_and_none_keeps_old(db):
    row = uim.record_mapping(db, local_item_id=10, source_id=3,
                             upstream_item_id="ep-ok", confidence=0.9,
                             episode_index=2)
    assert row.episode_index == 2
    row2 = uim.record_mapping(db, local_item_id=10, source_id=3,
                              upstream_item_id="ep-ok", confidence=0.8,
                              episode_index=None)
    assert row2.episode_index == 2
    movie = uim.record_mapping(db, local_item_id=11, source_id=3,
                               upstream_item_id="movie", confidence=0.9,
                               episode_index=None)
    assert movie.episode_index is None
