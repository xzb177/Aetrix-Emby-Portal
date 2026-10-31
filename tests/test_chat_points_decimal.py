# -*- coding: utf-8 -*-
"""发言奖励小数化（2026-10-31 用户亲令：每条 1 能量太多，改默认 0.1）。

存储结论：WebUser.points 与 PointsLog.amount 均为 Integer，全链整数，
不迁列类型；改走「小数累计」：每条奖励按分（0.01 能量）累计到用户行
chat_frac_cents，满 100 分结算 1 能量进余额＋台账，余数跨日保留不清零；
当日封顶按当日累计（含未结算小数，chat_day_cents）判定，不让小数绕开。
"""

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend import config_schema, models
from backend.api import welfare_admin


@pytest.fixture()
def db():
    engine = create_engine("sqlite://")
    models.Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    yield session
    session.close()


def _set_config(db, key, value):
    cfg = db.query(models.SystemConfig).filter(models.SystemConfig.key == key).first()
    if cfg:
        cfg.value = str(value)
    else:
        db.add(models.SystemConfig(key=key, value=str(value)))
    db.commit()


def _make_user(db, **kw):
    u = models.WebUser(username=kw.pop("username", "u1"), password_hash="x", **kw)
    db.add(u)
    db.commit()
    db.refresh(u)
    return u


# ---------- 配置与校验 ----------


def test_default_per_msg_is_0_1():
    assert welfare_admin.WELFARE_CONFIG_KEYS["points_chat_per_msg"] == "0.1"


def test_schema_is_decimal_spec():
    assert config_schema.WELFARE_CONFIG_SCHEMA["points_chat_per_msg"] == (
        "float",
        0,
        100,
        2,
    )


def test_normalize_accepts_decimals_and_canonicalizes():
    spec = config_schema.WELFARE_CONFIG_SCHEMA["points_chat_per_msg"]
    assert config_schema.normalize("points_chat_per_msg", spec, 0.1) == "0.1"
    assert config_schema.normalize("points_chat_per_msg", spec, "0.2") == "0.2"
    assert config_schema.normalize("points_chat_per_msg", spec, "0.10") == "0.1"
    assert config_schema.normalize("points_chat_per_msg", spec, 2) == "2"
    assert config_schema.normalize("points_chat_per_msg", spec, 0) == "0"
    # 空串/None = 未设置（删行，回退默认值）
    assert config_schema.normalize("points_chat_per_msg", spec, "") is None
    assert config_schema.normalize("points_chat_per_msg", spec, None) is None


def test_normalize_rejects_bad_decimals():
    spec = config_schema.WELFARE_CONFIG_SCHEMA["points_chat_per_msg"]
    for bad in (-1, "-0.1", "abc", True, "0.123", 101, "100.01"):
        with pytest.raises(config_schema.ConfigValueError):
            config_schema.normalize("points_chat_per_msg", spec, bad)


# ---------- 计分：小数累计与结算 ----------


def test_ten_messages_settle_exactly_one_point(db):
    from backend import points

    u = _make_user(db, username="chatter", points=0)
    _set_config(db, "points_chat_per_msg", "0.1")
    _set_config(db, "points_chat_daily_cap", "20")
    got = [points.award_chat_points(db, u.id) for _ in range(10)]
    # 前 9 条只累计小数（结算 0），第 10 条满 1 能量结算
    assert got == [0] * 9 + [1]
    db.refresh(u)
    assert u.points == 1
    rows = (
        db.query(models.PointsLog)
        .filter(models.PointsLog.user_id == u.id, models.PointsLog.type == "chat")
        .all()
    )
    assert len(rows) == 1 and rows[0].amount == 1


def test_five_messages_at_0_2_settle_one_point(db):
    from backend import points

    u = _make_user(db, username="chatter", points=0)
    _set_config(db, "points_chat_per_msg", "0.2")
    _set_config(db, "points_chat_daily_cap", "20")
    got = [points.award_chat_points(db, u.id) for _ in range(5)]
    assert got == [0] * 4 + [1]
    db.refresh(u)
    assert u.points == 1


def test_default_without_config_rows_is_0_1(db):
    """未配行时默认 0.1：发 10 条才结算 1 能量（旧默认 1 会发 10 条得 10）。"""
    from backend import points

    u = _make_user(db, username="chatter", points=0)
    got = [points.award_chat_points(db, u.id) for _ in range(10)]
    assert got == [0] * 9 + [1]
    db.refresh(u)
    assert u.points == 1


def test_remainder_survives_day_rollover(db):
    """余数跨日不清零：昨日攒 0.9，今日第 1 条补满结算，余 0.2 留待明日。"""
    from backend import points

    u = _make_user(db, username="chatter", points=0)
    _set_config(db, "points_chat_per_msg", "0.3")
    _set_config(db, "points_chat_daily_cap", "20")
    for _ in range(3):
        assert points.award_chat_points(db, u.id) == 0
    db.refresh(u)
    assert u.points == 0
    # 模拟跨日：把当日累计标记改成旧日期（当日计数应重置，余数保留）
    u.chat_day = "2000-01-01"
    u.chat_day_cents = 300
    db.commit()
    assert points.award_chat_points(db, u.id) == 1
    db.refresh(u)
    assert u.points == 1
    assert u.chat_frac_cents == 20


def test_daily_cap_counts_fractional_accrual(db):
    """封顶按能量口径含小数：0.3/条、上限 1 → 累计 0.3/0.6/0.9/1.0 后停发。"""
    from backend import points

    u = _make_user(db, username="chatter", points=0)
    _set_config(db, "points_chat_per_msg", "0.3")
    _set_config(db, "points_chat_daily_cap", "1")
    got = [points.award_chat_points(db, u.id) for _ in range(5)]
    # 前三条各累计 30 分；第四条只补到上限（10 分）并结算 1；第五条停发
    assert got == [0, 0, 0, 1, 0]
    db.refresh(u)
    assert u.points == 1
    status = points.get_chat_points_status(db, u.id)
    assert status["earned_today"] == 1
    # 已达上限后再发不累计、不结算
    assert points.award_chat_points(db, u.id) == 0
    db.refresh(u)
    assert u.points == 1
    assert u.chat_frac_cents == 0


def test_negative_config_falls_back_to_default_0_1(db):
    """负配置回落默认 0.1（与旧口径同型：回落默认值、绝不扣分）。"""
    from backend import points

    u = _make_user(db, username="chatter", points=0)
    _set_config(db, "points_chat_per_msg", "-3")
    _set_config(db, "points_chat_daily_cap", "100")
    for _ in range(10):
        points.award_chat_points(db, u.id)
        db.refresh(u)
        assert u.points >= 0
    assert u.points == 1


def test_status_reports_decimal_progress(db):
    from backend import points

    u = _make_user(db, username="chatter", points=0)
    _set_config(db, "points_chat_per_msg", "0.1")
    _set_config(db, "points_chat_daily_cap", "20")
    for _ in range(3):
        points.award_chat_points(db, u.id)
    status = points.get_chat_points_status(db, u.id)
    assert status["per_msg"] == 0.1
    assert status["earned_today"] == 0.3
    assert status["daily_cap"] == 20
    # 台账只记已结算整数：0.3 尚未结算，累计结算口径为 0
    assert status["total_earned"] == 0
