"""谷歌 Drive 绑定开关（SystemConfig 键 drive_binding_enabled）。

站主 2026-11-04 亲令：谷歌绑定能力留着，但弄一个开关把绑定关掉，
现置关。钉死的口径：

- 开关只管「新建 Drive 绑定」一个动作：OAuth 发起与回调完成、
  「我的网盘」providers 名单里的 drive，都受它管；
- 默认关：无配置行 / 值非法 / 读取异常，一律按关处理（只有字面
  "true" 才算开，word 风格，与 register_gift_* 开关同口径）；
- 已有绑定记录、列表、解绑、取绑定等既有能力一律不受开关影响；
- 管理端在既有 Drive API 配置卡片上可见可改（GET/PUT
  /api/admin/drive-api-config 的 binding_enabled 字段），写入走
  config_service 的 drive_api 家族校验，存 "true"/"false"；
- qps 字段不传 binding_enabled 时不许顺手改开关（None = 不动）。
"""

import time
from types import SimpleNamespace
from urllib.parse import parse_qs, urlparse

import pytest
from fastapi import HTTPException, Response
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from starlette.requests import Request as StarletteRequest

from backend import config_schema, config_service
from backend import models as base_models
from backend.api import admin_economy
from backend.emby_server import models as em
from backend.emby_server import drive_oauth as do
from backend.emby_server import portal
from backend.emby_server import user_bindings as ub
from backend.emby_server.upstream_sources import seal_secret
from backend.integrations import store


@pytest.fixture()
def db_factory():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False},
                           poolclass=StaticPool)
    base_models.Base.metadata.create_all(engine)
    em.Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)


@pytest.fixture()
def db(db_factory):
    store.invalidate()
    session = db_factory()
    yield session
    session.close()
    store.invalidate()


@pytest.fixture(autouse=True)
def _oauth_env(monkeypatch):
    monkeypatch.setenv("DRIVE_OAUTH_CLIENT_ID", "test-client-id")
    monkeypatch.setenv("DRIVE_OAUTH_CLIENT_SECRET", "test-client-secret")
    monkeypatch.setenv("EMBY_PUBLIC_URL", "https://aetrix.example")
    monkeypatch.delenv("BYOS_EXTRA_BINDING_PROVIDERS", raising=False)
    do._probe_thread = None
    yield
    do._probe_thread = None


def _set_switch(db, value):
    """value=None 表示删行（回到无配置行状态）。"""
    row = (db.query(base_models.SystemConfig)
           .filter(base_models.SystemConfig.key == ub.KEY_DRIVE_BINDING_ENABLED)
           .first())
    if value is None:
        if row is not None:
            db.delete(row)
    elif row is None:
        db.add(base_models.SystemConfig(
            key=ub.KEY_DRIVE_BINDING_ENABLED, value=value))
    else:
        row.value = value
    db.commit()
    store.invalidate()


def _mount(db, name, mount_type, is_enabled=True):
    row = em.StorageMount(name=name, mount_type=mount_type, path="",
                          is_enabled=is_enabled)
    db.add(row)
    db.commit()
    return row


def _seed_drive_binding(db, user_id=7, label="盘", status="active"):
    token = do.encode_drive_token(
        "AT-OLD", "RT-OLD", int(time.time()) + 3600)
    cred = em.MountCredential(
        mount_id=None, scope="user", owner_user_id=user_id,
        sealed_credential=seal_secret(token), key_id="fernet-v1",
        status=status)
    db.add(cred)
    db.flush()
    binding = em.UserStorageBinding(
        user_id=user_id, provider="drive", account_label=label,
        credential_id=cred.id, status=status)
    db.add(binding)
    db.commit()
    return binding, cred


def _http_request(host="aetrix.example"):
    scope = {"type": "http", "method": "POST", "path": "/",
             "headers": [(b"host", host.encode())], "query_string": b"",
             "scheme": "https", "server": (host, 443),
             "client": ("127.0.0.1", 40000)}
    return StarletteRequest(scope)


class TestSwitchRead:
    def test_missing_row_is_off(self, db):
        assert ub.drive_binding_enabled(db) is False

    def test_false_row_is_off(self, db):
        _set_switch(db, "false")
        assert ub.drive_binding_enabled(db) is False

    def test_true_row_is_on(self, db):
        _set_switch(db, "true")
        assert ub.drive_binding_enabled(db) is True

    def test_garbage_row_is_off(self, db):
        _set_switch(db, "banana")
        assert ub.drive_binding_enabled(db) is False

    def test_read_failure_is_off(self, db, monkeypatch):
        def _boom(*args, **kwargs):
            raise RuntimeError("db down")

        monkeypatch.setattr(store, "get_value", _boom)
        assert ub.drive_binding_enabled(db) is False


class TestProvidersGate:
    def test_drive_mount_hidden_by_default(self, db):
        _mount(db, "m-drive", "drive")
        assert ub.list_binding_providers(db) == []

    def test_drive_mount_hidden_when_off(self, db):
        _mount(db, "m-drive", "drive")
        _set_switch(db, "false")
        assert ub.list_binding_providers(db) == []

    def test_drive_mount_listed_when_on(self, db):
        _mount(db, "m-drive", "drive")
        _set_switch(db, "true")
        assert ub.list_binding_providers(db) == ["drive"]

    def test_switch_beats_extra_env(self, db, monkeypatch):
        monkeypatch.setenv("BYOS_EXTRA_BINDING_PROVIDERS", "drive")
        assert ub.list_binding_providers(db) == []
        _set_switch(db, "true")
        assert ub.list_binding_providers(db) == ["drive"]


class TestOAuthStartGate:
    def test_start_rejected_when_off_and_no_state_row(self, db):
        with pytest.raises(do.DriveOAuthError) as exc:
            do.start_drive_oauth(db, 7, "我的盘")
        assert exc.value.kind == "disabled"
        assert "暂未开放" in exc.value.message
        assert db.query(em.DriveOAuthState).count() == 0

    def test_start_rejected_when_row_false(self, db):
        _set_switch(db, "false")
        with pytest.raises(do.DriveOAuthError) as exc:
            do.start_drive_oauth(db, 7, "我的盘")
        assert exc.value.kind == "disabled"

    def test_start_ok_when_on(self, db):
        _set_switch(db, "true")
        result = do.start_drive_oauth(db, 7, "我的盘")
        assert result["authorization_url"].startswith(
            "https://accounts.google.com")
        assert db.query(em.DriveOAuthState).count() == 1


class TestOAuthCompleteGate:
    def test_complete_rejected_when_switched_off_mid_flow(self, db):
        _set_switch(db, "true")
        result = do.start_drive_oauth(db, 7, "我的盘")
        state = parse_qs(urlparse(result["authorization_url"]).query)["state"][0]
        _set_switch(db, "false")
        with pytest.raises(do.DriveOAuthError) as exc:
            do.complete_drive_oauth(db, state, "CODE")
        assert exc.value.kind == "disabled"
        # 门在消费 state 之前：state 行不许被烧掉
        assert db.query(em.DriveOAuthState).count() == 1


class TestRouteGate:
    def test_portal_start_403_with_human_message_when_off(self, db):
        with pytest.raises(HTTPException) as exc:
            portal.start_drive_oauth_binding(
                portal.DriveOAuthStart(account_label="盘"),
                _http_request(), Response(), SimpleNamespace(id=7), db)
        assert exc.value.status_code == 403
        assert "暂未开放" in exc.value.detail

    def test_portal_start_ok_when_on(self, db):
        _set_switch(db, "true")
        result = portal.start_drive_oauth_binding(
            portal.DriveOAuthStart(account_label="盘"),
            _http_request(), Response(), SimpleNamespace(id=7), db)
        assert result["authorization_url"].startswith(
            "https://accounts.google.com")


class TestExistingBindingsUnaffected:
    def test_list_get_delete_still_work_when_off(self, db):
        binding, _cred = _seed_drive_binding(db, 7)
        # 开关关（无行默认关）时既有绑定照常可见、可用、可解绑
        listed = ub.list_bindings(db, 7)
        assert [b["id"] for b in listed] == [binding.id]
        assert listed[0]["provider"] == "drive"
        assert ub.get_active_binding(db, 7, "drive") is not None
        ub.delete_binding(db, 7, binding.id)
        assert ub.list_bindings(db, 7) == []


class TestAdminConfig:
    def test_save_config_roundtrip_word_style(self, db):
        config_service.save_config(
            db, {"drive_binding_enabled": True}, schema="drive_api")
        db.commit()  # save_config 本身不提交（端点层提交），口径对齐
        row = (db.query(base_models.SystemConfig)
               .filter(base_models.SystemConfig.key == "drive_binding_enabled")
               .first())
        assert row.value == "true"
        config_service.save_config(
            db, {"drive_binding_enabled": False}, schema="drive_api")
        db.commit()
        db.refresh(row)
        assert row.value == "false"

    def test_save_config_rejects_garbage(self, db):
        with pytest.raises(config_schema.ConfigValueError):
            config_service.save_config(
                db, {"drive_binding_enabled": "banana"}, schema="drive_api")

    def test_get_defaults_off(self, db):
        payload = admin_economy.drive_api_get_config(1, db)
        assert payload["binding_enabled"] is False
        assert payload["defaults"]["binding_enabled"] is False
        assert payload["qps"] == 8.0

    def test_put_turns_on_and_persists(self, db):
        payload = admin_economy.drive_api_update_config(
            admin_economy.DriveApiConfigRequest(qps=8, binding_enabled=True),
            1, db)
        assert payload["binding_enabled"] is True
        store.invalidate()
        assert ub.drive_binding_enabled(db) is True

    def test_put_without_binding_field_leaves_switch_untouched(self, db):
        admin_economy.drive_api_update_config(
            admin_economy.DriveApiConfigRequest(qps=8, binding_enabled=True),
            1, db)
        payload = admin_economy.drive_api_update_config(
            admin_economy.DriveApiConfigRequest(qps=10), 1, db)
        assert payload["qps"] == 10.0
        assert payload["binding_enabled"] is True
