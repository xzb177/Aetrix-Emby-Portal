"""r36：防盗链（play_sign）配置写入迁移到 config_service 统一编排层。

覆盖：
- config_service.SCHEMAS 里 "hotlink" 家族已挂名，三键校验口径与
  play_sign 旧手写实现逐字一致（开关 true/false、TTL 60~86400、
  白名单逗号串空串也存行）；
- play_sign.write_config / write_allowed_referers 改走统一通道后，
  写入回读/范围拒绝/描述落库行为不变；
- admin 端点 hotlink_update_config 的 400 文案路径（ConfigValueError
  是 ValueError 子类）。
"""
import pytest


def _fresh_db():
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from backend.database import Base
    from backend.emby_server import models as em  # noqa: F401 注册模型

    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)()


@pytest.fixture()
def db():
    d = _fresh_db()
    try:
        yield d
    finally:
        d.close()


class TestHotlinkSchemaRegistered:
    def test_schema_in_config_service(self):
        from backend import config_service

        specs = config_service.SCHEMAS.get("hotlink")
        assert specs is not None
        # 2026-10-21 A 类摘面板：hotlink_protection_enabled 已摘除注册表
        # （改走 env HOTLINK_PROTECTION_ENABLED，默认开）
        assert "hotlink_protection_enabled" not in specs
        assert specs["play_sign_ttl_seconds"] == ("int", 60, 86400)
        assert specs["play_allowed_referers"] == ("raw",)

    def test_ttl_range_rejected(self):
        from backend import config_service
        from backend.config_schema import ConfigValueError

        db = _fresh_db()
        try:
            with pytest.raises(ConfigValueError):
                config_service.save_config(
                    db, {"play_sign_ttl_seconds": 30}, schema="hotlink")
            with pytest.raises(ConfigValueError):
                config_service.save_config(
                    db, {"play_sign_ttl_seconds": 90000}, schema="hotlink")
            with pytest.raises(ConfigValueError):
                config_service.save_config(
                    db, {"play_sign_ttl_seconds": "abc"}, schema="hotlink")
            # ConfigValueError 必须是 ValueError 子类（端点 except 口径不变）
            assert issubclass(ConfigValueError, ValueError)
        finally:
            db.close()

    def test_unknown_key_rejected(self, db):
        from backend import config_service
        from backend.config_schema import ConfigValueError

        with pytest.raises(ConfigValueError):
            config_service.save_config(
                db, {"no_such_key": "1"}, schema="hotlink")


class TestWriteConfigViaUnifiedChannel:
    def test_roundtrip_and_immediate_read(self, db):
        from backend import models
        from backend.emby_server import play_sign

        # A-008（2026-10-30）：write_config 已不再接受 enabled 参数；
        # payload 的 enabled 是当前生效值（缺省开），不是写进去的
        payload = play_sign.write_config(db, play_sign_ttl=1800)
        assert payload["enabled"] is True
        assert payload["play_sign_ttl"] == 1800
        row = db.query(models.SystemConfig).filter(
            models.SystemConfig.key == play_sign.CONFIG_HOTLINK_ENABLED).first()
        assert row is None
        row2 = db.query(models.SystemConfig).filter(
            models.SystemConfig.key == play_sign.CONFIG_PLAY_SIGN_TTL).first()
        assert row2.value == "1800" and row2.description
        # 热读即时生效（统一通道写时已失效）
        assert play_sign.hotlink_enabled(db) is True
        assert play_sign.play_sign_ttl_seconds(db) == 1800
        # 存量行关掉后读口径反映关
        db.add(models.SystemConfig(
            key=play_sign.CONFIG_HOTLINK_ENABLED, value="false"))
        db.commit()
        from backend.integrations import store as _store
        _store.invalidate(play_sign.CONFIG_HOTLINK_ENABLED)
        assert play_sign.hotlink_enabled(db) is False

    def test_ttl_validation_unchanged(self, db):
        from backend.emby_server import play_sign

        with pytest.raises(ValueError):
            play_sign.write_config(db, play_sign_ttl=30)
        with pytest.raises(ValueError):
            play_sign.write_config(db, play_sign_ttl=90000)
        with pytest.raises(ValueError):
            play_sign.write_config(db, play_sign_ttl="abc")
        # 非法写入不留半截行
        from backend import models

        assert db.query(models.SystemConfig).filter(
            models.SystemConfig.key == play_sign.CONFIG_PLAY_SIGN_TTL).first() is None

    def test_referer_roundtrip(self, db):
        from backend import models
        from backend.emby_server import play_sign

        hosts = play_sign.write_allowed_referers(
            db, "https://cdn.example.com/x, bad host , ,cdn.example.com")
        assert hosts == ["cdn.example.com"]
        row = db.query(models.SystemConfig).filter(
            models.SystemConfig.key == play_sign.CONFIG_ALLOWED_REFERERS).first()
        assert row.value == "cdn.example.com" and row.description
        assert play_sign.get_allowed_referers(db) == ["cdn.example.com"]
        # 空白名单：旧口径落库 ""（不是删行），读侧空=不检查
        assert play_sign.write_allowed_referers(db, "  ") == []
        row = db.query(models.SystemConfig).filter(
            models.SystemConfig.key == play_sign.CONFIG_ALLOWED_REFERERS).first()
        assert row is not None and row.value == ""
        assert play_sign.get_allowed_referers(db) == []

    def test_enabled_word_style_kept(self, db):
        from backend import models
        from backend.emby_server import play_sign

        # 2026-10-21 A 类摘面板：write_config 不再写开关行；word 风格的
        # 读口径保留——存量行写 "false"/"true" 都能被正确读出
        play_sign.write_config(db, play_sign_ttl=900)
        row = db.query(models.SystemConfig).filter(
            models.SystemConfig.key == play_sign.CONFIG_HOTLINK_ENABLED).first()
        assert row is None
        db.add(models.SystemConfig(
            key=play_sign.CONFIG_HOTLINK_ENABLED, value="true"))
        db.commit()
        from backend.integrations import store as _store
        _store.invalidate(play_sign.CONFIG_HOTLINK_ENABLED)
        assert play_sign.hotlink_enabled(db) is True
