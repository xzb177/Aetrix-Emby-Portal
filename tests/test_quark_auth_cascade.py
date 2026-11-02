# -*- coding: utf-8 -*-
"""夸克转存 403 风暴修复：凭据串行化 + CAS 回写 + 判死去单点化。

钉死的范围（2026-11-02 排查单实证：近 24h failed 127＝真
auth_error 约 42＋级联 no_binding 71；根因是同一凭据被四口并发
共用、读快照→用→无条件覆盖回写互踩 __puus 轮换）：
- absorb 回写 CAS：密封值仍等于取用快照才整体覆盖；已被他人
  轮换时只合并轮换字段（__puus/__pus），绝不拿旧派生值回滚
  他人的新值，绑定不落死。
- per-binding Redis 锁（键 binding:<id>）：同绑定临界区串行；
  Redis 不可用时 fail-open 放行（不因锁设施打停业务）。
- 判死去单点化：转存单次 QuarkAuthError 先用库内最新 Cookie
  验活复核——复核过 → job 按 retriable 重排、绑定仍 active、
  不记 auth_error 终态；复核也鉴权失败 → 才落 expired。
- 可观测性：401/403 时 error_detail 带步位名（dir/pre/hash/
  part/finish/confirm）与上游 code。
- 验活口径对齐：验活在 check + list_root 之外加 list_dir
  全量档（_size=100 + fetch_all_file）。
- 认领止血：quark 落点 per-owner 在飞上限单独钳 1（worker
  认领层），同 owner 其余单与他人单不受影响。

全部离线：sqlite 内存库 + 桩客户端/桩 _send/假 Redis；
monkeypatch 一律 patch 类/模块属性（仓库纪律）。
"""
import os
import threading

os.environ.setdefault("DATABASE_TYPE", "sqlite")
os.environ.setdefault("REDIS_ENABLED", "false")

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend import models as base_models
from backend.emby_server import models as em
from backend.emby_server import quarkdrive
from backend.emby_server import transfer_variant as tv
from backend.emby_server import user_bindings as ub
from backend.emby_server.upstream_sources import seal_secret, unseal_secret
from backend.integrations import store


@pytest.fixture()
def db():
    engine = create_engine("sqlite://",
                           connect_args={"check_same_thread": False},
                           poolclass=StaticPool)
    base_models.Base.metadata.create_all(engine)
    em.Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    store.invalidate()
    yield session
    session.close()
    store.invalidate()


@pytest.fixture(autouse=True)
def _enable_transfer(monkeypatch):
    monkeypatch.setenv("TRANSFER_VARIANT_ENABLED", "1")


_counter = [0]


def _item(db, **kw):
    _counter[0] += 1
    row = em.MediaItem(guid=f"qc-{_counter[0]}", library_id=1,
                       item_type="movie", name="测试片",
                       drive_file_id="origfid",
                       file_path="mount://3/电影/a.mkv", size=1000)
    for key, value in kw.items():
        setattr(row, key, value)
    db.add(row)
    db.commit()
    return row


def _mount(db, mount_id, mount_type, path):
    row = em.StorageMount(id=mount_id, name=f"m{mount_id}",
                          mount_type=mount_type, path=path)
    db.add(row)
    db.commit()
    return row


def _binding(db, user_id, provider="quark", status="active",
             cookie="__puus=tok; __pus=x"):
    cred = em.MountCredential(scope="user", owner_user_id=user_id,
                              status="active",
                              sealed_credential=seal_secret(cookie),
                              key_id="fernet-v1")
    db.add(cred)
    db.commit()
    row = em.UserStorageBinding(user_id=user_id, provider=provider,
                                account_label="test",
                                credential_id=cred.id, status=status)
    db.add(row)
    db.commit()
    return row


def _stored_cookie(db, binding):
    cred = (db.query(em.MountCredential)
            .filter(em.MountCredential.id == binding.credential_id)
            .first())
    return unseal_secret(cred.sealed_credential)


# ---------------------------------------------------------------------------
# absorb CAS：并发两条流同快照出发，一条轮换后另一条不得回滚
# ---------------------------------------------------------------------------

def test_absorb_cas_conflict_merges_rotation_only(db):
    original = "__puus=p0; __pus=s0; other=a"
    binding = _binding(db, 7, "quark", cookie=original)
    # 流 A 先行：轮换 __puus 并改了其他字段后回写（快照仍是原值，
    # 整体覆盖，快路径）。
    flow_a = "__puus=pA; __pus=s0; other=a2"
    assert ub.absorb_quark_cookie_rotation(db, binding, original,
                                           flow_a) is True
    assert _stored_cookie(db, binding) == flow_a
    # 流 B 迟到：仍从旧快照出发，手里是旧派生值（other=a）+
    # 自己拿到的更新轮换 __puus=pB。回写绝不许把 other 滚回 a，
    # 只合并轮换字段。
    flow_b = "__puus=pB; __pus=s0; other=a"
    assert ub.absorb_quark_cookie_rotation(db, binding, original,
                                           flow_b) is True
    merged = _stored_cookie(db, binding)
    assert "other=a2" in merged
    assert "__puus=pB" in merged
    db.refresh(binding)
    assert binding.status == "active"


def test_absorb_cas_no_new_rotation_keeps_stored(db):
    original = "__puus=p0; __pus=s0; other=a"
    binding = _binding(db, 7, "quark", cookie=original)
    flow_a = "__puus=pA; __pus=s0; other=a2"
    assert ub.absorb_quark_cookie_rotation(db, binding, original,
                                           flow_a) is True
    # 流 B 没有拿到任何新轮换（current 只是旧快照的原样重放），
    # 不许覆盖流 A 的新值。
    assert ub.absorb_quark_cookie_rotation(db, binding, original,
                                           original) is False
    assert _stored_cookie(db, binding) == flow_a


# ---------------------------------------------------------------------------
# per-binding 锁：假 Redis 验串行；无 Redis 时 fail-open
# ---------------------------------------------------------------------------

class _FakeRedis:
    """最小 Redis 替身：只实现锁用到的 set(nx/px) 与 eval 语义。"""

    def __init__(self):
        self._data = {}
        self._mu = threading.Lock()

    def set(self, key, value, nx=False, px=None):
        with self._mu:
            if nx and key in self._data:
                return None
            self._data[key] = value
            return True

    def eval(self, script, numkeys, key, *args):
        with self._mu:
            if self._data.get(key) != args[0]:
                return 0
            if "pexpire" in script:
                return 1
            del self._data[key]
            return 1


def test_binding_lock_serializes_same_binding(monkeypatch):
    fake = _FakeRedis()
    import backend.database as dbmod
    monkeypatch.setattr(dbmod, "get_redis", lambda: fake)
    occupancy = {"now": 0, "max": 0}
    held_flags = []
    mu = threading.Lock()

    def worker():
        with ub.quark_binding_lock(42, wait_sec=10) as held:
            held_flags.append(held)
            with mu:
                occupancy["now"] += 1
                occupancy["max"] = max(occupancy["max"],
                                       occupancy["now"])
            import time
            time.sleep(0.15)
            with mu:
                occupancy["now"] -= 1

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=15)
    assert held_flags == [True, True]
    assert occupancy["max"] == 1


def test_binding_lock_fail_open_without_redis(monkeypatch):
    import backend.database as dbmod
    monkeypatch.setattr(dbmod, "get_redis", lambda: None)
    with ub.quark_binding_lock(42, wait_sec=1) as held:
        assert held is False


# ---------------------------------------------------------------------------
# 判死复核：alive / dead / unknown 三态
# ---------------------------------------------------------------------------

class _RecheckClient:
    behavior = "alive"  # alive | dead | unknown

    def __init__(self, cookie):
        self.cookie = cookie

    def check(self):
        if type(self).behavior == "dead":
            raise quarkdrive.QuarkAuthError("夸克登录态失效")
        if type(self).behavior == "unknown":
            raise quarkdrive.QuarkError("夸克请求失败：网络抖动")
        return {"ok": True}

    def list_root(self):
        return []

    def list_dir(self, pdir_fid, page_size=100):
        return []


def test_recheck_verdicts(db):
    binding = _binding(db, 7, "quark")
    _RecheckClient.behavior = "alive"
    assert ub.recheck_quark_binding(
        db, binding, client_factory=_RecheckClient) == "alive"
    _RecheckClient.behavior = "dead"
    assert ub.recheck_quark_binding(
        db, binding, client_factory=_RecheckClient) == "dead"
    _RecheckClient.behavior = "unknown"
    assert ub.recheck_quark_binding(
        db, binding, client_factory=_RecheckClient) == "unknown"
    db.refresh(binding)
    assert binding.status == "active"  # 复核本身永不落状态


# ---------------------------------------------------------------------------
# 执行器：单次鉴权失败先复核，复核过不判死、复核死才判死
# ---------------------------------------------------------------------------

from backend.emby_server import transfer_quark as tq


class _FakeDriveApi:
    def __init__(self, data):
        self._data = data

    def get_file(self, drive_id, fid):
        return {"size": len(self._data)}

    def download_range(self, drive_id, fid, start, end):
        return self._data[start:end + 1]


class _ExecClient:
    """执行器客户端桩：upload 步抛鉴权错；验活步行为可调。"""

    verify_behavior = "alive"  # alive | dead
    instances = []

    def __init__(self, cookie):
        self.cookie = cookie
        self.dir_fid = "qdir1"
        type(self).instances.append(self)

    def list_dir(self, fid, page_size=100):
        if fid == "0":
            return [{"fid": self.dir_fid, "name": "aetrix-transfer",
                     "size": 0, "is_dir": True}]
        return []

    def check(self):
        if type(self).verify_behavior == "dead":
            raise quarkdrive.QuarkAuthError("夸克登录态失效")
        return {"ok": True}

    def list_root(self):
        if type(self).verify_behavior == "dead":
            raise quarkdrive.QuarkAuthError("夸克登录态失效")
        return []

    def upload_file(self, reader, name, size, pdir_fid, *,
                    md5="", sha1=""):
        raise quarkdrive.QuarkAuthError("夸克登录态失效")


@pytest.fixture()
def fake_exec(monkeypatch):
    _ExecClient.verify_behavior = "alive"
    _ExecClient.instances = []
    monkeypatch.setattr(tq, "_make_quark_client", _ExecClient)
    return _ExecClient


def _drive_item(db):
    _mount(db, 3, "drive", "drive:drive1")
    return _item(db)


def _quark_job(db, item, user_id=7):
    row = em.TransferJob(
        item_id=item.id, trigger="playback", drive_id="drive1",
        provider="drive", owner_scope="user", owner_ref=str(user_id),
        dest_drive_id=f"quark:user-{user_id}", status="running",
        attempts=1)
    db.add(row)
    db.commit()
    return row


def test_executor_single_auth_recheck_alive_retries_not_kills(
        db, fake_exec):
    item = _drive_item(db)
    binding = _binding(db, 7, "quark")
    job = _quark_job(db, item)
    out = tq.run_cross_to_quark(db, job, item,
                                _FakeDriveApi(b"x" * 1000))
    assert out["outcome"] == "retried"
    db.refresh(job)
    assert job.status == "pending"
    assert job.error_kind == "retriable"
    db.refresh(binding)
    assert binding.status == "active"


def test_executor_auth_recheck_dead_expires_binding(db, fake_exec):
    fake_exec.verify_behavior = "dead"
    item = _drive_item(db)
    binding = _binding(db, 7, "quark")
    job = _quark_job(db, item)
    out = tq.run_cross_to_quark(db, job, item,
                                _FakeDriveApi(b"x" * 1000))
    assert out["outcome"] == "failed"
    assert out["error_kind"] == "auth_error"
    db.refresh(binding)
    assert binding.status == "expired"
    db.refresh(job)
    assert job.status == "failed"
    assert job.error_kind == "auth_error"


# ---------------------------------------------------------------------------
# 可观测性：401/403 的 error_detail 带步位名与上游 code（真客户端
# + _send 桩，全程 403 → dir 步判死）
# ---------------------------------------------------------------------------

class _Headers:
    def __init__(self, items=None):
        self._m = {}
        for key, value in (items or {}).items():
            self._m[key.lower()] = value

    def get(self, name, default=None):
        return self._m.get(name.lower(), default)

    def get_list(self, name):
        value = self._m.get(name.lower())
        if value is None:
            return []
        return value if isinstance(value, list) else [value]


class _Resp:
    def __init__(self, status=200, body=None, headers=None):
        self.status_code = status
        self._body = body
        self.headers = _Headers(headers)
        self.text = ""

    def json(self):
        return self._body


def test_executor_403_detail_carries_step_and_upstream_code(
        db, monkeypatch):
    def _send_403(method, url, **kwargs):
        return _Resp(403, {"status": 403, "code": 31001,
                           "message": "登录态已过期", "data": None})

    monkeypatch.setattr(quarkdrive, "_send", _send_403)
    item = _drive_item(db)
    binding = _binding(db, 7, "quark")
    job = _quark_job(db, item)
    out = tq.run_cross_to_quark(db, job, item,
                                _FakeDriveApi(b"x" * 1000))
    assert out["outcome"] == "failed"
    assert out["error_kind"] == "auth_error"
    db.refresh(job)
    assert "[dir]" in (job.error_detail or "")
    assert "31001" in (job.error_detail or "")
    db.refresh(binding)
    assert binding.status == "expired"


# ---------------------------------------------------------------------------
# 验活口径对齐：_verify_quark_cookie 含 list_dir 全量档
# ---------------------------------------------------------------------------

def test_verify_quark_cookie_includes_full_list_dir(monkeypatch):
    calls = []

    def _send_ok(method, url, **kwargs):
        calls.append({"method": method, "url": url,
                      "params": kwargs.get("params") or {}})
        if url.endswith("/file/sort"):
            return _Resp(200, {"status": 200, "code": 0, "message": "",
                               "data": {"list": [], "total": 0}})
        return _Resp(200, {"status": 200, "code": 0, "message": "",
                           "data": {}})

    monkeypatch.setattr(quarkdrive, "_send", _send_ok)
    out = ub._verify_quark_cookie("__puus=tok")
    assert out == "__puus=tok"
    sort_calls = [c for c in calls if c["url"].endswith("/file/sort")]
    full = [c for c in sort_calls
            if str(c["params"].get("_size")) == "100"
            and str(c["params"].get("fetch_all_file")) == "1"]
    assert full, "验活缺 list_dir 全量档（_size=100 + fetch_all_file）"


# ---------------------------------------------------------------------------
# 认领止血：quark 落点 per-owner 在飞钳 1
# ---------------------------------------------------------------------------

def _cand(owner_ref, dest, *, trigger="background", minutes_ago=10):
    from datetime import datetime, timedelta
    row = em.TransferJob(
        item_id=1, trigger=trigger, drive_id="drive1",
        provider="drive", owner_scope="user", owner_ref=owner_ref,
        dest_drive_id=dest, status="pending")
    row.created_at = datetime.now() - timedelta(minutes=minutes_ago)
    return row


def test_pick_candidates_quark_per_owner_cap_one():
    # 组内按 id 序：quark、pan115、quark——钳只拦后面的 quark
    # 单，同 owner 的非 quark 单照常被挑（挑单算法的轮转语义）。
    jobs = [
        _cand("7", "quark:user-7"),
        _cand("7", "pan115:user-7"),
        _cand("7", "quark:user-7"),
        _cand("8", "quark:user-8"),
        _cand("8", "quark:user-8"),
    ]
    picked = tv._pick_candidates(
        jobs, 10, {}, 4, cross_cap=8,
        quark_cap=1, quark_running_counts={})
    picked_ids = {id(j) for j in picked}
    quark7 = [j for j in (jobs[0], jobs[2]) if id(j) in picked_ids]
    quark8 = [j for j in jobs[3:] if id(j) in picked_ids]
    assert len(quark7) == 1
    assert len(quark8) == 1
    assert id(jobs[1]) in picked_ids  # 同 owner 的非 quark 单照常


def test_pick_candidates_quark_cap_counts_running():
    jobs = [_cand("7", "quark:user-7")]
    picked = tv._pick_candidates(
        jobs, 10, {}, 4, cross_cap=8,
        quark_cap=1, quark_running_counts={("user", "7"): 1})
    assert picked == []
