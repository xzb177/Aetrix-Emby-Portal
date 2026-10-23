"""Drive 302 只下发服务端跟链后的最终地址（2026-10-23 防盗链）。

此前三处 302 直接把 drive.usercontent.google.com/download?id=<file_id>
下发给客户端：file_id 明文 + 拼法公开 + anyone reader = 永久入口暴露。
drive_final_location 先调 resolve_drive_final_url 跟链，只下发短命的
最终地址；失败回落 legacy 保播放；成功结果缓存 300 秒。
"""
import asyncio
import os

os.environ.setdefault("DATABASE_TYPE", "sqlite")
os.environ.setdefault("REDIS_ENABLED", "false")

import pytest

from backend.emby_server import streaming
from backend.emby_server.drive_auth import drive_usercontent_download_url

FID = "1AbCdEfGhIjKlMnOpQrStUvWxYz"
FINAL = "https://rr4---sn-example.googleusercontent.com/videoplayback?expire=1899999999&sig=xyz"


@pytest.fixture(autouse=True)
def _clear_cache():
    streaming._FINAL_URL_CACHE.clear()
    yield
    streaming._FINAL_URL_CACHE.clear()


def _patch_resolver(monkeypatch, result=None, exc=None):
    calls = {"n": 0}

    async def fake(url, headers=None):
        calls["n"] += 1
        if exc is not None:
            raise exc
        return result

    monkeypatch.setattr(streaming, "resolve_drive_final_url", fake)
    return calls


def test_returns_final_url_not_usercontent(monkeypatch):
    _patch_resolver(monkeypatch, result=FINAL)
    out = asyncio.run(streaming.drive_final_location(FID))
    assert out == FINAL
    assert FID not in out


def test_success_is_cached(monkeypatch):
    calls = _patch_resolver(monkeypatch, result=FINAL)
    assert asyncio.run(streaming.drive_final_location(FID)) == FINAL
    assert asyncio.run(streaming.drive_final_location(FID)) == FINAL
    assert calls["n"] == 1


def test_failure_falls_back_to_legacy(monkeypatch):
    _patch_resolver(monkeypatch, result=None)
    out = asyncio.run(streaming.drive_final_location(FID))
    assert out == drive_usercontent_download_url(FID)


def test_exception_falls_back_to_legacy(monkeypatch):
    _patch_resolver(monkeypatch, exc=RuntimeError("boom"))
    out = asyncio.run(streaming.drive_final_location(FID))
    assert out == drive_usercontent_download_url(FID)


def test_failure_is_not_cached(monkeypatch):
    calls = _patch_resolver(monkeypatch, result=None)
    asyncio.run(streaming.drive_final_location(FID))
    asyncio.run(streaming.drive_final_location(FID))
    assert calls["n"] == 2


def test_empty_file_id():
    assert asyncio.run(streaming.drive_final_location("")) == ""
    assert asyncio.run(streaming.drive_final_location("  ")) == ""


def test_sync_wrapper(monkeypatch):
    _patch_resolver(monkeypatch, result=FINAL)
    assert streaming.drive_final_location_sync(FID) == FINAL
