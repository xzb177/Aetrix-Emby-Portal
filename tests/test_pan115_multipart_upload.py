# -*- coding: utf-8 -*-
"""115 分片（多段）上传整套（2026-10-30 用户亲点根治单：>5 GiB 搬运）。

背景：跨 provider Drive→115 时 simple_upload 有 5 GiB 硬上限，
秒传未命中的大文件此前直接判 unsupported（生产 job 1174 实证）。
本单在 #1526 秒传底子（initupload 加密协议）之上补 OSS 分片本体：
initupload status=1 的 bucket/object/callback 不再丢弃，接
gettoken STS 凭据 → OSS InitiateMultipartUpload → 逐片 PUT →
CompleteMultipartUpload 回调落盘。

全部离线：initupload 加密响应、OSS HTTP、用户 115 客户端、SA
下载一律桩替身；协议加解密在测试侧按 p115cipher 公开常量独立
实现交叉核对。真实端到端未实测（不许用生产用户 Cookie 做开发
测试），上线后靠真实大文件 job 观察。
"""
import os

os.environ.setdefault("DATABASE_TYPE", "sqlite")
os.environ.setdefault("REDIS_ENABLED", "false")

import base64
import hashlib
import io
import json
from types import SimpleNamespace
from urllib.parse import parse_qsl, urlsplit

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
_PAYLOAD = bytes(range(256)) * 8  # 2048 字节
_PAYLOAD_SHA1 = hashlib.sha1(_PAYLOAD).hexdigest()
_LIMIT = 1024  # 路由测试把 simple 上限打小，避免真造 5 GiB 数据


def _item(db, **kw):
    _counter[0] += 1
    row = em.MediaItem(guid=f"mp-{_counter[0]}", library_id=1,
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


def _cross_job(db, item_id, user_id=7, provider="drive", dest="pan115:user-7",
               drive_id="shared1"):
    row = em.TransferJob(item_id=item_id, trigger="playback",
                         drive_id=drive_id, provider=provider,
                         owner_scope="user", owner_ref=str(user_id),
                         dest_drive_id=dest, status="pending")
    db.add(row)
    db.commit()
    return row


def _enable(monkeypatch):
    monkeypatch.setenv("TRANSFER_VARIANT_ENABLED", "1")
    monkeypatch.setattr(tv, "_115_SIMPLE_UPLOAD_LIMIT", _LIMIT)


class _FakeSA:
    """SA 桩：get_file 报大小，download_range 按段切字节并计数。"""

    def __init__(self, payload=_PAYLOAD, size=None):
        self.payload = payload
        self.size = size
        self.download_calls = 0
        self.ranges = []

    def get_file(self, drive_id, fid):
        return {"id": fid,
                "size": self.size if self.size is not None
                else len(self.payload)}

    def download_range(self, drive_id, fid, start, end):
        self.download_calls += 1
        self.ranges.append((start, end))
        return self.payload[start:end + 1]


class _FakeUser115:
    """用户侧 115 桩：rapid/simple/multipart 行为可配、全程可观测。"""

    def __init__(self, rapid="miss", multipart="uploaded",
                 callback_sha1=None):
        self.dirs = {
            "0": [{"name": "aetrix-transfer", "is_dir": True, "cid": "900"}],
            "900": [],
        }
        self.rapid_behavior = rapid
        self.multipart_behavior = multipart
        self.callback_sha1 = callback_sha1
        self.rapid_calls = []
        self.simple_calls = []
        self.multipart_calls = []

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
        if self.rapid_behavior == "hit":
            self._place(filename, "rapidfid", "pcrapid", filesize, file_sha1)
            return {"outcome": "hit", "fid": "rapidfid",
                    "pickcode": "pcrapid", "raw": {"status": 2}}
        return {"outcome": "miss", "fid": "", "pickcode": "", "raw": {}}

    def simple_upload(self, fileobj, filename, pid):
        data = fileobj.read()
        self.simple_calls.append(data)
        sha1 = self.callback_sha1 or hashlib.sha1(data).hexdigest()
        self._place(filename, "upfid", "pcup", len(data), sha1)
        return {"fid": "upfid", "pickcode": "pcup", "sha1": sha1,
                "raw": {"data": {"file_id": "upfid", "sha1": sha1}}}

    def multipart_upload(self, fileobj, filename, filesize, file_sha1, pid,
                         range_reader=None, resume_session=None,
                         on_progress=None):
        # 续传参数桩侧忽略（执行器新版必传；旧桩忽略即兼容）。
        self.multipart_calls.append((filename, filesize, file_sha1, pid))
        if self.multipart_behavior == "auth":
            raise transfer115.Pan115AuthError("Cookie 已失效")
        if self.multipart_behavior == "error":
            raise transfer115.Pan115Error("OSS 第 2 片重试 3 次仍失败")
        if self.multipart_behavior == "read_all":
            fileobj.read()
        sha1 = self.callback_sha1 or file_sha1
        if self.multipart_behavior != "no_place":
            self._place(filename, "mpfid", "pcmp", filesize, sha1)
        return {"outcome": "uploaded", "fid": "mpfid", "pickcode": "pcmp",
                "sha1": sha1, "raw": {}}


def _patch_cross(monkeypatch, fake115):
    monkeypatch.setattr(tv, "_make_pan115_client", lambda ck: fake115)
    monkeypatch.setattr(ub, "binding_115_cookie",
                        lambda db_arg, user_id, binding_id: "COOKIE")


def _variant_rows(db):
    return db.query(em.TransferVariant).all()


# ---------- 路由：>上限走分片、恰上限走 simple、秒传命中不走分片 ----------

def test_big_boundary_limit_plus_one_goes_multipart(db, monkeypatch):
    _enable(monkeypatch)
    size = _LIMIT + 1
    item = _item(db, size=size, content_hash=_PAYLOAD_SHA1,
                 content_hash_algo="sha1")
    _binding(db, 7)
    job = _cross_job(db, item.id)
    fake = _FakeUser115(rapid="miss")
    _patch_cross(monkeypatch, fake)
    res = tv.run_transfer_job_once(db, job, _FakeSA(size=size))
    assert res["outcome"] == "done"
    assert len(fake.rapid_calls) == 1  # 先试秒传（顺序写死）
    assert len(fake.multipart_calls) == 1
    assert fake.multipart_calls[0][1] == size
    assert fake.multipart_calls[0][2] == _PAYLOAD_SHA1
    assert fake.simple_calls == []  # 超限文件不许碰 simple_upload
    row = _variant_rows(db)[0]
    assert row.variant_file_id == "mpfid" and row.pickcode == "pcmp"


def test_exactly_limit_uses_simple_not_multipart(db, monkeypatch):
    _enable(monkeypatch)
    size = _LIMIT
    item = _item(db, size=size, content_hash=_PAYLOAD_SHA1,
                 content_hash_algo="sha1")
    _binding(db, 7)
    job = _cross_job(db, item.id)
    fake = _FakeUser115(rapid="miss")
    _patch_cross(monkeypatch, fake)
    res = tv.run_transfer_job_once(db, job, _FakeSA(size=size))
    assert res["outcome"] == "done"
    assert fake.multipart_calls == []
    assert len(fake.simple_calls) == 1
    assert len(fake.simple_calls[0]) == size


def test_rapid_hit_big_skips_multipart_and_simple(db, monkeypatch):
    _enable(monkeypatch)
    size = _LIMIT + 1
    item = _item(db, size=size, content_hash=_PAYLOAD_SHA1,
                 content_hash_algo="sha1")
    _binding(db, 7)
    job = _cross_job(db, item.id)
    fake = _FakeUser115(rapid="hit")
    _patch_cross(monkeypatch, fake)
    sa = _FakeSA(size=size)
    res = tv.run_transfer_job_once(db, job, sa)
    assert res["outcome"] == "done"
    assert fake.multipart_calls == [] and fake.simple_calls == []
    assert sa.download_calls == 0  # 命中不搬一字节、也不算哈希


# ---------- 分片失败口径：不登记变体、error_kind 细分 ----------

def test_multipart_part_failure_fails_job_no_variant(db, monkeypatch):
    # 与 simple 腿同一失败口径：Pan115Error 走有界重试，attempts
    # 耗尽后 failed/retriable；全程不登记变体、不回落 simple。
    _enable(monkeypatch)
    size = _LIMIT + 1
    item = _item(db, size=size, content_hash=_PAYLOAD_SHA1,
                 content_hash_algo="sha1")
    _binding(db, 7)
    job = _cross_job(db, item.id)
    job.attempts = tv.MAX_ATTEMPTS - 1  # 门控在 attempts>=MAX 时直接终态，预设 MAX 会截走本次运行、丢分片文案；留一次让本次真实走完分片失败再耗尽
    db.commit()
    fake = _FakeUser115(rapid="miss", multipart="error")
    _patch_cross(monkeypatch, fake)
    res = tv.run_transfer_job_once(db, job, _FakeSA(size=size))
    assert res["outcome"] == "failed"
    db.refresh(job)
    assert job.error_kind == "retriable"
    assert "分片" in (job.error_detail or "") or \
        "OSS" in (job.error_detail or "")
    assert _variant_rows(db) == []
    assert fake.simple_calls == []  # 失败不静默回落 simple（也装不下）


def test_multipart_dir_match_failure_copy_unresolved(db, monkeypatch):
    _enable(monkeypatch)
    size = _LIMIT + 1
    item = _item(db, size=size, content_hash=_PAYLOAD_SHA1,
                 content_hash_algo="sha1")
    _binding(db, 7)
    job = _cross_job(db, item.id)
    fake = _FakeUser115(rapid="miss", multipart="no_place")
    _patch_cross(monkeypatch, fake)
    res = tv.run_transfer_job_once(db, job, _FakeSA(size=size))
    assert res["outcome"] == "failed"
    assert res["error_kind"] == "copy_unresolved"
    assert _variant_rows(db) == []


def test_multipart_sha1_mismatch_fails(db, monkeypatch):
    # 分片路从严：回调 sha1 与源端透传 sha1 不一致 = 落盘字节与源
    # 不是同一份（多段拼接），判失败不登记（与 simple 路回调优先
    # 的口径区别在此，分片拼接没有「以回调为准」的余地）。
    _enable(monkeypatch)
    size = _LIMIT + 1
    stream_sha1 = hashlib.sha1(_PAYLOAD[:size]).hexdigest()
    item = _item(db, size=size, content_hash=stream_sha1,
                 content_hash_algo="sha1")
    _binding(db, 7)
    job = _cross_job(db, item.id)
    wrong = hashlib.sha1(b"someone-elses-bytes").hexdigest()
    fake = _FakeUser115(rapid="miss", multipart="read_all",
                        callback_sha1=wrong)
    _patch_cross(monkeypatch, fake)
    res = tv.run_transfer_job_once(db, job, _FakeSA(size=size))
    assert res["outcome"] == "failed"
    assert res["error_kind"] == "remote_error"
    db.refresh(job)
    assert "sha1" in (job.error_detail or "")
    assert _variant_rows(db) == []


def test_multipart_auth_error_marks_binding(db, monkeypatch):
    _enable(monkeypatch)
    size = _LIMIT + 1
    item = _item(db, size=size, content_hash=_PAYLOAD_SHA1,
                 content_hash_algo="sha1")
    binding = _binding(db, 7)
    job = _cross_job(db, item.id)
    fake = _FakeUser115(rapid="miss", multipart="auth")
    _patch_cross(monkeypatch, fake)
    res = tv.run_transfer_job_once(db, job, _FakeSA(size=size))
    assert res["outcome"] == "failed"
    assert res["error_kind"] == "auth_error"
    db.refresh(binding)
    assert binding.status != "active"
    assert _variant_rows(db) == []


# ---------- 无可信 sha1 的大文件：先流式算哈希，再试秒传/分片 ----------

def test_no_sha1_big_hash_pass_then_multipart(db, monkeypatch):
    _enable(monkeypatch)
    size = _LIMIT + 1
    expect_sha1 = hashlib.sha1(_PAYLOAD[:size]).hexdigest()
    item = _item(db, size=size)  # 无 content_hash
    _binding(db, 7)
    job = _cross_job(db, item.id)
    fake = _FakeUser115(rapid="miss")
    _patch_cross(monkeypatch, fake)
    sa = _FakeSA(size=size)
    res = tv.run_transfer_job_once(db, job, sa)
    assert res["outcome"] == "done"
    assert sa.download_calls > 0  # 哈希遍真过了源字节
    # 算完的 sha1 先喂秒传、再喂分片（顺序不变）
    assert fake.rapid_calls[0][2] == expect_sha1
    assert fake.multipart_calls[0][2] == expect_sha1
    db.refresh(item)
    assert item.content_hash_algo == "sha1"
    assert item.content_hash == expect_sha1  # 沉淀给下一单


def test_no_sha1_small_no_hash_pass(db, monkeypatch):
    # 小文件维持现行口径：不为算哈希多下一整遍给 simple 添堵——
    # 哈希只在流经 simple 的那一程顺手沉淀（rapid 文件已有同名
    # 用例，此处只钉「无 sha1 小文件不试秒传、直接 simple」）。
    _enable(monkeypatch)
    size = _LIMIT
    item = _item(db, size=size)
    _binding(db, 7)
    job = _cross_job(db, item.id)
    fake = _FakeUser115(rapid="hit")
    _patch_cross(monkeypatch, fake)
    res = tv.run_transfer_job_once(db, job, _FakeSA(size=size))
    assert res["outcome"] == "done"
    assert fake.rapid_calls == []
    assert fake.multipart_calls == []
    assert len(fake.simple_calls) == 1


# ---------- 开关：pan115_multipart_upload_enabled（#1454 口径） ----------

def test_multipart_switch_default_on(db):
    from backend.config_schema import TRANSFER_CONFIG_SCHEMA
    assert TRANSFER_CONFIG_SCHEMA["pan115_multipart_upload_enabled"] == ("bool",)
    assert tv.pan115_multipart_upload_enabled(db) is True
    payload = tv.transfer_config_payload(db)
    assert payload["pan115_multipart_upload_enabled"] is True


def test_multipart_switch_off_big_unsupported_no_hash_pass(db, monkeypatch):
    _enable(monkeypatch)
    tv.write_transfer_config(db, pan115_multipart_upload_enabled=False)
    assert tv.pan115_multipart_upload_enabled(db) is False
    size = _LIMIT + 1
    item = _item(db, size=size)  # 无 sha1：开关关时不许白算哈希遍
    _binding(db, 7)
    job = _cross_job(db, item.id)
    fake = _FakeUser115(rapid="miss")
    _patch_cross(monkeypatch, fake)
    sa = _FakeSA(size=size)
    res = tv.run_transfer_job_once(db, job, sa)
    assert res["outcome"] == "failed"
    assert res["error_kind"] == "unsupported"
    assert sa.download_calls == 0
    assert fake.multipart_calls == [] and fake.simple_calls == []


def test_multipart_switch_off_with_sha1_rapid_still_tried(db, monkeypatch):
    # 开关只管分片路：有可信 sha1 时秒传照试，命中与大小无关。
    _enable(monkeypatch)
    tv.write_transfer_config(db, pan115_multipart_upload_enabled=False)
    size = _LIMIT + 1
    item = _item(db, size=size, content_hash=_PAYLOAD_SHA1,
                 content_hash_algo="sha1")
    _binding(db, 7)
    job = _cross_job(db, item.id)
    fake = _FakeUser115(rapid="hit")
    _patch_cross(monkeypatch, fake)
    res = tv.run_transfer_job_once(db, job, _FakeSA(size=size))
    assert res["outcome"] == "done"
    assert len(fake.rapid_calls) == 1
    assert fake.multipart_calls == []


# ---------- 客户端协议（p115oss/py115 对照，测试侧独立加解密） ----------

_TEST_AES_KEY = bytes([0xfb, 0x1a, 0x19, 0xd6, 0x52, 0xf5, 0xaa, 0xf7,
                       0xbc, 0x65, 0x1d, 0x0f, 0x69, 0xbf, 0x42, 0x2f])
_TEST_AES_IV = bytes([0x69, 0xbf, 0x42, 0x2f, 0x49, 0x96, 0x05, 0x50,
                      0xa0, 0xad, 0x44, 0xec, 0x34, 0x46, 0xcb, 0x4c])


def _test_aes_encrypt(data: bytes) -> bytes:
    from cryptography.hazmat.primitives.ciphers import (
        Cipher, algorithms, modes)
    pad = 16 - (len(data) % 16)
    data = data + bytes([pad]) * pad
    enc = Cipher(algorithms.AES(_TEST_AES_KEY),
                 modes.CBC(_TEST_AES_IV)).encryptor()
    return enc.update(data) + enc.finalize()


def _test_aes_decrypt(data: bytes) -> bytes:
    from cryptography.hazmat.primitives.ciphers import (
        Cipher, algorithms, modes)
    dec = Cipher(algorithms.AES(_TEST_AES_KEY),
                 modes.CBC(_TEST_AES_IV)).decryptor()
    raw = dec.update(data) + dec.finalize()
    return raw[:-raw[-1]]


def _test_framed_lz4_encode(data: bytes) -> bytes:
    out = bytearray()
    for i in range(0, len(data), 0x2000):
        chunk = data[i:i + 0x2000]
        block = bytearray()
        n = len(chunk)
        if n < 15:
            block.append(n << 4)
        else:
            block.append(0xF0)
            rest = n - 15
            while rest >= 255:
                block.append(255)
                rest -= 255
            block.append(rest)
        block += chunk
        out += len(block).to_bytes(2, "little")
        out += block
    return bytes(out)


def _test_encrypt_response(obj) -> bytes:
    return _test_aes_encrypt(
        _test_framed_lz4_encode(json.dumps(obj).encode()))


_CALLBACK = {
    "callback": json.dumps({
        "callbackUrl": "https://callback.115.com/api",
        "callbackBody": "bucket=${bucket}&object=${object}&sha1=${sha1}",
        "callbackBodyType": "application/x-www-form-urlencoded",
    }),
    "callback_var": json.dumps({"x:my_var1": "value1"}),
}
_INIT_OK = {"status": 1, "bucket": "fhnfile", "object": "obj/key.bin",
            "callback": _CALLBACK, "pickcode": "pcinit"}


class _FakeOSS:
    """OSS 桩：按 method + query 分发，记录全部请求，可配失败点。"""

    def __init__(self, payload_sha1, fail_part=None, bad_etag=False):
        self.payload_sha1 = payload_sha1
        self.fail_part = fail_part
        self.bad_etag = bad_etag
        self.requests = []  # [{method, query(dict), headers, data}]

    def __call__(self, method, url, headers=None, data=None, **kw):
        query = dict(parse_qsl(urlsplit(url).query))
        self.requests.append({"method": method, "url": url, "query": query,
                              "headers": dict(headers or {}), "data": data})
        if "uploads" in query:  # InitiateMultipartUpload
            body = (b"<InitiateMultipartUploadResult>"
                    b"<Bucket>fhnfile</Bucket><Key>obj/key.bin</Key>"
                    b"<UploadId>UPLOAD-42</UploadId>"
                    b"</InitiateMultipartUploadResult>")
            return 200, {}, body
        if method == "PUT":
            number = int(query["partNumber"])
            if self.fail_part == number:
                raise transfer115.Pan115Error("115 OSS 返回 HTTP 500")
            md5 = hashlib.md5(data).hexdigest().upper()
            if self.bad_etag:
                md5 = "0" * 32
            return 200, {"etag": f'"{md5}"'}, b""
        if method == "DELETE":
            return 204, {}, b""
        if method == "POST" and "uploadId" in query:  # Complete
            body = json.dumps({"data": {
                "file_id": "mpfid", "pick_code": "pcmp",
                "sha1": self.payload_sha1}}).encode()
            return 200, {}, body
        raise AssertionError(f"未预期 OSS 请求：{method} {url}")

    def by_method(self, method):
        return [r for r in self.requests if r["method"] == method]


def _patch_client_http(monkeypatch, init_responses, oss):
    """桩住客户端三条 HTTP 通道：_http_json（uploadinfo/gettoken）、
    _http_bytes（initupload 加密响应队列）、_http_full（OSS）。"""
    json_calls = []

    def _fake_json(method, url, params=None, data=None, **kw):
        json_calls.append(url)
        if "gettoken" in url:
            return {"AccessKeyId": "AKID-TEST",
                    "AccessKeySecret": "SECRET-TEST",
                    "SecurityToken": "STOKEN-TEST",
                    "Expiration": "2099-01-01T00:00:00Z"}
        return {"user_id": "12345", "userkey": "uk-test"}

    monkeypatch.setattr(transfer115, "_http_json", _fake_json)
    queue = list(init_responses)
    byte_calls = []

    def _fake_bytes(method, url, params=None, data=None, **kw):
        byte_calls.append({"params": params, "data": data})
        return _test_encrypt_response(queue.pop(0))

    monkeypatch.setattr(transfer115, "_http_bytes", _fake_bytes)
    monkeypatch.setattr(transfer115, "_http_full", oss)
    return SimpleNamespace(json_calls=json_calls, byte_calls=byte_calls)


def test_determine_partsize_math():
    f = transfer115._determine_partsize
    min10 = 10 * 1024 * 1024
    assert f(0) == min10
    assert f(123) == min10
    # 恰在上限内：分片数不超 10000 且大小是下限的 2 的幂倍
    big = 5 * 1024 ** 3 + 1
    ps = f(big)
    assert ps >= min10 and (ps // min10) & (ps // min10 - 1) == 0
    assert -(-big // ps) <= 10000
    # p115oss 原算法口径（下限 100 KiB）逐值对照
    assert f(100 * 1024, min_partsize=100 * 1024) == 100 * 1024
    assert f(100 * 1024 * 10000 + 1,
             min_partsize=100 * 1024) == 200 * 1024
    assert f(100 * 1024 * 10000,
             min_partsize=100 * 1024) == 100 * 1024


def test_multipart_client_flow_and_slicing(monkeypatch):
    monkeypatch.setattr(transfer115, "_MULTIPART_MIN_PART_SIZE", 1024)
    payload = bytes(range(256)) * 8 + b"tail!"  # 2053 = 1024*2 + 5
    payload = payload[:1024 * 2 + 5]
    sha1 = hashlib.sha1(payload).hexdigest()
    oss = _FakeOSS(payload_sha1=sha1)
    http = _patch_client_http(monkeypatch, [_INIT_OK], oss)
    client = transfer115.Pan115Client("COOKIE")
    out = client.multipart_upload(io.BytesIO(payload), "a.mkv",
                                  len(payload), sha1, "900")
    assert out["outcome"] == "uploaded"
    assert out["fid"] == "mpfid" and out["pickcode"] == "pcmp"
    assert out["sha1"] == sha1
    # gettoken 真取过
    assert any("gettoken" in u for u in http.json_calls)
    # OSS 三步齐全且顺序：init(POST uploads) → PUT×3 → complete(POST uploadId)
    kinds = []
    for r in oss.requests:
        if "uploads" in r["query"]:
            kinds.append("init")
        elif r["method"] == "PUT":
            kinds.append("put")
        elif r["method"] == "POST":
            kinds.append("complete")
    assert kinds == ["init", "put", "put", "put", "complete"]
    init_req = oss.requests[0]
    assert init_req["query"].get("sequential") == "1"
    assert init_req["headers"]["x-oss-security-token"] == "STOKEN-TEST"
    assert init_req["headers"]["Authorization"].startswith("OSS AKID-TEST:")
    # 切片数学：[1024, 1024, 5]，字节逐片对得上源
    puts = oss.by_method("PUT")
    assert [len(p["data"]) for p in puts] == [1024, 1024, 5]
    assert b"".join(p["data"] for p in puts) == payload
    assert [p["query"]["partNumber"] for p in puts] == ["1", "2", "3"]
    assert all(p["query"]["uploadId"] == "UPLOAD-42" for p in puts)
    # complete：XML 清单含全部分片 ETag；callback 头 base64 解开后
    # ${sha1} 已替换为整文件 sha1（大写，与 fileid 同口径）
    comp = [r for r in oss.requests
            if r["method"] == "POST" and "uploadId" in r["query"]][0]
    body = comp["data"].decode()
    for p in puts:
        md5 = hashlib.md5(p["data"]).hexdigest().upper()
        assert f"<ETag>\"{md5}\"</ETag>" in body
    cb = json.loads(base64.b64decode(
        comp["headers"]["x-oss-callback"]).decode())
    assert sha1.upper() in cb["callbackBody"]
    assert "${sha1}" not in cb["callbackBody"]
    cbv = json.loads(base64.b64decode(
        comp["headers"]["x-oss-callback-var"]).decode())
    assert cbv == {"x:my_var1": "value1"}
    assert oss.by_method("DELETE") == []  # 成功不 abort


def test_multipart_client_exact_multiple_no_empty_tail(monkeypatch):
    monkeypatch.setattr(transfer115, "_MULTIPART_MIN_PART_SIZE", 1024)
    payload = bytes(range(256)) * 8  # 恰好 2 片
    sha1 = hashlib.sha1(payload).hexdigest()
    oss = _FakeOSS(payload_sha1=sha1)
    _patch_client_http(monkeypatch, [_INIT_OK], oss)
    client = transfer115.Pan115Client("COOKIE")
    out = client.multipart_upload(io.BytesIO(payload), "a.mkv",
                                  len(payload), sha1, "900")
    assert out["outcome"] == "uploaded"
    puts = oss.by_method("PUT")
    assert [len(p["data"]) for p in puts] == [1024, 1024]


def test_multipart_client_init_hit_no_oss(monkeypatch):
    oss = _FakeOSS(payload_sha1=_PAYLOAD_SHA1)
    http = _patch_client_http(
        monkeypatch,
        [{"status": 2, "pickcode": "pcrapid", "file_id": "rapidfid"}], oss)
    client = transfer115.Pan115Client("COOKIE")
    out = client.multipart_upload(io.BytesIO(_PAYLOAD), "a.mkv",
                                  len(_PAYLOAD), _PAYLOAD_SHA1, "900")
    assert out["outcome"] == "hit"
    assert out["fid"] == "rapidfid" and out["pickcode"] == "pcrapid"
    assert oss.requests == []  # 没碰 OSS
    assert not any("gettoken" in u for u in http.json_calls)


def test_multipart_client_sign_check_then_upload(monkeypatch):
    monkeypatch.setattr(transfer115, "_MULTIPART_MIN_PART_SIZE", 1024)
    payload = bytes(range(256)) * 4
    sha1 = hashlib.sha1(payload).hexdigest()
    oss = _FakeOSS(payload_sha1=sha1)
    http = _patch_client_http(
        monkeypatch,
        [{"status": 7, "sign_key": "SK1", "sign_check": "10-19"}, _INIT_OK],
        oss)
    client = transfer115.Pan115Client("COOKIE")
    out = client.multipart_upload(
        io.BytesIO(payload), "a.mkv", len(payload), sha1, "900",
        range_reader=lambda s, e: payload[s:e + 1])
    assert out["outcome"] == "uploaded"
    assert len(http.byte_calls) == 2
    body2 = dict(parse_qsl(
        _test_aes_decrypt(http.byte_calls[1]["data"]).decode()))
    assert body2["sign_key"] == "SK1"
    assert body2["sign_val"] == hashlib.sha1(
        payload[10:20]).hexdigest().upper()


def test_multipart_client_part_failure_retries_and_aborts(monkeypatch):
    monkeypatch.setattr(transfer115, "_MULTIPART_MIN_PART_SIZE", 1024)
    payload = bytes(range(256)) * 12  # 3 片
    sha1 = hashlib.sha1(payload).hexdigest()
    oss = _FakeOSS(payload_sha1=sha1, fail_part=2)
    _patch_client_http(monkeypatch, [_INIT_OK], oss)
    client = transfer115.Pan115Client("COOKIE")
    with pytest.raises(transfer115.Pan115Error):
        client.multipart_upload(io.BytesIO(payload), "a.mkv",
                                len(payload), sha1, "900")
    part2 = [r for r in oss.by_method("PUT")
             if r["query"]["partNumber"] == "2"]
    assert len(part2) == transfer115._MULTIPART_PART_ATTEMPTS
    assert len(oss.by_method("DELETE")) == 1  # abort 打过
    completes = [r for r in oss.requests
                 if r["method"] == "POST" and "uploadId" in r["query"]]
    assert completes == []  # 未 complete 不冒充完成


def test_multipart_client_etag_mismatch_fails_and_aborts(monkeypatch):
    monkeypatch.setattr(transfer115, "_MULTIPART_MIN_PART_SIZE", 1024)
    payload = bytes(range(256)) * 4
    sha1 = hashlib.sha1(payload).hexdigest()
    oss = _FakeOSS(payload_sha1=sha1, bad_etag=True)
    _patch_client_http(monkeypatch, [_INIT_OK], oss)
    client = transfer115.Pan115Client("COOKIE")
    with pytest.raises(transfer115.Pan115Error):
        client.multipart_upload(io.BytesIO(payload), "a.mkv",
                                len(payload), sha1, "900")
    assert len(oss.by_method("DELETE")) == 1


def test_multipart_client_missing_callback_fails_before_parts(monkeypatch):
    oss = _FakeOSS(payload_sha1=_PAYLOAD_SHA1)
    _patch_client_http(
        monkeypatch,
        [{"status": 1, "bucket": "fhnfile", "object": "obj/key.bin"}], oss)
    client = transfer115.Pan115Client("COOKIE")
    with pytest.raises(transfer115.Pan115Error):
        client.multipart_upload(io.BytesIO(_PAYLOAD), "a.mkv",
                                len(_PAYLOAD), _PAYLOAD_SHA1, "900")
    assert oss.by_method("PUT") == []  # 没白传一片


# ---------- 片间令牌刷新＋会话令牌记账（换代第一刀 step 1） ----------
# 背景（可行性报告一.2/八.2）：upload_token 此前丢弃 STS Expiration，
# 一次 multipart 全程只领一枚令牌用到底——2026-10-31 生产一单在
# part 188 撞 SecurityTokenExpired、三试同令牌全败（会话本身不随
# 令牌死）。本节钉：part PUT / complete 撞令牌过期先刷新再重试该步；
# 每片至多刷新 1 次、整单有总闸；非过期错误不触发刷新；刷新也救
# 不回时按原口径失败 abort。会话 dict 带令牌领取/到期时刻，经既有
# on_progress 通道由调用方 sidecar 落盘（本模块不另起存储）。

_EXPIRED_MSG = (
    "115 OSS 返回 HTTP 403：b'<Error><Code>SecurityTokenExpired</Code>"
    "<Message>The security token has expired</Message></Error>'")


class _FakeOSSExpiring(_FakeOSS):
    """OSS 桩扩展：指定片/complete 的前 N 次请求撞令牌过期。

    expire_plan 键 ("part", 片号) / ("complete", 0)，值是过期次数；
    命中时抛与 _http_full 生产文案同形的 Pan115Error。seen_tokens
    记每次 PUT/complete 实际所带 x-oss-security-token，供断言刷新
    后确实换了新令牌在传。
    """

    def __init__(self, payload_sha1, expire_plan=None, **kw):
        super().__init__(payload_sha1, **kw)
        self.expire_plan = dict(expire_plan or {})
        self.seen_tokens = []

    def __call__(self, method, url, headers=None, data=None, **kw):
        query = dict(parse_qsl(urlsplit(url).query))
        key = None
        if method == "PUT":
            key = ("part", int(query["partNumber"]))
        elif method == "POST" and "uploadId" in query:
            key = ("complete", 0)
        if key is not None:
            self.seen_tokens.append(
                (key, (headers or {}).get("x-oss-security-token")))
            left = self.expire_plan.get(key, 0)
            if left > 0:
                self.expire_plan[key] = left - 1
                raise transfer115.Pan115Error(_EXPIRED_MSG)
        return super().__call__(method, url, headers=headers,
                               data=data, **kw)


class _FakeOSSResume(_FakeOSSExpiring):
    """过期桩之上加 ListParts：按 saved_parts 回 XML 已成片清单。"""

    def __init__(self, payload_sha1, saved_parts, **kw):
        super().__init__(payload_sha1, **kw)
        self.saved_parts = saved_parts  # [(片号, ETag)]

    def __call__(self, method, url, headers=None, data=None, **kw):
        if method == "GET":
            items = "".join(
                f"<Part><PartNumber>{n}</PartNumber>"
                f"<ETag>{e}</ETag></Part>"
                for n, e in self.saved_parts)
            body = (f"<ListPartsResult>{items}"
                    "<IsTruncated>false</IsTruncated>"
                    "</ListPartsResult>").encode()
            return 200, {}, body
        return super().__call__(method, url, headers=headers,
                               data=data, **kw)


def _patch_client_http_tokens(monkeypatch, init_responses, oss,
                              expirations=()):
    """同 _patch_client_http，但 gettoken 每次回不同凭据
    （AKID-n/STOKEN-n）与 expirations 队列里的到期时刻，以便断言
    刷新次数与刷新后所带令牌。"""
    json_calls = []
    state = {"n": 0}
    exps = list(expirations)

    def _fake_json(method, url, params=None, data=None, **kw):
        json_calls.append(url)
        if "gettoken" in url:
            state["n"] += 1
            n = state["n"]
            return {"AccessKeyId": f"AKID-{n}",
                    "AccessKeySecret": f"SECRET-{n}",
                    "SecurityToken": f"STOKEN-{n}",
                    "Expiration": exps.pop(0) if exps
                    else "2099-01-01T00:00:00Z"}
        return {"user_id": "12345", "userkey": "uk-test"}

    monkeypatch.setattr(transfer115, "_http_json", _fake_json)
    queue = list(init_responses)

    def _fake_bytes(method, url, params=None, data=None, **kw):
        return _test_encrypt_response(queue.pop(0))

    monkeypatch.setattr(transfer115, "_http_bytes", _fake_bytes)
    monkeypatch.setattr(transfer115, "_http_full", oss)
    return SimpleNamespace(
        json_calls=json_calls, gettoken_count=lambda: state["n"])


def test_upload_token_returns_expiration(monkeypatch):
    oss = _FakeOSS(payload_sha1=_PAYLOAD_SHA1)
    _patch_client_http_tokens(monkeypatch, [], oss,
                              ["2031-05-06T07:08:09Z"])
    client = transfer115.Pan115Client("COOKIE")
    token = client.upload_token()
    assert token["AccessKeyId"] == "AKID-1"
    assert token["SecurityToken"] == "STOKEN-1"
    assert token["Expiration"] == "2031-05-06T07:08:09Z"


def test_multipart_part_token_expired_refreshes_and_succeeds(monkeypatch):
    monkeypatch.setattr(transfer115, "_MULTIPART_MIN_PART_SIZE", 1024)
    payload = bytes(range(256)) * 12  # 3 片
    sha1 = hashlib.sha1(payload).hexdigest()
    oss = _FakeOSSExpiring(payload_sha1=sha1, expire_plan={("part", 2): 1})
    http = _patch_client_http_tokens(
        monkeypatch, [_INIT_OK], oss,
        ["2030-01-01T00:00:00Z", "2030-01-02T00:00:00Z"])
    client = transfer115.Pan115Client("COOKIE")
    out = client.multipart_upload(io.BytesIO(payload), "a.mkv",
                                  len(payload), sha1, "900")
    assert out["outcome"] == "uploaded"
    assert http.gettoken_count() == 2  # 初始 1 + 片间刷新 1
    part2_tokens = [t for (k, t) in oss.seen_tokens if k == ("part", 2)]
    # 第 2 片先撞过期、刷新后带新令牌重传成功
    assert part2_tokens == ["STOKEN-1", "STOKEN-2"]
    assert oss.by_method("DELETE") == []  # 自愈成功不 abort


def test_multipart_part_token_still_expired_after_refresh_bounded(monkeypatch):
    # 刷新后仍过期：每片只刷新 1 次，其后按原 3 次尝试口径失败并
    # abort——不许无界领令牌、不许无界重试。
    monkeypatch.setattr(transfer115, "_MULTIPART_MIN_PART_SIZE", 1024)
    payload = bytes(range(256)) * 12
    sha1 = hashlib.sha1(payload).hexdigest()
    oss = _FakeOSSExpiring(payload_sha1=sha1, expire_plan={("part", 2): 99})
    http = _patch_client_http_tokens(monkeypatch, [_INIT_OK], oss)
    client = transfer115.Pan115Client("COOKIE")
    with pytest.raises(transfer115.Pan115Error):
        client.multipart_upload(io.BytesIO(payload), "a.mkv",
                                len(payload), sha1, "900")
    assert http.gettoken_count() == 2  # 初始 + 本片唯一一次刷新
    part2_tokens = [t for (k, t) in oss.seen_tokens if k == ("part", 2)]
    # 刷新前 1 次尝试 + 刷新后按常规尝试上限收尾
    assert len(part2_tokens) == 1 + transfer115._MULTIPART_PART_ATTEMPTS
    assert part2_tokens[0] == "STOKEN-1"
    assert set(part2_tokens[1:]) == {"STOKEN-2"}
    assert len(oss.by_method("DELETE")) == 1  # 按原口径 abort


def test_multipart_part_plain_error_never_refreshes(monkeypatch):
    monkeypatch.setattr(transfer115, "_MULTIPART_MIN_PART_SIZE", 1024)
    payload = bytes(range(256)) * 12
    sha1 = hashlib.sha1(payload).hexdigest()
    oss = _FakeOSS(payload_sha1=sha1, fail_part=2)
    http = _patch_client_http_tokens(monkeypatch, [_INIT_OK], oss)
    client = transfer115.Pan115Client("COOKIE")
    with pytest.raises(transfer115.Pan115Error):
        client.multipart_upload(io.BytesIO(payload), "a.mkv",
                                len(payload), sha1, "900")
    assert http.gettoken_count() == 1  # 非过期错误不触发刷新


def test_multipart_complete_token_expired_refreshes(monkeypatch):
    monkeypatch.setattr(transfer115, "_MULTIPART_MIN_PART_SIZE", 1024)
    payload = bytes(range(256)) * 4
    sha1 = hashlib.sha1(payload).hexdigest()
    oss = _FakeOSSExpiring(payload_sha1=sha1,
                           expire_plan={("complete", 0): 1})
    http = _patch_client_http_tokens(monkeypatch, [_INIT_OK], oss)
    client = transfer115.Pan115Client("COOKIE")
    out = client.multipart_upload(io.BytesIO(payload), "a.mkv",
                                  len(payload), sha1, "900")
    assert out["outcome"] == "uploaded"
    assert http.gettoken_count() == 2
    comp_tokens = [t for (k, t) in oss.seen_tokens
                   if k == ("complete", 0)]
    assert comp_tokens == ["STOKEN-1", "STOKEN-2"]
    assert oss.by_method("DELETE") == []


def test_multipart_resume_part_token_expired_refreshes(monkeypatch):
    monkeypatch.setattr(transfer115, "_MULTIPART_MIN_PART_SIZE", 1024)
    payload = bytes(range(256)) * 12  # 3 片，续传时第 1 片已成
    sha1 = hashlib.sha1(payload).hexdigest()
    md5_1 = hashlib.md5(payload[:1024]).hexdigest().upper()
    oss = _FakeOSSResume(payload_sha1=sha1, saved_parts=[(1, f'"{md5_1}"')],
                         expire_plan={("part", 2): 1})
    http = _patch_client_http_tokens(monkeypatch, [], oss)
    session = {"bucket": "fhnfile", "object_key": "obj/key.bin",
               "upload_id": "UPLOAD-42", "callback": _CALLBACK,
               "parts": [{"PartNumber": 1, "ETag": f'"{md5_1}"'}]}
    client = transfer115.Pan115Client("COOKIE")
    out = client.multipart_upload(io.BytesIO(payload), "a.mkv",
                                  len(payload), sha1, "900",
                                  resume_session=session)
    assert out["outcome"] == "uploaded"
    assert http.gettoken_count() == 2  # 续传取凭据 1 + 刷新 1
    put_numbers = [k[1] for (k, _t) in oss.seen_tokens if k[0] == "part"]
    assert put_numbers == [2, 2, 3]  # 第 2 片过期一次后重传，再传第 3 片


def test_multipart_session_carries_token_metadata(monkeypatch):
    # 交付状态落盘（本模块侧）：会话 dict 带令牌领取时刻与到期时刻，
    # 经既有 on_progress 通道落进调用方 sidecar；刷新后 failed 事件
    # 里的会话须带刷新后那枚的到期时刻，init 事件仍是初始那枚。
    monkeypatch.setattr(transfer115, "_MULTIPART_MIN_PART_SIZE", 1024)
    payload = bytes(range(256)) * 12  # 3 片
    sha1 = hashlib.sha1(payload).hexdigest()
    oss = _FakeOSSExpiring(payload_sha1=sha1, fail_part=3,
                           expire_plan={("part", 2): 1})
    _patch_client_http_tokens(
        monkeypatch, [_INIT_OK], oss,
        ["2030-01-01T00:00:00Z", "2030-01-02T00:00:00Z"])
    events = []
    client = transfer115.Pan115Client("COOKIE")
    with pytest.raises(transfer115.Pan115Error):
        client.multipart_upload(io.BytesIO(payload), "a.mkv",
                                len(payload), sha1, "900",
                                on_progress=events.append)
    init_ev = [e for e in events if e.get("event") == "init"][0]
    assert init_ev["session"]["token"]["expiration"] == \
        "2030-01-01T00:00:00Z"
    assert isinstance(init_ev["session"]["token"]["fetched_at"], int)
    failed_ev = [e for e in events if e.get("event") == "failed"][0]
    assert failed_ev["session"]["token"]["expiration"] == \
        "2030-01-02T00:00:00Z"
    assert [p["PartNumber"]
            for p in failed_ev["session"]["parts"]] == [1, 2]
    assert oss.by_method("DELETE") == []  # 有 on_progress 时不 abort（既有口径）
