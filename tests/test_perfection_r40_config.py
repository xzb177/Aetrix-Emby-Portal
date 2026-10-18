"""r40 配置纪律增量复查回归测试：F1–F5。

F1: emby_view_unlock_* 两键此前有后端读写接口但管理后台无入口
    （本轮在 WelfarePoints.vue 补「查看权限解锁」卡片；后端接口行为不变，
    此处锁定 view_unlock 家族注册与接口读写回环）。
F2: max_pending_orders_per_user 已在白名单+schema，前端 Settings.vue 补字段
    （此处锁定后端读写通道完整）。
F3: TG 一键登录 build_login_url 改读正统键 site_url（历史键 site_base_url
    仅回退兼容）。
F4: 删除 tg_bot_commands_enabled 死开关（bot_enabled 是唯一总开关）。
F5: code_default_plan_id 纳入 economy 家族可配（schema+白名单），
    codes._resolve_plan 能读到经正统通道写入的值。
"""
import os

os.environ.setdefault("DATABASE_TYPE", "sqlite")
os.environ.setdefault("REDIS_ENABLED", "false")
os.environ.setdefault("SECRET_KEY", "test-secret-key-must-be-at-least-32-chars")

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend import config_schema, config_service, models
from backend.api import admin_economy
from backend.integrations import store
from backend.tg_bot import login_token


@pytest.fixture()
def db():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    models.Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine, autoflush=False, autocommit=False)()
    login_token._mem_tokens.clear()
    login_token._mem_rate.clear()
    try:
        yield session
    finally:
        session.close()


@pytest.fixture(autouse=True)
def _clean_store_cache():
    yield
    store.invalidate()


def _row(db, key):
    return db.query(models.SystemConfig).filter(models.SystemConfig.key == key).first()


def _make_user(db, telegram_id: int):
    u = models.WebUser(username=f"r40_{telegram_id}", telegram_id=telegram_id,
                       is_active=True, password_hash="x")
    db.add(u)
    db.commit()
    db.refresh(u)
    return u


# ---------- F3：site_url 为正统键 ----------

def test_build_login_url_uses_site_url(db):
    user = _make_user(db, 4001)
    store.write_values(db, {"site_url": "https://aetrix.example.com"})
    db.commit()
    url = login_token.build_login_url(db, user)
    assert url is not None and url.startswith("https://aetrix.example.com/tg-login?token=")


def test_build_login_url_falls_back_to_legacy_key(db):
    user = _make_user(db, 4002)
    db.add(models.SystemConfig(key="site_base_url", value="https://legacy.example.com"))
    db.commit()
    url = login_token.build_login_url(db, user)
    assert url is not None and url.startswith("https://legacy.example.com/tg-login?token=")


def test_build_login_url_prefers_site_url_over_legacy(db):
    user = _make_user(db, 4003)
    store.write_values(db, {"site_url": "https://new.example.com"})
    db.add(models.SystemConfig(key="site_base_url", value="https://legacy.example.com"))
    db.commit()
    url = login_token.build_login_url(db, user)
    assert url is not None and url.startswith("https://new.example.com/tg-login?token=")


def test_build_login_url_none_when_both_unset(db):
    user = _make_user(db, 4004)
    assert login_token.build_login_url(db, user) is None


# ---------- F4：死开关已删除 ----------

def test_poller_has_no_dead_switch():
    import pathlib
    src = pathlib.Path("backend/tg_bot/poller.py").read_text(encoding="utf-8")
    # 死开关的读调用必须消失（注释里提及键名做说明是允许的）
    assert 'get_value(db, "tg_bot_commands_enabled"' not in src
    assert "get_value(db, 'tg_bot_commands_enabled'" not in src


# ---------- F5：code_default_plan_id 可配 ----------

def test_code_default_plan_id_in_whitelist_and_schema(db):
    assert admin_economy.ECONOMY_CONFIG_KEYS["code_default_plan_id"] == "int"
    assert "code_default_plan_id" in config_schema.ECONOMY_CONFIG_SCHEMA


def test_code_default_plan_id_roundtrip_and_resolve(db):
    from backend import codes
    plan = models.SubscriptionPlan(name="r40-plan", price=10, duration_days=30,
                                   realm_id=None, is_active=True, sort_order=0)
    db.add(plan)
    db.commit()
    db.refresh(plan)
    config_service.save_config(db, {"code_default_plan_id": str(plan.id)}, schema="economy")
    db.commit()
    assert _row(db, "code_default_plan_id").value == str(plan.id)
    # realm_id=None 的套餐对所有服可用（_resolve_plan 的 plan.realm_id is None 分支）
    got = codes._resolve_plan(db, realm_id=1)
    assert got is not None and got.id == plan.id


def test_code_default_plan_id_empty_means_unset(db):
    config_service.save_config(db, {"code_default_plan_id": ""}, schema="economy")
    db.commit()
    assert _row(db, "code_default_plan_id") is None


# ---------- F1/F2：后端通道完整（前端入口由 type-check 覆盖） ----------

def test_view_unlock_family_registered(db):
    assert "view_unlock" in config_service.SCHEMAS
    assert "emby_view_unlock_points" in config_schema.VIEW_UNLOCK_CONFIG_SCHEMA
    assert "emby_view_unlock_days" in config_schema.VIEW_UNLOCK_CONFIG_SCHEMA


def test_max_pending_orders_roundtrip(db):
    assert admin_economy.ECONOMY_CONFIG_KEYS["max_pending_orders_per_user"] == "int"
    config_service.save_config(db, {"max_pending_orders_per_user": "8"}, schema="economy")
    db.commit()
    assert _row(db, "max_pending_orders_per_user").value == "8"
