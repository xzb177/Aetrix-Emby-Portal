"""P1（2026-10-12）：get_drive_bearer_token(key) 时间分桶 SA 亲和的回归测试。

- 同一 key 在同一 bucket 内 → 固定同一 SA（token 缓存必命中）
- bucket 递增后 → 自动落到另一个 SA（无需状态机），分布均匀
- key=None 时保持原有抖动轮换行为
- 无 SA / 异常时 fail-silent 回退，不抛异常
- drive_auth_headers 内部从 url 提取 file_id（uc?id= / drive/v3/files/）
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


def _pin_bucket(monkeypatch, bucket: int):
    """把时间固定在给定 bucket（monotonic 秒数 = bucket * 窗口）。"""
    monkeypatch.setattr(
        drive_auth.time, "monotonic",
        lambda: bucket * drive_auth._SA_AFFINITY_WINDOW_SECONDS + 1.0,
    )


def _email_of_token(fake_drive_changes, token):
    """从 fake token 反查是哪个 SA 的 token（token-for-<email>）。"""
    return token[len("token-for-"):]


def test_affine_same_key_same_bucket_same_sa(fake_drive_changes, monkeypatch):
    """同一 file_id 在同一 bucket 内 50 次调用 → 同一 SA。"""
    _pin_bucket(monkeypatch, 7)
    tokens = [drive_auth.get_drive_bearer_token(key="fileA") for _ in range(50)]
    assert len(set(tokens)) == 1
    # 第一次换取后全部命中缓存：只换了一次 token
    assert fake_drive_changes._sa_access_token.call_count == 1


def test_affine_different_keys_can_differ(fake_drive_changes, monkeypatch):
    """同一 bucket 内不同 file_id 允许分散到不同 SA（hash 分布）。"""
    _pin_bucket(monkeypatch, 7)
    tokens = {drive_auth.get_drive_bearer_token(key=f"file{i}") for i in range(20)}
    # 20 个文件散到 5 个 SA：不要求恰好占满，但必须用到 2 个以上
    assert len(tokens) >= 2


def test_affine_bucket_increment_moves_sa(fake_drive_changes, monkeypatch):
    """bucket 递增后亲和自动切换 SA（无需状态机），多 bucket 下分布均匀。"""
    emails = set()
    for bucket in range(30):
        _pin_bucket(monkeypatch, bucket)
        token = drive_auth.get_drive_bearer_token(key="hotfile")
        emails.add(_email_of_token(fake_drive_changes, token))
    # 30 个 bucket 应当自然分散到多个 SA（sha256 伪随机）
    assert len(emails) >= 2


def test_affine_continuity_within_bucket(fake_drive_changes, monkeypatch):
    """同一 bucket 内亲和 SA 在 bucket 边界附近依然稳定（取窗口首尾）。"""
    monkeypatch.setattr(
        drive_auth.time, "monotonic",
        lambda: 7 * drive_auth._SA_AFFINITY_WINDOW_SECONDS + 0.001,
    )
    t1 = drive_auth.get_drive_bearer_token(key="fileX")
    monkeypatch.setattr(
        drive_auth.time, "monotonic",
        lambda: 8 * drive_auth._SA_AFFINITY_WINDOW_SECONDS - 0.001,
    )
    t2 = drive_auth.get_drive_bearer_token(key="fileX")
    assert t1 == t2


def test_key_none_keeps_jitter_rotation(fake_drive_changes):
    """key=None 时走原有抖动轮换：连续调用轮询不同 SA。"""
    tokens = [drive_auth.get_drive_bearer_token() for _ in range(3)]
    assert tokens == ["token-for-sa1@x", "token-for-sa2@x", "token-for-sa3@x"]


def test_affine_no_sa_falls_back_silently(monkeypatch):
    """无可用 SA 时 key 亲和回退 _pick_sa，返回 None 且不抛异常。"""
    fake = _make_fake_drive_changes(emails=())
    monkeypatch.setattr(drive_auth, "_load_drive_changes", lambda: fake)
    assert drive_auth.get_drive_bearer_token(key="fileA") is None


def test_pick_sa_affine_never_raises(monkeypatch):
    """_all_sa_dicts 异常时 _pick_sa_affine 回退，不抛异常。"""
    fake = _make_fake_drive_changes()
    monkeypatch.setattr(drive_auth, "_load_drive_changes", lambda: fake)
    monkeypatch.setattr(
        drive_auth, "_all_sa_dicts",
        mock.Mock(side_effect=RuntimeError("boom")),
    )
    # 回退到 _pick_sa（抖动轮换），仍然能拿到 token
    token = drive_auth.get_drive_bearer_token(key="fileA")
    assert token == "token-for-sa1@x"


@pytest.mark.parametrize(
    "url, expected",
    [
        # uc?id= 形式
        ("https://drive.google.com/uc?export=download&id=fileABC123", "fileABC123"),
        ("https://drive.google.com/uc?id=fileXYZ", "fileXYZ"),
        # drive/v3/files/<id> 形式
        (
            "https://www.googleapis.com/drive/v3/files/fileDEF456?alt=media",
            "fileDEF456",
        ),
        ("https://www.googleapis.com/drive/v3/files/fileGHI", "fileGHI"),
        # 非 Drive URL / 无 id → None
        ("https://example.com/foo", None),
        ("https://drive.google.com/uc?export=download", None),
        ("", None),
    ],
)
def test_drive_file_id_from_url(url, expected):
    assert drive_auth._drive_file_id_from_url(url) == expected


def test_drive_auth_headers_passes_file_id_key(fake_drive_changes, monkeypatch):
    """drive_auth_headers 内部提取 file_id 并作为亲和 key：两次调用同一文件只换一次 token。"""
    url = "https://www.googleapis.com/drive/v3/files/fileAAA?alt=media"
    h1 = drive_auth.drive_auth_headers(url)
    h2 = drive_auth.drive_auth_headers(url)
    assert h1 == h2
    assert h1["Authorization"].startswith("Bearer token-for-")
    assert fake_drive_changes._sa_access_token.call_count == 1


def test_drive_auth_headers_non_drive_returns_empty(fake_drive_changes):
    assert drive_auth.drive_auth_headers("https://example.com/x") == {}
    assert fake_drive_changes._sa_access_token.call_count == 0
