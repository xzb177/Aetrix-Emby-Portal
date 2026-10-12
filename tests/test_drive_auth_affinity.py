"""P1（2026-10-12）：_affinity_pick 会话级 SA 亲和的回归测试（main 设计）。

main（ada90c5）实现：_affinity_pick(affinity_key) 有状态字典亲和——
- 同一 key 在 90s + 0~30s 随机抖动窗口内固定同一 SA（token 缓存必命中）
- 到期后回到抖动轮换重新选 SA（无规律可循，反风控）
- key 为空 → 与旧行为完全一致（直接 _pick_sa() 抖动轮换）
- 永不抛异常（get_drive_bearer_token 外层兜底）
- drive_auth_headers(url, affinity_key=None)：不传时默认用 url 本身
  （同一文件的 .strm 验签后 canonical URL 天然按文件稳定），调用方零改动
"""

from __future__ import annotations

import os
from unittest import mock

os.environ.setdefault("DATABASE_TYPE", "sqlite")

import pytest

try:
    from backend.emby_server import drive_auth
except ImportError:  # 兼容以 backend 为根的扁平导入路径
    from emby_server import drive_auth


SA_EMAILS = ("sa1@x", "sa2@x", "sa3@x", "sa4@x", "sa5@x")


def _make_sa(email: str) -> dict:
    return {"client_email": email, "private_key": f"private-key-for-{email}"}


def _make_fake_drive_changes(emails=SA_EMAILS):
    fake = mock.MagicMock()
    fake._discover_sa_files.return_value = [_make_sa(e) for e in emails]
    fake._sa_access_token.side_effect = (
        lambda sa, min_remaining=60: "token-for-" + sa["client_email"]
    )
    rotation = {"i": 0}

    def _deterministic_rotation_start(n):
        s = rotation["i"] % n
        rotation["i"] += 1
        return s

    fake._sa_rotation_start.side_effect = _deterministic_rotation_start
    return fake


@pytest.fixture(autouse=True)
def reset_state():
    drive_auth._reset_state()
    yield
    drive_auth._reset_state()


@pytest.fixture
def fake_drive_changes(monkeypatch):
    fake = _make_fake_drive_changes()
    monkeypatch.setattr(drive_auth, "_load_drive_changes", lambda: fake)
    return fake


def _pin_monotonic(monkeypatch, t: float):
    monkeypatch.setattr(drive_auth.time, "monotonic", lambda: t)


def _email_of_token(token):
    """从 fake token 反查是哪个 SA 的 token（token-for-<email>）。"""
    return token[len("token-for-"):]


def test_affinity_same_key_within_ttl_same_sa(fake_drive_changes, monkeypatch):
    """同一 affinity_key 在 TTL 窗口内 50 次调用 → 固定同一 SA，只换一次 token。"""
    _pin_monotonic(monkeypatch, 1000.0)
    tokens = [
        drive_auth.get_drive_bearer_token(affinity_key="fileA") for _ in range(50)
    ]
    assert len(set(tokens)) == 1
    assert fake_drive_changes._sa_access_token.call_count == 1


def test_affinity_different_keys_can_differ(fake_drive_changes, monkeypatch):
    """不同 key 允许分散到不同 SA（抖动轮换选出的 SA 不同）。"""
    _pin_monotonic(monkeypatch, 1000.0)
    tokens = {
        drive_auth.get_drive_bearer_token(affinity_key=f"file{i}") for i in range(10)
    }
    # 10 个 key 经抖动轮换选 SA：不要求占满，但必须用到 2 个以上
    assert len(tokens) >= 2


def test_affinity_expires_then_repicks(fake_drive_changes, monkeypatch):
    """TTL 窗口过期后亲和失效：回到抖动轮换，自动换到另一个 SA。"""
    _pin_monotonic(monkeypatch, 1000.0)
    t1 = drive_auth.get_drive_bearer_token(affinity_key="hotfile")
    # TTL 上限 = 90 + 30 = 120s；200s 后必过期
    _pin_monotonic(monkeypatch, 1200.0)
    t2 = drive_auth.get_drive_bearer_token(affinity_key="hotfile")
    assert _email_of_token(t1) != _email_of_token(t2)


def test_affinity_not_refreshed_on_hit(fake_drive_changes, monkeypatch):
    """命中亲和不刷新 TTL：首次写入后 90s+ 必定过期（窗口不因热点延长）。"""
    _pin_monotonic(monkeypatch, 1000.0)
    t1 = drive_auth.get_drive_bearer_token(affinity_key="fileA")
    # 窗口内反复命中
    for dt in (10.0, 30.0, 60.0, 89.0):
        _pin_monotonic(monkeypatch, 1000.0 + dt)
        assert drive_auth.get_drive_bearer_token(affinity_key="fileA") == t1
    # 超过 90s 基础窗口后亲和必然已失效（命中不续期，最长 120s）
    _pin_monotonic(monkeypatch, 1000.0 + 200.0)
    t2 = drive_auth.get_drive_bearer_token(affinity_key="fileA")
    assert _email_of_token(t2) != _email_of_token(t1)


def test_affinity_key_none_keeps_jitter_rotation(fake_drive_changes):
    """affinity_key=None 时走原有抖动轮换：连续调用轮询不同 SA。"""
    tokens = [drive_auth.get_drive_bearer_token() for _ in range(3)]
    assert tokens == ["token-for-sa1@x", "token-for-sa2@x", "token-for-sa3@x"]


def test_affinity_no_sa_falls_back_silently(monkeypatch):
    """无可用 SA 时返回 None 且不抛异常。"""
    fake = _make_fake_drive_changes(emails=())
    monkeypatch.setattr(drive_auth, "_load_drive_changes", lambda: fake)
    assert drive_auth.get_drive_bearer_token(affinity_key="fileA") is None


def test_affinity_pick_never_raises(monkeypatch, fake_drive_changes):
    """选 SA 阶段抛异常时 get_drive_bearer_token 兜底返回 None，不抛异常。"""
    monkeypatch.setattr(
        drive_auth, "_pick_sa", mock.Mock(side_effect=RuntimeError("boom"))
    )
    assert drive_auth.get_drive_bearer_token(affinity_key="fileA") is None


def test_affinity_max_entries_bounded(fake_drive_changes, monkeypatch):
    """亲和字典有上限：超限时整体清空，不无限膨胀。"""
    _pin_monotonic(monkeypatch, 1000.0)
    max_entries = drive_auth._SA_AFFINITY_MAX_ENTRIES
    for i in range(max_entries + 10):
        drive_auth.get_drive_bearer_token(affinity_key=f"key{i}")
    assert len(drive_auth._sa_affinity) <= max_entries


def test_drive_auth_headers_defaults_key_to_url(fake_drive_changes):
    """drive_auth_headers 不传 affinity_key 时默认用 url 本身：同一 URL 两次调用只换一次 token。"""
    url = "https://www.googleapis.com/drive/v3/files/fileAAA?alt=media"
    h1 = drive_auth.drive_auth_headers(url)
    h2 = drive_auth.drive_auth_headers(url)
    assert h1 == h2
    assert h1["Authorization"].startswith("Bearer token-for-")
    assert fake_drive_changes._sa_access_token.call_count == 1


def test_drive_auth_headers_explicit_key(fake_drive_changes):
    """显式传 affinity_key：不同 URL 共用同一 key 时亲和到同一 SA。"""
    h1 = drive_auth.drive_auth_headers(
        "https://www.googleapis.com/drive/v3/files/A?alt=media",
        affinity_key="shared",
    )
    h2 = drive_auth.drive_auth_headers(
        "https://www.googleapis.com/drive/v3/files/B?alt=media",
        affinity_key="shared",
    )
    assert h1 == h2
    assert fake_drive_changes._sa_access_token.call_count == 1


def test_drive_auth_headers_non_drive_returns_empty(fake_drive_changes):
    assert drive_auth.drive_auth_headers("https://example.com/x") == {}
    assert fake_drive_changes._sa_access_token.call_count == 0


def test_all_sa_dicts_cached_no_repeated_file_reads(fake_drive_changes, monkeypatch):
    """P2-1：_all_sa_dicts 结果模块级缓存——第二次调用不再读文件。

    模拟 100 个 SA 文件在磁盘上：_discover_sa_files 返回路径列表，
    _read_sa_file 被计次（代表 open+json.loads）。第一次调用读全部 100 个；
    之后再次调用必须 0 文件读取、0 重新发现。
    """
    drive_auth._reset_state()
    paths = [f"/tmp/sa{i:03d}.json" for i in range(100)]
    fake_drive_changes._discover_sa_files.return_value = paths
    read_calls = {"n": 0}

    def counting_read(path):
        read_calls["n"] += 1
        return _make_sa(f"sa-{os.path.basename(path)}@x")

    monkeypatch.setattr(drive_auth, "_read_sa_file", counting_read)

    first = drive_auth._all_sa_dicts()
    assert len(first) == 100
    assert read_calls["n"] == 100
    assert fake_drive_changes._discover_sa_files.call_count == 1

    # 第二次调用：不重复发现、不重复读文件
    second = drive_auth._all_sa_dicts()
    assert read_calls["n"] == 100
    assert fake_drive_changes._discover_sa_files.call_count == 1
    assert [s["client_email"] for s in second] == [s["client_email"] for s in first]

    # 返回的是拷贝：调用方篡改不污染快照
    second.append({"client_email": "evil@x"})
    third = drive_auth._all_sa_dicts()
    assert len(third) == 100

    # _reset_state 后缓存失效，恢复"重新发现"语义
    drive_auth._reset_state()
    fourth = drive_auth._all_sa_dicts()
    assert read_calls["n"] == 200
    assert fake_drive_changes._discover_sa_files.call_count == 2
    assert len(fourth) == 100
