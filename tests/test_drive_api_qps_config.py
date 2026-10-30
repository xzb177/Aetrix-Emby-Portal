"""Drive API 全局 QPS 配置接线测试（2026-10-30）。

覆盖 drive_changes 的 SystemConfig 接线（key=drive_api_qps）：
- 无配置行 → 读取回默认 8、refresh 后限速器保持 8
- 配置 16 → refresh 后限速器被设为 16
- 越界值（99 / 0）→ 读取侧钳 [1, 20]；垃圾值 → 回默认 8
- 配置读取异常 → 回落 8 且 refresh 不抛
- refresh 节流：非 force 连调不重复读配置
- config_service 家族校验：越界保存被拒；write_config 落库且本进程立即生效
"""
from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend import config_schema, config_service
from backend import models as base_models
from backend.emby_server import drive_changes as dc
from backend.integrations import store


@pytest.fixture()
def db():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False},
        poolclass=StaticPool)
    base_models.Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    store.invalidate()
    yield session
    session.close()
    store.invalidate()


@pytest.fixture(autouse=True)
def _reset_limiter():
    dc.set_api_qps(8.0)
    if hasattr(dc, "_api_qps_last_refresh"):
        dc._api_qps_last_refresh = 0.0
    yield
    dc.set_api_qps(8.0)


def _set_sysconfig(db, key, value):
    row = (db.query(base_models.SystemConfig)
           .filter(base_models.SystemConfig.key == key).first())
    if row is None:
        db.add(base_models.SystemConfig(key=key, value=value))
    else:
        row.value = value
    db.commit()
    store.invalidate()


class TestConfiguredApiQps:
    def test_missing_row_defaults_8(self, db):
        assert dc.configured_api_qps(db) == 8.0
        assert dc.refresh_api_qps(db, force=True) == 8.0
        assert dc._api_limiter._qps == 8.0

    def test_config_16_applies(self, db):
        _set_sysconfig(db, "drive_api_qps", "16")
        assert dc.refresh_api_qps(db, force=True) == 16.0
        assert dc._api_limiter._qps == 16.0

    def test_out_of_range_clamped(self, db):
        _set_sysconfig(db, "drive_api_qps", "99")
        assert dc.configured_api_qps(db) == 20.0
        _set_sysconfig(db, "drive_api_qps", "0")
        assert dc.configured_api_qps(db) == 1.0

    def test_garbage_value_defaults_8(self, db):
        _set_sysconfig(db, "drive_api_qps", "abc")
        assert dc.configured_api_qps(db) == 8.0

    def test_read_failure_falls_back(self, db, monkeypatch):
        def _boom(*args, **kwargs):
            raise RuntimeError("db down")

        monkeypatch.setattr(store, "get_value", _boom)
        assert dc.configured_api_qps(db) == 8.0
        # refresh 不许把异常抛给 _api_get 调用方
        assert dc.refresh_api_qps(db, force=True) == 8.0

    def test_refresh_throttled(self, db, monkeypatch):
        calls = []
        orig = store.get_value

        def counting(db_, key, default="", **kwargs):
            calls.append(key)
            return orig(db_, key, default, **kwargs)

        monkeypatch.setattr(store, "get_value", counting)
        dc.refresh_api_qps(db, force=True)
        n = len(calls)
        assert n >= 1
        dc.refresh_api_qps(db)  # 非 force 且刚刷过 → 节流跳过，不再读配置
        assert len(calls) == n


class TestWritePath:
    def test_payload_defaults(self, db):
        payload = dc.config_payload(db)
        assert payload["qps"] == 8.0
        assert payload["defaults"]["qps"] == 8.0

    def test_save_rejects_out_of_range(self, db):
        with pytest.raises(config_schema.ConfigValueError):
            config_service.save_config(
                db, {"drive_api_qps": 25}, schema="drive_api")
        with pytest.raises(config_schema.ConfigValueError):
            config_service.save_config(
                db, {"drive_api_qps": 0}, schema="drive_api")

    def test_write_config_applies_immediately(self, db):
        payload = dc.write_config(db, qps=16)
        assert payload["qps"] == 16.0
        assert dc._api_limiter._qps == 16.0
        assert dc.configured_api_qps(db) == 16.0
