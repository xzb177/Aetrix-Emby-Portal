"""服务器资源优化（perf/server-resource）的测试。

覆盖：
1. CacheManager 内存回退有界（条目上限 + TTL 过期 + FIFO 淘汰）
2. 请求体大小限制中间件（超限 413）
3. Emby 登录限流规则存在（15次/分钟）
4. Download/File 端点一律服务端代理（不再对客户端 302）
5. 主列表接口 EnableTotalRecordCount=false 时跳过 COUNT(*)
"""
import importlib
import sys
import time
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent


# ---------- 1. CacheManager 有界 ----------

def _fresh_cache_manager():
    """拿一个 Redis 不可用的 CacheManager（走内存回退路径）。
    只改模块属性并复位，不 reload 整个模块，避免影响其它测试。"""
    import backend.database as dbmod
    dbmod.redis_client = None
    dbmod.CacheManager._memory_cache = {}
    dbmod.CacheManager._memory_cache_order = []
    return dbmod.CacheManager


def test_memory_cache_ttl_expiry():
    CM = _fresh_cache_manager()
    CM.set("k1", "v1", ttl=1)
    assert CM.get("k1") == "v1"
    time.sleep(1.1)
    assert CM.get("k1") is None  # 过期后读不到
    assert CM.exists("k1") is False


def test_memory_cache_bounded():
    CM = _fresh_cache_manager()
    max_n = CM._MEMORY_CACHE_MAX
    for i in range(max_n + 100):
        CM.set(f"k{i}", "x" * 10, ttl=3600)
    assert len(CM._memory_cache) <= max_n
    # 最老的被淘汰，最新的还在
    assert CM.get("k0") is None
    assert CM.get(f"k{max_n + 99}") == "x" * 10


def test_memory_cache_delete_cleans_order():
    CM = _fresh_cache_manager()
    CM.set("a", "1", ttl=60)
    CM.set("b", "2", ttl=60)
    CM.delete("a")
    assert "a" not in CM._memory_cache_order
    assert CM.get("a") is None
    assert CM.get("b") == "2"


# ---------- 2. 请求体限制 ----------

def test_body_limit_middleware_exists():
    """main.py 里有请求体大小限制中间件：读 Content-Length，超限回 413。"""
    import ast
    src = open(REPO_ROOT / "backend/main.py").read()
    tree = ast.parse(src)
    names = [n.name for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]
    assert "request_body_limit_middleware" in names
    # 中间件函数体内要有 413 和 content-length 检查
    for n in ast.walk(tree):
        if isinstance(n, ast.AsyncFunctionDef) and n.name == "request_body_limit_middleware":
            body_src = ast.get_source_segment(src, n)
            assert "413" in body_src
            assert "content-length" in body_src
            assert "MAX_REQUEST_BODY_MB" in body_src
            break
    else:
        raise AssertionError("middleware body not found")


# ---------- 3. Emby 登录限流 ----------

def test_emby_login_rate_limit_rule_exists():
    """API_RATE_LIMITS 里有 /emby/Users/AuthenticateByName 的 15次/分钟规则，
    且中间件覆盖 /emby/ 路径（原来只覆盖 /api/）。
    EM（backend/main.py，单进程模式）和 EA（emby_api，分离部署生产用）都要有。

    r38 起规则表收敛到 backend/ratelimit.py 的 API_RATE_LIMITS（横切能力只许一套），
    两处中间件只许引用、不许各自手写。本测试改查规范位置，并校验两处确实在用它。

    EA 侧 2026-10-07 起为纯 ASGI 类（emby_api/asgi_middleware.py，
    中转零拷贝优化），此处同时接受函数式与类式两种形态。
    """
    import ast

    def _check_rule(path, var_name):
        """解析文件中的限流规则表，校验登录规则存在且 <=15/分钟。"""
        src = open(path).read()
        assert '"/emby/Users/AuthenticateByName"' in src, path
        tree = ast.parse(src)
        for n in ast.walk(tree):
            if isinstance(n, ast.Assign):
                for t in n.targets:
                    if isinstance(t, ast.Name) and t.id == var_name:
                        val = ast.literal_eval(n.value)
                        rules = {p: (a, b) for p, a, b in val}
                        anon, auth = rules["/emby/Users/AuthenticateByName"]
                        assert anon <= 15 and auth <= 15, path
                        return
        raise AssertionError(f"rate limit rule {var_name} not parsed in {path}")

    def _check_mw_covers_emby(path, names):
        """限流中间件的路径判断要包含 /emby/（函数式或类式均可）。"""
        src = open(path).read()
        tree = ast.parse(src)
        for n in ast.walk(tree):
            if isinstance(n, ast.AsyncFunctionDef) and n.name in names:
                body_src = ast.get_source_segment(src, n)
                assert "/emby/" in body_src, path
                return
            if isinstance(n, ast.ClassDef) and n.name in names:
                body_src = ast.get_source_segment(src, n)
                assert "/emby/" in body_src, path
                return
        raise AssertionError(f"rate limit middleware {names} not found in {path}")

    # 规则表规范位置：backend/ratelimit.py::API_RATE_LIMITS（r38 收敛）
    _check_rule(REPO_ROOT / "backend/ratelimit.py", "API_RATE_LIMITS")
    ea_mw_path = REPO_ROOT / "emby_api/asgi_middleware.py"
    # EM 与 EA 不许各自手写表：两处都必须引用规范表
    for p in (REPO_ROOT / "backend/main.py", ea_mw_path):
        src = open(p).read()
        assert "API_RATE_LIMITS" in src, f"{p} 未引用共用规则表"
    # 中间件覆盖 /emby/ 路径：EM 函数式，EA 纯 ASGI 类
    _check_mw_covers_emby(REPO_ROOT / "backend/main.py", {"rate_limit_middleware"})
    _check_mw_covers_emby(ea_mw_path, {"EaRateLimitMiddleware"})

def test_body_limit_middleware_in_both_apps():
    """EM 和 EA 都有请求体大小限制中间件（413）。

    EA 侧 2026-10-07 起为纯 ASGI 类（emby_api/asgi_middleware.py，
    中转零拷贝优化），此处同时接受函数式与类式两种形态。
    """
    import ast

    def _find(path, names):
        src = open(path).read()
        tree = ast.parse(src)
        for n in ast.walk(tree):
            if isinstance(n, ast.AsyncFunctionDef) and n.name in names:
                return ast.get_source_segment(src, n)
            if isinstance(n, ast.ClassDef) and n.name in names:
                return ast.get_source_segment(src, n)
        return None

    # EM：函数式（backend/main.py）
    body_src = _find(REPO_ROOT / "backend/main.py",
                     {"request_body_limit_middleware"})
    assert body_src, "request_body_limit_middleware not found in backend/main.py"
    assert "413" in body_src
    assert "content-length" in body_src
    assert "MAX_REQUEST_BODY_MB" in body_src

    # EA：纯 ASGI 类（emby_api/asgi_middleware.py）
    body_src = _find(REPO_ROOT / "emby_api/asgi_middleware.py",
                     {"EaBodyLimitMiddleware"})
    assert body_src, "EaBodyLimitMiddleware not found in emby_api/asgi_middleware.py"
    assert "413" in body_src
    assert "content-length" in body_src
    assert "MAX_REQUEST_BODY_MB" in body_src


# ---------- 4. Download/File：中继已删，Drive 条目 302 直链 ----------

def test_download_paths_redirect_drive_to_usercontent():
    """下载 / 拉文件两个端点：中继删除后不再做服务端代理转发。

    Drive 条目 302 到谷歌 usercontent 直链（凭据不再经服务器）；
    结构性护栏：两个文件里不许再出现 serve_remote 调用。
    """
    import ast
    for path in [
        REPO_ROOT / "backend/emby_server/media_routes.py",
        REPO_ROOT / "backend/emby_server/mount_routes.py",
    ]:
        src = open(path).read()
        assert "can_redirect_direct" not in src, path
        assert "status_code=302" in src, path
        assert "drive_usercontent_download_url" in src, path
        tree = ast.parse(src)
        calls = [
            n.func.id for n in ast.walk(tree)
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
        ]
        assert "serve_remote" not in calls, path



# ---------- 5. EnableTotalRecordCount=false ----------

def test_query_items_skips_count_when_disabled():
    """_query_items 里有 EnableTotalRecordCount=false 分支：跳过 query.count()，
    改用已取到的 id 列表判断有没有下一页（has_more）。"""
    import ast
    src = open(REPO_ROOT / "backend/emby_server/api.py").read()
    assert "EnableTotalRecordCount" in src
    tree = ast.parse(src)
    found = False
    for n in ast.walk(tree):
        if isinstance(n, ast.FunctionDef) and n.name == "_query_items":
            body_src = ast.get_source_segment(src, n)
            assert "want_total" in body_src
            # 不查总数时，用 has_more 判断有没有下一页（不再依赖 limit+1 取巧，
            # 去重后的 primary_ids 已在内存，直接比较长度即可）
            assert "has_more" in body_src
            found = True
            break
    assert found, "_query_items not found"
