"""观影时长分档活力奖励：档位边界 + 每日上限钳制（与签到共用上限）。"""
import os
import sys
from datetime import datetime

os.environ.setdefault("DATABASE_TYPE", "sqlite")
os.environ.setdefault("REDIS_ENABLED", "false")
sys.path.insert(0, ".")

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend import models, vitality
from backend.integrations import store as _store


@pytest.fixture()
def db():
    engine = create_engine("sqlite://")
    models.Base.metadata.create_all(engine)
    s = sessionmaker(bind=engine)()
    # 配置有进程内 TTL 热缓存：别的测试关掉过的开关不能串进来
    _store.invalidate(
        "vitality_enabled", "vitality_daily_cost", "vitality_max",
        "vitality_daily_gain_limit",
        "vitality_watch_tier1_minutes", "vitality_watch_tier1_reward",
        "vitality_watch_tier2_minutes", "vitality_watch_tier2_reward",
        vitality.CONFIG_LAST_DEDUCT_DATE)
    yield s
    s.close()


def _user(db, name, welfare=True, vitality_v=0):
    u = models.WebUser(username=name, password_hash="x",
                       is_welfare=welfare, vitality=vitality_v)
    db.add(u)
    db.commit()
    return u


def _vitality_of(db, user_id):
    return db.query(models.WebUser.vitality).filter(
        models.WebUser.id == user_id).scalar()


def _gain_log(db, user_id, reason, delta):
    # 直接插一条当日正向流水（模拟已签到 / 已发观影奖励）
    db.add(models.VitalityLog(user_id=user_id, delta=delta,
                              balance_after=delta, reason=reason,
                              created_at=datetime.now()))
    db.commit()


def test_tier_boundaries(db):
    """档位边界：29→0、30→1、59→1、60→2、120→2（每例独立用户，互不占上限）。"""
    cases = [(29, 0), (30, 1), (59, 1), (60, 2), (120, 2)]
    for i, (minutes, expected) in enumerate(cases):
        u = _user(db, f"tier{i}")
        got = vitality.award_watch_minutes_reward(db, u.id, minutes)
        assert got == expected, (minutes, got)
        assert _vitality_of(db, u.id) == expected


def test_daily_cap_shared_with_checkin(db):
    """与签到共用每日上限 2：当天已签到 +1 时，一小时只能再得 +1。"""
    u = _user(db, "capped")
    _gain_log(db, u.id, "checkin", 1)
    assert vitality.award_watch_minutes_reward(db, u.id, 60) == 1
    assert _vitality_of(db, u.id) == 1


def test_daily_cap_reached_returns_zero(db):
    """已达上限（签到 +1 + 观影 +1）时，一小时再结算得 0。"""
    u = _user(db, "full")
    _gain_log(db, u.id, "checkin", 1)
    _gain_log(db, u.id, "watch_reward", 1)
    assert vitality.award_watch_minutes_reward(db, u.id, 60) == 0
    assert _vitality_of(db, u.id) == 0


def test_non_welfare_gets_zero(db):
    """非公益用户不参与活力值体系，一小时也得 0。"""
    u = _user(db, "plain", welfare=False)
    assert vitality.award_watch_minutes_reward(db, u.id, 60) == 0
    assert _vitality_of(db, u.id) == 0


def test_disabled_gets_zero(db):
    """功能关闭时不发，一小时也得 0。"""
    u = _user(db, "off")
    db.add(models.SystemConfig(key="vitality_enabled", value="false"))
    db.commit()
    _store.invalidate("vitality_enabled")
    assert vitality.award_watch_minutes_reward(db, u.id, 60) == 0
    assert _vitality_of(db, u.id) == 0


def test_tier_validation_rejects_inverted_tiers():
    """校验：二档分钟数小于一档分钟数时报错；默认档位合法。"""
    assert vitality.validate_vitality_config({}) == {}
    errors = vitality.validate_vitality_config({
        "vitality_watch_tier1_minutes": "60",
        "vitality_watch_tier2_minutes": "30",
    })
    assert "vitality_watch_tier2_minutes" in errors
    errors = vitality.validate_vitality_config({
        "vitality_watch_tier1_reward": "-1",
    })
    assert "vitality_watch_tier1_reward" in errors
