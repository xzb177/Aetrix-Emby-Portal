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


# ==================== R49 M2：凭据类路径后缀集中判定 ====================
# 散落的 _require_super 收进 admin_roles 的 SUPER_ONLY_SUFFIXES：读写一律只限超管。
# 口径补正（2026-10-19）：原 PR 文把 GET 读放宽成「人人可看」，冒烟测试确诊是回归
# （旧 _require_super 拦读写、冒烟断言「读写都被拒」），恢复成读也要拦。

_RCLONE = "/api/admin/servers/5/rclone-conf"

# 7 条服务器写路由：operator 仍可写（M2 未动它们，防回归）
_SERVER_WRITE_ROUTES = [
    ("POST", "/api/admin/servers"),
    ("PUT", "/api/admin/servers/5"),
    ("POST", "/api/admin/servers/test"),
    ("POST", "/api/admin/servers/5/test"),
    ("POST", "/api/admin/servers/5/activate"),
    ("POST", "/api/admin/servers/5/toggle"),
    ("DELETE", "/api/admin/servers/5"),
]


def test_rclone_conf_write_is_super_only():
    """写 rclone.conf 仍只限超管：operator/viewer 的 POST 一律 403"""
    for role in (ROLE_OPERATOR, ROLE_VIEWER):
        reason = blocked_reason(_req(_RCLONE, "POST"), _admin(role))
        assert reason, f"{role} POST {_RCLONE} 应被拦"
    assert blocked_reason(_req(_RCLONE, "POST"), _admin(ROLE_SUPER)) is None


def test_rclone_conf_read_is_super_only():
    """GET rclone-conf 也只限超管：operator/viewer 的读一律 403
    （2026-10-19 补正：rclone.conf 含网盘凭据，恢复旧 _require_super 的读写都拦口径；
    冒烟测试 scripts/smoke_test_servers.py 的「读写都被拒」是长约，之前放宽读是回归）"""
    for role in (ROLE_OPERATOR, ROLE_VIEWER):
        reason = blocked_reason(_req(_RCLONE, "GET"), _admin(role))
        assert reason, f"{role} GET {_RCLONE} 应被拦"
        assert "超级管理员" in reason
    assert blocked_reason(_req(_RCLONE, "GET"), _admin(ROLE_SUPER)) is None


@pytest.mark.parametrize("method,path", _SERVER_WRITE_ROUTES)
def test_server_write_routes_still_open_to_operator(method, path):
    """M2 未动服务器其余写路由：operator 仍可写，viewer 仍被拦（防回归）"""
    assert blocked_reason(_req(path, method), _admin(ROLE_OPERATOR)) is None, f"operator {method} {path}"
    assert blocked_reason(_req(path, method), _admin(ROLE_VIEWER)), f"viewer {method} {path} 应被拦"


def test_rclone_conf_suffix_does_not_overmatch():
    """后缀判定不误伤：路径里含 rclone-conf 但不是后缀结尾的不受影响"""
    assert blocked_reason(_req("/api/admin/servers/rclone-conf-list", "POST"), _admin(ROLE_OPERATOR)) is None
    assert blocked_reason(_req("/api/admin/mounts/health", "POST"), _admin(ROLE_OPERATOR)) is None


# ==================== 批 7 补后端批（2026-10-26） ====================
# 批 7 审计定档的 5 个 B 档写口：运营此前可写，现收进 SUPER_ONLY_PREFIXES。
# 判定走同一集中机制（get_current_admin → ensure_admin_allowed → blocked_reason，
# emby 路由的 require_staff 经 _bearer_admin 同样走 get_current_admin）。
# 前缀判定只拦非安全方法，各口 GET 读口不受影响——与名单内其它前缀同口径。

_BATCH7_SUPER_ONLY_WRITES = [
    ("POST", "/api/admin/emby/mounts/rclone/conf"),
    ("PUT", "/api/admin/settings/registration"),
    ("PUT", "/api/admin/hotlink-config"),
    ("POST", "/api/admin/login-logs/purge"),
    ("PUT", "/api/admin/items-dedup-cache-config"),
]

_BATCH7_READ_SIBLINGS = [
    "/api/admin/emby/mounts/rclone/conf",
    "/api/admin/settings/registration",
    "/api/admin/hotlink-config",
    "/api/admin/login-logs",
    "/api/admin/items-dedup-cache-config",
]


@pytest.mark.parametrize("method,path", _BATCH7_SUPER_ONLY_WRITES)
def test_batch7_operator_and_viewer_cannot_write(method, path):
    """运营 / 只读调这 5 个写口一律被拦（返回拒绝的判定即 blocked_reason 非空）"""
    for role in (ROLE_OPERATOR, ROLE_VIEWER):
        reason = blocked_reason(_req(path, method), _admin(role))
        assert reason, f"{role} {method} {path} 应被拦"
        assert "超级管理员" in reason


@pytest.mark.parametrize("method,path", _BATCH7_SUPER_ONLY_WRITES)
def test_batch7_super_can_still_write(method, path):
    """超管调这 5 个写口仍放行：不能把超管自己挡在门外"""
    assert blocked_reason(_req(path, method), _admin(ROLE_SUPER)) is None, f"super {method} {path}"


@pytest.mark.parametrize("path", _BATCH7_READ_SIBLINGS)
def test_batch7_reads_stay_open_to_operator(path):
    """只改写口：各口 GET 读口运营仍可看（前缀判定只拦非安全方法）"""
    assert blocked_reason(_req(path, "GET"), _admin(ROLE_OPERATOR)) is None, f"operator GET {path}"
    assert blocked_reason(_req(path, "GET"), _admin(ROLE_VIEWER)) is None, f"viewer GET {path}"