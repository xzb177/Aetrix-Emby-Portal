"""Rex 播放器一键导入 deep link 生成规则测试。

为什么值得测：`rex://import` 的 host/port 从服务器地址解析出来，
拼错了用户点按钮只会「打不开 App」，属于用户不会报障的那类坑。
端口缺省规则（https→443 / http→80）也最容易悄悄写错。
"""
import os

os.environ.setdefault("DATABASE_TYPE", "sqlite")
os.environ.setdefault("REDIS_ENABLED", "false")

from backend.emby_server.portal import _rex_import_link, REX_IMPORT_URL


def test_uses_rex_scheme():
    got = _rex_import_link("https://emby.example.com", "emby_1_a82c3e")
    assert got.startswith(REX_IMPORT_URL + "?type=emby&")
    assert REX_IMPORT_URL == "rex://import", got


def test_https_without_port_defaults_to_443():
    got = _rex_import_link("https://emby.example.com", "emby_1_a82c3e")
    assert got == (
        "rex://import?type=emby&scheme=https&host=emby.example.com"
        "&port=443&username=emby_1_a82c3e"
    ), got


def test_http_without_port_defaults_to_80():
    got = _rex_import_link("http://192.168.1.10", "emby_1_x")
    assert got == (
        "rex://import?type=emby&scheme=http&host=192.168.1.10"
        "&port=80&username=emby_1_x"
    ), got


def test_explicit_port_is_kept():
    got = _rex_import_link("http://192.168.1.10:8096", "emby_1_x")
    assert "&host=192.168.1.10&port=8096&" in got, got
    assert "192.168.1.10:8096" not in got.split("host=")[1].split("&")[0], got


def test_username_is_url_encoded():
    got = _rex_import_link("https://emby.example.com", "user name+1")
    assert "&username=user%20name%2B1" in got, got


def test_env_scheme_override(monkeypatch):
    monkeypatch.setenv("EMBY_URL_SCHEME", "https")
    got = _rex_import_link("http://192.168.1.10", "emby_1_x")
    assert got.startswith("rex://import?type=emby&scheme=https&"), got


def test_empty_url_does_not_crash():
    got = _rex_import_link("", "emby_1_x")
    assert got.startswith("rex://import?type=emby&"), got
