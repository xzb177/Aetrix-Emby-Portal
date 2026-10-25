"""管理后台/数据库是这几处开关的权威来源。"""
import os

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend import models as base_models
from backend.api import admin_economy, admin_scrape
from backend.emby_server import auto_scan, change_watcher, db_backup, enrich_worker
from backend.emby_server.metasources import config as ms_config
from backend.integrations import store
from backend.lottery import is_enabled


@pytest.fixture()
def db(monkeypatch):
    engine = create_engine("sqlite://")
    # 追新轮询会查媒体库表；导入模型后 create_all 才建得出来。
    from backend.emby_server import models as em  # noqa: F401

    base_models.Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    monkeypatch.setattr("backend.database.SessionLocal", Session)
    monkeypatch.setattr(change_watcher, "SessionLocal", Session)
    session = Session()
    enrich_worker._night_cfg_cache["at"] = 0.0
    enrich_worker._night_cfg_cache["cfg"] = None
    enrich_worker._ep_fields_cfg_cache["at"] = 0.0
    enrich_worker._ep_fields_cfg_cache["cfg"] = None
    saved = os.environ.pop("ENRICH_EPISODE_NIGHT_ONLY", None)
    saved_lottery = os.environ.pop("LOTTERY_ENABLED", None)
    try:
        yield session
    finally:
        session.close()
        if saved is None:
            os.environ.pop("ENRICH_EPISODE_NIGHT_ONLY", None)
        else:
            os.environ["ENRICH_EPISODE_NIGHT_ONLY"] = saved
        if saved_lottery is None:
            os.environ.pop("LOTTERY_ENABLED", None)
        else:
            os.environ["LOTTERY_ENABLED"] = saved_lottery
        store.invalidate()
        enrich_worker._night_cfg_cache["at"] = 0.0
        enrich_worker._night_cfg_cache["cfg"] = None
        enrich_worker._ep_fields_cfg_cache["at"] = 0.0
        enrich_worker._ep_fields_cfg_cache["cfg"] = None
        ms_config.invalidate()


def _value(db, key: str) -> str | None:
    row = (
        db.query(base_models.SystemConfig)
        .filter(base_models.SystemConfig.key == key)
        .first()
    )
    return None if row is None else row.value


def test_night_only_ignores_env(db):
    os.environ["ENRICH_EPISODE_NIGHT_ONLY"] = "0"
    store.write_values(db, {enrich_worker.CFG_EPISODE_NIGHT_ONLY: "true"})
    db.commit()
    store.invalidate(enrich_worker.CFG_EPISODE_NIGHT_ONLY)
    enrich_worker._night_cfg_cache["cfg"] = None
    cfg = enrich_worker._episode_night_config(db)
    assert cfg["enabled"] is True
    assert cfg["from_env"] is False

    store.write_values(db, {enrich_worker.CFG_EPISODE_NIGHT_ONLY: "0"})
    db.commit()
    store.invalidate(enrich_worker.CFG_EPISODE_NIGHT_ONLY)
    enrich_worker._night_cfg_cache["cfg"] = None
    os.environ["ENRICH_EPISODE_NIGHT_ONLY"] = "1"
    cfg = enrich_worker._episode_night_config(db)
    assert cfg["enabled"] is False
    assert cfg["from_env"] is False


def test_lottery_enabled_ignores_env(db):
    os.environ["LOTTERY_ENABLED"] = "0"
    assert is_enabled(db) is True
    db.add(base_models.SystemConfig(key="lottery_enabled", value="1"))
    db.commit()
    assert is_enabled(db) is True
    row = db.query(base_models.SystemConfig).filter_by(key="lottery_enabled").one()
    row.value = "0"
    db.commit()
    os.environ["LOTTERY_ENABLED"] = "1"
    assert is_enabled(db) is False


def test_omitted_enabled_preserves_stored_value(db):
    store.write_values(db, {
        auto_scan.CONFIG_ENABLED: "1",
        change_watcher.CONFIG_ENABLED: "1",
        enrich_worker.CFG_EPISODE_NIGHT_ONLY: "1",
        enrich_worker.CFG_UNIFIED_FALLBACK: "1",
        ms_config.CONFIG_ENABLED: "1",
    })
    db.commit()
    store.invalidate()

    admin_scrape.save_auto_scan(
        admin_scrape.AutoScanSaveRequest(time="04:00"), staff=None, db=db)
    admin_scrape.save_chase_new(
        admin_scrape.ChaseNewSaveRequest(interval=15), staff=None, db=db)
    admin_scrape.save_enrich_episode_night(
        admin_scrape.EnrichEpisodeNightSaveRequest(night_start="03:00"),
        staff=None, db=db)
    admin_scrape.save_unified_fallback(
        admin_scrape._UnifiedFallbackBody(), staff=None, db=db)
    admin_scrape.save_meta_sources(
        admin_scrape.MetaSourcesSaveRequest(), staff=None, db=db)

    assert auto_scan.get_config(db)["enabled"] is True
    assert change_watcher.get_config(db)["enabled"] is True
    enrich_worker._night_cfg_cache["cfg"] = None
    assert enrich_worker.get_episode_night_config(db)["enabled"] is True
    assert enrich_worker.get_unified_fallback_config(db)["enabled"] is True
    ms_config.invalidate()
    store.invalidate(ms_config.CONFIG_ENABLED)
    assert ms_config.read_config(db, []).enabled is True


def test_economy_get_missing_on_switches_use_code_default(db):
    out = admin_economy.economy_get_settings(current_admin=None, db=db)["settings"]
    assert out["promotion_reward_enabled"] == "true"
    assert out["stream_accel_enabled"] == "true"
    assert out["checkin_enabled"] == ""

    db.add(base_models.SystemConfig(key="stream_accel_enabled", value="false"))
    db.commit()
    out = admin_economy.economy_get_settings(current_admin=None, db=db)["settings"]
    assert out["stream_accel_enabled"] == "false"


def test_true_row_is_on_at_remaining_read_sites(db, monkeypatch):
    store.write_values(db, {
        change_watcher.CONFIG_ENABLED: "true",
        db_backup.CONFIG_ENABLED: "true",
        enrich_worker.CFG_DOUBAN_BAN_DOWNGRADE: "true",
        enrich_worker.CFG_EPISODE_TITLE: "true",
    })
    db.commit()
    store.invalidate()

    change_watcher._check_once()
    assert _value(db, change_watcher.CONFIG_LAST_CHECK)

    monkeypatch.setattr(db_backup, "list_backups", lambda: [])
    monkeypatch.setattr(db_backup, "get_db_file_path", lambda: db_backup.Path("/tmp/aetrix.db"))
    assert db_backup.get_config(db)["enabled"] is True

    assert enrich_worker.get_douban_ban_downgrade_config(db)["enabled"] is True
    assert enrich_worker._douban_ban_downgrade_flag_on() is True

    for name in (
        enrich_worker.ENRICH_EPISODE_TITLE_ENV,
        enrich_worker.ENRICH_EPISODE_OVERVIEW_ENV,
        enrich_worker.ENRICH_EPISODE_STILL_ENV,
    ):
        os.environ.pop(name, None)
    enrich_worker._ep_fields_cfg_cache["cfg"] = None
    assert enrich_worker._episode_field_config(db)["title"] is True
