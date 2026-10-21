"""第 4 批 4-3：config_service.save_config 统一写入通道的单测。

save_config 的契约：逐键按家族 schema 校验（任一非法整批不落库）→
store.write_values 落库（写时即失效）→ 空值删行 → 可选审计钩子；不提交，
提交由调用方按现有事务边界决定。
"""
import os

os.environ.setdefault("DATABASE_TYPE", "sqlite")
os.environ.setdefault("REDIS_ENABLED", "false")

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend import config_service, models
from backend.config_schema import ConfigValueError
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
    yield
    store.invalidate()


def _row(db, key):
    return db.query(models.SystemConfig).filter(models.SystemConfig.key == key).first()


class TestSaveConfigValidation:
    def test_unknown_family_rejected(self, db):
        with pytest.raises(ConfigValueError):
            config_service.save_config(db, {"a": "1"}, schema="nope")

    def test_unknown_key_rejected_whole_batch(self, db):
        db.add(models.SystemConfig(key="alert_check_interval_sec", value="600"))
        db.commit()
        with pytest.raises(ConfigValueError):
            config_service.save_config(
                db,
                {"alert_check_interval_sec": 300, "bogus_key": "x"},
                schema="alerts",
            )
        db.rollback()
        # 整批不落库：旧行未被改动，也没产生新行
        assert _row(db, "alert_check_interval_sec").value == "600"
        assert _row(db, "bogus_key") is None

    def test_bad_value_rejected_whole_batch(self, db):
        with pytest.raises(ConfigValueError):
            config_service.save_config(
                db, {"alert_check_interval_sec": 30}, schema="alerts"
            )
        assert _row(db, "alert_check_interval_sec") is None

    def test_non_dict_rejected(self, db):
        with pytest.raises(ConfigValueError):
            config_service.save_config(db, [("a", "1")], schema="alerts")


class TestSaveConfigWrite:
    def test_write_and_invalidate(self, db):
        # 旧值灌进热缓存，模拟 4-1 的 bug 场景
        db.add(models.SystemConfig(key="alert_check_interval_sec", value="600"))
        db.commit()
        assert store.get_value(db, "alert_check_interval_sec") == "600"
        keys = config_service.save_config(
            db, {"alert_check_interval_sec": 300}, schema="alerts"
        )
        db.commit()
        assert keys == ["alert_check_interval_sec"]
        # 立读新值：写时即失效，不被旧缓存挡住
        assert store.get_value(db, "alert_check_interval_sec") == "300"

    def test_bool_digit_style_preserved(self, db):
        # 2026-10-21 A 类摘面板：原向量 system_alerts_enabled 已摘除注册表，
        # 改用仍保留的 digit 风格键 welfare_require_tg_bind（welfare 家族）
        config_service.save_config(db, {"welfare_require_tg_bind": True}, schema="welfare")
        db.commit()
        assert _row(db, "welfare_require_tg_bind").value == "1"

    def test_empty_string_deletes_row(self, db):
        # 统一通道的「未设置」语义：空串=删行（与 economy/welfare 口径一致）
        db.add(models.SystemConfig(key="webhook_alert_secret", value="s3cr3t"))
        db.commit()
        config_service.save_config(db, {"webhook_alert_secret": ""}, schema="alerts")
        db.commit()
        assert _row(db, "webhook_alert_secret") is None

    def test_descriptions_only_on_new_rows(self, db):
        db.add(models.SystemConfig(
            key="alert_check_interval_sec", value="600", description="旧描述"))
        db.commit()
        config_service.save_config(
            db, {"alert_check_interval_sec": 300}, schema="alerts",
            descriptions={"alert_check_interval_sec": "新描述"})
        db.commit()
        # 存量行只改值，不改描述（与旧 _upsert 行为一致）
        assert _row(db, "alert_check_interval_sec").description == "旧描述"

    def test_audit_hook_called_with_normalized(self, db):
        seen = []
        config_service.save_config(
            db, {"alert_check_interval_sec": 120}, schema="alerts",
            audit=lambda family, normalized: seen.append((family, normalized)))
        assert seen == [("alerts", {"alert_check_interval_sec": "120"})]

    def test_enum_and_str_kinds(self, db):
        config_service.save_config(
            db, {"registration_mode": "closed",
                 "registration_closed_message": "维护中"},
            schema="registration")
        db.commit()
        assert _row(db, "registration_mode").value == "closed"
        assert _row(db, "registration_closed_message").value == "维护中"
        with pytest.raises(ConfigValueError):
            config_service.save_config(
                db, {"registration_mode": "invite-only"}, schema="registration")
