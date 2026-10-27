# -*- coding: utf-8 -*-
"""极限优化（2026-10-27）：目录缓存、并行核对+加权限、存在性正缓存
预填、详情页预转存、准备中态等流。

全部离线：SQLite :memory: + 桩客户端 / 桩 Redis；不碰真实 Drive。
"""
import asyncio
import os
from datetime import datetime
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend import models as base_models
from backend.emby_server import models as em
from backend.emby_server import transfer_variant as tv

os.environ.setdefault("DATABASE_TYPE", "sqlite")
os.environ.setdefault("REDIS_ENABLED", "false")


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
def _reset_cache(monkeypatch):
    tv.clear_transfer_folder_cache()
    monkeypatch.setenv("TRANSFER_VARIANT_ENABLED", "1")
    yield
    tv.clear_transfer_folder_cache()


_counter = [0]


def _item(db, **kw):
    _counter[0] += 1
    row = em.MediaItem(guid=f"ex-{_counter[0]}", library_id=1,
                       item_type="movie", name="测试片",
                       drive_file_id="origfid",
                       file_path="mount://3/电影/a.mkv", size=1000)
    for key, value in kw.items():
        setattr(row, key, value)
    db.add(row)
    db.commit()
    return row


def _binding(db, user_id, provider="drive"):
    cred = em.MountCredential(scope="user", owner_user_id=user_id,
                              status="active")
    db.add(cred)
    db.commit()
    row = em.UserStorageBinding(user_id=user_id, provider=provider,
                                account_label="test",
                                credential_id=cred.id, status="active")
    db.add(row)
    db.commit()
    return row


def _mount(db, mount_id=3, mount_type="drive", path="drive:shared1"):
    row = em.StorageMount(id=mount_id, name=f"m{mount_id}",
                          mount_type=mount_type, path=path)
    db.add(row)
    db.commit()
    return row


def _user(user_id=7):
    return SimpleNamespace(id=user_id)


# ---------- 目录缓存 ----------

def test_folder_cache_second_call_no_second_list(monkeypatch):
    """同一绑定同一目录：第二次 ensure_folder 不再调父类列目录。"""
    calls = []

    def _fake_parent(self, drive_id, dir_name):
        calls.append((drive_id, dir_name))
        return "folder-1"

    monkeypatch.setattr(tv.DriveApiClient, "ensure_folder", _fake_parent)
    binding = SimpleNamespace(id=9, last_verified_at=datetime(2026, 10, 27, 1, 0, 0))
    client = tv.UserDriveApiClient.__new__(tv.UserDriveApiClient)
    client._db = None
    client._binding = binding
    client._token = None
    assert client.ensure_folder("myDrive:7", "aetrix-transfer") == "folder-1"
    assert client.ensure_folder("myDrive:7", "aetrix-transfer") == "folder-1"
    assert len(calls) == 1


def test_folder_cache_rebind_fingerprint_miss(monkeypatch):
    """重绑（last_verified_at 变了）后缓存键失效，必须重新列目录。"""
    calls = []

    def _fake_parent(self, drive_id, dir_name):
        calls.append(1)
        return "folder-1"

    monkeypatch.setattr(tv.DriveApiClient, "ensure_folder", _fake_parent)
    binding = SimpleNamespace(id=9, last_verified_at=datetime(2026, 10, 27, 1, 0, 0))
    client = tv.UserDriveApiClient.__new__(tv.UserDriveApiClient)
    client._db = None
    client._binding = binding
    client._token = None
    client.ensure_folder("myDrive:7", "aetrix-transfer")
    binding.last_verified_at = datetime(2026, 10, 27, 2, 0, 0)
    client.ensure_folder("myDrive:7", "aetrix-transfer")
    assert len(calls) == 2


# ---------- 核对 → 加权限顺序（Path A，极限优化时亲验否决并行） ----------

class _FakeUserDrive:
    def __init__(self, dir_ids=None, perm_fail=False):
        self.dir_ids = dir_ids
        self.perm_fail = perm_fail
        self.perm_calls = []

    def ensure_folder(self, drive_id, dir_name):
        return "ufolder"

    def list_file_ids_by_name(self, drive_id, folder_id, name):
        if self.dir_ids is not None:
            return list(self.dir_ids)
        return ["usercopy"]

    def copy_file(self, drive_id, source_fid, folder_id, name):
        return {"id": "usercopy", "size": 1000}

    def add_anyone_reader(self, drive_id, fid):
        self.perm_calls.append(fid)
        if self.perm_fail:
            raise Exception("perm fail")


class _FakeSA:
    def ensure_folder(self, drive_id, dir_name):
        return "tfolder"

    def find_file_by_name(self, drive_id, folder_id, name):
        return None

    def copy_file(self, drive_id, source_fid, folder_id, name):
        return {"id": "tmpfid", "size": 1000}

    def add_anyone_reader(self, drive_id, fid):
        pass

    def delete_file(self, drive_id, fid):
        pass


def _user_job(db, item_id, user_id=7):
    row = em.TransferJob(item_id=item_id, trigger="playback",
                         drive_id="shared1", provider="drive",
                         owner_scope="user", owner_ref=str(user_id),
                         dest_drive_id=f"userdrive:{user_id}",
                         status="pending")
    db.add(row)
    db.commit()
    return row


def test_path_a_parallel_verify_and_perm_both_called(db, monkeypatch):
    """Path A 成功：核对与加权限都跑过、登记 ready（顺序核对→加权限）。"""
    item = _item(db)
    _binding(db, 7)
    job = _user_job(db, item.id)
    fake = _FakeUserDrive()
    monkeypatch.setattr(tv, "UserDriveApiClient",
                        lambda db_arg, binding: fake)
    res = tv.run_transfer_job_once(db, job, _FakeSA())
    assert res["outcome"] == "done"
    assert fake.perm_calls == ["usercopy"]
    row = db.query(em.TransferVariant).filter(
        em.TransferVariant.item_id == item.id).first()
    assert row.status == "ready"


def test_path_a_perm_fail_still_orphan(db, monkeypatch):
    """加权限失败仍记 orphan（并行不改变失败语义）。"""
    item = _item(db)
    _binding(db, 7)
    job = _user_job(db, item.id)
    fake = _FakeUserDrive(perm_fail=True)
    monkeypatch.setattr(tv, "UserDriveApiClient",
                        lambda db_arg, binding: fake)
    res = tv.run_transfer_job_once(db, job, _FakeSA())
    assert res["outcome"] == "failed"
    assert res["error_kind"] == "orphan"


def test_path_a_verify_miss_still_copy_unresolved(db, monkeypatch):
    """核对不到仍记 copy_unresolved，且不补权限（并行否决的证据）。"""
    item = _item(db)
    _binding(db, 7)
    job = _user_job(db, item.id)
    fake = _FakeUserDrive(dir_ids=[])
    monkeypatch.setattr(tv, "UserDriveApiClient",
                        lambda db_arg, binding: fake)
    res = tv.run_transfer_job_once(db, job, _FakeSA())
    assert res["outcome"] == "failed"
    assert res["error_kind"] == "copy_unresolved"
    assert fake.perm_calls == []


# ---------- 存在性正缓存预填 ----------

def test_publish_exists_ok_writes_redis_and_clears_tombstone(monkeypatch):
    """登记 ready 预填：写正缓存键、清墓碑键；Redis 不可用不抛错。"""
    store = {}

    class _FakeRedis:
        def set(self, key, value, ex=None):
            store[key] = value

        def delete(self, key):
            store.pop(key, None)

    fake_rc = _FakeRedis()
    store["aetrix:drive_exists:inv:fid1"] = "1"
    # transfer_variant 用 backend.database.get_redis（懒导入），直接
    # 桩 database 模块的 get_redis。
    import backend.database as _dbmod
    monkeypatch.setattr(_dbmod, "get_redis", lambda: fake_rc)
    assert tv.publish_drive_exists_ok("fid1") is True
    assert store.get("aetrix:drive_exists:ok:fid1") == "1"
    assert "aetrix:drive_exists:inv:fid1" not in store


def test_publish_exists_ok_no_redis_is_false(monkeypatch):
    import backend.database as _dbmod
    monkeypatch.setattr(_dbmod, "get_redis", lambda: None)
    assert tv.publish_drive_exists_ok("fid1") is False


# ---------- 预转存 ----------

def test_prefetch_movie_creates_slow_lane_job(db):
    """详情页电影：建 prefetch 单（慢通道，非 playback）。"""
    _mount(db)
    item = _item(db)
    _binding(db, 7)
    out = tv.maybe_prefetch_transfer(db, item, _user(7))
    assert out["triggered"] is True
    assert out["target_item_id"] == item.id
    job = db.query(em.TransferJob).filter(
        em.TransferJob.item_id == item.id).first()
    assert job is not None and job.trigger == "prefetch"


def test_prefetch_second_call_throttled(db):
    """24h 内已有任务：第二次预转存不重复建单。"""
    _mount(db)
    item = _item(db)
    _binding(db, 7)
    assert tv.maybe_prefetch_transfer(db, item, _user(7))["triggered"] is True
    out2 = tv.maybe_prefetch_transfer(db, item, _user(7))
    assert out2["triggered"] is False and out2["reason"] == "throttled"
    assert db.query(em.TransferJob).count() == 1


def test_prefetch_already_ready_no_job(db):
    """已有 ready 副本：不建单。"""
    _mount(db)
    item = _item(db)
    _binding(db, 7)
    row = em.TransferVariant(item_id=item.id, source_file_id="origfid",
                             variant_file_id="copy1",
                             variant_drive_id="userdrive:7",
                             owner_scope="user", owner_ref="7",
                             status="ready")
    db.add(row)
    db.commit()
    out = tv.maybe_prefetch_transfer(db, item, _user(7))
    assert out["triggered"] is False and out["reason"] == "already_ready"
    assert db.query(em.TransferJob).count() == 0


def test_prefetch_series_targets_first_episode(db):
    """详情页是剧：只转首集（季号/集号排序），不转剧行本身。"""
    _mount(db)
    show = _item(db, item_type="series", drive_file_id="",
                 file_path="mount://3/剧")
    ep2 = _item(db, item_type="episode", series_id=show.id,
                season_number=1, episode_number=2,
                file_path="mount://3/剧/S01E02.mkv")
    ep1 = _item(db, item_type="episode", series_id=show.id,
                season_number=1, episode_number=1,
                file_path="mount://3/剧/S01E01.mkv")
    _binding(db, 7)
    out = tv.maybe_prefetch_transfer(db, show, _user(7))
    assert out["triggered"] is True
    assert out["target_item_id"] == ep1.id
    assert db.query(em.TransferJob).count() == 1
    assert ep2.id != out["target_item_id"]


def test_prefetch_no_binding_no_job(db):
    """无任何绑定：不建单（不越权拦无绑定用户）。"""
    _mount(db)
    item = _item(db)
    out = tv.maybe_prefetch_transfer(db, item, _user(7))
    assert out["triggered"] is False
    assert out["reason"] == "no_matching_binding"
    assert db.query(em.TransferJob).count() == 0


def test_prefetch_cross_provider_follows_bind_which(db):
    """只绑 115、条目是 Drive：建跨单（落盘 pan115），不偷换同盘单。"""
    _mount(db)
    item = _item(db)
    _binding(db, 7, provider="pan115")
    out = tv.maybe_prefetch_transfer(db, item, _user(7))
    assert out["triggered"] is True
    job = db.query(em.TransferJob).first()
    assert job.provider == "drive"
    assert job.dest_drive_id == "pan115:user-7"


def test_prefetch_daily_cap(db, monkeypatch):
    """每天 5 单帽子：第 6 个详情页不再预转。"""
    monkeypatch.setenv("TRANSFER_PREFETCH_DAILY_MAX", "2")
    _mount(db)
    _binding(db, 7)
    for _ in range(2):
        item = _item(db)
        assert tv.maybe_prefetch_transfer(db, item, _user(7))["triggered"] is True
    item3 = _item(db)
    out = tv.maybe_prefetch_transfer(db, item3, _user(7))
    assert out["triggered"] is False and out["reason"] == "daily_cap"


# ---------- 等流 ----------

def test_wait_returns_immediately_when_not_preparing():
    """非 preparing verdict 不等，直接回原 verdict。"""
    gate = {"verdict": "proceed", "reason": "has_ready_variant"}
    out = asyncio.run(tv.wait_for_transfer_ready(
        gate, lambda: asyncio.sleep(0)))
    assert out == gate


def test_wait_throttled_does_not_wait():
    """节流 preparing 不等（等再久也没在途任务）。"""
    gate = {"verdict": "preparing", "reason": "throttled"}
    out = asyncio.run(tv.wait_for_transfer_ready(
        gate, lambda: asyncio.sleep(0), max_sec=5, poll_sec=0.05))
    assert out == gate


def test_wait_polls_until_proceed():
    """preparing → 两次重查后 proceed：等流返回 proceed。"""
    calls = []

    async def _gate():
        calls.append(1)
        if len(calls) < 3:
            return {"verdict": "preparing", "reason": "job_in_flight"}
        return {"verdict": "proceed", "reason": "has_ready_variant"}

    out = asyncio.run(tv.wait_for_transfer_ready(
        {"verdict": "preparing", "reason": "job_created"}, _gate,
        max_sec=5, poll_sec=0.05))
    assert out["verdict"] == "proceed"
    assert len(calls) == 3


def test_wait_timeout_returns_preparing():
    """一直 preparing：超时回 preparing（调用方按旧 409 处理）。"""
    async def _gate():
        return {"verdict": "preparing", "reason": "job_in_flight"}

    out = asyncio.run(tv.wait_for_transfer_ready(
        {"verdict": "preparing", "reason": "job_in_flight"}, _gate,
        max_sec=0.15, poll_sec=0.05))
    assert out["verdict"] == "preparing"
