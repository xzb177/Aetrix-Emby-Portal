"""回归测试：drive_auth 的 Bearer token 必须按 SA 分账号缓存并轮询。

覆盖 P1 修复：此前 token 全局缓存 55 分钟，导致 round-robin 形同虚设，
所有下载流量压在单个 SA 上把配额干爆（probe 403）。
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


SA_EMAILS = ("sa1@x", "sa2@x", "sa3@x")


def _make_sa(email: str) -> dict:
    """构造一个可被 _is_sa_dict 认可的假 SA 字典。"""
    return {"client_email": email, "private_key": f"private-key-for-{email}"}


def _make_fake_drive_changes(emails=SA_EMAILS, token_side_effect=None):
    """构造假 drive_changes 模块。

    - ``_discover_sa_files()`` 返回假 SA 字典列表
    - ``_sa_access_token(sa)`` 默认返回 ``"token-for-" + sa["client_email"]``
    """
    fake = mock.MagicMock()
    fake._discover_sa_files.return_value = [_make_sa(e) for e in emails]
    if token_side_effect is None:
        fake._sa_access_token.side_effect = lambda sa: "token-for-" + sa["client_email"]
    else:
        fake._sa_access_token.side_effect = token_side_effect

    rotation = {"i": 0}

    def _deterministic_rotation_start(n):
        s = rotation["i"] % n
        rotation["i"] += 1
        return s

    fake._sa_rotation_start.side_effect = _deterministic_rotation_start

    def _reset_rotation():
        rotation["i"] = 0

    fake.reset_rotation = _reset_rotation

    return fake


@pytest.fixture(autouse=True)
def reset_drive_auth_state():
    """每个用例前后重置模块级 token 缓存。"""
    drive_auth._reset_state()
    yield
    drive_auth._reset_state()


@pytest.fixture
def fake_drive_changes(monkeypatch):
    """打桩 drive_auth._load_drive_changes，返回假 drive_changes 模块。"""
    fake = _make_fake_drive_changes()
    monkeypatch.setattr(drive_auth, "_load_drive_changes", lambda: fake)
    return fake


def test_token_rotates_across_sas(fake_drive_changes):
    """连续 3 次调用应轮询到 3 个不同的 SA，各拿各自的 token。"""

    tokens = [drive_auth.get_drive_bearer_token() for _ in range(3)]

    assert tokens == ["token-for-sa1@x", "token-for-sa2@x", "token-for-sa3@x"]
    assert len(set(tokens)) == 3
    assert fake_drive_changes._sa_access_token.call_count == 3


def test_per_sa_cache_hit(fake_drive_changes):
    """同一 SA 在缓存有效期内再次被轮询到时，不重复换 token。"""
    first_round = [drive_auth.get_drive_bearer_token() for _ in range(3)]
    assert fake_drive_changes._sa_access_token.call_count == 3

    second_round = [drive_auth.get_drive_bearer_token() for _ in range(3)]

    assert second_round == first_round
    # 未发生新的换 token 调用：缓存按 SA 维度生效
    assert fake_drive_changes._sa_access_token.call_count == 3


def test_no_sa_returns_none(monkeypatch):
    """无可用 SA 时返回 None，且不尝试换 token。"""
    fake = _make_fake_drive_changes(emails=())
    monkeypatch.setattr(drive_auth, "_load_drive_changes", lambda: fake)

    assert drive_auth.get_drive_bearer_token() is None
    assert fake._sa_access_token.call_count == 0


def test_empty_token_not_cached(fake_drive_changes):
    """换 token 返回空值时返回 None，且空值不入缓存，下次调用会重试。"""
    tokens = iter(["", "token-for-sa1@x"])
    fake_drive_changes._sa_access_token.side_effect = lambda sa: next(tokens)

    assert drive_auth.get_drive_bearer_token() is None
    assert fake_drive_changes._sa_access_token.call_count == 1

    fake_drive_changes.reset_rotation()  # 回到起点，再次轮询到同一个 SA：空值未缓存，必须重试
    assert drive_auth.get_drive_bearer_token() == "token-for-sa1@x"
    assert fake_drive_changes._sa_access_token.call_count == 2
