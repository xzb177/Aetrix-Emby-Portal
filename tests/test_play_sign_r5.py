"""R5 轮回归：verify_play_sign 对非 ASCII 签名的健壮性（R5-A1）。

背景：hmac.compare_digest(str, str) 在任一含非 ASCII 字符时抛 TypeError；
auth.get_play_user 把 URL query 的 sign 未经 try 直接传入 verify_play_sign，
32 个非 ASCII 字符可触发未认证 500。本测试锁定：任何非法 sign 输入一律返回
False，永不抛异常。
"""
import time

from backend.emby_server import play_sign


def _valid():
    exp, sign = play_sign.issue_play_sign(42, "item-guid-1")
    return 42, "item-guid-1", exp, sign


def test_non_ascii_32_chars_returns_false_not_raise():
    # 32 个中文字符：len==32 能通过长度检查，旧代码在此处抛 TypeError
    evil = "中" * 32
    assert len(evil) == 32
    assert play_sign.verify_play_sign(42, "item-guid-1", int(time.time()) + 60, evil) is False


def test_non_ascii_mixed_with_valid_exp():
    uid, item, exp, _sign = _valid()
    assert play_sign.verify_play_sign(uid, item, exp, "é" * 32) is False


def test_non_str_sign_returns_false():
    uid, item, exp, _sign = _valid()
    assert play_sign.verify_play_sign(uid, item, exp, None) is False
    assert play_sign.verify_play_sign(uid, item, exp, 12345) is False
    assert play_sign.verify_play_sign(uid, item, exp, b"a" * 32) is False


def test_valid_sign_still_passes():
    uid, item, exp, sign = _valid()
    assert sign.isascii() and len(sign) == 32
    assert play_sign.verify_play_sign(uid, item, exp, sign) is True


def test_tampered_and_expired_still_false():
    uid, item, exp, sign = _valid()
    assert play_sign.verify_play_sign(uid, item, exp, "0" * 32) is False
    assert play_sign.verify_play_sign(uid, item, int(time.time()) - 1, sign) is False
    assert play_sign.verify_play_sign(uid, "other-item", exp, sign) is False
