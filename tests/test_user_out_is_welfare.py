"""回归：/auth/me 出参带 is_welfare，前端据此跳过非公益用户的活力值请求。

背景（2026-10-22 开服当晚）：活力值仅限公益服用户，非公益用户查询必 403；
前端顶栏等五个调用点对所有登录用户都发这个请求，服务端一晚刷了 111 条 403。
修复：_user_out 透出 is_welfare，前端本地已知非公益时直接不发请求。
服务端仍是最终裁决（标记缺失的老登录态照常问服务端），这里锁出参口径。
"""
from __future__ import annotations

import os
from types import SimpleNamespace

os.environ.setdefault("DATABASE_TYPE", "sqlite")

import pytest

portal_mod = pytest.importorskip("backend.api.emby_portal")


def _stub_user(**overrides):
    base = dict(
        id=7,
        username="u7",
        email=None,
        emby_username=None,
        is_vip=False,
        is_active=True,
        is_staff=False,
        is_welfare=False,
        created_at=None,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def test_user_out_defaults_is_welfare_false():
    out = portal_mod._user_out(_stub_user(), db=None)
    assert out.is_welfare is False


def test_user_out_passes_through_is_welfare_true():
    out = portal_mod._user_out(_stub_user(is_welfare=True), db=None)
    assert out.is_welfare is True


def test_user_out_tolerates_missing_is_welfare_attr():
    user = _stub_user()
    del user.is_welfare
    out = portal_mod._user_out(user, db=None)
    assert out.is_welfare is False
