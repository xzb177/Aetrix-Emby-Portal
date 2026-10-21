"""配置简化第 4 批 4-4：reminders/coupons/welfare/registration/view_unlock
迁移到 config_service.save_config 的回环测试。

每个家族的测试都验证三件事（迁移的核心承诺）：
1. 写后经 store 立读到新值（失效覆盖不回退）；
2. 落库字节与迁移前逐字一致（风格位 digit/word、原文直存等）；
3. 非法值整批拒绝、脏键拒绝（注册表语义）。
"""
import os

os.environ.setdefault("DATABASE_TYPE", "sqlite")
os.environ.setdefault("REDIS_ENABLED", "false")

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend import config_schema, config_service, models
from backend.integrations import store


@pytest.fixture()
def db():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    models.Base.metadata.create_all(engine)
    TestingSessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    session = TestingSessionLocal()
    try:
        yield session
    finally:
        session.close()


@pytest.fixture(autouse=True)
def _clean_store_cache():
    """store 缓存是进程全局的：每个用例后全清，避免互相污染。"""
    yield
    store.invalidate()


def _row(db, key):
    return (
        db.query(models.SystemConfig)
        .filter(models.SystemConfig.key == key)
        .first()
    )


# ---------- reminders：enabled word 风格 + days 原文直存 ----------

def test_reminders_enabled_and_days_raw(db):
    # days 含非法档位也不拒绝（今天端点原文存储、读侧宽容，逐字保留）
    config_service.save_config(
        db,
        {"expiry_reminder_days": "7,x,3"},
        schema="reminders",
    )
    db.commit()
    assert _row(db, "expiry_reminder_days").value == "7,x,3"
    # 2026-10-21 A 类摘面板：expiry_reminder_enabled 已摘除注册表，
    # 再经统一通道写必须整批拒绝（未知键）
    with pytest.raises(config_schema.ConfigValueError):
        config_service.save_config(
            db, {"expiry_reminder_enabled": "true"}, schema="reminders")
    db.rollback()


# ---------- coupons：word 风格 + int 范围 ----------

def test_coupons_roundtrip(db):
    # 2026-10-21 A 类摘面板：coupon_enabled 已摘除注册表（env 控制），
    # 本家族只剩 coupon_reserve_hours 可写
    config_service.save_config(
        db, {"coupon_reserve_hours": 5}, schema="coupons"
    )
    db.commit()
    assert _row(db, "coupon_reserve_hours").value == "5"
    with pytest.raises(config_schema.ConfigValueError):
        config_service.save_config(
            db, {"coupon_enabled": "true"}, schema="coupons")
    db.rollback()


def test_coupons_reject_out_of_range_atomic(db):
    with pytest.raises(config_schema.ConfigValueError):
        config_service.save_config(
            db, {"coupon_enabled": "false", "coupon_reserve_hours": 99999}, schema="coupons"
        )
    db.rollback()
    # 整批不落库：连合法的 enabled 也没写
    assert _row(db, "coupon_enabled") is None


# ---------- welfare：digit 风格布尔必须存 "1"/"0"（与今天逐字一致） ----------

def test_welfare_digit_bool_style(db):
    # 2026-10-21 A 类摘面板：lottery_enabled 已摘除，改用保留的 B 类
    # lottery_require_group_member（同为 digit 风格）
    config_service.save_config(db, {"lottery_require_group_member": True}, schema="welfare")
    db.commit()
    assert _row(db, "lottery_require_group_member").value == "1"


def test_welfare_word_bool_style(db):
    # 2026-10-21 A 类摘面板：welfare 家族的 word 风格 bool 键已全数摘除，
    # word 风格向量改到 economy 家族的保留键 recharge_enabled（B 类）
    config_service.save_config(db, {"recharge_enabled": True}, schema="economy")
    db.commit()
    assert _row(db, "recharge_enabled").value == "true"


def test_welfare_group_ids_raw(db):
    config_service.save_config(db, {"bot_group_ids": "1,2"}, schema="welfare")
    db.commit()
    assert _row(db, "bot_group_ids").value == "1,2"


# ---------- registration：枚举 + 空消息删行 ----------

def test_registration_roundtrip(db):
    config_service.save_config(
        db,
        {
            "registration_mode": "open",
            "registration_closed_message": "暂停开放",
            "register_ratelimit_max": 10,
        },
        schema="registration",
    )
    db.commit()
    assert store.get_value(db, "registration_mode", "nope") == "open"
    assert store.get_value(db, "registration_closed_message", "nope") == "暂停开放"
    assert store.get_value(db, "register_ratelimit_max", "nope") == "10"


def test_registration_mode_enum_rejects(db):
    with pytest.raises(config_schema.ConfigValueError):
        config_service.save_config(
            db, {"registration_mode": "invite"}, schema="registration"
        )
    db.rollback()
    assert _row(db, "registration_mode") is None


# ---------- view_unlock：int 键 ----------

def test_view_unlock_roundtrip(db):
    config_service.save_config(
        db,
        {"emby_view_unlock_points": 10, "emby_view_unlock_days": 0},
        schema="view_unlock",
    )
    db.commit()
    assert store.get_value(db, "emby_view_unlock_points", "nope") == "10"
    assert _row(db, "emby_view_unlock_days").value == "0"


def test_view_unlock_rejects_negative_atomic(db):
    with pytest.raises(config_schema.ConfigValueError):
        config_service.save_config(
            db,
            {"emby_view_unlock_points": 5, "emby_view_unlock_days": -1},
            schema="view_unlock",
        )
    db.rollback()
    assert _row(db, "emby_view_unlock_points") is None


# ---------- 未知键一律拒绝（防笔误造脏键） ----------

def test_unknown_key_rejected(db):
    with pytest.raises(config_schema.ConfigValueError):
        config_service.save_config(db, {"bogus_key": "x"}, schema="coupons")
    db.rollback()
    assert _row(db, "bogus_key") is None
