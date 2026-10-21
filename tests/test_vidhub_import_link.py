"""VidHub 播放器一键导入 deep link 生成规则测试。

格式由用户 2026-10-15 提供：vidhub://import?type=emby&scheme=&host=&port=
&username=&password=，与 Rex 同款参数口径。host/port 从服务器地址解析，
拼错了用户点按钮只会「打不开 App」，属于用户不会报障的那类坑。
"""
import os

os.environ.setdefault("DATABASE_TYPE", "sqlite")
os.environ.setdefault("REDIS_ENABLED", "false")

from backend.emby_server.portal import _vidhub_import_link, VIDHUB_IMPORT_URL


def test_uses_vidhub_scheme():
    got = _vidhub_import_link("https://emby.example.com", "emby_1_a82c3e")
    assert got.startswith(VIDHUB_IMPORT_URL + "?type=emby&")
    assert VIDHUB_IMPORT_URL == "vidhub://import", got


def test_https_without_port_defaults_to_443():
    got = _vidhub_import_link("https://emby.example.com", "emby_1_a82c3e")
    assert got == (
        "vidhub://import?type=emby&scheme=https&host=emby.example.com"
        "&port=443&username=emby_1_a82c3e"
    ), got


def test_http_without_port_defaults_to_80():
    got = _vidhub_import_link("http://192.168.1.10", "emby_1_x")
    assert got == (
        "vidhub://import?type=emby&scheme=http&host=192.168.1.10"
        "&port=80&username=emby_1_x"
    ), got


def test_explicit_port_is_kept():
    # 用户给的示例形态：http + 显式 8096 端口
    got = _vidhub_import_link("http://192.168.254.111:8096", "abc")
    assert got == (
        "vidhub://import?type=emby&scheme=http&host=192.168.254.111"
        "&port=8096&username=abc"
    ), got


def test_username_is_url_encoded():
    got = _vidhub_import_link("https://emby.example.com", "user name+1")
    assert "&username=user%20name%2B1" in got, got


def test_password_appended_only_when_given():
    got = _vidhub_import_link("http://192.168.254.111:8096", "abc", "123456")
    assert got.endswith("&password=123456"), got
    got2 = _vidhub_import_link("http://192.168.254.111:8096", "abc")
    assert "password" not in got2, got2


def test_empty_url_does_not_crash():
    got = _vidhub_import_link("", "emby_1_x")
    assert got.startswith("vidhub://import?type=emby&"), got
