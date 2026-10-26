# -*- coding: utf-8 -*-
"""转存变体机制（FMBY original_transfer 机制化，D1 播放源单独立列）。

全部离线：Drive API 一律用桩替身；中继嗅探用桩 resp/client；
时间走任务行/登记行的时间戳，不依赖真实配置。所有验证均为 mock：
真实 Drive copy 路径、video_stream 全链路接线、目标盘空间均未实测，
见 PR 描述的未验证项，本文件不编造闭环。
"""
import os

os.environ.setdefault("DATABASE_TYPE", "sqlite")
os.environ.setdefault("REDIS_ENABLED", "false")

import asyncio
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend import models as base_models
from backend.emby_server import models as em
from backend.emby_server import transfer_variant as tv
from backend.emby_server import scanner as scanner_mod
from backend.emby_server import streaming as streaming_mod
from backend.emby_server.streaming import serve_mount_relay_async
from backend.emby_server import stream_limits as sl
from backend.integrations import store


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


def _item(db, **kw):
    _counter[0] += 1
    row = em.MediaItem(guid=f"tv-{_counter[0]}", library_id=1,
                       item_type="movie", name="测试片",
                       drive_file_id="origfid",
                       file_path="mount://3/电影/a.mkv", size=1000)
    for key, value in kw.items():
        setattr(row, key, value)
    db.add(row)
    db.commit()
    return row


def _variant(db, item_id, fid="copyfid", scope="source", ref=None,
             status="ready"):
    row = em.TransferVariant(item_id=item_id, source_file_id="origfid",
                             variant_file_id=fid, variant_drive_id="drive1",
                             owner_scope=scope, owner_ref=ref,
                             status=status, size_bytes=1000)
    db.add(row)
    db.commit()
    return row


def _job(db, item_id, trigger="auto", status="done", drive_id="drive1"):
    row = em.TransferJob(item_id=item_id, trigger=trigger,
                         drive_id=drive_id, status=status)
    db.add(row)
    db.commit()
    return row


def _binding(db, user_id, provider="drive", status="active"):
    cred = em.MountCredential(scope="user", owner_user_id=user_id,
                              status="active")
    db.add(cred)
    db.commit()
    row = em.UserStorageBinding(user_id=user_id, provider=provider,
                                account_label="test",
                                credential_id=cred.id,
                                status=status)
    db.add(row)
    db.commit()
    return row


def _user(user_id=7):
    return SimpleNamespace(id=user_id)


def _enable(monkeypatch, auto=False):
    monkeypatch.setenv("TRANSFER_VARIANT_ENABLED", "1")
    if auto:
        monkeypatch.setenv("TRANSFER_VARIANT_AUTO_ENABLED", "1")


# ---------- 选路 ----------

def test_master_off_resolves_original(db, monkeypatch):
    monkeypatch.setenv("TRANSFER_VARIANT_ENABLED", "0")
    item = _item(db)
    _variant(db, item.id)
    fid, row = tv.resolve_play_file_id(db, item, _user())
    assert fid == "origfid" and row is None


def test_source_variant_used_without_binding(db, monkeypatch):
    _enable(monkeypatch)
    item = _item(db)
    _variant(db, item.id)
    fid, row = tv.resolve_play_file_id(db, item, _user())
    assert fid == "copyfid" and row is not None


def test_user_variant_isolated(db, monkeypatch):
    _enable(monkeypatch)
    item = _item(db)
    _variant(db, item.id, fid="usercopy", scope="user", ref="7")
    _variant(db, item.id, fid="sourcecopy", scope="source")
    fid7, _ = tv.resolve_play_file_id(db, item, _user(7))
    assert fid7 == "usercopy"
    fid8, _ = tv.resolve_play_file_id(db, item, _user(8))
    assert fid8 == "sourcecopy"  # 用户 8 用不了用户 7 的副本，落到服主副本


def test_byos_user_skips_source_variant(db, monkeypatch):
    _enable(monkeypatch)
    item = _item(db)
    _binding(db, 7, provider="drive", status="active")
    _variant(db, item.id, fid="sourcecopy", scope="source")
    fid, row = tv.resolve_play_file_id(db, item, _user(7))
    assert fid == "origfid" and row is None  # 服主副本不给 BYOS 用户播


def test_unbind_returns_original(db, monkeypatch):
    _enable(monkeypatch)
    item = _item(db)
    _variant(db, item.id)
    assert tv.release_binding(db, item.id) == 1
    fid, row = tv.resolve_play_file_id(db, item, _user())
    assert fid == "origfid" and row is None


def test_anchor_lock_survives_drive_file_id_rewrite(db, monkeypatch):
    _enable(monkeypatch)
    item = _item(db)
    _variant(db, item.id)
    # 模拟重扫把 drive_file_id 改回原件/其它值：播放源仍指副本。
    item.drive_file_id = "rewrittenfid"
    db.commit()
    fid, _ = tv.resolve_play_file_id(db, item, _user())
    assert fid == "copyfid"


# ---------- Quota 信号识别 ----------

def test_quota_page_detection_matrix():
    assert tv.is_quota_page_bytes(
        200, "text/html; charset=utf-8", b"<html>Quota exceeded</html>")
    assert tv.is_quota_page_bytes(
        200, "text/html", b"DownloadQuotaExceeded")
    assert not tv.is_quota_page_bytes(
        200, "video/mp4", b"Quota exceeded")  # 视频字节不是 html
    assert not tv.is_quota_page_bytes(
        403, "text/html", b"Quota exceeded")  # 403 走现行 403 口径
    assert not tv.is_quota_page_bytes(
        200, "text/html", b"<html>hello</html>")  # 无标记不认
    assert not tv.is_quota_page_bytes(200, "text/html", b"")


# ---------- 触发与节流 ----------

def test_throttle_within_24h(db, monkeypatch):
    _enable(monkeypatch, auto=True)
    item = _item(db)
    _job(db, item.id, trigger="auto", status="done")
    result = tv.note_quota_signal(db, item, _user(), None, "drive1")
    assert result["action"] == "throttled"
    assert db.query(em.TransferJob).count() == 1  # 没建新任务


def test_manual_job_master_off(monkeypatch, db):
    monkeypatch.setenv("TRANSFER_VARIANT_ENABLED", "0")
    item = _item(db)
    with pytest.raises(tv.TransferError) as excinfo:
        tv.create_manual_job(db, item, "drive1")
    assert excinfo.value.kind == "master_off"


def test_manual_job_has_ready_variant(monkeypatch, db):
    _enable(monkeypatch)
    item = _item(db)
    _variant(db, item.id)
    with pytest.raises(tv.TransferError) as excinfo:
        tv.create_manual_job(db, item, "drive1")
    assert excinfo.value.kind == "has_ready_variant"


# ---------- 工人 ----------

class _StubDrive:
    """DriveApiClient 桩：按构造参数决定 copy/权限/get 行为。"""

    def __init__(self, copy_behavior="ok", perm_ok=True, get_ok=True):
        self.copy_behavior = copy_behavior
        self.perm_ok = perm_ok
        self.get_ok = get_ok
        self.copy_calls = 0
        self.perm_calls = []
        self.folders = []

    def ensure_folder(self, drive_id, dir_name):
        self.folders.append(dir_name)
        return "folder1"

    def copy_file(self, drive_id, source_fid, folder_id, name):
        self.copy_calls += 1
        if self.copy_behavior == "forbidden":
            raise tv._CopyForbidden("copy 被拒：403")
        if self.copy_behavior == "flaky" and self.copy_calls < 3:
            raise tv._Retriable("copy 失败：429")
        return {"id": "copyfid", "size": 1000}

    def add_anyone_reader(self, drive_id, fid):
        self.perm_calls.append(fid)
        if not self.perm_ok:
            raise Exception("补 anyone 权限失败：500")

    def get_file(self, drive_id, fid):
        if not self.get_ok:
            raise tv._NotFound("副本不存在")
        return {"id": fid, "trashed": False}


def _pending_job(db, item_id, drive_id="drive1"):
    return _job(db, item_id, trigger="manual", status="pending",
                drive_id=drive_id)


def test_worker_copy_success(db):
    item = _item(db)
    job = _pending_job(db, item.id)
    stub = _StubDrive()
    result = tv.run_transfer_job_once(db, job, stub)
    assert result["outcome"] == "done"
    assert job.status == "done" and job.attempts == 1
    row = db.query(em.TransferVariant).one()
    assert row.status == "ready" and row.variant_file_id == "copyfid"
    assert row.size_bytes == 1000 and stub.perm_calls == ["copyfid"]


def test_worker_copy_403_dead_no_retry(db):
    item = _item(db)
    job = _pending_job(db, item.id)
    result = tv.run_transfer_job_once(db, job, _StubDrive("forbidden"))
    assert result["outcome"] == "failed" and result["error_kind"] == "dead"
    assert job.status == "failed" and job.attempts == 1  # 不重试
    assert db.query(em.TransferVariant).count() == 0


def test_worker_copy_429_twice_then_success(db):
    item = _item(db)
    job = _pending_job(db, item.id)
    stub = _StubDrive("flaky")
    # 第 1 次：429 → pending + 退避；第 2 次：429 → pending；第 3 次：成功。
    assert tv.run_transfer_job_once(db, job, stub)["outcome"] == "retried"
    job.next_retry_at = None
    db.commit()
    assert tv.run_transfer_job_once(db, job, stub)["outcome"] == "retried"
    job.next_retry_at = None
    db.commit()
    assert tv.run_transfer_job_once(db, job, stub)["outcome"] == "done"
    assert job.attempts == 3


def test_worker_permission_failure_orphan(db):
    item = _item(db)
    job = _pending_job(db, item.id)
    result = tv.run_transfer_job_once(
        db, job, _StubDrive(perm_ok=False))
    assert result["outcome"] == "failed" and result["error_kind"] == "orphan"
    row = db.query(em.TransferVariant).one()
    assert row.status == "orphan" and row.variant_file_id == "copyfid"


# ---------- 扫描排除 ----------

def test_scanner_skips_transfer_dir(monkeypatch):
    monkeypatch.setenv("TRANSFER_VARIANT_DIR", "aetrix-transfer")

    class _Provider:
        local_root = None

        def walk_media(self, root=None):
            return [
                SimpleNamespace(rel="/电影/a.mp4", name="a.mp4", size=10),
                SimpleNamespace(rel="/aetrix-transfer/x.mp4", name="x.mp4",
                                size=10),
                SimpleNamespace(rel="aetrix-transfer/y.mp4", name="y.mp4",
                                size=10),
            ]

    src = SimpleNamespace(mount=SimpleNamespace(id=3), subpath="/",
                          source_row_id=None)
    rels = [f.rel for f in scanner_mod._mount_files(src, _Provider(), [])]
    assert "/电影/a.mp4" in rels
    assert "/aetrix-transfer/x.mp4" not in rels
    assert "aetrix-transfer/y.mp4" not in rels


# ---------- 巡检 ----------

def test_patrol_two_strikes(db):
    item = _item(db)
    row = _variant(db, item.id)
    stub = _StubDrive(get_ok=False)
    tv.patrol_once(db, lambda: stub)
    db.refresh(row)
    assert row.status == "ready" and row.stale_count == 1  # 第一次仍 ready
    tv.patrol_once(db, lambda: stub)
    db.refresh(row)
    assert row.status == "stale" and row.stale_count == 2  # 第二次算死


# ---------- 播放中变体遇 Quota ----------

def test_playing_variant_quota_marks_stale_and_queues(db, monkeypatch):
    _enable(monkeypatch, auto=True)
    item = _item(db)
    row = _variant(db, item.id)
    result = tv.note_quota_signal(db, item, _user(), row, "drive1")
    assert result["action"] == "queued"
    db.refresh(row)
    assert row.status == "stale"  # 播放实证立即标死，不走两次规则
    job = db.query(em.TransferJob).one()
    assert job.trigger == "auto" and job.status == "pending"


# ---------- 中继嗅探 ----------

class _FakeResp:
    def __init__(self, chunks, content_type="text/html; charset=utf-8"):
        self._chunks = list(chunks)
        self.status_code = 200
        self.headers = {"content-type": content_type}
        self.closed = False

    async def aiter_bytes(self, _n):
        for chunk in self._chunks:
            yield chunk

    async def aclose(self):
        self.closed = True


class _FakeClient:
    def __init__(self):
        self.closed = False

    async def aclose(self):
        self.closed = True


def _sniff_setup(monkeypatch, resp):
    client = _FakeClient()

    async def _follow(c, url, forward, silent=False):
        return resp, url, 0

    monkeypatch.setattr(streaming_mod, "_follow_redirects", _follow)
    monkeypatch.setattr(streaming_mod, "_mount_relay_client",
                        lambda: client)
    return client


def test_relay_sniff_quota_raises(monkeypatch):
    resp = _FakeResp([b"<html>Quota exceeded</html>", b"rest"])
    client = _sniff_setup(monkeypatch, resp)
    request = SimpleNamespace(headers={})
    with pytest.raises(tv.QuotaPageError):
        asyncio.run(serve_mount_relay_async(
            "https://example.com/file", request, sniff_quota_page=True))
    assert resp.closed and client.closed


def test_relay_sniff_non_quota_streams_full_body(monkeypatch):
    resp = _FakeResp([b"<html>hello</html>", b"more-bytes"])
    _sniff_setup(monkeypatch, resp)
    request = SimpleNamespace(headers={})

    # 注意：必须在同一个 asyncio.run 内建响应并收完 body——跨 run 时
    # run 结束会 shutdown_asyncgens，把嗅探已预读一半的内层生成器关掉，
    # 第二个 run 再续读只剩首块。这是测试写法问题，不是嗅探逻辑问题
    # （真实请求里建响应与收 body 都在同一请求循环内）。
    async def _serve_and_collect():
        response = await serve_mount_relay_async(
            "https://example.com/file", request, sniff_quota_page=True)
        return b"".join([c async for c in response.body_iterator])

    body = asyncio.run(_serve_and_collect())
    assert body == b"<html>hello</html>more-bytes"  # 首块补回，一个不少
