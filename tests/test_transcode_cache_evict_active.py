# -*- coding: utf-8 -*-
"""回归测试：转码缓存 LRU 淘汰不得删除正被活跃会话服务中的目录。

针对已确认 bug：``transcode._enforce_cache_capacity`` 只跳过刚落盘的
``new_dir``；最旧的缓存即使正被 ``register_cache_session`` 登记的会话服务
（``cached=True``，客户端后续切片全从 ``info["dir"]`` 直读），也会被删掉 →
该用户的播放中途 404/503 中断。

用例 1、3 在未修复代码上失败（目录被删），修复后通过；
用例 2 证明淘汰功能本身没被关掉（修复前后都通过）。
"""
import json
import os

os.environ.setdefault("DATABASE_TYPE", "sqlite")
os.environ.setdefault("REDIS_ENABLED", "false")

from datetime import datetime, timedelta

import pytest

from backend.emby_server import transcode as tc


def _make_cache_entry(root, name, days_ago, payload_kb=60):
    d = os.path.join(root, name)
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, "master.m3u8"), "w") as fh:
        fh.write("#EXTM3U\n")
    with open(os.path.join(d, "seg0.ts"), "wb") as fh:
        fh.write(b"\x00" * 1024 * payload_kb)
    with open(os.path.join(d, "cache.json"), "w") as fh:
        json.dump(
            {"completed_at": (datetime.now() - timedelta(days=days_ago)).isoformat()},
            fh,
        )
    return d


class _StubStreaming:
    """模拟 streaming 模块：_TRANSCODE_PROCS 里登记着正在服务某目录的会话。"""

    def __init__(self, procs):
        self._TRANSCODE_PROCS = procs


@pytest.fixture()
def cache_env(tmp_path, monkeypatch):
    base = str(tmp_path / "tcache")
    os.makedirs(base, exist_ok=True)
    monkeypatch.setenv("EMBY_TRANSCODE_CACHE_DIR", base)
    # ~107KB 上限：两个 60KB 缓存目录必超限，触发淘汰
    monkeypatch.setenv("EMBY_TRANSCODE_CACHE_MAX_GB", "0.0001")
    return base


def _with_active_session(monkeypatch, dir_path):
    stub = _StubStreaming(
        {"sess1": {"proc": None, "dir": dir_path, "cached": True, "user_id": 7}}
    )
    monkeypatch.setattr(tc, "_streaming", lambda: stub)


def test_evict_skips_active_session_dir(cache_env, monkeypatch):
    """活跃会话正在服务的最旧缓存，淘汰必须跳过。"""
    old = _make_cache_entry(cache_env, "aaa_old", days_ago=3)
    new = _make_cache_entry(cache_env, "zzz_new", days_ago=0)
    _with_active_session(monkeypatch, old)

    tc._enforce_cache_capacity(new)

    assert os.path.isfile(os.path.join(old, "master.m3u8")), \
        "正被活跃会话服务的缓存目录不应被 LRU 淘汰删除"
    assert os.path.isdir(new)


def test_evict_still_removes_idle_old_dir(cache_env, monkeypatch):
    """无活跃会话时，最旧的缓存照常被淘汰（淘汰功能本身没被关掉）。"""
    old = _make_cache_entry(cache_env, "aaa_old", days_ago=3)
    new = _make_cache_entry(cache_env, "zzz_new", days_ago=0)
    monkeypatch.setattr(tc, "_streaming", lambda: _StubStreaming({}))

    tc._enforce_cache_capacity(new)

    assert not os.path.exists(old), "无活跃会话时，最旧的缓存应被正常淘汰"
    assert os.path.isdir(new)


def test_evict_prefers_idle_victim(cache_env, monkeypatch):
    """旧目录活跃 + 中间目录闲置：删闲置的，保留活跃的。"""
    old = _make_cache_entry(cache_env, "aaa_old", days_ago=3)
    mid = _make_cache_entry(cache_env, "mmm_mid", days_ago=1)
    new = _make_cache_entry(cache_env, "zzz_new", days_ago=0)
    _with_active_session(monkeypatch, old)

    tc._enforce_cache_capacity(new)

    assert os.path.isfile(os.path.join(old, "master.m3u8")), "活跃会话的目录必须保留"
    assert not os.path.exists(mid), "闲置的中间目录应被淘汰"
    assert os.path.isdir(new)
