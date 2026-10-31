"""A-121：内封字幕抽取并发共享固定 tmp 名致败者误报 404。

旧码：tmp = cached + ".part" 固定名；同轨并发首抽时先完成者 os.replace
后 tmp 路径消失，后完成者走 not isfile(tmp) 分支抛 404（轨道本身健康，
重试即因缓存命中成功，属间歇性误报）。另有混合缓存机制风险：败者持有
已发布 inode 的写 fd，输出有差异时可续写已发布缓存。

修法：tmp 名加 pid+uuid 唯一后缀，各路写各的 tmp、发布仍原子；
失败分支改为“缓存已存在即回缓存、否则 404”，败者不再误报。
"""
from __future__ import annotations

import os
import threading
import time
import types

import pytest
from fastapi import HTTPException

from backend.emby_server import subtitles

VTT = b"WEBVTT\n\n00:00:01.000 --> 00:00:03.000\nHello\n"


def _fake_run_factory(barrier, state):
    """模拟慢速分块写 tmp 的 ffmpeg，确定性复现败者路径。

    所有路先写首块并在屏障处会合；首个到达者（leader）直接返回，
    其 extract 随即 replace 发布缓存；其余路（follower）在持有 tmp
    写 fd 的状态下等到缓存出现后再写完返回——此时旧码下 tmp 路径已
    消失，follower 必走 404 分支；新码下各路 tmp 独立，全部成功。
    """

    def _fake_run(cmd, capture_output=False, timeout=None):
        tmp = cmd[-1]
        with open(tmp, "wb") as f:
            f.write(VTT[:10])
            f.flush()
            barrier.wait(timeout=30)
            with state["lock"]:
                state["arrivals"] += 1
                leader = state["arrivals"] == 1
            if not leader:
                deadline = time.time() + 30
                while not os.path.exists(state["cached"]) and time.time() < deadline:
                    time.sleep(0.01)
            f.write(VTT[10:])
        return types.SimpleNamespace(returncode=0, stdout=b"", stderr=b"")

    return _fake_run


@pytest.fixture
def _cold_cache(tmp_path, monkeypatch):
    monkeypatch.setattr(subtitles, "SUB_CACHE_DIR", str(tmp_path / "subs"))
    monkeypatch.setattr(subtitles.shutil, "which", lambda *_a, **_k: "/bin/ffmpeg")
    media = tmp_path / "media.mkv"
    media.write_bytes(b"\x00" * 64)
    return str(media)


def test_concurrent_first_extraction_all_succeed_same_bytes(_cold_cache, monkeypatch):
    n = 10
    barrier = threading.Barrier(n)
    state = {"lock": threading.Lock(), "arrivals": 0,
             "cached": subtitles._cache_path("guid-a121", 3)}
    monkeypatch.setattr(subtitles.subprocess, "run", _fake_run_factory(barrier, state))
    results, errors = [], []

    def _worker():
        try:
            results.append(subtitles.extract_embedded(_cold_cache, "guid-a121", 3, 0))
        except Exception as exc:  # noqa: BLE001 — 并发失败形态本身就是断言对象
            errors.append(exc)

    threads = [threading.Thread(target=_worker) for _ in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=90)
    assert not errors, f"并发首抽出现失败: {errors!r}"
    assert len(results) == n
    cached = results[0]
    assert all(r == cached for r in results)
    with open(cached, "rb") as f:
        assert f.read() == VTT


def test_failed_extraction_falls_back_to_peer_published_cache(_cold_cache, monkeypatch):
    # 本路 ffmpeg 失败、但同伴已在此期间发布缓存：应回缓存而非误报 404。
    cached = subtitles._cache_path("guid-a121", 3)

    def _fail_run(cmd, capture_output=False, timeout=None):
        os.makedirs(os.path.dirname(cached), exist_ok=True)
        with open(cached, "wb") as f:
            f.write(VTT)
        return types.SimpleNamespace(returncode=1, stdout=b"", stderr=b"")

    monkeypatch.setattr(subtitles.subprocess, "run", _fail_run)
    assert subtitles.extract_embedded(_cold_cache, "guid-a121", 3, 0) == cached
    with open(cached, "rb") as f:
        assert f.read() == VTT


def test_failed_extraction_without_cache_still_404(_cold_cache, monkeypatch):
    monkeypatch.setattr(
        subtitles.subprocess, "run",
        lambda *a, **k: types.SimpleNamespace(returncode=1, stdout=b"", stderr=b""))
    with pytest.raises(HTTPException) as ei:
        subtitles.extract_embedded(_cold_cache, "guid-a121", 3, 0)
    assert ei.value.status_code == 404


def test_single_extraction_unchanged(_cold_cache, monkeypatch):
    calls = []

    def _fake_run(cmd, capture_output=False, timeout=None):
        calls.append(cmd[-1])
        with open(cmd[-1], "wb") as f:
            f.write(VTT)
        return types.SimpleNamespace(returncode=0, stdout=b"", stderr=b"")

    monkeypatch.setattr(subtitles.subprocess, "run", _fake_run)
    first = subtitles.extract_embedded(_cold_cache, "guid-a121", 3, 0)
    second = subtitles.extract_embedded(_cold_cache, "guid-a121", 3, 0)
    assert first == second
    assert len(calls) == 1  # 第二次命中缓存，不再抽取
    with open(first, "rb") as f:
        assert f.read() == VTT
