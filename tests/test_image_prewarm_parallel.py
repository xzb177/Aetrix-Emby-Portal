# -*- coding: utf-8 -*-
"""v2.57.0 提速包 item4：image_store.prewarm 单批内并行下载。

- 去重语义不变（重复 URL 只算一次）
- 空输入返回 0
- 并行正确性：每个唯一 URL 都被尝试且仅一次，成功计数对
- 并行确实快：4 张 × 0.3s/张，串行 1.2s，并行应 < 0.9s
"""
import os
import time

os.environ.setdefault("DATABASE_TYPE", "sqlite")
os.environ.setdefault("REDIS_ENABLED", "false")

from backend.emby_server import image_store


def test_prewarm_dedup_and_count(monkeypatch):
    seen = []

    def fake_localize(url, attempts=1):
        seen.append(url)
        return f"/img/{hash(url) % 1000}.jpg"

    monkeypatch.setattr(image_store, "localize", fake_localize)
    n = image_store.prewarm(
        ["http://x/1.jpg", "http://x/2.jpg", "http://x/1.jpg", "", None,
         "http://x/3.jpg"])
    assert n == 3
    assert sorted(seen) == ["http://x/1.jpg", "http://x/2.jpg", "http://x/3.jpg"]


def test_prewarm_empty():
    assert image_store.prewarm([]) == 0
    assert image_store.prewarm(None) == 0


def test_prewarm_failure_does_not_raise(monkeypatch):
    def boom(url, attempts=1):
        raise RuntimeError("nope")

    monkeypatch.setattr(image_store, "localize", boom)
    assert image_store.prewarm(["http://x/1.jpg"]) == 0


def test_prewarm_is_parallel(monkeypatch):
    def slow(url, attempts=1):
        time.sleep(0.3)
        return "/img/a.jpg"

    monkeypatch.setattr(image_store, "localize", slow)
    t0 = time.monotonic()
    n = image_store.prewarm([f"http://x/{i}.jpg" for i in range(4)])
    dt = time.monotonic() - t0
    assert n == 4
    # 串行要 1.2s；4 线程并行约 0.3s，0.9s 是宽松上限（CI 机器再慢也够）
    assert dt < 0.9, f"prewarm 疑似串行：{dt:.2f}s"
