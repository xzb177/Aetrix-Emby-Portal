# -*- coding: utf-8 -*-
"""首次播放（first_play）：预检轮换 + 预热缓存的单元测试。

不碰真实谷歌、不碰真实 SA：probe 与 token_fn 全部注入。
不按播放器名分流：本模块签名里没有任何客户端名参数。
"""
import time

import pytest

from backend.emby_server import first_play as fp


def _probe(status, ct="video/x-matroska"):
    def _fn(url, token):
        return {"ok": status == 206, "status": status,
                "content_type": ct if status == 206 else "text/html",
                "content_range": "bytes 0-0/1000" if status == 206 else None,
                "ttfb_ms": 5}
    return _fn


def test_probe_206_ok_and_403_not_ok():
    ok = fp.probe_alt_media("https://example.invalid/f", "tok",
                            _http=lambda u, h, t: type("R", (), {
                                "status": 206,
                                "headers": {"Content-Type": "video/x-matroska",
                                            "Content-Range": "bytes 0-0/1000"}})())
    assert ok["ok"] is True and ok["status"] == 206
    bad = fp.probe_alt_media("https://example.invalid/f", "tok",
                             _http=lambda u, h, t: type("R", (), {
                                 "status": 403, "headers": {}})())
    assert bad["ok"] is False and bad["status"] == 403


def test_resolve_rotates_on_403_and_uses_good_token(monkeypatch):
    monkeypatch.setattr(fp, "alt_media_url", lambda fid: "https://example.invalid/f")
    tokens = ["bad-token", "good-token"]
    seen = []

    def token_fn(attempt):
        seen.append(attempt)
        return tokens[attempt] if attempt < len(tokens) else None

    def probe(url, token):
        return _probe(403 if token == "bad-token" else 206)(url, token)

    result = fp.resolve_verified_alt_media("fid1", token_fn, probe=probe)
    assert result["ok"] is True
    assert result["token"] == "good-token"
    assert result["rotations"] == 1  # 第 2 个 SA 才通
    assert seen == [0, 1]


def test_resolve_gives_up_after_max_rotations_and_reports_last_status(monkeypatch):
    monkeypatch.setattr(fp, "alt_media_url", lambda fid: "https://example.invalid/f")
    result = fp.resolve_verified_alt_media(
        "fid1", lambda attempt: "bad", max_rotations=3,
        probe=lambda u, t: _probe(403)(u, t))
    assert result["ok"] is False
    assert result["last_status"] == 403
    assert result["rotations"] == 3


def test_resolve_does_not_rotate_on_429(monkeypatch):
    monkeypatch.setattr(fp, "alt_media_url", lambda fid: "https://example.invalid/f")
    calls = []

    def token_fn(attempt):
        calls.append(attempt)
        return "tok"

    result = fp.resolve_verified_alt_media(
        "fid1", token_fn, probe=lambda u, t: _probe(429)(u, t))
    assert result["ok"] is False and calls == [0]  # 429 换 SA 无帮助，直接放弃


def test_resolve_missing_token_is_skipped(monkeypatch):
    monkeypatch.setattr(fp, "alt_media_url", lambda fid: "https://example.invalid/f")
    result = fp.resolve_verified_alt_media(
        "fid1", lambda attempt: None if attempt == 0 else "good",
        probe=lambda u, t: _probe(206)(u, t))
    assert result["ok"] is True and result["rotations"] == 1


def test_warm_store_load_roundtrip_and_fid_isolation():
    store = {}

    def cache_set(k, v, ttl):
        store[k] = v

    def cache_get(k):
        return store.get(k)

    assert fp.store_warm(cache_set, "guid1", 7,
                         {"url": "u", "token": "t", "fid": "fid1",
                          "rotations": 0, "tail_ok": True}) is True
    entry = fp.load_warm(cache_get, "guid1", 7)
    assert entry is not None and entry["token"] == "t" and entry["fid"] == "fid1"
    # 另一用户读不到同一条预热（键按用户隔离）。
    assert fp.load_warm(cache_get, "guid1", 8) is None


def test_warm_usable_fresh_and_stale(monkeypatch):
    monkeypatch.delenv("FIRST_PLAY_WARM_FRESH_SEC", raising=False)
    now = time.time()
    fresh = {"url": "u", "token": "t", "warmed_at": now - 60}
    widened = {"url": "u", "token": "t", "warmed_at": now - 150}
    stale = {"url": "u", "token": "t", "warmed_at": now - 200}
    assert fp.warm_usable(fresh, now=now) is True
    assert fp.warm_usable(widened, now=now) is True  # 默认新鲜窗口 180（原 120）
    assert fp.warm_usable(stale, now=now) is False
    assert fp.warm_usable({"url": "u", "warmed_at": now}) is False  # 无令牌不可用


def test_warm_windows_read_env(monkeypatch):
    monkeypatch.setenv("FIRST_PLAY_WARM_FRESH_SEC", "90")
    monkeypatch.setenv("FIRST_PLAY_WARM_TTL_SEC", "42")
    assert fp.warm_fresh_sec() == 90
    assert fp.warm_ttl_sec() == 42
    now = time.time()
    assert fp.warm_usable(
        {"url": "u", "token": "t", "warmed_at": now - 100}, now=now) is False
    seen = {}
    assert fp.store_warm(
        lambda k, v, ttl: seen.setdefault("ttl", ttl),
        "g", 1, {"url": "u", "token": "t", "fid": "f"}) is True
    assert seen["ttl"] == 42
    monkeypatch.setenv("FIRST_PLAY_WARM_FRESH_SEC", "nope")
    monkeypatch.setenv("FIRST_PLAY_WARM_TTL_SEC", "")
    assert fp.warm_fresh_sec() == 180
    assert fp.warm_ttl_sec() == 300
    monkeypatch.setenv("FIRST_PLAY_WARM_FRESH_SEC", "-3")
    assert fp.warm_fresh_sec() == 0


def test_no_client_name_in_first_play_api():
    import inspect

    sigs = [inspect.signature(fp.resolve_verified_alt_media),
            inspect.signature(fp.warm_first_play_sync),
            inspect.signature(fp.probe_alt_media)]
    joined = " ".join(str(s) for s in sigs).lower()
    for name in ("rex", "senplayer", "vidhub", "client_name", "device_name"):
        assert name not in joined
