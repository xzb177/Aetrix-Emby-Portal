"""签到增强（货币系统再设计一期）：连签里程碑 / 全勤奖 / 签到暴击。

口径（设计稿《货币系统再设计-20261101》第四节.1，用户 2026-11-01 批准）：
- 里程碑：连签达到 7/14/21/28 天各 +15（键可配），每轮连签只发一次，断签重置后可再得；
- 全勤奖：自然月（Asia/Shanghai）签到 ≥28 天 +30（键可配），按月幂等只发一次；
- 暴击：5% 概率当日签到所得 ×3、封顶 24（键可配），只作用于当日基础奖励
  （基础+连签加成−惩罚），不放大里程碑/全勤；签到返回带 crit 标记。
三项奖励都是签到行为入账，积分来源分类登记为自挣（points.POINT_TYPE_SELF_EARNED）。
"""
import os
import sys
from datetime import datetime, timedelta

os.environ.setdefault("DATABASE_TYPE", "sqlite")
os.environ.setdefault("REDIS_ENABLED", "false")
sys.path.insert(0, ".")

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend import models
from backend.api import economy


@pytest.fixture()
def db():
    engine = create_engine("sqlite://")
    models.Base.metadata.create_all(engine)
    s = sessionmaker(bind=engine)()
    yield s
    s.close()


BASE_CFG = {
    "checkin_enabled": "true",
    "checkin_base_min": "3",
    "checkin_base_max": "3",
    "checkin_streak_bonus": "1",
    "checkin_streak_max_bonus": "5",
    "checkin_penalty_pct": "0",
    # 默认关暴击，保证非暴击用例确定性；暴击用例单独开 100
    "checkin_crit_pct": "0",
}


def _seed_cfg(db, extra=None):
    cfg = dict(BASE_CFG)
    if extra:
        cfg.update(extra)
    for k, v in cfg.items():
        db.add(models.SystemConfig(key=k, value=str(v)))
    db.commit()


def _user(db, name="u1", welfare=True):
    u = models.WebUser(username=name, password_hash="x",
                       is_welfare=welfare, points=0, vitality=14)
    db.add(u)
    db.commit()
    return u


def _seed_streak(db, user, end_offset_days, streaks):
    """在 today-end_offset_days 起往前回填一轮连签。

    streaks 为该轮每天的 streak 值列表（升序），最后一项对应
    today-end_offset_days 那天。end_offset_days=1 表示昨天刚签过。
    """
    today = economy._today_start()
    n = len(streaks)
    for i, st in enumerate(streaks):
        day = today - timedelta(days=end_offset_days + (n - 1 - i))
        db.add(models.CheckinRecord(user_id=user.id, checkin_date=day,
                                     points_awarded=0, streak=st))
    db.commit()


def _logs(db, user, type_):
    return db.query(models.PointsLog).filter(
        models.PointsLog.user_id == user.id,
        models.PointsLog.type == type_).all()


def _balance(db, user):
    db.expire_all()
    return db.get(models.WebUser, user.id).points


# ==================== 规则默认值 ====================

def test_rules_defaults(db):
    _seed_cfg(db)  # BASE_CFG 显式关了暴击（crit_pct=0），其余新键走缺省
    rules = economy._checkin_rules(db)
    assert rules["milestone_points"] == 15
    assert rules["milestone_days"] == [7, 14, 21, 28]
    assert rules["full_attendance_points"] == 30
    assert rules["full_attendance_min_days"] == 28
    assert rules["crit_pct"] == 0
    assert rules["crit_cap"] == 24


def test_rules_defaults_without_any_config(db):
    rules = economy._checkin_rules(db)
    assert rules["milestone_points"] == 15
    assert rules["milestone_days"] == [7, 14, 21, 28]
    assert rules["full_attendance_points"] == 30
    assert rules["full_attendance_min_days"] == 28
    assert rules["crit_pct"] == 5
    assert rules["crit_cap"] == 24


# ==================== 连签里程碑 ====================

def test_milestone_granted_at_day7(db):
    _seed_cfg(db)
    u = _user(db)
    _seed_streak(db, u, 1, [1, 2, 3, 4, 5, 6])
    res = economy._do_checkin_core(db, u)
    assert res["streak"] == 7
    # 当日基础奖励 3 + 连签加成 min(1*6, 5) = 8；里程碑另发 15
    assert res["points_awarded"] == 8
    assert res["milestone_points"] == 15
    assert _balance(db, u) == 23
    logs = _logs(db, u, "checkin_milestone")
    assert len(logs) == 1 and logs[0].amount == 15


def test_milestone_not_repeated_on_day8(db):
    _seed_cfg(db)
    u = _user(db)
    _seed_streak(db, u, 1, [1, 2, 3, 4, 5, 6, 7])
    res = economy._do_checkin_core(db, u)
    assert res["streak"] == 8
    assert res["milestone_points"] == 0
    assert _logs(db, u, "checkin_milestone") == []


def test_milestone_each_milestone_day_in_one_run(db):
    _seed_cfg(db)
    u = _user(db)
    _seed_streak(db, u, 1, list(range(1, 14)))  # 昨天 streak=13
    res = economy._do_checkin_core(db, u)
    assert res["streak"] == 14
    assert res["milestone_points"] == 15
    assert len(_logs(db, u, "checkin_milestone")) == 1


def test_milestone_break_resets_streak(db):
    _seed_cfg(db)
    u = _user(db)
    _seed_streak(db, u, 2, [18, 19, 20])  # 前天是最后一签，昨天断了
    res = economy._do_checkin_core(db, u)
    assert res["streak"] == 1
    assert res["milestone_points"] == 0


def test_milestone_granted_again_after_break_and_rebuild(db):
    _seed_cfg(db)
    u = _user(db)
    # 旧一轮：7 天前结束（streak 到 7），中间断签；新一轮：昨天 streak=6
    _seed_streak(db, u, 8, [1, 2, 3, 4, 5, 6, 7])
    _seed_streak(db, u, 1, [1, 2, 3, 4, 5, 6])
    res = economy._do_checkin_core(db, u)
    assert res["streak"] == 7
    assert res["milestone_points"] == 15
    assert len(_logs(db, u, "checkin_milestone")) == 1


def test_milestone_disabled_when_points_zero(db):
    _seed_cfg(db, {"checkin_milestone_points": "0"})
    u = _user(db)
    _seed_streak(db, u, 1, [1, 2, 3, 4, 5, 6])
    res = economy._do_checkin_core(db, u)
    assert res["streak"] == 7
    assert res["milestone_points"] == 0
    assert _balance(db, u) == 8


def test_milestone_custom_days(db):
    _seed_cfg(db, {"checkin_milestone_days": "5"})
    u = _user(db)
    _seed_streak(db, u, 1, [1, 2, 3, 4])
    res = economy._do_checkin_core(db, u)
    assert res["streak"] == 5
    assert res["milestone_points"] == 15


def test_milestone_not_for_non_welfare(db):
    _seed_cfg(db)
    u = _user(db, welfare=False)
    _seed_streak(db, u, 1, [1, 2, 3, 4, 5, 6])
    res = economy._do_checkin_core(db, u)
    assert res["streak"] == 7
    assert res["points_awarded"] == 0
    assert res["milestone_points"] == 0
    assert _logs(db, u, "checkin_milestone") == []


# ==================== 签到暴击 ====================

def test_crit_triples_daily_reward(db):
    _seed_cfg(db, {"checkin_crit_pct": "100"})
    u = _user(db)
    _seed_streak(db, u, 1, [1, 2, 3, 4, 5, 6])
    res = economy._do_checkin_core(db, u)
    # 基础奖励 8，暴击 ×3 = 24（恰为封顶）
    assert res["crit"] is True
    assert res["points_awarded"] == 24
    assert _balance(db, u) == 24 + 15  # 里程碑不被暴击放大，另发 15
    rec = db.query(models.CheckinRecord).filter_by(user_id=u.id).order_by(
        models.CheckinRecord.checkin_date.desc()).first()
    assert rec.points_awarded == 24


def test_crit_capped(db):
    _seed_cfg(db, {"checkin_crit_pct": "100",
                   "checkin_base_min": "10", "checkin_base_max": "10"})
    u = _user(db)
    _seed_streak(db, u, 1, [1])  # streak=2 → 奖励 10+1=11 → ×3=33 → 封顶 24
    res = economy._do_checkin_core(db, u)
    assert res["crit"] is True
    assert res["points_awarded"] == 24


def test_crit_never_when_pct_zero(db):
    _seed_cfg(db, {"checkin_crit_pct": "0"})
    u = _user(db)
    _seed_streak(db, u, 1, [1, 2, 3, 4, 5, 6])
    res = economy._do_checkin_core(db, u)
    assert res["crit"] is False
    assert res["points_awarded"] == 8


def test_crit_not_applied_to_zero_reward(db):
    _seed_cfg(db, {"checkin_crit_pct": "100"})
    u = _user(db, welfare=False)
    res = economy._do_checkin_core(db, u)
    assert res["crit"] is False
    assert res["points_awarded"] == 0


# ==================== 全勤奖 ====================

FAKE_TODAY = datetime(2026, 11, 27, 16, 0, 0)  # 上海 2026-11-28 00:00 的 UTC naive


def _seed_month(db, user, year, month, days, streak_start=1):
    """回填某上海自然月内 days（日号列表）的签到记录（UTC naive 帧）。"""
    base = datetime(year, month, 1) - timedelta(hours=8)
    for i, d in enumerate(days):
        db.add(models.CheckinRecord(
            user_id=user.id,
            checkin_date=base + timedelta(days=d - 1),
            points_awarded=0, streak=streak_start + i))
    db.commit()


def test_full_attendance_granted_and_idempotent(db, monkeypatch):
    _seed_cfg(db)
    u = _user(db)
    _seed_month(db, u, 2026, 11, list(range(1, 28)))  # 11月 1..27 日，streak 1..27
    monkeypatch.setattr(economy, "_today_start", lambda: FAKE_TODAY)
    res = economy._do_checkin_core(db, u)
    assert res["streak"] == 28
    assert res["full_attendance_points"] == 30
    logs = _logs(db, u, "checkin_full")
    assert len(logs) == 1 and logs[0].amount == 30
    # 次日再签：月内天数 29 ≥ 28，但按月幂等、不重发
    monkeypatch.setattr(economy, "_today_start",
                        lambda: FAKE_TODAY + timedelta(days=1))
    res2 = economy._do_checkin_core(db, u)
    assert res2["full_attendance_points"] == 0
    assert len(_logs(db, u, "checkin_full")) == 1


def test_full_attendance_counts_only_current_month(db, monkeypatch):
    _seed_cfg(db, {"checkin_full_attendance_min_days": "5"})
    u = _user(db)
    _seed_month(db, u, 2026, 10, [27, 28, 29, 30, 31])  # 10 月的 5 天不算
    _seed_month(db, u, 2026, 11, [25, 26, 27])          # 11 月仅 3 天（+今日=4 < 5）
    monkeypatch.setattr(economy, "_today_start", lambda: FAKE_TODAY)
    res = economy._do_checkin_core(db, u)
    assert res["full_attendance_points"] == 0
    assert _logs(db, u, "checkin_full") == []


def test_full_attendance_disabled(db, monkeypatch):
    _seed_cfg(db, {"checkin_full_attendance_points": "0"})
    u = _user(db)
    _seed_month(db, u, 2026, 11, list(range(1, 28)))
    monkeypatch.setattr(economy, "_today_start", lambda: FAKE_TODAY)
    res = economy._do_checkin_core(db, u)
    assert res["full_attendance_points"] == 0
    assert _logs(db, u, "checkin_full") == []


# ==================== 管理端配置 ====================

def test_admin_new_keys_saveable_and_read_back(db):
    from backend.api import admin_economy
    admin = models.WebUser(username="admin", password_hash="x", is_staff=True)
    db.add(admin)
    db.commit()
    res = admin_economy.economy_update_settings(
        admin_economy.EconomySettingsRequest(settings={
            "checkin_milestone_points": "20",
            "checkin_milestone_days": "7, 21",
            "checkin_full_attendance_points": "50",
            "checkin_full_attendance_min_days": "26",
            "checkin_crit_pct": "10",
            "checkin_crit_cap": "30",
        }), current_admin=admin, db=db)
    assert set(res["changed"]) >= {
        "checkin_milestone_points", "checkin_milestone_days",
        "checkin_full_attendance_points", "checkin_full_attendance_min_days",
        "checkin_crit_pct", "checkin_crit_cap"}
    got = admin_economy.economy_get_settings(current_admin=admin, db=db)["settings"]
    assert got["checkin_milestone_days"] == "7,21"  # int_list 归一化落库
    rules = economy._checkin_rules(db)
    assert rules["milestone_points"] == 20
    assert rules["milestone_days"] == [7, 21]
    assert rules["full_attendance_points"] == 50
    assert rules["full_attendance_min_days"] == 26
    assert rules["crit_pct"] == 10
    assert rules["crit_cap"] == 30


@pytest.mark.parametrize("bad", [
    {"checkin_crit_pct": "150"},
    {"checkin_crit_pct": "-1"},
    {"checkin_milestone_points": "-5"},
    {"checkin_full_attendance_min_days": "400"},
    {"checkin_milestone_days": "7,abc"},
    {"checkin_milestone_days": "0,7"},
])
def test_admin_new_keys_validation(db, bad):
    from fastapi import HTTPException
    from backend.api import admin_economy
    from backend.config_schema import ConfigValueError
    admin = models.WebUser(username="admin", password_hash="x", is_staff=True)
    db.add(admin)
    db.commit()
    with pytest.raises((HTTPException, ConfigValueError)):
        admin_economy.economy_update_settings(
            admin_economy.EconomySettingsRequest(settings=bad),
            current_admin=admin, db=db)
    assert db.query(models.SystemConfig).filter(
        models.SystemConfig.key.in_(list(bad))).count() == 0


# ==================== 来源分类登记 ====================

def test_new_point_types_registered_self_earned():
    from backend import points as points_mod
    assert points_mod.POINT_TYPE_SELF_EARNED["checkin_milestone"] is True
    assert points_mod.POINT_TYPE_SELF_EARNED["checkin_full"] is True
