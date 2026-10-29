"""连签保资格（streak_renew）后台可配置：总开关＋续期天数（2026-10-30 用户亲点）。

此前缺口：阈值 welfare_streak_renew_min 早已可配，但
1. 没有启停开关——到期 tick 无条件尝试 streak_renew，服主关不掉；
2. 续期天数在 welfare_expiry 里写死 30（实际延期与 WelfareGrantLog.days 两处）。
本单补两键（默认＝现行行为，缺行时与改动前逐字一致）：
- welfare_streak_renew_enabled（bool，默认 true）：关后整条 streak_renew
  不跑，到期照常走保留期/取消；bot 连签行同步不再宣称自动续期。
- welfare_streak_renew_days（int，默认 30，1-365）：延期与日志 days 同走此值。
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

from backend import config_schema, models, welfare_expiry
from backend.api import economy, welfare_admin
from backend.integrations import store as _store
from backend.tg_bot import features


def _db():
    engine = create_engine("sqlite://")
    models.Base.metadata.create_all(engine)
    s = sessionmaker(bind=engine)()
    _store.invalidate(
        "welfare_grace_days", "welfare_streak_renew_min",
        "welfare_streak_renew_enabled", "welfare_streak_renew_days",
    )
    return s


def _set_config(db, key, value):
    cfg = db.query(models.SystemConfig).filter(
        models.SystemConfig.key == key).first()
    if cfg:
        cfg.value = str(value)
    else:
        db.add(models.SystemConfig(key=key, value=str(value)))
    db.commit()


def _user(db, name, **kw):
    u = models.WebUser(username=name, password_hash="x", **kw)
    db.add(u)
    db.commit()
    return u


def _expiry_user(db, name, expired_days_ago):
    now = datetime.now()
    return _user(db, name, is_welfare=True,
                 welfare_expires_at=now - timedelta(days=expired_days_ago),
                 welfare_granted_at=now - timedelta(days=60))


def _checkin(db, user_id, when, streak):
    db.add(models.CheckinRecord(user_id=user_id, checkin_date=when,
                                points_awarded=0, streak=streak))
    db.commit()


def _renew_logs(db, user_id):
    return db.query(models.WelfareGrantLog).filter_by(
        user_id=user_id, channel="streak_renew").all()


# ==================== 1. 注册与校验 ====================

def test_new_keys_registered_with_current_defaults():
    assert welfare_admin.WELFARE_CONFIG_KEYS["welfare_streak_renew_enabled"] == "true"
    assert welfare_admin.WELFARE_CONFIG_KEYS["welfare_streak_renew_days"] == "30"
    assert config_schema.WELFARE_CONFIG_SCHEMA["welfare_streak_renew_enabled"] == ("bool",)
    assert config_schema.WELFARE_CONFIG_SCHEMA["welfare_streak_renew_days"] == ("int", 1, 365)
    # 阈值键不动
    assert welfare_admin.WELFARE_CONFIG_KEYS["welfare_streak_renew_min"] == "30"
    assert config_schema.WELFARE_CONFIG_SCHEMA["welfare_streak_renew_min"] == ("int", 0, 3650)


def test_new_keys_normalize():
    schema = config_schema.WELFARE_CONFIG_SCHEMA
    assert config_schema.normalize(
        "welfare_streak_renew_enabled", schema["welfare_streak_renew_enabled"], True) == "true"
    assert config_schema.normalize(
        "welfare_streak_renew_enabled", schema["welfare_streak_renew_enabled"], False) == "false"
    assert config_schema.normalize(
        "welfare_streak_renew_days", schema["welfare_streak_renew_days"], 45) == "45"
    with pytest.raises(config_schema.ConfigValueError):
        config_schema.normalize(
            "welfare_streak_renew_days", schema["welfare_streak_renew_days"], 0)
    with pytest.raises(config_schema.ConfigValueError):
        config_schema.normalize(
            "welfare_streak_renew_days", schema["welfare_streak_renew_days"], 366)


# ==================== 2. 开关 ====================

def test_switch_off_within_grace_falls_back_to_grace():
    """开关关：过期 3 天（保留期内）＋连签达标 → 不续，走保留期只记录。"""
    db = _db()
    _set_config(db, "welfare_streak_renew_enabled", "false")
    today = economy._today_start_utc_naive()
    u = _expiry_user(db, "off_grace", 3)
    exp_before = u.welfare_expires_at
    _checkin(db, u.id, today, 30)
    res = welfare_expiry.check_welfare_expiry(db)
    row = db.query(models.WebUser).filter_by(id=u.id).one()
    assert row.is_welfare is True
    assert row.welfare_expires_at == exp_before
    assert res["renewed"] == [] and res["grace"] == [u.id]
    assert _renew_logs(db, u.id) == []
    db.close()


def test_switch_off_beyond_grace_still_expires():
    """开关关：过期 10 天（超保留期）＋连签达标 → 不续，走取消路径。"""
    db = _db()
    _set_config(db, "welfare_streak_renew_enabled", "false")
    today = economy._today_start_utc_naive()
    u = _expiry_user(db, "off_expire", 10)
    _checkin(db, u.id, today - timedelta(days=1), 35)
    res = welfare_expiry.check_welfare_expiry(db)
    row = db.query(models.WebUser).filter_by(id=u.id).one()
    assert row.is_welfare is False
    assert res["expired"] == [u.id] and res["renewed"] == []
    assert _renew_logs(db, u.id) == []
    assert db.query(models.WelfareGrantLog).filter_by(
        user_id=u.id, channel="auto_expire").count() == 1
    db.close()


def test_switch_on_renews():
    """开关显式开（存 "true"）→ 与默认行为一致，正常续期。"""
    db = _db()
    _set_config(db, "welfare_streak_renew_enabled", "true")
    today = economy._today_start_utc_naive()
    u = _expiry_user(db, "on", 3)
    _checkin(db, u.id, today, 30)
    res = welfare_expiry.check_welfare_expiry(db)
    assert res["renewed"] == [u.id]
    assert len(_renew_logs(db, u.id)) == 1
    db.close()


# ==================== 3. 续期天数 ====================

def test_custom_days_45_applies_to_expiry_and_log():
    """天数改 45：实际延期与 WelfareGrantLog.days 都必须是 45。"""
    db = _db()
    _set_config(db, "welfare_streak_renew_enabled", "true")
    _set_config(db, "welfare_streak_renew_days", "45")
    today = economy._today_start_utc_naive()
    before = datetime.now()
    u = _expiry_user(db, "days45", 3)
    _checkin(db, u.id, today, 30)
    res = welfare_expiry.check_welfare_expiry(db)
    after = datetime.now()
    row = db.query(models.WebUser).filter_by(id=u.id).one()
    assert res["renewed"] == [u.id]
    assert before + timedelta(days=45) <= row.welfare_expires_at <= after + timedelta(days=45)
    logs = _renew_logs(db, u.id)
    assert len(logs) == 1 and logs[0].days == 45
    db.close()


def test_custom_days_without_enabled_row_defaults_on():
    """只配天数、不配开关行 → 开关默认开，45 天生效。"""
    db = _db()
    _set_config(db, "welfare_streak_renew_days", "45")
    today = economy._today_start_utc_naive()
    u = _expiry_user(db, "days_only", 3)
    _checkin(db, u.id, today, 30)
    res = welfare_expiry.check_welfare_expiry(db)
    assert res["renewed"] == [u.id]
    assert _renew_logs(db, u.id)[0].days == 45
    db.close()


def test_defaults_unchanged_no_config_rows():
    """无任何配置行：行为与改动前逐字一致（续 30 天、日志 30）。"""
    db = _db()
    today = economy._today_start_utc_naive()
    before = datetime.now()
    u = _expiry_user(db, "defaults", 3)
    _checkin(db, u.id, today, 30)
    res = welfare_expiry.check_welfare_expiry(db)
    after = datetime.now()
    row = db.query(models.WebUser).filter_by(id=u.id).one()
    assert res["renewed"] == [u.id]
    assert before + timedelta(days=30) <= row.welfare_expires_at <= after + timedelta(days=30)
    assert _renew_logs(db, u.id)[0].days == 30
    db.close()


# ==================== 4. 阈值键不回归 ====================

def test_threshold_key_still_governs():
    """阈值改 45：streak 30 不续（保留期）、streak 45 才续，续期天数仍默认 30。"""
    db = _db()
    _set_config(db, "welfare_streak_renew_min", "45")
    today = economy._today_start_utc_naive()
    short = _expiry_user(db, "short", 3)
    _checkin(db, short.id, today, 30)
    enough = _expiry_user(db, "enough", 3)
    _checkin(db, enough.id, today, 45)
    res = welfare_expiry.check_welfare_expiry(db)
    assert res["grace"] == [short.id]
    assert res["renewed"] == [enough.id]
    assert _renew_logs(db, enough.id)[0].days == 30
    db.close()


# ==================== 5. bot 连签行文案 ====================

def test_bot_line_default_promises_renew():
    db = _db()
    today = economy._today_start_utc_naive()
    u = _user(db, "bot_default")
    _checkin(db, u.id, today, 5)
    line = features._streak_line(db, u.id)
    assert "连签：5 天（满 30 天自动续公益资格）" in line
    db.close()


def test_bot_line_switch_off_hides_promise():
    """开关关：连签行只报天数，不再宣称自动续期。"""
    db = _db()
    _set_config(db, "welfare_streak_renew_enabled", "false")
    today = economy._today_start_utc_naive()
    u = _user(db, "bot_off")
    _checkin(db, u.id, today, 5)
    line = features._streak_line(db, u.id)
    assert line == "📋 连签：5 天"
    assert "自动续" not in line
    db.close()


def test_bot_line_custom_threshold_shown():
    db = _db()
    _set_config(db, "welfare_streak_renew_min", "45")
    today = economy._today_start_utc_naive()
    u = _user(db, "bot_min45")
    _checkin(db, u.id, today, 5)
    line = features._streak_line(db, u.id)
    assert "满 45 天自动续公益资格" in line
    db.close()
