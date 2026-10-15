"""回归测试：get_drive_bearer_token 换取某 SA 的 token 时，不得阻塞其他 SA 的取 token。

背景（r12）：旧实现"查缓存→换 token→写缓存"全程持模块全局 _cache_lock，
换 token 是 RSA 签名 + HTTPS POST（最长 30s），期间所有其他 SA 的取 token
（含缓存命中）都被串行阻塞。修复后换取走 per-SA 锁 + 短临界区查/写缓存。
"""
from __future__ import annotations

import os
import threading
import time
from unittest import mock

os.environ.setdefault("DATABASE_TYPE", "sqlite")

import pytest

try:
    from backend.emby_server import drive_auth
except ImportError:
    from emby_server import drive_auth


def _make_sa(email: str) -> dict:
    return {"client_email": email, "private_key": f"private-key-for-{email}"}


@pytest.fixture(autouse=True)
def reset_drive_auth_state():
    drive_auth._reset_state()
    yield
    drive_auth._reset_state()


@pytest.fixture
def fake_drive_changes(monkeypatch):
    fake = mock.MagicMock()
    fake._token_cache = {}
    fake._discover_sa_files.return_value = [_make_sa("sa1@x"), _make_sa("sa2@x")]
    fake._sa_rotation_start.side_effect = lambda n: 0
    monkeypatch.setattr(drive_auth, "_load_drive_changes", lambda: fake)
    return fake


def _pick_sa_by_thread(picks):
    def _pick():
        return picks.get(threading.current_thread().name)

    return _pick


def test_other_sa_not_blocked_while_one_minting(fake_drive_changes, monkeypatch):
    """sa1 正在换取（sleep 2s）时，sa2 的缓存命中必须毫秒级返回，不被阻塞。"""
    entered_mint = threading.Event()
    release_mint = threading.Event()

    def slow_mint(sa, min_remaining=60):
        entered_mint.set()
        assert release_mint.wait(timeout=10), "mint 等待释放超时"
        return "token-for-" + sa["client_email"]

    fake_drive_changes._sa_access_token.side_effect = slow_mint

    picks = {}

    def fake_pick_sa():
        return picks.get(threading.current_thread().name)

    monkeypatch.setattr(drive_auth, "_pick_sa", fake_pick_sa)
    monkeypatch.setattr(drive_auth, "_affinity_pick", lambda key: fake_pick_sa())

    # 预热 sa2 缓存：走一次正常换取
    picks[threading.current_thread().name] = _make_sa("sa2@x")
    release_mint.set()
    assert drive_auth.get_drive_bearer_token() == "token-for-sa2@x"
    release_mint.clear()

    results = {}

    def worker(name, sa):
        picks[name] = sa
        t0 = time.monotonic()
        results[name] = (drive_auth.get_drive_bearer_token(), time.monotonic() - t0)

    t1 = threading.Thread(target=worker, args=("T1", _make_sa("sa1@x")), name="T1")
    t2 = threading.Thread(target=worker, args=("T2", _make_sa("sa2@x")), name="T2")
    t1.start()
    assert entered_mint.wait(timeout=10), "T1 未进入换取"
    time.sleep(0.2)
    t2.start()
    t2.join(timeout=10)
    assert "T2" in results, "T2 被阻塞超过 10 秒（旧实现会卡在全局锁上）"
    token2, dur2 = results["T2"]
    assert token2 == "token-for-sa2@x"
    assert dur2 < 1.5, f"T2 缓存命中却被阻塞了 {dur2:.2f}s"
    release_mint.set()
    t1.join(timeout=10)
    token1, _ = results["T1"]
    assert token1 == "token-for-sa1@x"


def test_same_sa_not_minted_twice_concurrently(fake_drive_changes, monkeypatch):
    """同一 SA 并发取 token 时，换取只发生一次（per-SA 串行 + 写回复查）。"""
    mint_calls = []
    gate = threading.Event()

    def counting_mint(sa, min_remaining=60):
        mint_calls.append(sa["client_email"])
        assert gate.wait(timeout=10)
        return "token-for-" + sa["client_email"]

    fake_drive_changes._sa_access_token.side_effect = counting_mint

    sa = _make_sa("sa1@x")
    monkeypatch.setattr(drive_auth, "_pick_sa", lambda: sa)
    monkeypatch.setattr(drive_auth, "_affinity_pick", lambda key: sa)

    results = []
    barrier = threading.Barrier(5)

    def worker():
        barrier.wait()
        results.append(drive_auth.get_drive_bearer_token())

    threads = [threading.Thread(target=worker) for _ in range(5)]
    for t in threads:
        t.start()
    time.sleep(0.5)
    gate.set()
    for t in threads:
        t.join(timeout=10)
    assert len(mint_calls) == 1, f"同一 SA 并发换取了 {len(mint_calls)} 次"
    assert all(r == "token-for-sa1@x" for r in results)


def test_mint_failure_returns_none_and_not_cached(fake_drive_changes, monkeypatch):
    """换取抛异常/返回空时返回 None 且不污染缓存（永不抛异常契约）。"""
    fake_drive_changes._sa_access_token.side_effect = RuntimeError("boom")
    sa = _make_sa("sa1@x")
    monkeypatch.setattr(drive_auth, "_pick_sa", lambda: sa)
    monkeypatch.setattr(drive_auth, "_affinity_pick", lambda key: sa)
    assert drive_auth.get_drive_bearer_token() is None
    assert "sa1@x" not in drive_auth._token_cache

    fake_drive_changes._sa_access_token.side_effect = lambda sa, min_remaining=60: ""
    assert drive_auth.get_drive_bearer_token() is None
    assert "sa1@x" not in drive_auth._token_cache
