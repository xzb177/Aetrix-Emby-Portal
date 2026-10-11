"""播放可观测（2026-10 简化）：计数器 + 快照

单路径，不再按线路分桶。全部离线：不发网络请求，不起播放器。
"""
from __future__ import annotations

import asyncio

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend.emby_server import line_health, line_stats


@pytest.fixture(autouse=True)
def _clean_counters():
    line_stats.reset()
    yield
    line_stats.reset()


@pytest.fixture()
def db():
    from backend import models as base_models

    engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
    base_models.Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    yield session
    session.close()


# ---------------------------------------------------------------------------
# 1. 计数器
# ---------------------------------------------------------------------------

def test_counter_records_requests_and_bytes():
    line_stats.record_request()
    line_stats.record_bytes(1024)
    line_stats.record_bytes(2048)
    row = line_stats.counter()
    assert row["requests"] == 1
    assert row["bytes_out"] == 3072
    assert row["idle_seconds"] is not None


def test_counter_ignores_nonpositive_bytes():
    line_stats.record_request()
    line_stats.record_bytes(0)
    line_stats.record_bytes(-5)
    row = line_stats.counter()
    assert row["requests"] == 1 and row["bytes_out"] == 0


# ---------------------------------------------------------------------------
# 2. 响应包装：字节口径宁窄不虚高
# ---------------------------------------------------------------------------

def _run_body_iterator(response) -> int:
    total = 0

    async def _drain():
        nonlocal total
        async for chunk in response.body_iterator:
            total += len(chunk)

    asyncio.run(_drain())
    return total


def test_observe_traffic_counts_bytes_of_streaming_response():
    from starlette.responses import StreamingResponse

    from backend.emby_server.api import _observe_traffic

    async def _chunks():
        yield b"a" * 100
        yield b"b" * 50

    response = _observe_traffic(StreamingResponse(_chunks()))
    assert _run_body_iterator(response) == 150
    assert line_stats.counter()["bytes_out"] == 150
    assert line_stats.counter()["requests"] == 1


def test_observe_traffic_does_not_fake_bytes_for_plain_response():
    from starlette.responses import PlainTextResponse, Response

    from backend.emby_server.api import _observe_traffic

    response = _observe_traffic(
        Response(content=b"x" * 4096, headers={"content-length": "4096"}))
    assert line_stats.counter()["bytes_out"] == 0
    assert line_stats.counter()["requests"] == 1
    assert isinstance(_observe_traffic(PlainTextResponse("ok")), Response)


def test_observe_traffic_still_counts_bytes_when_client_aborts_midway():
    from starlette.responses import StreamingResponse

    from backend.emby_server.api import _observe_traffic

    async def _chunks():
        yield b"a" * 100
        raise RuntimeError("client gone")

    response = _observe_traffic(StreamingResponse(_chunks()))
    with pytest.raises(RuntimeError):
        _run_body_iterator(response)
    assert line_stats.counter()["bytes_out"] == 100


# ---------------------------------------------------------------------------
# 3. 快照组装
# ---------------------------------------------------------------------------

def test_snapshot_has_single_path_shape(db):
    snap = line_health.snapshot(db)
    assert snap["path"] == "relay"
    assert snap["label"] == "代理中转"
    assert "cdn" in snap and "cache" in snap
    assert snap["cdn"]["enabled"] is False
    assert snap["cache"]["enabled"] is False


def test_snapshot_counts_traffic(db):
    line_stats.record_request()
    line_stats.record_bytes(500)
    snap = line_health.snapshot(db)
    assert snap["requests"] == 1
    assert snap["bytes_out"] == 500
def test_snapshot_calls_cache_state_exactly_once(db, monkeypatch):
    """回归：snapshot() 内部 _cache_state(db) 只允许调用一次，避免数据不一致与性能退化。"""
    calls = {"n": 0}

    def _fake_cache_state(_db):
        # 每次调用计数，并返回固定快照数据
        calls["n"] += 1
        return {
            "hits": 7,
            "misses": 3,
            "hit_rate": 0.7,
            "enabled": True,
            "dir_exists": True,
            "dir": "/tmp/x",
            "bytes_used": 100,
            "max_bytes": 1000,
            "entries_total": 5,
        }

    monkeypatch.setattr(line_health, "_cache_state", _fake_cache_state)

    snap = line_health.snapshot(db)

    # 只应调用一次
    assert calls["n"] == 1
    # 同一快照来源，数据自洽
    assert snap["cache"]["hits"] == 7
    assert snap["cache"]["misses"] == 3
    assert snap["cache"]["hit_rate"] == 0.7
