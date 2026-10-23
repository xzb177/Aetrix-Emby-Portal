"""豆瓣开关/间隔单一存储（2026-10-23 运营参数去重）

此前豆瓣在多源页有两套配置：源表的 ``meta_source_enabled_douban`` /
``meta_source_rate_douban`` 与豆瓣卡的 ``douban_enabled`` /
``douban_min_interval``，生产里两处间隔已经不一致（1.0 vs 2.5），
引擎还把两个开关串联（都开才跑）。这里钉住：快照只认豆瓣自己的
两个键；保存时豆瓣限速只写 ``douban_min_interval``。
"""
from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend.emby_server.metasources import config as ms_config
from backend.emby_server.metasources import sources as ms_sources
from backend.integrations import store
from backend.models import SystemConfig


@pytest.fixture()
def db():
    from backend import models as base_models

    engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
    base_models.Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    store.invalidate()
    yield session
    session.close()
    store.invalidate()


def _set(db, key, value):
    db.add(SystemConfig(key=key, value=value))
    db.commit()
    store.invalidate()


def _get(db, key):
    row = db.query(SystemConfig).filter(SystemConfig.key == key).first()
    return row.value if row else None


def _douban(db):
    snap = ms_config.read_config(db, ms_sources.SPECS)
    return snap.by_id("douban")


def test_douban_enabled_comes_from_douban_key_not_meta_key(db):
    _set(db, "meta_source_enabled_douban", "1")
    _set(db, "douban_enabled", "0")
    assert _douban(db).enabled is False


def test_douban_enabled_ignores_meta_key_off(db):
    _set(db, "meta_source_enabled_douban", "0")
    _set(db, "douban_enabled", "1")
    assert _douban(db).enabled is True


def test_douban_rate_comes_from_douban_min_interval(db):
    _set(db, "meta_source_rate_douban", "9")
    _set(db, "douban_min_interval", "3.5")
    assert _douban(db).rate == 3.5


def test_other_sources_still_use_meta_keys(db):
    _set(db, "meta_source_enabled_bangumi", "0")
    _set(db, "meta_source_rate_bangumi", "7")
    snap = ms_config.read_config(db, ms_sources.SPECS)
    bangumi = snap.by_id("bangumi")
    assert bangumi.enabled is False
    assert bangumi.rate == 7.0


def test_write_config_douban_rate_writes_douban_key_only(db):
    ms_config.write_config(
        db, enabled=True, prefer_chinese=True,
        order=list(ms_config.DEFAULT_ORDER),
        rates={"douban": 4.0, "bangumi": 2.0})
    assert _get(db, "douban_min_interval") == "4.0"
    assert _get(db, "meta_source_rate_douban") is None
    assert _get(db, "meta_source_rate_bangumi") == "2.0"
