# -*- coding: utf-8 -*-
"""秒传接力扩围（2026-10-30 23:11 用户拍板第一优先项）：补哈希
登记三缺口，让接力预检的命中资产只增不减。

背景（生产实查 2026-10-30）：有效条目约 17.2 万，content_hash
为 sha1 的仅 14 条——接力预检几乎必然 skip（reason=
no_trusted_sha1）。预检本身已在 Drive→115 执行器搬字节前
（#1538），缺的是登记侧：

1. 扫描降级覆盖：_apply_scan_identity 对已匹配行无条件以源哈希
   覆写单值列，转存沉淀的 sha1 会被下一轮扫描给的 md5 洗掉。
   本单口径：存量 sha1 不被非 sha1 降级覆盖（与 _persist_item_sha1
   「md5 可随时从 Drive 重得、sha1 只能靠整文件字节算出」的既有
   取舍一致）；源给 sha1 时照常刷新（内容真变了新值跟着变）；
   identity_json 证据快照仍按源值原样记录（快照语义不变）。
2. 115→Drive 透传未算哈希：源列表 sha1 缺失时字节全程过 worker
   却不沉淀；列表 sha1 写错时旧代码照单全收、毒化后续秒传资格。
   本单口径：读取器包 _HashingReader，上传成功后以透传算出的
   sha1 沉淀；与源列表值不一致打警告并以透传为准（透传是实际
   搬运字节的事实）；透传没算满才回落源列表值（旧口径）。
3. 同 fid 兄弟行不共享：同 drive_file_id 的多行（同文件在多库）
   只沉淀 job 命中的一行。本单口径：_persist_item_sha1 同步给
   同 fid 的有效兄弟行（软删/已合并行不动）。

全部离线：115 客户端、用户 Drive 客户端、下载一律桩替身。
可信判据（_trusted_item_sha1）一字不动：不可信哈希仍不试秒传。
"""
import os

os.environ.setdefault("DATABASE_TYPE", "sqlite")
os.environ.setdefault("REDIS_ENABLED", "false")

import hashlib
import json
import logging
from datetime import datetime
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend import models as base_models
from backend.emby_server import models as em
from backend.emby_server import transfer_variant as tv
from backend.emby_server.scanner import ScanFile, _apply_scan_identity
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


_counter = [7000]
_PAYLOAD = bytes(range(256)) * 4  # 1024 字节
_PAYLOAD_SHA1 = hashlib.sha1(_PAYLOAD).hexdigest()
_WRONG_SHA1 = "0" * 40
_MD5 = "a" * 32
_TV_LOGGER = "backend.emby_server.transfer_variant"


def _item(db, **kw):
    _counter[0] += 1
    row = em.MediaItem(guid=f"expand-{_counter[0]}", library_id=1,
                       item_type="movie", name="扩围测试片",
                       drive_file_id="origfid",
                       file_path="mount://3/电影/expand.mkv",
                       size=len(_PAYLOAD))
    for key, value in kw.items():
        setattr(row, key, value)
    db.add(row)
    db.commit()
    return row


# ---------- 缺口 1：扫描不许把已沉淀 sha1 降级洗掉 ----------

def _scanfile(**kw):
    base = {"stored_path": "/x/a.mkv", "name": "a.mkv", "local_dir": None}
    base.update(kw)
    return ScanFile(**base)


def test_scan_md5_does_not_downgrade_persisted_sha1():
    item = SimpleNamespace(content_hash=_PAYLOAD_SHA1,
                           content_hash_algo="sha1", identity_json=None)
    _apply_scan_identity(item, _scanfile(content_hash=_MD5,
                                         content_hash_algo="md5"))
    # 列保住秒传资产；证据快照仍按源值原样记录（快照语义不变）。
    assert item.content_hash == _PAYLOAD_SHA1
    assert item.content_hash_algo == "sha1"
    assert json.loads(item.identity_json) == {
        "content_hash": _MD5, "content_hash_algo": "md5"}


def test_scan_sha1_refreshes_persisted_sha1():
    # 内容真变了：源给的新 sha1 照常刷新存量 sha1（不许被保护挡住）。
    item = SimpleNamespace(content_hash=_PAYLOAD_SHA1,
                           content_hash_algo="sha1", identity_json=None)
    _apply_scan_identity(item, _scanfile(content_hash=_WRONG_SHA1,
                                         content_hash_algo="sha1"))
    assert item.content_hash == _WRONG_SHA1
    assert item.content_hash_algo == "sha1"


def test_scan_md5_still_overwrites_stored_md5():
    # 存量不是 sha1 时旧口径不变：源值照常刷新。
    item = SimpleNamespace(content_hash="b" * 32,
                           content_hash_algo="md5", identity_json=None)
    _apply_scan_identity(item, _scanfile(content_hash=_MD5,
                                         content_hash_algo="md5"))
    assert item.content_hash == _MD5
    assert item.content_hash_algo == "md5"


# ---------- 缺口 3：同 fid 兄弟行共享登记 ----------

def test_persist_sha1_propagates_to_same_fid_siblings(db):
    main = _item(db, drive_file_id="fidX")
    sib_md5 = _item(db, drive_file_id="fidX", content_hash=_MD5,
                    content_hash_algo="md5")
    sib_null = _item(db, drive_file_id="fidX")
    sib_deleted = _item(db, drive_file_id="fidX",
                        deleted_at=datetime(2026, 1, 1))
    sib_merged = _item(db, drive_file_id="fidX", merged_into_id=main.id)
    other = _item(db, drive_file_id="fidY")
    # 软删行 id 必须趁未被钩子挡住时先取（include_deleted 块内）。
    from backend.emby_server.soft_delete import include_deleted
    with include_deleted():
        guard_ids = (sib_deleted.id, sib_merged.id, other.id)
    tv._persist_item_sha1(db, main, _PAYLOAD_SHA1, origin="test_expand")
    for row in (main, sib_md5, sib_null):
        db.refresh(row)
        assert row.content_hash == _PAYLOAD_SHA1
        assert row.content_hash_algo == "sha1"
    # 软删/已合并行不是有效登记面，不动；别的文件不动。软删行被
    # 全局软删钩子挡住 ORM refresh，改走裸 SQL 看真值。
    from sqlalchemy import text
    for rid in guard_ids:
        got = db.execute(
            text("SELECT content_hash, content_hash_algo "
                 "FROM emby_items WHERE id = :i"),
            {"i": rid}).first()
        assert got is not None
        assert got[0] is None and got[1] is None


# ---------- 缺口 2：115→Drive 透传算哈希沉淀 ----------

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


def _run_115_to_drive(db, monkeypatch, listing_sha1, caplog=None):
    _counter[0] += 1
    item = _item(db, file_path="115:/电影/a.mkv", drive_file_id="srcfid")
    _binding(db, 7)
    job = em.TransferJob(item_id=item.id, trigger="playback",
                         drive_id=None, provider="pan115",
                         owner_scope="user", owner_ref="7",
                         dest_drive_id="userdrive:7", status="pending")
    db.add(job)
    db.commit()

    class _FakeServer115:
        ua = "ua-test"

        def __init__(self):
            setattr(self, "cookie", "server-ck")

        def list_dir(self, cid="0", limit=200):
            if str(cid) == "0":
                return [{"name": "电影", "is_dir": True, "cid": "11"}]
            entry = {"name": "a.mkv", "is_dir": False,
                     "pickcode": "srccode", "size": len(_PAYLOAD),
                     "fid": "srcfid"}
            if listing_sha1 is not None:
                entry["sha1"] = listing_sha1
            return [entry]

        def download_url(self, pickcode):
            return "https://dl.example/file"

    monkeypatch.setattr(
        "backend.emby_server.transfer115.resolve_cookie",
        lambda db_, library=None: ("server-ck", "test"))
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
    monkeypatch.setenv("TRANSFER_VARIANT_ENABLED", "1")
    res = tv.run_transfer_job_once(db, job, None)
    assert res["outcome"] == "done"
    db.refresh(item)
    return item


def test_cross_115_to_drive_sediments_stream_sha1_when_listing_lacks_it(
        db, monkeypatch):
    item = _run_115_to_drive(db, monkeypatch, listing_sha1=None)
    assert item.content_hash_algo == "sha1"
    assert item.content_hash == _PAYLOAD_SHA1


def test_cross_115_to_drive_prefers_stream_sha1_over_wrong_listing(
        db, monkeypatch, caplog):
    with caplog.at_level(logging.WARNING, logger=_TV_LOGGER):
        item = _run_115_to_drive(db, monkeypatch, listing_sha1=_WRONG_SHA1)
    assert item.content_hash_algo == "sha1"
    assert item.content_hash == _PAYLOAD_SHA1  # 实际搬运字节为准
    assert "sha1 不一致" in caplog.text


def test_cross_115_to_drive_keeps_listing_sha1_when_stream_unread(
        db, monkeypatch):
    # 透传没算满（上传桩不读 reader）时回落源列表值：旧口径不丢。
    _counter[0] += 1
    item = _item(db, file_path="115:/电影/a.mkv", drive_file_id="srcfid")
    _binding(db, 7)
    job = em.TransferJob(item_id=item.id, trigger="playback",
                         drive_id=None, provider="pan115",
                         owner_scope="user", owner_ref="7",
                         dest_drive_id="userdrive:7", status="pending")
    db.add(job)
    db.commit()

    class _FakeServer115:
        ua = "ua-test"

        def __init__(self):
            setattr(self, "cookie", "server-ck")

        def list_dir(self, cid="0", limit=200):
            if str(cid) == "0":
                return [{"name": "电影", "is_dir": True, "cid": "11"}]
            return [{"name": "a.mkv", "is_dir": False,
                     "pickcode": "srccode", "size": len(_PAYLOAD),
                     "fid": "srcfid", "sha1": _PAYLOAD_SHA1}]

        def download_url(self, pickcode):
            return "https://dl.example/file"

    monkeypatch.setattr(
        "backend.emby_server.transfer115.resolve_cookie",
        lambda db_, library=None: ("server-ck", "test"))
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
        lambda self, d, name, folder, size, reader: "newdrivefid")
    monkeypatch.setattr(tv.UserDriveApiClient, "add_anyone_reader",
                        lambda self, d, f: None)
    monkeypatch.setattr(tv.UserDriveApiClient, "list_file_ids_by_name",
                        lambda self, d, f, n: ["newdrivefid"])
    monkeypatch.setenv("TRANSFER_VARIANT_ENABLED", "1")
    res = tv.run_transfer_job_once(db, job, None)
    assert res["outcome"] == "done"
    db.refresh(item)
    assert item.content_hash_algo == "sha1"
    assert item.content_hash == _PAYLOAD_SHA1
