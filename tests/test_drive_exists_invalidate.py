"""存在性正缓存：删除跨进程失效 + 同起始 Range 短时复验。

正缓存默认 600s。文件已在网盘删除、库还没扫完时，播放不能继续 302 到死链。
- 扫描确认丢失 / 管理端删除 / drive_share 见到 404 → 摘缓存；Redis 墓碑跨进程，
  Redis 挂了只清本进程且不抛。
- 同一客户端、同一文件、相同 Range 起始在窗口内又请求 → 跳过缓存重新 files.get。
  起始字节变了（拖动 / 下一段）仍走缓存。
"""
import os

os.environ.setdefault("DATABASE_TYPE", "sqlite")
os.environ.setdefault("REDIS_ENABLED", "false")

import asyncio
import time
import uuid
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from starlette.requests import Request

from backend import models
from backend.emby_server import api, drive_auth, drive_share as dsh
from backend.emby_server import playback_risk
from backend.emby_server import models as em
from backend.emby_server import playback_strategy as strategy
from backend.emby_server import scanner


@pytest.fixture(autouse=True)
def _grace_single_pass(monkeypatch):
    # 存在性缓存摘除挂在「真正判缺」那一刻；缺失宽限（批一 #6）默认
    # 3 轮才判缺，本文件测摘除本身，压到 1 轮隔离变量
    monkeypatch.setattr(scanner, "MISSING_GRACE_SCANS", 1)


DRIVE = "0ASharedDrive"
FID = "fileGone1"


class FakeRedis:
    def __init__(self, boom=False):
        self.store = {}
        self.boom = boom

    def _check(self):
        if self.boom:
            raise RuntimeError("redis down")

    def set(self, key, val, ex=None, nx=False):
        self._check()
        if nx and key in self.store:
            return None
        self.store[key] = val
        return True

    def exists(self, key):
        self._check()
        return 1 if key in self.store else 0

    def delete(self, key):
        self._check()
        return 1 if self.store.pop(key, None) is not None else 0

    def expire(self, key, sec):
        self._check()
        return key in self.store

    def get(self, key):
        self._check()
        return self.store.get(key)


def _req(range_header=None, device=None, session=None):
    headers = []
    if range_header is not None:
        headers.append((b"range", range_header.encode()))
    if device:
        auth = f'MediaBrowser Client="T", DeviceId="{device}", Version="1"'
        headers.append((b"x-emby-authorization", auth.encode()))
    qs = b""
    if session:
        qs = f"PlaySessionId={session}".encode()
    scope = {
        "type": "http", "method": "GET", "path": "/Videos/item/stream",
        "raw_path": b"/Videos/item/stream", "query_string": qs,
        "headers": headers, "scheme": "http", "server": ("testserver", 80),
        "client": ("testclient", 50000), "root_path": "",
    }
    return Request(scope)


def _user(uid=7):
    return SimpleNamespace(id=uid)


@pytest.fixture(autouse=True)
def _iso(monkeypatch):
    monkeypatch.setattr(api, "_drive_exists_redis", lambda: None)
    monkeypatch.setattr(playback_risk, "_ensure_flusher", lambda: None)
    api._drive_exists_cache.clear()
    api._drive_exists_retry.clear()
    api._drive_exists_forced.clear()
    # 本文件钉“每次真重试都复验”的旧口径；强制复验限频另见
    # tests/test_startup_dedup.py。
    monkeypatch.setenv("DRIVE_EXISTS_FORCE_MIN_INTERVAL_SEC", "0")
    yield
    api._drive_exists_cache.clear()
    api._drive_exists_retry.clear()
    api._drive_exists_forced.clear()


@pytest.fixture()
def db():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    models.Base.metadata.create_all(engine)
    em.Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    yield session
    session.close()


def _http(monkeypatch, code=200):
    calls = []

    def fake(url, *, params=None, headers=None, timeout=None):
        calls.append(code)
        return SimpleNamespace(status_code=code)

    monkeypatch.setattr(api, "_drive_exists_http_get", fake)
    from backend.emby_server import drive_changes
    monkeypatch.setattr(drive_changes, "get_token_for_drive",
                        lambda drive_id, force_refresh=False: "tok")
    return calls


def _lib(db):
    row = em.Library(guid=uuid.uuid4().hex, name="库", collection_type="movies", paths="")
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def _item(db, lib_id, guid, path, drive_file_id=None):
    row = em.MediaItem(
        guid=guid, library_id=lib_id, item_type="movie", name=guid,
        file_path=path, size=4096, drive_file_id=drive_file_id)
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


# ---------- 失效 ----------

def test_invalidate_drops_local_and_redis_tombstone_blocks_other_process(monkeypatch):
    fake = FakeRedis()
    monkeypatch.setattr(api, "_drive_exists_redis", lambda: fake)
    api._drive_exists_cache[(DRIVE, FID)] = time.monotonic() + 600
    api.invalidate_drive_exists_cache(DRIVE, FID)
    assert (DRIVE, FID) not in api._drive_exists_cache
    assert fake.exists(api._exists_inval_key(FID)) == 1
    # EA 进程里还留着失效前写入的正缓存
    api._drive_exists_cache[(DRIVE, FID)] = time.monotonic() + 600
    assert api._drive_exists_cache_hit(DRIVE, FID) is False
    assert (DRIVE, FID) not in api._drive_exists_cache


def test_invalidate_redis_down_local_only_no_error(monkeypatch):
    monkeypatch.setattr(api, "_drive_exists_redis", lambda: FakeRedis(boom=True))
    api._drive_exists_cache[(DRIVE, FID)] = time.monotonic() + 600
    api.invalidate_drive_exists_cache(DRIVE, FID)  # 不抛
    assert (DRIVE, FID) not in api._drive_exists_cache
    # 别的进程没收到墓碑：它自己的正缓存仍然命中
    api._drive_exists_cache[(DRIVE, FID)] = time.monotonic() + 600
    assert api._drive_exists_cache_hit(DRIVE, FID) is True


def test_confirmed_exists_clears_tombstone(monkeypatch):
    fake = FakeRedis()
    monkeypatch.setattr(api, "_drive_exists_redis", lambda: fake)
    api.invalidate_drive_exists_cache(DRIVE, FID)
    assert fake.exists(api._exists_inval_key(FID)) == 1
    calls = _http(monkeypatch, 200)
    api._drive_file_exists_sync(DRIVE, FID)
    assert len(calls) == 1
    assert fake.exists(api._exists_inval_key(FID)) == 0
    api._drive_file_exists_sync(DRIVE, FID)
    assert len(calls) == 1


def test_redis_ok_promotes_local_on_miss(monkeypatch):
    """本进程未命中时，跨进程正缓存（预热 / 工人）应省掉 files.get。"""
    fake = FakeRedis()
    fake.set(api._DRIVE_EXISTS_OK_PREFIX + FID, "1")
    monkeypatch.setattr(api, "_drive_exists_redis", lambda: fake)
    assert (DRIVE, FID) not in api._drive_exists_cache
    assert api._drive_exists_cache_hit(DRIVE, FID) is True
    assert (DRIVE, FID) in api._drive_exists_cache


def test_tombstone_beats_redis_ok_on_miss(monkeypatch):
    fake = FakeRedis()
    fake.set(api._DRIVE_EXISTS_OK_PREFIX + FID, "1")
    fake.set(api._exists_inval_key(FID), "1")
    monkeypatch.setattr(api, "_drive_exists_redis", lambda: fake)
    assert api._drive_exists_cache_hit(DRIVE, FID) is False
    assert (DRIVE, FID) not in api._drive_exists_cache


def test_recheck_404_evicts_positive_entry(monkeypatch):
    fake = FakeRedis()
    monkeypatch.setattr(api, "_drive_exists_redis", lambda: fake)
    _http(monkeypatch, 200)
    api._drive_file_exists_sync(DRIVE, FID)
    assert (DRIVE, FID) in api._drive_exists_cache
    _http(monkeypatch, 404)
    with pytest.raises(api._DriveFileGone):
        api._drive_file_exists_sync(DRIVE, FID, force_recheck=True)
    assert (DRIVE, FID) not in api._drive_exists_cache
    assert fake.exists(api._exists_inval_key(FID)) == 1


def test_scanner_gone_file_evicts_present_file_does_not(db, monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(api, "invalidate_drive_exists_for_item",
                        lambda db_, item: calls.append((item.drive_file_id, item.file_path)))
    lib = _lib(db)
    kept_path = tmp_path / "kept.mkv"
    kept_path.write_bytes(b"x")
    _item(db, lib.id, "keep-guid", str(kept_path), drive_file_id="fid-kept")
    _item(db, lib.id, "gone-guid", str(tmp_path / "missing.mkv"), drive_file_id="fid-gone")
    removed = scanner._remove_missing_items(db, lib, {"keep-guid"})
    assert removed == 1
    assert calls == [("fid-gone", str(tmp_path / "missing.mkv"))]


def test_admin_delete_evicts_drive_file(db, monkeypatch):
    from backend.emby_server import portal
    calls = []
    monkeypatch.setattr(api, "invalidate_drive_exists_for_item",
                        lambda db_, item: calls.append(item.drive_file_id))
    lib = _lib(db)
    item = _item(db, lib.id, uuid.uuid4().hex, None, drive_file_id="fid-admin")
    plain = _item(db, lib.id, uuid.uuid4().hex, None, drive_file_id=None)
    portal.admin_delete_item(item.guid, staff=None, db=db)
    assert calls == ["fid-admin"]
    portal.admin_delete_item(plain.guid, staff=None, db=db)
    assert calls == ["fid-admin"]


def test_share_404_is_not_found(monkeypatch):
    monkeypatch.setattr(dsh, "_http_post",
                        lambda *a, **k: SimpleNamespace(status_code=404, json=lambda: {}))
    assert dsh.ensure_shared("fid", "tok") == "not_found"


def test_patrol_404_evicts(db, monkeypatch):
    calls = []
    monkeypatch.setattr(api, "invalidate_drive_exists_for_item",
                        lambda db_, item: calls.append(item.drive_file_id))
    monkeypatch.setattr(dsh, "_JITTER_MIN_SEC", 0.0)
    monkeypatch.setattr(dsh, "_JITTER_MAX_SEC", 0.0)
    monkeypatch.setattr(dsh, "DRIVE_SHARE_AUTO_ENABLED", True)
    monkeypatch.setattr(dsh, "_http_post",
                        lambda *a, **k: SimpleNamespace(status_code=404, json=lambda: {}))
    monkeypatch.setattr(drive_auth, "get_drive_bearer_token", lambda *a, **k: "tok")
    lib = _lib(db)
    _item(db, lib.id, uuid.uuid4().hex, "/tmp/nope.mkv", drive_file_id="fid-patrol")
    assert dsh.drive_share_patrol(db, limit=10) == 0
    assert calls == ["fid-patrol"]


def test_share_worker_404_evicts(monkeypatch):
    seen = []
    monkeypatch.setattr(dsh, "ensure_shared", lambda f, t: "not_found")
    monkeypatch.setattr(dsh, "_invalidate_gone_file",
                        lambda item_id, file_id, db=None: seen.append((item_id, file_id)))
    monkeypatch.setattr(dsh, "_mark_share_result", lambda *a, **k: None)
    monkeypatch.setattr(drive_auth, "get_drive_bearer_token", lambda *a, **k: "tok")
    assert dsh._process_one_share(9, "fid-q") == "not_found"
    assert seen == [(9, "fid-q")]


def _age_retries(sec=1.0):
    """把登记时间往前拨。1s 已过开播连发（0.3s），仍在默认 15s 复验窗口内。"""
    for key in list(api._drive_exists_retry):
        api._drive_exists_retry[key] -= sec


# ---------- 重试复验 ----------

def test_retry_window_env(monkeypatch, caplog):
    """窗口在启动时解析。负数=关闭；非法值 warning 不进播放热路径。"""
    import logging
    monkeypatch.delenv("DRIVE_EXISTS_RETRY_WINDOW_SEC", raising=False)
    assert api._drive_exists_retry_window_from_env() == 15
    monkeypatch.setenv("DRIVE_EXISTS_RETRY_WINDOW_SEC", "0")
    assert api._drive_exists_retry_window_from_env() == 0
    monkeypatch.setenv("DRIVE_EXISTS_RETRY_WINDOW_SEC", "-3")
    assert api._drive_exists_retry_window_from_env() == 0
    monkeypatch.setenv("DRIVE_EXISTS_RETRY_WINDOW_SEC", "99999")
    assert api._drive_exists_retry_window_from_env() == 600
    monkeypatch.setenv("DRIVE_EXISTS_RETRY_WINDOW_SEC", "abc")
    with caplog.at_level(logging.WARNING):
        assert api._drive_exists_retry_window_from_env() == 15
    assert any("DRIVE_EXISTS_RETRY_WINDOW_SEC" in r.message for r in caplog.records)
    caplog.clear()
    with caplog.at_level(logging.WARNING):
        for _ in range(5):
            api.drive_exists_retry_window()
    assert not any("DRIVE_EXISTS_RETRY_WINDOW_SEC" in r.message for r in caplog.records)


def test_same_range_is_retry_seek_is_not():
    user = _user()
    fid = "file-retry"
    assert api.drive_exists_rapid_retry(_req("bytes=0-", device="devA"), user, fid) is False
    # 下一段 / 拖动：起始字节变了，不算重试
    assert api.drive_exists_rapid_retry(
        _req("bytes=1048576-2097151", device="devA"), user, fid) is False
    # 非 0 起始的重复立刻算重试
    assert api.drive_exists_rapid_retry(
        _req("bytes=1048576-2097151", device="devA"), user, fid) is True
    # 开播连发的 bytes=0- 不算重试，时间戳停在第一次
    assert api.drive_exists_rapid_retry(_req("bytes=0-", device="devA"), user, fid) is False
    _age_retries(1.0)
    assert api.drive_exists_rapid_retry(_req("bytes=0-", device="devA"), user, fid) is True
    # 另一台设备从 0 开始，是它自己的第一次
    assert api.drive_exists_rapid_retry(_req("bytes=0-", device="devB"), user, fid) is False


def test_absent_range_is_not_byte_zero():
    user = _user()
    fid = "file-absent"
    assert api.drive_exists_rapid_retry(_req(None, device="devA"), user, fid) is False
    assert api.drive_exists_rapid_retry(_req("bytes=0-", device="devA"), user, fid) is False
    assert api.drive_exists_rapid_retry(_req(None, device="devA"), user, fid) is True


def test_retry_window_expiry_and_zero_disables(monkeypatch):
    user = _user()
    fid = "file-age"
    req = _req("bytes=0-", device="devA")
    assert api.drive_exists_rapid_retry(req, user, fid) is False
    for key in list(api._drive_exists_retry):
        api._drive_exists_retry[key] -= 100
    assert api.drive_exists_rapid_retry(req, user, fid) is False
    monkeypatch.setattr(api, "_retry_window_sec", 0)
    assert api.drive_exists_rapid_retry(req, user, fid) is False
    assert api.drive_exists_rapid_retry(req, user, fid) is False


def test_retry_seen_across_process_via_redis(monkeypatch):
    fake = FakeRedis()
    monkeypatch.setattr(api, "_drive_exists_redis", lambda: fake)
    user = _user()
    req = _req("bytes=0-", device="devA")
    assert api.drive_exists_rapid_retry(req, user, "file-xproc") is False
    api._drive_exists_retry.clear()  # 另一个 EA worker 没有本进程记忆
    # 开播连发：Redis 里的时间戳还很新，不当重试
    assert api.drive_exists_rapid_retry(req, user, "file-xproc") is False
    for key, val in list(fake.store.items()):
        if str(key).startswith(api._DRIVE_EXISTS_RETRY_PREFIX):
            fake.store[key] = f"{time.time() - 1:.6f}"
    api._drive_exists_retry.clear()
    assert api.drive_exists_rapid_retry(req, user, "file-xproc") is True
    # 起始不同：另一把钥匙，即使 Redis 在也不算重试
    assert api.drive_exists_rapid_retry(
        _req("bytes=4096-", device="devA"), user, "file-xproc") is False
    api._drive_exists_retry.clear()
    # 非 0 起始的重复立刻算重试（不走 300ms 连发豁免）
    assert api.drive_exists_rapid_retry(
        _req("bytes=4096-", device="devA"), user, "file-xproc") is True


def test_retry_redis_down_uses_local(monkeypatch):
    monkeypatch.setattr(api, "_drive_exists_redis", lambda: FakeRedis(boom=True))
    user = _user()
    req = _req("bytes=0-", device="devA")
    assert api.drive_exists_rapid_retry(req, user, "file-local") is False
    assert api.drive_exists_rapid_retry(req, user, "file-local") is False
    _age_retries(1.0)
    assert api.drive_exists_rapid_retry(req, user, "file-local") is True


def test_force_recheck_refetches_seek_does_not(monkeypatch):
    calls = _http(monkeypatch, 200)
    api._drive_file_exists_sync(DRIVE, "file-force")
    assert len(calls) == 1
    user = _user()
    assert api.drive_exists_rapid_retry(
        _req("bytes=0-", device="devA"), user, "file-force") is False
    assert api.drive_exists_rapid_retry(
        _req("bytes=1048576-", device="devA"), user, "file-force") is False
    api._drive_file_exists_sync(DRIVE, "file-force")  # 拖动仍走缓存
    assert len(calls) == 1
    _age_retries(1.0)
    assert api.drive_exists_rapid_retry(
        _req("bytes=0-", device="devA"), user, "file-force") is True
    api._drive_file_exists_sync(DRIVE, "file-force", force_recheck=True)
    assert len(calls) == 2
    # 200 刷新了缓存，下一次非重试不再打
    api._drive_file_exists_sync(DRIVE, "file-force")
    assert len(calls) == 2


def _stream_request(range_header, device):
    return _req(range_header, device=device)


def _stub_stream(monkeypatch, fid, codes):
    """codes 是 files.get 依次返回的状态码；用完后重复最后一个。"""
    item = SimpleNamespace(
        container="mkv", drive_file_id=fid,
        file_path="mount://3/MoviePilot/x.mkv", library=None)
    mount = SimpleNamespace(
        id=3, path="drive:" + DRIVE, is_enabled=True, mount_type="drive", config="{}")

    class _Q:
        def filter(self, *a, **k):
            return self

        def first(self):
            return mount

    class _QEmpty:
        def filter(self, *a, **k):
            return self

        def first(self):
            return None

        def all(self):
            return []

    # 路径授权门禁（#1280 接线）会在 video_stream 内查三层授权表；
    # 本桩库除挂载查询外对授权表一律回空（无行 = 放行），否则通配桩
    # 会把挂载对象当成角色映射回给门禁，触发 fail-closed 403。
    _PATH_AUTH_MODELS = (
        em.UserRoleTemplate, em.RoleTemplateSourcePathGrant,
        em.UserSourcePathGrant, em.LibraryAccessGrant,
    )

    class _DB:
        def query(self, model):
            model_cls = getattr(model, "class_", model)
            if model_cls in _PATH_AUTH_MODELS:
                return _QEmpty()
            return _Q()

    monkeypatch.setattr(api, "_require_item", lambda db, item_id: item)
    monkeypatch.setattr(api, "_item_visible", lambda db, user, it: True)
    monkeypatch.setattr(api, "ensure_playback_allowed", lambda db, user: None)
    monkeypatch.setattr(strategy, "drive_direct_play_enabled", lambda db: True)
    seq = list(codes)

    def fake(url, *, params=None, headers=None, timeout=None):
        code = seq.pop(0) if seq else codes[-1]
        return SimpleNamespace(status_code=code)

    monkeypatch.setattr(api, "_drive_exists_http_get", fake)
    from backend.emby_server import drive_changes
    monkeypatch.setattr(drive_changes, "get_token_for_drive",
                        lambda drive_id, force_refresh=False: "tok")
    return _DB(), fake


def test_video_stream_retry_rechecks_seek_does_not(monkeypatch):
    fid = "file-stream-retry"
    calls = []

    def fake(url, *, params=None, headers=None, timeout=None):
        calls.append(1)
        return SimpleNamespace(status_code=200)

    db, _ = _stub_stream(monkeypatch, fid, [200])
    monkeypatch.setattr(api, "_drive_exists_http_get", fake)
    user = _user()

    async def once(rng, device):
        return await api.video_stream("item", _stream_request(rng, device), user, db)

    resp = asyncio.run(once("bytes=0-", "player-1"))
    assert resp.status_code == 302 and len(calls) == 1
    # 下一段：缓存命中，不再 files.get
    resp = asyncio.run(once("bytes=1048576-", "player-1"))
    assert resp.status_code == 302 and len(calls) == 1
    # 过了开播连发，同一起始又来：复验
    _age_retries(1.0)
    resp = asyncio.run(once("bytes=0-", "player-1"))
    assert resp.status_code == 302 and len(calls) == 2
    # 别的设备同一起始：它的第一次，缓存仍有效
    resp = asyncio.run(once("bytes=0-", "player-2"))
    assert resp.status_code == 302 and len(calls) == 2


def test_video_stream_retry_404_evicts(monkeypatch):
    fid = "file-stream-404"
    calls = []

    def fake(url, *, params=None, headers=None, timeout=None):
        calls.append(1)
        return SimpleNamespace(status_code=404 if len(calls) > 1 else 200)

    db, _ = _stub_stream(monkeypatch, fid, [200])
    monkeypatch.setattr(api, "_drive_exists_http_get", fake)
    user = _user()

    async def once(rng):
        return await api.video_stream("item", _stream_request(rng, "player-1"), user, db)

    resp = asyncio.run(once("bytes=0-"))
    assert resp.status_code == 302 and len(calls) == 1
    _age_retries(1.0)
    with pytest.raises(Exception) as exc:
        asyncio.run(once("bytes=0-"))
    assert getattr(exc.value, "status_code", None) == 404
    assert (DRIVE, fid) not in api._drive_exists_cache
    # 墓碑 / 本地都没了：换个起始也会重新 files.get，而不是再 302
    with pytest.raises(Exception) as exc2:
        asyncio.run(once("bytes=999-"))
    assert getattr(exc2.value, "status_code", None) == 404
    assert len(calls) == 3
