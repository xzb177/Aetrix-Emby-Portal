# -*- coding: utf-8 -*-
"""换代线 step 4：115 执行面接管＋调度合并（PR-B）。

- 接管开（默认）时，Drive→115 的续传会话恢复以 transfer_deliveries
  落盘行为准（校验过才用），sidecar 退为第二落点；两者不一致以
  落盘行为准并记日志。接管/记账关 → 恢复顺序逐字回旧（sidecar 先）。
- 清单 complete 且整文件 sha1 在册时，执行器直接采信该 sha1
  （秒传首查即带哈希），省掉大文件整遍哈希重读。
- 调度合并：同 content_key 已有在跑的领跑单时，后到单不烧
  attempts、不碰源，defer 回 pending 等领跑单把暂存/清单备好；
  领跑租约过期、单子太老、接管关时都不合并、照常执行。

全部离线，桩口径同 test_transfer_cross_merge_resume.py。
"""
import os

os.environ.setdefault("DATABASE_TYPE", "sqlite")
os.environ.setdefault("REDIS_ENABLED", "false")

import hashlib
import json
from datetime import datetime, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend import models as base_models
from backend.emby_server import models as em
from backend.emby_server import transfer_manifest as tman
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


_counter = [0]
_PAYLOAD = bytes((i * 7) % 256 for i in range(2000))
_PAYLOAD_SHA1 = hashlib.sha1(_PAYLOAD).hexdigest()


def _item(db, **kw):
    _counter[0] += 1
    row = em.MediaItem(guid=f"tk-{_counter[0]}", library_id=1,
                       item_type="movie", name="测试片",
                       drive_file_id="origfid",
                       file_path="mount://3/电影/a.mkv", size=len(_PAYLOAD))
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


def _cross_job(db, item_id, user_id=7, dest=None, drive_id="shared1"):
    row = em.TransferJob(item_id=item_id, trigger="playback",
                         drive_id=drive_id, provider="drive",
                         owner_scope="user", owner_ref=str(user_id),
                         dest_drive_id=dest or f"pan115:user-{user_id}",
                         status="pending")
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
    def __init__(self, multipart_mode="ok"):
        self.dirs = {
            "0": [{"name": "aetrix-transfer", "is_dir": True, "cid": "900"}],
            "900": [],
        }
        self.multipart_mode = multipart_mode
        self.multipart_calls = []
        self.rapid_calls = []

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
        self.rapid_calls.append(file_sha1)
        return {"outcome": "miss", "fid": "", "pickcode": "", "raw": {}}

    def simple_upload(self, fileobj, filename, pid):
        data = fileobj.read()
        sha1 = hashlib.sha1(data).hexdigest()
        self._place(filename, "upfid", "pcup", len(data), sha1)
        return {"fid": "upfid", "pickcode": "pcup", "sha1": sha1, "raw": {}}

    def multipart_upload(self, fileobj, filename, filesize, file_sha1, pid,
                         range_reader=None, resume_session=None,
                         on_progress=None):
        self.multipart_calls.append(resume_session)
        if self.multipart_mode == "fail_once" and len(self.multipart_calls) == 1:
            if on_progress is not None:
                on_progress({"event": "init",
                             "session": {"bucket": "bkt", "object_key": "obj",
                                         "upload_id": "UP-1",
                                         "callback": {"callback": "Yg==",
                                                      "callback_var": "dmI="},
                                         "parts": []}})
                on_progress({"event": "part", "number": 1, "etag": "E1"})
            raise transfer115.Pan115Error("模拟分片上传中途失败")
        self._place(filename, "mpfid", "pcmp", filesize, file_sha1)
        return {"outcome": "uploaded", "fid": "mpfid", "pickcode": "pcmp",
                "sha1": file_sha1, "raw": {}}


@pytest.fixture()
def stage_env(monkeypatch, tmp_path):
    monkeypatch.setenv("TRANSFER_STAGE_DIR", str(tmp_path / "stage"))
    monkeypatch.setattr(tv, "_CROSS_SEGMENT", 512)
    return tmp_path / "stage"


def _patch_cross(monkeypatch, fake115):
    monkeypatch.setattr(tv, "_make_pan115_client", lambda ck: fake115)
    monkeypatch.setattr(ub, "binding_115_cookie",
                        lambda db_arg, user_id, binding_id: "COOKIE")


# ---------- 清单可信 sha1 ----------

def _seed_manifest(db, state="complete", sha1=_PAYLOAD_SHA1, size=len(_PAYLOAD)):
    """用 #1648 的记账正道（recorder＋tracker）把清单喂到目标状态。"""
    key = tv._stage_key("drive", "shared1", "origfid", size)
    recorder = tman.begin_recorder(db, key, size)
    assert recorder is not None
    recorder.tracker.feed(0, _PAYLOAD[:size])
    recorder.flush(db, whole_sha1=sha1 if state == "complete" else None)
    row = db.query(em.TransferContentManifest).filter_by(
        content_key=key).one()
    assert row.manifest_state == state
    return key


def test_manifest_trusted_sha1_helper(db):
    key = _seed_manifest(db)
    assert tman.manifest_trusted_sha1(db, key, len(_PAYLOAD)) == _PAYLOAD_SHA1
    assert tman.manifest_trusted_sha1(db, key, len(_PAYLOAD) + 1) is None
    assert tman.manifest_trusted_sha1(db, "nonexistent", len(_PAYLOAD)) is None


def test_manifest_trusted_sha1_partial_manifest_rejected(db):
    key = _seed_manifest(db, state="partial")
    assert tman.manifest_trusted_sha1(db, key, len(_PAYLOAD)) is None


def test_executor_uses_manifest_sha1_for_first_rapid(
        db, monkeypatch, stage_env):
    monkeypatch.setattr(tv, "_115_SIMPLE_UPLOAD_LIMIT", 1024)
    _seed_manifest(db)
    item = _item(db)  # 条目本身无 content_hash
    _binding(db, 7)
    fake115 = _FakeUser115()
    _patch_cross(monkeypatch, fake115)
    sa = _FakeSA()
    res = tv.run_transfer_job_once(db, _cross_job(db, item.id, user_id=7), sa)
    assert res["outcome"] == "done"
    # 秒传首查即带清单 sha1（无清单时首查只能带空串、先哈希后二查）
    assert fake115.rapid_calls[0] == _PAYLOAD_SHA1


# ---------- 续传恢复优先级：落盘行 > sidecar（接管开时） ----------

def _run_fail_once(db, monkeypatch, stage_env):
    monkeypatch.setattr(tv, "_115_SIMPLE_UPLOAD_LIMIT", 1024)
    item = _item(db)
    _binding(db, 7)
    fake115 = _FakeUser115(multipart_mode="fail_once")
    _patch_cross(monkeypatch, fake115)
    sa = _FakeSA()
    job = _cross_job(db, item.id, user_id=7)
    res1 = tv.run_transfer_job_once(db, job, sa)
    assert res1["outcome"] == "retried"
    return item, job, fake115, sa


def _rewrite_db_session_upload_id(db, job_id, upload_id):
    row = db.query(em.TransferDelivery).filter_by(job_id=job_id).first()
    assert row is not None
    data = json.loads(row.session)
    data["upload_id"] = upload_id
    row.session = json.dumps(data)
    db.commit()


def test_db_session_wins_over_sidecar_when_takeover_on(
        db, monkeypatch, stage_env):
    item, job, fake115, sa = _run_fail_once(db, monkeypatch, stage_env)
    # 落盘行比 sidecar 新（模拟 sidecar 写入后又有进展只落了 DB）
    _rewrite_db_session_upload_id(db, job.id, "UP-DB")
    res2 = tv.run_transfer_job_once(db, job, sa)
    assert res2["outcome"] == "done"
    resumed = fake115.multipart_calls[1]
    assert resumed is not None and resumed.get("upload_id") == "UP-DB"


def test_sidecar_wins_when_takeover_off(db, monkeypatch, stage_env):
    tv.write_transfer_config(db, transfer_manifest_takeover_enabled=False)
    item, job, fake115, sa = _run_fail_once(db, monkeypatch, stage_env)
    _rewrite_db_session_upload_id(db, job.id, "UP-DB")
    res2 = tv.run_transfer_job_once(db, job, sa)
    assert res2["outcome"] == "done"
    resumed = fake115.multipart_calls[1]
    assert resumed is not None and resumed.get("upload_id") == "UP-1"


# ---------- 调度合并 ----------

def _leader(db, item_id, user_id=7, lease_alive=True):
    job = _cross_job(db, item_id, user_id=user_id)
    job.status = "running"
    job.attempts = 1
    job.lease_expires_at = (datetime.now() + timedelta(seconds=600)
                            if lease_alive
                            else datetime.now() - timedelta(seconds=5))
    db.commit()
    return job


def test_follower_defers_to_running_leader(db, monkeypatch, stage_env):
    item = _item(db)
    _binding(db, 7)
    _binding(db, 8)
    leader = _leader(db, item.id, user_id=7)
    fake115 = _FakeUser115()
    _patch_cross(monkeypatch, fake115)
    sa = _FakeSA()
    follower = _cross_job(db, item.id, user_id=8)
    res = tv.run_transfer_job_once(db, follower, sa)
    assert res["outcome"] == "deferred"
    assert res["leader_job_id"] == leader.id
    db.refresh(follower)
    assert follower.status == "pending"
    assert follower.attempts == 0  # 让路不烧尝试次数
    assert follower.error_kind is None
    assert follower.next_retry_at is not None
    assert sa.download_calls == 0  # 一字节源都没碰


def test_follower_runs_when_leader_lease_expired(db, monkeypatch, stage_env):
    item = _item(db)
    _binding(db, 7)
    _binding(db, 8)
    _leader(db, item.id, user_id=7, lease_alive=False)
    fake115 = _FakeUser115()
    _patch_cross(monkeypatch, fake115)
    sa = _FakeSA()
    follower = _cross_job(db, item.id, user_id=8)
    res = tv.run_transfer_job_once(db, follower, sa)
    assert res["outcome"] == "done"


def test_no_merge_when_takeover_off(db, monkeypatch, stage_env):
    tv.write_transfer_config(db, transfer_manifest_takeover_enabled=False)
    item = _item(db)
    _binding(db, 7)
    _binding(db, 8)
    _leader(db, item.id, user_id=7)
    fake115 = _FakeUser115()
    _patch_cross(monkeypatch, fake115)
    sa = _FakeSA()
    follower = _cross_job(db, item.id, user_id=8)
    res = tv.run_transfer_job_once(db, follower, sa)
    assert res["outcome"] == "done"  # 接管关＝逐字旧行为，不让路


def test_leader_completion_then_follower_uses_stage(
        db, monkeypatch, stage_env):
    item = _item(db)
    _binding(db, 7)
    _binding(db, 8)
    fake115 = _FakeUser115()
    _patch_cross(monkeypatch, fake115)
    sa = _FakeSA()
    job7 = _cross_job(db, item.id, user_id=7)
    assert tv.run_transfer_job_once(db, job7, sa)["outcome"] == "done"
    calls_after_leader = sa.download_calls
    job8 = _cross_job(db, item.id, user_id=8)
    assert tv.run_transfer_job_once(db, job8, sa)["outcome"] == "done"
    assert sa.download_calls == calls_after_leader  # 跟随单零重读源
