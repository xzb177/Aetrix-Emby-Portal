# -*- coding: utf-8 -*-
"""播放与客户端策略读写测试（2026-10-17）。

覆盖（r28 实锤回归）：
- 前端送短名（policy_payload 的字段名）时 write_policy 必须生效
  （此前只认长键名，管理后台「保存」静默失效）；
- 直接送长键名仍兼容；非法键被忽略；
- Drive 302 总开关（drive_direct_play_enabled）经 write_policy 写入后，
  playback_strategy.drive_direct_play_enabled 能读回同一值。
- （2026-10-17 转码删除）：playback_transcode_enabled /
  playback_max_concurrent_transcodes 两键已作废——送进来被忽略，不落库；
  transcode_enabled()/max_transcodes() helper 已移除。
全部离线：只用内存 sqlite + SystemConfig 单表。
"""
import os

os.environ.setdefault("DATABASE_TYPE", "sqlite")
os.environ.setdefault("REDIS_ENABLED", "false")

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend import models as app_models
from backend import playback_policy as pp
from backend.database import Base
from backend.emby_server import playback_strategy as ps
from backend.integrations import store


@pytest.fixture()
def memdb():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine, tables=[app_models.SystemConfig.__table__])
    session = sessionmaker(bind=engine)()
    store.invalidate()
    yield session
    store.invalidate()
    session.close()


def test_write_policy_short_names_take_effect(memdb):
    """前端实际送的短名必须写入（r28 实锤：此前 applied={} 静默失效）。"""
    applied = pp.write_policy(memdb, {"drive_direct_play_enabled": False})
    memdb.commit()
    assert applied == {ps.KEY_DRIVE_DIRECT_PLAY: "false"}
    assert ps.drive_direct_play_enabled(memdb) is False

    payload = pp.policy_payload(memdb)
    assert payload["drive_direct_play_enabled"] is False
    # 2026-10-17 瘦身：客户端名单已删除，payload 不再含这两字段
    assert "blocked_agents" not in payload
    assert "allowed_agents" not in payload
    # 2026-10-17 转码删除：payload 不再含转码键
    assert "transcode_enabled" not in payload
    assert "max_concurrent_transcodes" not in payload


def test_write_policy_long_keys_still_compatible(memdb):
    """直接送 SystemConfig 长键名仍生效（兼容旧调用方）。"""
    applied = pp.write_policy(memdb, {ps.KEY_DRIVE_DIRECT_PLAY: "false"})
    memdb.commit()
    assert applied == {ps.KEY_DRIVE_DIRECT_PLAY: "false"}
    assert ps.drive_direct_play_enabled(memdb) is False


def test_write_policy_ignores_unknown_keys(memdb):
    applied = pp.write_policy(memdb, {"no_such_key": "x",
                                      "drive_direct_play_enabled": True})
    memdb.commit()
    assert applied == {ps.KEY_DRIVE_DIRECT_PLAY: "true"}


def test_removed_agent_keys_are_ignored(memdb):
    """已删除的客户端名单键：写进来被忽略，不落库。"""
    applied = pp.write_policy(memdb, {"blocked_agents": "Old-TV", "allowed_agents": "emby"})
    memdb.commit()
    assert applied == {}


def test_removed_transcode_keys_are_ignored(memdb):
    """2026-10-17 转码删除：两转码键（短名/长名）写进来一律被忽略，不落库。"""
    applied = pp.write_policy(memdb, {
        "transcode_enabled": False,
        "max_concurrent_transcodes": 5,
        "playback_transcode_enabled": "false",
        "playback_max_concurrent_transcodes": "5",
    })
    memdb.commit()
    assert applied == {}
    rows = {r.key for r in memdb.query(app_models.SystemConfig).all()}
    assert not (rows & {"playback_transcode_enabled",
                        "playback_max_concurrent_transcodes"})
    # helper 已随模块删除，不许再有残留引用
    assert not hasattr(pp, "transcode_enabled")
    assert not hasattr(pp, "max_transcodes")
    assert "playback_transcode_enabled" not in pp.POLICY_KEYS


def test_removed_agent_keys_are_ignored(memdb):
    """已删除的客户端名单键：写进来被忽略，不落库。"""
    applied = pp.write_policy(memdb, {"blocked_agents": "Old-TV", "allowed_agents": "emby"})
    memdb.commit()
    assert applied == {}


def test_drive_direct_play_switch_roundtrip(memdb):
    """Drive 302 总开关：写短名 -> playback_strategy 读回同一值。"""
    # 缺省开（与 playback_strategy.drive_direct_play_enabled 缺省一致）
    assert pp.policy_payload(memdb)["drive_direct_play_enabled"] is True
    assert ps.drive_direct_play_enabled(memdb) is True

    applied = pp.write_policy(memdb, {"drive_direct_play_enabled": False})
    memdb.commit()
    assert applied == {ps.KEY_DRIVE_DIRECT_PLAY: "false"}
    assert pp.policy_payload(memdb)["drive_direct_play_enabled"] is False
    assert ps.drive_direct_play_enabled(memdb) is False

    pp.write_policy(memdb, {"drive_direct_play_enabled": True})
    memdb.commit()
    assert ps.drive_direct_play_enabled(memdb) is True


def test_drive_direct_play_in_policy_keys(memdb):
    """白名单与默认值登记（配置自愈/管理后台 keys 回显用）。"""
    assert pp.POLICY_KEYS[ps.KEY_DRIVE_DIRECT_PLAY] == "bool"
    assert pp.POLICY_DEFAULTS[ps.KEY_DRIVE_DIRECT_PLAY] == "true"
