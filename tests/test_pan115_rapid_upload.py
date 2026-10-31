# -*- coding: utf-8 -*-
"""115 秒传整套（2026-10-30 用户亲点整套派单：记哈希＋秒传协议）。

全部离线：115 initupload 协议、用户 115 客户端、SA 下载一律桩替身；
协议加密/解密在测试侧用 cryptography 按 p115cipher 0.0.6 公开常量
独立实现，与被测实现互为交叉核对。真实端到端未实测（不许用生产
用户 Cookie 做开发测试），上线后等真实 pan115 用户触发观察。
"""
import os

os.environ.setdefault("DATABASE_TYPE", "sqlite")
os.environ.setdefault("REDIS_ENABLED", "false")

import hashlib
from types import SimpleNamespace
from urllib.parse import parse_qsl

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
_PAYLOAD = bytes(range(256)) * 4  # 1024 字节
_PAYLOAD_SHA1 = hashlib.sha1(_PAYLOAD).hexdigest()
_BIG_SIZE = tv._115_SIMPLE_UPLOAD_LIMIT + 1


def _item(db, **kw):
    _counter[0] += 1
    row = em.MediaItem(guid=f"rapid-{_counter[0]}", library_id=1,
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
    """用户侧 115 桩：转存目录预建；rapid/simple 行为可配、全程可观测。"""

    def __init__(self, rapid="hit", callback_sha1=None):
        self.dirs = {
            "0": [{"name": "aetrix-transfer", "is_dir": True, "cid": "900"}],
            "900": [],
        }
        self.rapid_behavior = rapid
        self.callback_sha1 = callback_sha1
        self.rapid_calls = []
        self.simple_calls = []
        self.multipart_calls = []
        self.sign_ranges = []

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
        if self.rapid_behavior == "auth":
            raise transfer115.Pan115AuthError("Cookie 已失效")
        if self.rapid_behavior == "error":
            raise transfer115.Pan115Error("协议炸了")
        if self.rapid_behavior == "miss":
            return {"outcome": "miss", "fid": "", "pickcode": "", "raw": {}}
        if self.rapid_behavior == "sign_then_hit":
            assert range_reader is not None
            data = range_reader(0, 99)
            self.sign_ranges.append(data)
        self._place(filename, "rapidfid", "pcrapid", filesize, file_sha1)
        return {"outcome": "hit", "fid": "rapidfid", "pickcode": "pcrapid",
                "raw": {"status": 2}}

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
        self._place(filename, "mpfid", "pcmp", filesize, file_sha1)
        return {"outcome": "uploaded", "fid": "mpfid", "pickcode": "pcmp",
                "sha1": file_sha1, "raw": {}}


def _patch_cross(monkeypatch, fake115):
    monkeypatch.setattr(tv, "_make_pan115_client", lambda cookie: fake115)
    monkeypatch.setattr(ub, "binding_115_cookie",
                        lambda db_arg, user_id, binding_id: "COOKIE")


# ---------- ① 全量上传后 sha1 沉淀 ----------

def test_cross_upload_persists_sha1(db, monkeypatch):
    _enable(monkeypatch)
    item = _item(db)
    _binding(db, 7)
    job = _cross_job(db, item.id)
    fake = _FakeUser115()
    _patch_cross(monkeypatch, fake)
    res = tv.run_transfer_job_once(db, job, _FakeSA())
    assert res["outcome"] == "done"
    assert fake.simple_calls == [_PAYLOAD]  # 字节真流过一遍
    assert fake.rapid_calls == []  # 无存量 sha1 时不试秒传、不多下整遍
    db.refresh(item)
    assert item.content_hash_algo == "sha1"
    assert item.content_hash == _PAYLOAD_SHA1


def test_cross_upload_callback_sha1_wins_over_stream(db, monkeypatch):
    _enable(monkeypatch)
    item = _item(db)
    _binding(db, 7)
    job = _cross_job(db, item.id)
    other = hashlib.sha1(b"other-bytes").hexdigest()
    fake = _FakeUser115(callback_sha1=other)
    _patch_cross(monkeypatch, fake)
    res = tv.run_transfer_job_once(db, job, _FakeSA())
    assert res["outcome"] == "done"
    db.refresh(item)
    # 取舍：回调是 115 对实际落盘字节算的哈希（目标端事实），赢源端透传值
    assert item.content_hash == other


def test_cross_upload_md5_overwritten_by_sha1(db, monkeypatch):
    _enable(monkeypatch)
    item = _item(db, content_hash=hashlib.md5(_PAYLOAD).hexdigest(),
                 content_hash_algo="md5")
    _binding(db, 7)
    job = _cross_job(db, item.id)
    fake = _FakeUser115()
    _patch_cross(monkeypatch, fake)
    res = tv.run_transfer_job_once(db, job, _FakeSA())
    assert res["outcome"] == "done"
    assert fake.rapid_calls == []  # md5 不是可信 sha1，不许拿去试秒传
    db.refresh(item)
    assert item.content_hash_algo == "sha1"
    assert item.content_hash == _PAYLOAD_SHA1


# ---------- ② 秒传命中不消费字节读取器 ----------

def test_cross_rapid_hit_skips_byte_reader(db, monkeypatch):
    _enable(monkeypatch)
    item = _item(db, content_hash=_PAYLOAD_SHA1, content_hash_algo="sha1")
    _binding(db, 7)
    job = _cross_job(db, item.id)
    fake = _FakeUser115(rapid="hit")
    _patch_cross(monkeypatch, fake)
    sa = _FakeSA()
    res = tv.run_transfer_job_once(db, job, sa)
    assert res["outcome"] == "done"
    assert len(fake.rapid_calls) == 1
    assert fake.simple_calls == []
    assert sa.download_calls == 0  # 全程没碰字节读取器
    row = db.query(em.TransferVariant).one()
    assert row.variant_file_id == "rapidfid" and row.pickcode == "pcrapid"


def test_cross_rapid_sign_check_uses_range_reader(db, monkeypatch):
    _enable(monkeypatch)
    item = _item(db, content_hash=_PAYLOAD_SHA1, content_hash_algo="sha1")
    _binding(db, 7)
    job = _cross_job(db, item.id)
    fake = _FakeUser115(rapid="sign_then_hit")
    _patch_cross(monkeypatch, fake)
    sa = _FakeSA()
    res = tv.run_transfer_job_once(db, job, sa)
    assert res["outcome"] == "done"
    assert fake.sign_ranges == [_PAYLOAD[0:100]]  # 区间字节来自源 Range
    assert fake.simple_calls == []


# ---------- ③ 未命中回落 simple_upload ----------

def test_cross_rapid_miss_falls_back_to_simple_upload(db, monkeypatch):
    _enable(monkeypatch)
    item = _item(db, content_hash=_PAYLOAD_SHA1, content_hash_algo="sha1")
    _binding(db, 7)
    job = _cross_job(db, item.id)
    fake = _FakeUser115(rapid="miss")
    _patch_cross(monkeypatch, fake)
    res = tv.run_transfer_job_once(db, job, _FakeSA())
    assert res["outcome"] == "done"
    assert len(fake.rapid_calls) == 1
    assert fake.simple_calls == [_PAYLOAD]
    row = db.query(em.TransferVariant).one()
    assert row.variant_file_id == "upfid"


# ---------- ⑤ 协议异常回落不失败 ----------

def test_cross_rapid_error_falls_back_not_fail(db, monkeypatch):
    _enable(monkeypatch)
    item = _item(db, content_hash=_PAYLOAD_SHA1, content_hash_algo="sha1")
    _binding(db, 7)
    job = _cross_job(db, item.id)
    fake = _FakeUser115(rapid="error")
    _patch_cross(monkeypatch, fake)
    res = tv.run_transfer_job_once(db, job, _FakeSA())
    assert res["outcome"] == "done"
    assert len(fake.rapid_calls) == 1
    assert fake.simple_calls == [_PAYLOAD]


def test_cross_rapid_auth_error_marks_binding(db, monkeypatch):
    _enable(monkeypatch)
    item = _item(db, content_hash=_PAYLOAD_SHA1, content_hash_algo="sha1")
    binding = _binding(db, 7)
    job = _cross_job(db, item.id)
    fake = _FakeUser115(rapid="auth")
    _patch_cross(monkeypatch, fake)
    res = tv.run_transfer_job_once(db, job, _FakeSA())
    assert res["outcome"] == "failed"
    assert res["error_kind"] == "auth_error"
    assert fake.simple_calls == []  # 鉴权失败不许回落去撞第二次
    db.refresh(binding)
    assert binding.status != "active"


# ---------- ④ >5 GiB：命中可 done、未命中走分片 ----------

def test_cross_big_rapid_hit_done(db, monkeypatch):
    _enable(monkeypatch)
    item = _item(db, size=_BIG_SIZE, content_hash=_PAYLOAD_SHA1,
                 content_hash_algo="sha1")
    _binding(db, 7)
    job = _cross_job(db, item.id)
    fake = _FakeUser115(rapid="hit")
    _patch_cross(monkeypatch, fake)
    res = tv.run_transfer_job_once(db, job, _FakeSA(size=_BIG_SIZE))
    assert res["outcome"] == "done"
    assert fake.simple_calls == []
    row = db.query(em.TransferVariant).one()
    assert row.variant_file_id == "rapidfid"


def test_cross_big_rapid_miss_goes_multipart(db, monkeypatch):
    # 行为变更（2026-10-30 分片上传根治单）：>5 GiB 秒传未命中
    # 不再判 unsupported，改走 multipart_upload；simple 仍不许碰
    # 超限文件。分片路细账在 test_pan115_multipart_upload.py。
    _enable(monkeypatch)
    item = _item(db, size=_BIG_SIZE, content_hash=_PAYLOAD_SHA1,
                 content_hash_algo="sha1")
    _binding(db, 7)
    job = _cross_job(db, item.id)
    fake = _FakeUser115(rapid="miss")
    _patch_cross(monkeypatch, fake)
    res = tv.run_transfer_job_once(db, job, _FakeSA(size=_BIG_SIZE))
    assert res["outcome"] == "done"
    assert len(fake.rapid_calls) == 1  # 先试了秒传才轮到分片
    assert len(fake.multipart_calls) == 1
    assert fake.simple_calls == []
    row = db.query(em.TransferVariant).one()
    assert row.variant_file_id == "mpfid"


# ---------- 开关：关时不试秒传 ----------

def test_rapid_switch_off_skips_attempt(db, monkeypatch):
    _enable(monkeypatch)
    tv.write_transfer_config(db, pan115_rapid_upload_enabled=False)
    assert tv.pan115_rapid_upload_enabled(db) is False
    item = _item(db, content_hash=_PAYLOAD_SHA1, content_hash_algo="sha1")
    _binding(db, 7)
    job = _cross_job(db, item.id)
    fake = _FakeUser115(rapid="hit")
    _patch_cross(monkeypatch, fake)
    res = tv.run_transfer_job_once(db, job, _FakeSA())
    assert res["outcome"] == "done"
    assert fake.rapid_calls == []
    assert fake.simple_calls == [_PAYLOAD]


def test_rapid_switch_default_on(db):
    from backend.config_schema import TRANSFER_CONFIG_SCHEMA
    assert TRANSFER_CONFIG_SCHEMA["pan115_rapid_upload_enabled"] == ("bool",)
    assert tv.pan115_rapid_upload_enabled(db) is True
    payload = tv.transfer_config_payload(db)
    assert payload["pan115_rapid_upload_enabled"] is True


# ---------- 透传哈希包装 ----------

def test_hashing_reader_full_and_reread():
    data = bytes(range(256)) * 3
    reader = tv._HashingReader(
        tv._SegmentReader(len(data), lambda s, e: data[s:e + 1]), len(data))
    assert reader.sha1_hexdigest() is None  # 没读全不许出值
    first = reader.read(100)
    assert reader.sha1_hexdigest() is None
    rest = reader.read()
    assert first + rest == data
    assert reader.sha1_hexdigest() == hashlib.sha1(data).hexdigest()
    reader.seek(0)  # 回读已计入区间：不许重复喂哈希
    assert reader.read(50) == data[:50]
    assert reader.sha1_hexdigest() == hashlib.sha1(data).hexdigest()


# ---------- 115→Drive 与同 provider：源 sha1 顺手沉淀 ----------

def test_cross_115_to_drive_persists_source_sha1(db, monkeypatch):
    _enable(monkeypatch)
    item = _item(db, file_path="115:/电影/a.mkv", drive_file_id="srcfid")
    _binding(db, 7, provider="drive")
    job = _cross_job(db, item.id, provider="pan115", dest="userdrive:7",
                     drive_id=None)

    class _FakeServer115:
        cookie = "server-cookie"
        ua = "ua-test"

        def list_dir(self, cid="0", limit=200):
            if str(cid) == "0":
                return [{"name": "电影", "is_dir": True, "cid": "11"}]
            return [{"name": "a.mkv", "is_dir": False, "pickcode": "srccode",
                     "size": len(_PAYLOAD), "fid": "srcfid",
                     "sha1": _PAYLOAD_SHA1}]

        def download_url(self, pickcode):
            return "https://dl.example/file"

    monkeypatch.setattr(
        "backend.emby_server.transfer115.resolve_cookie",
        lambda db_, library=None: ("server-cookie", "test"))
    monkeypatch.setattr(tv, "_make_pan115_client",
                        lambda cookie: _FakeServer115())

    def _fake_request(method, url, headers=None, timeout=None, **kw):
        start, end = (int(x) for x in
                      headers["Range"][len("bytes="):].split("-"))
        return SimpleNamespace(status_code=206,
                               content=_PAYLOAD[start:end + 1])

    monkeypatch.setattr(tv.httpx, "request", _fake_request)
    monkeypatch.setattr(tv.UserDriveApiClient, "ensure_folder",
                        lambda self, d, n: "ufolder")
    monkeypatch.setattr(
        tv.UserDriveApiClient, "resumable_upload",
        lambda self, d, name, folder, size, reader: (
            reader.read(), "newdrivefid")[1])
    monkeypatch.setattr(tv.UserDriveApiClient, "add_anyone_reader",
                        lambda self, d, f: None)
    monkeypatch.setattr(tv.UserDriveApiClient, "list_file_ids_by_name",
                        lambda self, d, f, n: ["newdrivefid"])
    res = tv.run_transfer_job_once(db, job, _FakeSA())
    assert res["outcome"] == "done"
    db.refresh(item)
    assert item.content_hash_algo == "sha1"
    assert item.content_hash == _PAYLOAD_SHA1


def test_same_provider_115_copy_persists_dir_sha1(db, monkeypatch):
    _enable(monkeypatch)
    item = _item(db, file_path="mount://9/电影/a.mkv", size=len(_PAYLOAD))
    _binding(db, 7)
    job = _cross_job(db, item.id, provider="pan115", dest="pan115:user-7",
                     drive_id="pan115:mount-9")

    class _FakeCopy115:
        def __init__(self):
            self.dirs = {"0": [{"cid": "d1", "name": "aetrix-transfer",
                                "is_dir": True}],
                         "d1": []}

        def list_dir(self, cid="0", limit=200):
            return [dict(e) for e in self.dirs.get(cid or "0", [])]

        def mkdir(self, parent_cid, name):
            return {}

        def copy_file(self, source_fid, dest_cid):
            self.dirs["d1"].append(
                {"fid": "newfid", "cid": "c-new", "name": "a.mkv",
                 "is_dir": False, "size": len(_PAYLOAD),
                 "pickcode": "PC-NEW", "sha1": _PAYLOAD_SHA1})
            return {"fid": "newfid", "pickcode": "PC-NEW"}

    _patch_cross(monkeypatch, _FakeCopy115())
    res = tv.run_transfer_job_once(db, job, _FakeSA())
    assert res["outcome"] == "done"
    db.refresh(item)
    assert item.content_hash_algo == "sha1"
    assert item.content_hash == _PAYLOAD_SHA1


# ---------- 客户端协议（p115cipher 对照，测试侧独立加解密） ----------

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
    """p115cipher 的分段 lz4 帧：2 字节小端块长 + lz4 block（纯字面量块）。"""
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
    import json as _json
    return _test_aes_encrypt(
        _test_framed_lz4_encode(_json.dumps(obj).encode()))


def _patch_upload_info(monkeypatch, user_id="12345", userkey="uk-test"):
    def _fake_json(method, url, params=None, data=None, **kw):
        return {"user_id": user_id, "userkey": userkey}

    monkeypatch.setattr(transfer115, "_http_json", _fake_json)


def test_rapid_client_sign_check_then_hit(monkeypatch):
    import json as _json
    _patch_upload_info(monkeypatch)
    calls = []

    def _fake_bytes(method, url, params=None, data=None, **kw):
        calls.append({"params": params, "data": data})
        if len(calls) == 1:
            return _test_encrypt_response(
                {"status": 7, "sign_key": "SK1", "sign_check": "10-19"})
        return _test_encrypt_response(
            {"status": 2, "pickcode": "pcrapid", "file_id": "rapidfid"})

    monkeypatch.setattr(transfer115, "_http_bytes", _fake_bytes)
    client = transfer115.Pan115Client("COOKIE")
    out = client.rapid_upload("a.mkv", len(_PAYLOAD), _PAYLOAD_SHA1, "900",
                              range_reader=lambda s, e: _PAYLOAD[s:e + 1])
    assert out["outcome"] == "hit"
    assert out["fid"] == "rapidfid" and out["pickcode"] == "pcrapid"
    assert len(calls) == 2
    assert calls[0]["params"].get("k_ec")  # ECDH token 在场
    body1 = dict(parse_qsl(_test_aes_decrypt(calls[0]["data"]).decode()))
    assert body1["fileid"] == _PAYLOAD_SHA1.upper()
    assert body1["target"] == "U_1_900"
    assert body1["filename"] == "a.mkv"
    assert "sign_key" not in body1  # 首轮无二轮字段
    h1 = hashlib.sha1(
        f"12345{_PAYLOAD_SHA1.upper()}U_1_9000".encode()).hexdigest()
    assert body1["sig"] == hashlib.sha1(
        f"uk-test{h1}000000".encode()).hexdigest().upper()
    body2 = dict(parse_qsl(_test_aes_decrypt(calls[1]["data"]).decode()))
    assert body2["sign_key"] == "SK1"
    assert body2["sign_val"] == hashlib.sha1(
        _PAYLOAD[10:20]).hexdigest().upper()
    assert _json.dumps(body2)  # 保持可序列化（占位断言，防误用）


def test_rapid_client_miss(monkeypatch):
    _patch_upload_info(monkeypatch)
    monkeypatch.setattr(
        transfer115, "_http_bytes",
        lambda method, url, params=None, data=None, **kw: _test_encrypt_response(
            {"status": 1, "bucket": "b", "object": "o"}))
    client = transfer115.Pan115Client("COOKIE")
    out = client.rapid_upload("a.mkv", len(_PAYLOAD), _PAYLOAD_SHA1, "900")
    assert out["outcome"] == "miss"


def test_rapid_client_http_401_is_auth(monkeypatch):
    _patch_upload_info(monkeypatch)

    def _boom(method, url, params=None, data=None, **kw):
        raise transfer115.Pan115AuthError("115 返回 HTTP 401")

    monkeypatch.setattr(transfer115, "_http_bytes", _boom)
    client = transfer115.Pan115Client("COOKIE")
    with pytest.raises(transfer115.Pan115AuthError):
        client.rapid_upload("a.mkv", len(_PAYLOAD), _PAYLOAD_SHA1, "900")


def test_rapid_client_response_trailing_tail_bytes(monkeypatch):
    """活体实测形态：initupload 响应密文尾部带非块对齐尾巴
    （188 = 176 密文 + 12 尾巴），须截到块对齐再解。旧实现整段
    喂解密器在 finalize 抛错，秒传上线以来每次都死在这里。"""
    _patch_upload_info(monkeypatch)
    monkeypatch.setattr(
        transfer115, "_http_bytes",
        lambda method, url, params=None, data=None, **kw:
        _test_encrypt_response(
            {"status": 2, "pickcode": "pctail", "file_id": "tailfid"})
        + b"\xa5" * 12)
    client = transfer115.Pan115Client("COOKIE")
    out = client.rapid_upload("a.mkv", len(_PAYLOAD), _PAYLOAD_SHA1, "900")
    assert out["outcome"] == "hit" and out["fid"] == "tailfid"


def _test_aes_encrypt_nopad(data: bytes) -> bytes:
    from cryptography.hazmat.primitives.ciphers import (
        Cipher, algorithms, modes)
    assert len(data) % 16 == 0
    enc = Cipher(algorithms.AES(_TEST_AES_KEY),
                 modes.CBC(_TEST_AES_IV)).encryptor()
    return enc.update(data) + enc.finalize()


def test_rapid_client_response_aligned_without_padding(monkeypatch):
    """明文块对齐时服务端不补填充块：末字节不是合法 PKCS7 填充
    也必须解出（旧实现严口径 unpad 会判「填充非法」）。"""
    import json as _json
    obj = {"status": 1, "bucket": "b", "object": "o", "filler": ""}
    framed = _test_framed_lz4_encode(_json.dumps(obj).encode())
    obj["filler"] = "x" * ((-len(framed)) % 16)
    framed = _test_framed_lz4_encode(_json.dumps(obj).encode())
    assert len(framed) % 16 == 0
    raw = _test_aes_encrypt_nopad(framed)
    _patch_upload_info(monkeypatch)
    monkeypatch.setattr(
        transfer115, "_http_bytes",
        lambda method, url, params=None, data=None, **kw: raw)
    client = transfer115.Pan115Client("COOKIE")
    out = client.rapid_upload("a.mkv", len(_PAYLOAD), _PAYLOAD_SHA1, "900")
    assert out["outcome"] == "miss"


def test_rapid_client_initupload_uses_app_ua(monkeypatch):
    """initupload 是 app 端点，服务端按 UA 里的 app 版本号放行：
    浏览器 UA 回 status=4/403「请升级到最新版本」（活体实证）。
    请求 UA 必须带 115wangpan_android 版本标记。"""
    _patch_upload_info(monkeypatch)
    seen = {}

    def _fake_bytes(method, url, params=None, data=None, **kw):
        seen["ua"] = kw.get("ua") or ""
        return _test_encrypt_response({"status": 1})

    monkeypatch.setattr(transfer115, "_http_bytes", _fake_bytes)
    client = transfer115.Pan115Client("COOKIE")
    client.rapid_upload("a.mkv", len(_PAYLOAD), _PAYLOAD_SHA1, "900")
    assert "115wangpan_android/" in seen["ua"]
    assert transfer115.PAN115_APP_VERSION in seen["ua"]
