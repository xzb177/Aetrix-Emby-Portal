# -*- coding: utf-8 -*-
"""公益服规则后台可配置（feat/welfare-admin-config）。

审计结论（2026-10-29）：公益服／经济规则绝大多数早已走标准口径
（SystemConfig 键＋config_schema＋管理端字段），本单只补缺口：
1. welfare_streak_renew_min：键＋校验早已有，管理端公益配置页无字段 → 只补字段；
2. vitality_watch_tier1/2_minutes/reward：键＋校验早已有，管理端系统设置活力值卡无字段 → 只补字段；
3. vitality_checkin_gain：签到恢复活力点数此前硬编码 1 → 提升为 SystemConfig 键（默认 1）；
4. register_gift_welfare_days／register_gift_view_days：新注册赠送天数此前硬编码 30／30
   → 提升为 SystemConfig 键（默认 30／30）。
计分语义、默认值、行为一字不许变；故意未暴露项见 PR body。
"""

import os
from datetime import datetime, timedelta

os.environ.setdefault("DATABASE_TYPE", "sqlite")
os.environ.setdefault("REDIS_ENABLED", "false")

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend import config_schema, models, vitality
from backend.api import admin_economy, welfare_admin
from backend.config_service import save_config
from backend.integrations import store as _store


@pytest.fixture()
def db():
    engine = create_engine("sqlite://")
    models.Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    # 活力配置走进程内 TTL 热缓存（store.get_value，60 秒，按键名全局共享，
    # 不区分数据库）：前序测试文件（如 test_watch_vitality_tiers.py 的
    # test_disabled_gets_zero）在自己 :memory: 库里写 vitality_enabled=false
    # 并读过一次后，该值会在 60 秒内串进本文件的全新 :memory: 库，导致
    # _do_checkin_core 读到 enabled=False、签到活力增益全程 +0（CI 全量
    # 下 test_checkin_gain_* 两用例实得 5 的真凶；单跑本文件时无污染故全过）。
    # 邻近活力测试文件均在 fixture 里先失效，本文件补上同一口径：
    # 每个用例开始前把 get_vitality_config 读到的全部 store 键失效，
    # 本库缺行时走代码缺省（enabled 默认开），不再继承别库的缓存值。
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
    yield session
    session.close()


def _set_config(db, key, value):
    cfg = db.query(models.SystemConfig).filter(models.SystemConfig.key == key).first()
    if cfg:
        cfg.value = str(value)
    else:
        db.add(models.SystemConfig(key=key, value=str(value)))
    db.commit()


def _make_user(db, username="u1", **kw):
    u = models.WebUser(username=username, password_hash="x", **kw)
    db.add(u)
    db.commit()
    db.refresh(u)
    return u


# ==================== 1. 注册与默认值 ====================

def test_new_keys_registered_with_current_defaults():
    assert welfare_admin.WELFARE_CONFIG_KEYS["register_gift_welfare_days"] == "30"
    assert welfare_admin.WELFARE_CONFIG_KEYS["register_gift_view_days"] == "30"
    assert config_schema.WELFARE_CONFIG_SCHEMA["register_gift_welfare_days"] == (
        "int", 0, config_schema._BIG)
    assert config_schema.WELFARE_CONFIG_SCHEMA["register_gift_view_days"] == (
        "int", 1, config_schema._BIG)
    assert admin_economy.ECONOMY_CONFIG_KEYS["vitality_checkin_gain"] == "int"
    assert config_schema.ECONOMY_CONFIG_SCHEMA["vitality_checkin_gain"] == (
        "int", 0, config_schema._BIG)


def test_preexisting_keys_for_ui_only_fields_untouched():
    # 连签续期门槛与观影分档：键＋校验早已有，本单只补管理端字段
    assert welfare_admin.WELFARE_CONFIG_KEYS["welfare_streak_renew_min"] == "30"
    assert config_schema.WELFARE_CONFIG_SCHEMA["welfare_streak_renew_min"] == (
        "int", 0, 3650)
    for key in ("vitality_watch_tier1_minutes", "vitality_watch_tier1_reward",
                "vitality_watch_tier2_minutes", "vitality_watch_tier2_reward"):
        assert admin_economy.ECONOMY_CONFIG_KEYS[key] == "int"
        assert config_schema.ECONOMY_CONFIG_SCHEMA[key] == (
            "int", 0, config_schema._BIG)
    # 经济键集与注册表仍逐键对齐（config_slim_b4e 的契约）
    assert set(config_schema.ECONOMY_CONFIG_SCHEMA) == set(admin_economy.ECONOMY_CONFIG_KEYS)


# ==================== 2. 校验 ====================

def test_new_keys_normalize_valid_values():
    assert config_schema.normalize(
        "register_gift_welfare_days",
        config_schema.WELFARE_CONFIG_SCHEMA["register_gift_welfare_days"], 45) == "45"
    # 福利天数 0=永不过期（grant_welfare 口径），合法
    assert config_schema.normalize(
        "register_gift_welfare_days",
        config_schema.WELFARE_CONFIG_SCHEMA["register_gift_welfare_days"], 0) == "0"
    assert config_schema.normalize(
        "register_gift_view_days",
        config_schema.WELFARE_CONFIG_SCHEMA["register_gift_view_days"], 15) == "15"
    assert config_schema.normalize(
        "vitality_checkin_gain",
        config_schema.ECONOMY_CONFIG_SCHEMA["vitality_checkin_gain"], 3) == "3"
    assert config_schema.normalize(
        "vitality_checkin_gain",
        config_schema.ECONOMY_CONFIG_SCHEMA["vitality_checkin_gain"], 0) == "0"


def test_new_keys_normalize_rejects_invalid():
    welfare_spec = config_schema.WELFARE_CONFIG_SCHEMA["register_gift_welfare_days"]
    for bad in (-1, "abc", 1.5, True):
        with pytest.raises(config_schema.ConfigValueError):
            config_schema.normalize("register_gift_welfare_days", welfare_spec, bad)
    # 查看天数 0 按 timedelta 是“立即过期”不是永久，管理端直接禁掉
    view_spec = config_schema.WELFARE_CONFIG_SCHEMA["register_gift_view_days"]
    for bad in (0, -1, "abc"):
        with pytest.raises(config_schema.ConfigValueError):
            config_schema.normalize("register_gift_view_days", view_spec, bad)
    gain_spec = config_schema.ECONOMY_CONFIG_SCHEMA["vitality_checkin_gain"]
    for bad in (-1, "abc"):
        with pytest.raises(config_schema.ConfigValueError):
            config_schema.normalize("vitality_checkin_gain", gain_spec, bad)


def test_tier_order_cross_validation_still_applies(db):
    # 二档分钟数小于一档：端点层复合校验直接 400（vitality.validate_vitality_config 口径）
    with pytest.raises(HTTPException):
        admin_economy._validate_vitality_settings(
            db, {"vitality_watch_tier1_minutes": "30",
                 "vitality_watch_tier2_minutes": "20"})
    # 合法组合不报错（未提交的键走库里现值／默认值合并校验）
    admin_economy._validate_vitality_settings(
        db, {"vitality_watch_tier1_minutes": "30",
             "vitality_watch_tier2_minutes": "60"})


# ==================== 3. 保存后读数生效 ====================

def test_welfare_keys_save_and_read_back(db):
    save_config(db, {"welfare_streak_renew_min": "45",
                     "register_gift_welfare_days": "45",
                     "register_gift_view_days": "15"}, schema="welfare")
    db.commit()
    got = welfare_admin.welfare_config_get(db=db, current_admin=_make_user(db, "admin"))
    assert got["welfare_streak_renew_min"] == "45"
    assert got["register_gift_welfare_days"] == "45"
    assert got["register_gift_view_days"] == "15"
    with pytest.raises(config_schema.ConfigValueError):
        save_config(db, {"welfare_streak_renew_min": "-1"}, schema="welfare")


def test_economy_keys_save_and_read_back(db):
    save_config(db, {"vitality_checkin_gain": "3",
                     "vitality_watch_tier1_minutes": "45"}, schema="economy")
    db.commit()
    for key, want in (("vitality_checkin_gain", "3"),
                      ("vitality_watch_tier1_minutes", "45")):
        row = db.query(models.SystemConfig).filter(models.SystemConfig.key == key).first()
        assert row is not None and row.value == want
    with pytest.raises(config_schema.ConfigValueError):
        save_config(db, {"vitality_checkin_gain": "-1"}, schema="economy")


# ==================== 4. 签到活力奖励走新键 ====================

def _checkin_welfare_user(db, username, vitality=5):
    _set_config(db, "checkin_enabled", "true")
    return _make_user(db, username, is_welfare=True, vitality=vitality, points=0)


def test_checkin_gain_default_is_1(db):
    from backend.api.economy import _do_checkin_core

    u = _checkin_welfare_user(db, "welf1", vitality=5)
    _do_checkin_core(db, u)
    db.refresh(u)
    assert u.vitality == 6


def test_checkin_gain_custom_and_zero(db):
    from backend.api.economy import _do_checkin_core

    # PA-014：签到活力受每日免费上限钳制（默认上限 2 会把 gain 3 压成 2）；
    # 本用例测的是“自定义 gain 值生效”，故把上限抬到 10 隔离钳制影响，
    # 钳制本身由 test_pa013_016_economy.py 的 PA-014 用例覆盖。
    _set_config(db, "vitality_daily_gain_limit", "10")
    _set_config(db, "vitality_checkin_gain", "3")
    u = _checkin_welfare_user(db, "welf2", vitality=5)
    _do_checkin_core(db, u)
    db.refresh(u)
    assert u.vitality == 8

    _set_config(db, "vitality_checkin_gain", "0")
    u2 = _checkin_welfare_user(db, "welf3", vitality=5)
    # 同一 DB 里换用户签到：第二人同样走新键
    _do_checkin_core(db, u2)
    db.refresh(u2)
    assert u2.vitality == 5


# ==================== 5. 新注册赠送走新键 ====================

def test_register_gifts_custom_days(db):
    from backend.api.emby_portal import _grant_register_gifts

    _set_config(db, "register_gift_welfare_enabled", "true")
    _set_config(db, "register_gift_view_enabled", "true")
    _set_config(db, "register_gift_welfare_days", "45")
    _set_config(db, "register_gift_view_days", "15")
    user = _make_user(db, "newbie")
    before = datetime.now()
    _grant_register_gifts(db, user)
    after = datetime.now()

    db.refresh(user)
    assert before + timedelta(days=45) - timedelta(seconds=5) <= user.welfare_expires_at
    assert user.welfare_expires_at <= after + timedelta(days=45) + timedelta(seconds=5)
    row = db.query(models.EmbyViewUnlock).filter(
        models.EmbyViewUnlock.user_id == user.id).one()
    assert before + timedelta(days=15) - timedelta(seconds=5) <= row.expires_at
    assert row.expires_at <= after + timedelta(days=15) + timedelta(seconds=5)


def test_register_gifts_invalid_rows_fall_back_to_30(db):
    from backend.api.emby_portal import _grant_register_gifts

    _set_config(db, "register_gift_welfare_enabled", "true")
    _set_config(db, "register_gift_view_enabled", "true")
    _set_config(db, "register_gift_welfare_days", "abc")
    _set_config(db, "register_gift_view_days", "0")
    user = _make_user(db, "newbie2")
    before = datetime.now()
    _grant_register_gifts(db, user)
    after = datetime.now()

    db.refresh(user)
    assert before + timedelta(days=30) - timedelta(seconds=5) <= user.welfare_expires_at
    assert user.welfare_expires_at <= after + timedelta(days=30) + timedelta(seconds=5)
    row = db.query(models.EmbyViewUnlock).filter(
        models.EmbyViewUnlock.user_id == user.id).one()
    assert before + timedelta(days=30) - timedelta(seconds=5) <= row.expires_at
    assert row.expires_at <= after + timedelta(days=30) + timedelta(seconds=5)
