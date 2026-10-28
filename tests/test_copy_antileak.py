# -*- coding: utf-8 -*-
"""转存副本防外流三件套（2026-10-28）：解绑收权限 + 闲置 TTL + 发放可查。

钉死：
- 收回作用域：只动本人 userdrive: 行（owner_scope=user 且
  variant_drive_id 以 userdrive: 开头）；共享源文件、115 行、
  别人名下的行一律不动；只收权限，文件与行本身不动、行状态与
  file_id 不变（锁死用例 test_transfer_cleanup.py 的解绑断言仍过）。
- invalid 钩子只用已存未过期令牌、绝不刷新（否则递归）；无可用
  令牌时整批跳过、不阻塞、不抛异常；throttled 不落行也不收回。
- TTL 口径：最后活动 = max（变体行 last_played_at、
  UserMediaData.last_played_at、变体行 created_at）；先删除 pass
  再收回 pass；两 pass 每轮各有界；无 active 绑定记 skipped、
  不记 error；永不抛异常。
- 发放日志：只记免凭证 usercontent 发放；独立短会话写日志 +
  刷新变体行 last_played_at；行已删仍记日志；写失败返回 False。

全部离线：Drive 调用一律桩（monkeypatch 客户端类/_request），
不碰真实谷歌；令牌口一律桩，不解封真实凭据。
"""
import os

os.environ.setdefault("DATABASE_TYPE", "sqlite")
os.environ.setdefault("REDIS_ENABLED", "false")

from datetime import datetime, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend import models as base_models
from backend.emby_server import models as em
from backend.emby_server import transfer_variant as tv
from backend.emby_server import user_bindings
from backend.emby_server import drive_oauth
from backend.api import playback_admin


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


_counter = [0]


def _user(db):
    _counter[0] += 1
    row = base_models.WebUser(username=f"anti-{_counter[0]}",
                              password_hash="x")
    db.add(row)
    db.commit()
    return row


def _binding(db, user, status="active"):
    _counter[0] += 1
    cred = em.MountCredential(scope="user", owner_user_id=user.id,
                              sealed_credential="x", key_id="k")
    db.add(cred)
    db.commit()
    row = em.UserStorageBinding(user_id=user.id, provider="drive",
                                account_label=f"盘{user.id}-{_counter[0]}",
                                credential_id=cred.id, status=status)
    db.add(row)
    db.commit()
    return row


def _variant(db, item_id, fid, ref, status="ready", drive=None,
             last_played=None, created=None):
    row = em.TransferVariant(
        item_id=item_id, source_file_id="origfid", variant_file_id=fid,
        variant_drive_id=drive if drive is not None else f"userdrive:{ref}",
        owner_scope="user", owner_ref=str(ref), status=status,
        size_bytes=1000,
        last_played_at=last_played,
        created_at=created or datetime(2026, 10, 27))
    db.add(row)
    db.commit()
    return row


class _Resp:
    def __init__(self, status_code, payload=None):
        self.status_code = status_code
        self._payload = payload or {}

    def json(self):
        return self._payload


def _drive_client(monkeypatch, handler):
    client = tv.DriveApiClient()
    monkeypatch.setattr(
        client, "_request",
        lambda method, url, drive_id, **kwargs: handler(method, url))
    return client


# ---------------------------------------------------------------------------
# DriveApiClient：list / remove / delete
# ---------------------------------------------------------------------------

def test_list_permissions_ok_and_404(monkeypatch):
    perms = {"permissions": [{"id": "p1", "type": "anyone"}]}
    client = _drive_client(
        monkeypatch,
        lambda m, u: _Resp(200, perms) if "permissions" in u else _Resp(404))
    assert client.list_permissions("myDrive:1", "fid1") == perms["permissions"]
    client404 = _drive_client(monkeypatch, lambda m, u: _Resp(404))
    with pytest.raises(tv._NotFound):
        client404.list_permissions("myDrive:1", "gone")


def test_list_permissions_other_error_raises(monkeypatch):
    client = _drive_client(monkeypatch, lambda m, u: _Resp(500))
    with pytest.raises(Exception, match="列文件权限失败"):
        client.list_permissions("myDrive:1", "fid1")


def test_remove_anyone_reader_revokes_only_anyone(monkeypatch):
    calls = []

    def handler(method, url):
        calls.append((method, url))
        if method == "GET":
            return _Resp(200, {"permissions": [
                {"id": "p-any", "type": "anyone"},
                {"id": "p-user", "type": "user"}]})
        if method == "DELETE":
            assert url.endswith("/permissions/p-any")
            return _Resp(204)
        raise AssertionError(method)

    client = _drive_client(monkeypatch, handler)
    assert client.remove_anyone_reader("myDrive:1", "fid1") == "revoked"
    assert [c[0] for c in calls] == ["GET", "DELETE"]


def test_remove_anyone_reader_already_clean_and_gone(monkeypatch):
    clean = _drive_client(
        monkeypatch,
        lambda m, u: _Resp(200, {"permissions": [
            {"id": "p-user", "type": "user"}]}))
    assert clean.remove_anyone_reader("myDrive:1", "fid1") == "already_clean"
    gone = _drive_client(monkeypatch, lambda m, u: _Resp(404))
    assert gone.remove_anyone_reader("myDrive:1", "fid1") == "gone"


def test_remove_anyone_reader_delete_failure_raises(monkeypatch):
    def handler(method, url):
        if method == "GET":
            return _Resp(200, {"permissions": [{"id": "p1",
                                                "type": "anyone"}]})
        return _Resp(403)

    client = _drive_client(monkeypatch, handler)
    with pytest.raises(Exception, match="删 anyone 权限失败"):
        client.remove_anyone_reader("myDrive:1", "fid1")


def test_delete_file_statuses(monkeypatch):
    ok = _drive_client(monkeypatch, lambda m, u: _Resp(204))
    ok.delete_file_ignore_missing("myDrive:1", "fid1")
    gone = _drive_client(monkeypatch, lambda m, u: _Resp(404))
    gone.delete_file_ignore_missing("myDrive:1", "fid1")
    bad = _drive_client(monkeypatch, lambda m, u: _Resp(403))
    with pytest.raises(Exception, match="删文件失败"):
        bad.delete_file_ignore_missing("myDrive:1", "fid1")


# ---------------------------------------------------------------------------
# revoke_user_copy_sharing：作用域 / 结果统计 / 令牌路径
# ---------------------------------------------------------------------------

class _FakeClient:
    """桩客户端：按 fid 表运行为；构造参数记下来供断言。"""
    seen = []

    def __init__(self, db, binding):
        self.binding = binding
        _FakeClient.seen.append(binding)

    def remove_anyone_reader(self, drive_id, fid):
        behavior = _FakeClient.behavior.get(fid, "revoked")
        if behavior == "raise":
            raise Exception("网络抖动")
        return behavior

    def delete_file_ignore_missing(self, drive_id, fid):
        if _FakeClient.behavior.get(fid) == "raise":
            raise Exception("删文件失败")
        return None


def _fake_clients(monkeypatch, behavior):
    _FakeClient.seen = []
    _FakeClient.behavior = behavior
    monkeypatch.setattr(tv, "UserDriveApiClient", _FakeClient)


def test_revoke_scope_only_own_userdrive_rows(db, monkeypatch):
    me = _user(db)
    other = _user(db)
    _fake_clients(monkeypatch, {})
    own = _variant(db, 11, "ownfid", me.id)
    own_stale = _variant(db, 12, "stalefid", me.id, status="stale")
    _variant(db, 13, "otherfid", other.id)
    _variant(db, 14, "srcfid", me.id, drive="mount:3")
    _variant(db, 15, "panfid", me.id, drive="pan115:user-1")
    _binding(db, me)
    stats = tv.revoke_user_copy_sharing(db, me.id)
    db.commit()
    assert stats == {"total": 2, "revoked": 2, "already_clean": 0,
                     "gone": 0, "errors": 0}
    db.refresh(own)
    db.refresh(own_stale)
    assert own.share_revoked_at is not None
    assert own_stale.share_revoked_at is not None
    assert own.status == "ready" and own.variant_file_id == "ownfid"


def test_revoke_outcomes_and_errors(db, monkeypatch):
    me = _user(db)
    _fake_clients(monkeypatch, {"a": "revoked", "b": "already_clean",
                                "c": "gone", "d": "raise"})
    ra = _variant(db, 21, "a", me.id)
    rb = _variant(db, 22, "b", me.id)
    rc = _variant(db, 23, "c", me.id)
    rd = _variant(db, 24, "d", me.id)
    _binding(db, me)
    stats = tv.revoke_user_copy_sharing(db, me.id)
    db.commit()
    assert stats["revoked"] == 1 and stats["already_clean"] == 1
    assert stats["gone"] == 1 and stats["errors"] == 1
    for row in (ra, rb, rc):
        db.refresh(row)
        assert row.share_revoked_at is not None
    db.refresh(rd)
    assert rd.share_revoked_at is None


def test_revoke_binding_given_uses_only_that_binding(db, monkeypatch):
    me = _user(db)
    b1 = _binding(db, me)
    _binding(db, me)
    _fake_clients(monkeypatch, {})
    _variant(db, 31, "onlyfid", me.id)
    tv.revoke_user_copy_sharing(db, me.id, binding=b1)
    assert _FakeClient.seen == [b1]


def test_revoke_multi_binding_tries_in_order(db, monkeypatch):
    me = _user(db)
    _binding(db, me)
    _binding(db, me)

    class _Flaky(_FakeClient):
        calls = []

        def remove_anyone_reader(self, drive_id, fid):
            _Flaky.calls.append(self.binding)
            if len(_Flaky.calls) == 1:
                raise Exception("第一枚令牌 401")
            return "revoked"

    _Flaky.calls = []
    monkeypatch.setattr(tv, "UserDriveApiClient", _Flaky)
    _variant(db, 41, "multifid", me.id)
    stats = tv.revoke_user_copy_sharing(db, me.id)
    assert stats["revoked"] == 1 and stats["errors"] == 0
    assert len(_Flaky.calls) == 2


def test_revoke_no_refresh_path_without_token_skips(db, monkeypatch):
    me = _user(db)
    b = _binding(db, me)
    _variant(db, 51, "notokfid", me.id)
    monkeypatch.setattr(tv, "_stored_drive_token", lambda db_, b_: None)
    called = []
    monkeypatch.setattr(tv, "_RawTokenClient",
                        lambda tok: called.append(tok) or _FakeClient(None, None))
    stats = tv.revoke_user_copy_sharing(db, me.id, binding=b,
                                        allow_refresh=False)
    assert stats["total"] == 1 and stats["errors"] == 1
    assert called == []


def test_revoke_no_refresh_path_with_token_uses_raw_client(db, monkeypatch):
    me = _user(db)
    b = _binding(db, me)
    _variant(db, 61, "rawfid", me.id)
    monkeypatch.setattr(tv, "_stored_drive_token",
                        lambda db_, b_: "tok123")

    class _Raw:
        seen_token = None

        def __init__(self, token):
            _Raw.seen_token = token

        def remove_anyone_reader(self, drive_id, fid):
            return "revoked"

    monkeypatch.setattr(tv, "_RawTokenClient", _Raw)
    stats = tv.revoke_user_copy_sharing(db, me.id, binding=b,
                                        allow_refresh=False)
    assert stats["revoked"] == 1 and _Raw.seen_token == "tok123"


# ---------------------------------------------------------------------------
# copy_idle_cleanup：口径 / 顺序 / 有界 / 无绑定
# ---------------------------------------------------------------------------

def _setup_ttl(db, monkeypatch, revoke_hours=2, delete_hours=2):
    monkeypatch.setattr(tv, "copy_idle_revoke_hours", lambda: revoke_hours)
    monkeypatch.setattr(tv, "copy_idle_delete_hours", lambda: delete_hours)


def test_ttl_last_activity_max_and_delete_first(db, monkeypatch):
    me = _user(db)
    _fake_clients(monkeypatch, {})
    _binding(db, me)
    now = datetime(2026, 10, 28)
    old = now - timedelta(hours=100)
    # 删：100 小时未动（created 兜底）→ 删文件 + 行。
    doomed = _variant(db, 71, "doomed", me.id, created=old)
    # 只收：3 小时未动（≥收 2 小时、＜删 4 小时）→ 收权限不删行。
    idle = _variant(db, 72, "idle", me.id, last_played=now - timedelta(hours=3),
                    created=old)
    # 新：1 小时 → 不动。
    fresh = _variant(db, 73, "fresh", me.id, created=now - timedelta(hours=1))
    # UMD 更近：变体 100 小时未动、但该用户该条目 30 分钟前有进度 → 不动。
    umd_recent = _variant(db, 74, "umdrecent", me.id, created=old)
    db.add(em.UserMediaData(user_id=me.id, item_id=74,
                            last_played_at=now - timedelta(minutes=30)))
    # 取新者：变体 50 小时、UMD 1 小时 → 闲置 1 小时 → 不动。
    umd_max = _variant(db, 75, "umdmax", me.id,
                       last_played=now - timedelta(hours=50), created=old)
    db.add(em.UserMediaData(user_id=me.id, item_id=75,
                            last_played_at=now - timedelta(hours=1)))
    db.commit()
    _setup_ttl(db, monkeypatch, revoke_hours=2, delete_hours=4)
    stats = tv.copy_idle_cleanup(db, now=now)
    assert stats["deleted"] == 1 and stats["revoked"] == 1
    assert stats["errors"] == 0
    assert db.query(em.TransferVariant).filter(
        em.TransferVariant.id == doomed.id).first() is None
    db.refresh(idle)
    assert idle.share_revoked_at is not None and idle.status == "ready"
    for row in (fresh, umd_recent, umd_max):
        db.refresh(row)
        assert row.share_revoked_at is None


def test_ttl_no_binding_skipped_not_error(db, monkeypatch):
    me = _user(db)
    _fake_clients(monkeypatch, {})
    now = datetime(2026, 10, 28)
    row = _variant(db, 81, "nobind", me.id,
                   created=now - timedelta(hours=100))
    _setup_ttl(db, monkeypatch)
    stats = tv.copy_idle_cleanup(db, now=now)
    assert stats["skipped_no_binding"] == 2  # 删除 pass + 收回 pass 各记 1
    assert stats["errors"] == 0 and stats["deleted"] == 0
    assert db.query(em.TransferVariant).filter(
        em.TransferVariant.id == row.id).first() is not None


def test_ttl_delete_failure_keeps_row(db, monkeypatch):
    me = _user(db)
    _fake_clients(monkeypatch, {"failfid": "raise"})
    _binding(db, me)
    now = datetime(2026, 10, 28)
    row = _variant(db, 91, "failfid", me.id,
                   created=now - timedelta(hours=100))
    _setup_ttl(db, monkeypatch)
    stats = tv.copy_idle_cleanup(db, now=now)
    assert stats["deleted"] == 0 and stats["errors"] >= 1
    assert db.query(em.TransferVariant).filter(
        em.TransferVariant.id == row.id).first() is not None


def test_ttl_limit_bounds_batch(db, monkeypatch):
    me = _user(db)
    _fake_clients(monkeypatch, {})
    _binding(db, me)
    now = datetime(2026, 10, 28)
    for i in range(3):
        _variant(db, 100 + i, f"lim{i}", me.id,
                 created=now - timedelta(hours=100))
    _setup_ttl(db, monkeypatch)
    stats = tv.copy_idle_cleanup(db, now=now, limit=2)
    assert stats["checked"] == 2 and stats["deleted"] == 2
    assert db.query(em.TransferVariant).count() == 1


def test_ttl_threshold_defaults_and_invalid_env(monkeypatch):
    monkeypatch.delenv("COPY_IDLE_REVOKE_HOURS", raising=False)
    monkeypatch.delenv("COPY_IDLE_DELETE_HOURS", raising=False)
    assert tv.copy_idle_revoke_hours() == 2
    assert tv.copy_idle_delete_hours() == 2
    monkeypatch.setenv("COPY_IDLE_REVOKE_HOURS", "abc")
    monkeypatch.setenv("COPY_IDLE_DELETE_HOURS", "-5")
    assert tv.copy_idle_revoke_hours() == 2
    assert tv.copy_idle_delete_hours() == 2
    # 旧 DAYS 键不再产生歧义：即使残留在 .env 里也不影响小时阈值。
    monkeypatch.setenv("COPY_IDLE_REVOKE_DAYS", "30")
    monkeypatch.setenv("COPY_IDLE_DELETE_DAYS", "90")
    assert tv.copy_idle_revoke_hours() == 2
    assert tv.copy_idle_delete_hours() == 2


def test_ttl_hour_boundary_keeps_1h59m_deletes_2h(db, monkeypatch):
    me = _user(db)
    _fake_clients(monkeypatch, {})
    _binding(db, me)
    now = datetime(2026, 10, 28)
    old = now - timedelta(hours=100)
    # 1 小时 59 分 → 整小时取整为 1，不触发。
    kept = _variant(db, 121, "kept", me.id,
                    last_played=now - timedelta(hours=1, minutes=59),
                    created=old)
    # 2 小时整 → 触发删除。
    doomed = _variant(db, 122, "doomed2h", me.id,
                      last_played=now - timedelta(hours=2),
                      created=old)
    db.commit()
    _setup_ttl(db, monkeypatch)
    stats = tv.copy_idle_cleanup(db, now=now)
    assert stats["deleted"] == 1 and stats["errors"] == 0
    assert db.query(em.TransferVariant).filter(
        em.TransferVariant.id == doomed.id).first() is None
    db.refresh(kept)
    assert kept.share_revoked_at is None
    assert db.query(em.TransferVariant).filter(
        em.TransferVariant.id == kept.id).first() is not None


def test_ttl_old_stock_row_cleaned_under_new_threshold(db, monkeypatch):
    """存量老行按新阈值正确处理：30 天未动的副本首轮即删（受 limit 约束）。"""
    me = _user(db)
    _fake_clients(monkeypatch, {})
    _binding(db, me)
    now = datetime(2026, 10, 28)
    row = _variant(db, 123, "oldstock", me.id,
                   created=now - timedelta(days=30))
    db.commit()
    _setup_ttl(db, monkeypatch)
    stats = tv.copy_idle_cleanup(db, now=now)
    assert stats["deleted"] == 1
    assert db.query(em.TransferVariant).filter(
        em.TransferVariant.id == row.id).first() is None


def test_idle_cleanup_due_hourly_gate(monkeypatch):
    state = {}
    now_ts = 1000000.0
    assert tv._idle_cleanup_due(state, now_ts) is True  # 从未跑过，首轮即跑
    state["last_idle_cleanup"] = now_ts - 1800  # 30 分钟前跑过
    assert tv._idle_cleanup_due(state, now_ts) is False
    state["last_idle_cleanup"] = now_ts - 3600  # 整 1 小时
    assert tv._idle_cleanup_due(state, now_ts) is True
    monkeypatch.setattr(tv, "COPY_IDLE_CLEANUP_INTERVAL_SEC", 60)
    state["last_idle_cleanup"] = now_ts - 61
    assert tv._idle_cleanup_due(state, now_ts) is True
    state["last_idle_cleanup"] = now_ts - 59
    assert tv._idle_cleanup_due(state, now_ts) is False


# ---------------------------------------------------------------------------
# note_usercopy_issued：独立会话 / 行已删 / 写失败
# ---------------------------------------------------------------------------

def test_note_issued_writes_log_and_touch(db, monkeypatch):
    me = _user(db)
    row = _variant(db, 111, "notefid", me.id)
    import backend.database as _dbmod
    factory = sessionmaker(bind=db.get_bind())
    monkeypatch.setattr(_dbmod, "SessionLocal", factory)
    assert tv.note_usercopy_issued(None, me.id, 111, row.id, "notefid") is True
    log = db.query(em.DirectIssueLog).first()
    assert log is not None and log.user_id == me.id
    assert log.item_id == 111 and log.variant_file_id == "notefid"
    assert log.via == "usercontent"
    db.refresh(row)
    assert row.last_played_at is not None


def test_note_issued_missing_row_still_logs(db, monkeypatch):
    me = _user(db)
    import backend.database as _dbmod
    factory = sessionmaker(bind=db.get_bind())
    monkeypatch.setattr(_dbmod, "SessionLocal", factory)
    assert tv.note_usercopy_issued(None, me.id, 112, 999999, "ghost") is True
    assert db.query(em.DirectIssueLog).count() == 1
    assert tv.note_usercopy_issued(None, me.id, 113, None, "noid") is True
    assert db.query(em.DirectIssueLog).count() == 2


def test_note_issued_session_failure_returns_false(monkeypatch):
    import backend.database as _dbmod

    def _boom():
        raise RuntimeError("库挂了")

    monkeypatch.setattr(_dbmod, "SessionLocal", _boom)
    assert tv.note_usercopy_issued(None, 1, 1, 1, "fid") is False


# ---------------------------------------------------------------------------
# delete_binding 钩子 / invalid 钩子 / 管理端查询
# ---------------------------------------------------------------------------

def test_delete_binding_revokes_then_deletes(db, monkeypatch):
    me = _user(db)
    binding = _binding(db, me)
    row = _variant(db, 121, "keepfid", me.id)
    calls = []
    monkeypatch.setattr(
        tv, "revoke_user_copy_sharing",
        lambda db_, uid, binding=None, **kw: calls.append((uid, binding)))
    user_bindings.delete_binding(db, me.id, binding.id)
    assert calls == [(me.id, binding)]
    assert db.query(em.UserStorageBinding).filter(
        em.UserStorageBinding.id == binding.id).first() is None
    db.refresh(row)
    assert row.status == "ready" and row.variant_file_id == "keepfid"


def test_delete_binding_revoke_failure_does_not_block(db, monkeypatch):
    me = _user(db)
    binding = _binding(db, me)

    def _boom(db_, uid, binding=None, **kw):
        raise RuntimeError("Drive 挂了")

    monkeypatch.setattr(tv, "revoke_user_copy_sharing", _boom)
    user_bindings.delete_binding(db, me.id, binding.id)
    assert db.query(em.UserStorageBinding).filter(
        em.UserStorageBinding.id == binding.id).first() is None


def test_refresh_failure_hook_only_on_invalid(db, monkeypatch):
    me = _user(db)
    binding = _binding(db, me)
    calls = []
    monkeypatch.setattr(
        tv, "revoke_user_copy_sharing",
        lambda db_, uid, binding=None, **kw: calls.append(kw.get("allow_refresh")))
    drive_oauth._apply_refresh_failure(db, binding, "expired")
    assert calls == [False]
    calls.clear()
    drive_oauth._apply_refresh_failure(db, binding, "throttled")
    assert calls == []


def test_admin_issue_log_query_filters(db):
    base = datetime(2026, 10, 28, 10, 0, 0)
    for i, (uid, item) in enumerate([(1, 10), (1, 11), (2, 10)]):
        db.add(em.DirectIssueLog(user_id=uid, item_id=item, variant_id=i + 1,
                                 variant_file_id=f"f{i}",
                                 via="usercontent",
                                 created_at=base + timedelta(hours=i)))
    db.commit()
    rows = playback_admin.query_direct_issue_logs(db, user_id=1)
    assert [r["item_id"] for r in rows] == [11, 10]
    rows = playback_admin.query_direct_issue_logs(db, item_id=10)
    assert [r["user_id"] for r in rows] == [2, 1]
    rows = playback_admin.query_direct_issue_logs(
        db, since=base + timedelta(hours=1))
    assert len(rows) == 2
    rows = playback_admin.query_direct_issue_logs(
        db, until=base + timedelta(hours=1))
    assert len(rows) == 2
    rows = playback_admin.query_direct_issue_logs(db, limit=1)
    assert len(rows) == 1 and rows[0]["variant_file_id"] == "f2"
    rows = playback_admin.query_direct_issue_logs(db, limit=500)
    assert len(rows) == 3


def test_admin_issue_time_parse():
    assert playback_admin._parse_issue_time(None, "since") is None
    assert playback_admin._parse_issue_time(
        "2026-10-28T10:00:00Z", "since") == datetime(2026, 10, 28, 10, 0, 0)
    with pytest.raises(ValueError):
        playback_admin._parse_issue_time("不是时间", "since")
