# -*- coding: utf-8 -*-
"""招1「分块共享」+ 招4「请求捏合整形」回归测试（2026-10-16）。

覆盖 streaming.serve_remote_async 的分块层：
- 有界单段 Range 按固定块对齐取流，同块只打一次上游、跨子区间共享
- 取过的块进内存 LRU 缓存二次命中；文件尾短块按 total 截断口径组装
- 缓存键含鉴权摘要：不同凭据绝不共享
- 上游失败/闸超时一律回落原路径（singleflight/impl），绝不让播放失败
- 总开关关、开放 Range、非 Drive URL 一律不进块层（行为与升级前一致）

全部离线：上游由替身 impl 按虚拟文件切片应答，不做真实网络请求。
"""
import os

os.environ.setdefault("DATABASE_TYPE", "sqlite")
os.environ.setdefault("REDIS_ENABLED", "false")

import asyncio

import pytest
from fastapi import HTTPException
from fastapi.responses import StreamingResponse

from backend.emby_server import streaming

API_URL = "https://www.googleapis.com/drive/v3/files/FID123?alt=media"

# 虚拟文件：2 个整块 + 一个短尾（块大小在 fixture 里设为 1MiB）
BS = 1024 * 1024
TOTAL = 2 * BS + 12345
DATA = bytes((i * 7 + 13) % 256 for i in range(TOTAL))


class _FakeRequest:
    def __init__(self, range_header=None, method="GET"):
        self.headers = {}
        if range_header:
            self.headers["range"] = range_header
        self.method = method


@pytest.fixture(autouse=True)
def _clean_cs(monkeypatch):
    monkeypatch.setenv("PLAYBACK_CHUNK_SHARE_ENABLED", "1")
    monkeypatch.setenv("PLAYBACK_CHUNK_SIZE_BYTES", str(BS))
    monkeypatch.setenv("PLAYBACK_CHUNK_MAX_RANGE_BYTES", str(32 * BS))
    monkeypatch.setenv("PLAYBACK_CHUNK_CACHE_MB", "2048")
    monkeypatch.setenv("PLAYBACK_UPSTREAM_JITTER_MS", "0")
    monkeypatch.setenv("PLAYBACK_UPSTREAM_MAX_CONCURRENCY", "64")
    monkeypatch.setenv("PLAYBACK_UPSTREAM_PER_SA_CONCURRENCY", "8")
    monkeypatch.setenv("PLAYBACK_UPSTREAM_GATE_TIMEOUT_SEC", "10")
    streaming._CS_CACHE.clear()
    streaming._CS_CACHE_BYTES = 0
    streaming._CS_INFLIGHT.clear()
    streaming._CS_GATES.clear()
    streaming._CS_SA_GATES.clear()
    streaming._SF_INFLIGHT.clear()
    streaming._QUOTA_NEGATIVE_CACHE.clear()
    for k in streaming._CS_COUNTERS:
        streaming._CS_COUNTERS[k] = 0
    for k in streaming._SF_COUNTERS:
        streaming._SF_COUNTERS[k] = 0
    yield
    streaming._CS_CACHE.clear()
    streaming._CS_CACHE_BYTES = 0
    streaming._CS_INFLIGHT.clear()


def _install_fake_impl(monkeypatch, exc=None, delay=0.0):
    """替身上游：按请求 Range 从虚拟文件切片，语义与 Drive alt=media 对齐。"""
    calls = {"n": 0, "ranges": []}

    async def fake_impl(url, request, headers=None, media_type="video/mp4",
                        cache_control=None, pt=None):
        calls["n"] += 1
        if delay:
            await asyncio.sleep(delay)
        if exc is not None:
            raise exc
        rh = request.headers.get("range") or ""
        m = streaming.re.fullmatch(r"bytes=(\d+)-(\d*)", rh)
        assert m, f"替身只接单段 Range，收到 {rh!r}"
        start = int(m.group(1))
        end = int(m.group(2)) if m.group(2) else TOTAL - 1
        end = min(end, TOTAL - 1)
        calls["ranges"].append((start, end))
        piece = DATA[start:end + 1]

        async def gen():
            yield piece

        return StreamingResponse(
            gen(), status_code=206, media_type=media_type,
            headers={"Content-Range": f"bytes {start}-{end}/{TOTAL}",
                     "Accept-Ranges": "bytes"})

    monkeypatch.setattr(streaming, "_serve_remote_async_impl", fake_impl)
    return calls


def _serve(url, range_header, headers=None):
    return asyncio.run(streaming.serve_remote_async(
        url, _FakeRequest(range_header), headers or {}))


def test_chunk_layer_fetch_then_cache_hit(monkeypatch):
    calls = _install_fake_impl(monkeypatch)
    resp = _serve(API_URL, "bytes=0-99")
    assert resp.status_code == 206
    assert resp.body == DATA[0:100]
    assert resp.headers["content-range"] == f"bytes 0-99/{TOTAL}"
    assert calls["n"] == 1
    assert calls["ranges"] == [(0, BS - 1)]  # 取的是整块，不是客户端子区间
    # 第二次同区间：缓存命中，不再打上游
    resp2 = _serve(API_URL, "bytes=0-99")
    assert resp2.body == DATA[0:100]
    assert calls["n"] == 1
    assert streaming._CS_COUNTERS["hit"] >= 1


def test_chunk_layer_shares_block_across_subranges(monkeypatch):
    calls = _install_fake_impl(monkeypatch, delay=0.05)

    async def main():
        return await asyncio.gather(
            streaming.serve_remote_async(
                API_URL, _FakeRequest("bytes=0-100"), {}),
            streaming.serve_remote_async(
                API_URL, _FakeRequest("bytes=50-150"), {}))

    r1, r2 = asyncio.run(main())
    assert r1.body == DATA[0:101]
    assert r2.body == DATA[50:151]
    assert calls["n"] == 1  # 同块并发只取一次
    assert streaming._CS_COUNTERS["join"] >= 1


def test_chunk_layer_multi_block_and_tail(monkeypatch):
    calls = _install_fake_impl(monkeypatch)
    end = TOTAL - 1
    resp = _serve(API_URL, f"bytes={BS - 10}-{end}")
    assert resp.status_code == 206
    assert resp.body == DATA[BS - 10:]
    assert resp.headers["content-range"] == f"bytes {BS - 10}-{end}/{TOTAL}"
    # 跨 3 块（块1、块2、短尾块）各取一次
    assert calls["n"] == 3


def test_chunk_layer_auth_isolation(monkeypatch):
    calls = _install_fake_impl(monkeypatch)
    h1 = {"Authorization": "Bearer SA-ONE"}
    h2 = {"Authorization": "Bearer SA-TWO"}
    r1 = _serve(API_URL, "bytes=0-99", h1)
    r2 = _serve(API_URL, "bytes=0-99", h2)
    assert r1.body == r2.body == DATA[0:100]
    assert calls["n"] == 2  # 不同凭据不共享，各取一次


def test_chunk_layer_fallback_on_upstream_error(monkeypatch):
    calls = _install_fake_impl(
        monkeypatch, exc=HTTPException(status_code=403, detail="quota"))
    with pytest.raises(HTTPException):
        _serve(API_URL, "bytes=0-99")
    # 块层试一次（失败）→ 回落原路径再试一次 → 同样 403 抛给调用方
    assert calls["n"] == 2
    assert streaming._CS_COUNTERS["fetch_error"] >= 1


def test_chunk_layer_disabled_is_passthrough(monkeypatch):
    monkeypatch.setenv("PLAYBACK_CHUNK_SHARE_ENABLED", "0")
    calls = _install_fake_impl(monkeypatch)
    _serve(API_URL, "bytes=0-100")
    _serve(API_URL, "bytes=50-150")
    assert calls["n"] == 2  # 关开关：不同子区间各自取，无块共享
    assert streaming._CS_COUNTERS["fetch"] == 0


def test_chunk_layer_open_range_not_chunked(monkeypatch):
    calls = _install_fake_impl(monkeypatch)
    resp = _serve(API_URL, "bytes=0-")
    assert resp.status_code == 206
    assert calls["n"] == 1
    assert streaming._CS_COUNTERS["fetch"] == 0  # 开放区间走原路径


def test_chunk_layer_non_drive_url_not_chunked(monkeypatch):
    calls = _install_fake_impl(monkeypatch)
    _serve("http://example.com/file.mkv", "bytes=0-100")
    _serve("http://example.com/file.mkv", "bytes=50-150")
    assert streaming._CS_COUNTERS["fetch"] == 0
    assert calls["n"] == 2


def test_chunk_cache_eviction_prefers_nonzero_blocks(monkeypatch):
    # 块 256KiB、缓存上限 1MiB：只装得下 4 块
    monkeypatch.setenv("PLAYBACK_CHUNK_SIZE_BYTES", str(256 * 1024))
    monkeypatch.setenv("PLAYBACK_CHUNK_CACHE_MB", "1")
    calls = _install_fake_impl(monkeypatch)
    # 依次摸 0..4 号块（每块单独请求一个字节）→ 装第 5 块时要赶人
    for idx in range(5):
        _serve(API_URL, f"bytes={idx * 256 * 1024}-{idx * 256 * 1024}")
    keys = {k[1] for k in streaming._CS_CACHE}
    assert 0 in keys           # 0 号块（文件头）被优先保留
    assert len(keys) <= 4       # 硬上限没被突破
    assert streaming.chunk_share_counters()["cache_bytes"] <= 1024 * 1024


def test_gate_timeout_falls_back(monkeypatch):
    monkeypatch.setenv("PLAYBACK_UPSTREAM_MAX_CONCURRENCY", "1")
    monkeypatch.setenv("PLAYBACK_UPSTREAM_GATE_TIMEOUT_SEC", "0.5")
    calls = _install_fake_impl(monkeypatch)

    async def main():
        # 先把全局闸占死，块层领队等 0.5s 超时 → 整请求回落原路径成功
        loop_id = id(asyncio.get_running_loop())
        sem = streaming._cs_gate(streaming._CS_GATES, loop_id, 1)
        await sem.acquire()
        try:
            return await streaming.serve_remote_async(
                API_URL, _FakeRequest("bytes=0-99"), {})
        finally:
            sem.release()

    resp = asyncio.run(main())
    assert resp.status_code == 206
    assert resp.body == DATA[0:100]
    assert streaming._CS_COUNTERS["gate_timeout"] >= 1
    assert calls["n"] == 1  # 只有回落的那次真正取流
