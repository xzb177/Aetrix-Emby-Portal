"""完美审计 R8（B8 经济）PA-013/014/015/016 回归测试。

- PA-013：不活跃扫描须把签到与播放计为活跃信号，不能只认网站登录。
- PA-014：签到活力恢复须按每日免费上限剩余量钳制。
- PA-015：签到返回的 vitality_gained 须是增量而非加后余额。
- PA-016：签到状态接口的连签须有“昨天”下界，断签旧记录不算当前连签。
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

from backend import models, vitality, welfare_expiry
from backend.api import economy
from backend.emby_server import models as em_models
from backend.integrations import store as _store


@pytest.fixture()
def db():
    engine = create_engine("sqlite://")
    models.Base.metadata.create_all(engine)
    s = sessionmaker(bind=engine)()
    _store.invalidate("welfare_inactive_days")
    # 活力配置走进程内 TTL 热缓存（按 key 全局共享、不区分数据库）：
    # 全量跑时前序测试文件在本库外写过的值会串进来，故每个用例前失效，
    # 与 test_welfare_admin_config.py 的 fixture 同口径。
    _store.invalidate(
        vitality.CONFIG_ENABLED,
        vitality.CONFIG_MAX,
        vitality.CONFIG_DAILY_COST,
        vitality.CONFIG_LIMIT_THRESHOLD,
        vitality.CONFIG_POINT_COST,
        vitality.CONFIG_WATCH_REWARD,
        vitality.CONFIG_DAILY_GAIN_LIMIT,
        vitality.CONFIG_ARCHIVE_CLAMP,
        vitality.CONFIG_WATCH_TIER1_MINUTES,
        vitality.CONFIG_WATCH_TIER1_REWARD,
        vitality.CONFIG_WATCH_TIER2_MINUTES,
        vitality.CONFIG_WATCH_TIER2_REWARD,
        vitality.CONFIG_CHECKIN_SKIP_DEDUCT_ENABLED,
        vitality.CONFIG_LAST_DEDUCT_DATE)
    yield s
    s.close()


def _user(db, name, **kw):
    u = models.WebUser(username=name, password_hash="x", is_welfare=True, **kw)
    db.add(u)
    db.commit()
    return u


def _seed_checkin_cfg(db, extra=None):
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


# ---------------- PA-013 ----------------

def test_pa013_checkin_yesterday_keeps_welfare(db):
    """40 天未网站登录、昨天刚签到 → 不取消公益身份。"""
    now = datetime.now()
    u = _user(db, "checkin_active", last_login_at=now - timedelta(days=40),
              welfare_expires_at=now + timedelta(days=365),
              welfare_granted_at=now - timedelta(days=40))
    db.add(models.CheckinRecord(user_id=u.id, checkin_date=now - timedelta(days=1),
                                points_awarded=0, streak=40))
    db.commit()
    res = welfare_expiry.check_inactive_users(db)
    assert res["disabled"] == []
    assert db.query(models.WebUser).filter_by(id=u.id).one().is_welfare is True


def test_pa013_recent_playback_keeps_welfare(db):
    """40 天未网站登录、5 天前有播放会话 → 不取消公益身份。"""
    now = datetime.now()
    u = _user(db, "playback_active", last_login_at=now - timedelta(days=40),
              welfare_expires_at=now + timedelta(days=365),
              welfare_granted_at=now - timedelta(days=40))
    db.add(em_models.PlaybackSession(
        user_id=u.id, session_key="pa013-sess",
        start_time=now - timedelta(days=40),
        last_update_at=now - timedelta(days=5)))
    db.commit()
    res = welfare_expiry.check_inactive_users(db)
    assert res["disabled"] == []
    assert db.query(models.WebUser).filter_by(id=u.id).one().is_welfare is True


def test_pa013_truly_inactive_still_disabled(db):
    """40 天无任何信号 → 仍取消公益身份（既有行为不回归）。"""
    now = datetime.now()
    u = _user(db, "truly_inactive", last_login_at=now - timedelta(days=40),
              welfare_expires_at=now + timedelta(days=365),
              welfare_granted_at=now - timedelta(days=40))
    res = welfare_expiry.check_inactive_users(db)
    assert res["disabled"] == [u.id]
    assert db.query(models.WebUser).filter_by(id=u.id).one().is_welfare is False


# ---------------- PA-014 / PA-015 ----------------

def _checkin_user(db, name, vitality):
    u = _user(db, name, points=0, vitality=vitality)
    return u


def test_pa014_checkin_vitality_clamped_to_daily_remaining(db):
    """上限 3（非缺省，证明读的是配置值）、今日已免费得 1、gain 5 →
    签到只补剩余 2 点活力（修前全额发 5 点）。"""
    _seed_checkin_cfg(db, {"vitality_daily_gain_limit": "3",
                           "vitality_checkin_gain": "5"})
    u = _checkin_user(db, "clamp_user", vitality=1)
    today_start = economy._today_start()
    db.add(models.VitalityLog(user_id=u.id, delta=1, balance_after=1,
                              reason="watch_reward", created_at=today_start))
    db.commit()
    award = economy._do_checkin_core(db, u)
    final = db.query(models.WebUser).filter_by(id=u.id).one().vitality
    assert final == 3
    assert award["vitality_gained"] == 2


def test_pa015_checkin_vitality_gained_is_increment(db):
    """活力 10、gain 1、上限未满 → 返回增量 1 而非余额 11。"""
    _seed_checkin_cfg(db, {"vitality_daily_gain_limit": "10",
                           "vitality_checkin_gain": "1"})
    u = _checkin_user(db, "increment_user", vitality=10)
    award = economy._do_checkin_core(db, u)
    final = db.query(models.WebUser).filter_by(id=u.id).one().vitality
    assert final == 11
    assert award["vitality_gained"] == 1


def test_pa015_checkin_vitality_full_returns_zero(db):
    """活力已满 14、gain 1 → 增量为 0（面板靠 >0 隐藏该行）。"""
    _seed_checkin_cfg(db, {"vitality_daily_gain_limit": "10",
                           "vitality_checkin_gain": "1"})
    u = _checkin_user(db, "full_user", vitality=14)
    award = economy._do_checkin_core(db, u)
    final = db.query(models.WebUser).filter_by(id=u.id).one().vitality
    assert final == 14
    assert award["vitality_gained"] == 0


def test_pa014_default_gain_unchanged(db):
    """默认 gain=1、上限 2、今日未得 → 签到补 1 点，行为不变。"""
    _seed_checkin_cfg(db, {"vitality_daily_gain_limit": "2",
                           "vitality_checkin_gain": "1"})
    u = _checkin_user(db, "default_user", vitality=5)
    award = economy._do_checkin_core(db, u)
    final = db.query(models.WebUser).filter_by(id=u.id).one().vitality
    assert final == 6
    assert award["vitality_gained"] == 1


# ---------------- PA-016 ----------------

def test_pa016_stale_streak_not_shown(db):
    """10 天前 streak=20 的旧记录 → 状态连签返回 0。"""
    u = _user(db, "stale_user")
    today = economy._today_start()
    db.add(models.CheckinRecord(user_id=u.id,
                                checkin_date=today - timedelta(days=10),
                                points_awarded=0, streak=20))
    db.commit()
    resp = economy.checkin_status(current_user=u, db=db)
    assert resp.streak == 0


def test_pa016_yesterday_streak_shown(db):
    """昨天 streak=6 的记录 → 状态连签返回 6。"""
    u = _user(db, "yesterday_user")
    today = economy._today_start()
    db.add(models.CheckinRecord(user_id=u.id,
                                checkin_date=today - timedelta(days=1),
                                points_awarded=0, streak=6))
    db.commit()
    resp = economy.checkin_status(current_user=u, db=db)
    assert resp.streak == 6
