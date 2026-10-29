# -*- coding: utf-8 -*-
"""Path B file-only 搬字节分流（2026-10-29 copy 404 修复）。

机制（生产实测钉住，mock 按谷歌真实语义建模）：
- file-only 令牌对非应用自创文件 files.get/copy 一律 404（源共享盘
  原件、SA 暂存副本皆然），故 Path A 与 Path B 的 user copy 永不
  可能成功；双范围令牌可见 anyone-reader 源，copy 404 只是分享
  传播延迟、可重试。
- 修复：Path B 用户 copy 被拒且绑定纯 file-only 时，改走搬字节
  （SA 按段下载暂存 → 用户令牌可续传上传，上传即应用自创文件），
  随后走同一条核对／anyone／登记／删暂存尾巴。
- 大小以 SA 侧暂存实测为准，绝不用条目 size 猜（猜小会静默截断；
  本文件有条目 size 故意写 stale 的用例钉住这一点）。

全部离线：Drive API 一律用桩替身；真实 file-only 令牌行为、GB 级
搬运全链路、部署后打回观察均未在本文件实测，见 PR 未验证项。
"""
import os

os.environ.setdefault("DATABASE_TYPE", "sqlite")
os.environ.setdefault("REDIS_ENABLED", "false")

from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend import models as base_models
from backend.emby_server import models as em
from backend.emby_server import transfer_variant as tv

FILE_SCOPE = "https://www.googleapis.com/auth/drive.file"
READONLY_SCOPE = "https://www.googleapis.com/auth/drive.readonly"


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


def _item(db, **kw):
    _counter[0] += 1
    row = em.MediaItem(guid=f"pb-{_counter[0]}", library_id=1,
                       item_type="movie", name="测试片",
                       drive_file_id="origfid",
                       file_path="mount://3/电影/a.mkv", size=1000)
    for key, value in kw.items():
        setattr(row, key, value)
    db.add(row)
    db.commit()
    return row


def _binding(db, user_id, scopes):
    cred = em.MountCredential(scope="user", owner_user_id=user_id,
                              status="active")
    db.add(cred)
    db.commit()
    row = em.UserStorageBinding(user_id=user_id, provider="drive",
                                account_label="test",
                                credential_id=cred.id,
                                status="active",
                                granted_scopes=scopes)
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


class _FakeUserBytemove:
    """file-only 用户侧桩：copy 非应用文件一律 404（谷歌语义）。

    flaky_once：暂存 copy 第一次 404、第二次成功（模拟双范围传播
    延迟）；默认每次 404（模拟 file-only 永久不可见）。
    """

    def __init__(self, dir_ids=None, flaky_once=False):
        self.dir_ids = dir_ids
        self.flaky_once = flaky_once
        self._tmp_failed_once = False
        self.copy_calls = []
        self.perm_calls = []
        self.upload_calls = []
        self.uploaded = b""
        self._binding = None

    def ensure_folder(self, drive_id, dir_name):
        return "ufolder"

    def list_file_ids_by_name(self, drive_id, folder_id, name):
        if self.dir_ids is not None:
            return list(self.dir_ids)
        return ["uploadfid"]

    def copy_file(self, drive_id, source_fid, folder_id, name):
        self.copy_calls.append(source_fid)
        if source_fid == "origfid":
            raise tv._CopyForbidden("copy 被拒：404")
        if source_fid == "tmpfid":
            if self.flaky_once:
                if not self._tmp_failed_once:
                    self._tmp_failed_once = True
                    raise tv._CopyForbidden("copy 被拒：404")
                return {"id": "usercopy", "size": 1000}
            raise tv._CopyForbidden("copy 被拒：404")
        return {"id": "usercopy", "size": 1000}

    def resumable_upload(self, drive_id, name, folder_id, size, reader):
        self.upload_calls.append((name, folder_id, size))
        reader.seek(0)
        chunks = []
        while True:
            chunk = reader.read(1024 * 1024)
            if not chunk:
                break
            chunks.append(chunk)
        self.uploaded = b"".join(chunks)
        return "uploadfid"

    def add_anyone_reader(self, drive_id, fid):
        self.perm_calls.append(fid)


class _FakeSABytemove:
    """Path B SA 侧桩：暂存数据由调用方给定字节串。"""

    def __init__(self, data, tmp_exists=False, get_size=None):
        self.data = data
        self.tmp_exists = tmp_exists
        self.get_size = len(data) if get_size is None else get_size
        self.copy_calls = 0
        self.deleted = []

    def ensure_folder(self, drive_id, dir_name):
        return "tfolder"

    def find_file_by_name(self, drive_id, folder_id, name):
        return "tmpfid" if self.tmp_exists else None

    def copy_file(self, drive_id, source_fid, folder_id, name):
        self.copy_calls += 1
        return {"id": "tmpfid", "size": len(self.data)}

    def get_file(self, drive_id, fid):
        return {"id": fid, "size": self.get_size}

    def download_range(self, drive_id, fid, start, end):
        assert fid == "tmpfid"
        return self.data[start:end + 1]

    def add_anyone_reader(self, drive_id, fid):
        pass

    def delete_file(self, drive_id, fid):
        self.deleted.append(fid)


def test_path_b_fileonly_copy404_falls_back_to_bytemove(db, monkeypatch):
    item = _item(db)
    binding = _binding(db, 7, FILE_SCOPE)
    job = _user_job(db, item.id)
    fake = _FakeUserBytemove()
    fake._binding = binding
    monkeypatch.setattr(tv, "UserDriveApiClient",
                        lambda db_arg, b: fake)
    sa = _FakeSABytemove(b"x" * 1000)
    result = tv.run_transfer_job_once(db, job, sa)
    assert result["outcome"] == "done" and job.status == "done"
    assert fake.copy_calls == ["origfid", "tmpfid"]
    assert fake.upload_calls == [("a.mkv", "ufolder", 1000)]
    assert fake.uploaded == b"x" * 1000
    assert fake.perm_calls == ["uploadfid"]
    assert sa.copy_calls == 1 and sa.deleted == ["tmpfid"]
    row = db.query(em.TransferVariant).filter(
        em.TransferVariant.status == "ready").one()
    assert row.variant_file_id == "uploadfid"
    assert row.variant_drive_id == "userdrive:7"
    assert row.owner_ref == "7"


def test_path_b_fileonly_upload_uses_sa_size_not_item_size(db, monkeypatch):
    # 条目 size 故意写 stale（999），SA 暂存实测 1000：上传必须按
    # 1000 字节搬，绝不能按条目 size 截断。
    item = _item(db, size=999)
    binding = _binding(db, 7, FILE_SCOPE)
    job = _user_job(db, item.id)
    fake = _FakeUserBytemove()
    fake._binding = binding
    monkeypatch.setattr(tv, "UserDriveApiClient",
                        lambda db_arg, b: fake)
    sa = _FakeSABytemove(b"y" * 1000)
    result = tv.run_transfer_job_once(db, job, sa)
    assert result["outcome"] == "done"
    assert fake.upload_calls == [("a.mkv", "ufolder", 1000)]
    assert fake.uploaded == b"y" * 1000


def test_path_b_fileonly_bytemove_multisegment_exact(db, monkeypatch):
    n = 8 * 1024 * 1024 + 13
    data = bytes((i * 7) % 256 for i in range(n))
    item = _item(db, size=n)
    binding = _binding(db, 7, FILE_SCOPE)
    job = _user_job(db, item.id)
    fake = _FakeUserBytemove()
    fake._binding = binding
    monkeypatch.setattr(tv, "UserDriveApiClient",
                        lambda db_arg, b: fake)
    sa = _FakeSABytemove(data)
    result = tv.run_transfer_job_once(db, job, sa)
    assert result["outcome"] == "done"
    assert fake.uploaded == data


def test_path_b_dual_scope_copy404_still_retries(db, monkeypatch):
    # 双范围：暂存 copy 404 仍按传播延迟重试，不走搬字节。
    item = _item(db)
    binding = _binding(db, 7, FILE_SCOPE + " " + READONLY_SCOPE)
    job = _user_job(db, item.id)
    fake = _FakeUserBytemove(dir_ids=["usercopy"], flaky_once=True)
    fake._binding = binding
    monkeypatch.setattr(tv, "UserDriveApiClient",
                        lambda db_arg, b: fake)
    sa = _FakeSABytemove(b"z" * 1000)
    assert tv.run_transfer_job_once(db, job, sa)["outcome"] == "retried"
    assert fake.upload_calls == []
    job.next_retry_at = None
    sa.tmp_exists = True
    db.commit()
    assert tv.run_transfer_job_once(db, job, sa)["outcome"] == "done"
    assert fake.upload_calls == []
    assert sa.copy_calls == 1 and sa.deleted == ["tmpfid"]


def test_path_b_fileonly_bytemove_size_unknown_unsupported(db, monkeypatch):
    item = _item(db)
    binding = _binding(db, 7, FILE_SCOPE)
    job = _user_job(db, item.id)
    fake = _FakeUserBytemove()
    fake._binding = binding
    monkeypatch.setattr(tv, "UserDriveApiClient",
                        lambda db_arg, b: fake)
    sa = _FakeSABytemove(b"w" * 1000, get_size=0)
    result = tv.run_transfer_job_once(db, job, sa)
    assert result["outcome"] == "failed"
    assert result["error_kind"] == "unsupported"
    assert fake.upload_calls == []
    assert db.query(em.TransferVariant).count() == 0


def test_is_file_only_binding_cases():
    assert tv._is_file_only_binding(SimpleNamespace(
        granted_scopes=FILE_SCOPE)) is True
    assert tv._is_file_only_binding(SimpleNamespace(
        granted_scopes=FILE_SCOPE + " " + READONLY_SCOPE)) is False
    assert tv._is_file_only_binding(SimpleNamespace(
        granted_scopes=READONLY_SCOPE)) is False
    assert tv._is_file_only_binding(SimpleNamespace(
        granted_scopes="")) is False
    assert tv._is_file_only_binding(None) is False
    assert tv._is_file_only_binding(SimpleNamespace()) is False
