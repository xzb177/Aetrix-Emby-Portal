# -*- coding: utf-8 -*-
"""v2.57.0 提速包 item3：TMDB 限流按 key 独立成桶。

- 每把 key 一个独立 _RequestLimiter，有效上限 = key 数 × TMDB_PER_SEC
- 429 只压低肇事 key 的桶，不连累其他 key
- 测试兼容：整体替换 client._limiter 为普通 _RequestLimiter 时，
  _keyed_limiter() 直接返回它（无 _per_key 属性），旧测试语义不变
"""
import os

os.environ.setdefault("DATABASE_TYPE", "sqlite")
os.environ.setdefault("REDIS_ENABLED", "false")

import pytest
from types import SimpleNamespace

from backend.emby_server import tmdb as tmdb_mod
from backend.emby_server.tmdb import TmdbClient, _KeyedLimiter, _RequestLimiter


class _Session:
    def __init__(self, answer):
        self._answer = answer
        self.calls = []

    def get(self, url, params):
        self.calls.append((url, dict(params)))
        return self._answer(url, params)


def _resp(status=200, payload=None, headers=None):
    return SimpleNamespace(status_code=status, headers=headers or {},
                           json=lambda: ({} if payload is None else payload))


def _client(monkeypatch, keys=("k1", "k2"), answer=None) -> TmdbClient:
    monkeypatch.delenv("TMDB_API_KEYS", raising=False)
    monkeypatch.delenv("TMDB_API_KEY", raising=False)
    monkeypatch.setattr(tmdb_mod, "_read_config_value", lambda db: "")
    client = TmdbClient()
    client._set_keys(list(keys), "env")
    if answer is not None:
        client.session = _Session(answer)
    monkeypatch.setattr(client, "_ensure_session", lambda: None)
    return client


def test_buckets_are_independent_per_key():
    lim = _KeyedLimiter(rate=2.0, min_rate=1.0)
    lim.sync_keys(["k1", "k2"])
    b1 = lim.limiter_for("k1")
    b2 = lim.limiter_for("k2")
    assert b1 is not b2
    # 把 k1 的桶打空（2 token），k2 不受影响
    b1.acquire()
    b1.acquire()
    # k1 桶已空、k2 桶仍满：直接看内部 token（同进程单线程测试）
    assert b1._tokens < 1.0
    assert b2._tokens >= 2.0


def test_429_only_throttles_the_offending_key(monkeypatch):
    client = _client(monkeypatch, keys=("k1", "k2"))
    before_k2 = client._keyed_limiter("k2").rate
    client._throttle("k1", None)  # k1 被 429
    assert client._keyed_limiter("k1").rate < before_k2
    assert client._keyed_limiter("k2").rate == before_k2  # k2 纹丝不动
    # 顶层聚合口径：throttled 计数 +1
    assert client._limiter.throttled_count == 1


def test_aggregated_stats_sum_across_keys(monkeypatch):
    client = _client(monkeypatch, keys=("k1", "k2"))
    st = client.stats()
    # 每把 key ceiling = TMDB_PER_SEC，聚合 = 2 倍
    assert st["ceiling"] == round(2 * tmdb_mod.TMDB_PER_SEC, 3)
    assert st["rate"] == st["ceiling"]  # 都没被 429 过


def test_plain_limiter_replacement_keeps_old_semantics(monkeypatch):
    """测试整体替换 client._limiter 为普通桶时，按旧语义走（无 per-key 路由）。"""
    client = _client(monkeypatch, keys=("k1", "k2"))
    plain = _RequestLimiter(rate=1_000_000.0, min_rate=1.0)
    client._limiter = plain
    assert client._keyed_limiter("k1") is plain
    assert client._keyed_limiter("k2") is plain
    # _throttle 走被替换的桶
    client._throttle("k1", None)
    assert plain.throttled_count == 1


def test_sync_keys_drops_departed_keeps_survivors(monkeypatch):
    client = _client(monkeypatch, keys=("k1", "k2"))
    b1 = client._keyed_limiter("k1")
    b1.note_throttled(None)  # k1 被压低
    lowered = b1.rate
    client._set_keys(["k1", "k3"], "env")  # k2 离场，k3 入场
    assert client._keyed_limiter("k1").rate == lowered  # 存活 key 状态保留
    assert client._keyed_limiter("k3").rate == tmdb_mod.TMDB_PER_SEC  # 新 key 满速
    assert "k2" not in client._limiter._per_key  # 离场 key 的桶被清理


def test_request_routing_uses_per_key_bucket(monkeypatch):
    """端到端：_request 按 key 走独立桶（fake session，不碰网络）。"""
    client = _client(
        monkeypatch, keys=("k1", "k2"),
        answer=lambda url, params: _resp(payload={"ok": True}))
    b1 = client._keyed_limiter("k1")
    tokens_before = b1._tokens
    client._request("/3/movie/1", {}, key="k1")
    assert b1._tokens < tokens_before  # k1 的桶被扣了 token
    assert len(client.session.calls) == 1
