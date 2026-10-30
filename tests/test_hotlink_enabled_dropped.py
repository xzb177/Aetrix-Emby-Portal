"""A-008：PUT /hotlink-config 的 enabled 字段已删除（2026-10-30）。

开关改走 env HOTLINK_PROTECTION_ENABLED（2026-10-21 A 类摘面板），
端点不再接受 enabled；审计只记真实写过的键，不许再出现
「记为已改、实际未改」。GET 回包的 enabled 仍如实返回当前生效值。
"""
import inspect
import json
import os

os.environ.setdefault("DATABASE_TYPE", "sqlite")
os.environ.setdefault("REDIS_ENABLED", "false")

from types import SimpleNamespace

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


def _staff():
    return SimpleNamespace(id=1, is_superuser=True, username="admin")


def _audit_details(d, action):
    from backend import models

    rows = (
        d.query(models.AdminLog)
        .filter(models.AdminLog.action == action)
        .order_by(models.AdminLog.id)
        .all()
    )
    out = []
    for row in rows:
        det = row.details
        if isinstance(det, str):
            det = json.loads(det)
        out.append(det or {})
    return out


class TestHotlinkEnabledDropped:
    def test_request_model_has_no_enabled_field(self):
        from backend.api.admin_economy import HotlinkConfigRequest

        assert "enabled" not in HotlinkConfigRequest.model_fields

    def test_write_config_signature_has_no_enabled(self):
        from backend.emby_server import play_sign

        assert "enabled" not in inspect.signature(play_sign.write_config).parameters

    def test_legacy_enabled_ignored_and_audit_truthful(self, db):
        from backend.api.admin_economy import (
            HotlinkConfigRequest,
            hotlink_update_config,
        )
        from backend.emby_server import play_sign

        # 直接 API 调用方仍发旧字段 enabled=false：必须被忽略，不能翻转开关，
        # 审计里也不许出现 enabled。
        req = HotlinkConfigRequest.model_validate(
            {"enabled": False, "play_sign_ttl": 900, "allowed_referers": "a.com"}
        )
        result = hotlink_update_config(req, _staff(), db)
        assert result["enabled"] is True  # 当前生效值（缺省开），不是调用方发来的 False
        assert play_sign.hotlink_enabled(db) is True
        details = _audit_details(db, "hotlink_update_config")
        assert len(details) == 1
        assert "enabled" not in details[0]
        assert details[0]["play_sign_ttl"] == 900
        assert details[0]["allowed_referers"] == "a.com"

    def test_audit_omits_unset_referers(self, db):
        from backend.api.admin_economy import (
            HotlinkConfigRequest,
            hotlink_update_config,
        )

        req = HotlinkConfigRequest.model_validate({"play_sign_ttl": 1200})
        hotlink_update_config(req, _staff(), db)
        details = _audit_details(db, "hotlink_update_config")
        assert len(details) == 1
        assert "enabled" not in details[0]
        assert details[0]["play_sign_ttl"] == 1200
        assert "allowed_referers" not in details[0]  # 没传=没改，不记

    def test_get_still_reports_real_enabled(self, db):
        from backend.emby_server import play_sign

        # GET 回包的 enabled 是展示用，仍如实返回当前生效值，不动它。
        assert play_sign.config_payload(db)["enabled"] is True
