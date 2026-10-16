"""Singleflight：同文件同 Range 并发请求合并（serve_remote_async 包装层）。

- 同 key（URL+Range+鉴权摘要）并发只真正取流一次，结果共享
- 不同 Range / 开放 Range / 无 Range / 不同凭据不合并
- 领队异常（配额 403 等）由等待者共享，不重复打源站
- 领队正文超限（上游无视 Range）→ 全部回落独立取流
"""
from __future__ import annotations

import asyncio

import pytest
from fastapi import HTTPException
from fastapi.responses import StreamingResponse

from backend.emby_server import streaming


class _FakeRequest:
    def __init__(self, range_header=None, method="GET"):
        self.headers = {}
        if range_header:
            self.headers["range"] = range_header
        self.method = method


@pytest.fixture(autouse=True)
def _clean_sf():
    streaming._SF_INFLIGHT.clear()
    for k in streaming._SF_COUNTERS:
        streaming._SF_COUNTERS[k] = 0
    yield
    streaming._SF_INFLIGHT.clear()


def _install_fake_impl(monkeypatch, body=b"hello-world", delay=0.05, exc=None):
    calls = {"n": 0}

    async def fake_impl(url, request, headers=None, media_type="video/mp4",
                        cache_control=None, pt=None):
        calls["n"] += 1
        if delay:
            await asyncio.sleep(delay)
        if exc is not None:
            raise exc

        async def gen():
            yield body

        return StreamingResponse(
            gen(), status_code=206, media_type=media_type,
            headers={"Content-Range": "bytes 0-10/100"})

    monkeypatch.setattr(streaming, "_serve_remote_async_impl", fake_impl)
    return calls


def _body(resp) -> bytes:
    body = getattr(resp, "body", None)
    if body is not None:
        return body

    async def _read():
        out = bytearray()
        async for chunk in resp.body_iterator:
            out += chunk
        return bytes(out)

    return asyncio.run(_read())


def test_singleflight_merges_concurrent_identical_range(monkeypatch):
    calls = _install_fake_impl(monkeypatch)

    async def main():
        return await asyncio.gather(
            streaming.serve_remote_async(
                "http://x/f", _FakeRequest("bytes=0-10"), {}),
            streaming.serve_remote_async(
                "http://x/f", _FakeRequest("bytes=0-10"), {}),
        )

    r1, r2 = asyncio.run(main())
    assert calls["n"] == 1
    assert _body(r1) == b"hello-world"
    assert _body(r2) == b"hello-world"
    assert r1.status_code == 206 and r2.status_code == 206
    assert r1.headers.get("content-range") == "bytes 0-10/100"
    assert streaming._SF_COUNTERS["joined"] == 1


def test_singleflight_different_ranges_not_merged(monkeypatch):
    calls = _install_fake_impl(monkeypatch)

    async def main():
        await asyncio.gather(
            streaming.serve_remote_async(
                "http://x/f", _FakeRequest("bytes=0-10"), {}),
            streaming.serve_remote_async(
                "http://x/f", _FakeRequest("bytes=11-21"), {}),
        )

    asyncio.run(main())
    assert calls["n"] == 2


def test_singleflight_open_range_not_merged(monkeypatch):
    calls = _install_fake_impl(monkeypatch)

    async def main():
        await asyncio.gather(
            streaming.serve_remote_async(
                "http://x/f", _FakeRequest("bytes=0-"), {}),
            streaming.serve_remote_async(
                "http://x/f", _FakeRequest("bytes=0-"), {}),
        )

    asyncio.run(main())
    assert calls["n"] == 2


def test_singleflight_no_range_not_merged(monkeypatch):
    calls = _install_fake_impl(monkeypatch)

    async def main():
        await asyncio.gather(
            streaming.serve_remote_async("http://x/f", _FakeRequest(None), {}),
            streaming.serve_remote_async("http://x/f", _FakeRequest(None), {}),
        )

    asyncio.run(main())
    assert calls["n"] == 2


def test_singleflight_auth_header_separates(monkeypatch):
    calls = _install_fake_impl(monkeypatch)

    async def main():
        await asyncio.gather(
            streaming.serve_remote_async(
                "http://x/f", _FakeRequest("bytes=0-10"),
                {"Authorization": "Bearer A"}),
            streaming.serve_remote_async(
                "http://x/f", _FakeRequest("bytes=0-10"),
                {"Authorization": "Bearer B"}),
        )

    asyncio.run(main())
    assert calls["n"] == 2


def test_singleflight_range_over_cap_not_merged(monkeypatch):
    calls = _install_fake_impl(monkeypatch)
    big_range = "bytes=0-%d" % (64 * 1024 * 1024)  # 64MiB+1 字节，超默认上限

    async def main():
        await asyncio.gather(
            streaming.serve_remote_async("http://x/f", _FakeRequest(big_range), {}),
            streaming.serve_remote_async("http://x/f", _FakeRequest(big_range), {}),
        )

    asyncio.run(main())
    assert calls["n"] == 2


def test_singleflight_leader_error_shared(monkeypatch):
    calls = _install_fake_impl(
        monkeypatch, exc=HTTPException(status_code=403, detail="quota"))

    async def main():
        return await asyncio.gather(
            streaming.serve_remote_async(
                "http://x/f", _FakeRequest("bytes=0-10"), {}),
            streaming.serve_remote_async(
                "http://x/f", _FakeRequest("bytes=0-10"), {}),
            return_exceptions=True,
        )

    results = asyncio.run(main())
    assert calls["n"] == 1
    assert all(isinstance(r, HTTPException) and r.status_code == 403
               for r in results)


def test_singleflight_oversized_body_falls_back(monkeypatch):
    # 上游无视 Range 回了大正文：领队收齐超限 → 两边都回落独立取流
    big = b"x" * (9 * 1024 * 1024)
    calls = _install_fake_impl(monkeypatch, body=big)

    async def main():
        return await asyncio.gather(
            streaming.serve_remote_async(
                "http://x/f", _FakeRequest("bytes=0-10"), {}),
            streaming.serve_remote_async(
                "http://x/f", _FakeRequest("bytes=0-10"), {}),
        )

    r1, r2 = asyncio.run(main())
    assert calls["n"] == 3  # 1 次领队 + 2 次回落
    assert len(_body(r1)) == len(big)
    assert len(_body(r2)) == len(big)
    assert streaming._SF_COUNTERS["fallback"] == 2
