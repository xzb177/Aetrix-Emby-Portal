#!/usr/bin/env python3
"""写端点鉴权覆盖检查（v2.30.0）

**问题**：漏掉一个鉴权依赖不会让任何检查变红——路由照常注册、类型检查照常通过、
已有的冒烟测试也不会去点那个新端点（它测的是自己关心的那些路径）。结果就是一个
**既没登录也能写**的接口，而它往往要等到被人扫到才知道。

真实排摸（写这条检查时跑出来的）：112 个写端点里，只有 5 个没有鉴权依赖，
而这 5 个全是**本来就该免鉴权**的——注册 / 登录 / 刷新令牌、支付网关回调
（靠验签）、管理员登录（靠限流 + 人机验证 + 登录日志）。也就是说现在的口径是干净的，
但这份干净只靠「每个人都记得写 `Depends(get_current_user)`」维持。

因此把口径变成一条可以执行的规则：

    `/api/admin/*` 与 `/api/user/*` 下的每个写端点（POST/PUT/PATCH/DELETE）
    都必须带上鉴权依赖 —— 或者登记在 ``PRE_AUTH`` 里并写明「它靠什么别的机制挡住」

判定是源码级的：路由级 ``dependencies`` 或端点签名里出现下表中的任一依赖名即算覆盖。
免鉴权端点必须显式登记，而且必须说清替代机制（限流 / 验签 / 验证码…），
不能只写一句「故意的」。

用法：python3 scripts/check_auth_coverage.py
"""

from __future__ import annotations

import importlib
import inspect
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

os.environ.setdefault("DATABASE_TYPE", "sqlite")
os.environ.setdefault("REDIS_ENABLED", "false")

WRITE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})

# 算「已被鉴权」的依赖名（任一出现在路由级 dependencies 或端点源码里即可）
AUTH_DEPENDENCIES = (
    "get_current_user",
    "get_current_user_optional",
    "get_current_user_or_token",
    "get_current_admin",
    "get_admin_or_emby_user",
    "require_admin",
    "require_staff",
    "require_panel_key",       # EA ↔ EM 之间用共享密钥，不是管理员 JWT
)

# 定义对外路由的模块（新增域模块要登记在这里，否则它下面的写端点不被判定）
ROUTER_MODULES = (
    # 用户端
    "backend.api.user",
    "backend.api.store",  # 想看商店骨架：POST /want 走 get_current_user
    "backend.api.emby_portal",
    "backend.api.economy",
    "backend.api.invitation",
    "backend.api.assistant",
    "backend.api.site",
    "backend.websocket",
    # 管理端
    "backend.api.admin_core",
    "backend.api.admin",
    "backend.api.admin_ops",
    "backend.api.admin_economy",
    "backend.api.admin_user_grants",
    "backend.api.admins_admin",
    "backend.api.setup",
    "backend.api.orders_admin",
    "backend.api.coupons_admin",
    "backend.api.capabilities_admin",
    "backend.api.playback_admin",
    "backend.api.realms",
    "backend.api.reminders_admin",
    "backend.api.admin_apikey",  # API Key / Service Token 管理（v2.58.0）
    "backend.api.servers",
    "backend.api.emby_servers",
    "backend.emby_server.portal",
    "backend.emby_server.portal_mount_routes",
    # EA 侧（面板密钥鉴权；EM 不挂载它们）
    "backend.emby_server.mount_health",
    "backend.emby_server.nodes",
)

# 免鉴权写端点：(方法, 路径) → 靠什么别的机制挡住。
# 判断标准：**必须写出替代机制**（限流 / 验签 / 一次性令牌 / 验证码…），
# 不能只写一句「故意的」——「免鉴权」是一个需要解释的决定。
PRE_AUTH: dict[tuple[str, str], str] = {
    ("POST", "/api/user/auth/register"):
        "注册本身就是取得身份前的动作；走注册模式开关（开放 / 凭码 / 关闭）+ 限流 + 人机验证",
    ("POST", "/api/user/auth/login"):
        "登录同理：限流（按 IP）+ 人机验证 + 登录日志（成功与失败都记）",
    ("POST", "/api/user/auth/refresh"):
        "刷新令牌：只认请求里带的那枚 refresh token 自己，无效即拒（限流同上）",
    ("POST", "/api/user/auth/tg-login"):
        "Bot 一键免密登录：登录本身就是取得身份前的动作；凭 32 字节随机一次性 token"
        "（10 分钟过期、一次消费、每用户每小时最多 10 个），且 token 内 user_id 与"
        " telegram_id 必须与 WebUser 记录一致，不一致即 401",
    ("POST", "/api/user/economy/payment/notify"):
        "支付网关回调：拿不到管理员身份（是网关在调），靠 HMAC 验签 + 金额与订单实付核对 + 订单幂等",
    ("POST", "/api/admin/auth/login"):
        "管理员登录：还没通过鉴权就没有身份可归属；限流（8 次/分钟/IP）+ 人机验证 + 登录日志",
    ("POST", "/api/admin/setup"):
        "首次运行向导：建第一个管理员（完成后永久 403）；还没通过鉴权就没有身份可归属；"
        "替代机制：限流（8 次/分钟/IP）+ 登录日志（成功与失败都记）+ 一次性——"
        "setup_completed 置 True 后入口永久关闭，库里已有管理员时自愈补旗并 403",
}


def collect_write_endpoints() -> dict[tuple[str, str], tuple[object, bool]]:
    """{(方法, 路径): (端点函数, 路由级是否已鉴权)}"""
    found: dict[tuple[str, str], tuple[object, bool]] = {}
    for module_name in ROUTER_MODULES:
        module = importlib.import_module(module_name)
        for obj in list(vars(module).values()):
            if not hasattr(obj, "__dict__"):
                continue
            routes = vars(obj).get("routes")
            if not isinstance(routes, list):
                continue
            router_auth = False
            for dependency in vars(obj).get("dependencies") or ():
                name = getattr(getattr(dependency, "dependency", None), "__name__", "")
                if name in AUTH_DEPENDENCIES:
                    router_auth = True
                    break
            for route in routes:
                methods = set(getattr(route, "methods", None) or ()) & WRITE_METHODS
                path = getattr(route, "path", "")
                if not methods or not (path.startswith("/api/admin") or path.startswith("/api/user")):
                    continue
                for method in sorted(methods):
                    found.setdefault((method, path), (getattr(route, "endpoint", None), router_auth))
    return found


def guarded(endpoint, router_auth: bool) -> bool:
    """这条路由是否已被鉴权（路由级依赖，或端点签名里出现鉴权依赖）"""
    if router_auth:
        return True
    if endpoint is None:
        return False
    try:
        source = inspect.getsource(endpoint)
    except (OSError, TypeError):
        return False
    return any(name in source for name in AUTH_DEPENDENCIES)


def scan(endpoints: dict | None = None) -> tuple[list[str], list[str], int]:
    """→ (未鉴权的写端点, 名单里已失效的免鉴权条目, 写端点总数)

    ``endpoints`` 只给自检用：喂一份合成端点表，证明这条检查**真的会失败**。
    """
    endpoints = collect_write_endpoints() if endpoints is None else endpoints
    missing: list[str] = []
    for (method, path), (endpoint, router_auth) in sorted(
        endpoints.items(), key=lambda kv: kv[0][1]
    ):
        if (method, path) in PRE_AUTH:
            continue
        if not guarded(endpoint, router_auth):
            missing.append(f"{method:6s} {path:58s} ({getattr(endpoint, '__module__', '?')})")
    stale = [f"{m} {p}（免鉴权登记已失效，删掉它）" for (m, p) in PRE_AUTH if (m, p) not in endpoints]
    return missing, stale, len(endpoints)


def main() -> int:
    missing, stale, total = scan()
    if missing:
        print(f"❌ 有 {len(missing)} 个写端点没有鉴权（共 {total} 个写端点）：")
        for item in missing:
            print("   ", item)
        print("\n改法：给端点加 `Depends(get_current_user)`（用户端）/ `Depends(get_current_admin)`"
              "（管理端），\n或在路由上写 `dependencies=[Depends(...)]`；"
              "确实应该免鉴权（注册 / 登录 / 网关回调类）就登记到\n"
              "``PRE_AUTH`` 并写清「靠什么别的机制挡住」——限流、验签、一次性令牌，"
              "不能只写一句「故意的」。")
        return 1
    if stale:
        print(f"❌ 免鉴权名单里有 {len(stale)} 条死条目：")
        for item in stale:
            print("   ", item)
        return 1
    print(f"✅ 写端点鉴权覆盖完整（共 {total} 个写端点；"
          f"{len(PRE_AUTH)} 个已登记的免鉴权端点）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
