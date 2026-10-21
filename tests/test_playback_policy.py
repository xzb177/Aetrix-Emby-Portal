# -*- coding: utf-8 -*-
"""播放与客户端策略读写测试（2026-10-17）。

覆盖（r28 实锤回归）：
- 前端送短名（policy_payload 的字段名）时 write_policy 必须生效
  （此前只认长键名，管理后台「保存」静默失效）；
- 直接送长键名仍兼容；非法键被忽略；
- Drive 302 总开关（drive_direct_play_enabled）经 write_policy 写入后，
  playback_strategy.drive_direct_play_enabled 能读回同一值。
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
    """前端实际送的短名必须写入（r28 实锤：此前 applied={} 静默失效）。

    2026-10-21 A 类摘面板：transcode_enabled 不再经 write_policy 写
    （改走 env PLAYBACK_TRANSCODE_ENABLED / 存量行），短名里只有
    max_concurrent_transcodes 落库；转码开关的生效值由存量行决定。"""
    applied = pp.write_policy(memdb, {
        "transcode_enabled": False,
        "max_concurrent_transcodes": 5,
    })
    memdb.commit()
    assert applied == {"playback_max_concurrent_transcodes": "5"}
    assert pp.transcode_enabled(memdb) is True  # 未写行 → 缺省开
    assert pp.max_transcodes(memdb) == 5

    payload = pp.policy_payload(memdb)
    assert payload["transcode_enabled"] is True
    assert payload["max_concurrent_transcodes"] == 5
    # 存量行关掉后读口径立即反映
    memdb.add(app_models.SystemConfig(
        key="playback_transcode_enabled", value="false"))
    memdb.commit()
    store.invalidate("playback_transcode_enabled")
    assert pp.transcode_enabled(memdb) is False
    assert pp.policy_payload(memdb)["transcode_enabled"] is False
    # 2026-10-17 瘦身：客户端名单已删除，payload 不再含这两字段
    assert "blocked_agents" not in payload
    assert "allowed_agents" not in payload


def test_write_policy_long_keys_still_compatible(memdb):
    """数值键长名仍生效；开关长名 2026-10-21 起被忽略（摘面板）。"""
    applied = pp.write_policy(memdb, {"playback_max_concurrent_transcodes": "7"})
    memdb.commit()
    assert applied == {"playback_max_concurrent_transcodes": "7"}
    assert pp.max_transcodes(memdb) == 7
    applied = pp.write_policy(memdb, {"playback_transcode_enabled": "false"})
    memdb.commit()
    assert applied == {}
    assert pp.transcode_enabled(memdb) is True


def test_write_policy_ignores_unknown_keys(memdb):
    # transcode_enabled 2026-10-21 起与未知键同等对待：忽略不落库
    applied = pp.write_policy(memdb, {"no_such_key": "x", "transcode_enabled": True})
    memdb.commit()
    assert applied == {}


def test_removed_agent_keys_are_ignored(memdb):
    """已删除的客户端名单键：写进来被忽略，不落库。"""
    applied = pp.write_policy(memdb, {"blocked_agents": "Old-TV", "allowed_agents": "emby"})
    memdb.commit()
    assert applied == {}


def test_drive_direct_play_switch_roundtrip(memdb):
    """Drive 302 总开关：2026-10-21 摘面板后 write_policy 不再写它，
    生效值由存量行 / env DRIVE_DIRECT_PLAY_ENABLED / 缺省（开）决定，
    policy_payload 与 playback_strategy 读同一口径。"""
    # 缺省开（与 playback_strategy.drive_direct_play_enabled 缺省一致）
    assert pp.policy_payload(memdb)["drive_direct_play_enabled"] is True
    assert ps.drive_direct_play_enabled(memdb) is True

    applied = pp.write_policy(memdb, {"drive_direct_play_enabled": False})
    memdb.commit()
    assert applied == {}
    assert ps.drive_direct_play_enabled(memdb) is True

    memdb.add(app_models.SystemConfig(
        key=ps.KEY_DRIVE_DIRECT_PLAY, value="false"))
    memdb.commit()
    store.invalidate(ps.KEY_DRIVE_DIRECT_PLAY)
    assert pp.policy_payload(memdb)["drive_direct_play_enabled"] is False
    assert ps.drive_direct_play_enabled(memdb) is False


def test_drive_direct_play_in_policy_keys(memdb):
    """2026-10-21 摘面板：该键退出可写白名单（POLICY_KEYS / FIELD_TO_KEY），
    但缺省登记保留（配置自愈播种 + 读口径缺省）。"""
    assert ps.KEY_DRIVE_DIRECT_PLAY not in pp.POLICY_KEYS
    assert "drive_direct_play_enabled" not in pp.FIELD_TO_KEY
    assert pp.POLICY_DEFAULTS[ps.KEY_DRIVE_DIRECT_PLAY] == "true"
