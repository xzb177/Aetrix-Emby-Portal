"""Drive OAuth 第二绑定入口（新应用，DRIVE_OAUTH2_CLIENT_ID/SECRET）。

钉死口径（多一件不做）：
- 新入口 start 用第二套客户端拼授权地址，state 行落 oauth_client；
  同一个 callback 靠 state 行分流换 token，绑定行落 oauth_client，
  刷新按绑定行记录选客户端——用错客户端刷新必失败，本文件用桩
  断言每次 token 请求带的 client_id / client_secret 原样；
- 新入口仅限持有「查看凭证」的用户（判定复用全站唯一口径
  subscriptions.view_grant，现算不信状态字段）：发起端拒绝、
  回调端复核拒绝（发起后凭证被收回也绑不成）；老入口不受此限；
- DRIVE_OAUTH2_* 未配置时新入口不可用且不报错，老入口零影响；
- 老入口既有行为不变：授权地址、换 token 参数、落行口径与上线前
  一致，只是绑定行多落一个来源标记。
Google / Drive 网络全部用桩替身，不碰真实上游。
"""

import time
from datetime import datetime
from types import SimpleNamespace
from urllib.parse import parse_qs, unquote, urlparse

import pytest
from fastapi import HTTPException, Response
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from starlette.requests import Request as StarletteRequest

from backend import models as base_models
from backend import realms
from backend.emby_server import models as em
from backend.emby_server import drive_oauth as do
from backend.emby_server import portal
from backend.emby_server import user_bindings as ub
from backend.emby_server.upstream_sources import seal_secret, unseal_secret
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
    session = db_factory()
    # 与 P2 套件同口径：Drive 绑定开关播种为开（开关两态由
    # test_drive_binding_switch.py 专门覆盖）。
    session.add(base_models.SystemConfig(
        key="drive_binding_enabled", value="true"))
    session.commit()
    store.invalidate()
    yield session
    session.close()
    store.invalidate()


@pytest.fixture(autouse=True)
def _oauth_env(monkeypatch):
    monkeypatch.setenv("DRIVE_OAUTH_CLIENT_ID", "test-client-id")
    monkeypatch.setenv("DRIVE_OAUTH_CLIENT_SECRET", "test-client-secret")
    monkeypatch.setenv("EMBY_PUBLIC_URL", "https://aetrix.example")
    monkeypatch.delenv("DRIVE_OAUTH2_CLIENT_ID", raising=False)
    monkeypatch.delenv("DRIVE_OAUTH2_CLIENT_SECRET", raising=False)
    do._probe_thread = None
    yield
    do._probe_thread = None


@pytest.fixture()
def oauth2_env(monkeypatch):
    monkeypatch.setenv("DRIVE_OAUTH2_CLIENT_ID", "client2-id")
    monkeypatch.setenv("DRIVE_OAUTH2_CLIENT_SECRET", "client2-secret")


class _FakeResp:
    def __init__(self, status_code, payload):
        self.status_code = status_code
        self._payload = payload

    def json(self):
        if isinstance(self._payload, dict):
            return self._payload
        raise ValueError("not json")

    @property
    def text(self):
        return self._payload if isinstance(self._payload, str) else ""


def _token_post_factory(behavior="ok", calls=None):
    """token 端点桩（与 P2 套件同款行为集），calls 收每次请求 data。"""
    def _post(url, data=None, timeout=30):
        if calls is not None:
            calls.append(data or {})
        if behavior == "invalid_grant":
            return _FakeResp(400, {"error": "invalid_grant"})
        grant = (data or {}).get("grant_type", "")
        if grant == "authorization_code":
            return _FakeResp(200, {"access_token": "AT-CODE",
                                   "refresh_token": "RT-CODE",
                                   "expires_in": 3600,
                                   "token_type": "Bearer"})
        return _FakeResp(200, {"access_token": "AT-NEW",
                               "refresh_token": "RT-NEW",
                               "expires_in": 3600,
                               "token_type": "Bearer"})
    return _post


def _about_get_factory():
    def _get(url, headers=None, timeout=30):
        return _FakeResp(200, {"user": {"emailAddress": "u@example.com"}})
    return _get


def _web_user(db, user_id):
    user = base_models.WebUser(
        id=user_id, username=f"oauth2-user-{user_id}",
        password_hash="x", is_active=True)
    db.add(user)
    db.commit()
    return user


def _grant_view_credential(db, user_id):
    """给用户在当前服（公益服）落一条永久查看凭证（EmbyViewUnlock）。

    判定走 subscriptions.view_grant：公益服 = 未过期解锁行即凭证。
    本帮手同时把该公益服设为面板当前服（view_grant 无 realm 参数
    时的解析口径），保证判定命中的就是这条行。
    """
    user = _web_user(db, user_id)
    realm = base_models.ServerRealm(
        name="公益服", slug=f"free-{user_id}", access_mode="free",
        is_active=True)
    db.add(realm)
    db.commit()
    realms.set_active_realm(db, realm.id)
    db.add(base_models.EmbyViewUnlock(
        user_id=user_id, realm_id=realm.id, unlocked_at=datetime.now(),
        expires_at=None, points_spent=0))
    db.commit()
    return user


def _state_of(started):
    query = parse_qs(urlparse(started["authorization_url"]).query)
    return unquote(query["state"][0])


def _start_and_complete(db, user_id=7, label="我的盘", client=None,
                        post=None, get=None):
    started = do.start_drive_oauth(db, user_id, label, client=client)
    binding = do.complete_drive_oauth(
        db, _state_of(started), "CODE-123",
        http_post=post or _token_post_factory(),
        http_get=get or _about_get_factory())
    return binding


def _expire_binding_token(db, binding, refresh="RT-OLD"):
    """把绑定凭据里的令牌改成已过期（逼刷新路径真走 token 端点）。"""
    cred = (db.query(em.MountCredential)
            .filter(em.MountCredential.id == binding.credential_id).first())
    cred.sealed_credential = seal_secret(do.encode_drive_token(
        "AT-OLD", refresh, int(time.time()) - 10))
    db.commit()


def _http_request(host="aetrix.example"):
    scope = {"type": "http", "method": "POST", "path": "/",
             "headers": [(b"host", host.encode())], "query_string": b"",
             "scheme": "https", "server": (host, 443),
             "client": ("127.0.0.1", 40000)}
    return StarletteRequest(scope)


# ---------------------------------------------------------------------------
# start：第二套客户端 + 门禁 + 未配置
# ---------------------------------------------------------------------------

def test_oauth2_start_uses_second_client_and_records_state(
        db, oauth2_env):
    _grant_view_credential(db, 7)
    started = do.start_drive_oauth(db, 7, "新盘", client=do.CLIENT_OAUTH2)
    url = started["authorization_url"]
    assert "client_id=client2-id" in url
    assert "client_id=test-client-id" not in url
    row = db.query(em.DriveOAuthState).first()
    assert row is not None
    assert row.oauth_client == "oauth2"


def test_oauth2_start_rejected_without_view_credential(db, oauth2_env):
    _web_user(db, 8)  # 有账号、无查看凭证
    with pytest.raises(do.DriveOAuthError) as exc:
        do.start_drive_oauth(db, 8, "新盘", client=do.CLIENT_OAUTH2)
    assert exc.value.kind == "forbidden"
    # 拒绝必须发生在建 state 行之前，不留半截会话
    assert db.query(em.DriveOAuthState).count() == 0


def test_oauth2_start_rejected_for_unknown_user(db, oauth2_env):
    with pytest.raises(do.DriveOAuthError) as exc:
        do.start_drive_oauth(db, 999, "新盘", client=do.CLIENT_OAUTH2)
    assert exc.value.kind == "forbidden"
    assert db.query(em.DriveOAuthState).count() == 0


def test_oauth2_start_rejected_when_unconfigured(db):
    _grant_view_credential(db, 7)
    with pytest.raises(do.DriveOAuthError) as exc:
        do.start_drive_oauth(db, 7, "新盘", client=do.CLIENT_OAUTH2)
    assert exc.value.kind == "validation"
    assert db.query(em.DriveOAuthState).count() == 0


def test_primary_start_needs_no_view_credential(db):
    # 老入口不受查看凭证门禁管辖：无用户行也照常发起（P2 既有口径）。
    started = do.start_drive_oauth(db, 7, "老盘")
    assert "client_id=test-client-id" in started["authorization_url"]
    row = db.query(em.DriveOAuthState).first()
    assert (row.oauth_client or "primary") == "primary"


# ---------------------------------------------------------------------------
# complete：state 分流换 token + 来源落行 + 回调复核
# ---------------------------------------------------------------------------

def test_oauth2_complete_exchanges_with_second_client(db, oauth2_env):
    _grant_view_credential(db, 7)
    calls = []
    binding = _start_and_complete(
        db, 7, "新盘", client=do.CLIENT_OAUTH2,
        post=_token_post_factory(calls=calls))
    assert binding.status == "active"
    assert binding.oauth_client == "oauth2"
    exchange = [c for c in calls
                if c.get("grant_type") == "authorization_code"]
    assert len(exchange) == 1
    assert exchange[0]["client_id"] == "client2-id"
    assert exchange[0]["client_secret"] == "client2-secret"
    # 序列化（用户端与管理端共用）带来源标记
    assert ub.serialize_binding(binding)["oauth_client"] == "oauth2"


def test_primary_complete_unchanged_and_marked_primary(db):
    calls = []
    binding = _start_and_complete(
        db, 7, "老盘", post=_token_post_factory(calls=calls))
    assert binding.status == "active"
    assert (binding.oauth_client or "primary") == "primary"
    exchange = [c for c in calls
                if c.get("grant_type") == "authorization_code"]
    assert len(exchange) == 1
    assert exchange[0]["client_id"] == "test-client-id"
    assert exchange[0]["client_secret"] == "test-client-secret"
    assert ub.serialize_binding(binding)["oauth_client"] == "primary"


def test_callback_rechecks_view_credential(db, oauth2_env):
    _grant_view_credential(db, 7)
    started = do.start_drive_oauth(db, 7, "新盘", client=do.CLIENT_OAUTH2)
    state = _state_of(started)
    # 发起后凭证被收回：回调端必须复核，不能只信发起时判过
    (db.query(base_models.EmbyViewUnlock)
     .filter(base_models.EmbyViewUnlock.user_id == 7).delete())
    db.commit()
    with pytest.raises(do.DriveOAuthError) as exc:
        do.complete_drive_oauth(
            db, state, "CODE-123",
            http_post=_token_post_factory(), http_get=_about_get_factory())
    assert exc.value.kind == "forbidden"
    assert db.query(em.UserStorageBinding).count() == 0
    # state 已烧：同一 state 再来只能是 not_found，不许重试换 token
    with pytest.raises(do.DriveOAuthError) as exc2:
        do.complete_drive_oauth(
            db, state, "CODE-123",
            http_post=_token_post_factory(), http_get=_about_get_factory())
    assert exc2.value.kind == "not_found"


# ---------------------------------------------------------------------------
# 刷新：按绑定行记录选客户端
# ---------------------------------------------------------------------------

def test_refresh_uses_client_recorded_on_binding(db, oauth2_env):
    _grant_view_credential(db, 7)
    binding = _start_and_complete(db, 7, "新盘", client=do.CLIENT_OAUTH2)
    _expire_binding_token(db, binding)
    calls = []
    token = do.get_user_drive_access_token(
        db, 7, http_post=_token_post_factory(calls=calls))
    assert token == "AT-NEW"
    refresh = [c for c in calls if c.get("grant_type") == "refresh_token"]
    assert len(refresh) == 1
    assert refresh[0]["client_id"] == "client2-id"
    assert refresh[0]["client_secret"] == "client2-secret"


def test_refresh_primary_binding_uses_primary_client(db):
    binding = _start_and_complete(db, 7, "老盘")
    _expire_binding_token(db, binding)
    calls = []
    token = do.get_user_drive_access_token(
        db, 7, http_post=_token_post_factory(calls=calls))
    assert token == "AT-NEW"
    refresh = [c for c in calls if c.get("grant_type") == "refresh_token"]
    assert len(refresh) == 1
    assert refresh[0]["client_id"] == "test-client-id"
    assert refresh[0]["client_secret"] == "test-client-secret"


def test_verify_binding_refresh_uses_recorded_client(db, oauth2_env):
    _grant_view_credential(db, 7)
    binding = _start_and_complete(db, 7, "新盘", client=do.CLIENT_OAUTH2)
    _expire_binding_token(db, binding)
    calls = []
    out = do.refresh_drive_binding(
        db, 7, binding.id,
        http_post=_token_post_factory(calls=calls),
        http_get=_about_get_factory())
    assert out.status == "active"
    refresh = [c for c in calls if c.get("grant_type") == "refresh_token"]
    assert len(refresh) == 1
    assert refresh[0]["client_id"] == "client2-id"


def test_sweep_refresh_uses_recorded_client(db, oauth2_env):
    _grant_view_credential(db, 7)
    binding = _start_and_complete(db, 7, "新盘", client=do.CLIENT_OAUTH2)
    _expire_binding_token(db, binding)
    calls = []
    summary = do.sweep_drive_credentials(
        db, http_post=_token_post_factory(calls=calls),
        http_get=_about_get_factory())
    assert summary["refreshed"] == 1
    refresh = [c for c in calls if c.get("grant_type") == "refresh_token"]
    assert len(refresh) == 1
    assert refresh[0]["client_id"] == "client2-id"


def test_rebind_via_oauth2_keeps_client_on_binding(db, oauth2_env):
    _grant_view_credential(db, 7)
    binding = _start_and_complete(db, 7, "新盘", client=do.CLIENT_OAUTH2)
    calls = []
    started = do.start_drive_oauth(
        db, 7, "新盘", client=do.CLIENT_OAUTH2,
        rebind_binding_id=binding.id)
    out = do.complete_drive_oauth(
        db, _state_of(started), "CODE-456",
        http_post=_token_post_factory(calls=calls),
        http_get=_about_get_factory())
    assert out.id == binding.id
    assert out.oauth_client == "oauth2"
    assert db.query(em.UserStorageBinding).count() == 1
    exchange = [c for c in calls
                if c.get("grant_type") == "authorization_code"]
    assert exchange[0]["client_id"] == "client2-id"


# ---------------------------------------------------------------------------
# 入口状态口（前端渲染依据）与门户口
# ---------------------------------------------------------------------------

def test_entry_status_available_and_eligible(db, oauth2_env):
    _grant_view_credential(db, 7)
    status = do.oauth2_entry_status(db, 7)
    assert status["available"] is True
    assert status["eligible"] is True


def test_entry_status_ineligible_carries_reason(db, oauth2_env):
    _web_user(db, 8)
    status = do.oauth2_entry_status(db, 8)
    assert status["available"] is True
    assert status["eligible"] is False
    assert status["reason"]  # 无凭证用户要能看懂为何用不了


def test_entry_status_unavailable_when_unconfigured(db):
    _grant_view_credential(db, 7)
    status = do.oauth2_entry_status(db, 7)
    assert status["available"] is False


def test_unconfigured_oauth2_leaves_primary_flow_untouched(db):
    binding = _start_and_complete(db, 7, "老盘")
    assert binding.status == "active"
    assert do.oauth2_configured() is False


def test_portal_oauth2_start_403_for_ineligible(db, oauth2_env):
    _web_user(db, 8)
    with pytest.raises(HTTPException) as exc:
        portal.start_drive_oauth2_binding(
            portal.DriveOAuthStart(account_label="新盘"),
            _http_request(), Response(), SimpleNamespace(id=8), db)
    assert exc.value.status_code == 403


def test_portal_oauth2_start_ok_for_eligible(db, oauth2_env):
    _grant_view_credential(db, 7)
    result = portal.start_drive_oauth2_binding(
        portal.DriveOAuthStart(account_label="新盘"),
        _http_request(), Response(), SimpleNamespace(id=7), db)
    assert "client_id=client2-id" in result["authorization_url"]


def test_providers_endpoint_carries_oauth2_status(db, oauth2_env):
    _web_user(db, 8)
    result = portal.list_storage_binding_providers(
        SimpleNamespace(id=8), db)
    assert "providers" in result
    assert result["drive_oauth2"]["available"] is True
    assert result["drive_oauth2"]["eligible"] is False
