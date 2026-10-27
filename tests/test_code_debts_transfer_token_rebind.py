"""代码欠账三项（独立于 FMBY 各路、#1359、#1360）：

① TransferVariant.size_bytes 模型改 BigInteger（生产表已是 BIGINT，
   模型仍是 Integer，PG 下超 2GB 副本写回会溢出）；
② get_binding_drive_access_token 的 force_refresh 真换令牌，且
   transfer_variant.UserDriveApiClient 的 401 重试走它（此前令牌口
   根本没有 force_refresh 参数，客户端纵使传了也被丢掉，401 重试
   拿回同一枚未过期旧令牌、必再 401）；
③ 重绑重置扩到“列转存目录失败：401”（此前只认建目录失败；
   只认 401，列目录 429/5xx 等瞬态不由重绑复活）。

Google / Drive 网络全部用桩替身，不碰真实上游。
"""

import time
from datetime import datetime, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.dialects import postgresql
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend import models as base_models
from backend.emby_server import models as em
from backend.emby_server import drive_oauth as do
from backend.emby_server import transfer_variant as tv
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
    yield


class _FakeResp:
    def __init__(self, status_code, payload):
        self.status_code = status_code
        self._payload = payload

    def json(self):
        return self._payload

    @property
    def text(self):
        return self._payload if isinstance(self._payload, str) else ""


def _seed_binding(db, user_id=7, access="AT-OLD", refresh="RT-OLD",
                  expires_in=3600, scope=None):
    token = do.encode_drive_token(
        access, refresh, int(time.time()) + expires_in, scope=scope)
    cred = em.MountCredential(
        mount_id=None, scope="user", owner_user_id=user_id,
        sealed_credential=seal_secret(token), key_id="fernet-v1",
        status="active")
    db.add(cred)
    db.flush()
    binding = em.UserStorageBinding(
        user_id=user_id, provider="drive", account_label="盘",
        credential_id=cred.id, status="active",
        granted_scopes=scope)
    db.add(binding)
    db.commit()
    return binding, cred


def _refresh_post_factory(calls):
    def _post(url, data=None, timeout=30):
        calls.append((url, data or {}))
        assert (data or {}).get("grant_type") == "refresh_token"
        return _FakeResp(200, {
            "access_token": "AT-NEW",
            "refresh_token": (data or {}).get("refresh_token"),
            "expires_in": 3600,
        })
    return _post


def _seed_job(db, item_id, user_id="7", status="failed",
              error_detail="建转存目录失败：403", attempts=3):
    job = em.TransferJob(
        item_id=item_id, trigger="playback", provider="drive",
        owner_scope="user", owner_ref=user_id, status=status,
        attempts=attempts,
        error_kind="retriable" if status == "failed" else None,
        error_detail=error_detail,
        next_retry_at=datetime.now() - timedelta(seconds=60)
        if status == "failed" else None)
    db.add(job)
    db.commit()
    return job


# ---------------------------------------------------------------------------
# ① size_bytes → BigInteger
# ---------------------------------------------------------------------------

def test_size_bytes_compiles_to_bigint_on_postgresql():
    compiled = em.TransferVariant.__table__.c.size_bytes.type.compile(
        dialect=postgresql.dialect())
    assert compiled == "BIGINT"


def test_size_bytes_roundtrips_over_2gb(db):
    big = 195_738_044_172  # 库里现存最大单体（4K 大片量级，远超 2^31）
    row = em.TransferVariant(
        item_id=1, source_file_id="src", variant_file_id="var",
        owner_scope="user", owner_ref="7", status="ready",
        size_bytes=big)
    db.add(row)
    db.commit()
    got = db.query(em.TransferVariant).filter_by(id=row.id).first()
    assert got.size_bytes == big


# ---------------------------------------------------------------------------
# ② force_refresh 真换令牌 + 401 重试走它
# ---------------------------------------------------------------------------

def test_token_without_force_returns_unexpired_cached(db):
    binding, _ = _seed_binding(db)
    calls = []
    got = do.get_binding_drive_access_token(
        db, binding, http_post=_refresh_post_factory(calls))
    assert got == "AT-OLD"
    assert calls == []  # 未过期且不强制时，一次 token 端点也不碰


def test_token_with_force_refreshes_and_persists(db):
    binding, cred = _seed_binding(db)
    calls = []
    got = do.get_binding_drive_access_token(
        db, binding, http_post=_refresh_post_factory(calls),
        force_refresh=True)
    assert got == "AT-NEW"
    assert len(calls) == 1
    stored = do.decode_drive_token(unseal_secret(cred.sealed_credential))
    assert stored["access_token"] == "AT-NEW"
    assert stored["refresh_token"] == "RT-OLD"  # 未轮换时沿用旧 refresh


def test_token_force_refresh_without_refresh_token_is_expired(db):
    binding, _ = _seed_binding(db, refresh="")
    calls = []
    with pytest.raises(do.DriveOAuthError) as excinfo:
        do.get_binding_drive_access_token(
            db, binding, http_post=_refresh_post_factory(calls),
            force_refresh=True)
    assert excinfo.value.kind == "expired"
    assert calls == []  # 无 refresh 可换，不应空打 token 端点


def test_user_token_port_passes_force_refresh_through(db):
    binding, _ = _seed_binding(db)
    calls = []
    got = do.get_user_drive_access_token(
        db, 7, http_post=_refresh_post_factory(calls), force_refresh=True)
    assert got == "AT-NEW"
    assert len(calls) == 1


def test_client_headers_force_refresh_reaches_token_port(db, monkeypatch):
    binding, _ = _seed_binding(db)
    client = tv.UserDriveApiClient(db, binding)
    client._token = "AT-STALE"
    seen = {}

    def _fake_token(db_arg, binding_arg, force_refresh=False):
        seen["force_refresh"] = force_refresh
        return "AT-FRESH"

    monkeypatch.setattr(do, "get_binding_drive_access_token", _fake_token)
    # 不强制：用缓存令牌，不碰令牌口。
    assert client._headers("myDrive:7")["Authorization"] == "Bearer AT-STALE"
    assert seen == {}
    # 401 重试点：强制刷新必须真传到令牌口，拿回新令牌。
    assert client._headers("myDrive:7", force_refresh=True)[
        "Authorization"] == "Bearer AT-FRESH"
    assert seen["force_refresh"] is True
    assert client._token == "AT-FRESH"


# ---------------------------------------------------------------------------
# ③ 重绑重置扩到“列转存目录失败：401”
# ---------------------------------------------------------------------------

def test_reset_covers_mkdir_and_list_401_only(db):
    mkdir_job = _seed_job(db, 11, error_detail="建转存目录失败：403")
    list_401_job = _seed_job(db, 12, error_detail="列转存目录失败：401")
    list_429_job = _seed_job(db, 13, error_detail="列转存目录失败：429")
    list_500_job = _seed_job(db, 14, error_detail="列转存目录失败：500")
    other_job = _seed_job(db, 15, error_detail="源文件已死")
    other_user_job = _seed_job(db, 16, user_id="8",
                               error_detail="列转存目录失败：401")
    pending_job = _seed_job(db, 17, status="pending",
                            error_detail="列转存目录失败：401")

    assert do.reset_mkdir_failed_jobs(db, 7) == 2
    db.commit()

    for job in (mkdir_job, list_401_job):
        db.refresh(job)
        assert job.status == "pending"
        assert job.attempts == 0
        assert job.error_kind is None
        assert job.error_detail is None
        assert job.next_retry_at is None
    for job in (list_429_job, list_500_job, other_job, other_user_job):
        db.refresh(job)
        assert job.status == "failed"  # 瞬态与他人行不许顺手复活
    db.refresh(pending_job)
    assert pending_job.status == "pending"
    assert pending_job.attempts == 3  # 本就 pending 的行不动


def test_reset_ignores_none_error_detail(db):
    job = _seed_job(db, 18, error_detail=None)
    assert do.reset_mkdir_failed_jobs(db, 7) == 0
    db.refresh(job)
    assert job.status == "failed"
