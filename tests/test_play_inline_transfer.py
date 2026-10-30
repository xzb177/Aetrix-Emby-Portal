# -*- coding: utf-8 -*-
"""点播当场先办（2026-10-29 用户亲点）：点播触发的同 provider
服务端 copy 不再必经工人队列，请求路径当场领取执行，办成即同
一请求内放行；办不完/不合格/没名额才回 preparing 让工人兜底。

钉住的口径：
- 当场成功：同一请求内重查门禁变 proceed、job done、变体
  ready，执行核心只被调一次；
- 当场超时：回 preparing，任务行不被弄脏（仍是认领语义下的
  running + 有效租约），执行在后台收尾后重查门禁变 proceed；
- 同 (item, owner) 并发两请求：执行核心只被调一次；
- 已被工人认领的单：请求侧不当场跑；
- 开关关：行为与现行纯队列逐字一致（不领、不跑、回原 verdict）；
- 名额满：立即回 preparing，不等待、不执行；
- 不合格单（跨 provider / file-only / 非点播触发 / 服主盘 /
  无绑定）：不进当场路径，执行核心零调用、任务行不动；
- 真失败：按现行 error_kind 落账，重查门禁判 failed，不吞。

全部离线：SQLite + 假执行器（monkeypatch 只打模块属性）；
真实 Drive/115 不碰。
"""
import os

os.environ.setdefault("DATABASE_TYPE", "sqlite")
os.environ.setdefault("REDIS_ENABLED", "false")

import asyncio
import threading
import time
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend import models as base_models
from backend.emby_server import models as em
from backend.emby_server import transfer_variant as tv
from backend.emby_server.drive_oauth import (
    DRIVE_FILE_SCOPE, DRIVE_READONLY_SCOPE)
from backend.integrations import store


@pytest.fixture()
def db_factory(tmp_path):
    # 文件库 + 默认连接池：每条线程（两路 to_thread 认领线程、心跳
    # 线程、主线程）各自独立连接——与生产真实连接池同语义。
    # 不许用 StaticPool 共享单连接：pysqlite 的 cursor.rowcount 取自
    # 连接级 sqlite3_changes() 计数，两路并发认领的 UPDATE 在同一
    # 连接上交错时，输家命中 0 行的 UPDATE 会把计数清零，赢家随后
    # 读到的 rowcount 变 0，两路都误判 not_claimed、执行核心零调用
    # （2026-10-30 抖动定性 trace 实锤：UPDATE 后行已是 running，
    # 两路 rowcount 却同为 0，test_concurrent_requests_execute_once
    # 因此随机红无关 PR 的 CI）。timeout 是 sqlite 忙等上限：输家
    # 的 UPDATE 要等赢家提交后才看得到 status 已变，需要忙等而非
    # 直接报 database is locked。
    engine = create_engine(
        f"sqlite:///{tmp_path}/play_inline.db",
        connect_args={"check_same_thread": False, "timeout": 30})
    base_models.Base.metadata.create_all(engine)
    em.Base.metadata.create_all(engine)
    yield sessionmaker(bind=engine)
    engine.dispose()


@pytest.fixture()
def db(db_factory):
    session = db_factory()
    yield session
    session.close()


@pytest.fixture(autouse=True)
def _clean_store_cache():
    store.invalidate()
    yield
    store.invalidate()


@pytest.fixture(autouse=True)
def _inline_counter_clean():
    """当场执行在飞计数是进程全局：每例前后归零防互相污染。"""
    with tv._inline_inflight_lock:
        tv._inline_inflight = 0
    yield
    with tv._inline_inflight_lock:
        tv._inline_inflight = 0


def _set_config(db, key, value):
    row = (db.query(base_models.SystemConfig)
           .filter(base_models.SystemConfig.key == key).first())
    if row is None:
        db.add(base_models.SystemConfig(key=key, value=value))
    else:
        row.value = value
    db.commit()
    store.invalidate()


def _mount(db, mount_id=3, mount_type="drive", path="drive:drive1"):
    row = em.StorageMount(id=mount_id, name=f"m{mount_id}",
                          mount_type=mount_type, path=path)
    db.add(row)
    db.commit()
    return row


_item_counter = [0]


def _item(db, **kw):
    _item_counter[0] += 1
    row = em.MediaItem(guid=f"pi-{_item_counter[0]}", library_id=1,
                       item_type="movie", name="测试片",
                       drive_file_id="origfid",
                       file_path="mount://3/电影/a.mkv", size=1000)
    for key, value in kw.items():
        setattr(row, key, value)
    db.add(row)
    db.commit()
    return row


def _binding(db, user_id, provider="drive", status="active",
             granted_scopes=None):
    cred = em.MountCredential(scope="user", owner_user_id=user_id,
                              status="active")
    db.add(cred)
    db.commit()
    row = em.UserStorageBinding(
        user_id=user_id, provider=provider, account_label="test",
        credential_id=cred.id, status=status,
        granted_scopes=granted_scopes)
    db.add(row)
    db.commit()
    return row


def _user_job(db, item_id, user_id, provider="drive", status="pending",
              trigger="playback", owner_scope="user", dest=None):
    if dest is None:
        dest = (f"userdrive:{user_id}" if provider == "drive"
                else f"pan115:user-{user_id}")
    row = em.TransferJob(
        item_id=item_id, trigger=trigger, drive_id="drive1",
        provider=provider, owner_scope=owner_scope,
        owner_ref=str(user_id) if owner_scope == "user" else None,
        dest_drive_id=dest, status=status)
    db.add(row)
    db.commit()
    return row


def _user(user_id=7):
    return SimpleNamespace(id=user_id)


def _enable(monkeypatch):
    monkeypatch.setenv("TRANSFER_VARIANT_ENABLED", "1")


def _cfg(monkeypatch, enabled=True, budget_sec=10, concurrency=4):
    monkeypatch.setattr(tv, "_play_inline_config", lambda: {
        "enabled": enabled, "budget_sec": budget_sec,
        "concurrency": concurrency})


def _job_state(db, job_id):
    db.expire_all()
    job = (db.query(em.TransferJob)
           .filter(em.TransferJob.id == job_id).one())
    return job.status, job.error_kind, job.claimed_by


def _done_executor(calls, delay=0.0):
    """假执行核心下游：把单办成 done 并登记 ready 变体行。"""
    def _run(db_session, job_row, drive_api):
        calls.append(job_row.id)
        if delay:
            time.sleep(delay)
        job_row.status = "done"
        job_row.error_kind = None
        job_row.error_detail = None
        job_row.next_retry_at = None
        db_session.add(em.TransferVariant(
            item_id=job_row.item_id, source_file_id="origfid",
            variant_file_id="copyfid",
            variant_drive_id=job_row.dest_drive_id,
            owner_scope="user", owner_ref=job_row.owner_ref,
            status="ready", size_bytes=1000))
        db_session.commit()
        return {"job_id": job_row.id, "outcome": "done"}
    return _run


def _regate_factory(db, item, user):
    async def _regate():
        return tv.ensure_user_copy_gate(db, item, user)
    return _regate


# ---------- 配置键 ----------

def test_inline_config_defaults_and_overrides(db):
    """三键默认：开 / 20 秒 / 4；SystemConfig 行覆盖；钳制生效。"""
    assert tv.play_inline_enabled(db) is True
    assert tv.play_inline_budget_sec(db) == 20
    assert tv.play_inline_concurrency(db) == 4
    _set_config(db, "play_inline_transfer_enabled", "false")
    _set_config(db, "play_inline_transfer_budget_sec", "45")
    _set_config(db, "play_inline_transfer_concurrency", "2")
    assert tv.play_inline_enabled(db) is False
    assert tv.play_inline_budget_sec(db) == 45
    assert tv.play_inline_concurrency(db) == 2
    _set_config(db, "play_inline_transfer_budget_sec", "999")
    _set_config(db, "play_inline_transfer_concurrency", "99")
    assert tv.play_inline_budget_sec(db) == 120
    assert tv.play_inline_concurrency(db) == 16
    _set_config(db, "play_inline_transfer_budget_sec", "abc")
    assert tv.play_inline_budget_sec(db) == 20


# ---------- 原子认领 ----------

def test_claim_job_for_inline_atomic(db, db_factory):
    """pending 已到期可领且只领一次；running/退避期领不到。"""
    job = _user_job(db, 1, 7)
    assert tv.claim_job_for_inline(db, job.id) is True
    status, _kind, claimed_by = _job_state(db, job.id)
    assert status == "running"
    assert claimed_by == tv._INLINE_WORKER_ID
    other = db_factory()
    try:
        assert tv.claim_job_for_inline(other, job.id) is False
    finally:
        other.close()
    backoff = _user_job(db, 2, 7)
    backoff.next_retry_at = datetime.now() + timedelta(hours=1)
    db.commit()
    assert tv.claim_job_for_inline(db, backoff.id) is False
    assert _job_state(db, backoff.id)[0] == "pending"


# ---------- 当场成功：同一请求内放行 ----------

def test_inline_success_releases_same_request(db, db_factory, monkeypatch):
    _enable(monkeypatch)
    _cfg(monkeypatch)
    _mount(db)
    item = _item(db)
    _binding(db, 7)
    calls = []
    monkeypatch.setattr(tv, "run_transfer_job_once", _done_executor(calls))
    gate = tv.ensure_user_copy_gate(db, item, _user())
    assert gate["verdict"] == "preparing" and gate["reason"] == "job_created"
    result = asyncio.run(tv.maybe_run_play_inline(
        gate, _regate_factory(db, item, _user()),
        session_factory=db_factory, drive_api_factory=lambda: None))
    assert result["verdict"] == "proceed"
    assert result["reason"] == "has_ready_variant"
    assert calls == [gate["job_id"]]
    assert _job_state(db, gate["job_id"])[0] == "done"


def test_inline_failure_lands_error_kind(db, db_factory, monkeypatch):
    """真失败按现行 error_kind 落账：重查门禁判 failed，不吞。"""
    _enable(monkeypatch)
    _cfg(monkeypatch)
    _mount(db)
    item = _item(db)
    _binding(db, 7)

    def _dead(db_session, job_row, drive_api):
        job_row.status = "failed"
        job_row.error_kind = "dead"
        job_row.error_detail = "原件已死"
        db_session.commit()
        return {"job_id": job_row.id, "outcome": "failed",
                "error_kind": "dead"}

    monkeypatch.setattr(tv, "run_transfer_job_once", _dead)
    gate = tv.ensure_user_copy_gate(db, item, _user())
    assert gate["verdict"] == "preparing"
    result = asyncio.run(tv.maybe_run_play_inline(
        gate, _regate_factory(db, item, _user()),
        session_factory=db_factory, drive_api_factory=lambda: None))
    assert result["verdict"] == "failed" and result["reason"] == "dead"
    status, kind, _by = _job_state(db, gate["job_id"])
    assert status == "failed" and kind == "dead"


# ---------- 当场超时：状态不脏、后台收尾后可播 ----------

def test_inline_timeout_leaves_clean_state(db, db_factory, monkeypatch):
    _enable(monkeypatch)
    _cfg(monkeypatch, budget_sec=0.3)
    _mount(db)
    item = _item(db)
    _binding(db, 7)
    calls = []
    monkeypatch.setattr(
        tv, "run_transfer_job_once", _done_executor(calls, delay=2.0))
    gate = tv.ensure_user_copy_gate(db, item, _user())
    assert gate["verdict"] == "preparing"
    job_id = gate["job_id"]

    async def _scenario():
        result = await tv.maybe_run_play_inline(
            gate, _regate_factory(db, item, _user()),
            session_factory=db_factory, drive_api_factory=lambda: None)
        # 预算到点回 preparing；此刻任务行必须是认领语义下的
        # running + 有效租约，没有被写成任何假终态。
        assert result["verdict"] == "preparing"
        status, _kind, claimed_by = _job_state(db, job_id)
        assert status == "running" and claimed_by == tv._INLINE_WORKER_ID
        # 工人此刻领不到同一单（租约未过期，不双跑）。
        s = db_factory()
        try:
            assert tv.claim_due_jobs(s) == []
        finally:
            s.close()
        # 后台执行自己收尾（同一执行核心），之后重查变 proceed。
        await asyncio.sleep(2.5)
        return tv.ensure_user_copy_gate(db, item, _user())

    final = asyncio.run(_scenario())
    assert final["verdict"] == "proceed"
    assert calls == [job_id]
    assert _job_state(db, job_id)[0] == "done"


# ---------- 并发：执行核心只被调一次 ----------

def test_concurrent_requests_execute_once(db, db_factory, monkeypatch):
    _enable(monkeypatch)
    _cfg(monkeypatch, budget_sec=10)
    _mount(db)
    item = _item(db)
    _binding(db, 7)
    calls = []
    monkeypatch.setattr(
        tv, "run_transfer_job_once", _done_executor(calls, delay=0.5))
    gate = tv.ensure_user_copy_gate(db, item, _user())
    assert gate["verdict"] == "preparing"

    async def _both():
        regate = _regate_factory(db, item, _user())
        return await asyncio.gather(
            tv.maybe_run_play_inline(
                gate, regate, session_factory=db_factory,
                drive_api_factory=lambda: None),
            tv.maybe_run_play_inline(
                gate, regate, session_factory=db_factory,
                drive_api_factory=lambda: None))

    results = asyncio.run(_both())
    assert calls == [gate["job_id"]]
    verdicts = sorted(r["verdict"] for r in results)
    assert verdicts == ["preparing", "proceed"]


def test_worker_claimed_job_not_run_inline(db, db_factory, monkeypatch):
    """已被工人认领（running + 租约）的单，请求侧不当场跑。"""
    _enable(monkeypatch)
    _cfg(monkeypatch)
    _mount(db)
    item = _item(db)
    _binding(db, 7)
    job = _user_job(db, item.id, 7, status="running")
    job.lease_expires_at = datetime.now() + timedelta(seconds=300)
    job.claimed_by = "worker-xyz"
    db.commit()
    calls = []
    monkeypatch.setattr(tv, "run_transfer_job_once", _done_executor(calls))
    gate = tv.ensure_user_copy_gate(db, item, _user())
    assert gate["verdict"] == "preparing"
    assert gate["reason"] == "job_in_flight"
    result = asyncio.run(tv.maybe_run_play_inline(
        gate, _regate_factory(db, item, _user()),
        session_factory=db_factory, drive_api_factory=lambda: None))
    assert result["verdict"] == "preparing"
    assert calls == []
    assert tv.claim_and_execute_inline(
        job.id, session_factory=db_factory,
        drive_api_factory=lambda: None) == "not_claimed"
    assert calls == []


# ---------- 开关关 / 名额满 ----------

def test_switch_off_is_pure_queue_behavior(db, db_factory, monkeypatch):
    _enable(monkeypatch)
    _set_config(db, "play_inline_transfer_enabled", "false")
    assert tv.play_inline_enabled(db) is False
    _cfg(monkeypatch, enabled=False)
    _mount(db)
    item = _item(db)
    _binding(db, 7)
    calls = []
    monkeypatch.setattr(tv, "run_transfer_job_once", _done_executor(calls))
    gate = tv.ensure_user_copy_gate(db, item, _user())
    assert gate["verdict"] == "preparing"
    result = asyncio.run(tv.maybe_run_play_inline(
        gate, _regate_factory(db, item, _user()),
        session_factory=db_factory, drive_api_factory=lambda: None))
    assert result == gate
    assert calls == []
    assert _job_state(db, gate["job_id"])[0] == "pending"


def test_no_slot_returns_preparing_immediately(db, db_factory, monkeypatch):
    _enable(monkeypatch)
    _cfg(monkeypatch, budget_sec=10, concurrency=1)
    _mount(db)
    item = _item(db)
    _binding(db, 7)
    calls = []
    monkeypatch.setattr(tv, "run_transfer_job_once", _done_executor(calls))
    gate = tv.ensure_user_copy_gate(db, item, _user())
    assert gate["verdict"] == "preparing"
    assert tv._inline_try_acquire(1) is True  # 人为占满唯一名额
    try:
        started = time.monotonic()
        result = asyncio.run(tv.maybe_run_play_inline(
            gate, _regate_factory(db, item, _user()),
            session_factory=db_factory, drive_api_factory=lambda: None))
        elapsed = time.monotonic() - started
    finally:
        tv._inline_release()
    assert result["verdict"] == "preparing"
    assert elapsed < 5  # 远小于预算：没等名额、没执行
    assert calls == []
    assert _job_state(db, gate["job_id"])[0] == "pending"


# ---------- 不合格单不进当场路径 ----------

def test_ineligible_jobs_never_run_inline(db, db_factory, monkeypatch):
    _enable(monkeypatch)
    _mount(db)
    item = _item(db)
    _binding(db, 7)
    _binding(db, 8, granted_scopes=DRIVE_READONLY_SCOPE)
    _binding(db, 9, granted_scopes=DRIVE_FILE_SCOPE)  # file-only
    calls = []
    monkeypatch.setattr(tv, "run_transfer_job_once", _done_executor(calls))
    cases = {
        "cross": _user_job(db, item.id, 7, dest="pan115:user-7"),
        "prefetch": _user_job(db, item.id, 8, trigger="prefetch"),
        "source_scope": _user_job(db, item.id, 7, owner_scope="source"),
        "file_only": _user_job(db, item.id, 9),
        "no_binding": _user_job(db, item.id, 42),
    }
    for name, job in cases.items():
        outcome = tv.claim_and_execute_inline(
            job.id, session_factory=db_factory,
            drive_api_factory=lambda: None)
        assert outcome == "ineligible", f"{name}: {outcome}"
        assert _job_state(db, job.id)[0] == "pending", name
    assert calls == []


def test_non_preparing_gate_untouched(db, monkeypatch):
    """非 preparing verdict 与无 job_id 的 preparing 都原样返回。"""
    _cfg(monkeypatch)
    proceed = {"verdict": "proceed", "reason": "has_ready_variant"}
    assert asyncio.run(tv.maybe_run_play_inline(proceed, None)) is proceed
    no_job = {"verdict": "preparing", "reason": "throttled"}
    assert asyncio.run(tv.maybe_run_play_inline(no_job, None)) is no_job
