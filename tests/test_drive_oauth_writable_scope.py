"""Drive 可写范围（只读 + file）：授权范围、实授入库、重绑原地换凭据、
重绑重置建目录失败任务。Google / Drive 网络全部用桩替身，不碰真实上游。

钉死的口径（2026-10-27 转存 mkdir 403 根因的修复）：
- 授权请求发两档范围（readonly + file），缺一不可；单 file 不够
  （读共享源与 BYOS 已有文件不在 file 覆盖里），全盘 drive 只当
  组合实测失败时的 fallback，不擅自升级；
- 回调以 Google token 响应原样回的已授范围为准入库，不拿请求串冒充；
- 老绑定无记录一律按需重绑（老代码只申请过只读，不可能自带写权限）；
- 重绑原地换凭据（绑定行 id、凭据行 id、label 全不变），不新增行；
- 重绑成功把该用户建转存目录失败的 failed 任务打回 pending（工人只捡
  pending、门禁对 failed 返 409、24h 节流挡新任务，三重死锁不重置
  永远播不了）；其它失败原因的任务不许顺手复活；
- 刷新沿用旧存 scope，不拿请求串覆盖。
"""

import time
from urllib.parse import parse_qs, unquote, urlparse

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


READONLY = do.DRIVE_READONLY_SCOPE
FILE = do.DRIVE_FILE_SCOPE
COMBINED = READONLY + " " + FILE


def _token_post_factory(granted_scope=COMBINED, behavior="ok", calls=None):
    """token 端点桩：授权码换 token 回实授 scope；吊销只记录不报错。"""
    def _post(url, data=None, timeout=30):
        if calls is not None:
            calls.append((url, data or {}))
        if "revoke" in url:
            return _FakeResp(200, {})
        grant = (data or {}).get("grant_type", "")
        if behavior == "invalid_grant":
            return _FakeResp(400, {"error": "invalid_grant"})
        if grant == "refresh_token":
            return _FakeResp(200, {
                "access_token": "AT-REFRESHED",
                "refresh_token": (data or {}).get("refresh_token"),
                "expires_in": 3600,
            })
        return _FakeResp(200, {
            "access_token": "AT-CODE",
            "refresh_token": "RT-CODE",
            "expires_in": 3600,
            "scope": granted_scope,
        })
    return _post


def _about_get_factory():
    def _get(url, headers=None, timeout=30):
        return _FakeResp(200, {"user": {"emailAddress": "u@example.com"}})
    return _get


def _state_of(started):
    url = started["authorization_url"]
    return unquote(parse_qs(urlparse(url).query)["state"][0])


def _seed_binding(db, user_id=7, label="我的盘", status="active",
                  token_scope=READONLY, refresh="RT-OLD"):
    token = do.encode_drive_token(
        "AT-OLD", refresh, int(time.time()) + 3600, scope=token_scope)
    cred = em.MountCredential(
        mount_id=None, scope="user", owner_user_id=user_id,
        sealed_credential=seal_secret(token), key_id="fernet-v1",
        status=status)
    db.add(cred)
    db.flush()
    binding = em.UserStorageBinding(
        user_id=user_id, provider="drive", account_label=label,
        credential_id=cred.id, status=status,
        granted_scopes=token_scope)
    db.add(binding)
    db.commit()
    return binding, cred


def _seed_job(db, item_id, user_id="7", status="failed",
              error_detail="建转存目录失败：403", attempts=3):
    from datetime import datetime, timedelta
    job = em.TransferJob(
        item_id=item_id, trigger="playback", provider="drive",
        owner_scope="user", owner_ref=user_id, status=status,
        attempts=attempts, error_kind="retriable" if status == "failed" else None,
        error_detail=error_detail,
        next_retry_at=datetime.now() - timedelta(seconds=60)
        if status == "failed" else None)
    db.add(job)
    db.commit()
    return job


# ---------------------------------------------------------------------------
# 授权范围与实授入库
# ---------------------------------------------------------------------------

def test_auth_url_requests_both_scopes(db):
    started = do.start_drive_oauth(db, 7, "盘")
    scope = parse_qs(urlparse(started["authorization_url"]).query)["scope"][0]
    assert set(scope.split()) == {READONLY, FILE}


def test_complete_new_bind_stores_granted_scope(db):
    started = do.start_drive_oauth(db, 7, "盘")
    binding = do.complete_drive_oauth(
        db, _state_of(started), "CODE-123",
        http_post=_token_post_factory(), http_get=_about_get_factory())
    assert binding.status == "active"
    assert binding.granted_scopes == COMBINED
    serialized = ub.serialize_binding(binding)
    assert serialized["needs_rebind"] is False
    assert serialized["granted_scopes"] == [READONLY, FILE]
    token = do.decode_drive_token(unseal_secret(
        db.query(em.MountCredential).filter(
            em.MountCredential.id == binding.credential_id).one()
        .sealed_credential))
    assert token["scope"] == COMBINED


def test_complete_readonly_only_grant_stays_needs_rebind(db):
    started = do.start_drive_oauth(db, 7, "盘")
    binding = do.complete_drive_oauth(
        db, _state_of(started), "CODE-123",
        http_post=_token_post_factory(granted_scope=READONLY),
        http_get=_about_get_factory())
    assert binding.status == "active"
    assert ub.serialize_binding(binding)["needs_rebind"] is True


def test_needs_rebind_legacy_none_and_file_only(db):
    assert do.binding_needs_rebind(None) is True
    assert do.binding_needs_rebind("") is True
    assert do.binding_needs_rebind(READONLY) is True
    assert do.binding_needs_rebind(COMBINED) is False
    assert do.binding_needs_rebind(FILE) is False


# ---------------------------------------------------------------------------
# 重绑 start
# ---------------------------------------------------------------------------

def test_start_rebind_same_label_allowed_and_target_checked(db):
    binding, _ = _seed_binding(db, 7, "我的盘")
    started = do.start_drive_oauth(
        db, 7, "我的盘", rebind_binding_id=binding.id)
    row = db.query(em.DriveOAuthState).one()
    assert row.rebind_binding_id == binding.id
    assert row.account_label == "我的盘"
    # 他人绑定、不存在的 id 一律 validation，不生成 state。
    _seed_binding(db, 8, "别人的盘")
    other = db.query(em.UserStorageBinding).filter(
        em.UserStorageBinding.user_id == 8).one()
    with pytest.raises(do.DriveOAuthError) as exc:
        do.start_drive_oauth(db, 7, "我的盘", rebind_binding_id=other.id)
    assert exc.value.kind == "validation"
    with pytest.raises(do.DriveOAuthError) as exc:
        do.start_drive_oauth(db, 7, "我的盘", rebind_binding_id=99999)
    assert exc.value.kind == "validation"


def test_start_rebind_not_blocked_by_quota(db):
    first, _ = _seed_binding(db, 9, "盘0")
    _seed_binding(db, 9, "盘1")
    _seed_binding(db, 9, "盘2")
    # 3 行已占满默认上限（3），新绑会被拒；重绑自身不新增行，必须放行。
    with pytest.raises(do.DriveOAuthError):
        do.start_drive_oauth(db, 9, "新盘")
    started = do.start_drive_oauth(
        db, 9, "盘0", rebind_binding_id=first.id)
    assert started["authorization_url"]


# ---------------------------------------------------------------------------
# 重绑 complete：原地换凭据 + 重置任务 + 吊销旧 refresh
# ---------------------------------------------------------------------------

def test_complete_rebind_replaces_in_place_and_resets_mkdir_jobs(db):
    binding, cred = _seed_binding(db, 7, "我的盘")
    mkdir_job = _seed_job(db, 101)
    other_failed = _seed_job(db, 102, error_detail="源文件已删", attempts=2)
    pending_job = _seed_job(db, 103, status="pending",
                            error_detail=None, attempts=0)
    other_user_job = _seed_job(db, 104, user_id="8")
    calls = []
    started = do.start_drive_oauth(
        db, 7, "我的盘", rebind_binding_id=binding.id)
    out = do.complete_drive_oauth(
        db, _state_of(started), "CODE-123",
        http_post=_token_post_factory(calls=calls),
        http_get=_about_get_factory())
    # 原地：绑定行 id、凭据行 id、label 全不变，不新增行。
    assert out.id == binding.id
    assert out.credential_id == cred.id
    assert out.account_label == "我的盘"
    assert out.status == "active"
    assert out.granted_scopes == COMBINED
    assert db.query(em.UserStorageBinding).count() == 1
    assert db.query(em.MountCredential).count() == 1
    assert ub.serialize_binding(out)["needs_rebind"] is False
    token = do.decode_drive_token(unseal_secret(
        db.query(em.MountCredential).filter(
            em.MountCredential.id == cred.id).one().sealed_credential))
    assert token["access_token"] == "AT-CODE"
    assert token["refresh_token"] == "RT-CODE"
    assert token["scope"] == COMBINED
    # 旧 refresh 被吊销（尽力，不挡结果）。
    assert any("revoke" in url and data.get("token") == "RT-OLD"
               for url, data in calls)
    # 任务：只有建目录失败的本用户 failed 行被打回 pending。
    db.refresh(mkdir_job)
    assert mkdir_job.status == "pending"
    assert mkdir_job.attempts == 0
    assert mkdir_job.error_kind is None
    assert mkdir_job.error_detail is None
    assert mkdir_job.next_retry_at is None
    db.refresh(other_failed)
    assert other_failed.status == "failed" and other_failed.attempts == 2
    db.refresh(pending_job)
    assert pending_job.status == "pending"
    db.refresh(other_user_job)
    assert other_user_job.status == "failed"


def test_complete_rebind_failure_marks_target_not_new_rows(db):
    binding, _ = _seed_binding(db, 7, "我的盘")
    started = do.start_drive_oauth(
        db, 7, "我的盘", rebind_binding_id=binding.id)
    out = do.complete_drive_oauth(
        db, _state_of(started), "CODE-123",
        http_post=_token_post_factory(behavior="invalid_grant"),
        http_get=_about_get_factory())
    assert out.id == binding.id
    assert out.status == "expired"
    assert out.last_error_kind == "expired"
    assert db.query(em.UserStorageBinding).count() == 1
    assert db.query(em.MountCredential).count() == 1


# ---------------------------------------------------------------------------
# 刷新沿用旧存 scope
# ---------------------------------------------------------------------------

def test_refresh_preserves_stored_scope(db):
    binding, cred = _seed_binding(db, 7, "盘", token_scope=READONLY)
    do._store_refreshed_token(db, binding, cred, {
        "access_token": "AT-NEW", "refresh_token": "RT-OLD",
        "expires_at": int(time.time()) + 3600})
    token = do.decode_drive_token(unseal_secret(cred.sealed_credential))
    assert token["scope"] == READONLY
    binding2, cred2 = _seed_binding(db, 8, "盘2", token_scope=COMBINED)
    do._store_refreshed_token(db, binding2, cred2, {
        "access_token": "AT-NEW", "refresh_token": "RT-OLD",
        "expires_at": int(time.time()) + 3600})
    token2 = do.decode_drive_token(unseal_secret(cred2.sealed_credential))
    assert token2["scope"] == COMBINED
