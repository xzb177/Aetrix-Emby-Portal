"""报到页奖励框动态实数：GET /checkin/status 的 today_points 字段回归测试。

用户 2026-11-05 亲点：奖励框在规则三数之外，按连报天数动态显示
「今日实得 / 明日可得」。今日实得只能取今日 CheckinRecord.points_awarded
（当日基础奖励：基础+连签加成−惩罚、含暴击放大；里程碑/全勤是独立台账），
未报到必须为 null（前端据此显示「今日报到可得」预估，不许拿 0 冒充实得）。
"""
import os
import sys
from datetime import timedelta

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


def _seed_cfg(db, extra=None):
    """生产现行口径：base=3 固定、连签 +1/天、封顶 5；随机项全关，算术可复现。"""
    cfg = {
        "checkin_enabled": "true",
        "checkin_base_min": "3",
        "checkin_base_max": "3",
        "checkin_streak_bonus": "1",
        "checkin_streak_max_bonus": "5",
        "checkin_penalty_pct": "0",
        "checkin_crit_pct": "0",
        "checkin_milestone_points": "0",
        "checkin_full_attendance_points": "0",
    }
    if extra:
        cfg.update(extra)
    for k, v in cfg.items():
        db.add(models.SystemConfig(key=k, value=str(v)))
    db.commit()


def _user(db, name, **kw):
    kw.setdefault("is_welfare", True)
    u = models.WebUser(username=name, password_hash="x", points=0, vitality=14, **kw)
    db.add(u)
    db.commit()
    return u


def test_today_points_matches_record_after_checkin(db):
    """验算样例：连报第 5 天报到，实得＝3＋min(1×(5-1),5)＝7，取记录值。"""
    _seed_cfg(db)
    u = _user(db, "streak5")
    today = economy._today_start()
    db.add(models.CheckinRecord(user_id=u.id,
                                 checkin_date=today - timedelta(days=1),
                                 points_awarded=6, streak=4))
    db.commit()
    res = economy._do_checkin_core(db, u)
    assert res["streak"] == 5 and res["points_awarded"] == 7
    resp = economy.checkin_status(current_user=u, db=db)
    assert resp.checked_today is True
    assert resp.streak == 5
    assert resp.today_points == 7


def test_today_points_is_record_value_not_formula(db):
    """记录值含暴击放大时必须原样回（21≠公式 7），不许前端/后端按公式重算实得。"""
    _seed_cfg(db)
    u = _user(db, "crit_user")
    today = economy._today_start()
    db.add(models.CheckinRecord(user_id=u.id, checkin_date=today,
                                 points_awarded=21, streak=5))
    db.commit()
    resp = economy.checkin_status(current_user=u, db=db)
    assert resp.checked_today is True
    assert resp.today_points == 21


def test_today_points_none_when_not_checked(db):
    """未报到：today_points 为 null，连签仍按昨天口径回 4。"""
    _seed_cfg(db)
    u = _user(db, "not_yet")
    today = economy._today_start()
    db.add(models.CheckinRecord(user_id=u.id,
                                 checkin_date=today - timedelta(days=1),
                                 points_awarded=6, streak=4))
    db.commit()
    resp = economy.checkin_status(current_user=u, db=db)
    assert resp.checked_today is False
    assert resp.streak == 4
    assert resp.today_points is None


def test_today_points_zero_for_non_welfare(db):
    """非公益服用户报到只记连签、实得 0：today_points 必须是 0 而不是 null。"""
    _seed_cfg(db)
    u = _user(db, "plain_user", is_welfare=False)
    res = economy._do_checkin_core(db, u)
    assert res["points_awarded"] == 0
    resp = economy.checkin_status(current_user=u, db=db)
    assert resp.checked_today is True
    assert resp.points_enabled is False
    assert resp.today_points == 0


def test_today_points_defaults_to_none(db):
    """响应模型带默认值 None：旧客户端/旧构造路径不传该字段不报错（兼容口径）。"""
    resp = economy.CheckinStatusResponse(
        enabled=True, checked_today=False, streak=0, base_points=3,
        streak_bonus=1, streak_max_bonus=5, points=0)
    assert resp.today_points is None
