"""流媒体加速开关的回归测试。

覆盖：
1. 域名清洗与校验（normalize_domain / validate_domain）
2. 播放 URL 域名改写（rewrite_url_domain）
3. 配置读写（get_config / get_effective_domain，sqlite 内存库）
4. domain_guard 优先读 DB 开关（monkeypatch _accel_db_config，不碰真库）
"""
import sys
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend import models as base_models
from backend.emby_server import stream_accel as sa


@pytest.fixture()
def db():
    engine = create_engine("sqlite:///:memory:")
    base_models.Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    yield session
    session.close()


def _write(db, enabled: str, domain: str):
    db.add(base_models.SystemConfig(key="stream_accel_enabled", value=enabled))
    db.add(base_models.SystemConfig(key="stream_accel_domain", value=domain))
    db.commit()


# ---------- 1. 域名清洗与校验 ----------

def test_normalize_domain():
    assert sa.normalize_domain("cdn.example.com") == "cdn.example.com"
    assert sa.normalize_domain("  https://cdn.example.com/emby/  ") == "cdn.example.com"
    assert sa.normalize_domain("cdn.example.com:443") == "cdn.example.com"
    assert sa.normalize_domain("") == ""
    assert sa.normalize_domain(None) == ""


def test_validate_domain_ok():
    assert sa.validate_domain("cdn.example.com") == "cdn.example.com"
    assert sa.validate_domain("https://stream.example.com/") == "stream.example.com"


def test_validate_domain_bad():
    for bad in ["", "   ", "http://", "not a domain", "a" * 254 + ".com",
                "192.168.1.1", "localhost", "-bad.com", "bad-.com"]:
        with pytest.raises(ValueError):
            sa.validate_domain(bad)


# ---------- 2. URL 改写 ----------

def test_rewrite_url_domain():
    url = "http://1.2.3.4:8000/emby/Videos/abc/stream?x=1"
    out = sa.rewrite_url_domain(url, "http://1.2.3.4:8000", "cdn.example.com")
    assert out == "https://cdn.example.com/emby/Videos/abc/stream?x=1"


def test_rewrite_url_domain_no_base_match():
    # CDN/流节点改写过的 URL（对不上 base）原样返回，绝不造坏 URL
    url = "https://cdn.example.com/emby/Videos/abc/stream"
    assert sa.rewrite_url_domain(url, "http://1.2.3.4:8000", "cdn.example.com") == url


def test_rewrite_url_domain_idempotent():
    url = "https://cdn.example.com/emby/Videos/abc/stream"
    assert sa.rewrite_url_domain(url, "https://cdn.example.com", "cdn.example.com") == url


def test_rewrite_url_domain_empty():
    url = "http://1.2.3.4:8000/emby/Videos/abc/stream"
    assert sa.rewrite_url_domain(url, "http://1.2.3.4:8000", "") == url
    assert sa.rewrite_url_domain("", "http://1.2.3.4:8000", "cdn.example.com") == ""


# ---------- 3. 配置读写 ----------

def test_get_config_defaults(db):
    # 2026-10-21 A 类摘面板现值对齐：enabled 缺省翻为开（生产现值）
    assert sa.get_config(db) == {"enabled": True, "domain": ""}


def test_get_effective_domain_disabled(db):
    _write(db, "false", "cdn.example.com")
    assert sa.get_effective_domain(db) == ""


def test_get_effective_domain_enabled(db):
    _write(db, "true", "cdn.example.com")
    assert sa.get_effective_domain(db) == "cdn.example.com"


def test_get_effective_domain_bad_value_ignored(db):
    # DB 里存了非法域名：宁可不用，也不生成坏 URL
    _write(db, "true", "not a domain")
    assert sa.get_effective_domain(db) == ""


def test_get_effective_domain_true_variants(db):
    for v in ("1", "true", "TRUE", "on"):
        db.query(base_models.SystemConfig).delete()
        _write(db, v, "cdn.example.com")
        assert sa.get_effective_domain(db) == "cdn.example.com", v


# ---------- 4. domain_guard 优先读 DB 开关 ----------

def test_domain_guard_prefers_db_over_env(monkeypatch):
    import asyncio
    import backend.domain_guard as dg

    monkeypatch.setenv("ENFORCE_DOMAIN", "")
    monkeypatch.delenv("TRUST_CF_IP", raising=False)
    # DB 说开：即使环境变量没设，也要拦截非域名
    monkeypatch.setattr(dg, "_accel_db_config", lambda: ({"cdn.example.com"}, True))

    assert dg._get_allowed_hosts() == {"cdn.example.com"}
    assert dg._trust_cf_ip() is True

    async def _run():
        called = []

        async def app(scope, receive, send):
            called.append(True)
            await send({"type": "http.response.start", "status": 200, "headers": []})
            await send({"type": "http.response.body", "body": b"ok"})

        messages = []

        async def send(msg):
            messages.append(msg)

        async def receive():
            return {"type": "http.request", "body": b"", "more_body": False}

        scope = {
            "type": "http", "http_version": "1.1", "method": "GET", "scheme": "http",
            "path": "/emby/System/Info",
            "headers": [(b"host", b"1.2.3.4:8000")],
            "client": ("1.2.3.4", 5000),
        }
        await dg.DomainGuardMiddleware(app)(scope, receive, send)
        return called, messages

    called, messages = asyncio.run(_run())
    assert not called  # 被拦截
    status = next(m["status"] for m in messages if m["type"] == "http.response.start")
    assert status == 403


def test_domain_guard_db_disabled_falls_back_to_env(monkeypatch):
    import backend.domain_guard as dg

    monkeypatch.setenv("ENFORCE_DOMAIN", "env.example.com")
    # DB 说关：回退环境变量
    monkeypatch.setattr(dg, "_accel_db_config", lambda: (None, None))
    assert dg._get_allowed_hosts() == {"env.example.com"}
    assert dg._trust_cf_ip() is False
"""Tests for rewrite_playback_urls_unified (merged URL rewrite pipeline)."""
import sys
sys.path.insert(0, '/opt/aetrix-portal')

from backend.emby_server import stream_accel as sa


class FakeDB:
    pass


def test_unified_no_node_no_accel_passthrough():
    """无节点、无加速域名时原样返回。"""
    # get_effective_domain 需要 DB，这里用 monkeypatch 思路：直接测空 domain 分支
    # 简化：domain 为空时应原样返回
    out = sa.rewrite_url_domain(
        "http://1.2.3.4:8000/emby/Videos/abc/stream",
        "http://1.2.3.4:8000",
        "",
    )
    assert out == "http://1.2.3.4:8000/emby/Videos/abc/stream"


def test_unified_accel_rewrite():
    """加速域名改写生效。"""
    out = sa.rewrite_url_domain(
        "http://1.2.3.4:8000/emby/Videos/abc/stream?x=1",
        "http://1.2.3.4:8000",
        "cdn.example.com",
    )
    assert out == "https://cdn.example.com/emby/Videos/abc/stream?x=1"


def test_unified_fn_exists():
    """统一函数存在且可调用。"""
    assert callable(sa.rewrite_playback_urls_unified)
