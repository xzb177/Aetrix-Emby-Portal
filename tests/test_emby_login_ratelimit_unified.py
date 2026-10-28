"""r4-auth 回归：Emby 登录四条路径共用同一个网关限流桶。

#1417 的注释称小写别名“限流语义与大写路径完全一致”，实测不成立：
- /emby/ 大小写各建独立桶（计数键含命中的前缀），合计 30 次/分钟；
- 裸 /Users/ 两条路径连中间件的门都进不了（门只认 /api/ 与 /emby/），
  网关层完全不限流；只有 handler 内按账号的 10 次/分钟还在兜底，
  但换用户名横扫时每个账号各有各的桶，挡不住。
修复：检查前 canonical_rate_limit_path() 归一 + 两边中间件的门加 /Users/。
本文件用内存计数桩掉 backend.database.rate_limit_incr，跑真实的
_check_rate_limit / _ea_check_limit / EaRateLimitMiddleware，不复制逻辑。
"""
import asyncio
import os

os.environ.setdefault("DATABASE_TYPE", "sqlite")
os.environ.setdefault("REDIS_ENABLED", "false")
os.environ.setdefault("SECRET_KEY", "test-login-ratelimit-key-32chars-min")

import pytest

from backend.ratelimit import canonical_rate_limit_path

UPPER_EMBY = "/emby/Users/AuthenticateByName"
LOWER_EMBY = "/emby/Users/authenticatebyname"
UPPER_BARE = "/Users/AuthenticateByName"
LOWER_BARE = "/Users/authenticatebyname"


@pytest.fixture()
def fake_incr(monkeypatch):
    counts = {}

    def _incr(key, ttl=None):
        counts[key] = counts.get(key, 0) + 1
        return counts[key]

    monkeypatch.setattr("backend.database.rate_limit_incr", _incr)
    return counts


def test_canonical_maps_all_four_login_paths():
    for p in (UPPER_EMBY, LOWER_EMBY, UPPER_BARE, LOWER_BARE):
        assert canonical_rate_limit_path(p) == UPPER_EMBY
    # 非登录路径原样返回，不被误归一。
    assert canonical_rate_limit_path("/Users/123") == "/Users/123"
    assert canonical_rate_limit_path("/emby/Users") == "/emby/Users"
    assert canonical_rate_limit_path("/emby/Users/AuthenticateByName/") == (
        "/emby/Users/AuthenticateByName/")


def test_em_shared_bucket(fake_incr):
    from backend.main import _check_rate_limit

    ip = "203.0.113.9"
    for _ in range(15):
        assert _check_rate_limit(ip, UPPER_EMBY, False)[0] is True
    assert _check_rate_limit(ip, UPPER_EMBY, False)[0] is False
    # 同一 IP 换大小写、换裸路径，仍在同一个桶里：立刻被拦，不是各 15 次。
    assert _check_rate_limit(ip, LOWER_EMBY, False)[0] is False
    assert _check_rate_limit(ip, UPPER_BARE, False)[0] is False
    assert _check_rate_limit(ip, LOWER_BARE, False)[0] is False
    # 不同 IP 不受连坐。
    assert _check_rate_limit("203.0.113.10", LOWER_BARE, False)[0] is True
    # 桶只有一个键（规范前缀），不是大小写各一。
    login_keys = [k for k in fake_incr if "AuthenticateByName" in k]
    assert len(login_keys) == 2  # 两个 IP 各一桶
    assert all("authenticatebyname" not in k for k in login_keys)


def test_ea_shared_bucket(fake_incr):
    from emby_api.asgi_middleware import _ea_check_limit

    ip = "203.0.113.11"
    for _ in range(15):
        assert _ea_check_limit(ip, LOWER_EMBY, False)[0] is True
    assert _ea_check_limit(ip, LOWER_EMBY, False)[0] is False
    assert _ea_check_limit(ip, UPPER_EMBY, False)[0] is False
    assert _ea_check_limit(ip, LOWER_BARE, False)[0] is False


def test_non_login_users_path_not_throttled(fake_incr):
    """门放宽到 /Users/ 后，其它 /Users/ 路径仍无匹配规则：直接放行且不计数。"""
    from backend.main import _check_rate_limit
    from emby_api.asgi_middleware import _ea_check_limit

    for _ in range(200):
        assert _check_rate_limit("203.0.113.12", "/Users/123", False)[0] is True
        assert _ea_check_limit("203.0.113.12", "/Users/123", False)[0] is True
    assert fake_incr == {}


def test_ea_middleware_blocks_bare_login_path(fake_incr):
    """EA 中间件整段实测：裸小写登录 15 次后第 16 次回 429，不再进下游。"""
    from emby_api.asgi_middleware import EaRateLimitMiddleware

    calls = []

    async def _app(scope, receive, send):
        calls.append(scope["path"])
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})

    mw = EaRateLimitMiddleware(_app)

    async def _one():
        status = {}

        async def _receive():
            return {"type": "http.request", "body": b"", "more_body": False}

        async def _send(msg):
            if msg["type"] == "http.response.start":
                status["code"] = msg["status"]

        scope = {
            "type": "http", "method": "POST", "path": LOWER_BARE,
            "headers": [], "client": ("203.0.113.13", 1234),
            "server": ("testserver", 80), "scheme": "http",
            "query_string": b"", "root_path": "",
        }
        await mw(scope, _receive, _send)
        return status["code"]

    for _ in range(15):
        assert asyncio.run(_one()) == 200
    assert len(calls) == 15
    assert asyncio.run(_one()) == 429
    assert len(calls) == 15  # 第 16 次没进下游


def test_middleware_gates_cover_bare_users():
    """两边中间件的门必须含 /Users/（与 test_server_resource 的结构断言同口径）。"""
    import ast
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    for path, name in ((root / "backend/main.py", "rate_limit_middleware"),
                       (root / "emby_api/asgi_middleware.py", "EaRateLimitMiddleware")):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if isinstance(node, (ast.AsyncFunctionDef, ast.ClassDef)) and node.name == name:
                body = ast.get_source_segment(path.read_text(), node)
                assert '"/Users/"' in body, f"{path} 的 {name} 门未覆盖裸 /Users/"
                break
        else:
            raise AssertionError(f"{path} 里没找到 {name}")
