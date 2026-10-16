"""P0-1（2026-10-12）：premint_tokens 必须 fail-silent，永不抛异常。"""

from __future__ import annotations

import os
from unittest import mock

os.environ.setdefault("DATABASE_TYPE", "sqlite")

try:
    from backend.emby_server import drive_auth
except ImportError:  # 兼容以 backend 为根的扁平导入路径
    from emby_server import drive_auth


def _make_sa(email: str) -> dict:
    return {"client_email": email, "private_key": f"private-key-for-{email}"}


def _fake_drive_changes(monkeypatch, emails=("sa1@x", "sa2@x"), fail=False):
    fake = mock.MagicMock()
    fake._discover_sa_files.return_value = [_make_sa(e) for e in emails]
    if fail:
        fake._sa_access_token.side_effect = RuntimeError("network down")
    else:
        fake._sa_access_token.side_effect = (
            lambda sa, min_remaining=60: "token-for-" + sa["client_email"]
        )
    monkeypatch.setattr(drive_auth, "_load_drive_changes", lambda: fake)
    # 预铸测试不需要真实 sleep：打掉 0~0.5s 抖动，加速用例
    monkeypatch.setattr(drive_auth.time, "sleep", lambda s: None)
    return fake


def test_premint_empty_sa_silent(monkeypatch):
    """无 SA 时返回空结果，不抛异常。"""
    monkeypatch.setattr(drive_auth, "_all_sa_dicts", lambda: [])
    monkeypatch.setattr(drive_auth, "_load_drive_changes", lambda: mock.MagicMock())
    drive_auth._reset_state()
    try:
        result = drive_auth.premint_tokens(max_workers=2)
    finally:
        drive_auth._reset_state()
    assert result == {"total": 0, "ok": 0, "failed": 0}


def test_premint_all_fail_silent(monkeypatch):
    """全部 SA 换 token 抛异常时返回 failed 计数，不抛异常。"""
    _fake_drive_changes(monkeypatch, fail=True)
    drive_auth._reset_state()
    try:
        result = drive_auth.premint_tokens(max_workers=2)
    finally:
        drive_auth._reset_state()
    assert result["total"] == 2
    assert result["ok"] == 0
    assert result["failed"] == 2


def test_premint_success_and_idempotent(monkeypatch):
    """成功预铸写入缓存；第二次调用幂等（直接命中缓存不再换 token）。"""
    fake = _fake_drive_changes(monkeypatch)
    drive_auth._reset_state()
    try:
        r1 = drive_auth.premint_tokens(max_workers=2)
        assert r1 == {"total": 2, "ok": 2, "failed": 0}
        calls_after_first = fake._sa_access_token.call_count
        assert calls_after_first == 2
        r2 = drive_auth.premint_tokens(max_workers=2)
        assert r2["ok"] == 2
        # 幂等：缓存命中，不再发起新的换 token 请求
        assert fake._sa_access_token.call_count == calls_after_first
    finally:
        drive_auth._reset_state()


def test_premint_discover_raises_silent(monkeypatch):
    """_all_sa_dicts 内部异常时返回空结果，不抛异常。"""
    monkeypatch.setattr(
        drive_auth, "_all_sa_dicts",
        mock.Mock(side_effect=RuntimeError("sa dir missing")),
    )
    drive_auth._reset_state()
    try:
        result = drive_auth.premint_tokens(max_workers=2)
    finally:
        drive_auth._reset_state()
    assert result == {"total": 0, "ok": 0, "failed": 0}


def test_premint_stagger_uses_window(monkeypatch):
    """stagger_seconds>0 时抖动窗口按其打散，而非固定 0~0.5s。"""
    sleeps = []
    monkeypatch.setattr(drive_auth.time, "sleep", lambda s: sleeps.append(s))
    fake = mock.MagicMock()
    fake._discover_sa_files.return_value = [_make_sa(f"sa{i}@x") for i in range(6)]
    fake._sa_access_token.side_effect = (
        lambda sa, min_remaining=60: "token-for-" + sa["client_email"]
    )
    monkeypatch.setattr(drive_auth, "_load_drive_changes", lambda: fake)
    monkeypatch.setattr(drive_auth, "_all_sa_dicts",
                        lambda: [_make_sa(f"sa{i}@x") for i in range(6)])
    drive_auth._reset_state()
    try:
        result = drive_auth.premint_tokens(max_workers=2, stagger_seconds=10)
    finally:
        drive_auth._reset_state()
    assert result["ok"] == 6
    assert len(sleeps) == 6
    assert all(0 <= s <= 10 for s in sleeps)
    assert max(sleeps) > 0.5, "stagger 窗口未生效：仍是旧的 0~0.5s 抖动"


def test_premint_default_jitter_unchanged(monkeypatch):
    """stagger_seconds 缺省时保持旧的 0~0.5s 抖动。"""
    sleeps = []
    monkeypatch.setattr(drive_auth.time, "sleep", lambda s: sleeps.append(s))
    fake = mock.MagicMock()
    fake._discover_sa_files.return_value = [_make_sa(f"sa{i}@x") for i in range(4)]
    fake._sa_access_token.side_effect = (
        lambda sa, min_remaining=60: "token-for-" + sa["client_email"]
    )
    monkeypatch.setattr(drive_auth, "_load_drive_changes", lambda: fake)
    monkeypatch.setattr(drive_auth, "_all_sa_dicts",
                        lambda: [_make_sa(f"sa{i}@x") for i in range(4)])
    drive_auth._reset_state()
    try:
        result = drive_auth.premint_tokens(max_workers=2)
    finally:
        drive_auth._reset_state()
    assert result["ok"] == 4
    assert all(0 <= s <= 0.5 for s in sleeps)
