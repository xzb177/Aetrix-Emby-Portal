# -*- coding: utf-8 -*-
"""转存等待压缩 期一＋期二：探针道（2026-11-01 方案
reports/transfer-wait-compression-design-20261101.md）。

病灶（方案 §0/§1）：秒传试探没有独立调度身份——它只是跨单被
认领后执行器的第一步，而认领被 cross_cap 全局卡死，点播当场
先办又明文排除跨单。指纹已登记的单，本体秒级，却要在搬字节
队列后面干等。

本文件钉住改后口径：
- 期一（纯调度）：_pick_candidates 里可探针单（条目有可信 sha1
  或清单 sha1）优先占用跨槽、跨槽为它保留至少 1 槽；建单日志
  带 sha1_present；claimed_at 在认领时落值。
- 期二（探针道）：probe_only 执行只试 initupload——命中落变体
  done（与现行命中收尾一致）；未命中/探针失败以 probe_miss
  释放回 pending：不烧 attempts、不写 error_kind 终态、不置
  failed；探针认领不占跨槽（跨槽满时仍能跑）、不计入
  _running_cross_count；探针并发键 transfer_probe_lane_concurrency
  默认 4、四齐（schema 注册＋payload/write 往返＋端点透传）；
  点播当场探针在 inline 跨单排除处开仅探针例外，命中同请求
  放行、未命中照旧回 preparing。
- 不可探针单（无可信 sha1）行为逐字不变：探针道不碰它，普通
  认领照旧。

全部离线：SQLite :memory:，115/SA 一律桩替身。
"""
import os

os.environ.setdefault("DATABASE_TYPE", "sqlite")
os.environ.setdefault("REDIS_ENABLED", "false")

import asyncio
import hashlib
import logging
from datetime import datetime, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend import models as base_models
from backend.emby_server import models as em
from backend.emby_server import transfer_variant as tv
from backend.emby_server import transfer115
from backend.emby_server import user_bindings as ub
from backend.integrations import store

_TV_LOGGER = "backend.emby_server.transfer_variant"

_PAYLOAD = bytes(range(256)) * 4  # 1024 字节
_PAYLOAD_SHA1 = hashlib.sha1(_PAYLOAD).hexdigest()


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
def _clean_state():
    store.invalidate()
    tv._paused_until.clear()
    tv._WAKE_EVENT.clear()
    yield
    store.invalidate()
    tv._paused_until.clear()
    tv._WAKE_EVENT.clear()


def _set_config(db, key, value):
    row = (db.query(base_models.SystemConfig)
           .filter(base_models.SystemConfig.key == key).first())
    if row is None:
        db.add(base_models.SystemConfig(key=key, value=str(value)))
    else:
        row.value = str(value)
    db.commit()
    store.invalidate()


def _item(db, sha1=_PAYLOAD_SHA1, **kw):
    row = em.MediaItem(guid=f"probe-{kw.pop('tag', id(object()))}",
                       library_id=1, item_type="movie", name="探针测试片",
                       drive_file_id="origfid",
                       file_path="mount://3/电影/probe.mkv",
                       size=len(_PAYLOAD))
    if sha1:
        row.content_hash = sha1
        row.content_hash_algo = "sha1"
    for key, value in kw.items():
        setattr(row, key, value)
    db.add(row)
    db.commit()
    return row


def _binding(db, user_id, provider="pan115", status="active"):
    cred = em.MountCredential(scope="user", owner_user_id=user_id,
                              status="active")
    db.add(cred)
    db.commit()
    row = em.UserStorageBinding(user_id=user_id, provider=provider,
                                account_label="test",
                                credential_id=cred.id, status=status)
    db.add(row)
    db.commit()
    return row


def _cross_job(db, item_id, user_id, **kw):
    kw.setdefault("status", "pending")
    row = em.TransferJob(item_id=item_id, trigger="playback",
                         drive_id="shared1", provider="drive",
                         owner_scope="user", owner_ref=str(user_id),
                         dest_drive_id=f"pan115:user-{user_id}", **kw)
    db.add(row)
    db.commit()
    return row


class _FakeSA:
    def __init__(self, payload=_PAYLOAD):
        self.payload = payload
        self.download_calls = 0

    def get_file(self, drive_id, fid):
        return {"id": fid, "size": len(self.payload)}

    def download_range(self, drive_id, fid, start, end):
        self.download_calls += 1
        return self.payload[start:end + 1]


class _FakeUser115:
    """rapid 行为可配：hit / miss / error（Pan115Error）/
    auth_error（Pan115AuthError）。"""

    def __init__(self, rapid="hit"):
        self.dirs = {
            "0": [{"name": "aetrix-transfer", "is_dir": True, "cid": "900"}],
            "900": [],
        }
        self.rapid_behavior = rapid
        self.rapid_calls = []
        self.simple_calls = []

    def list_dir(self, cid="0", limit=200):
        return list(self.dirs.get(str(cid), []))

    def mkdir(self, parent_cid, name):
        self.dirs.setdefault("0", []).append(
            {"name": name, "is_dir": True, "cid": "900"})
        return {}

    def rapid_upload(self, filename, filesize, file_sha1, pid,
                     range_reader=None):
        self.rapid_calls.append((filename, filesize, file_sha1, pid))
        if self.rapid_behavior == "error":
            raise transfer115.Pan115Error("协议炸了")
        if self.rapid_behavior == "auth_error":
            raise transfer115.Pan115AuthError("Cookie 已失效")
        if self.rapid_behavior == "miss":
            return {"outcome": "miss", "fid": "", "pickcode": "", "raw": {}}
        self.dirs["900"] = [{"name": filename, "is_dir": False,
                             "fid": "rapidfid", "pickcode": "pcrapid",
                             "size": filesize, "sha1": file_sha1}]
        return {"outcome": "hit", "fid": "rapidfid", "pickcode": "pcrapid",
                "raw": {"status": 2}}

    def simple_upload(self, fileobj, filename, pid):
        data = fileobj.read()
        self.simple_calls.append(data)
        sha1 = hashlib.sha1(data).hexdigest()
        self.dirs["900"] = [{"name": filename, "is_dir": False,
                             "fid": "upfid", "pickcode": "pcup",
                             "size": len(data), "sha1": sha1}]
        return {"fid": "upfid", "pickcode": "pcup", "sha1": sha1,
                "raw": {"data": {"file_id": "upfid", "sha1": sha1}}}


def _patch_user115(monkeypatch, fake):
    monkeypatch.setattr(
        ub, "binding_115_cookie",
        lambda db_arg, user_id, binding_id: "CK-1")
    monkeypatch.setattr(tv, "_make_pan115_client", lambda cookie: fake)


def _enable(monkeypatch):
    monkeypatch.setenv("TRANSFER_VARIANT_ENABLED", "1")


# ---------- 配置键：默认/钳制/schema/payload 往返（四齐） ----------

def test_probe_lane_concurrency_default_and_clamp(db):
    assert tv.probe_lane_concurrency(db) == 4
    _set_config(db, "transfer_probe_lane_concurrency", 7)
    assert tv.probe_lane_concurrency(db) == 7
    _set_config(db, "transfer_probe_lane_concurrency", 99)
    assert tv.probe_lane_concurrency(db) == 16
    _set_config(db, "transfer_probe_lane_concurrency", "junk")
    assert tv.probe_lane_concurrency(db) == 4


def test_probe_lane_concurrency_schema_registered():
    from backend.config_schema import TRANSFER_CONFIG_SCHEMA
    assert TRANSFER_CONFIG_SCHEMA["transfer_probe_lane_concurrency"] == (
        "int", 1, 16)
    assert TRANSFER_CONFIG_SCHEMA["transfer_probe_enabled"] == ("bool",)
    assert TRANSFER_CONFIG_SCHEMA["transfer_probe_priority_enabled"] == (
        "bool",)


def test_probe_lane_concurrency_payload_write_roundtrip(db):
    payload = tv.transfer_config_payload(db)
    assert payload["transfer_probe_lane_concurrency"] == 4
    out = tv.write_transfer_config(db, transfer_probe_lane_concurrency=6)
    assert out["transfer_probe_lane_concurrency"] == 6
    assert tv.probe_lane_concurrency(db) == 6
    row = (db.query(base_models.SystemConfig)
           .filter_by(key="transfer_probe_lane_concurrency").one())
    assert row.value == "6"
    with pytest.raises(ValueError):
        tv.write_transfer_config(db, transfer_probe_lane_concurrency=99)
    assert tv.probe_lane_concurrency(db) == 6


# ---------- 期一：可探针优先 + 预留槽 + claimed_at + 建单日志 ----------

def test_pick_candidates_probeable_priority_and_reserve(db):
    """跨槽 cap=2：两个不可探针跨单 + 一个可探针跨单。
    不可探针单最多用 cap-1=1 槽（预留 1），可探针单必被挑中。"""
    plain_a = _cross_job(db, 101, 11)
    plain_b = _cross_job(db, 102, 12)
    probe_c = _cross_job(db, 103, 13)
    picked = tv._pick_candidates(
        [plain_a, plain_b, probe_c], 10, {}, 10,
        cross_cap=2, cross_running=0,
        probeable_ids={id(probe_c)})
    picked_ids = {j.id for j in picked}
    assert probe_c.id in picked_ids
    plain_picked = [j for j in picked if j.id in (plain_a.id, plain_b.id)]
    assert len(plain_picked) == 1  # 预留槽：不可探针只占 1 个


def test_pick_candidates_without_probeable_ids_unchanged(db):
    a = _cross_job(db, 101, 11)
    b = _cross_job(db, 102, 12)
    c = _cross_job(db, 103, 13)
    picked = tv._pick_candidates([a, b, c], 10, {}, 10,
                                 cross_cap=2, cross_running=0)
    assert len(picked) == 2  # 无探针分层时仍是先到先得占满 2 槽


def test_pick_candidates_same_owner_probeable_first(db):
    """同 owner 两单：不可探针（id 小）在前、可探针在后——
    分层后可探针单在同 owner 内先被挑。"""
    plain = _cross_job(db, 101, 11)
    probe = _cross_job(db, 102, 11)
    picked = tv._pick_candidates(
        [plain, probe], 1, {}, 10, cross_cap=3, cross_running=0,
        probeable_ids={id(probe)})
    assert [j.id for j in picked] == [probe.id]


def test_claim_due_jobs_sets_claimed_at(db, monkeypatch):
    _enable(monkeypatch)
    item = _item(db)
    _binding(db, 7)
    job = _cross_job(db, item.id, 7)
    claimed = tv.claim_due_jobs(db)
    assert claimed == [job.id]
    db.refresh(job)
    assert job.status == "running"
    assert job.claimed_at is not None


def test_create_cross_job_logs_sha1_present(db, monkeypatch, caplog):
    _enable(monkeypatch)
    # 源盘标识推导不在本用例范围（挂载行口径另有专测），桩掉只
    # 钉建单日志的指纹状态位。
    monkeypatch.setattr(tv, "_source_drive_id",
                        lambda db_arg, item_arg, provider: "shared1")
    item = _item(db)
    _binding(db, 7)
    with caplog.at_level(logging.INFO, logger=_TV_LOGGER):
        job = tv.create_cross_provider_job(db, item, 7, "drive")
    assert "sha1_present=1" in caplog.text
    assert f"job={job.id}" in caplog.text


def test_create_cross_job_logs_sha1_absent(db, monkeypatch, caplog):
    _enable(monkeypatch)
    monkeypatch.setattr(tv, "_source_drive_id",
                        lambda db_arg, item_arg, provider: "shared1")
    item = _item(db, sha1=None)
    _binding(db, 7)
    with caplog.at_level(logging.INFO, logger=_TV_LOGGER):
        tv.create_cross_provider_job(db, item, 7, "drive")
    assert "sha1_present=0" in caplog.text


# ---------- 期二：探针认领不占跨槽 ----------

def test_probe_claim_works_when_cross_lane_full(db, monkeypatch):
    """跨槽被一条 running 跨单占满（cap=1）：普通认领领不到
    pending 跨单；探针认领照样领走可探针单，且不计入跨槽计数。"""
    _enable(monkeypatch)
    _set_config(db, "transfer_cross_lane_concurrency", 1)
    item = _item(db)
    _binding(db, 7)
    _binding(db, 8)
    running = _cross_job(
        db, item.id, 8, status="running",
        lease_expires_at=datetime.now() + timedelta(seconds=300))
    pending = _cross_job(db, item.id, 7)
    assert tv._running_cross_count(db) == 1
    assert tv.claim_due_jobs(db) == []  # 跨槽满，普通认领无份
    claimed = tv.claim_probe_jobs(db)
    assert claimed == [pending.id]
    db.refresh(pending)
    assert pending.status == "running"
    assert (pending.claimed_by or "").startswith("probe:")
    assert pending.claimed_at is not None
    assert pending.last_probe_at is not None
    # 探针单不计入跨槽占用：计数仍只认普通 running 跨单口径外——
    # 探针 claimed_by 带 probe: 标记，_running_cross_count 排除它。
    assert tv._running_cross_count(db) == 1
    db.refresh(running)
    assert running.status == "running"


def test_probe_claim_respects_lane_concurrency_key(db, monkeypatch):
    _enable(monkeypatch)
    _set_config(db, "transfer_probe_lane_concurrency", 1)
    item = _item(db)
    jobs = []
    for uid in (7, 8, 9):
        _binding(db, uid)
        jobs.append(_cross_job(db, item.id, uid))
    claimed = tv.claim_probe_jobs(db)
    assert len(claimed) == 1


def test_probe_claim_skips_non_probeable(db, monkeypatch):
    """无可信 sha1 的跨单：探针道不碰；普通认领行为不变。"""
    _enable(monkeypatch)
    item = _item(db, sha1=None)
    _binding(db, 7)
    job = _cross_job(db, item.id, 7)
    assert tv.claim_probe_jobs(db) == []
    db.refresh(job)
    assert job.status == "pending"
    claimed = tv.claim_due_jobs(db)
    assert claimed == [job.id]


def test_probe_claim_throttled_by_last_probe_at(db, monkeypatch):
    _enable(monkeypatch)
    item = _item(db)
    _binding(db, 7)
    job = _cross_job(db, item.id, 7, last_probe_at=datetime.now())
    assert tv.claim_probe_jobs(db) == []
    db.refresh(job)
    assert job.status == "pending"


def test_probe_disabled_switch_stops_lane(db, monkeypatch):
    _enable(monkeypatch)
    _set_config(db, "transfer_probe_enabled", "false")
    item = _item(db)
    _binding(db, 7)
    _cross_job(db, item.id, 7)
    assert tv.claim_probe_jobs(db) == []


# ---------- 期二：probe_only 执行语义 ----------

def test_probe_hit_done_and_variant_ready(db, monkeypatch, db_factory):
    _enable(monkeypatch)
    item = _item(db)
    _binding(db, 7)
    job = _cross_job(db, item.id, 7)
    fake = _FakeUser115(rapid="hit")
    _patch_user115(monkeypatch, fake)
    sa = _FakeSA()
    stats = tv.process_probe_round(db_factory, lambda: sa)
    assert stats["hit"] == 1
    db.refresh(job)
    assert job.status == "done"
    assert len(fake.rapid_calls) == 1
    assert fake.simple_calls == []  # 命中零字节搬运
    assert sa.download_calls == 0
    rows = (db.query(em.TransferVariant)
            .filter(em.TransferVariant.item_id == item.id).all())
    assert len(rows) == 1
    assert rows[0].status == "ready"
    assert rows[0].owner_ref == "7"


def test_probe_miss_releases_without_burning_attempts(db, monkeypatch,
                                                     db_factory):
    _enable(monkeypatch)
    item = _item(db)
    _binding(db, 7)
    job = _cross_job(db, item.id, 7)
    fake = _FakeUser115(rapid="miss")
    _patch_user115(monkeypatch, fake)
    stats = tv.process_probe_round(db_factory, lambda: _FakeSA())
    assert stats["miss"] == 1
    db.refresh(job)
    assert job.status == "pending"      # 释放回队列等正常搬运认领
    assert job.attempts == 0            # 不烧 attempts
    assert job.error_kind is None       # 不写终态
    assert job.lease_expires_at is None
    assert job.claimed_by is None
    assert job.last_probe_at is not None
    assert fake.simple_calls == []      # 探针绝不搬字节


def test_probe_protocol_error_releases_and_logs(db, monkeypatch,
                                                db_factory, caplog):
    _enable(monkeypatch)
    item = _item(db)
    _binding(db, 7)
    job = _cross_job(db, item.id, 7)
    fake = _FakeUser115(rapid="error")
    _patch_user115(monkeypatch, fake)
    with caplog.at_level(logging.WARNING, logger=_TV_LOGGER):
        stats = tv.process_probe_round(db_factory, lambda: _FakeSA())
    assert stats["miss"] == 1
    db.refresh(job)
    assert job.status == "pending"
    assert job.attempts == 0
    assert "transfer_probe outcome=error" in caplog.text


def test_probe_auth_error_releases_not_terminal(db, monkeypatch,
                                                db_factory, caplog):
    """探针时用户 Cookie 被 115 拒：响亮记日志 + 按未命中释放，
    不在探针态判死——终态由普通执行路同口径处理。"""
    _enable(monkeypatch)
    item = _item(db)
    _binding(db, 7)
    job = _cross_job(db, item.id, 7)
    fake = _FakeUser115(rapid="auth_error")
    _patch_user115(monkeypatch, fake)
    with caplog.at_level(logging.WARNING, logger=_TV_LOGGER):
        stats = tv.process_probe_round(db_factory, lambda: _FakeSA())
    assert stats["miss"] == 1
    db.refresh(job)
    assert job.status == "pending"
    assert job.attempts == 0
    assert job.error_kind is None
    assert "transfer_probe outcome=error" in caplog.text


# ---------- 期二：点播当场探针 ----------

def test_inline_probe_hit_releases_gate_same_request(db, monkeypatch,
                                                     db_factory):
    _enable(monkeypatch)
    item = _item(db)
    _binding(db, 7)
    job = _cross_job(db, item.id, 7)
    fake = _FakeUser115(rapid="hit")
    _patch_user115(monkeypatch, fake)
    initial = {"verdict": "preparing", "job_id": job.id}

    async def _gate():
        return {"verdict": "proceed"}

    result = asyncio.run(tv.maybe_run_play_inline(
        initial, _gate, session_factory=db_factory,
        drive_api_factory=lambda: _FakeSA()))
    assert result["verdict"] == "proceed"  # 命中同请求放行
    db.refresh(job)
    assert job.status == "done"
    assert fake.simple_calls == []


def test_inline_probe_miss_falls_back_to_preparing(db, monkeypatch,
                                                   db_factory):
    _enable(monkeypatch)
    item = _item(db)
    _binding(db, 7)
    job = _cross_job(db, item.id, 7)
    fake = _FakeUser115(rapid="miss")
    _patch_user115(monkeypatch, fake)
    initial = {"verdict": "preparing", "job_id": job.id}

    async def _gate():
        return {"verdict": "proceed"}

    result = asyncio.run(tv.maybe_run_play_inline(
        initial, _gate, session_factory=db_factory,
        drive_api_factory=lambda: _FakeSA()))
    assert result == initial  # 未命中照旧 preparing，工人兜底
    db.refresh(job)
    assert job.status == "pending"
    assert job.attempts == 0


def test_inline_probe_skips_non_probeable_cross_job(db, monkeypatch,
                                                    db_factory):
    """跨单但无指纹：当场探针不适用，行为与现行逐字一致
    （跨单不当场执行、回 preparing）。"""
    _enable(monkeypatch)
    item = _item(db, sha1=None)
    _binding(db, 7)
    job = _cross_job(db, item.id, 7)
    fake = _FakeUser115(rapid="hit")
    _patch_user115(monkeypatch, fake)
    initial = {"verdict": "preparing", "job_id": job.id}

    async def _gate():
        return {"verdict": "proceed"}

    result = asyncio.run(tv.maybe_run_play_inline(
        initial, _gate, session_factory=db_factory,
        drive_api_factory=lambda: _FakeSA()))
    assert result == initial
    assert fake.rapid_calls == []
    db.refresh(job)
    assert job.status == "pending"
