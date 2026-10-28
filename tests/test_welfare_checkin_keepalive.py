"""保号 A+B：签到日免日耗（vitality.daily_deduct）＋连签保资格（welfare_expiry）。

A：上一上海自然日有签到记录的用户，daily_deduct 本轮不扣；从不签到的照常扣。
B：已过期用户若最近签到在昨天或今天、且该条 streak >= 阈值（默认 30），
   直接续期 30 天、不扣分、记 streak_renew；查看凭证不动；续期后重跑不重复续。
"""
import os
import sys
from datetime import datetime, timedelta

os.environ.setdefault("DATABASE_TYPE", "sqlite")
os.environ.setdefault("REDIS_ENABLED", "false")
sys.path.insert(0, ".")

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend import models, vitality, welfare_expiry
from backend.api import economy
from backend.integrations import store as _store


def _db():
    engine = create_engine("sqlite://")
    models.Base.metadata.create_all(engine)
    s = sessionmaker(bind=engine)()
    _store.invalidate(
        "vitality_enabled", "vitality_daily_cost", "vitality_max",
        vitality.CONFIG_LAST_DEDUCT_DATE,
        "welfare_grace_days", "welfare_streak_renew_min",
    )
    return s


def _frames():
    today = economy._today_start_utc_naive()
    return today, today - timedelta(days=1)


def _user(db, name, **kw):
    u = models.WebUser(username=name, password_hash="x", **kw)
    db.add(u)
    db.commit()
    return u


def _checkin(db, user_id, when, streak):
    db.add(models.CheckinRecord(user_id=user_id, checkin_date=when,
                                points_awarded=0, streak=streak))
    db.commit()


def _vitality(db, user_id):
    return db.query(models.WebUser).filter_by(id=user_id).one().vitality


# ---------------- A：签到日免日耗 ----------------

def test_deduct_skips_yesterday_checkin_only():
    """昨天签过 → 不扣；没签过 → 照扣；非公益 → 本来就不扣；扣到 0 计 suspended。"""
    db = _db()
    today, yesterday = _frames()
    skip = _user(db, "skip", is_welfare=True, vitality=5)
    _checkin(db, skip.id, yesterday, 5)
    control = _user(db, "control", is_welfare=True, vitality=5)
    zero = _user(db, "zero", is_welfare=True, vitality=1)
    non_welfare = _user(db, "nonw", is_welfare=False, vitality=5)
    _checkin(db, non_welfare.id, yesterday, 5)
    res = vitality.daily_deduct(db)
    assert _vitality(db, skip.id) == 5
    assert _vitality(db, control.id) == 4
    assert _vitality(db, zero.id) == 0
    assert _vitality(db, non_welfare.id) == 5
    assert res["deducted"] == 2
    assert res["suspended"] == 1
    assert res["skipped_checkin"] == 1
    db.close()


def test_vitality_climbs_back_with_daily_checkin():
    """活力 2 的用户恢复每天签到：扣减跳过（ stays 2）＋当天签到 +1 → 3，爬回阈值。"""
    db = _db()
    today, yesterday = _frames()
    for key, val in [("checkin_enabled", "true"), ("checkin_base_min", "3"),
                     ("checkin_base_max", "3"), ("checkin_streak_bonus", "1"),
                     ("checkin_streak_max_bonus", "5"), ("checkin_penalty_pct", "0")]:
        db.add(models.SystemConfig(key=key, value=val))
    db.commit()
    u = _user(db, "climb", is_welfare=True, vitality=2, points=0)
    _checkin(db, u.id, yesterday, 5)
    vitality.daily_deduct(db)
    assert _vitality(db, u.id) == 2
    award = economy._do_checkin_core(db, u)
    assert award["points_awarded"] > 0
    assert _vitality(db, u.id) == 3
    db.close()


def test_deduct_ignores_stale_checkin():
    """最近签到是 3 天前（stale）→ 不跳过，照常扣。"""
    db = _db()
    today, yesterday = _frames()
    u = _user(db, "stale", is_welfare=True, vitality=5)
    _checkin(db, u.id, today - timedelta(days=3), 40)
    res = vitality.daily_deduct(db)
    assert _vitality(db, u.id) == 4
    assert res["skipped_checkin"] == 0
    db.close()


# ---------------- B：连签保资格 ----------------

def _expiry_user(db, name, expired_days_ago, **kw):
    now = datetime.now()
    return _user(db, name, is_welfare=True,
                 welfare_expires_at=now - timedelta(days=expired_days_ago),
                 welfare_granted_at=now - timedelta(days=60), **kw)


def test_streak_renew_expired_within_grace():
    """过期 3 天（保留期内）＋今天签到 streak 30 → 续 30 天、零扣分、查看行不动。"""
    db = _db()
    today, yesterday = _frames()
    before = datetime.now()
    u = _expiry_user(db, "renew", 3)
    _checkin(db, u.id, today, 30)
    db.add(models.EmbyViewUnlock(user_id=u.id, realm_id=1,
                                 expires_at=before + timedelta(days=5),
                                 points_spent=50))
    db.commit()
    view_before = db.query(models.EmbyViewUnlock).filter_by(user_id=u.id).one().expires_at
    res = welfare_expiry.check_welfare_expiry(db)
    after = datetime.now()
    row = db.query(models.WebUser).filter_by(id=u.id).one()
    assert row.is_welfare is True
    assert before + timedelta(days=30) <= row.welfare_expires_at <= after + timedelta(days=30)
    assert res["renewed"] == [u.id]
    assert u.id not in res["grace"] and u.id not in res["expired"]
    log = db.query(models.WelfareGrantLog).filter_by(
        user_id=u.id, channel="streak_renew").one()
    assert log.days == 30
    assert db.query(models.PointsLog).filter_by(user_id=u.id).count() == 0
    view_after = db.query(models.EmbyViewUnlock).filter_by(user_id=u.id).one()
    assert view_after.expires_at == view_before and view_after.points_spent == 50
    db.close()


def test_streak_renew_beyond_grace_before_expire():
    """过期 10 天（超保留期）＋昨天签到 streak 35 → 仍续期，不走取消路径。"""
    db = _db()
    today, yesterday = _frames()
    u = _expiry_user(db, "renew_late", 10)
    _checkin(db, u.id, yesterday, 35)
    res = welfare_expiry.check_welfare_expiry(db)
    row = db.query(models.WebUser).filter_by(id=u.id).one()
    assert row.is_welfare is True
    assert res["renewed"] == [u.id] and res["expired"] == []
    assert db.query(models.WelfareGrantLog).filter_by(
        user_id=u.id, channel="auto_expire").count() == 0
    db.close()


def test_streak_below_threshold_follows_old_path():
    """过期 3 天＋今天签到 streak 29（<30）→ 原路径：保留期只记录，不取消不续期。"""
    db = _db()
    today, yesterday = _frames()
    u = _expiry_user(db, "short", 3)
    _checkin(db, u.id, today, 29)
    res = welfare_expiry.check_welfare_expiry(db)
    row = db.query(models.WebUser).filter_by(id=u.id).one()
    assert row.is_welfare is True
    assert res["grace"] == [u.id] and res["renewed"] == []
    assert db.query(models.WelfareGrantLog).filter_by(user_id=u.id).count() == 0
    db.close()


def test_streak_stale_does_not_renew():
    """过期 8 天＋streak 40 但最近签到是 3 天前 → 不续，走取消路径。"""
    db = _db()
    today, yesterday = _frames()
    u = _expiry_user(db, "stale_streak", 8)
    _checkin(db, u.id, today - timedelta(days=3), 40)
    res = welfare_expiry.check_welfare_expiry(db)
    row = db.query(models.WebUser).filter_by(id=u.id).one()
    assert row.is_welfare is False
    assert res["expired"] == [u.id] and res["renewed"] == []
    db.close()


def test_streak_renew_idempotent():
    """续期后重跑 tick：不再过期、不重复续期、不多写日志。"""
    db = _db()
    today, yesterday = _frames()
    u = _expiry_user(db, "idem", 3)
    _checkin(db, u.id, today, 30)
    first = welfare_expiry.check_welfare_expiry(db)
    assert first["renewed"] == [u.id]
    exp_after_first = db.query(models.WebUser).filter_by(id=u.id).one().welfare_expires_at
    second = welfare_expiry.check_welfare_expiry(db)
    assert second["renewed"] == [] and second["grace"] == [] and second["expired"] == []
    assert db.query(models.WebUser).filter_by(id=u.id).one().welfare_expires_at == exp_after_first
    assert db.query(models.WelfareGrantLog).filter_by(
        user_id=u.id, channel="streak_renew").count() == 1
    db.close()


def test_null_expiry_untouched():
    """expires_at 为 NULL（永不过期）＋连签 30 → 不受影响，无日志。"""
    db = _db()
    today, yesterday = _frames()
    u = _user(db, "perm", is_welfare=True, welfare_expires_at=None)
    _checkin(db, u.id, today, 30)
    res = welfare_expiry.check_welfare_expiry(db)
    row = db.query(models.WebUser).filter_by(id=u.id).one()
    assert row.is_welfare is True and row.welfare_expires_at is None
    assert res["renewed"] == []
    assert db.query(models.WelfareGrantLog).filter_by(user_id=u.id).count() == 0
    db.close()
