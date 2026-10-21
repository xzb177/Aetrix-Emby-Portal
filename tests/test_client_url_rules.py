"""用户端地址的兜底口径（v2.32.0）

真实起因：一台刚装好的测试服务器（还没配任何 Emby 入口地址）上，媒体库页第一眼就是一条红提示
——「用户端账号卡显示的地址是 http://localhost:8000：只有这台机器自己能连」。那个地址从来不是
配置，只是 ``resolve_emby_base_url`` 最后写死的占位值；而面板与接口同源，管理员**正在用的**
这个地址本来就是用户该连的地址。

所以这里钉住两件事：

- 解析优先级：服地址 / 服务入口配置 / 环境变量（都是人配的）→ **当前请求的地址** → 占位
  ``localhost``；
- 占位的 ``localhost`` 必须被标成 ``URL_SOURCE_NONE``（不是 ``config``）：面板据此知道
  「这地址没人配过」，于是不拿它当证据报红，改为按管理员当前访问地址显示
  （见 ``reachability.client_endpoint_check``）。

不碰数据库：本文件只验证这一层纯逻辑（配置读不到时的兜底顺序），接口与落库口径由
``scripts/smoke_test_reachability.py`` 端到端守着。
"""
import pytest
from starlette.requests import Request

from backend.emby_server import portal
from backend.emby_server.reachability import _is_local_only


class _EmptyDb:
    """空配置的会话替身：读任何配置都抛错

    ``_config_value`` 读配置失败时按「未配置」处理，所以这等价于「刚装好、还没写过任何设置」
    ——也就是测试机第一眼看到那条红提示的状态。真正的落库口径由冒烟测试（接口层）守着。
    """

    def query(self, *_args, **_kwargs):
        raise RuntimeError("空配置：这个会话读不到任何设置")


def _request(scheme: str = "http", host: str = "panel.example:8000") -> Request:
    """一个最小的请求对象（只有 scheme + Host 参与推断）"""
    scope = {
        "type": "http", "method": "GET", "path": "/", "raw_path": b"/", "query_string": b"",
        "headers": [(b"host", host.encode())], "scheme": scheme,
        "server": (host.split(":")[0], 8000), "client": ("testclient", 50000), "root_path": "",
    }
    return Request(scope)


# ==================== 优先级 ====================

def test_request_address_is_used_when_nothing_is_configured(monkeypatch):
    monkeypatch.delenv("EMBY_PUBLIC_URL", raising=False)
    url, source = portal.resolve_emby_base_url_with_source(_EmptyDb(), None, _request())
    assert url == "http://panel.example:8000"
    assert source == portal.URL_SOURCE_REQUEST


def test_env_wins_over_the_request_address(monkeypatch):
    """老部署靠 EMBY_PUBLIC_URL 固定对外地址：配过的一律优先"""
    monkeypatch.setenv("EMBY_PUBLIC_URL", "https://media.example.com/")
    url, source = portal.resolve_emby_base_url_with_source(_EmptyDb(), None, _request())
    assert url == "https://media.example.com"          # 去尾斜杠
    assert source == portal.URL_SOURCE_ENV


def test_placeholder_is_marked_as_not_configured(monkeypatch):
    """连请求都拿不到时才回退占位地址，且必须标成 none（面板据此不报红）"""
    monkeypatch.delenv("EMBY_PUBLIC_URL", raising=False)
    url, source = portal.resolve_emby_base_url_with_source(_EmptyDb(), None, None)
    assert url == "http://localhost:8000"
    assert source == portal.URL_SOURCE_NONE


def test_resolve_returns_just_the_url(monkeypatch):
    """老签名保持兼容：只返回地址字符串"""
    monkeypatch.delenv("EMBY_PUBLIC_URL", raising=False)
    assert portal.resolve_emby_base_url(_EmptyDb(), None, _request()) == "http://panel.example:8000"
    assert portal.resolve_emby_base_url(_EmptyDb(), None) == "http://localhost:8000"


# ==================== 请求地址怎么推出来 ====================

def test_request_base_url_keeps_scheme_and_host(monkeypatch):
    """没配 EMBY_PUBLIC_URL、对端也不可信时，仍用请求自己的 scheme + Host"""
    monkeypatch.delenv("EMBY_PUBLIC_URL", raising=False)
    monkeypatch.delenv("FORWARDED_ALLOW_IPS", raising=False)
    assert portal._request_base_url(_request("https", "media.example.com")) == \
        "https://media.example.com"


def test_request_base_url_refuses_non_http_and_missing_request(monkeypatch):
    """非 http(s) 的 scheme 与「没有请求」都算推断不出来（宁可回到占位，也不下发一个坏地址）"""
    monkeypatch.delenv("EMBY_PUBLIC_URL", raising=False)
    assert portal._request_base_url(_request(scheme="ftp")) == ""
    assert portal._request_base_url(None) == ""
    assert portal._request_base_url(object()) == ""


# ==================== 只有本机能连的地址怎么认 ====================

@pytest.mark.parametrize("url", [
    "http://localhost:8000",
    "http://localhost",
    "https://127.0.0.1:8920/emby",
    "http://0.0.0.0:8000",
    "http://[::1]:8000",       # IPv6 字面量带方括号：按 ':' 切会把它切成 '['
    "http://[::]:8000",
])
def test_local_only_addresses_are_recognized(url):
    assert _is_local_only(url) is True


@pytest.mark.parametrize("url", [
    "https://media.example.com",
    "http://192.0.2.10:8000",     # 内网/测试机的真实地址：客户端连得上，不该报「只有本机能连」
    "http://panel.example:8000",
    "",                            # 没配地址：由上面的 source 口径处理，不走这里
])
def test_real_addresses_are_not_treated_as_local_only(url):
    assert _is_local_only(url) is False
