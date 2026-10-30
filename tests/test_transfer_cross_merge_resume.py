# -*- coding: utf-8 -*-
"""跨 provider 在途合并＋断点续传（2026-10-31 第三单）。

生产案：过去 24h 跨盘到 pan115 共 71 单、92 次 attempts——源平均多读
约三成、最狠一单 9 遍。根因是搬字节不落盘：每次尝试都从源盘重读
全部字节，且同内容多人转存时各读各的。

本单机制（源字节暂存，transfer_variant._SourceStage）：
- 同内容（源 provider＋源 fid＋大小）第一次搬运时把源字节边读边写
  进 worker 本地暂存文件（按段落盘、分段前缀口径）；后续同内容单
  （不分用户——各用户仍向本人盘各传一份、变体行各归各人）直接读
  暂存、源盘零次读取，即「在途合并」落到字节层；
- 失败重试时已暂存前缀直接复用，简单上传整单重发也不重读源，
  即源侧「断点续传」；115 分片路另有目的端续传（见下）。
- 暂存是 best-effort：目录不可写、磁盘余量不足、总量超帽时静默
  退回现行直读源口径，绝不阻塞转存本身。

115 分片目的端续传（transfer115.multipart_upload）：
- 失败时若调用方传了 on_progress，上传会话（bucket/object/upload_id/
  callback/已成片）通过回调持久化、不立即 abort；重试时凭 ListParts
  校验服务端已成片与持久化一致才续传剩余片，不一致则 abort 旧会
  话重走全新上传；不传 on_progress 的旧调用方维持原 abort 口径。
- 注意：续传会话只在同用户盘内复用（sidecar 按 job 校验 content_key
  ＋大小＋目标目录），绝不跨用户串会话。

全部离线：SQLite :memory:，SA 下载、115 客户端、OSS HTTP 一律桩替身；
不碰生产 Cookie、不碰 Redis。InstaBox 上跑时 TMPDIR 须指向家目录
（本机 /tmp 已满），见运行命令。
"""
import os

os.environ.setdefault("DATABASE_TYPE", "sqlite")
os.environ.setdefault("REDIS_ENABLED", "false")

import hashlib
import io
import json

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


_counter = [0]
# 2000 字节；_CROSS_SEGMENT 打小到 512 后正好 4 个窗口（512×3＋464）。
_PAYLOAD = bytes((i * 7) % 256 for i in range(2000))
_PAYLOAD_SHA1 = hashlib.sha1(_PAYLOAD).hexdigest()


def _item(db, **kw):
    _counter[0] += 1
    row = em.MediaItem(guid=f"mr-{_counter[0]}", library_id=1,
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
    """SA 桩：get_file 报大小，download_range 按段切字节并计数。"""

    def __init__(self, payload=_PAYLOAD):
        self.payload = payload
        self.download_calls = 0
        self.ranges = []

    def get_file(self, drive_id, fid):
        return {"id": fid, "size": len(self.payload)}

    def download_range(self, drive_id, fid, start, end):
        self.download_calls += 1
        self.ranges.append((start, end))
        return self.payload[start:end + 1]


class _FakeUser115:
    """用户侧 115 桩：simple/multipart 行为可配，multipart 接受续传参数。"""

    def __init__(self, simple_mode="ok", multipart_mode="ok"):
        self.dirs = {
            "0": [{"name": "aetrix-transfer", "is_dir": True, "cid": "900"}],
            "900": [],
        }
        self.simple_mode = simple_mode
        self.multipart_mode = multipart_mode
        self.simple_calls = []
        self.multipart_calls = []  # (resume_session or None, on_progress? bool)

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
        return {"outcome": "miss", "fid": "", "pickcode": "", "raw": {}}

    def simple_upload(self, fileobj, filename, pid):
        data = fileobj.read()
        self.simple_calls.append(data)
        if self.simple_mode == "fail_once" and len(self.simple_calls) == 1:
            raise transfer115.Pan115Error("模拟 simple 上传中途失败")
        if self.simple_mode == "fail_always":
            raise transfer115.Pan115Error("模拟 simple 上传恒失败")
        sha1 = hashlib.sha1(data).hexdigest()
        self._place(filename, "upfid", "pcup", len(data), sha1)
        return {"fid": "upfid", "pickcode": "pcup", "sha1": sha1, "raw": {}}

    def multipart_upload(self, fileobj, filename, filesize, file_sha1, pid,
                         range_reader=None, resume_session=None,
                         on_progress=None):
        self.multipart_calls.append((resume_session, on_progress is not None))
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
        sha1 = file_sha1
        self._place(filename, "mpfid", "pcmp", filesize, sha1)
        return {"outcome": "uploaded", "fid": "mpfid", "pickcode": "pcmp",
                "sha1": sha1, "raw": {}}


@pytest.fixture()
def stage_env(monkeypatch, tmp_path):
    """暂存目录指到 pytest 临时目录；段窗口打小以便多窗口断言。"""
    monkeypatch.setenv("TRANSFER_STAGE_DIR", str(tmp_path / "stage"))
    monkeypatch.setattr(tv, "_CROSS_SEGMENT", 512)
    return tmp_path / "stage"


def _patch_cross(monkeypatch, fake115):
    monkeypatch.setattr(tv, "_make_pan115_client", lambda ck: fake115)
    monkeypatch.setattr(ub, "binding_115_cookie",
                        lambda db_arg, user_id, binding_id: "COOKIE")


def _variant_rows(db):
    return db.query(em.TransferVariant).all()


# ---------- 执行器：暂存共享（在途合并） ----------

def test_second_user_reuses_stage_zero_source_reads(db, monkeypatch, stage_env):
    item = _item(db)
    _binding(db, 7)
    _binding(db, 8)
    fake115 = _FakeUser115()
    _patch_cross(monkeypatch, fake115)
    sa = _FakeSA()
    job7 = _cross_job(db, item.id, user_id=7)
    res7 = tv.run_transfer_job_once(db, job7, sa)
    assert res7["outcome"] == "done"
    assert sa.download_calls == 4  # 4 个 512 窗口，源字节全程只读一遍
    job8 = _cross_job(db, item.id, user_id=8)
    res8 = tv.run_transfer_job_once(db, job8, sa)
    assert res8["outcome"] == "done"
    assert sa.download_calls == 4  # 第二人零次碰源
    rows = _variant_rows(db)
    assert len(rows) == 2
    assert {r.owner_ref for r in rows} == {"7", "8"}
    assert {r.variant_drive_id for r in rows} == {"pan115:user-7",
                                                  "pan115:user-8"}
    assert all(r.status == "ready" for r in rows)
    staged = list(stage_env.glob("*.bin"))
    assert len(staged) == 1 and staged[0].stat().st_size == len(_PAYLOAD)


def test_retry_after_upload_failure_reads_only_stage(db, monkeypatch, stage_env):
    item = _item(db)
    _binding(db, 7)
    fake115 = _FakeUser115(simple_mode="fail_once")
    _patch_cross(monkeypatch, fake115)
    sa = _FakeSA()
    job = _cross_job(db, item.id, user_id=7)
    res1 = tv.run_transfer_job_once(db, job, sa)
    assert res1["outcome"] == "retried"
    assert sa.download_calls == 4  # 首尝读满源、暂存完成
    res2 = tv.run_transfer_job_once(db, job, sa)
    assert res2["outcome"] == "done"
    assert sa.download_calls == 4  # 重试零次重读源
    db.refresh(job)
    assert job.status == "done" and job.attempts == 2
    assert len(_variant_rows(db)) == 1


def test_different_source_no_share(db, monkeypatch, stage_env):
    item1 = _item(db, drive_file_id="origfid")
    item2 = _item(db, drive_file_id="origfid2")
    _binding(db, 7)
    fake115 = _FakeUser115()
    _patch_cross(monkeypatch, fake115)
    sa = _FakeSA()
    assert tv.run_transfer_job_once(
        db, _cross_job(db, item1.id, user_id=7), sa)["outcome"] == "done"
    assert sa.download_calls == 4
    assert tv.run_transfer_job_once(
        db, _cross_job(db, item2.id, user_id=7), sa)["outcome"] == "done"
    assert sa.download_calls == 8  # 不同源不共享暂存


def test_stage_unwritable_falls_back_to_source(db, monkeypatch, tmp_path):
    blocker = tmp_path / "blocker"
    blocker.write_bytes(b"x")
    monkeypatch.setenv("TRANSFER_STAGE_DIR", str(blocker / "stage"))
    monkeypatch.setattr(tv, "_CROSS_SEGMENT", 512)
    item = _item(db)
    _binding(db, 7)
    fake115 = _FakeUser115()
    _patch_cross(monkeypatch, fake115)
    sa = _FakeSA()
    res = tv.run_transfer_job_once(db, _cross_job(db, item.id, user_id=7), sa)
    assert res["outcome"] == "done"  # 暂存不可用也不阻塞转存
    assert sa.download_calls == 4


def test_attempts_and_error_kind_unchanged_on_permanent_failure(
        db, monkeypatch, stage_env):
    item = _item(db)
    _binding(db, 7)
    fake115 = _FakeUser115(simple_mode="fail_always")
    _patch_cross(monkeypatch, fake115)
    sa = _FakeSA()
    job = _cross_job(db, item.id, user_id=7)
    assert tv.run_transfer_job_once(db, job, sa)["outcome"] == "retried"
    assert tv.run_transfer_job_once(db, job, sa)["outcome"] == "retried"
    assert tv.run_transfer_job_once(db, job, sa)["outcome"] == "failed"
    db.refresh(job)
    assert job.status == "failed" and job.error_kind == "retriable"
    assert job.attempts == 3


# ---------- 执行器：分片 sidecar 往返 ----------

def test_multipart_sidecar_roundtrip(db, monkeypatch, stage_env):
    monkeypatch.setattr(tv, "_115_SIMPLE_UPLOAD_LIMIT", 1024)
    item = _item(db)  # 无 content_hash：首尝先哈希遍再走分片
    _binding(db, 7)
    fake115 = _FakeUser115(multipart_mode="fail_once")
    _patch_cross(monkeypatch, fake115)
    sa = _FakeSA()
    job = _cross_job(db, item.id, user_id=7)
    res1 = tv.run_transfer_job_once(db, job, sa)
    assert res1["outcome"] == "retried"
    assert len(fake115.multipart_calls) == 1
    assert fake115.multipart_calls[0][0] is None  # 首尝无会话
    res2 = tv.run_transfer_job_once(db, job, sa)
    assert res2["outcome"] == "done"
    assert len(fake115.multipart_calls) == 2
    resumed = fake115.multipart_calls[1][0]
    assert resumed is not None and resumed.get("upload_id") == "UP-1"
    assert resumed.get("parts") == [{"PartNumber": 1, "ETag": "E1"}]
    assert list(stage_env.glob("mp-*.json")) == []  # done 后会话清掉


# ---------- 暂存单元：前缀、非顺序写、过期清扫 ----------

def test_stage_prefix_tee_read_and_sweep(monkeypatch, tmp_path):
    monkeypatch.setenv("TRANSFER_STAGE_DIR", str(tmp_path / "stage"))
    monkeypatch.setenv("TRANSFER_STAGE_TTL_SEC", "60")
    stage = tv._acquire_source_stage("drive", "d1", "f1", 2000)
    assert stage is not None
    assert stage.has(0, 0) is False
    stage.tee(0, _PAYLOAD[:512])
    assert stage.has(0, 511) is True
    assert stage.has(0, 512) is False
    stage.tee(1500, _PAYLOAD[1500:2000])  # 非顺序写不推进前缀
    assert stage.staged_prefix == 512
    stage.tee(512, _PAYLOAD[512:1024])
    assert stage.read(0, 511) == _PAYLOAD[:512]
    assert stage.staged_prefix == 1024
    # 过期清扫：把本 stage 文件 mtime 拨老，再 acquire 另一 key 时清掉
    import os as _os
    import time as _time
    old = _time.time() - 3600
    for path in tmp_path.glob("stage/*"):
        _os.utime(path, (old, old))
    old_bin = stage.bin_path
    tv._acquire_source_stage("drive", "d1", "f2", 100)
    import os as _os2
    assert not _os2.path.exists(old_bin)  # 过期 stage 被清扫


# ---------- 客户端：分片续传（OSS 桩） ----------

class _FakeOSSHttp:
    """OSS 传输桩：init/PUT/complete/abort/ListParts，失败可注入。"""

    def __init__(self, fail_part=None):
        self.requests = []
        self.parts = {}  # number -> (data, etag)
        self.fail_part = fail_part
        self.aborts = 0

    def __call__(self, method, url, *, headers=None, data=None, client=None):
        from urllib.parse import parse_qsl, urlsplit
        query = dict(parse_qsl(urlsplit(url).query))
        self.requests.append({"method": method, "query": query,
                              "data": data})
        if "uploads" in query:
            return (200, {}, b"<InitiateMultipartUploadResult>"
                             b"<UploadId>UPLOAD-42</UploadId>"
                             b"</InitiateMultipartUploadResult>")
        if method == "PUT" and "partNumber" in query:
            number = int(query["partNumber"])
            if self.fail_part is not None and number == self.fail_part:
                raise transfer115.Pan115Error("模拟 OSS 第 %s 片失败"
                                              % number)
            etag = hashlib.md5(data).hexdigest()
            self.parts[number] = (data, etag)
            return (200, {"etag": '"%s"' % etag}, b"")
        if method == "GET" and "uploadId" in query:
            body = "<ListPartsResult>" + "".join(
                "<Part><PartNumber>%s</PartNumber>"
                "<ETag>\"%s\"</ETag></Part>" % (n, e)
                for n, (_, e) in sorted(self.parts.items())
            ) + "</ListPartsResult>"
            return (200, {}, body.encode())
        if method == "POST" and "uploadId" in query:
            return (200, {}, json.dumps(
                {"data": {"file_id": "mpfid", "pickcode": "pcmp",
                          "sha1": "deadbeef"}}).encode())
        if method == "DELETE" and "uploadId" in query:
            self.aborts += 1
            self.parts.clear()
            return (200, {}, b"")
        raise AssertionError("未预期的 OSS 请求：%s %s" % (method, query))


def _patch_oss_client(monkeypatch, oss):
    monkeypatch.setattr(transfer115, "_http_full", oss)
    monkeypatch.setattr(
        transfer115.Pan115Client, "_upload_init",
        lambda self, filename, filesize, file_sha1, pid, range_reader=None:
        {"status": 1, "bucket": "bkt", "object": "obj",
         "callback": {"callback": "Yg==", "callback_var": "dmI="}})
    monkeypatch.setattr(
        transfer115.Pan115Client, "upload_token",
        lambda self: {"AccessKeyId": "AKID", "AccessKeySecret": "SECRET",
                      "SecurityToken": "STOKEN"})


def test_multipart_resume_continues_after_part_failure(monkeypatch):
    monkeypatch.setattr(transfer115, "_MULTIPART_MIN_PART_SIZE", 1024)
    payload = bytes(range(256)) * 8 + b"tail!"  # 2053＝1024＋1024＋5，三片
    sha1 = hashlib.sha1(payload).hexdigest()
    oss = _FakeOSSHttp(fail_part=3)
    _patch_oss_client(monkeypatch, oss)
    client = transfer115.Pan115Client("COOKIE")
    events = []
    with pytest.raises(transfer115.Pan115Error):
        client.multipart_upload(io.BytesIO(payload), "a.mkv", len(payload),
                                sha1, "900", on_progress=events.append)
    assert oss.aborts == 0  # 传了 on_progress：会话留给重试，不 abort
    session = None
    parts = []
    for ev in events:
        if ev["event"] == "init":
            session = dict(ev["session"])
        elif ev["event"] == "part":
            parts.append({"PartNumber": ev["number"], "ETag": ev["etag"]})
    assert session is not None and session["upload_id"] == "UPLOAD-42"
    assert [p["PartNumber"] for p in parts] == [1, 2]
    session["parts"] = parts
    oss.fail_part = None
    mark = len(oss.requests)
    out = client.multipart_upload(io.BytesIO(payload), "a.mkv", len(payload),
                                  sha1, "900", resume_session=session)
    assert out["outcome"] == "uploaded"
    puts2 = [r for r in oss.requests[mark:] if r["method"] == "PUT"]
    # 续传只补第 3 片，1、2 片绝不重传
    assert [r["query"]["partNumber"] for r in puts2] == ["3"]
    comp = [r for r in oss.requests if r["method"] == "POST"
            and "uploadId" in r["query"]][0]
    body = comp["data"].decode()
    for number in (1, 2, 3):
        assert "<PartNumber>%s</PartNumber>" % number in body


def test_multipart_resume_stale_falls_back_to_fresh(monkeypatch):
    monkeypatch.setattr(transfer115, "_MULTIPART_MIN_PART_SIZE", 1024)
    payload = bytes(range(256)) * 8 + b"tail!"
    sha1 = hashlib.sha1(payload).hexdigest()
    oss = _FakeOSSHttp()
    _patch_oss_client(monkeypatch, oss)
    client = transfer115.Pan115Client("COOKIE")
    # 服务端已有 1、2 片，但持久化会话谎称第 2 片 ETag 不同 → 判定过期
    oss.parts = {1: (payload[:1024],
                     hashlib.md5(payload[:1024]).hexdigest()),
                 2: (payload[1024:2048],
                     hashlib.md5(payload[1024:2048]).hexdigest())}
    stale = {"bucket": "bkt", "object_key": "obj", "upload_id": "UPLOAD-42",
             "callback": {"callback": "Yg==", "callback_var": "dmI="},
             "parts": [{"PartNumber": 1,
                        "ETag": hashlib.md5(payload[:1024]).hexdigest()},
                       {"PartNumber": 2, "ETag": "0" * 32}]}
    out = client.multipart_upload(io.BytesIO(payload), "a.mkv", len(payload),
                                  sha1, "900", resume_session=stale)
    assert out["outcome"] == "uploaded"
    assert oss.aborts == 1  # 旧会话 abort 后重走全新上传
    puts = [r for r in oss.requests if r["method"] == "PUT"]
    assert [r["query"]["partNumber"] for r in puts] == ["1", "2", "3"]


def test_multipart_no_progress_keeps_abort_on_failure(monkeypatch):
    """锚：不传 on_progress 的旧调用方维持原 abort 口径。"""
    monkeypatch.setattr(transfer115, "_MULTIPART_MIN_PART_SIZE", 1024)
    payload = bytes(range(256)) * 8 + b"tail!"
    sha1 = hashlib.sha1(payload).hexdigest()
    oss = _FakeOSSHttp(fail_part=2)
    _patch_oss_client(monkeypatch, oss)
    client = transfer115.Pan115Client("COOKIE")
    with pytest.raises(transfer115.Pan115Error):
        client.multipart_upload(io.BytesIO(payload), "a.mkv", len(payload),
                                sha1, "900")
    assert oss.aborts == 1
