# -*- coding: utf-8 -*-
"""起播去重（2026-10-29）。

生产实测每次起播多打两类谷歌请求：
- PlaybackInfo 每次 permissions.create（300-400ms），副本早已公开也打；
- Hills 起播连发 4 次 /stream，每次被判成重试强制 files.get（150-470ms）。

钉死：
- ensure_user_copy_public 确认公开后缓存；命中时不再调 ensure_shared。
- 缓存命中仍回库核对：副本行不在 ready、share_revoked_at 晚于缓存写入、
  行不存在、缓存读写异常 → 一律按未命中走原逻辑（不会把已收回的副本当公开）。
- revoke_user_copy_sharing 收回时主动删键。
- 无绑定 / 授权失败照旧 False，失败结果不缓存。TTL=0 关闭缓存。
- 同一文件强制复验 60 秒内最多一次（可调，0=不限），跨进程经 Redis NX；
  Redis 挂了按本进程；异常时放行复验（宁可多查）。
全部离线。
"""
import os

os.environ.setdefault("DATABASE_TYPE", "sqlite")
os.environ.setdefault("REDIS_ENABLED", "false")

from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend import models  # noqa: F401
from backend.database import Base
from backend.emby_server import api, drive_oauth, drive_share
from backend.emby_server import models as em
from backend.emby_server import transfer_variant as tv

FID = "1copyFIDaaaaaaaaaaaaaaaaaaaaaaaa"


@pytest.fixture()
def db():
    engine = create_engine("sqlite://",
                           connect_args={"check_same_thread": False},
                           poolclass=StaticPool)
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


class _CM:
    store = {}
    boom = False

    @classmethod
    def get(cls, key, *, domain=None):
        if cls.boom:
            raise RuntimeError("cache down")
        return cls.store.get((domain, key))

    @classmethod
    def set(cls, key, value, ttl=300, *, domain=None):
        if cls.boom:
            raise RuntimeError("cache down")
        cls.store[(domain, key)] = value
        return True

    @classmethod
    def delete(cls, key, *, domain=None):
        cls.store.pop((domain, key), None)
        return True


@pytest.fixture(autouse=True)
def _iso(monkeypatch):
    _CM.store = {}
    _CM.boom = False
    monkeypatch.setattr("backend.database.CacheManager", _CM)
    monkeypatch.delenv("COPY_PUBLIC_CACHE_TTL_SEC", raising=False)
    monkeypatch.setattr(tv, "_active_binding",
                        lambda db_, uid, p: SimpleNamespace(id=15))
    monkeypatch.setattr(drive_oauth, "get_binding_drive_access_token",
                        lambda db_, b: "tok-user")
    api._drive_exists_forced.clear()
    monkeypatch.setattr(api, "_drive_exists_redis", lambda: None)
    yield
    api._drive_exists_forced.clear()


def _row(db, status="ready", revoked=None, uid=7, fid=FID):
    row = em.TransferVariant(item_id=1, source_file_id="src",
                             variant_file_id=fid,
                             variant_drive_id=f"userdrive:{uid}",
                             owner_scope="user", owner_ref=str(uid),
                             status=status, share_revoked_at=revoked)
    db.add(row)
    db.commit()
    return row


def _count_share(monkeypatch, result="ok"):
    calls = {"n": 0}

    def share(fid, tok):
        calls["n"] += 1
        return result

    monkeypatch.setattr(drive_share, "ensure_shared", share)
    return calls


# ---------------------------------------------------------------------------
# A. permissions.create 去重
# ---------------------------------------------------------------------------

def test_second_call_hits_cache(db, monkeypatch):
    _row(db)
    calls = _count_share(monkeypatch)
    assert tv.ensure_user_copy_public(db, 7, FID) is True
    assert tv.ensure_user_copy_public(db, 7, FID) is True
    assert tv.ensure_user_copy_public(db, 7, FID) is True
    assert calls["n"] == 1


def test_already_also_cached(db, monkeypatch):
    _row(db)
    calls = _count_share(monkeypatch, "already")
    assert tv.ensure_user_copy_public(db, 7, FID) is True
    assert tv.ensure_user_copy_public(db, 7, FID) is True
    assert calls["n"] == 1


def test_failure_not_cached(db, monkeypatch):
    _row(db)
    calls = _count_share(monkeypatch, "failed")
    assert tv.ensure_user_copy_public(db, 7, FID) is False
    assert tv.ensure_user_copy_public(db, 7, FID) is False
    assert calls["n"] == 2


def test_revoked_after_cache_invalidates(db, monkeypatch):
    row = _row(db)
    calls = _count_share(monkeypatch)
    assert tv.ensure_user_copy_public(db, 7, FID) is True
    # 收回写 share_revoked_at=now（晚于缓存）——即使没删键也必须失效。
    row.share_revoked_at = datetime.now() + timedelta(seconds=1)
    db.commit()
    assert tv.ensure_user_copy_public(db, 7, FID) is True
    assert calls["n"] == 2


def test_revoked_before_cache_still_hits(db, monkeypatch):
    # 早先收回过、后来重新授权（缓存写在收回之后）：命中有效。
    _row(db, revoked=datetime.now() - timedelta(hours=3))
    calls = _count_share(monkeypatch)
    assert tv.ensure_user_copy_public(db, 7, FID) is True
    assert tv.ensure_user_copy_public(db, 7, FID) is True
    assert calls["n"] == 1


@pytest.mark.parametrize("status", ["deleted", "stale", "released", "orphan"])
def test_non_ready_row_invalidates(db, monkeypatch, status):
    row = _row(db)
    calls = _count_share(monkeypatch)
    assert tv.ensure_user_copy_public(db, 7, FID) is True
    row.status = status
    db.commit()
    tv.ensure_user_copy_public(db, 7, FID)
    assert calls["n"] == 2


def test_missing_row_invalidates(db, monkeypatch):
    calls = _count_share(monkeypatch)
    assert tv.ensure_user_copy_public(db, 7, FID) is True  # 无行：照旧授权
    assert tv.ensure_user_copy_public(db, 7, FID) is True
    assert calls["n"] == 2


def test_other_users_row_does_not_validate(db, monkeypatch):
    _row(db, uid=8)  # 别人的同 fid 行不能为本人兜底
    calls = _count_share(monkeypatch)
    tv.ensure_user_copy_public(db, 7, FID)
    tv.ensure_user_copy_public(db, 7, FID)
    assert calls["n"] == 2


def test_cache_errors_fall_back(db, monkeypatch):
    _row(db)
    calls = _count_share(monkeypatch)
    _CM.boom = True
    assert tv.ensure_user_copy_public(db, 7, FID) is True
    assert tv.ensure_user_copy_public(db, 7, FID) is True
    assert calls["n"] == 2


def test_ttl_zero_disables(db, monkeypatch):
    monkeypatch.setenv("COPY_PUBLIC_CACHE_TTL_SEC", "0")
    _row(db)
    calls = _count_share(monkeypatch)
    tv.ensure_user_copy_public(db, 7, FID)
    tv.ensure_user_copy_public(db, 7, FID)
    assert calls["n"] == 2


def test_no_binding_never_uses_cache(db, monkeypatch):
    _row(db)
    calls = _count_share(monkeypatch)
    assert tv.ensure_user_copy_public(db, 7, FID) is True
    monkeypatch.setattr(tv, "_active_binding", lambda db_, uid, p: None)
    assert tv.ensure_user_copy_public(db, 7, FID) is False
    assert calls["n"] == 1


def test_revoke_drops_cache_key(db, monkeypatch):
    _row(db)
    _count_share(monkeypatch)
    assert tv.ensure_user_copy_public(db, 7, FID) is True
    assert any("usercopy_public:7:" in k for (_d, k) in _CM.store)

    class _Client:
        def remove_anyone_reader(self, marker, fid):
            return "revoked"

    monkeypatch.setattr(tv, "UserDriveApiClient", lambda db_, b: _Client())
    stats = tv.revoke_user_copy_sharing(db, 7,
                                        binding=SimpleNamespace(id=15))
    assert stats["revoked"] == 1
    assert not any("usercopy_public:7:" in k for (_d, k) in _CM.store)


# ---------------------------------------------------------------------------
# B. 强制复验按文件限频
# ---------------------------------------------------------------------------

class _FakeRedis:
    def __init__(self):
        self.store = {}

    def set(self, key, val, ex=None, nx=False):
        if nx and key in self.store:
            return None
        self.store[key] = val
        return True


def test_force_recheck_once_per_interval(monkeypatch):
    monkeypatch.delenv("DRIVE_EXISTS_FORCE_MIN_INTERVAL_SEC", raising=False)
    assert api._force_recheck_allowed("f1") is True
    assert api._force_recheck_allowed("f1") is False
    assert api._force_recheck_allowed("f1") is False
    assert api._force_recheck_allowed("f2") is True  # 按文件分桶


def test_force_recheck_interval_expires(monkeypatch):
    monkeypatch.setenv("DRIVE_EXISTS_FORCE_MIN_INTERVAL_SEC", "60")
    assert api._force_recheck_allowed("f1") is True
    api._drive_exists_forced["f1"] -= 61
    assert api._force_recheck_allowed("f1") is True


def test_force_recheck_zero_disables_limit(monkeypatch):
    monkeypatch.setenv("DRIVE_EXISTS_FORCE_MIN_INTERVAL_SEC", "0")
    for _ in range(4):
        assert api._force_recheck_allowed("f1") is True


def test_force_recheck_cross_process(monkeypatch):
    fake = _FakeRedis()
    monkeypatch.setattr(api, "_drive_exists_redis", lambda: fake)
    assert api._force_recheck_allowed("fx") is True
    api._drive_exists_forced.clear()  # 另一个 worker 没有本进程记忆
    assert api._force_recheck_allowed("fx") is False


def test_force_recheck_error_allows(monkeypatch):
    def boom():
        raise RuntimeError("redis down")

    monkeypatch.setattr(api, "_drive_exists_redis", boom)
    assert api._force_recheck_allowed("fy") is True


def test_rapid_retry_burst_only_one_forced(monkeypatch):
    # Hills 起播连发：同设备同 bytes=0- 多次，真重试只放行一次强制复验。
    monkeypatch.delenv("DRIVE_EXISTS_FORCE_MIN_INTERVAL_SEC", raising=False)
    from starlette.requests import Request

    def req():
        auth = 'MediaBrowser Client="Hills", DeviceId="d1", Version="1"'
        return Request({"type": "http", "method": "GET",
                        "path": "/Videos/x/stream",
                        "raw_path": b"/Videos/x/stream", "query_string": b"",
                        "headers": [(b"range", b"bytes=0-"),
                                    (b"x-emby-authorization", auth.encode())],
                        "scheme": "http", "server": ("t", 80),
                        "client": ("c", 1), "root_path": ""})

    user = SimpleNamespace(id=7)
    api._drive_exists_retry.clear()
    results = []
    for _ in range(4):
        results.append(api.drive_exists_rapid_retry(req(), user, "fburst"))
        for k in list(api._drive_exists_retry):
            api._drive_exists_retry[k] -= 1.0  # 越过 0.3 秒连发豁免
    assert results.count(True) == 1
    api._drive_exists_retry.clear()
