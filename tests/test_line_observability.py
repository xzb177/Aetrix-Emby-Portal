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
    assert snap["label"] == "302 直链"
    # 删除后口径：cache 路随本地缓存整删（PR-B 删了 line_health._cache_state）；
    # cdn 段随 CDN 配置能力整删（删除提交删了 line_health._cdn_state 与快照 "cdn" 键），故不在。
    assert "cdn" not in snap
    assert "cache" not in snap


def test_snapshot_counts_traffic(db):
    line_stats.record_request()
    line_stats.record_bytes(500)
    snap = line_health.snapshot(db)
    assert snap["requests"] == 1
    assert snap["bytes_out"] == 500
# 注：test_snapshot_calls_cache_state_exactly_once 已随 PR-B 删除——
# 它测的是 line_health._cache_state 的调用次数，而该函数已按删除清单
# （line_health 的 _cache_state）整删；snapshot() 不再产出 cache 段。
