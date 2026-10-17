# -*- coding: utf-8 -*-
"""播放策略能力测试（2026-10-16）。

覆盖：
- SigV4 预签名：结构/确定性/path 与 virtual 两种寻址/关键参数齐全。
- maybe_direct_url：默认 relay 永不直链；direct 需策略+名单+可用源
  三件齐全；配置损坏/源不完整一律回落 None（回落中继、不打断播放）。
- 管理端配置 API：保存/回读往返、密钥打码与「留空不改」、非法值 400。
全部离线：不做真实网络请求。
"""
import os

os.environ.setdefault("DATABASE_TYPE", "sqlite")
os.environ.setdefault("REDIS_ENABLED", "false")

from datetime import datetime, timezone

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend import models as app_models
from backend.database import Base
from backend.emby_server import playback_strategy as ps
from backend.integrations import store


@pytest.fixture()
def memdb():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine, tables=[app_models.SystemConfig.__table__])
    Session = sessionmaker(bind=engine)
    session = Session()
    yield session
    session.close()


@pytest.fixture(autouse=True)
def _isolate_process_global_state():
    """进程级全局状态隔离（每个用例进出各清一次，与执行顺序无关）。

    配置 TTL 热缓存（store）按配置键缓存、与具体 DB 无关，前一用例缓存的
    键模板等值会漏进下一用例。配额负缓存已随中继全删移除。
    """
    store.invalidate()
    yield
    store.invalidate()


def _set(db, key, value):
    store.write_values(db, {key: value})
    db.commit()


#: （配置瘦身第 1 批，2026-10-17：KEY_STRATEGY/KEY_SOURCES/KEY_HOTLIST/
#: KEY_KEY_TEMPLATE/KEY_TTL 五个死键已删，get_* 恒返回默认值。）
#: 曾用于构造"direct 可配"场景的 _SOURCE/_arm_direct 已随之移除。


# ---------------------------------------------------------------------------
# 预签名
# ---------------------------------------------------------------------------

_NOW = datetime(2026, 10, 16, 5, 0, 0, tzinfo=timezone.utc)


def _presign(**kw):
    args = dict(
        endpoint="https://acct123.r2.cloudflarestorage.com",
        bucket="hot", key="FID123", region="auto",
        access_key_id="AKIDEXAMPLE", secret_key="secret-example",
        expires_sec=3600, path_style=True, now=_NOW,
    )
    args.update(kw)
    return ps.presign_s3_get(**args)


def test_presign_path_style_structure():
    url = _presign()
    assert url.startswith(
        "https://acct123.r2.cloudflarestorage.com/hot/FID123?")
    for frag in ("X-Amz-Algorithm=AWS4-HMAC-SHA256",
                 "X-Amz-Credential=AKIDEXAMPLE%2F20261016%2Fauto%2Fs3%2Faws4_request",
                 "X-Amz-Date=20261016T050000Z",
                 "X-Amz-Expires=3600",
                 "X-Amz-SignedHeaders=host",
                 "X-Amz-Signature="):
        assert frag in url, frag
    sig = url.rsplit("X-Amz-Signature=", 1)[1]
    assert len(sig) == 64 and all(c in "0123456789abcdef" for c in sig)


def test_presign_deterministic_and_sensitive():
    assert _presign() == _presign()
    assert _presign() != _presign(key="OTHER")
    assert _presign() != _presign(secret_key="another-secret")
    assert _presign() != _presign(expires_sec=60)


def test_presign_virtual_style_host():
    url = _presign(path_style=False)
    assert url.startswith("https://hot.acct123.r2.cloudflarestorage.com/FID123?")


def test_presign_key_encoding():
    url = _presign(key="dir with space/文件.mkv")
    assert "/hot/dir%20with%20space/" in url
    assert "文件" not in url  # 非 ASCII 必须百分号编码


# ---------------------------------------------------------------------------
# 策略判定（配置瘦身第 1 批，2026-10-17）：以下 5 个键已从配置面删除——
# KEY_STRATEGY / KEY_SOURCES / KEY_HOTLIST / KEY_KEY_TEMPLATE / KEY_TTL。
# get_* 函数恒返回默认值，库里即使残留旧行，行为也不变；maybe_direct_url
# 恒返回 None（与删除前完全一致：策略恒为 relay，relay 直接 return None）。
# ---------------------------------------------------------------------------

def test_dead_config_keys_removed():
    for const in ("KEY_STRATEGY", "KEY_SOURCES", "KEY_HOTLIST",
                  "KEY_KEY_TEMPLATE", "KEY_TTL"):
        assert not hasattr(ps, const), f"{const} 应已从配置面删除"


def test_stale_config_rows_have_no_effect(memdb):
    # 库里残留旧行也不影响：get_* 恒返回默认值
    for key, value in (("playback_strategy", "direct"),
                       ("playback_direct_sources", '[{"name": "x"}]'),
                       ("playback_direct_hotlist", '["FID123"]'),
                       ("playback_direct_key_template", "mirror/{file_id}.bin"),
                       ("playback_direct_url_ttl_sec", "60")):
        _set(memdb, key, value)
    assert ps.get_strategy(memdb) == ps.STRATEGY_RELAY
    assert ps.get_sources(memdb) == []
    assert ps.get_hotlist(memdb) == set()
    assert ps.get_key_template(memdb) == ps.DEFAULT_KEY_TEMPLATE
    assert ps.get_ttl_sec(memdb) == ps.DEFAULT_TTL_SEC


def test_maybe_direct_url_always_none(memdb, clean_counters):
    _set(memdb, "playback_strategy", "direct")  # 残留行也不影响
    assert ps.maybe_direct_url(memdb, "FID123") is None
    assert ps.maybe_direct_url(memdb, None) is None
    assert ps.maybe_direct_url(memdb, "") is None
    # relay 在第一道检查直接 return None，不产生回落计数
    assert ps.strategy_counters()["direct_302"] == 0
    assert ps.strategy_counters()["fallback_relay"] == 0


def test_direct_redirect_headers_hardened():
    """302 直链卫生头（PR-2 尾巴）：签名 URL 不许被播放器/CDN 缓存复用
    （no-cache + no-store），也不把门户域名当 Referer 发给对象存储。"""
    headers = ps.DIRECT_REDIRECT_HEADERS
    cc = headers.get("Cache-Control", "")
    assert "no-store" in cc and "no-cache" in cc
    assert headers.get("Referrer-Policy") == "no-referrer"


# ---------------------------------------------------------------------------
# 计数可观测（配额感知降级已随中继全删移除，2026-10-17）
# ---------------------------------------------------------------------------

@pytest.fixture()
def clean_counters():
    ps.strategy_counters_reset()
    yield
    ps.strategy_counters_reset()


def test_relay_path_emits_no_counters(memdb, clean_counters):
    """relay 在第一道检查直接 return None，不产生任何计数（配置瘦身第 1 批后
    的恒定行为：direct 路径已不可达）。"""
    _set(memdb, "playback_strategy", "direct")  # 残留行也不影响
    assert ps.maybe_direct_url(memdb, "FID123") is None
    c = ps.strategy_counters()
    assert c["direct_302"] == 0
    assert c["fallback_relay"] == 0


