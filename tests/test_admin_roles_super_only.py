"""管理员角色边界：哪些写操作只允许超管。

## 为什么要有这个文件

``SUPER_ONLY_PREFIXES`` 是**前缀名单**：漏一个前缀，那个域���的写操作就对
「运营」角色敞开，界面上的「仅超管」提示只剩提示——直接打接口就绕过去了。

真实缺陷就是这个形状：访问拦截（UA 黑白名单 + IP 地区封禁）是全站开关，
路由注释写着「仅超管可改」，前端也确实没有把它开放给运营，但名单里**漏了**
``/api/admin/access-guard``，于是运营角色能直接改。一个只读名单写歪就能把
所有人（包括别的管理员）挡在门外，而这种开关只能回这一页改。
"""
from types import SimpleNamespace

import pytest

from backend.admin_roles import (
    ROLE_OPERATOR,
    ROLE_SUPER,
    ROLE_VIEWER,
    blocked_reason,
    is_super,
)


def _admin(role: str):
    return SimpleNamespace(admin_role=role, is_staff=False)


def _req(path: str, method: str = "POST"):
    return SimpleNamespace(url=SimpleNamespace(path=path), method=method)


@pytest.mark.parametrize("path", [
    "/api/admin/access-guard/policy",
    "/api/admin/access-guard/preview",
    "/api/admin/share-guard/policy",
    "/api/admin/admins",
    "/api/admin/playback/policy",
    "/api/admin/api-keys",  # v2.58.0：Service Token 的创建/吊销（防运营自发 admin Key 提权）
])
def test_operator_cannot_touch_super_only_endpoints(path):
    """运营角色对这些写操作一律 403 —— 界面提示之外，服务端自己也要判一次"""
    assert blocked_reason(_req(path), _admin(ROLE_OPERATOR)), f"{path} 应仅限超管"


def test_super_can_still_do_it():
    """不能把超管自己挡在门外"""
    assert blocked_reason(_req("/api/admin/access-guard/policy"), _admin(ROLE_SUPER)) is None
    assert is_super(_admin(ROLE_SUPER))


def test_reads_are_never_super_only():
    """只读 GET 一律放行：审计与排查角色必须能看到这些配置长什么样"""
    for role in (ROLE_SUPER, ROLE_OPERATOR, ROLE_VIEWER):
        for path in ("/api/admin/access-guard", "/api/admin/share-guard"):
            assert blocked_reason(_req(path, "GET"), _admin(role)) is None, f"{role} 看 {path}"


def test_viewer_cannot_write_anywhere():
    """只读角色连防共享、访问拦截都改不了（也不只是超管专属那几项）"""
    assert blocked_reason(_req("/api/admin/access-guard/policy"), _admin(ROLE_VIEWER))
    assert blocked_reason(_req("/api/admin/users/1/ban"), _admin(ROLE_VIEWER))


def test_empty_role_is_treated_as_lowest_privilege():
    """S1（fail closed）：空 / 未知角色按 viewer 处理，不再按 super——
    升级上来的老管理员由启动期迁移显式写成 super（见 admin_roles.ensure_legacy_admin_roles）"""
    legacy = SimpleNamespace(admin_role=None, is_staff=True)
    assert blocked_reason(_req("/api/admin/access-guard/policy"), legacy)
    assert blocked_reason(_req("/api/admin/users/1", method="PUT"), legacy)
    assert blocked_reason(_req("/api/admin/users/1", method="GET"), legacy) is None
    bogus = SimpleNamespace(admin_role="root", is_staff=True)
    assert blocked_reason(_req("/api/admin/users/1", method="PUT"), bogus)