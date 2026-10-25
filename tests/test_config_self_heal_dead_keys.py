"""管理后台清理批 1：启动自愈删除已退役死键残留行。

只覆盖批 1 的行为：死键行被删、活键行保留、二次运行为空操作、
heal_system_config 确实接线了清理。生产读方为零的结论见
backend/config_self_heal.py 里 DEAD_CONFIG_KEYS 的注释；本文件不复测
整仓 grep，只测删除逻辑本身。
"""
from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend import config_self_heal
from backend import models as base_models
from backend.integrations import store
from backend.models import SystemConfig


@pytest.fixture()
def db():
    """隔离内存库；另外清掉 store 的进程级读缓存。"""
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
    base_models.Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    store.invalidate()
    yield session
    session.close()
    store.invalidate()


def _add(session, key, value="true"):
    session.add(SystemConfig(key=key, value=value, description="test"))
    session.commit()


def _keys(session):
    return sorted(row[0] for row in session.query(SystemConfig.key).all())


def test_prune_removes_dead_rows_keeps_live(db):
    _add(db, "playback_transcode_enabled", "true")
    _add(db, "panel_auto_scan", "false")
    _add(db, "strm_gen_enabled", "true")
    _add(db, "site_name", "x")

    pruned = config_self_heal._prune_dead_config_keys(db)

    assert sorted(pruned) == [
        "panel_auto_scan",
        "playback_transcode_enabled",
        "strm_gen_enabled",
    ]
    assert _keys(db) == ["site_name"]


def test_prune_idempotent(db):
    _add(db, "cdn_enabled", "false")

    assert config_self_heal._prune_dead_config_keys(db) == ["cdn_enabled"]
    assert config_self_heal._prune_dead_config_keys(db) == []
    assert _keys(db) == []


def test_heal_system_config_calls_prune(monkeypatch, db):
    """接线：heal_system_config 走完补缺＋迁移后确实跑清理。"""
    monkeypatch.setattr(config_self_heal, "collect_system_config_defaults",
                        lambda: [])
    monkeypatch.setattr(config_self_heal, "_migrate_legacy_meta_keys",
                        lambda _db: [])
    _add(db, "chat_points_enabled", "false")
    _add(db, "site_name", "x")

    config_self_heal.heal_system_config(db)

    assert _keys(db) == ["site_name"]


def test_dead_list_covers_audit_keys():
    """清单完整性：审计点名的 10 个非 strm 键一个不少。"""
    for key in (
        "panel_auto_scan",
        "panel_douban_first",
        "panel_night_scrape",
        "panel_vitality_guard",
        "chase_new_strm_enabled",
        "local_cache_enabled",
        "playback_prewarm_enabled",
        "cdn_enabled",
        "playback_transcode_enabled",
        "chat_points_enabled",
    ):
        assert key in config_self_heal.DEAD_CONFIG_KEYS
