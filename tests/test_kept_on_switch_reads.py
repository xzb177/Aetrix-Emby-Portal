"""留在面板、缺省开的三个自愈键：补行等于代码缺省，且库里的 "true" 读作开。"""
import os

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend import models as base_models
from backend.emby_server import auto_scan, change_watcher, enrich_worker
from backend.emby_server.metasources import config as ms_config
from backend.integrations import store


_ON_KEYS = (
    auto_scan.CONFIG_ENABLED,
    change_watcher.CONFIG_ENABLED,
    ms_config.CONFIG_ENABLED,
)


@pytest.fixture()
def db(monkeypatch):
    engine = create_engine("sqlite://")
    base_models.Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    monkeypatch.setattr("backend.database.SessionLocal", Session)
    session = Session()
    enrich_worker._night_cfg_cache["at"] = 0.0
    enrich_worker._night_cfg_cache["cfg"] = None
    night_env = (
        enrich_worker.ENRICH_NIGHT_START_ENV,
        enrich_worker.ENRICH_NIGHT_END_ENV,
        enrich_worker.ENRICH_NIGHT_TZ_ENV,
    )
    saved = {name: os.environ.pop(name) for name in night_env if name in os.environ}
    try:
        yield session
    finally:
        session.close()
        os.environ.update(saved)
        store.invalidate(
            *_ON_KEYS,
            enrich_worker.CFG_UNIFIED_FALLBACK,
            enrich_worker.CFG_EPISODE_NIGHT_ONLY,
        )
        enrich_worker._night_cfg_cache["at"] = 0.0
        enrich_worker._night_cfg_cache["cfg"] = None
        ms_config.invalidate()


def _row(db, key: str):
    return (
        db.query(base_models.SystemConfig)
        .filter(base_models.SystemConfig.key == key)
        .one()
    )


def test_self_heal_inserts_code_default_on(db):
    """缺行时自愈写入的值等于各键代码缺省（开 = "1"）。"""
    from backend.config_self_heal import collect_system_config_defaults, heal_system_config

    defaults = {key: value for key, value, _ in collect_system_config_defaults()}
    assert auto_scan.get_config(db)["enabled"] is True
    assert change_watcher.get_config(db)["enabled"] is True
    store.invalidate(ms_config.CONFIG_ENABLED)
    assert ms_config.read_config(db, []).enabled is True

    healed = heal_system_config(db)
    for key in _ON_KEYS:
        assert defaults[key] == "1"
        assert key in healed
        assert _row(db, key).value == defaults[key] == "1"


def test_true_row_reads_as_on(db):
    """库里存 "true"（经济设置的 bool 写法）时，各读取点都当作开。"""
    store.write_values(
        db,
        {
            auto_scan.CONFIG_ENABLED: "true",
            change_watcher.CONFIG_ENABLED: "true",
            enrich_worker.CFG_UNIFIED_FALLBACK: "true",
            enrich_worker.CFG_EPISODE_NIGHT_ONLY: "true",
        },
    )
    db.commit()
    store.invalidate(
        auto_scan.CONFIG_ENABLED,
        change_watcher.CONFIG_ENABLED,
        enrich_worker.CFG_UNIFIED_FALLBACK,
        enrich_worker.CFG_EPISODE_NIGHT_ONLY,
    )
    enrich_worker._night_cfg_cache["at"] = 0.0
    enrich_worker._night_cfg_cache["cfg"] = None

    assert auto_scan.get_config(db)["enabled"] is True
    assert change_watcher.get_config(db)["enabled"] is True
    assert enrich_worker._unified_fallback_flag_on() is True
    assert enrich_worker.get_unified_fallback_config(db)["enabled"] is True
    night = enrich_worker._episode_night_config(db)
    assert night["enabled"] is True
    assert night["from_env"] is False
