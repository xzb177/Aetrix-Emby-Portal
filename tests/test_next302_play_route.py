"""302 Next E3：BYOS 选路优先（纯函数）+ 与 #1405 的交叉一致测试。

不变量：绑定用户无副本时，选路函数连「播共享原件」这个选项都不
提供；且 #1405 的 quota_playback_decision 对绑定用户 failover 恒 False。
两处口径必须同向，任一处放宽都是回归。
"""

import pytest

from backend.emby_server import next302_play_route as route
from backend.emby_server import transfer_variant as tv


def test_unbound_is_bind_guide_only():
    out = route.decide_route(bound=False, has_user_ready_copy=True)
    assert out["route"] == "bind_guide"
    assert out["shared"] is False


def test_bound_ready_copy_first():
    out = route.decide_route(bound=True, has_user_ready_copy=True)
    assert out["route"] == "user_copy"
    assert out["shared"] is False


@pytest.mark.parametrize("kwargs,expected", [
    ({"user_job_status": "pending"}, "preparing"),
    ({"user_job_status": "running"}, "preparing"),
    ({"user_job_status": "failed"}, "failed"),
    ({"quota_hit": True}, "quota_guide"),
    ({}, "no_copy_guide"),
])
def test_bound_without_copy_never_shared(kwargs, expected):
    out = route.decide_route(bound=True, has_user_ready_copy=False, **kwargs)
    assert out["route"] == expected
    assert out["shared"] is False


def test_stale_copy_is_not_playable():
    out = route.decide_route(bound=True, has_user_ready_copy=False,
                             user_copy_stale=True)
    assert out["route"] == "stale_copy"
    assert out["shared"] is False


def test_ready_copy_beats_stale_sibling():
    """同条目既有可播副本又有失效行：可播的赢，不挡播放。"""
    out = route.decide_route(bound=True, has_user_ready_copy=True,
                             user_copy_stale=True)
    assert out["route"] == "user_copy"


def test_pending_job_beats_stale_copy():
    """失效旧副本 + 新转存在途：在途的赢，报转存中而不是失效。"""
    out = route.decide_route(bound=True, has_user_ready_copy=False,
                             user_copy_stale=True,
                             user_job_status="running")
    assert out["route"] == "preparing"


@pytest.mark.parametrize("signal", [
    {},
    {"action": "queued"},
    {"action": "throttled"},
    {"action": "error"},
    {"reason": "auto_off"},
    {"reason": "has_ready_variant"},
    {"reason": "master_off"},
    {"reason": "job_within_24h"},
])
def test_cross_check_with_1405_quota_decision(signal):
    """与 #1405 交叉：绑定用户在任何 quota 信号下都不回落共享原件。"""
    verdict = tv.quota_playback_decision(signal, bound=True)
    assert verdict["failover"] is False
    assert verdict["status"] in (409, 503)
    # 本单选路同样：绑定用户无副本时不给共享路由。
    out = route.decide_route(bound=True, has_user_ready_copy=False)
    assert out["shared"] is False
    assert out["route"] != "user_copy"


def test_unbound_quota_decision_unchanged():
    """未绑定用户仍走旧回落（只可能是已下发签名）——本单不改这条。"""
    verdict = tv.quota_playback_decision({}, bound=False)
    assert verdict["failover"] is True
