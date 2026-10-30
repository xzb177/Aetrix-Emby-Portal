"""Drive 中继 SA failover（2026-10-29 用户亲令「中转加上换号能力」）。

锁四件事：
- 上游取流 401/403/429 → 按值失效该 SA 凭据（drive_auth 既有机制）
  并轮换下一个 SA 重试，总尝试次数由 max_attempts 界定（缺省 3）；
- 次数耗尽回最后一次的原始错误；404 不换号、原样透传（#1192）；
- BYOS 显式 access_token 腿只试一次、绝不取 SA 凭据、绝不记冷却
  （#1296 回归）；
- playback_strategy.drive_relay_sa_failover_max_attempts：默认 3、
  钳制 [1, 10]、读失败/非法值回默认 3。

monkeypatch 只打模块属性（与 test_drive_relay.py 同款）。
"""
import asyncio
import time
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from backend.emby_server import drive_auth
from backend.emby_server import drive_changes as _dc
from backend.emby_server import playback_strategy as _strategy
from backend.emby_server import streaming
from backend.emby_server import transfer_variant as _tv


def _run(coro):
    return asyncio.run(coro)


class _RelayRig:
    """mount/token/冷却三处替身，记录每次调用。"""

    def __init__(self, monkeypatch, outcomes, tokens=None,
                 email_map=None):
        # outcomes: 每次 mount 调用的结果序列，"RESP" 或 HTTP 状态码
        self.outcomes = list(outcomes)
        self.mount_auth = []
        self.token_forces = []
        self.invalidated = []
        self.noted = []
        self._tokens = list(tokens) if tokens else [
            "TOK1", "TOK2", "TOK3", "TOK4", "TOK5"]

        async def fake_mount(url, request, headers=None, media_type=None,
                             pt=None, sniff_quota_page=False):
            self.mount_auth.append((headers or {}).get("Authorization"))
            outcome = self.outcomes.pop(0)
            if outcome == "RESP":
                return "RESP"
            if outcome == "QUOTA":
                raise _tv.QuotaPageError("quota page")
            raise HTTPException(status_code=outcome, detail="up")

        def fake_token(drive_id, force_refresh=False):
            self.token_forces.append(force_refresh)
            idx = min(len(self.token_forces) - 1, len(self._tokens) - 1)
            return self._tokens[idx]

        def fake_note(url, header):
            self.noted.append((url, header))
            return True

        def fake_invalidate(token):
            self.invalidated.append(token)
            return True

        monkeypatch.setattr(streaming, "serve_mount_relay_async", fake_mount)
        monkeypatch.setattr(_dc, "get_token_for_drive", fake_token)
        monkeypatch.setattr(
            drive_auth, "invalidate_drive_bearer_token", fake_invalidate)
        monkeypatch.setattr(
            drive_auth, "note_drive_unauthorized", fake_note)
        monkeypatch.setattr(
            drive_auth, "sa_email_for_token",
            lambda token: f"sa-for-{token}@example.test")
        email_map = email_map or {}
        monkeypatch.setattr(
            _dc, "sa_email_for_token",
            lambda token: email_map.get(token), raising=False)

    def relay(self, **kwargs):
        return _run(streaming.serve_drive_relay_async(
            SimpleNamespace(headers={}), "shared1", "FID123", **kwargs))


# ---------- 触发换号 ----------

def test_first_sa_403_fails_over_to_second_sa(monkeypatch):
    rig = _RelayRig(monkeypatch, [403, "RESP"])
    assert rig.relay() == "RESP"
    assert rig.mount_auth == ["Bearer TOK1", "Bearer TOK2"]
    assert rig.token_forces == [False, True]
    assert rig.invalidated == ["TOK1"]
    assert rig.noted == []


def test_401_fails_over_and_notes_unauthorized(monkeypatch):
    rig = _RelayRig(monkeypatch, [401, "RESP"])
    assert rig.relay() == "RESP"
    assert len(rig.noted) == 1
    url, header = rig.noted[0]
    assert "FID123" in url
    assert header == "Bearer TOK1"
    assert rig.invalidated == []


def test_429_fails_over(monkeypatch):
    rig = _RelayRig(monkeypatch, [429, "RESP"])
    assert rig.relay() == "RESP"
    assert rig.mount_auth == ["Bearer TOK1", "Bearer TOK2"]
    assert rig.invalidated == ["TOK1"]


# ---------- 次数耗尽 / 边界 ----------

def test_attempts_exhausted_returns_last_error(monkeypatch):
    rig = _RelayRig(monkeypatch, [403, 403, 429])
    with pytest.raises(HTTPException) as ei:
        rig.relay(max_attempts=3)
    assert ei.value.status_code == 429  # 最后一次的原始错误
    assert len(rig.mount_auth) == 3
    assert rig.token_forces == [False, True, True]
    # 末次失败直接抛、不再记账
    assert rig.invalidated == ["TOK1", "TOK2"]


def test_default_attempts_is_three(monkeypatch):
    rig = _RelayRig(monkeypatch, [429, 429, 429])
    with pytest.raises(HTTPException) as ei:
        rig.relay()
    assert ei.value.status_code == 429
    assert len(rig.mount_auth) == 3


def test_max_attempts_one_disables_failover(monkeypatch):
    rig = _RelayRig(monkeypatch, [403])
    with pytest.raises(HTTPException) as ei:
        rig.relay(max_attempts=1)
    assert ei.value.status_code == 403
    assert len(rig.mount_auth) == 1
    assert rig.invalidated == []


def test_cooldown_bookkeeping_failure_does_not_break_failover(monkeypatch):
    rig = _RelayRig(monkeypatch, [403, "RESP"])

    def boom_invalidate(token):
        raise RuntimeError("cache down")

    monkeypatch.setattr(
        drive_auth, "invalidate_drive_bearer_token", boom_invalidate)
    assert rig.relay() == "RESP"
    assert rig.mount_auth == ["Bearer TOK1", "Bearer TOK2"]


# ---------- 不触发换号 ----------

def test_404_does_not_fail_over(monkeypatch):
    rig = _RelayRig(monkeypatch, [404])
    with pytest.raises(HTTPException) as ei:
        rig.relay()
    assert ei.value.status_code == 404
    assert len(rig.mount_auth) == 1
    assert rig.token_forces == [False]
    assert rig.invalidated == [] and rig.noted == []


def test_byos_access_token_never_fails_over(monkeypatch):
    rig = _RelayRig(monkeypatch, [403])

    def boom_token(drive_id, force_refresh=False):
        raise AssertionError("BYOS 腿不许取 SA 凭据")

    monkeypatch.setattr(_dc, "get_token_for_drive", boom_token)
    with pytest.raises(HTTPException) as ei:
        rig.relay(access_token="USER-TOKEN")
    assert ei.value.status_code == 403
    assert rig.mount_auth == ["Bearer USER-TOKEN"]
    assert rig.invalidated == [] and rig.noted == []


# ---------- 配置键 reader ----------

def _stub_store(monkeypatch, values):
    def fake_get(db, key, default=None):
        return values.get(key, default)

    monkeypatch.setattr(_strategy.store, "get_value", fake_get)


def test_failover_attempts_default_three(monkeypatch):
    _stub_store(monkeypatch, {})
    assert _strategy.drive_relay_sa_failover_max_attempts(object()) == 3


def test_failover_attempts_explicit_and_clamped(monkeypatch):
    key = _strategy.KEY_DRIVE_RELAY_SA_FAILOVER_MAX_ATTEMPTS
    _stub_store(monkeypatch, {key: "5"})
    assert _strategy.drive_relay_sa_failover_max_attempts(object()) == 5
    _stub_store(monkeypatch, {key: "0"})
    assert _strategy.drive_relay_sa_failover_max_attempts(object()) == 1
    _stub_store(monkeypatch, {key: "99"})
    assert _strategy.drive_relay_sa_failover_max_attempts(object()) == 10


def test_failover_attempts_bad_value_defaults_three(monkeypatch):
    key = _strategy.KEY_DRIVE_RELAY_SA_FAILOVER_MAX_ATTEMPTS
    _stub_store(monkeypatch, {key: "abc"})
    assert _strategy.drive_relay_sa_failover_max_attempts(object()) == 3


def test_failover_attempts_read_failure_defaults_three(monkeypatch):
    def boom(db, key, default=None):
        raise RuntimeError("db down")

    monkeypatch.setattr(_strategy.store, "get_value", boom)
    assert _strategy.drive_relay_sa_failover_max_attempts(object()) == 3


# ---------- A-009：额度页冷却＋排除已试 SA ----------

def test_quota_page_cools_down_and_retries(monkeypatch):
    rig = _RelayRig(monkeypatch, ["QUOTA", "RESP"])
    assert rig.relay() == "RESP"
    assert rig.mount_auth == ["Bearer TOK1", "Bearer TOK2"]
    assert rig.invalidated == ["TOK1"]
    assert rig.noted == []


def test_quota_page_last_attempt_does_not_cool_down(monkeypatch):
    rig = _RelayRig(monkeypatch, ["QUOTA"])
    with pytest.raises(_tv.QuotaPageError):
        rig.relay(max_attempts=1)
    assert rig.mount_auth == ["Bearer TOK1"]
    assert rig.invalidated == []


def test_failover_redraws_when_rotation_repeats_same_sa(monkeypatch):
    rig = _RelayRig(
        monkeypatch, [403, "RESP"],
        tokens=["TOK1", "TOK1", "TOK2"],
        email_map={"TOK1": "sa1@example.test",
                   "TOK2": "sa2@example.test"})
    assert rig.relay() == "RESP"
    assert rig.mount_auth == ["Bearer TOK1", "Bearer TOK2"]
    assert rig.invalidated == ["TOK1"]
    assert len(rig.token_forces) == 3  # 初取＋重抽各一次


def test_failover_redraw_bound_falls_back(monkeypatch):
    rig = _RelayRig(
        monkeypatch, [403, 403, 403],
        tokens=["TOK1"] * 10,
        email_map={"TOK1": "sa1@example.test"})
    with pytest.raises(HTTPException) as ei:
        rig.relay(max_attempts=3)
    assert ei.value.status_code == 403
    assert rig.mount_auth == ["Bearer TOK1"] * 3
    assert rig.invalidated == ["TOK1", "TOK1"]
    # 初取 1＋第 2/3 次各初取 1＋重抽 3：耗尽后接受旧令牌，不直接 503
    assert len(rig.token_forces) == 9


def test_dc_sa_email_for_token_reverse_lookup(monkeypatch):
    monkeypatch.setitem(
        _dc._token_cache, "sa1@example.test",
        ("TOK1", time.time() + 3600))
    assert _dc.sa_email_for_token("TOK1") == "sa1@example.test"
    assert _dc.sa_email_for_token("NOPE") is None


def test_invalidate_clears_both_token_caches(monkeypatch):
    monkeypatch.setitem(
        drive_auth._token_cache, "sa1@example.test",
        ("OLD", time.time() + 3600))
    monkeypatch.setitem(
        _dc._token_cache, "sa1@example.test",
        ("OLD", time.time() + 3600))
    assert drive_auth.invalidate_drive_bearer_token("OLD") is True
    assert "sa1@example.test" not in drive_auth._token_cache
    assert "sa1@example.test" not in _dc._token_cache
