"""管理后台字幕能力：桥接（DB 为准、env 回退）+ 能力中心卡片。

口径：本地 Python 3.12（CI 以 3.11 全量为准）；只跑本文件逐文件，
不报全绿。store 热缓存 60 秒，夹具每用例前后都失效，避免跨用例污染。
"""
import os
import sys

os.environ.setdefault("DATABASE_TYPE", "sqlite")
os.environ.setdefault("REDIS_ENABLED", "false")
sys.path.insert(0, ".")

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend import integrations, models
from backend.emby_server import smart_subtitles as ss
from backend.integrations import store

ENABLED = "SMART_SUBTITLES_ENABLED"
PROVIDER = "SMART_SUBTITLE_TRANSLATE_PROVIDER"
ENDPOINT = "SMART_SUBTITLE_TRANSLATE_ENDPOINT"
API_KEY = "SMART_SUBTITLE_TRANSLATE_API_KEY"
TRIO = (PROVIDER, ENDPOINT, API_KEY)


@pytest.fixture()
def db():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    models.Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    yield session
    session.close()


@pytest.fixture(autouse=True)
def _clean_store_cache():
    store.invalidate()
    yield
    store.invalidate()


@pytest.fixture(autouse=True)
def _clean_subtitle_env(monkeypatch):
    for k in (ENABLED, PROVIDER, ENDPOINT, API_KEY):
        monkeypatch.delenv(k, raising=False)
    yield


def _set_row(session, key, value):
    row = session.query(models.SystemConfig).filter(
        models.SystemConfig.key == key).first()
    if row is None:
        session.add(models.SystemConfig(key=key, value=value))
    else:
        row.value = value
    session.commit()
    store.invalidate()


def _get_row(session, key):
    row = session.query(models.SystemConfig).filter(
        models.SystemConfig.key == key).first()
    return row.value if row else None


def _set_env(monkeypatch, enabled=None, provider=None, endpoint=None, api_key=None):
    if enabled is not None:
        monkeypatch.setenv(ENABLED, enabled)
    if provider is not None:
        monkeypatch.setenv(PROVIDER, provider)
    if endpoint is not None:
        monkeypatch.setenv(ENDPOINT, endpoint)
    if api_key is not None:
        monkeypatch.setenv(API_KEY, api_key)


# ---- 桥接：开关 ----

def test_resolve_enabled_db_row_wins_over_env(db, monkeypatch):
    _set_env(monkeypatch, enabled="1")
    _set_row(db, ENABLED, "false")
    assert ss.resolve_enabled(db) is False
    _set_row(db, ENABLED, "true")
    assert ss.resolve_enabled(db) is True


def test_resolve_enabled_no_row_falls_back_to_env(db, monkeypatch):
    _set_env(monkeypatch, enabled="1")
    assert ss.resolve_enabled(db) is True
    _set_env(monkeypatch, enabled="0")
    assert ss.resolve_enabled(db) is False


def test_resolve_enabled_db_none_is_env_only(monkeypatch):
    _set_env(monkeypatch, enabled="1")
    assert ss.resolve_enabled(None) is True
    assert ss.resolve_enabled(None) == ss.enabled()


# ---- 桥接：三件套 ----

def test_resolve_provider_config_db_wins_per_key(db, monkeypatch):
    _set_env(monkeypatch, provider="env-p", endpoint="env-e", api_key="env-k")
    _set_row(db, PROVIDER, "db-p")
    provider, endpoint, api_key = ss.resolve_provider_config(db)
    assert (provider, endpoint, api_key) == ("db-p", "env-e", "env-k")


def test_resolve_provider_config_missing_reports_first_gap(db, monkeypatch):
    with pytest.raises(ss.TranslateNotConfigured) as ei:
        ss.resolve_provider_config(db)
    assert PROVIDER in str(ei.value)
    _set_row(db, PROVIDER, "p")
    with pytest.raises(ss.TranslateNotConfigured) as ei:
        ss.resolve_provider_config(db)
    assert ENDPOINT in str(ei.value)
    _set_row(db, ENDPOINT, "e")
    with pytest.raises(ss.TranslateNotConfigured) as ei:
        ss.resolve_provider_config(db)
    assert API_KEY in str(ei.value)
    # 错误信息绝不回显凭据值
    _set_row(db, API_KEY, "k-secret-123")
    assert ss.resolve_provider_config(db) == ("p", "e", "k-secret-123")


# ---- 桥接真实生效（不是摆设）：生成入口走 db ----

SRT = ("1\n00:00:01,000 --> 00:00:03,000\nHello\n\n"
       "2\n00:00:04,000 --> 00:00:06,000\nWorld\n")


def test_generate_db_switch_off_beats_env_on(db, monkeypatch, tmp_path):
    _set_env(monkeypatch, enabled="1")
    _set_row(db, ENABLED, "false")
    with pytest.raises(ss.SmartSubtitleDisabled):
        ss.generate_bilingual_track(1, SRT, "eng", "chi",
                                    translations=["你好", "世界"],
                                    cache_dir=str(tmp_path), db=db)


def test_generate_db_switch_on_beats_env_off(db, monkeypatch, tmp_path):
    _set_env(monkeypatch, enabled="0")
    _set_row(db, ENABLED, "true")
    out = ss.generate_bilingual_track(1, SRT, "eng", "chi",
                                      translations=["你好", "世界"],
                                      cache_dir=str(tmp_path), db=db)
    assert out["cue_count"] == 2


# ---- 能力中心卡片 ----

def test_capability_registered_and_not_testable(db):
    assert "smart_subtitles" in integrations.CAPABILITY_MODULES
    card = [c for c in integrations.list_capabilities(db)
            if c["slug"] == "smart_subtitles"]
    assert len(card) == 1
    assert card[0]["testable"] is False  # 无 test()：不挂测试按钮，不骗人


def test_form_shows_env_switch_on_but_card_not_enabled_until_configured(db, monkeypatch):
    # 生产现状：env 已开、三件套全空——表单开关必须显示开（不能睁眼说关），
    # 但卡片 enabled 仍为 False（三件套不齐，生成会 503，不能谎称已启用）。
    _set_env(monkeypatch, enabled="1")
    got = integrations.get_capability(db, "smart_subtitles")
    assert got["values"][ENABLED] == "true"
    assert got["item"]["enabled"] is False
    assert got["item"]["configured"] is False


def test_card_env_trio_shows_configured_and_key_masked(db, monkeypatch):
    _set_env(monkeypatch, enabled="1", provider="env-p",
             endpoint="https://example.invalid/v1", api_key="env-key-plain")
    got = integrations.get_capability(db, "smart_subtitles")
    assert got["item"]["configured"] is True
    assert got["item"]["enabled"] is True
    assert got["values"][PROVIDER] == "env-p"
    assert got["values"][ENDPOINT] == "https://example.invalid/v1"
    # 密钥只报掩码，绝不回显 env 明文
    assert got["values"][API_KEY] == "******"
    assert "env-key-plain" not in str(got)


def test_save_roundtrip_and_secret_keep_semantics(db, monkeypatch):
    _set_env(monkeypatch, api_key="env-key-plain")
    integrations.save_capability(db, "smart_subtitles", {
        ENABLED: True, PROVIDER: "db-p",
        ENDPOINT: "https://db.invalid/v1", API_KEY: "db-key-plain",
    })
    assert _get_row(db, ENABLED) == "true"
    assert _get_row(db, API_KEY) == "db-key-plain"
    got = integrations.get_capability(db, "smart_subtitles")
    assert got["values"][API_KEY] == "******"
    assert "db-key-plain" not in str(got)
    # 提交掩码 = 不修改
    integrations.save_capability(db, "smart_subtitles", {API_KEY: "******"})
    assert _get_row(db, API_KEY) == "db-key-plain"
    # 清空面板密钥 = 面板值归空、回退 env（不会删除 env 里的密钥）
    integrations.save_capability(db, "smart_subtitles", {API_KEY: ""})
    assert _get_row(db, API_KEY) == ""
    got = integrations.get_capability(db, "smart_subtitles")
    assert got["values"][API_KEY] == "******"
    # 未知键静默忽略，不造脏键
    integrations.save_capability(db, "smart_subtitles", {"no_such_key": "x"})
    assert _get_row(db, "no_such_key") is None


# ---- 管理端端点（TestClient + 依赖覆盖） ----

def _admin_client(monkeypatch):
    from backend.api.admin_core import get_current_admin
    from backend.api.capabilities_admin import capabilities_router
    from backend.database import get_db
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    models.Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    admin = models.WebUser(username="admin", password_hash="x", is_staff=True)
    session.add(admin)
    session.commit()
    app = FastAPI()
    app.include_router(capabilities_router)
    app.dependency_overrides[get_current_admin] = lambda: admin
    app.dependency_overrides[get_db] = lambda: session
    return TestClient(app), session


def test_admin_endpoint_get_put_and_audit_masks_secret(monkeypatch):
    client, session = _admin_client(monkeypatch)
    r = client.get("/api/admin/capabilities/smart_subtitles")
    assert r.status_code == 200, r.text
    assert r.json()["item"]["slug"] == "smart_subtitles"
    r = client.put("/api/admin/capabilities/smart_subtitles", json={"values": {
        ENABLED: True, PROVIDER: "p1",
        ENDPOINT: "https://e.invalid/v1", API_KEY: "audit-secret-123",  # secret-scan: allow：测试夹具假值，仅用于验证审计脱敏与掩码，不发往任何外部提供方
    }})
    assert r.status_code == 200, r.text
    assert r.json()["success"] is True
    assert _get_row(session, API_KEY) == "audit-secret-123"
    # 审计只记字段名，不记密钥值
    logs = session.query(models.AdminLog).all()
    assert logs, "应写入审计日志"
    assert "audit-secret-123" not in str([l.details for l in logs])
    # 管理端保存绝不写 .env：面板值只落在 system_configs
    r = client.get("/api/admin/capabilities/smart_subtitles")
    assert r.json()["values"][API_KEY] == "******"


def test_admin_endpoint_unknown_slug_404(monkeypatch):
    client, _ = _admin_client(monkeypatch)
    r = client.get("/api/admin/capabilities/no_such_slug")
    assert r.status_code == 404


# ---- R4 修复：面板清空（"" 行）必须回退 env，与表单回显一致 ----

def test_resolve_provider_config_cleared_row_falls_back_to_env(db, monkeypatch):
    # 面板清空写 "" 行（不是删行）；此前生成侧把 "" 当成“已填空值”，
    # 报未配置，而表单回显已回退 env 显示已配置，两边打架。
    _set_env(monkeypatch, provider="env-p", endpoint="https://env.invalid/v1",
             api_key="env-key-plain")
    integrations.save_capability(db, "smart_subtitles", {API_KEY: ""})
    assert _get_row(db, API_KEY) == ""
    assert ss.resolve_provider_config(db) == (
        "env-p", "https://env.invalid/v1", "env-key-plain")
    # 逐项：只清提供方时，提供方回退 env，其余仍用面板值
    integrations.save_capability(db, "smart_subtitles", {
        ENDPOINT: "https://db.invalid/v1", API_KEY: "db-key-plain"})
    integrations.save_capability(db, "smart_subtitles", {PROVIDER: ""})
    assert ss.resolve_provider_config(db) == (
        "env-p", "https://db.invalid/v1", "db-key-plain")


def test_resolve_enabled_blank_row_falls_back_to_env(db, monkeypatch):
    _set_env(monkeypatch, enabled="1")
    _set_row(db, ENABLED, "")
    assert ss.resolve_enabled(db) is True
    _set_env(monkeypatch, enabled="0")
    assert ss.resolve_enabled(db) is False


def test_generate_translate_path_uses_env_after_clear(db, monkeypatch, tmp_path):
    _set_env(monkeypatch, enabled="1", provider="env-p",
             endpoint="https://env.invalid/v1", api_key="env-key-plain")
    integrations.save_capability(db, "smart_subtitles", {API_KEY: ""})
    seen = {}

    def _fake_post(url, headers, payload):
        seen["url"] = url
        return {"translations": ["你好", "世界"]}

    out = ss.generate_bilingual_track(1, SRT, "eng", "chi",
                                      http_post=_fake_post,
                                      cache_dir=str(tmp_path), db=db)
    assert out["cue_count"] == 2
    assert seen["url"] == "https://env.invalid/v1"


def test_list_capabilities_fields_use_effective_values(db, monkeypatch):
    # 总览此前只对详情页用生效值：纯 env 配置时总览字段全空、徽标却已配置。
    _set_env(monkeypatch, enabled="1", provider="env-p",
             endpoint="https://env.invalid/v1", api_key="env-key-plain")
    cards = {c["slug"]: c for c in integrations.list_capabilities(db)}
    card = cards["smart_subtitles"]
    assert card["enabled"] is True and card["configured"] is True
    assert card["fields"][PROVIDER] == "env-p"
    assert card["fields"][ENDPOINT] == "https://env.invalid/v1"
    assert card["fields"][API_KEY] == "******"
    assert "env-key-plain" not in str(card)
