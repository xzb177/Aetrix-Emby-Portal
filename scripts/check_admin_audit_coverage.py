#!/usr/bin/env python3
"""管理端写端点审计覆盖检查（v2.30.0）

**问题**：后台的「操作日志」是排查「谁把这条设置改了 / 谁删了这个库 / 谁关了这张单」的唯一线索。
``/api/admin/emby/*`` 现在由中间件（``backend/emby_server/audit.py``）统一记录，
但 ``/api/admin/*`` 的其他域靠**每个端点自己记得写一行** ``_audit(...)``——
而「记得」不是一种机制：漏掉的那个通常就是最危险的那个。

真实案例（就是写这条检查时找到的）：``POST /api/admin/tickets/{id}/close`` 关单并推送给用户，
但一条审计都不写；它的两个兄弟端点（``PUT /tickets/{id}``、``POST /tickets/{id}/reply``）
都写了。于是用户收到「工单已被关闭」，运营在操作日志里查不到是谁关的。

这条检查把口径变成一条可以执行的规则：

    **每个 ``/api/admin`` 的写端点（POST/PUT/PATCH/DELETE）都必须留下审计记录**
    —— ``/api/admin/emby`` 前缀由中间件覆盖，其余必须在自己的函数体里调用 ``_audit(...)``。

判定方式是源码级的：取每个写端点的函数源码，找 ``_audit(`` 调用。
这样新增端点时不需要谁记得改一份清单，检查会自己发现。

**一种等价写法也算**（v2.39.0）：端点把整段同步写库交给**同一模块里的函数**，
自己只负责 ``await run_in_threadpool(那段函数)``——审计写在那个函数里，与那次写库
在同一个事务、同一个工作线程里执行（语义与写在路由里完全一样）。性能整改反复
做这件事（v2.36.0 求片链路、v2.37.0 探测层、v2.38.0/2.39.0 资金链路），所以这里
跟一层：端点直接调用的模块级函数里写了 ``_audit(`` 就算数。
只看一层、只认本模块的定义，不做完整调用图分析——宁可少报，也不把真缺口放过：
一个既不在自己函数体、也不在被调用函数里的写端点，依然会被点名。

**例外要显式登记**（``EXCEPTIONS``）：纯探测端点（不改配置、不动权限、不发消息、
不花钱，只是问一句「这个地址通不通」）与登录本身。例外必须写清原因，
而且如果它哪天开始写库 / 发消息了，就应该把它从名单里删掉——名单太长就说明这条规则
本身需要重新讨论，而不是继续加行。

用法：python3 scripts/check_admin_audit_coverage.py
"""

from __future__ import annotations

import ast
import importlib
import inspect
import os
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# 只在服务端可导入（读模型要在数据库环境齐备的情况下）；用内存 SQLite 避免碰到真库
os.environ.setdefault("DATABASE_TYPE", "sqlite")
os.environ.setdefault("REDIS_ENABLED", "false")

WRITE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})

# 「把整段同步写库交给模块级函数」的下放调用（与 check_blocking_routes 同一份口径）
OFFLOAD_CALLS = frozenset({"run_in_threadpool", "to_thread", "run_in_executor", "run_sync"})

# 真的调用了 ``_audit``：词边界是必要的——名字以 ``_audit`` 结尾的普通函数
#（``_worker_without_audit(``）不该被当成写了审计。
AUDIT_CALL = re.compile(r"\b_audit\(")

# 管理端路由所在的模块（含拆出去的域模块；新增域模块要登记在这里）
ROUTER_MODULES = (
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
)

# 由中间件统一审计的前缀（不要求端点自己写 _audit）
MIDDLEWARE_PREFIX = "/api/admin/emby"

# 例外：(方法, 路径) → 为什么不算「改了东西」。
# 判断标准：不改配置、不动权限、不发消息、不花钱。
EXCEPTIONS: dict[tuple[str, str], str] = {
    ("POST", "/api/admin/servers/test"):
        "纯探测一份还没保存的配置：不落库、不改任何状态，只是把连通性结果返回给表单",
    ("POST", "/api/admin/servers/{server_id}/test"):
        "重新体检并只刷新「可达性」读数：不改变谁能看什么、也不动任何业务配置",
    ("POST", "/api/admin/auth/login"):
        "登录本身没有「管理员身份」可归属（未通过鉴权），而且它已经落在**登录日志**里"
        "（authlog.record_event，reason=admin_login/admin_login_failed，含 IP 与 UA）；"
        "操作日志再记一份只会把真正的写操作冲淡",
    ("POST", "/api/admin/setup"):
        "首次运行向导建第一个管理员：没有「管理员身份」可归属（未通过鉴权），而且它已经落在"
        "**登录日志**里（authlog.record_event，reason=admin_setup/admin_setup_failed，含 IP 与 UA）；"
        "操作日志再记一份只会把真正的写操作冲淡",
}


def collect_admin_write_endpoints() -> dict[tuple[str, str], object]:
    """{(方法, 路径): 端点函数} —— 管理端所有写端点（去重）

    路由对象的 ``path`` 已经含 router 前缀（APIRouter 的写法），不要再拼一次。
    """
    found: dict[tuple[str, str], object] = {}
    for module_name in ROUTER_MODULES:
        module = importlib.import_module(module_name)
        for obj in list(vars(module).values()):
            if not hasattr(obj, "__dict__"):
                continue
            routes = vars(obj).get("routes")
            if not isinstance(routes, list) or not isinstance(vars(obj).get("prefix"), str):
                continue
            for route in routes:
                methods = set(getattr(route, "methods", None) or ()) & WRITE_METHODS
                path = getattr(route, "path", "")
                if not methods or not path.startswith("/api/admin"):
                    continue
                for method in sorted(methods):
                    found.setdefault((method, path), getattr(route, "endpoint", None))
    return found


def audited(endpoint) -> bool:
    """端点自己（或它直接调用的本模块函数）是否写了 ``_audit(...)``

    嵌套函数天然算数：``inspect.getsource`` 给的是整段源码，端点里定义的
    ``def _work(): ... _audit(...)`` 就在这份源码里。
    """
    if endpoint is None:
        return False
    try:
        source = inspect.getsource(endpoint)
    except (OSError, TypeError):
        return False
    return bool(AUDIT_CALL.search(source)) or _audited_in_callees(endpoint, source)


def _audited_in_callees(endpoint, source: str) -> bool:
    """端点直接调用的**本模块函数**里写了 ``_audit(...)``（只跟一层）

    对应这种写法（见 ``backend/api/admin.py`` 的 ``_mark_order_paid``）：
    端点只做 ``await run_in_threadpool(_helper, ...)``，落库 + 审计整段在
    ``_helper`` 里——下放线程池之后 ORM 属性都过期了，审计本来就得跟写库同一段。
    """
    module = inspect.getmodule(endpoint)
    if module is None:
        return False
    try:
        tree = ast.parse(source)
    except SyntaxError:  # 同步源码必然可解析；解析不了就按「未覆盖」处理
        return False
    called: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        fname = func.id if isinstance(func, ast.Name) else getattr(func, "attr", "")
        if isinstance(func, ast.Name):
            called.add(func.id)
        # ``await run_in_threadpool(_helper, db, ...)``：下线目标是实参而不是被调用方
        if fname in OFFLOAD_CALLS:
            for arg in list(node.args) + [kw.value for kw in node.keywords]:
                if isinstance(arg, ast.Name):
                    called.add(arg.id)
    for name in sorted(called):
        helper = getattr(module, name, None)
        if helper is None or helper is endpoint or not callable(helper):
            continue
        try:
            helper_source = inspect.getsource(helper)
        except (OSError, TypeError):
            continue
        if AUDIT_CALL.search(helper_source):
            return True
    return False


def scan(endpoints: dict | None = None) -> tuple[list[str], list[str], int]:
    """→ (未覆盖, 名单里已失效的例外, 写端点总数)

    ``endpoints`` 只给自检用：喂一份合成端点表，就能证明这条检查**真的会失败**
    （而一个永远返回 0 的检查等于没有）。默认扫真实路由。
    """
    endpoints = collect_admin_write_endpoints() if endpoints is None else endpoints
    missing: list[str] = []
    for (method, path), endpoint in sorted(endpoints.items(), key=lambda kv: kv[0][1]):
        if path.startswith(MIDDLEWARE_PREFIX):
            continue  # 中间件统一覆盖
        if (method, path) in EXCEPTIONS:
            continue
        if not audited(endpoint):
            missing.append(f"{method:6s} {path:52s} ({module_of(endpoint)})")
    # 例外名单不该腐坏：登记的端点已经消失（或已经补上审计）就是死条目
    stale = [f"{m} {p}（例外已不存在，删掉它）" for (m, p) in EXCEPTIONS if (m, p) not in endpoints]
    return missing, stale, len(endpoints)


def module_of(endpoint) -> str:
    return getattr(endpoint, "__module__", "?")


def main() -> int:
    missing, stale, total = scan()
    if missing:
        print(f"❌ 有 {len(missing)} 个管理端写端点没有留审计（共 {total} 个写端点）：")
        for item in missing:
            print(f"   {item}")
        print("\n改法：在端点里调用 ``_audit(db, current_admin, \"动作名\", \"目标类型\", 目标 id, {...})``"
              " 并在提交前 ``db.commit()``；\n"
              "真的是纯探测（不改配置 / 不动权限 / 不发消息 / 不花钱）就把它登记到 "
              "``PROBE_EXCEPTIONS`` 并写清原因。\n"
              "细节只记「改了什么」，绝不记请求体——密钥、Cookie、挂载凭据都在请求体里。")
        return 1
    if stale:
        print(f"❌ 例外名单里有 {len(stale)} 条死条目：")
        for item in stale:
            print(f"   {item}")
        return 1
    print(f"✅ 管理端写端点审计覆盖完整（共 {total} 个写端点；"
          f"{len(EXCEPTIONS)} 个已登记的例外）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
