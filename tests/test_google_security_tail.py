# -*- coding: utf-8 -*-
"""谷歌安全收口（2026-10-30）回归测试。

钉死五件（对应审计报告 ~/workspace/reports/security-audit-google-20261029.md
在当前 main 上复核仍成立的缺口）：

1. OAuth 回调浏览器会话绑定：start 落 nonce_hash、nonce 原文只
   交 portal 下发 cookie；回调校验三态（无/错 cookie 拒且烧
   state 行、会话用户不符拒、相符放行且行保留）＋上线前在途行
   （nonce_hash 为 NULL）fail-closed＋未知 state 透传给 complete
   的 not_found 口径＋cookie Domain 推导。
2. 解绑调 Google revoke：drive 绑定删行前用该绑定 refresh
   token 打吊销端点；吊销抛错不阻塞解绑；pan115 跳过。
3. Path B 暂存可验证化：成功后暂存删不掉落 orphan 登记行；
   清理周期暂存扫荡重试删除、成功删行销账、失败留行；作用域
   只限源盘 orphan（userdrive:/pan115: 的 orphan 不碰）。
4. 服主副本闲置 TTL：owner_scope='source' 的 ready 行纳入
   copy_idle_cleanup（先收后删、任一用户近期播过不算闲置、
   SystemConfig 开关可关、关时用户行口径不受影响）。
5. 死令牌待收回登记：凭据死亡钩子在收回被跳过后落
   share_revoke_pending_at；重绑成功后优先补收并清登记；
   清理周期 run_pending_share_revokes 兜底。

全部离线：Google/Drive 一律桩（monkeypatch 只打类/模块属性），
不碰真实端点；文中的令牌值都是假值。
"""
import os

os.environ.setdefault("DATABASE_TYPE", "sqlite")
os.environ.setdefault("REDIS_ENABLED", "false")

import time
from datetime import datetime, timedelta
from urllib.parse import unquote

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend import models as base_models
from backend.emby_server import models as em
from backend.emby_server import drive_oauth as do
from backend.emby_server import transfer_variant as tv
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
    store.invalidate()
    yield session
    session.close()
    store.invalidate()


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


def _token_post_factory(behavior="ok"):
    """token 端点桩（与 test_byos_p2_drive_oauth 同款行为集）。"""
    def _post(url, data=None, timeout=30):
        grant = (data or {}).get("grant_type", "")
        if behavior == "invalid_grant":
            return _FakeResp(400, {"error": "invalid_grant"})
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


def _seed_drive_binding(db, user_id=7, label="盘", status="active",
                        expires_at=None, refresh="RT-OLD",
                        access="AT-OLD"):
    """直接造双行（绕开 OAuth），凭据里存指定假令牌。"""
    token = do.encode_drive_token(
        access, refresh,
        expires_at if expires_at is not None else int(time.time()) + 3600)
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


def _user_variant(db, item_id, fid, ref, status="ready", drive=None,
                  last_played=None, created=None, pending=None):
    row = em.TransferVariant(
        item_id=item_id, source_file_id="origfid", variant_file_id=fid,
        variant_drive_id=drive if drive is not None else f"userdrive:{ref}",
        owner_scope="user", owner_ref=str(ref), status=status,
        size_bytes=1000, last_played_at=last_played,
        share_revoke_pending_at=pending,
        created_at=created or datetime(2026, 10, 27))
    db.add(row)
    db.commit()
    return row


def _source_variant(db, item_id, fid, created, last_played=None,
                    status="ready"):
    row = em.TransferVariant(
        item_id=item_id, source_file_id="origfid", variant_file_id=fid,
        variant_drive_id="drive1", owner_scope="source", owner_ref=None,
        status=status, size_bytes=1000, last_played_at=last_played,
        created_at=created)
    db.add(row)
    db.commit()
    return row


class _FakeUserClient:
    """UserDriveApiClient 桩：按 fid 表定 remove 行为。"""
    behavior = {}
    seen = []

    def __init__(self, db, binding):
        self.binding = binding
        _FakeUserClient.seen.append(binding)

    def remove_anyone_reader(self, drive_id, fid):
        behavior = _FakeUserClient.behavior.get(fid, "revoked")
        if behavior == "raise":
            raise Exception("网络抖动")
        return behavior

    def delete_file_ignore_missing(self, drive_id, fid):
        if _FakeUserClient.behavior.get(fid) == "raise":
            raise Exception("删文件失败")
        return None


class _FakeSAClient:
    """DriveApiClient 桩（服主收口 + 暂存扫荡共用）。"""
    removed = []
    deleted = []
    fail_delete = set()

    def __init__(self):
        pass

    def remove_anyone_reader(self, drive_id, fid):
        _FakeSAClient.removed.append((drive_id, fid))
        return "revoked"

    def delete_file_ignore_missing(self, drive_id, fid):
        if fid in _FakeSAClient.fail_delete:
            raise Exception("删暂存失败")
        _FakeSAClient.deleted.append((drive_id, fid))


def _reset_fakes():
    _FakeUserClient.behavior = {}
    _FakeUserClient.seen = []
    _FakeSAClient.removed = []
    _FakeSAClient.deleted = []
    _FakeSAClient.fail_delete = set()


# ---------------------------------------------------------------------------
# 1. OAuth 回调浏览器会话绑定
# ---------------------------------------------------------------------------

def _start_state(db, user_id=7, label="盘"):
    started = do.start_drive_oauth(db, user_id, label)
    state = unquote(
        started["authorization_url"].split("state=")[1].split("&")[0])
    return state, started["_oauth_nonce"]


def test_start_stores_nonce_hash(db):
    _state, nonce = _start_state(db)
    assert nonce
    row = db.query(em.DriveOAuthState).one()
    assert row.nonce_hash == do._hash_state(nonce)


def test_callback_binding_no_cookie_rejected_and_state_burned(db):
    state, _nonce = _start_state(db)
    with pytest.raises(do.DriveOAuthError) as exc:
        do.assert_callback_browser_binding(db, state, None, None)
    assert exc.value.kind == "auth_error"
    assert db.query(em.DriveOAuthState).count() == 0


def test_callback_binding_wrong_cookie_rejected(db):
    state, _nonce = _start_state(db)
    with pytest.raises(do.DriveOAuthError) as exc:
        do.assert_callback_browser_binding(db, state, "伪造的nonce", None)
    assert exc.value.kind == "auth_error"
    assert db.query(em.DriveOAuthState).count() == 0


def test_callback_binding_session_user_mismatch_rejected(db):
    state, nonce = _start_state(db, user_id=7)
    with pytest.raises(do.DriveOAuthError) as exc:
        do.assert_callback_browser_binding(db, state, nonce, 999)
    assert exc.value.kind == "auth_error"
    assert db.query(em.DriveOAuthState).count() == 0


def test_callback_binding_ok_keeps_state(db):
    state, nonce = _start_state(db, user_id=7)
    do.assert_callback_browser_binding(db, state, nonce, None)
    assert db.query(em.DriveOAuthState).count() == 1
    do.assert_callback_browser_binding(db, state, nonce, 7)
    assert db.query(em.DriveOAuthState).count() == 1


def test_callback_binding_unknown_state_passes_through(db):
    # state 不存在：不抛，交给 complete 按 not_found 原口径处理。
    do.assert_callback_browser_binding(db, "不存在的state", None, None)


def test_callback_binding_legacy_row_without_nonce_fail_closed(db):
    row = em.DriveOAuthState(
        state_hash=do._hash_state("legacy-state"), user_id=7,
        account_label="盘", sealed_verifier=seal_secret("verifier"),
        created_at=datetime.now(),
        expires_at=datetime.now() + timedelta(minutes=10))
    db.add(row)
    db.commit()
    with pytest.raises(do.DriveOAuthError) as exc:
        do.assert_callback_browser_binding(db, "legacy-state", "任意", None)
    assert exc.value.kind == "auth_error"
    assert db.query(em.DriveOAuthState).count() == 0


def test_nonce_cookie_domain_derivation():
    # _oauth_env 下回调 host = aetrix.example。
    assert do.oauth_nonce_cookie_domain("aetrix.example") is None
    assert do.oauth_nonce_cookie_domain("cdn.aetrix.example") == "aetrix.example"
    assert do.oauth_nonce_cookie_domain("other.org") is None
    assert do.oauth_nonce_cookie_domain(None) is None


# ---------------------------------------------------------------------------
# 2. 解绑调 Google revoke
# ---------------------------------------------------------------------------

def test_delete_binding_revokes_google_token(db):
    binding, cred = _seed_drive_binding(db, 7)
    calls = []

    def _post(url, data=None, timeout=30):
        calls.append((url, dict(data or {})))
        return _FakeResp(200, {})

    ub.delete_binding(db, 7, binding.id, http_post=_post)
    assert len(calls) == 1
    assert calls[0][0] == "https://oauth2.googleapis.com/revoke"
    assert calls[0][1].get("token") == "RT-OLD"
    assert db.query(em.UserStorageBinding).count() == 0
    assert db.query(em.MountCredential).count() == 0


def test_delete_binding_revoke_failure_does_not_block(db):
    binding, _cred = _seed_drive_binding(db, 7)

    def _post(url, data=None, timeout=30):
        raise RuntimeError("谷歌吊销端点不可达")

    ub.delete_binding(db, 7, binding.id, http_post=_post)
    assert db.query(em.UserStorageBinding).count() == 0
    assert db.query(em.MountCredential).count() == 0


def test_delete_binding_pan115_skips_revoke(db):
    cred = em.MountCredential(scope="user", owner_user_id=7,
                              sealed_credential="x", key_id="k",
                              status="active")
    db.add(cred)
    db.commit()
    binding = em.UserStorageBinding(
        user_id=7, provider="pan115", account_label="115盘",
        credential_id=cred.id, status="active")
    db.add(binding)
    db.commit()
    calls = []

    def _post(url, data=None, timeout=30):
        calls.append(url)
        return _FakeResp(200, {})

    ub.delete_binding(db, 7, binding.id, http_post=_post)
    assert calls == []
    assert db.query(em.UserStorageBinding).count() == 0
    assert db.query(em.MountCredential).count() == 0


# ---------------------------------------------------------------------------
# 3. Path B 暂存登记 + 扫荡
# ---------------------------------------------------------------------------

def _item(db):
    row = em.MediaItem(guid="secfix-1", library_id=1, item_type="movie",
                       name="测试片", drive_file_id="origfid",
                       file_path="mount://3/电影/a.mkv", size=1000)
    db.add(row)
    db.commit()
    return row


def _plain_binding(db, user_id=7):
    cred = em.MountCredential(scope="user", owner_user_id=user_id,
                              status="active")
    db.add(cred)
    db.commit()
    row = em.UserStorageBinding(user_id=user_id, provider="drive",
                                account_label="test",
                                credential_id=cred.id, status="active")
    db.add(row)
    db.commit()
    return row


def _user_job(db, item_id, user_id=7):
    row = em.TransferJob(item_id=item_id, trigger="playback",
                         drive_id="drive1", provider="drive",
                         owner_scope="user", owner_ref=str(user_id),
                         dest_drive_id=f"userdrive:{user_id}",
                         status="pending")
    db.add(row)
    db.commit()
    return row


class _FakeUserDrivePathB:
    """用户盘桩：源文件 copy 403 → 走 Path B；暂存 copy 成功。"""

    def ensure_folder(self, drive_id, dir_name):
        return "ufolder"

    def list_file_ids_by_name(self, drive_id, folder_id, name):
        return ["usercopy"]

    def copy_file(self, drive_id, source_fid, folder_id, name):
        if source_fid == "origfid":
            raise tv._CopyForbidden("copy 被拒：403")
        return {"id": "usercopy", "size": 1000}

    def add_anyone_reader(self, drive_id, fid):
        pass


class _StubSAStaging:
    """SA 桩：暂存 copy 成功，delete_file 按开关抛错。"""

    def __init__(self, delete_fails=False):
        self.delete_fails = delete_fails
        self.deleted = []

    def ensure_folder(self, drive_id, dir_name):
        return "tfolder"

    def find_file_by_name(self, drive_id, folder_id, name):
        return None

    def copy_file(self, drive_id, source_fid, folder_id, name):
        return {"id": "tmpfid", "size": 1000}

    def add_anyone_reader(self, drive_id, fid):
        pass

    def delete_file(self, drive_id, fid):
        if self.delete_fails:
            raise Exception("暂存删除失败")
        self.deleted.append(fid)


def test_path_b_staging_delete_failure_registers_orphan(db, monkeypatch):
    item = _item(db)
    _plain_binding(db, 7)
    job = _user_job(db, item.id)
    fake = _FakeUserDrivePathB()
    monkeypatch.setattr(tv, "UserDriveApiClient",
                        lambda db_arg, binding: fake)
    sa = _StubSAStaging(delete_fails=True)
    result = tv.run_transfer_job_once(db, job, sa)
    assert result["outcome"] == "done"
    orphans = db.query(em.TransferVariant).filter(
        em.TransferVariant.status == "orphan").all()
    assert len(orphans) == 1
    assert orphans[0].variant_file_id == "tmpfid"
    assert orphans[0].variant_drive_id == "drive1"
    assert orphans[0].owner_scope == "user" and orphans[0].owner_ref == "7"


def test_sweep_staging_leftovers_deletes_and_clears(db, monkeypatch):
    _reset_fakes()
    now = datetime(2026, 10, 30)
    src = _user_variant(db, 61, "tmpA", 7, status="orphan",
                        drive="drive1", created=now)
    user_orphan = _user_variant(db, 62, "tmpB", 7, status="orphan",
                                drive="userdrive:7", created=now)
    pan_orphan = _user_variant(db, 63, "tmpC", 7, status="orphan",
                               drive="pan115:user-7", created=now)
    fail = _user_variant(db, 64, "tmpD", 7, status="orphan",
                         drive="drive1", created=now)
    _FakeSAClient.fail_delete = {"tmpD"}
    monkeypatch.setattr(tv, "DriveApiClient", _FakeSAClient)
    stats = tv.sweep_staging_leftovers(db)
    db.commit()
    assert stats["checked"] == 2 and stats["deleted"] == 1
    assert stats["errors"] == 1
    assert _FakeSAClient.deleted == [("drive1", "tmpA")]
    remaining = {r.variant_file_id for r in
                 db.query(em.TransferVariant).all()}
    assert remaining == {"tmpB", "tmpC", "tmpD"}


# ---------------------------------------------------------------------------
# 4. 服主副本闲置 TTL
# ---------------------------------------------------------------------------

def _setup_ttl(monkeypatch, revoke_hours=2, delete_hours=2):
    monkeypatch.setattr(tv, "copy_idle_revoke_hours", lambda: revoke_hours)
    monkeypatch.setattr(tv, "copy_idle_delete_hours", lambda: delete_hours)


def test_source_copy_idle_cleanup_revokes_then_deletes(db, monkeypatch):
    _reset_fakes()
    monkeypatch.setattr(tv, "DriveApiClient", _FakeSAClient)
    now = datetime(2026, 10, 30)
    doomed = _source_variant(db, 71, "srcdelete",
                             created=now - timedelta(hours=100))
    revoke_only = _source_variant(db, 72, "srcrevoke",
                                  created=now - timedelta(hours=3))
    fresh = _source_variant(db, 73, "srcfresh",
                            created=now - timedelta(minutes=30))
    umd_recent = _source_variant(db, 74, "srcumd",
                                 created=now - timedelta(hours=100))
    db.add(em.UserMediaData(user_id=42, item_id=74,
                            last_played_at=now - timedelta(minutes=10)))
    db.commit()
    _setup_ttl(monkeypatch, revoke_hours=2, delete_hours=4)
    stats = tv.copy_idle_cleanup(db, now=now)
    assert stats["source_checked"] == 4
    assert stats["source_deleted"] == 1 and stats["source_revoked"] == 1
    assert stats["errors"] == 0
    assert _FakeSAClient.deleted == [("drive1", "srcdelete")]
    assert _FakeSAClient.removed == [("drive1", "srcrevoke")]
    assert db.query(em.TransferVariant).filter(
        em.TransferVariant.id == doomed.id).first() is None
    db.refresh(revoke_only)
    assert revoke_only.share_revoked_at is not None
    for row in (fresh, umd_recent):
        db.refresh(row)
        assert row.share_revoked_at is None
    assert db.query(em.TransferVariant).filter(
        em.TransferVariant.id == fresh.id).first() is not None
    assert db.query(em.TransferVariant).filter(
        em.TransferVariant.id == umd_recent.id).first() is not None


def test_source_copy_idle_cleanup_switch_off(db, monkeypatch):
    _reset_fakes()
    monkeypatch.setattr(tv, "DriveApiClient", _FakeSAClient)
    db.add(base_models.SystemConfig(
        key=tv.KEY_SOURCE_COPY_IDLE_CLEANUP, value="false"))
    db.commit()
    store.invalidate()
    now = datetime(2026, 10, 30)
    row = _source_variant(db, 81, "srcoff",
                          created=now - timedelta(hours=100))
    _setup_ttl(monkeypatch)
    stats = tv.copy_idle_cleanup(db, now=now)
    assert stats["source_checked"] == 0
    assert stats["source_deleted"] == 0 and stats["source_revoked"] == 0
    assert _FakeSAClient.deleted == [] and _FakeSAClient.removed == []
    assert db.query(em.TransferVariant).filter(
        em.TransferVariant.id == row.id).first() is not None


# ---------------------------------------------------------------------------
# 5. 死令牌待收回登记 + 重绑补收 + 清理兜底
# ---------------------------------------------------------------------------

def test_dead_token_marks_pending_then_rebind_executes(db, monkeypatch):
    _reset_fakes()
    monkeypatch.setattr(tv, "UserDriveApiClient", _FakeUserClient)
    binding, _cred = _seed_drive_binding(
        db, 7, expires_at=int(time.time()) - 60)
    row = _user_variant(db, 91, "pendfid", 7)
    do._apply_refresh_failure(db, binding, "expired")
    db.commit()
    db.refresh(row)
    assert row.share_revoked_at is None
    assert row.share_revoke_pending_at is not None
    # 重绑：新令牌可用后，complete 尾巴优先补收这批登记。
    started = do.start_drive_oauth(db, 7, "盘",
                                   rebind_binding_id=binding.id)
    state = unquote(
        started["authorization_url"].split("state=")[1].split("&")[0])
    rebound = do.complete_drive_oauth(
        db, state, "CODE-123", http_post=_token_post_factory(),
        http_get=_about_get_factory())
    assert rebound.status == "active"
    db.refresh(row)
    assert row.share_revoked_at is not None
    assert row.share_revoke_pending_at is None


def test_pending_revokes_run_in_idle_cleanup(db, monkeypatch):
    _reset_fakes()
    monkeypatch.setattr(tv, "UserDriveApiClient", _FakeUserClient)
    _seed_drive_binding(db, 7)
    now = datetime.now()
    row = _user_variant(db, 95, "pendfid2", 7, pending=now,
                        created=now)
    stats = tv.copy_idle_cleanup(db, now=now)
    assert stats["pending_revoked"] == 1
    db.refresh(row)
    assert row.share_revoked_at is not None
    assert row.share_revoke_pending_at is None


# ---------------------------------------------------------------------------
# 1b. portal 端点接线：start 下发 cookie、callback 读 cookie 把关
# ---------------------------------------------------------------------------

from types import SimpleNamespace

from fastapi import HTTPException, Response
from starlette.requests import Request as StarletteRequest

from backend.emby_server import portal


def _http_request(cookie=None, host="aetrix.example"):
    headers = [(b"host", host.encode())]
    if cookie is not None:
        headers.append((b"cookie", cookie.encode()))
    scope = {"type": "http", "method": "GET", "path": "/",
             "headers": headers, "query_string": b"",
             "scheme": "https", "server": (host, 443),
             "client": ("127.0.0.1", 40000)}
    return StarletteRequest(scope)


def test_portal_start_sets_nonce_cookie_and_hides_nonce(db):
    response = Response()
    result = portal.start_drive_oauth_binding(
        portal.DriveOAuthStart(account_label="盘"),
        _http_request(), response, SimpleNamespace(id=7), db)
    assert "_oauth_nonce" not in result
    set_cookie = response.headers.get("set-cookie", "")
    assert do.OAUTH_NONCE_COOKIE + "=" in set_cookie
    assert "HttpOnly" in set_cookie
    assert "Path=" + do.OAUTH_CALLBACK_PATH in set_cookie


def test_portal_callback_without_cookie_403(db, monkeypatch):
    state, _nonce = _start_state(db)
    called = []
    monkeypatch.setattr(do, "complete_drive_oauth",
                        lambda db_, s, c: called.append(s))
    with pytest.raises(HTTPException) as exc:
        portal.callback_drive_oauth_binding(
            _http_request(), code="CODE", state=state, error="", db=db)
    assert exc.value.status_code == 403
    assert called == []


def test_portal_callback_with_cookie_passes(db, monkeypatch):
    state, nonce = _start_state(db)
    binding, _cred = _seed_drive_binding(db, 7, label="已绑")
    monkeypatch.setattr(do, "complete_drive_oauth",
                        lambda db_, s, c: binding)
    result = portal.callback_drive_oauth_binding(
        _http_request(cookie=f"{do.OAUTH_NONCE_COOKIE}={nonce}"),
        code="CODE", state=state, error="", db=db)
    assert result["provider"] == "drive" and result["status"] == "active"
