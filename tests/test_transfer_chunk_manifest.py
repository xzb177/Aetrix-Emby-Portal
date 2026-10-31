# -*- coding: utf-8 -*-
"""换代线地基：交付状态落盘 + 块哈希清单记账（2026-11-01）。

蓝图 v2 附二换代线第一刀余项（step 1 交付状态落盘）＋ step 2
（清单＋块哈希透传），可行性报告 §七顺序 1–2。本单红线：纯记
账/透传——不改变交付、重试、成败判定任何语义；记账全程
fail-open。本文件覆盖：

- 清单生成/块哈希正确性（ChunkTracker 直算交叉核对、续传从
  块中间起读攒不齐该块、乱序/重读不污染）；
- blob 打包往返（bitmap 区分零填充与真实哈希、坏 blob 回空）；
- part↔块映射入册（persist_delivery 给每片补 chunk_from/to、
  committed 前缀折算）与落盘恢复（restore 三项校验同 sidecar
  口径、parts 归一回 sidecar 形态）；
- 执行器接线（Drive→115 分片路：哈希遍/上传程顺手长清单、
  init/part 事件同步落盘、sidecar 丢失凭 DB 恢复续传、done
  后交付行 completed）；
- fail-open（记账表被删/DB 坏时转存照常 done；模块函数对坏
  DB 只回失败标记不抛）；
- 开关（默认开＋注册齐全；关＝不读不写两表、sidecar 旧行为
  逐字不变）。

全部离线：SA/115 客户端桩与 test_transfer_cross_merge_resume
同形态；115 端到端活体不在本单范围（点火门未过，报告 §一.6）。
"""
import os

os.environ.setdefault("DATABASE_TYPE", "sqlite")
os.environ.setdefault("REDIS_ENABLED", "false")

import hashlib
import json

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend import models as base_models
from backend.emby_server import models as em
from backend.emby_server import transfer_manifest as tm
from backend.emby_server import transfer_variant as tv
from backend.emby_server import transfer115
from backend.emby_server import user_bindings as ub
from backend.integrations import store
from backend.emby_server import stream_limits as sl


@pytest.fixture()
def db_factory():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False},
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


# ---------------------------------------------------------------------------
# 纯计算：blob 打包 / part↔块映射 / ChunkTracker
# ---------------------------------------------------------------------------

def test_pack_unpack_roundtrip_with_holes():
    hashes = {0: hashlib.sha256(b"a").hexdigest(),
              2: hashlib.sha256(b"c").hexdigest()}
    blob, bitmap = tm.pack_chunk_hashes(hashes, 4)
    assert len(blob) == 4 * 32 and len(bitmap) == 1
    back = tm.unpack_chunk_hashes(blob, bitmap, 4)
    assert back == hashes  # 1、3 号块零填充不被误读成哈希


def test_unpack_rejects_garbage():
    assert tm.unpack_chunk_hashes(b"", b"", 3) == {}
    assert tm.unpack_chunk_hashes(b"\x00" * 10, b"\xff", 3) == {}
    assert tm.unpack_chunk_hashes(None, None, 0) == {}


def test_part_chunk_span_mapping():
    mib = 1024 * 1024
    # part 10 MiB、块 8 MiB、文件 25 MiB：两层不对齐时按字节
    # 区间折算覆盖块闭区间，不靠片数乘法。
    assert tm.part_chunk_span(1, 10 * mib, 25 * mib) == (0, 1)
    assert tm.part_chunk_span(2, 10 * mib, 25 * mib) == (1, 2)
    assert tm.part_chunk_span(3, 10 * mib, 25 * mib) == (2, 3)
    assert tm.part_chunk_span(4, 10 * mib, 25 * mib) == (0, -1)


class _BytesReader:
    def __init__(self, data):
        self._data = data
        self._pos = 0

    def tell(self):
        return self._pos

    def seek(self, offset, whence=0):
        self._pos = offset if whence == 0 else self._pos + offset
        return self._pos

    def read(self, n=-1):
        if n is None or n < 0:
            n = len(self._data) - self._pos
        chunk = self._data[self._pos:self._pos + n]
        self._pos += len(chunk)
        return chunk


def _payload(n):
    return bytes((i * 31 + 7) % 256 for i in range(n))


def test_tracker_full_stream_hashes_and_md5():
    payload = _payload(2560)  # 1024 块口径下 3 块（1024/1024/512）
    tracker = tm.ChunkTracker(len(payload), chunk_size=1024)
    reader = tm.TrackingReader(_BytesReader(payload), tracker)
    while reader.read(300):  # 故意用不对齐的小读长跨块喂
        pass
    expected = {i: hashlib.sha256(payload[i * 1024:(i + 1) * 1024]).hexdigest()
                for i in range(3)}
    assert tracker.chunk_hashes() == expected
    assert tracker.is_complete()
    assert tracker.whole_md5() == hashlib.md5(payload).hexdigest()


def test_tracker_resume_mid_chunk_cannot_finalize_straddler():
    payload = _payload(2560)
    tracker = tm.ChunkTracker(len(payload), chunk_size=1024)
    # 续传形态：从块 1 中间（1500）顺序读到尾——块 1 本程攒不
    # 齐保持未入册，块 2 齐；MD5 前沿没从 0 起过，作废。
    tracker.feed(1500, payload[1500:])
    hashes = tracker.chunk_hashes()
    assert 1 not in hashes
    assert hashes.get(2) == hashlib.sha256(payload[2048:]).hexdigest()
    assert tracker.whole_md5() is None
    assert not tracker.is_complete()
    # 另一程从 0 顺序读到 1500：块 0 补齐；块 1 只补到前 476
    # 字节——它后半段字节在第一程喂过但当时块头未见、已按纪律
    # 丢弃（tracker 不存字节），仍差一程从块头整块顺序读。
    tracker.feed(0, payload[:1500])
    assert 0 in tracker.chunk_hashes() and 1 not in tracker.chunk_hashes()
    assert not tracker.is_complete()
    tracker.feed(1024, payload[1024:2048])  # 从块头整块读 → 补齐
    assert tracker.is_complete()
    # MD5 前沿早已断裂：整文件 MD5 仍无（与透传 sha1 的"断裂
    # 即作废"同口径，靠哈希遍那种整程顺序读才拿得到）。
    assert tracker.whole_md5() is None


def test_tracker_reread_does_not_corrupt():
    payload = _payload(2048)
    tracker = tm.ChunkTracker(len(payload), chunk_size=1024)
    tracker.feed(0, payload[:1024])
    tracker.feed(0, payload[:1024])  # 同区间重读：已入册不重喂
    tracker.feed(1024, payload[1024:])
    assert tracker.chunk_hashes() == {
        0: hashlib.sha256(payload[:1024]).hexdigest(),
        1: hashlib.sha256(payload[1024:]).hexdigest()}
    assert tracker.whole_md5() == hashlib.md5(payload).hexdigest()


# ---------------------------------------------------------------------------
# 清单入册（DB）
# ---------------------------------------------------------------------------

_KEY = "ab" * 32


def test_manifest_recorder_partial_then_complete(db, monkeypatch):
    monkeypatch.setattr(tm, "CHUNK_SIZE", 1024)
    payload = _payload(2560)
    sha1 = hashlib.sha1(payload).hexdigest()
    # 第一程只读前两块 → partial（块未齐、无整文件锚）。
    rec1 = tm.begin_recorder(db, _KEY, len(payload))
    rec1.tracker.feed(0, payload[:2048])
    assert rec1.flush(db) is True
    row = db.query(em.TransferContentManifest).filter_by(
        content_key=_KEY).one()
    assert row.manifest_state == "partial"
    assert row.whole_sha1 is None
    got = tm.unpack_chunk_hashes(row.chunk_hashes, row.chunk_bitmap,
                                 row.chunk_count)
    assert set(got) == {0, 1}
    # 第二程（新句柄，种子=已入册两块）读全程 → complete，
    # 整文件 sha1/md5 双锚入册。
    rec2 = tm.begin_recorder(db, _KEY, len(payload))
    assert set(rec2.tracker.chunk_hashes()) == {0, 1}  # 种子已带
    rec2.tracker.feed(0, payload)
    assert rec2.flush(db, whole_sha1=sha1) is True
    db.refresh(row)
    assert row.manifest_state == "complete"
    assert row.whole_sha1 == sha1
    assert row.whole_md5 == hashlib.md5(payload).hexdigest()
    got = tm.unpack_chunk_hashes(row.chunk_hashes, row.chunk_bitmap,
                                 row.chunk_count)
    assert got == {i: hashlib.sha256(
        payload[i * 1024:(i + 1) * 1024]).hexdigest() for i in range(3)}


def test_manifest_conflict_first_writer_wins(db, monkeypatch):
    monkeypatch.setattr(tm, "CHUNK_SIZE", 1024)
    payload = _payload(1024)
    wrong = hashlib.sha256(b"someone-elses-bytes").hexdigest()
    blob, bitmap = tm.pack_chunk_hashes({0: wrong}, 1)
    db.add(em.TransferContentManifest(
        content_key=_KEY, size=len(payload), chunk_size=1024,
        chunk_count=1, chunk_hashes=blob, chunk_bitmap=bitmap,
        manifest_state="partial"))
    db.commit()
    rec = tm.begin_recorder(db, _KEY, len(payload))
    rec.tracker.feed(0, payload)
    assert rec.flush(db, whole_sha1=hashlib.sha1(payload).hexdigest())
    row = db.query(em.TransferContentManifest).filter_by(
        content_key=_KEY).one()
    got = tm.unpack_chunk_hashes(row.chunk_hashes, row.chunk_bitmap, 1)
    assert got[0] == wrong  # 已入册值不被后写覆盖（先写者胜）


class _BrokenDB:
    def query(self, *a, **kw):
        raise RuntimeError("db gone")

    def rollback(self):
        raise RuntimeError("rollback gone too")


def test_manifest_module_fail_open_on_broken_db():
    broken = _BrokenDB()
    rec = tm.begin_recorder(broken, _KEY, 2048)
    assert rec is not None  # 读不到种子按空种子继续，不抛
    rec.tracker.feed(0, _payload(2048))
    assert rec.flush(broken) is False
    assert tm.persist_delivery(
        broken, job_id=1, content_key=_KEY, dest_provider="pan115",
        dest_binding_id=1, dest_ref="900",
        session={"parts": []}, size=2048) is False
    assert tm.finish_delivery(broken, 1, "completed") is False
    assert tm.restore_delivery_session(broken, 1, _KEY, 2048, "900") is None


# ---------------------------------------------------------------------------
# 交付落盘 + 恢复
# ---------------------------------------------------------------------------

_MIB = 1024 * 1024


def _session(parts):
    return {"bucket": "bkt", "object_key": "obj", "upload_id": "UP-9",
            "callback": {"callback": "Yg==", "callback_var": "dmI="},
            "parts": parts}


def test_persist_delivery_mapping_and_restore(db):
    size = 25 * _MIB  # partsize 恒 10 MiB（_determine_partsize 下限）
    parts = [{"PartNumber": n, "ETag": f"E{n}"} for n in (1, 2, 3)]
    assert tm.persist_delivery(
        db, job_id=42, content_key=_KEY, dest_provider="pan115",
        dest_binding_id=9, dest_ref="900", session=_session(parts),
        size=size) is True
    row = db.query(em.TransferDelivery).filter_by(job_id=42).one()
    assert row.state == "active" and row.dest_ref == "900"
    assert row.committed_offset == size
    assert row.committed_chunks == size // tm.CHUNK_SIZE
    saved = json.loads(row.session)
    spans = [(p["chunk_from"], p["chunk_to"]) for p in saved["parts"]]
    assert spans == [(0, 1), (1, 2), (2, 3)]  # part↔块映射入册
    # 恢复：三项校验同 sidecar 口径，parts 归一回 sidecar 形态。
    restored = tm.restore_delivery_session(db, 42, _KEY, size, "900")
    assert restored["upload_id"] == "UP-9"
    assert restored["parts"] == parts
    assert tm.restore_delivery_session(db, 42, _KEY, size, "901") is None
    assert tm.restore_delivery_session(db, 42, "cd" * 32, size, "900") is None
    assert tm.restore_delivery_session(db, 42, _KEY, size - 1, "900") is None
    # 终态后不再可恢复。
    assert tm.finish_delivery(db, 42, "completed") is True
    assert tm.restore_delivery_session(db, 42, _KEY, size, "900") is None


def test_restore_rejects_incomplete_session(db):
    bad = _session([])
    del bad["callback"]
    assert tm.persist_delivery(
        db, job_id=43, content_key=_KEY, dest_provider="pan115",
        dest_binding_id=9, dest_ref="900", session=bad, size=2048)
    assert tm.restore_delivery_session(db, 43, _KEY, 2048, "900") is None


# ---------------------------------------------------------------------------
# 开关（注册 + 默认开 + 关行为在执行器用例里验）
# ---------------------------------------------------------------------------

def test_manifest_switch_registered_default_on(db):
    from backend.config_schema import TRANSFER_CONFIG_SCHEMA
    assert TRANSFER_CONFIG_SCHEMA[
        "transfer_manifest_accounting_enabled"] == ("bool",)
    assert tv.manifest_accounting_enabled(db) is True
    assert tv.transfer_config_payload(
        db)["transfer_manifest_accounting_enabled"] is True
    tv.write_transfer_config(db, transfer_manifest_accounting_enabled=False)
    assert tv.manifest_accounting_enabled(db) is False
    assert tv.transfer_config_payload(
        db)["transfer_manifest_accounting_enabled"] is False


# ---------------------------------------------------------------------------
# 执行器接线（Drive→115 分片路）
# ---------------------------------------------------------------------------

_counter = [0]
_PAYLOAD = bytes((i * 7) % 256 for i in range(2000))
_PAYLOAD_SHA1 = hashlib.sha1(_PAYLOAD).hexdigest()


def _item(db, **kw):
    _counter[0] += 1
    row = em.MediaItem(guid=f"cm-{_counter[0]}", library_id=1,
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


def _cross_job(db, item_id, user_id=7):
    row = em.TransferJob(item_id=item_id, trigger="playback",
                         drive_id="shared1", provider="drive",
                         owner_scope="user", owner_ref=str(user_id),
                         dest_drive_id=f"pan115:user-{user_id}",
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
    """首尝分片失败（带 init/part 事件）、再尝成功的 115 桩。"""

    def __init__(self):
        self.dirs = {
            "0": [{"name": "aetrix-transfer", "is_dir": True, "cid": "900"}],
            "900": [],
        }
        self.multipart_calls = []

    def list_dir(self, cid="0", limit=200):
        return list(self.dirs.get(str(cid), []))

    def mkdir(self, parent_cid, name):
        return {}

    def rapid_upload(self, filename, filesize, file_sha1, pid,
                     range_reader=None):
        return {"outcome": "miss", "fid": "", "pickcode": "", "raw": {}}

    def simple_upload(self, fileobj, filename, pid):  # pragma: no cover
        raise AssertionError("本组用例只许走分片路")

    def multipart_upload(self, fileobj, filename, filesize, file_sha1, pid,
                         range_reader=None, resume_session=None,
                         on_progress=None):
        self.multipart_calls.append(resume_session)
        if len(self.multipart_calls) == 1:
            if on_progress is not None:
                on_progress({"event": "init",
                             "session": {"bucket": "bkt",
                                         "object_key": "obj",
                                         "upload_id": "UP-1",
                                         "callback": {"callback": "Yg==",
                                                      "callback_var": "dmI="},
                                         "parts": []}})
                on_progress({"event": "part", "number": 1, "etag": "E1"})
            raise transfer115.Pan115Error("模拟分片上传中途失败")
        self.dirs["900"] = [{"name": filename, "is_dir": False,
                             "fid": "mpfid", "pickcode": "pcmp",
                             "size": filesize, "sha1": file_sha1}]
        return {"outcome": "uploaded", "fid": "mpfid", "pickcode": "pcmp",
                "sha1": file_sha1, "raw": {}}


@pytest.fixture()
def stage_env(monkeypatch, tmp_path):
    monkeypatch.setenv("TRANSFER_STAGE_DIR", str(tmp_path / "stage"))
    monkeypatch.setattr(tv, "_CROSS_SEGMENT", 512)
    monkeypatch.setattr(tv, "_115_SIMPLE_UPLOAD_LIMIT", 1024)
    return tmp_path / "stage"


def _patch_cross(monkeypatch, fake115):
    monkeypatch.setattr(tv, "_make_pan115_client", lambda ck: fake115)
    monkeypatch.setattr(ub, "binding_115_cookie",
                        lambda db_arg, user_id, binding_id: "COOKIE")


def _run_fail_then_done(db, monkeypatch, stage_env):
    item = _item(db)  # 无 content_hash：首尝先哈希遍再走分片
    _binding(db, 7)
    fake115 = _FakeUser115()
    _patch_cross(monkeypatch, fake115)
    sa = _FakeSA()
    job = _cross_job(db, item.id)
    res1 = tv.run_transfer_job_once(db, job, sa)
    assert res1["outcome"] == "retried"
    return item, job, fake115, sa


def test_executor_persists_delivery_and_manifest(db, monkeypatch, stage_env):
    item, job, fake115, sa = _run_fail_then_done(db, monkeypatch, stage_env)
    # 首尝失败后：交付行已落盘（active、会话与 part↔块映射齐）。
    drow = db.query(em.TransferDelivery).filter_by(job_id=job.id).one()
    assert drow.state == "active" and drow.dest_provider == "pan115"
    assert drow.dest_ref == "900"
    saved = json.loads(drow.session)
    assert saved["upload_id"] == "UP-1"
    assert saved["parts"] == [{"PartNumber": 1, "ETag": "E1",
                               "chunk_from": 0, "chunk_to": 0}]
    # 哈希遍已把清单长全：complete + 双锚 + 块哈希直算一致。
    mrow = db.query(em.TransferContentManifest).one()
    assert mrow.manifest_state == "complete"
    assert mrow.whole_sha1 == _PAYLOAD_SHA1
    assert mrow.whole_md5 == hashlib.md5(_PAYLOAD).hexdigest()
    got = tm.unpack_chunk_hashes(mrow.chunk_hashes, mrow.chunk_bitmap,
                                 mrow.chunk_count)
    assert got == {0: hashlib.sha256(_PAYLOAD).hexdigest()}
    # 再尝走 sidecar 续传 done：交付行转 completed、sidecar 清掉。
    res2 = tv.run_transfer_job_once(db, job, sa)
    assert res2["outcome"] == "done"
    assert fake115.multipart_calls[1].get("upload_id") == "UP-1"
    db.refresh(drow)
    assert drow.state == "completed"
    assert list(stage_env.glob("mp-*.json")) == []


def test_executor_restores_session_from_db_when_sidecar_lost(
        db, monkeypatch, stage_env):
    item, job, fake115, sa = _run_fail_then_done(db, monkeypatch, stage_env)
    for sidecar in stage_env.glob("mp-*.json"):
        sidecar.unlink()  # 模拟容器重建把 stage/sidecar 清掉
    res2 = tv.run_transfer_job_once(db, job, sa)
    assert res2["outcome"] == "done"
    resumed = fake115.multipart_calls[1]
    assert resumed is not None and resumed.get("upload_id") == "UP-1"
    # 恢复出的会话 parts 已归一（块映射字段不漏进执行面）。
    assert resumed.get("parts") == [{"PartNumber": 1, "ETag": "E1"}]


def test_executor_accounting_disabled_legacy_behavior(
        db, monkeypatch, stage_env):
    tv.write_transfer_config(db, transfer_manifest_accounting_enabled=False)
    item, job, fake115, sa = _run_fail_then_done(db, monkeypatch, stage_env)
    res2 = tv.run_transfer_job_once(db, job, sa)
    assert res2["outcome"] == "done"
    assert fake115.multipart_calls[1].get("upload_id") == "UP-1"  # sidecar 续传照旧
    assert db.query(em.TransferDelivery).count() == 0
    assert db.query(em.TransferContentManifest).count() == 0


def test_executor_accounting_tables_missing_fail_open(
        db, monkeypatch, stage_env):
    bind = db.get_bind()
    em.TransferDelivery.__table__.drop(bind)
    em.TransferContentManifest.__table__.drop(bind)
    item, job, fake115, sa = _run_fail_then_done(db, monkeypatch, stage_env)
    res2 = tv.run_transfer_job_once(db, job, sa)
    assert res2["outcome"] == "done"  # 记账全灭，转存语义不受影响
    db.refresh(job)
    assert job.status == "done"
