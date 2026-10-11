"""回归测试：外层缓存时长不得超过 token 真实寿命（min_remaining 不变式）。

覆盖 drive_auth.get_drive_bearer_token 外层缓存与 drive_changes._sa_access_token
内层缓存的 TTL 联动：外层必须向内层索取剩余寿命至少为
_TOKEN_TTL_SECONDS + _TOKEN_MIN_REMAIN_SECONDS 的 token，避免内层返回
只剩 300 秒的缓存 token 被外层按 3300 秒长期缓存，导致后续请求拿过期
token 打 Drive 源站持续 401。
"""

from __future__ import annotations

import os
import time
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


@pytest.fixture(autouse=True)
def reset_drive_auth_state():
    """每个用例前后清空 drive_auth 的模块级缓存，避免用例间串味。"""
    drive_auth._reset_state()
    yield
    drive_auth._reset_state()


@pytest.fixture
def fake_drive_changes(monkeypatch):
    """挂载假 drive_changes 模块，并显式提供真实的内层 _token_cache 字典。"""
    fake = mock.MagicMock()
    fake._token_cache = {}
    fake._discover_sa_files.return_value = [_make_sa(e) for e in SA_EMAILS]
    # 默认 side_effect 接受新增的 min_remaining 关键字，行为与旧调用方一致
    fake._sa_access_token.side_effect = (
        lambda sa, min_remaining=60: "token-for-" + sa["client_email"]
    )
    fake._sa_rotation_start.side_effect = lambda n: 0
    monkeypatch.setattr(drive_auth, "_load_drive_changes", lambda: fake)
    return fake


def test_outer_demands_min_remaining_from_inner(fake_drive_changes):
    """外层向内层索取 token 时必须声明所需的最短剩余寿命。"""
    drive_auth.get_drive_bearer_token()

    fake_drive_changes._sa_access_token.assert_called_once()
    _, kwargs = fake_drive_changes._sa_access_token.call_args
    assert kwargs["min_remaining"] == (
        drive_auth._TOKEN_TTL_SECONDS + drive_auth._TOKEN_MIN_REMAIN_SECONDS
    )


def test_stale_inner_token_not_cached_long(fake_drive_changes):
    """核心回归：内层只剩 300 秒的 token 不得被外层长期缓存。"""
    inner = {"sa1@x": ("T1-stale", time.time() + 300)}

    def sa_access_token(sa, min_remaining=60):
        now = time.time()
        cached = inner.get(sa["client_email"])
        if cached and cached[1] > now + min_remaining:
            return cached[0]
        inner[sa["client_email"]] = ("T-new", now + 3600)
        return "T-new"

    fake_drive_changes._sa_access_token.side_effect = sa_access_token

    t0 = time.monotonic()
    token = drive_auth.get_drive_bearer_token()

    assert token == "T-new"
    expiry = drive_auth._token_cache["sa1@x"][1]
    assert expiry - t0 <= 3600 + 120


def test_outer_cache_hit_still_short_circuits(fake_drive_changes):
    """外层缓存命中时不得再穿透到内层。"""
    first = drive_auth.get_drive_bearer_token()
    second = drive_auth.get_drive_bearer_token()

    assert first == second
    assert fake_drive_changes._sa_access_token.call_count == 1


def test_inner_default_threshold_unchanged():
    """内层默认 min_remaining 仍是 60 秒，既有调用方行为不变。"""
    try:
        from backend.emby_server import drive_changes
    except ImportError:  # 兼容以 backend 为根的扁平导入路径
        from emby_server import drive_changes

    email = "e@x"
    drive_changes._token_cache.pop(email, None)
    drive_changes._token_cache[email] = ("TOK", time.time() + 300)
    try:
        token = drive_changes._sa_access_token(
            {"client_email": email, "private_key": "x"}
        )
        assert token == "TOK"
    finally:
        drive_changes._token_cache.pop(email, None)
