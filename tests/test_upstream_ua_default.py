"""上游出站默认 UA 回归：源没配 UA 时不许裸用 Python-urllib 默认 UA。

实证：2026-10-26 瑟瑟服（id=3）前门对 Python-urllib 默认 UA 回 403，
聚合把 403 误判为凭据失效；换 Aetrix UA 即 200。
"""
from unittest import mock

from backend.emby_server import upstream_aggregate_run as agg


class _Src:
    user_agent = None
    referer = None
    extra_headers_json = None


def test_source_headers_default_ua():
    headers = agg._source_headers(_Src())
    assert headers["User-Agent"] == "Aetrix/1.0"


def test_default_http_get_sets_ua_when_missing():
    seen = {}

    class _Resp:
        status = 200

        def read(self):
            return b"{}"

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def fake_urlopen(req, timeout=None):
        seen["ua"] = req.get_header("User-agent") or req.get_header("User-Agent")
        return _Resp()

    # 传输已收进出站公共层：缝合点随之上移，断言一字未动。
    with mock.patch(
        "backend.emby_server.upstream_http.safe_urlopen", fake_urlopen
    ):
        status, _body = agg._default_http_get("http://example.invalid/Items", {"Accept": "application/json"}, 5)
    assert status == 200
    assert seen["ua"] == "Aetrix/1.0"
