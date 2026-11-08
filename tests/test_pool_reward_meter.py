# -*- coding: utf-8 -*-
"""公共贡献池 PR-6（goal-33）：激励计量纯函数 pool_reward.meter_member。

口径（design §11.2，写死在聚合纯函数里，全站一个口径）：
- 本人不计：viewer_user_id ＝成员 user_id 的起播 excluded_self
  （同一人名下多盘互看也按 user_id 判——每个成员行都以其
  user_id 做比对，故多盘互看天然同判）；
- 重复去重：同观众同副本在去重窗口内只计首次（窗口锚在上一次
  counted 上，间隔 < 窗口排除、≥ 窗口计）；
- 单日冻结线：单成员单日 counted 达线后当日余量 excluded_over_cap，
  且整期标 frozen（待人工审、不是没收）；
- 有效在线日：active 区间与北京时间自然日求交，单日 ≥12 小时计 1。

时间帧：入库时间列是 UTC naive（生产容器 UTC），日界按
Asia/Shanghai 换算——2026-11 的周期界（UTC）为
[2026-10-31 16:00, 2026-11-30 16:00)。
"""
from __future__ import annotations

import os

os.environ.setdefault("DATABASE_TYPE", "sqlite")
os.environ.setdefault("REDIS_ENABLED", "false")

from datetime import datetime, timedelta
from types import SimpleNamespace

from backend.emby_server import pool_reward

P_START = datetime(2026, 10, 31, 16, 0)   # 2026-11-01 00:00 上海
P_END = datetime(2026, 11, 30, 16, 0)     # 2026-12-01 00:00 上海
CFG = {
    "pool_reward_play_dedupe_window_sec": 1800,
    "pool_reward_daily_play_freeze": 1500,
}


def _ev(eid, viewer, variant, ts):
    return SimpleNamespace(
        id=eid, viewer_user_id=viewer, variant_id=variant,
        created_at=ts)


def _meter(events, intervals=(), user_id=7, cfg=None, start=P_START,
           end=P_END):
    return pool_reward.meter_member(
        events, member_user_id=user_id,
        status_intervals=list(intervals),
        period_start=start, period_end=end,
        cfg=dict(CFG if cfg is None else cfg))


def test_period_bounds_monthly_shanghai_frame():
    start, end = pool_reward.period_bounds_utc("monthly", "2026-11")
    assert (start, end) == (P_START, P_END)
    assert pool_reward.period_key_for(
        "monthly", datetime(2026, 11, 15).date()) == "2026-11"
    assert pool_reward.previous_period_key(
        "monthly", datetime(2026, 12, 3)) == "2026-11"


def test_period_bounds_weekly_monday_start():
    # 2026-11-02 是周一；W45 = 11-02（周一）至 11-09（周一）上海时。
    start, end = pool_reward.period_bounds_utc("weekly", "2026-W45")
    assert start == datetime(2026, 11, 1, 16, 0)
    assert end == datetime(2026, 11, 8, 16, 0)
    assert pool_reward.period_key_for(
        "weekly", datetime(2026, 11, 4).date()) == "2026-W45"


def test_self_play_excluded():
    t = datetime(2026, 11, 5, 2, 0)
    res = _meter([_ev(1, 7, 100, t), _ev(2, 8, 100, t)])
    assert res.raw_plays == 2
    assert res.counted_plays == 1
    assert res.verdicts[1] == "excluded_self"
    assert res.verdicts[2] == "counted"


def test_dedupe_window_boundary():
    t0 = datetime(2026, 11, 5, 2, 0)
    res = _meter([
        _ev(1, 8, 100, t0),
        _ev(2, 8, 100, t0 + timedelta(seconds=1799)),
        _ev(3, 8, 100, t0 + timedelta(seconds=1800)),
        _ev(4, 9, 100, t0 + timedelta(seconds=10)),
        _ev(5, 8, 101, t0 + timedelta(seconds=10)),
    ])
    assert res.verdicts[1] == "counted"
    assert res.verdicts[2] == "excluded_dedupe"
    # 窗口锚在上一次 counted（t0）：t0+1800 已出窗 → 计。
    assert res.verdicts[3] == "counted"
    # 换观众 / 换副本都不受去重影响。
    assert res.verdicts[4] == "counted"
    assert res.verdicts[5] == "counted"
    assert res.counted_plays == 4


def test_daily_freeze_caps_and_flags():
    cfg = dict(CFG, pool_reward_daily_play_freeze=3)
    t = datetime(2026, 11, 5, 2, 0)
    events = [_ev(i, 100 + i, 100, t + timedelta(minutes=i))
              for i in range(1, 6)]
    res = _meter(events, cfg=cfg)
    assert res.counted_plays == 3
    assert res.frozen is True
    assert res.verdicts[4] == "excluded_over_cap"
    assert res.verdicts[5] == "excluded_over_cap"
    # 次日（上海时）额度恢复，不再超限。
    t2 = datetime(2026, 11, 6, 2, 0)
    res2 = _meter([_ev(9, 100, 100, t2)], cfg=cfg)
    assert res2.counted_plays == 1
    assert res2.frozen is False


def test_freeze_zero_means_no_freeze():
    cfg = dict(CFG, pool_reward_daily_play_freeze=0)
    t = datetime(2026, 11, 5, 2, 0)
    events = [_ev(i, 100 + i, 100, t + timedelta(minutes=i))
              for i in range(1, 8)]
    res = _meter(events, cfg=cfg)
    assert res.counted_plays == 7
    assert res.frozen is False


def test_active_days_full_period_and_partial():
    active_all = [(P_START, P_END, "active")]
    res = _meter([], intervals=active_all)
    assert res.active_days == 30  # 2026-11 共 30 个上海自然日

    # 上海 11-03 当天 active 恰 12 小时（00:00–12:00 上海
    # = 11-02 16:00 – 11-03 04:00 UTC）→ 计 1 天。
    exact12 = [(datetime(2026, 11, 2, 16, 0),
                datetime(2026, 11, 3, 4, 0), "active")]
    assert _meter([], intervals=exact12).active_days == 1

    # 差 1 秒不足 12 小时 → 不计。
    under12 = [(datetime(2026, 11, 2, 16, 0),
                datetime(2026, 11, 3, 3, 59, 59), "active")]
    assert _meter([], intervals=under12).active_days == 0


def test_active_days_split_across_suspension():
    # 上海 11-03：active 00:00–06:00、suspended 06:00–08:00、
    # active 08:00–14:00 → 当日 active 合计 12 小时 → 计 1 天。
    intervals = [
        (datetime(2026, 11, 2, 16, 0), datetime(2026, 11, 2, 22, 0),
         "active"),
        (datetime(2026, 11, 2, 22, 0), datetime(2026, 11, 3, 0, 0),
         "suspended"),
        (datetime(2026, 11, 3, 0, 0), datetime(2026, 11, 3, 6, 0),
         "active"),
    ]
    assert _meter([], intervals=intervals).active_days == 1


def test_build_status_intervals_from_events():
    raw = [
        SimpleNamespace(from_status=None, to_status="pending",
                        created_at=datetime(2026, 10, 30, 10, 0)),
        SimpleNamespace(from_status="pending", to_status="active",
                        created_at=datetime(2026, 11, 2, 16, 0)),
        SimpleNamespace(from_status="active", to_status="suspended",
                        created_at=datetime(2026, 11, 10, 16, 0)),
    ]
    intervals = pool_reward.build_status_intervals(
        raw, member_status="suspended",
        period_start=P_START, period_end=P_END)
    assert intervals[0] == (P_START, datetime(2026, 11, 2, 16, 0),
                            "pending")
    assert intervals[1] == (datetime(2026, 11, 2, 16, 0),
                            datetime(2026, 11, 10, 16, 0), "active")
    assert intervals[2] == (datetime(2026, 11, 10, 16, 0), P_END,
                            "suspended")
    # active 区间 = 上海 11-03 00:00 至 11-11 00:00 → 8 天整。
    res = _meter([], intervals=intervals)
    assert res.active_days == 8
