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
