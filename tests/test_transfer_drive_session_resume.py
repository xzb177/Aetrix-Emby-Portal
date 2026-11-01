# -*- coding: utf-8 -*-
"""换代线 step 3：Drive 会话落盘续传（115→Drive 方向）。

此前 UserDriveApiClient.resumable_upload 的会话 URI 只活在单次
尝试内：尝试失败（进程重启/容器重建/网络抖动）后整单重传已
白传的字节。本文件钉死新契约：

- 会话 URI + 已提交偏移落 transfer_deliveries（transfer_manifest
  .persist_drive_session / restore_drive_session），恢复时三项
  校验（content_key + size + 目标目录）与 sidecar 同口径；
- resumable_upload 拿到落盘会话时先状态查询对账（服务端已
  提交偏移是权威），一致才从该偏移续；会话已死（400/404/410）
  响亮记日志并回落新建会话从头传——绝不因恢复失败打挂这一单；
- 状态查询直接回 200 = 上游已完成，直接拿 file id 收尾；
- 不带 hooks 的旧调用口径逐字不变（见 test_transfer_variant
  .test_resumable_upload_308_resume，本文件不重复）。

全部离线：httpx 用桩替身，DB 用内存 sqlite。
"""
import os

os.environ.setdefault("DATABASE_TYPE", "sqlite")
os.environ.setdefault("REDIS_ENABLED", "false")

import io
import logging
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend import models as base_models
from backend.emby_server import models as em
from backend.emby_server import transfer_manifest as tman
from backend.emby_server import transfer_variant as tv
from backend.integrations import store


@pytest.fixture()
def db():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False},
                           poolclass=StaticPool)
    base_models.Base.metadata.create_all(engine)
    em.Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    store.invalidate()
    yield session
    session.close()
    store.invalidate()


def _client(monkeypatch):
    monkeypatch.setattr(tv.UserDriveApiClient, "_headers",
                        lambda self, d, force_refresh=False:
                        {"Authorization": "Bearer <redacted>"})
    return tv.UserDriveApiClient.__new__(tv.UserDriveApiClient)


def _resp(status, headers=None, payload=None):
    resp = SimpleNamespace(status_code=status, headers=headers or {},
                           content=b"{}" if payload is not None else b"")
    if payload is not None:
        resp.json = lambda: payload
    return resp


# ---------- resumable_upload：凭落盘会话续传 ----------

def test_resume_from_persisted_session_skips_creation(monkeypatch):
    monkeypatch.setattr(tv, "_CROSS_SEGMENT", 8)
    data = b"0123456789ABCDEFGHIJ"  # 20 字节
    reader = io.BytesIO(data)
    posts = []
    puts = []

    def _fake_request(method, url, params=None, json=None, headers=None,
                      content=None, timeout=None, **kw):
        if method == "POST":
            posts.append(url)
            return _resp(200, {"location": "https://sess/new"})
        rng = (headers or {}).get("Content-Range", "")
        puts.append((rng, bytes(content or b"")))
        if rng == "bytes */20":  # 状态查询：已提交 8 字节
            return _resp(308, {"range": "bytes=0-7"})
        if rng == "bytes 8-15/20":
            return _resp(308, {"range": "bytes=0-15"})
        if rng == "bytes 16-19/20":
            return _resp(200, payload={"id": "upfid"})
        raise AssertionError(f"unexpected PUT {rng}")

    monkeypatch.setattr(tv.httpx, "request", _fake_request)
    client = _client(monkeypatch)
    progress = []
    client.drive_session_hooks = {
        "resume_session_uri": "https://sess/old",
        "on_progress": progress.append,
    }
    fid = client.resumable_upload("myDrive:7", "a.mkv", "folder",
                                  len(data), reader)
    assert fid == "upfid"
    assert posts == []  # 没有新建会话
    data_puts = [p for p in puts if not p[0].startswith("bytes *")]
    assert [p[0] for p in data_puts] == ["bytes 8-15/20", "bytes 16-19/20"]
    assert b"".join(p[1] for p in data_puts) == data[8:]
    assert progress[-1] == len(data)


def test_resume_dead_session_falls_back_to_new_session(monkeypatch, caplog):
    monkeypatch.setattr(tv, "_CROSS_SEGMENT", 8)
    data = b"0123456789ABCDEFGHIJ"
    reader = io.BytesIO(data)
    created = []
    sessions = []

    def _fake_request(method, url, params=None, json=None, headers=None,
                      content=None, timeout=None, **kw):
        if method == "POST":
            return _resp(200, {"location": "https://sess/new"})
        rng = (headers or {}).get("Content-Range", "")
        if rng.startswith("bytes *"):
            return _resp(404)  # 会话已死
        created.append((rng, url))
        return _resp(200, payload={"id": "freshfid"})

    monkeypatch.setattr(tv.httpx, "request", _fake_request)
    client = _client(monkeypatch)
    client.drive_session_hooks = {
        "resume_session_uri": "https://sess/dead",
        "on_session": sessions.append,
    }
    with caplog.at_level(logging.WARNING):
        fid = client.resumable_upload("myDrive:7", "a.mkv", "folder",
                                      len(data), reader)
    assert fid == "freshfid"
    # 回落新建：on_session 收到新会话 URI，数据从头发（分段口径
    # 下第一段回执直接 200 收尾），且失效有响亮日志。
    assert sessions == ["https://sess/new"]
    assert created and created[0][1] == "https://sess/new"
    assert any("会话" in r.message and "失效" in r.message
               for r in caplog.records)


def test_resume_query_completed_returns_existing_id(monkeypatch):
    data = b"0123456789"
    reader = io.BytesIO(data)
    puts = []

    def _fake_request(method, url, params=None, json=None, headers=None,
                      content=None, timeout=None, **kw):
        if method == "POST":
            raise AssertionError("已完成会话不许再新建")
        puts.append((headers or {}).get("Content-Range", ""))
        return _resp(200, payload={"id": "donefid"})

    monkeypatch.setattr(tv.httpx, "request", _fake_request)
    client = _client(monkeypatch)
    client.drive_session_hooks = {"resume_session_uri": "https://sess/done"}
    fid = client.resumable_upload("myDrive:7", "a.mkv", "folder",
                                  len(data), reader)
    assert fid == "donefid"
    assert puts == ["bytes */10"]  # 只有状态查询，没有数据 PUT


def test_resume_query_400_also_dead(monkeypatch):
    # 可行性报告 §二.3：违规/取消后会话作废以 400 呈现，同 404
    # 一样判死回落新建，不许把整单判失败。
    data = b"0123456789"
    reader = io.BytesIO(data)

    def _fake_request(method, url, params=None, json=None, headers=None,
                      content=None, timeout=None, **kw):
        if method == "POST":
            return _resp(200, {"location": "https://sess/new2"})
        rng = (headers or {}).get("Content-Range", "")
        if rng.startswith("bytes *"):
            return _resp(400)
        return _resp(200, payload={"id": "fresh2"})

    monkeypatch.setattr(tv.httpx, "request", _fake_request)
    client = _client(monkeypatch)
    client.drive_session_hooks = {"resume_session_uri": "https://sess/bad"}
    fid = client.resumable_upload("myDrive:7", "a.mkv", "folder",
                                  len(data), reader)
    assert fid == "fresh2"


# ---------- transfer_deliveries 落盘/恢复（Drive 会话） ----------

def test_drive_session_persist_restore_roundtrip(db):
    ok = tman.persist_drive_session(
        db, job_id=42, content_key="ck1", dest_ref="folder9",
        session_uri="https://sess/x", size=1000, name="a.mkv",
        committed_offset=256)
    assert ok is True
    got = tman.restore_drive_session(db, 42, "ck1", 1000, "folder9")
    assert got is not None
    assert got["session_uri"] == "https://sess/x"
    assert got["committed_offset"] == 256
    # 三项校验任一不符 → 不认（按无会话走全新上传）。
    assert tman.restore_drive_session(db, 42, "ckX", 1000, "folder9") is None
    assert tman.restore_drive_session(db, 42, "ck1", 999, "folder9") is None
    assert tman.restore_drive_session(db, 42, "ck1", 1000, "folderX") is None
    # 终态后不再恢复。
    tman.finish_delivery(db, 42, "completed")
    assert tman.restore_drive_session(db, 42, "ck1", 1000, "folder9") is None


def test_drive_session_persist_updates_offset(db):
    tman.persist_drive_session(
        db, job_id=43, content_key="ck2", dest_ref="f",
        session_uri="https://sess/y", size=500, name="b.mkv",
        committed_offset=0)
    tman.persist_drive_session(
        db, job_id=43, content_key="ck2", dest_ref="f",
        session_uri="https://sess/y", size=500, name="b.mkv",
        committed_offset=320)
    rows = db.query(em.TransferDelivery).filter_by(job_id=43).all()
    assert len(rows) == 1  # 同一单原地演进，不长新行
    assert rows[0].committed_offset == 320
    assert rows[0].dest_provider == "drive"


# ---------- 执行器接线：落盘会话进 hooks、成功落 completed ----------

_PAYLOAD = b"hello-115-to-drive" * 4


def test_executor_wires_persisted_session_into_upload(db, monkeypatch):
    monkeypatch.setenv("TRANSFER_VARIANT_ENABLED", "1")
    item = em.MediaItem(guid="dsr-1", library_id=1, item_type="movie",
                        name="测试片", drive_file_id="srcfid",
                        file_path="115:/电影/a.mkv", size=len(_PAYLOAD))
    db.add(item)
    db.commit()
    cred = em.MountCredential(scope="user", owner_user_id=7,
                              status="active")
    db.add(cred)
    db.commit()
    db.add(em.UserStorageBinding(user_id=7, provider="drive",
                                 account_label="t", credential_id=cred.id,
                                 status="active"))
    job = em.TransferJob(item_id=item.id, trigger="playback",
                         provider="pan115", owner_scope="user",
                         owner_ref="7", dest_drive_id="userdrive:7",
                         status="pending")
    db.add(job)
    db.commit()

    content_key = tv._stage_key("pan115", "", "srcfid", len(_PAYLOAD))
    tman.persist_drive_session(
        db, job_id=job.id, content_key=content_key, dest_ref="ufolder",
        session_uri="https://sess/persisted", size=len(_PAYLOAD),
        name="a.mkv", committed_offset=16)

    class _FakeServer115:
        cookie = "server-cookie"
        ua = "ua-test"

        def list_dir(self, cid="0", limit=200):
            if str(cid) == "0":
                return [{"name": "电影", "is_dir": True, "cid": "11"}]
            return [{"name": "a.mkv", "is_dir": False,
                     "pickcode": "srccode", "size": len(_PAYLOAD),
                     "fid": "srcfid"}]

        def download_url(self, pickcode):
            return "https://dl.example/file"

    monkeypatch.setattr("backend.emby_server.transfer115.resolve_cookie",
                        lambda db_, library=None: ("server-cookie", "t"))
    monkeypatch.setattr(tv, "_make_pan115_client",
                        lambda cookie: _FakeServer115())

    def _fake_request(method, url, headers=None, timeout=None, **kw):
        rng = headers["Range"]
        start, end = (int(x) for x in rng[len("bytes="):].split("-"))
        return SimpleNamespace(status_code=206,
                               content=_PAYLOAD[start:end + 1])

    monkeypatch.setattr(tv.httpx, "request", _fake_request)
    seen = {}

    def _fake_upload(self, drive_id, name, folder_id, size, reader):
        seen["hooks"] = getattr(self, "drive_session_hooks", None)
        reader.read()
        return "newdrivefid"

    monkeypatch.setattr(tv.UserDriveApiClient, "ensure_folder",
                        lambda self, d, n: "ufolder")
    monkeypatch.setattr(tv.UserDriveApiClient, "resumable_upload",
                        _fake_upload)
    monkeypatch.setattr(tv.UserDriveApiClient, "add_anyone_reader",
                        lambda self, d, f: None)
    monkeypatch.setattr(tv.UserDriveApiClient, "list_file_ids_by_name",
                        lambda self, d, f, n: ["newdrivefid"])
    res = tv.run_transfer_job_once(db, job, SimpleNamespace())
    assert res["outcome"] == "done"
    hooks = seen["hooks"] or {}
    assert hooks.get("resume_session_uri") == "https://sess/persisted"
    row = db.query(em.TransferDelivery).filter_by(job_id=job.id).first()
    assert row is not None and row.state == "completed"
