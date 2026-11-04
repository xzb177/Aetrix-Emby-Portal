"""P6 恒失败源配置级 fail-fast：Frodo 未配自备 key 时直接不尝试。

判据（只认凭据是否缺失，不写死源名清单）：
- env ``DOUBAN_FRODO_APIKEY`` 未设、空串、或显式填回内置默认值，
  一律视同未配——内置公开 key 已被豆瓣拒，任何请求恒 400。
- 未配时：不发请求、不喂熔断、不占限速（门放在熔断检查之前）。
- 配上自备 key 后：恢复尝试；此时失败仍照常喂熔断（兜底保留）。

覆盖点：判据四态、单点门（_request_json）、公开函数（search_by_title）、
熔断兜底保留。HTTP 一律 mock，不出网；熔断器 mock，不污染进程内状态。
"""
import json
import os
from unittest import mock

import pytest

from backend.emby_server.metasources import douban_frodo as frodo

ENV_KEY = "DOUBAN_FRODO_APIKEY"
DEFAULT_KEY = frodo._DEFAULT_APIKEY


@pytest.fixture()
def _clean_env():
    with mock.patch.dict(os.environ, {}, clear=False):
        os.environ.pop(ENV_KEY, None)
        os.environ["DOUBAN_FRODO_MIN_INTERVAL"] = "0"
        yield


def _patch_breaker():
    return (
        mock.patch.object(frodo.provider_breaker, "allow", return_value=True),
        mock.patch.object(frodo.provider_breaker, "record_failure"),
        mock.patch.object(frodo.provider_breaker, "record_success"),
    )


def _fake_ok(payload):
    resp = mock.MagicMock()
    resp.read.return_value = json.dumps(payload).encode("utf-8")
    resp.__enter__.return_value = resp
    resp.__exit__.return_value = False
    return resp


# ---------- 判据 ----------

def test_has_custom_apikey_four_states(_clean_env):
    assert frodo._has_custom_apikey() is False  # 未设
    with mock.patch.dict(os.environ, {ENV_KEY: ""}):
        assert frodo._has_custom_apikey() is False  # 空串
    with mock.patch.dict(os.environ, {ENV_KEY: DEFAULT_KEY}):
        assert frodo._has_custom_apikey() is False  # 填回内置死值 = 没填
    with mock.patch.dict(os.environ, {ENV_KEY: "  my-own-key  "}):
        assert frodo._has_custom_apikey() is True  # 自备 key（去空白后）


# ---------- 单点门 ----------

def test_request_skipped_without_key_no_http_no_breaker(_clean_env):
    allow_m, fail_m, ok_m = _patch_breaker()
    with allow_m as allow, fail_m as fail, ok_m as ok, \
            mock.patch("urllib.request.urlopen",
                       side_effect=AssertionError("must not fire")):
        assert frodo._request_json("https://frodo.douban.com/api/v2/x") is None
    allow.assert_not_called()  # 门在熔断检查之前
    fail.assert_not_called()
    ok.assert_not_called()


def test_request_attempted_with_custom_key(_clean_env):
    allow_m, fail_m, ok_m = _patch_breaker()
    with mock.patch.dict(os.environ, {ENV_KEY: "my-own-key"}), \
            allow_m, fail_m, ok_m as ok, \
            mock.patch("urllib.request.urlopen",
                       return_value=_fake_ok({"id": "1"})) as fired:
        assert frodo._request_json(
            "https://frodo.douban.com/api/v2/movie/1?apikey=my-own-key"
        ) == {"id": "1"}
    fired.assert_called_once()
    ok.assert_called_once()


def test_search_by_title_skipped_without_key(_clean_env):
    allow_m, fail_m, ok_m = _patch_breaker()
    with allow_m, fail_m, ok_m, \
            mock.patch("urllib.request.urlopen",
                       side_effect=AssertionError("must not fire")):
        assert frodo.search_by_title("测试剧", 2020, "tv") is None


def test_breaker_still_backstop_with_custom_key(_clean_env):
    import urllib.error
    allow_m, fail_m, ok_m = _patch_breaker()
    err = urllib.error.HTTPError("https://x", 400, "Bad Request", {}, None)
    with mock.patch.dict(os.environ, {ENV_KEY: "my-own-key"}), \
            allow_m, fail_m as fail, ok_m, \
            mock.patch("urllib.request.urlopen", side_effect=err):
        assert frodo._request_json("https://frodo.douban.com/api/v2/x") is None
    fail.assert_called()  # 已配 key 的真失败仍喂熔断（兜底保留）
