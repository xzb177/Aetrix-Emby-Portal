"""BYOS P2（Drive OAuth + 用户凭据维护）。

钉死的范围（多一件不做）：
- OAuth：start 建 state 行 + 返回授权地址（offline + consent，
  否则拿不到 refresh_token）；callback 单次验讫换 token + 验活后
  建双行；三档失败落行、一过性失败不落行、state 复用/过期拒绝；
- 凭据分离：取令牌唯一口只读 scope=user 且 owner=本人的凭据行，
  绝不回落 SA 池 / rclone.conf / scope=source 行；
- 刷新与维护：快过期刷新写回、invalid_grant 落 expired、429 不落行；
  维护工人只扫用户 Drive 绑定行，源级行跳过不碰；开关默认关。
Google / Drive 网络全部用桩替身，不碰真实上游。
"""

import threading
import time

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend import models as base_models
from backend.emby_server import models as em
from backend.emby_server import drive_oauth as do
from backend.emby_server import user_bindings as ub
from backend.emby_server.upstream_sources import seal_secret, unseal_secret


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
    yield session
    session.close()


@pytest.fixture(autouse=True)
def _oauth_env(monkeypatch):
    monkeypatch.setenv("DRIVE_OAUTH_CLIENT_ID", "test-client-id")
    monkeypatch.setenv("DRIVE_OAUTH_CLIENT_SECRET", "test-client-secret")
    monkeypatch.setenv("EMBY_PUBLIC_URL", "https://aetrix.example")
    monkeypatch.delenv("BYOS_DRIVE_CRED_MAINT_ENABLED", raising=False)
    do._probe_thread = None
    yield
    do._probe_thread = None


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
    """token 端点桩：ok / invalid_grant / invalid_client / throttled / transient。"""
    def _post(url, data=None, timeout=30):
        if calls is not None:
            calls.append(data or {})
        grant = (data or {}).get("grant_type", "")
        if behavior == "invalid_grant":
            return _FakeResp(400, {"error": "invalid_grant"})
        if behavior == "invalid_client":
            return _FakeResp(400, {"error": "invalid_client"})
        if behavior == "throttled":
            return _FakeResp(429, {"error": "rate_limit_exceeded"})
        if behavior == "transient":
            return _FakeResp(500, {"error": "server_error"})
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


def _about_get_factory(behavior="ok"):
    def _get(url, headers=None, timeout=30):
        if behavior == "unauthorized":
            return _FakeResp(401, {"error": {"code": 401, "message": "bad"}})
        return _FakeResp(200, {"user": {"emailAddress": "u@example.com"}})
    return _get


def _start_and_complete(db, user_id=7, label="我的盘",
                        post=None, get=None):
    started = do.start_drive_oauth(db, user_id, label)
    url = started["authorization_url"]
    state = url.split("state=")[1].split("&")[0]
    from urllib.parse import unquote

    state = unquote(state)
    binding = do.complete_drive_oauth(
        db, state, "CODE-123",
        http_post=post or _token_post_factory(),
        http_get=get or _about_get_factory())
    return binding


def _seed_drive_binding(db, user_id=7, label="盘", status="active",
                        expires_at=None, refresh="RT-OLD",
                        access="AT-OLD", scope="user", owner=None):
    """直接造双行（绕开 OAuth，用于刷新/维护/分离测试）。"""
    token = do.encode_drive_token(
        access, refresh,
        expires_at if expires_at is not None else int(time.time()) + 3600)
    cred = em.MountCredential(
        mount_id=None, scope=scope,
        owner_user_id=user_id if owner is None else owner,
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


# ---------------------------------------------------------------------------
# start
# ---------------------------------------------------------------------------

def test_start_returns_auth_url_with_offline_and_pkce(db):
    started = do.start_drive_oauth(db, 7, "我的盘")
    url = started["authorization_url"]
    assert url.startswith("https://accounts.google.com/o/oauth2/v2/auth")
    assert "access_type=offline" in url
    assert "prompt=consent" in url
    assert "code_challenge=" in url and "code_challenge_method=S256" in url
    assert "state=" in url
    row = db.query(em.DriveOAuthState).one()
    assert row.user_id == 7 and row.account_label == "我的盘"
    assert row.state_hash and len(row.state_hash) == 64
    assert unseal_secret(row.sealed_verifier)


def test_start_rejects_without_client_env_or_public_url(db, monkeypatch):
    monkeypatch.delenv("DRIVE_OAUTH_CLIENT_ID")
    with pytest.raises(do.DriveOAuthError) as exc:
        do.start_drive_oauth(db, 7, "盘")
    assert exc.value.kind == "validation"
    monkeypatch.setenv("DRIVE_OAUTH_CLIENT_ID", "x")
    monkeypatch.delenv("EMBY_PUBLIC_URL")
    with pytest.raises(do.DriveOAuthError) as exc:
        do.start_drive_oauth(db, 7, "盘")
    assert exc.value.kind == "validation"


def test_start_rejects_dup_label_and_limit(db):
    _start_and_complete(db, 7, "盘")
    with pytest.raises(do.DriveOAuthError) as exc:
        do.start_drive_oauth(db, 7, "盘")
    assert exc.value.kind == "validation"
    assert db.query(em.DriveOAuthState).count() == 0
    # 上限：直接造 3 行占满默认上限（3），再 start 即拒。
    for i in range(3):
        _seed_drive_binding(db, 9, f"盘{i}")
    with pytest.raises(do.DriveOAuthError) as exc:
        do.start_drive_oauth(db, 9, "新盘")
    assert exc.value.kind == "validation"


# ---------------------------------------------------------------------------
# complete
# ---------------------------------------------------------------------------

def test_complete_happy_creates_active_user_scoped_rows(db):
    binding = _start_and_complete(db, 7, "我的盘")
    assert binding.status == "active"
    assert binding.last_verified_at is not None
    cred = db.query(em.MountCredential).filter(
        em.MountCredential.id == binding.credential_id).one()
    assert cred.scope == "user" and cred.owner_user_id == 7
    assert cred.status == "active"
    token = do.decode_drive_token(unseal_secret(cred.sealed_credential))
    assert token["access_token"] == "AT-CODE"
    assert token["refresh_token"] == "RT-CODE"
    assert db.query(em.DriveOAuthState).count() == 0


def test_complete_state_single_use_and_expired(db):
    started = do.start_drive_oauth(db, 7, "盘")
    from urllib.parse import unquote

    state = unquote(started["authorization_url"].split("state=")[1].split("&")[0])
    binding = do.complete_drive_oauth(
        db, state, "CODE-123",
        http_post=_token_post_factory(), http_get=_about_get_factory())
    assert binding.status == "active"
    with pytest.raises(do.DriveOAuthError) as exc:
        do.complete_drive_oauth(
            db, state, "CODE-123",
            http_post=_token_post_factory(), http_get=_about_get_factory())
    assert exc.value.kind == "not_found"

    started2 = do.start_drive_oauth(db, 8, "盘3")
    state2 = unquote(started2["authorization_url"].split("state=")[1].split("&")[0])
    row = db.query(em.DriveOAuthState).filter(
        em.DriveOAuthState.state_hash == do._hash_state(state2)).one()
    from datetime import datetime, timedelta

    row.expires_at = datetime.now() - timedelta(seconds=1)
    db.commit()
    with pytest.raises(do.DriveOAuthError) as exc:
        do.complete_drive_oauth(
            db, state2, "CODE-123",
            http_post=_token_post_factory(), http_get=_about_get_factory())
    assert exc.value.kind == "not_found"
    assert db.query(em.DriveOAuthState).count() == 0


def test_complete_invalid_grant_creates_expired_row(db):
    started = do.start_drive_oauth(db, 7, "盘")
    from urllib.parse import unquote

    state = unquote(started["authorization_url"].split("state=")[1].split("&")[0])
    binding = do.complete_drive_oauth(
        db, state, "CODE-123",
        http_post=_token_post_factory("invalid_grant"),
        http_get=_about_get_factory())
    assert binding.status == "expired"
    assert binding.last_error_kind == "expired"


def test_complete_transient_creates_no_rows(db):
    started = do.start_drive_oauth(db, 7, "盘")
    from urllib.parse import unquote

    state = unquote(started["authorization_url"].split("state=")[1].split("&")[0])
    with pytest.raises(do.DriveOAuthError) as exc:
        do.complete_drive_oauth(
            db, state, "CODE-123",
            http_post=_token_post_factory("transient"),
            http_get=_about_get_factory())
    assert exc.value.kind == "transient"
    assert db.query(em.UserStorageBinding).count() == 0
    assert db.query(em.DriveOAuthState).count() == 0


def test_complete_without_refresh_token_is_auth_error_row(db):
    def _post_no_refresh(url, data=None, timeout=30):
        return _FakeResp(200, {"access_token": "AT-X", "expires_in": 3600})

    started = do.start_drive_oauth(db, 7, "盘")
    from urllib.parse import unquote

    state = unquote(started["authorization_url"].split("state=")[1].split("&")[0])
    binding = do.complete_drive_oauth(
        db, state, "CODE-123",
        http_post=_post_no_refresh, http_get=_about_get_factory())
    assert binding.status == "invalid"
    assert binding.last_error_kind == "auth_error"


# ---------------------------------------------------------------------------
# 凭据分离：唯一口绝不回落
# ---------------------------------------------------------------------------

def test_accessor_never_falls_back_to_source_rows(db):
    _start_and_complete(db, 7, "我的盘")
    # 源级行（服级 SA 池口径的替身）：纵有令牌也不给出。
    source_cred = em.MountCredential(
        mount_id=999, scope="source", owner_user_id=None,
        sealed_credential=seal_secret(do.encode_drive_token(
            "AT-SOURCE", "RT-SOURCE", int(time.time()) + 3600)),
        key_id="fernet-v1", status="active")
    db.add(source_cred)
    db.commit()
    assert do.get_user_drive_access_token(db, 7) == "AT-CODE"
    with pytest.raises(do.DriveOAuthError) as exc:
        do.get_user_drive_access_token(db, 8)
    assert exc.value.kind == "not_found"


def test_accessor_refreshes_expiring_and_marks_expired(db):
    binding, cred = _seed_drive_binding(
        db, 7, "盘", expires_at=int(time.time()) - 10)
    calls = []
    assert do.get_user_drive_access_token(
        db, 7, http_post=_token_post_factory(calls=calls)) == "AT-NEW"
    assert len(calls) == 1
    token = do.decode_drive_token(unseal_secret(
        db.query(em.MountCredential).filter(
            em.MountCredential.id == cred.id).one().sealed_credential))
    assert token["access_token"] == "AT-NEW"
    assert token["refresh_token"] == "RT-NEW"

    binding2, cred2 = _seed_drive_binding(
        db, 8, "盘2", expires_at=int(time.time()) - 10)
    with pytest.raises(do.DriveOAuthError) as exc:
        do.get_user_drive_access_token(
            db, 8, http_post=_token_post_factory("invalid_grant"))
    assert exc.value.kind == "expired"
    db.refresh(binding2)
    assert binding2.status == "expired"


def test_accessor_throttled_does_not_mark_row(db):
    binding, cred = _seed_drive_binding(
        db, 7, "盘", expires_at=int(time.time()) - 10)
    with pytest.raises(do.DriveOAuthError) as exc:
        do.get_user_drive_access_token(
            db, 7, http_post=_token_post_factory("throttled"))
    assert exc.value.kind == "transient"
    db.refresh(binding)
    assert binding.status == "active"


def test_verify_binding_drive_path_uses_refresh(db, monkeypatch):
    binding, cred = _seed_drive_binding(
        db, 7, "盘", expires_at=int(time.time()) - 10)
    import backend.emby_server.drive_oauth as do_mod
    # verify_binding 不接受 http 注入：桩掉模块级默认 post/get。
    monkeypatch.setattr(do_mod, "_default_http_post", _token_post_factory())
    monkeypatch.setattr(do_mod, "_default_http_get", _about_get_factory())
    out = ub.verify_binding(db, 7, binding.id)
    assert out.status == "active"


# ---------------------------------------------------------------------------
# 维护工人
# ---------------------------------------------------------------------------

def test_sweep_only_touches_user_drive_rows(db):
    now = int(time.time())
    b_ok, _ = _seed_drive_binding(db, 7, "快过期", expires_at=now - 10)
    b_fresh, _ = _seed_drive_binding(db, 8, "还新鲜", expires_at=now + 3600)
    b_exp, _ = _seed_drive_binding(db, 9, "已过期", status="expired",
                                   expires_at=now - 100)
    b_bad, _ = _seed_drive_binding(db, 10, "源级冒充", scope="source",
                                   owner=None, expires_at=now - 10)
    b_inv, _ = _seed_drive_binding(db, 11, "已失效", status="invalid",
                                   expires_at=now - 10)
    b_thr, _ = _seed_drive_binding(db, 12, "被限流", expires_at=now - 10,
                                   refresh="RT-THROTTLE")

    def _mixed_post(url, data=None, timeout=30):
        # 按 refresh_token 路由：被限流行回 429，其余行正常刷新。
        if (data or {}).get("refresh_token") == "RT-THROTTLE":
            return _FakeResp(429, {"error": "rate_limit_exceeded"})
        return _token_post_factory()(url, data, timeout)

    summary = do.sweep_drive_credentials(db, http_post=_mixed_post)
    assert summary == {"scanned": 6, "refreshed": 1, "auth_expired": 1,
                       "auth_error": 0, "throttled": 1, "transient_errors": 0,
                       "skipped": 3}
    db.refresh(b_ok)
    assert b_ok.status == "active"
    db.refresh(b_bad)
    assert b_bad.status == "active"  # 源级行没被碰（仍是造时的 active）
    db.refresh(b_exp)
    assert b_exp.status == "expired"
    db.refresh(b_thr)
    assert b_thr.status == "active"  # 限流行不落行，下轮再试


def test_maintenance_worker_gated_off_by_default(db, monkeypatch):
    assert do.maint_enabled() is False
    assert do.start_drive_cred_maintenance_worker() is False


def test_maintenance_worker_runs_sweep_when_on(db, monkeypatch):
    import backend.database as database

    calls = []

    class _FakeSession:
        def close(self):
            pass

    monkeypatch.setenv("BYOS_DRIVE_CRED_MAINT_ENABLED", "1")
    monkeypatch.setenv("BYOS_DRIVE_CRED_MAINT_INTERVAL_SEC", "60")
    monkeypatch.setattr(database, "SessionLocal", lambda: _FakeSession())
    monkeypatch.setattr(do, "sweep_drive_credentials",
                        lambda session: calls.append(1) or {"scanned": 0})
    stop = threading.Event()
    assert do.start_drive_cred_maintenance_worker(stop) is True
    deadline = time.time() + 5
    while time.time() < deadline and not calls:
        time.sleep(0.05)
    stop.set()
    assert calls, "启动即跑一轮未触发"
