"""R4 轮回归测试：限流 IP 提取的两项修复。

R4-1：可信代理下 X-Forwarded-For 最后一段非法（如 "unknown"）时，
     必须回落到直连 IP，不能被当作限流键/日志 IP。
R4-2：_trusted_proxies() 解析缓存——同配置下不重复解析，
     环境变量一变立即失效（monkeypatch 逐测改值必须生效）。
"""
from unittest.mock import MagicMock

import pytest

import backend.ratelimit as ratelimit
from backend.ratelimit import get_client_ip, _is_trusted_proxy, _trusted_proxies


def _make_request(direct_ip, xff=None, x_real_ip=None):
    req = MagicMock()
    req.client.host = direct_ip
    headers = {}
    if xff:
        headers["x-forwarded-for"] = xff
    if x_real_ip:
        headers["x-real-ip"] = x_real_ip
    req.headers.get = lambda k, default="": headers.get(k.lower(), default)
    req.headers.__getitem__ = lambda self, k: headers[k.lower()]
    return req


@pytest.fixture(autouse=True)
def _clear_cache():
    """每测前清空模块级解析缓存，避免用例间串扰。"""
    ratelimit._trusted_proxies_cache = None
    yield
    ratelimit._trusted_proxies_cache = None


class TestXffLastHopValidation:
    """R4-1：XFF 最后一段合法性校验"""

    def test_unknown_last_hop_falls_back_to_direct(self):
        """某些代理会写 XFF: <ip>, unknown → 回落到直连 IP"""
        req = _make_request("127.0.0.1", xff="203.0.113.5, unknown")
        assert get_client_ip(req) == "127.0.0.1"

    def test_garbage_last_hop_falls_back_to_direct(self):
        """最后一段是垃圾字符串 → 回落到直连 IP"""
        req = _make_request("127.0.0.1", xff="1.2.3.4, not-an-ip!!")
        assert get_client_ip(req) == "127.0.0.1"

    def test_valid_last_hop_still_used(self):
        """合法最后一段行为不变"""
        req = _make_request("127.0.0.1", xff="1.2.3.4, 5.6.7.8")
        assert get_client_ip(req) == "5.6.7.8"

    def test_valid_last_hop_ipv6(self):
        """合法 IPv6 最后一段也被采信"""
        req = _make_request("127.0.0.1", xff="1.2.3.4, 2001:db8::1")
        assert get_client_ip(req) == "2001:db8::1"

    def test_x_real_ip_still_priority(self):
        """X-Real-IP 合法时仍优先，不受 XFF 校验影响"""
        req = _make_request(
            "127.0.0.1", xff="1.2.3.4, unknown", x_real_ip="5.6.7.8"
        )
        assert get_client_ip(req) == "5.6.7.8"

    def test_invalid_x_real_ip_and_invalid_xff(self):
        """X-Real-IP 非法 + XFF 末段非法 → 回落直连"""
        req = _make_request(
            "127.0.0.1", xff="garbage", x_real_ip="also-garbage"
        )
        assert get_client_ip(req) == "127.0.0.1"


class TestTrustedProxiesCache:
    """R4-2：解析缓存只在环境变量变化时失效"""

    def test_cache_hit_on_same_env(self, monkeypatch):
        monkeypatch.setenv("TRUSTED_PROXIES", "10.0.0.5")
        first = _trusted_proxies()
        assert ratelimit._trusted_proxies_cache is not None
        second = _trusted_proxies()
        assert first == second
        # 同一个 raw_env 下缓存对象被复用（返回的是拷贝，内容相等）
        assert ratelimit._trusted_proxies_cache[0] == "10.0.0.5"

    def test_env_change_invalidates_cache(self, monkeypatch):
        monkeypatch.setenv("TRUSTED_PROXIES", "10.0.0.5")
        assert _is_trusted_proxy("10.0.0.5") is True
        monkeypatch.setenv("TRUSTED_PROXIES", "10.0.0.6")
        # 缓存必须失效：旧值不再可信，新值可信
        assert _is_trusted_proxy("10.0.0.5") is False
        assert _is_trusted_proxy("10.0.0.6") is True

    def test_delenv_falls_back_to_loopback(self, monkeypatch):
        monkeypatch.setenv("TRUSTED_PROXIES", "10.0.0.5")
        assert _is_trusted_proxy("10.0.0.5") is True
        monkeypatch.delenv("TRUSTED_PROXIES", raising=False)
        assert _is_trusted_proxy("10.0.0.5") is False
        assert _is_trusted_proxy("127.0.0.1") is True

    def test_returned_list_is_copy(self, monkeypatch):
        """返回的 list 是拷贝：调用方改动不污染缓存"""
        monkeypatch.setenv("TRUSTED_PROXIES", "10.0.0.5")
        lst = _trusted_proxies()
        lst.clear()
        assert _is_trusted_proxy("10.0.0.5") is True

    def test_invalid_entries_still_ignored_with_cache(self, monkeypatch):
        monkeypatch.setenv("TRUSTED_PROXIES", "not-an-ip, 10.0.0.5")
        assert _is_trusted_proxy("10.0.0.5") is True
        assert _is_trusted_proxy("not-an-ip") is False
