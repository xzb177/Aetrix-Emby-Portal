"""配置简化第 4 批 4-6：economy 家族迁移到 config_service.save_config。

钉住点（全部是与迁移前行为逐字一致的断言）：
1. 注册表与 ECONOMY_CONFIG_KEYS 逐键对齐，spec 口径与旧 economy_spec 一致；
2. 存库字节逐字一致：bool/int 走同一 normalize；str/secret 用 ("raw",) 原样存
   ——含"空串存行（不删行）""None 存成 'None'"这些历史口径；
3. 失效缺口补齐：旧代码只手动失效 stream_accel_*/vitality_*，其余键保存后
   60 秒内可能读旧值；迁移后全部键写时失效。
"""
import os
import sys

os.environ.setdefault("DATABASE_TYPE", "sqlite")
os.environ.setdefault("REDIS_ENABLED", "false")
sys.path.insert(0, ".")

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend import config_schema, models
from backend.api import admin_economy
from backend.integrations import store


@pytest.fixture()
def db():
    engine = create_engine("sqlite://")
    models.Base.metadata.create_all(engine)
    s = sessionmaker(bind=engine)()
    admin = models.WebUser(username="admin", password_hash="x", is_staff=True)
    s.add(admin)
    s.commit()
    yield s, admin
    s.close()


@pytest.fixture(autouse=True)
def _clean_store_cache():
    yield
    store.invalidate()


def _save(s, admin, settings):
    return admin_economy.economy_update_settings(
        admin_economy.EconomySettingsRequest(settings=settings),
        current_admin=admin, db=s)


def _row(s, key):
    return s.query(models.SystemConfig).filter(models.SystemConfig.key == key).first()


# ---------- 1. 注册表保真 ----------

def test_registry_covers_all_economy_keys():
    keys = set(admin_economy.ECONOMY_CONFIG_KEYS)
    assert set(config_schema.ECONOMY_CONFIG_SCHEMA) == keys, (
        "注册表必须与 ECONOMY_CONFIG_KEYS 逐键对齐")
    # 抽查：口径与旧 economy_spec 一致
    assert config_schema.ECONOMY_CONFIG_SCHEMA["invitation_rebate_percent"] == ("int", 0, 100)
    assert config_schema.ECONOMY_CONFIG_SCHEMA["checkin_penalty_pct"] == ("int", 0, 100)
    assert config_schema.ECONOMY_CONFIG_SCHEMA["series_merge_interval_sec"] == ("int", 3600, 604800)
    assert config_schema.ECONOMY_CONFIG_SCHEMA["series_merge_batch_limit"] == ("int", 10, 100000)
    assert config_schema.ECONOMY_CONFIG_SCHEMA["checkin_base_points"] == ("int", 0, 10 ** 9)
    assert config_schema.ECONOMY_CONFIG_SCHEMA["checkin_enabled"] == ("bool",)
    assert config_schema.ECONOMY_CONFIG_SCHEMA["payment_gateway_url"] == ("raw",)
    assert config_schema.ECONOMY_CONFIG_SCHEMA["payment_partner_key"] == ("raw",)
    assert "economy" in __import__("backend.config_service", fromlist=["x"]).SCHEMAS


# ---------- 2. 存库字节逐字一致 ----------

def test_byte_exact_bool_and_int(db):
    s, admin = db
    res = _save(s, admin, {
        "checkin_enabled": True, "exchange_enabled": False,
        "promotion_reward_enabled": 1,  # int 1 存 "1"（旧口径的 digit 直通）
        "checkin_base_points": "007",  # 归一化成 "7"
        "invitation_reward_points": " 5 ",
    })
    assert _row(s, "checkin_enabled").value == "true"
    assert _row(s, "exchange_enabled").value == "false"
    assert _row(s, "promotion_reward_enabled").value == "1"
    assert _row(s, "checkin_base_points").value == "7"
    assert _row(s, "invitation_reward_points").value == "5"
    assert res["changed"] == ["checkin_enabled", "exchange_enabled",
                              "promotion_reward_enabled", "checkin_base_points",
                              "invitation_reward_points"]


def test_str_raw_empty_stored_not_deleted(db):
    """str 键的空串是存行（不是删行）——与 ("str",) 的空串=删行不同，这是旧口径。"""
    s, admin = db
    _save(s, admin, {"site_url": "", "payment_gateway_url": "  https://x.io/  "})
    row = _row(s, "site_url")
    assert row is not None and row.value == ""
    # 原样存：前后空格不修
    assert _row(s, "payment_gateway_url").value == "  https://x.io/  "


def test_str_none_stored_as_none_string(db):
    """None 传进来：旧代码 str(None)="None" 存库——历史口径，原样保留。"""
    s, admin = db
    _save(s, admin, {"site_url": None})
    assert _row(s, "site_url").value == "None"


def test_secret_mask_keeps_row(db):
    s, admin = db
    _save(s, admin, {"payment_partner_key": "realkey"})
    res = _save(s, admin, {"payment_partner_key": "******"})
    assert _row(s, "payment_partner_key").value == "realkey"
    assert "payment_partner_key" not in res["changed"]
    # 空值同样保留
    res = _save(s, admin, {"payment_partner_key": ""})
    assert _row(s, "payment_partner_key").value == "realkey"
    assert "payment_partner_key" not in res["changed"]


def test_secret_real_value_stored_and_masked_in_changed(db):
    s, admin = db
    res = _save(s, admin, {"payment_partner_key": "abc123"})
    assert _row(s, "payment_partner_key").value == "abc123"
    # changed 只记键列表；审计里的值是 "(已更新)"（见下个断言的端点代码口径）
    changed = res["changed"]
    assert changed == ["payment_partner_key"]


def test_secret_masked_in_audit(db, monkeypatch):
    """secret 的原文不进审计日志：审计 details 里只记 "(已更新)"。"""
    s, admin = db
    calls = []
    monkeypatch.setattr(admin_economy, "_audit",
                        lambda db_, admin_, action, ttype=None, tid=None, details=None, ip=None:
                        calls.append((action, details)))
    _save(s, admin, {"payment_partner_key": "topsecret", "checkin_enabled": True})
    assert calls, "审计必须被调用"
    action, details = calls[-1]
    assert action == "economy_update_settings"
    assert details["payment_partner_key"] == "(已更新)"
    assert details["checkin_enabled"] == "true"
    assert "topsecret" not in str(calls)


def test_empty_int_deletes_row(db):
    s, admin = db
    _save(s, admin, {"checkin_base_points": "5"})
    assert _row(s, "checkin_base_points") is not None
    res = _save(s, admin, {"checkin_base_points": ""})
    assert _row(s, "checkin_base_points") is None
    assert res["changed"] == ["checkin_base_points"]


def test_invalid_rejected_atomically(db):
    s, admin = db
    # schema 层校验失败在直接调用层面抛 ConfigValueError；HTTP 400 的转换由
    # main.py 的统一 handler 承接（端到端见 test_config_value_error_400.py）。
    with pytest.raises(config_schema.ConfigValueError):
        _save(s, admin, {"invitation_reward_points": "5",
                         "invitation_rebate_percent": "101"})  # 超范围
    # 整批不落库：合法的那个也没写进去
    assert _row(s, "invitation_reward_points") is None
    assert _row(s, "invitation_rebate_percent") is None


def test_unknown_key_ignored(db):
    s, admin = db
    res = _save(s, admin, {"no_such_key": "1", "checkin_enabled": True})
    assert res["success"] is True
    assert res["changed"] == ["checkin_enabled"]
    assert _row(s, "no_such_key") is None


# ---------- 3. 失效缺口补齐 ----------

def test_invalidation_full_coverage(db):
    """invitation_reward_points 此前从不失效（旧代码只管 stream_accel/vitality）：
    旧值灌进热缓存 → 保存 → 必须立刻读到新值。"""
    s, admin = db
    s.add(models.SystemConfig(key="invitation_reward_points", value="10"))
    s.commit()
    assert store.get_value(s, "invitation_reward_points", "nope") == "10"  # 灌缓存
    _save(s, admin, {"invitation_reward_points": "20"})
    assert store.get_value(s, "invitation_reward_points", "nope") == "20"


def test_invalidation_on_delete(db):
    s, admin = db
    s.add(models.SystemConfig(key="checkin_base_points", value="5"))
    s.commit()
    assert store.get_value(s, "checkin_base_points", "nope") == "5"
    _save(s, admin, {"checkin_base_points": ""})  # 删行
    # 删行后读不到旧值（缺行 → 默认）
    assert store.get_value(s, "checkin_base_points", "gone") == "gone"


# ---------- 4. 端点层复合校验原样保留 ----------

def test_vitality_cross_field_still_rejected(db):
    s, admin = db
    with pytest.raises(HTTPException) as ei:
        _save(s, admin, {"vitality_max": "10", "vitality_limit_threshold": "20"})
    assert ei.value.status_code == 400
    assert _row(s, "vitality_max") is None


def test_checkin_min_gt_max_still_rejected(db):
    s, admin = db
    with pytest.raises(HTTPException):
        _save(s, admin, {"checkin_base_min": "9", "checkin_base_max": "2"})


def test_stream_accel_domain_linkage_still_enforced(db):
    """开加速开关必须配合法域名——联动校验仍在端点层。"""
    s, admin = db
    with pytest.raises(HTTPException) as ei:
        _save(s, admin, {"stream_accel_enabled": True,
                         "stream_accel_domain": "not a domain!!"})
    assert ei.value.status_code == 400
    assert _row(s, "stream_accel_enabled") is None
