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

import json
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


_SOURCE = {
    "name": "r2-hot",
    "type": "s3",
    "endpoint": "https://acct123.r2.cloudflarestorage.com",
    "bucket": "hot",
    "region": "auto",
    "access_key_id": "AKIDEXAMPLE",
    "secret_key": "secret-example",
    "path_style": True,
    "enabled": True,
}


def _arm_direct(db, fid="FID123", source=None):
    _set(db, ps.KEY_STRATEGY, "direct")
    _set(db, ps.KEY_SOURCES, json.dumps([source if source is not None else _SOURCE]))
    _set(db, ps.KEY_HOTLIST, json.dumps([fid]))


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
# 策略判定
# ---------------------------------------------------------------------------

def test_default_relay_never_direct(memdb):
    _arm_direct(memdb)
    _set(memdb, ps.KEY_STRATEGY, "relay")
    assert ps.maybe_direct_url(memdb, "FID123") is None


def test_direct_requires_hotlist(memdb):
    _set(memdb, ps.KEY_STRATEGY, "direct")
    _set(memdb, ps.KEY_SOURCES, json.dumps([_SOURCE]))
    assert ps.maybe_direct_url(memdb, "FID123") is None


def test_direct_happy_path(memdb):
    _arm_direct(memdb)
    url = ps.maybe_direct_url(memdb, "FID123")
    assert url is not None
    assert url.startswith("https://acct123.r2.cloudflarestorage.com/hot/FID123?")
    assert "X-Amz-Signature=" in url


def test_direct_missing_file_id(memdb):
    _arm_direct(memdb)
    assert ps.maybe_direct_url(memdb, None) is None
    assert ps.maybe_direct_url(memdb, "") is None


def test_direct_corrupt_config_falls_back(memdb):
    _set(memdb, ps.KEY_STRATEGY, "direct")
    _set(memdb, ps.KEY_SOURCES, "{not json")
    _set(memdb, ps.KEY_HOTLIST, json.dumps(["FID123"]))
    assert ps.maybe_direct_url(memdb, "FID123") is None


def test_direct_incomplete_source_skipped(memdb):
    broken = dict(_SOURCE, secret_key="")
    _arm_direct(memdb, source=broken)
    assert ps.maybe_direct_url(memdb, "FID123") is None
    disabled = dict(_SOURCE, enabled=False)
    _arm_direct(memdb, source=disabled)
    assert ps.maybe_direct_url(memdb, "FID123") is None


def test_direct_bad_strategy_value_treated_as_relay(memdb):
    _arm_direct(memdb)
    _set(memdb, ps.KEY_STRATEGY, "banana")
    assert ps.get_strategy(memdb) == "relay"
    assert ps.maybe_direct_url(memdb, "FID123") is None


def test_direct_key_template_applied(memdb):
    _arm_direct(memdb)
    _set(memdb, ps.KEY_KEY_TEMPLATE, "mirror/{file_id}.bin")
    url = ps.maybe_direct_url(memdb, "FID123")
    assert "/hot/mirror/FID123.bin?" in url


# ---------------------------------------------------------------------------
# 管理端配置 API
# ---------------------------------------------------------------------------

from backend.api import admin_playback as adm  # noqa: E402


def _save_body(**kw):
    body = adm.PlaybackConfigSave(
        strategy="direct",
        sources=[adm.DirectSourceIn(**_SOURCE)],
        hotlist=["FID123"],
        key_template="{file_id}",
        url_ttl_sec=3600,
    )
    for k, v in kw.items():
        setattr(body, k, v)
    return body


def test_admin_roundtrip_and_secret_masking(memdb):
    out = adm.playback_config_save(_save_body(), memdb)
    cfg = out["config"]
    assert cfg["strategy"] == "direct"
    assert cfg["hotlist"] == ["FID123"]
    src = cfg["sources"][0]
    assert src["secret_key"] == "********"
    assert src["has_secret_key"] is True
    # 库里存的是真密钥（只是回显打码）
    stored = ps.get_sources(memdb)
    assert stored[0]["secret_key"] == "secret-example"


def test_admin_secret_keep_on_blank(memdb):
    adm.playback_config_save(_save_body(), memdb)
    body = _save_body()
    body.sources[0].secret_key = ""
    adm.playback_config_save(body, memdb)
    assert ps.get_sources(memdb)[0]["secret_key"] == "secret-example"


def test_admin_rejects_bad_strategy(memdb):
    from fastapi import HTTPException
    with pytest.raises(HTTPException) as ei:
        adm.playback_config_save(_save_body(strategy="banana"), memdb)
    assert ei.value.status_code == 400


def test_admin_rejects_bad_template(memdb):
    from fastapi import HTTPException
    with pytest.raises(HTTPException):
        adm.playback_config_save(_save_body(key_template="no-placeholder"), memdb)
    with pytest.raises(HTTPException):
        adm.playback_config_save(_save_body(key_template="../{file_id}"), memdb)


def test_admin_rejects_duplicate_source_names(memdb):
    from fastapi import HTTPException
    body = _save_body()
    body.sources.append(adm.DirectSourceIn(**_SOURCE))
    with pytest.raises(HTTPException):
        adm.playback_config_save(body, memdb)


def test_direct_redirect_headers_hardened():
    """302 直链卫生头（PR-2 尾巴）：签名 URL 不许被播放器/CDN 缓存复用
    （no-cache + no-store），也不把门户域名当 Referer 发给对象存储。"""
    headers = ps.DIRECT_REDIRECT_HEADERS
    cc = headers.get("Cache-Control", "")
    assert "no-store" in cc and "no-cache" in cc
    assert headers.get("Referrer-Policy") == "no-referrer"


# ---------------------------------------------------------------------------
# PR-2 尾巴：配额感知降级 + 计数可观测
# ---------------------------------------------------------------------------

@pytest.fixture()
def clean_counters():
    ps.strategy_counters_reset()
    yield
    ps.strategy_counters_reset()


def test_direct_happy_path_302_counts(memdb, clean_counters):
    _arm_direct(memdb)
    url = ps.maybe_direct_url(memdb, "FID123")
    assert url is not None
    assert url.startswith("https://acct123.r2.cloudflarestorage.com/hot/FID123?")
    c = ps.strategy_counters()
    assert c["direct_302"] == 1
    assert c["fallback_relay"] == 0 and c["fallback_quota"] == 0


def test_direct_no_source_falls_back_to_relay(memdb, clean_counters):
    """direct + 无可用直链源 → 回落中继并计数。"""
    _set(memdb, ps.KEY_STRATEGY, "direct")
    _set(memdb, ps.KEY_SOURCES, json.dumps([]))
    _set(memdb, ps.KEY_HOTLIST, json.dumps(["FID123"]))
    assert ps.maybe_direct_url(memdb, "FID123") is None
    assert ps.strategy_counters()["fallback_relay"] == 1


def test_playback_config_get_includes_counters(memdb, clean_counters):
    """GET /playback/config 返回降级计数（管理侧可观测），且不破坏原有字段。"""
    _arm_direct(memdb)
    ps.maybe_direct_url(memdb, "FID123")
    cfg = adm._read_config(memdb)
    assert cfg["strategy"] == "direct"
    assert cfg["counters"]["direct_302"] == 1
    assert cfg["counters"]["fallback_relay"] == 0
    assert cfg["counters"]["fallback_quota"] == 0
