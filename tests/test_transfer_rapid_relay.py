# -*- coding: utf-8 -*-
"""秒传接力（2026-10-30 用户亲点「黑科技方案」）：同片同目标
provider 只搬一次字节，后来者哈希命中秒成。

口径：跨 provider Drive→115 时，第一位用户的 job 全量搬运完成
后把整文件 sha1 沉淀进条目（#1526 底子）；第二位用户的 job 在
被认领执行时走执行侧接力预检——先查条目可信 sha1，用本人
Cookie 试 initupload 认领：命中 → 只在本人盘登记本人变体行、
零字节搬运、job 直接 done（结构化日志 transfer_relay
outcome=hit）；未命中/无哈希/协议异常 → 原样进入现行字节搬运；
命中但目标目录核不到 → 明确失败（copy_unresolved），绝不明示
成功。他人 ready 变体行绝不共用、绝不改动（副本按用户归属）。

预检位置定在执行侧（run_transfer_job_once → 跨执行器），不在
建单时：排队 job 建单时条目可能尚无哈希，在途 job 完成沉淀后，
必须等它自己被认领执行时再查才看得到——本文件的接力序列测试
正是按这个时序建的（B 的 job 先于 A 完成而建）。

全部离线：115 客户端、SA 下载一律桩替身，不连真实 115。
未实测：跨用户秒传在 115 全局哈希库的真实命中率（上线后靠
transfer_relay outcome 日志观察）。
"""
import os

os.environ.setdefault("DATABASE_TYPE", "sqlite")
os.environ.setdefault("REDIS_ENABLED", "false")

import hashlib
import logging

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
from backend.emby_server import stream_limits as sl


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
    sl.clear_table_policy_cache()
    store.invalidate()
    yield session
    session.close()
    sl.clear_table_policy_cache()
    store.invalidate()


_counter = [1000]
_PAYLOAD = bytes(range(256)) * 4  # 1024 字节
_PAYLOAD_SHA1 = hashlib.sha1(_PAYLOAD).hexdigest()
_TV_LOGGER = "backend.emby_server.transfer_variant"


def _item(db, **kw):
    _counter[0] += 1
    row = em.MediaItem(guid=f"relay-{_counter[0]}", library_id=1,
                       item_type="movie", name="接力测试片",
                       drive_file_id="origfid",
                       file_path="mount://3/电影/relay.mkv",
                       size=len(_PAYLOAD))
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


def _cross_job(db, item_id, user_id):
    row = em.TransferJob(item_id=item_id, trigger="playback",
                         drive_id="shared1", provider="drive",
                         owner_scope="user", owner_ref=str(user_id),
                         dest_drive_id=f"pan115:user-{user_id}",
                         status="pending")
    db.add(row)
    db.commit()
    return row


def _enable(monkeypatch):
    monkeypatch.setenv("TRANSFER_VARIANT_ENABLED", "1")


class _FakeSA:
    """SA 桩：get_file 报大小，download_range 按段切字节并计数。"""

    def __init__(self, payload=_PAYLOAD):
        self.payload = payload
        self.download_calls = 0

    def get_file(self, drive_id, fid):
        return {"id": fid, "size": len(self.payload)}

    def download_range(self, drive_id, fid, start, end):
        self.download_calls += 1
        return self.payload[start:end + 1]


class _FakeUser115:
    """用户侧 115 桩：转存目录预建；rapid 行为可配。

    rapid 行为：hit=回执命中且文件真落目录；ghost=回执命中但目录
    里没有文件（命中后核对失败场景）；miss=未命中；error=协议异常。
    """

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

    def _place(self, filename, fid, pickcode, size, sha1):
        self.dirs["900"] = [{"name": filename, "is_dir": False, "fid": fid,
                             "pickcode": pickcode, "size": size,
                             "sha1": sha1}]

    def rapid_upload(self, filename, filesize, file_sha1, pid,
                     range_reader=None):
        self.rapid_calls.append((filename, filesize, file_sha1, pid))
        if self.rapid_behavior == "error":
            raise transfer115.Pan115Error("协议炸了")
        if self.rapid_behavior == "miss":
            return {"outcome": "miss", "fid": "", "pickcode": "", "raw": {}}
        if self.rapid_behavior == "ghost":
            # 回执说命中，但文件没真落进目标目录（核对必须判死）。
            return {"outcome": "hit", "fid": "ghostfid",
                    "pickcode": "pcghost", "raw": {"status": 2}}
        self._place(filename, "rapidfid", "pcrapid", filesize, file_sha1)
        return {"outcome": "hit", "fid": "rapidfid", "pickcode": "pcrapid",
                "raw": {"status": 2}}

    def simple_upload(self, fileobj, filename, pid):
        data = fileobj.read()
        self.simple_calls.append(data)
        sha1 = hashlib.sha1(data).hexdigest()
        self._place(filename, "upfid", "pcup", len(data), sha1)
        return {"fid": "upfid", "pickcode": "pcup", "sha1": sha1,
                "raw": {"data": {"file_id": "upfid", "sha1": sha1}}}


def _patch_users(monkeypatch, fakes):
    """按用户分发 115 桩：Cookie 按用户区分，客户端按 Cookie 路由。"""
    monkeypatch.setattr(
        ub, "binding_115_cookie",
        lambda db_arg, user_id, binding_id: f"CK-{user_id}")
    monkeypatch.setattr(
        tv, "_make_pan115_client",
        lambda cookie: fakes[int(str(cookie).split("-")[1])])


def _variants(db, item_id):
    return (db.query(em.TransferVariant)
            .filter(em.TransferVariant.item_id == item_id)
            .order_by(em.TransferVariant.id)
            .all())


# ---------- 接力主场景：前人搬字节沉淀，后人认领时秒成 ----------

def test_relay_second_user_hits_after_first_upload(db, monkeypatch, caplog):
    _enable(monkeypatch)
    item = _item(db)  # 建单时条目无任何哈希
    _binding(db, 7)
    _binding(db, 8)
    # B（用户 8）的 job 先建：建单时条目尚无哈希——预检若放在
    # 建单时必然错过；执行侧预检等它被认领时再查才看得到沉淀。
    job_b = _cross_job(db, item.id, 8)
    job_a = _cross_job(db, item.id, 7)
    fake_a = _FakeUser115(rapid="hit")
    fake_b = _FakeUser115(rapid="hit")
    _patch_users(monkeypatch, {7: fake_a, 8: fake_b})
    # A 先执行：无存量哈希 → 不试秒传、全量搬字节、沉淀 sha1。
    res_a = tv.run_transfer_job_once(db, job_a, _FakeSA())
    assert res_a["outcome"] == "done"
    assert fake_a.rapid_calls == []
    assert fake_a.simple_calls == [_PAYLOAD]
    db.refresh(item)
    assert item.content_hash_algo == "sha1"
    assert item.content_hash == _PAYLOAD_SHA1
    # B 后执行（被认领时）：接力预检命中，本人 Cookie 认领，
    # 零字节搬运，登记的是 B 本人的变体行。
    sa_b = _FakeSA()
    with caplog.at_level(logging.INFO, logger=_TV_LOGGER):
        res_b = tv.run_transfer_job_once(db, job_b, sa_b)
    assert res_b["outcome"] == "done"
    assert len(fake_b.rapid_calls) == 1
    assert fake_b.simple_calls == []
    assert sa_b.download_calls == 0
    rows = _variants(db, item.id)
    assert len(rows) == 2
    row_a, row_b = rows
    assert row_a.owner_ref == "7" and row_a.variant_file_id == "upfid"
    assert row_a.status == "ready"
    assert row_b.owner_ref == "8" and row_b.variant_file_id == "rapidfid"
    assert row_b.status == "ready"
    assert row_b.variant_drive_id == "pan115:user-8"
    assert "transfer_relay outcome=hit" in caplog.text
    assert f"item={item.id}" in caplog.text
    assert f"job={job_b.id}" in caplog.text


# ---------- 他人 ready 变体行绝不共用/改动 ----------

def test_relay_registers_own_variant_not_others(db, monkeypatch):
    _enable(monkeypatch)
    item = _item(db, content_hash=_PAYLOAD_SHA1, content_hash_algo="sha1")
    other = em.TransferVariant(
        item_id=item.id, source_file_id="origfid",
        variant_file_id="otherfid", variant_drive_id="pan115:user-9",
        owner_scope="user", owner_ref="9", status="ready",
        pickcode="pcother", size_bytes=len(_PAYLOAD))
    db.add(other)
    db.commit()
    _binding(db, 7)
    job = _cross_job(db, item.id, 7)
    fake = _FakeUser115(rapid="hit")
    _patch_users(monkeypatch, {7: fake})
    res = tv.run_transfer_job_once(db, job, _FakeSA())
    assert res["outcome"] == "done"
    rows = _variants(db, item.id)
    assert len(rows) == 2
    mine = [r for r in rows if r.owner_ref == "7"]
    theirs = [r for r in rows if r.owner_ref == "9"]
    assert len(mine) == 1 and mine[0].variant_file_id == "rapidfid"
    assert mine[0].status == "ready"
    assert len(theirs) == 1 and theirs[0].variant_file_id == "otherfid"
    assert theirs[0].status == "ready"  # 他人行原样未动


# ---------- 命中后目录核对失败：明确判死，不明示成功 ----------

def test_relay_hit_but_dir_missing_fails(db, monkeypatch, caplog):
    _enable(monkeypatch)
    item = _item(db, content_hash=_PAYLOAD_SHA1, content_hash_algo="sha1")
    _binding(db, 7)
    job = _cross_job(db, item.id, 7)
    fake = _FakeUser115(rapid="ghost")
    _patch_users(monkeypatch, {7: fake})
    with caplog.at_level(logging.INFO, logger=_TV_LOGGER):
        res = tv.run_transfer_job_once(db, job, _FakeSA())
    assert res["outcome"] == "failed"
    assert res["error_kind"] == "copy_unresolved"
    assert _variants(db, item.id) == []  # 没有登记任何 ready 行
    db.refresh(job)
    assert job.status == "failed"
    # 接力日志记了回执命中，但 job 终态是失败——核对不过不算成。
    assert "transfer_relay outcome=hit" in caplog.text


# ---------- 协议异常：回落全量搬运不判死 + 接力日志 ----------

def test_relay_error_falls_back_and_logs(db, monkeypatch, caplog):
    _enable(monkeypatch)
    item = _item(db, content_hash=_PAYLOAD_SHA1, content_hash_algo="sha1")
    _binding(db, 7)
    job = _cross_job(db, item.id, 7)
    fake = _FakeUser115(rapid="error")
    _patch_users(monkeypatch, {7: fake})
    with caplog.at_level(logging.INFO, logger=_TV_LOGGER):
        res = tv.run_transfer_job_once(db, job, _FakeSA())
    assert res["outcome"] == "done"
    assert len(fake.rapid_calls) == 1
    assert fake.simple_calls == [_PAYLOAD]
    assert "transfer_relay outcome=error" in caplog.text


# ---------- 未命中：照常搬运 + 接力日志 ----------

def test_relay_miss_falls_back_and_logs(db, monkeypatch, caplog):
    _enable(monkeypatch)
    item = _item(db, content_hash=_PAYLOAD_SHA1, content_hash_algo="sha1")
    _binding(db, 7)
    job = _cross_job(db, item.id, 7)
    fake = _FakeUser115(rapid="miss")
    _patch_users(monkeypatch, {7: fake})
    with caplog.at_level(logging.INFO, logger=_TV_LOGGER):
        res = tv.run_transfer_job_once(db, job, _FakeSA())
    assert res["outcome"] == "done"
    assert len(fake.rapid_calls) == 1
    assert fake.simple_calls == [_PAYLOAD]
    assert "transfer_relay outcome=miss" in caplog.text


# ---------- 无可信哈希：预检跳过、照常搬运 + 接力日志 ----------

def test_relay_skip_logged_without_hash(db, monkeypatch, caplog):
    _enable(monkeypatch)
    item = _item(db)
    _binding(db, 7)
    job = _cross_job(db, item.id, 7)
    fake = _FakeUser115(rapid="hit")
    _patch_users(monkeypatch, {7: fake})
    with caplog.at_level(logging.INFO, logger=_TV_LOGGER):
        res = tv.run_transfer_job_once(db, job, _FakeSA())
    assert res["outcome"] == "done"
    assert fake.rapid_calls == []  # 无哈希不试、不为算哈希多下整遍
    assert fake.simple_calls == [_PAYLOAD]
    assert "transfer_relay outcome=skip" in caplog.text
