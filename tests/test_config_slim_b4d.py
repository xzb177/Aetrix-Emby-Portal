# -*- coding: utf-8 -*-
"""配置简化第 4 批 4-5：playback_policy / access_guard / play_sign / cdn 四个家族
迁移到 config_service.save_config 后的回环用例。

每个家族都钉三件事：
1. 写后立读（走各自的读口径 policy_payload/config_payload）拿到新值；
2. 存字节断言（SystemConfig 行里存的到底是什么字符串）；
3. 非法输入拒绝且整批不落库（整批不写半截）。

全部离线：内存 sqlite + SystemConfig 单表；store 热缓存用例前后清零防交叉污染。
"""
import os

os.environ.setdefault("DATABASE_TYPE", "sqlite")
os.environ.setdefault("REDIS_ENABLED", "false")

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend import access_guard as ag
from backend import config_service
from backend import models as app_models
from backend import playback_policy as pp
from backend.database import Base
from backend.emby_server import cdn
from backend.emby_server import play_sign as ps
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


def _row(memdb, key):
    return (
        memdb.query(app_models.SystemConfig)
        .filter(app_models.SystemConfig.key == key)
        .first()
    )


# ==================== playback_policy ====================


def test_pp_roundtrip_and_bytes(memdb):
    applied = pp.write_policy(
        memdb,
        {
            "transcode_enabled": "yes",  # bool 归一：yes -> true
            "max_concurrent_transcodes": "4",
            "blocked_agents": "BadBot",
            "allowed_agents": "",  # 空串 = 未设置：删行，读侧 [] 等价
            "drive_direct_play_enabled": True,
        },
    )
    memdb.commit()
    assert applied[pp.CONFIG_TRANSCODE_ENABLED] == "true"
    assert applied[pp.CONFIG_MAX_TRANSCODES] == "4"
    assert applied[pp.CONFIG_BLOCKED_AGENTS] == "BadBot"

    payload = pp.policy_payload(memdb)
    assert payload["transcode_enabled"] is True
    assert payload["max_concurrent_transcodes"] == 4
    assert payload["drive_direct_play_enabled"] is True

    # 存字节：bool 存 true/false，int 存去钳制后的十进制串
    assert _row(memdb, pp.CONFIG_TRANSCODE_ENABLED).value == "true"
    assert _row(memdb, pp.CONFIG_MAX_TRANSCODES).value == "4"
    # 空串走统一「未设置」语义：删行（旧代码写空串行，读侧 _agent_list 口径一致）
    assert _row(memdb, pp.CONFIG_ALLOWED_AGENTS) is None


def test_pp_invalid_int_and_unknown_key_keep_old(memdb):
    pp.write_policy(memdb, {"max_concurrent_transcodes": 2})
    memdb.commit()
    applied = pp.write_policy(
        memdb, {"max_concurrent_transcodes": "abc", "no_such_key": 1}
    )
    memdb.commit()
    assert applied == {}
    # 非法值保持原值不动（行为与收敛前一致）
    assert _row(memdb, pp.CONFIG_MAX_TRANSCODES).value == "2"


def test_pp_bool_coercion_bytes(memdb):
    applied = pp.write_policy(
        memdb, {"transcode_enabled": "nope", "drive_direct_play_enabled": "on"}
    )
    memdb.commit()
    assert applied[pp.CONFIG_TRANSCODE_ENABLED] == "false"
    assert applied[pp.KEY_DRIVE_DIRECT_PLAY] == "true"
    assert _row(memdb, pp.CONFIG_TRANSCODE_ENABLED).value == "false"


def test_pp_save_after_write_reads_new_value(memdb):
    """写完 store 热缓存立即失效：立读拿到新值（4-1 式回环）。"""
    assert pp.transcode_enabled(memdb) is True  # 默认
    pp.write_policy(memdb, {"transcode_enabled": False})
    memdb.commit()
    assert pp.transcode_enabled(memdb) is False


# ==================== access_guard ====================


def test_ag_roundtrip_normalization_bytes(memdb):
    applied = ag.write_policy(
        memdb,
        {
            "ua_enabled": True,
            "ua_deny": "Googlebot；badbot\nnewbot",  # 中文分号+换行 -> 逗号规范化
            "region_mode": "allow",
            "region_countries": "中国",
        },
    )
    memdb.commit()
    assert applied[ag.CONFIG_UA_ENABLED] == "true"
    assert applied[ag.CONFIG_UA_DENY] == "googlebot,badbot,newbot"

    payload = ag.policy_payload(memdb)
    assert payload["ua_enabled"] is True
    assert payload["ua_deny"] == ["googlebot", "badbot", "newbot"]
    assert payload["region_mode"] == "allow"
    assert payload["region_countries"] == ["中国"]
    # 存字节：关键词落库即规范化后的逗号串
    assert _row(memdb, ag.CONFIG_UA_DENY).value == "googlebot,badbot,newbot"


def test_ag_bad_mode_and_unknown_key_write_nothing(memdb):
    applied = ag.write_policy(
        memdb, {"ua_denied": "typo", "region_mode": "sideways"}
    )
    memdb.commit()
    assert applied == {}
    assert memdb.query(app_models.SystemConfig).count() == 0


def test_ag_empty_keywords_delete_row_but_read_empty(memdb):
    """关键词清空：空串=未设置删行；读侧 split_keywords(缺行/空串) 都是 []。"""
    ag.write_policy(memdb, {"ua_deny": "badbot"})
    memdb.commit()
    assert _row(memdb, ag.CONFIG_UA_DENY).value == "badbot"
    ag.write_policy(memdb, {"ua_deny": ""})
    memdb.commit()
    assert _row(memdb, ag.CONFIG_UA_DENY) is None
    assert ag.policy_payload(memdb)["ua_deny"] == []


def test_ag_module_cache_invalidated_on_write(memdb):
    """写后模块热缓存（_hot）与 store 同时失效：立读新规则。"""
    assert ag.policy_payload(memdb)["ua_enabled"] is False
    ag.write_policy(memdb, {"ua_enabled": True})
    memdb.commit()
    assert ag.policy_payload(memdb)["ua_enabled"] is True


# ==================== play_sign ====================


def test_ps_roundtrip_bytes_and_descriptions(memdb):
    result = ps.write_config(
        memdb, enabled=True, play_sign_ttl=600, strm_sig_ttl=1200
    )
    # write_config 内部提交（原有行为）
    assert result["enabled"] is True
    assert result["play_sign_ttl"] == 600
    assert result["strm_sig_ttl"] == 1200
    assert _row(memdb, ps.CONFIG_HOTLINK_ENABLED).value == "true"
    assert _row(memdb, ps.CONFIG_PLAY_SIGN_TTL).value == "600"
    assert _row(memdb, ps.CONFIG_STRM_SIG_TTL).value == "1200"
    # 新行带 description（旧代码新建/存量行都写 description；统一通道只新行写）
    assert _row(memdb, ps.CONFIG_PLAY_SIGN_TTL).description


def test_ps_out_of_range_raises_and_writes_nothing(memdb):
    with pytest.raises(ValueError):
        ps.write_config(memdb, enabled=True, play_sign_ttl=30, strm_sig_ttl=1200)
    with pytest.raises(ValueError):
        # 联动下限：strm_min = max(300, 2*600) = 1200，1199 必须拒
        ps.write_config(memdb, enabled=True, play_sign_ttl=600, strm_sig_ttl=1199)
    with pytest.raises(ValueError):
        ps.write_config(memdb, enabled=True, play_sign_ttl="abc", strm_sig_ttl=1200)
    assert memdb.query(app_models.SystemConfig).count() == 0


def test_ps_write_then_store_reads_new(memdb):
    """写完 store 立读新值（4-1 式回环）：写时失效承接原逐键 invalidate。"""
    assert ps.play_sign_ttl_seconds(memdb) == ps.PLAY_TTL_DEFAULT
    ps.write_config(memdb, enabled=True, play_sign_ttl=1800, strm_sig_ttl=1800)
    assert ps.play_sign_ttl_seconds(memdb) == 1800


# ==================== cdn ====================


def test_cdn_roundtrip_bytes(memdb):
    state = cdn.write_config(memdb, "https://cdn.example.com/a", True)
    # normalize_domain 归一化成 scheme://host（路径被剥掉）
    assert state["enabled"] is True
    assert state["domain"] == "https://cdn.example.com"
    assert state["normalized"] == "https://cdn.example.com"
    assert _row(memdb, cdn.CONFIG_CDN_DOMAIN).value == "https://cdn.example.com"
    assert _row(memdb, cdn.CONFIG_CDN_ENABLED).value == "true"
    assert cdn.configured_domain(memdb) == "https://cdn.example.com"


def test_cdn_invalid_domain_raises_and_writes_nothing(memdb):
    with pytest.raises(ValueError):
        cdn.write_config(memdb, "bad domain", True)
    assert memdb.query(app_models.SystemConfig).count() == 0
    assert cdn.enabled(memdb) is False


def test_cdn_disabled_dirty_domain_written_verbatim(memdb):
    """关闭时域名原样落库（不归一化），但 enabled=false 时播放不受影响。"""
    cdn.write_config(memdb, "cdn.example.com/x y", False)
    assert _row(memdb, cdn.CONFIG_CDN_DOMAIN).value == "cdn.example.com/x y"
    assert _row(memdb, cdn.CONFIG_CDN_ENABLED).value == "false"
    assert cdn.enabled(memdb) is False


def test_all_four_families_registered_in_config_service(memdb):
    """四个家族的 schema 都在 config_service 挂名；未知家族整批拒绝。"""
    for family in ("playback_policy", "access_guard", "play_sign", "cdn"):
        assert family in config_service.SCHEMAS, family
    with pytest.raises(Exception):
        config_service.save_config(memdb, {"nope": "1"}, schema="nope")
