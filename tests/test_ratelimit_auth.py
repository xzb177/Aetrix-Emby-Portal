"""限流中间件 _is_authenticated 回归测试（r1）。

背景：旧实现只看 Authorization 头「有没有」（不验有效性），攻击者发任意
伪造头就能拿到认证档限额（5 倍）。现只认合法 access JWT（纯验签，
fail-closed）。
"""
import os
from types import SimpleNamespace

os.environ.setdefault("DATABASE_TYPE", "sqlite")
os.environ.setdefault("REDIS_ENABLED", "false")

from backend.main import _is_authenticated
from backend.security import create_access_token


def _req(headers):
    return SimpleNamespace(headers=headers)


def test_no_header_is_anonymous():
    assert _is_authenticated(_req({})) is False


def test_forged_bearer_header_is_anonymous():
    """伪造的 Bearer 头不能拿到认证档——这就是原来的漏洞。"""
    assert _is_authenticated(_req({"Authorization": "Bearer fake-token-123"})) is False


def test_forged_bearer_scheme_case_insensitive():
    assert _is_authenticated(_req({"Authorization": "bEaReR fake-token-123"})) is False


def test_forged_bare_token_is_anonymous():
    assert _is_authenticated(_req({"Authorization": "fake-token-123"})) is False


def test_empty_header_is_anonymous():
    assert _is_authenticated(_req({"Authorization": "   "})) is False


def test_valid_jwt_is_authenticated():
    token = create_access_token(42)
    assert _is_authenticated(_req({"Authorization": "Bearer " + token})) is True
    # 裸 token（无 scheme）也认
    assert _is_authenticated(_req({"Authorization": token})) is True


def test_emby_token_headers_not_counted():
    """X-Emby-Token 是 Emby 凭据，不再计入认证档。"""
    r = _req({"X-Emby-Token": "abcdef", "X-MediaBrowser-Token": "abcdef"})
    assert _is_authenticated(r) is False


# ---------- r38：EM/EA 共用同一判定（横切能力只许一套）----------

def test_shared_impl_em_ea_same_result():
    """r38 回归：EA 的 _ea_is_auth 与 EM 的 _is_authenticated 必须是同一判定。

    旧实现里 EA 只看头在不在（bool(auth or token)），伪造头就能拿到 600/分钟
    认证档；现两处都委托 backend.ratelimit.is_authenticated_request 纯验签。
    """
    from backend.ratelimit import is_authenticated_request
    from emby_api.asgi_middleware import _ea_is_auth

    forged = _req({"Authorization": "Bearer fake-token-999"})
    assert _ea_is_auth(forged) is False
    assert is_authenticated_request(forged) is False

    # Emby token（头或 api_key 参数）也不得计入认证档
    emby_tok = _req({"X-Emby-Token": "abcdef"})
    assert _ea_is_auth(emby_tok) is False
    assert is_authenticated_request(emby_tok) is False

    # 合法 JWT 两处都认
    token = create_access_token(42)
    good = _req({"Authorization": "Bearer " + token})
    assert _ea_is_auth(good) is True
    assert is_authenticated_request(good) is True
    assert _is_authenticated(good) is True


def test_rate_limit_table_is_single_source():
    """规则表只有一份：backend/ratelimit.py::API_RATE_LIMITS；EM/EA 引用它。"""
    from backend import ratelimit

    rules = dict((p, (a, b)) for p, a, b in ratelimit.API_RATE_LIMITS)
    assert rules["/emby/Users/AuthenticateByName"] == (15, 15)
    assert rules["/api/admin/"][0] == 60  # 管理接口匿名档

    import backend.main as em_main
    from emby_api import asgi_middleware as ea_mw

    assert em_main.RATE_LIMITS is ratelimit.API_RATE_LIMITS
    assert ea_mw._EA_RATE_LIMITS is ratelimit.API_RATE_LIMITS
