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
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend import models as base_models
from backend.emby_server import models as em
from backend.emby_server import transfer_variant as tv
from backend.emby_server import play_variants
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
    # 2026-10-30 连接复用后：client 按 loop 共享，Quota 路径只关
    # 响应（连接处置由池负责），不许再关 client——关了后续请求全废。
    assert resp.closed and not client.closed


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


# ---------- 首播转存门禁（2026-10-26 先转存再播） ----------

def _mount(db, mount_id, mount_type, path):
    row = em.StorageMount(id=mount_id, name=f"m{mount_id}",
                          mount_type=mount_type, path=path)
    db.add(row)
    db.commit()
    return row


def _user_job(db, item_id, user_id, provider="drive", status="pending",
              drive_id="drive1", trigger="playback"):
    dest = (f"userdrive:{user_id}" if provider == "drive"
            else f"pan115:user-{user_id}")
    row = em.TransferJob(item_id=item_id, trigger=trigger,
                         drive_id=drive_id, provider=provider,
                         owner_scope="user", owner_ref=str(user_id),
                         dest_drive_id=dest, status=status)
    db.add(row)
    db.commit()
    return row


def test_gate_master_off_proceeds(db, monkeypatch):
    monkeypatch.setenv("TRANSFER_VARIANT_ENABLED", "0")
    _mount(db, 3, "drive", "drive:drive1")
    item = _item(db)
    _binding(db, 7)
    assert tv.ensure_user_copy_gate(db, item, _user())["verdict"] == "proceed"
    assert db.query(em.TransferJob).count() == 0


def test_gate_no_binding_proceeds(db, monkeypatch):
    _enable(monkeypatch)
    _mount(db, 3, "drive", "drive:drive1")
    item = _item(db)
    gate = tv.ensure_user_copy_gate(db, item, _user())
    assert gate == {"verdict": "proceed", "reason": "no_matching_binding"}
    assert db.query(em.TransferJob).count() == 0


def test_gate_provider_mismatch_goes_cross(db, monkeypatch):
    # 2026-10-26 20:54 定版（2026-10-29 曾关停、2026-10-30 用户
    # 亲点重开恢复）：错配不放行共享链，只绑另一 provider 时走
    # 跨 provider 转存（绑哪播哪），门禁呈准备中。
    _enable(monkeypatch)
    _mount(db, 3, "drive", "drive:drive1")
    item = _item(db)
    _binding(db, 7, provider="pan115")
    gate = tv.ensure_user_copy_gate(db, item, _user())
    assert gate["verdict"] == "preparing"
    assert gate["reason"] == "job_created"
    job = db.query(em.TransferJob).one()
    assert job.provider == "drive" and job.owner_scope == "user"
    assert job.owner_ref == "7" and job.drive_id == "drive1"
    assert job.dest_drive_id == "pan115:user-7" and job.status == "pending"
    assert tv._is_cross_job(job) is True
    # 准备中文案（2026-10-30 口径 5）：说清片在哪边盘、绑的哪边、
    # 正在往绑的盘转、备好再播，不许只给笼统失败。
    assert "谷歌盘" in gate["detail"]
    assert "115 网盘" in gate["detail"]
    assert "转存完成后即可播放" in gate["detail"]


def test_gate_creates_user_job_preparing(db, monkeypatch):
    _enable(monkeypatch)
    _mount(db, 3, "drive", "drive:drive1")
    item = _item(db)
    _binding(db, 7)
    gate = tv.ensure_user_copy_gate(db, item, _user())
    assert gate["verdict"] == "preparing" and gate["reason"] == "job_created"
    job = db.query(em.TransferJob).one()
    assert job.provider == "drive" and job.owner_scope == "user"
    assert job.owner_ref == "7" and job.drive_id == "drive1"
    assert job.dest_drive_id == "userdrive:7" and job.status == "pending"


def test_gate_no_duplicate_job(db, monkeypatch):
    _enable(monkeypatch)
    _mount(db, 3, "drive", "drive:drive1")
    item = _item(db)
    _binding(db, 7)
    _user_job(db, item.id, 7)
    gate = tv.ensure_user_copy_gate(db, item, _user())
    assert gate["verdict"] == "preparing" and gate["reason"] == "job_in_flight"
    assert db.query(em.TransferJob).count() == 1


def test_gate_ready_drive_copy_proceeds(db, monkeypatch):
    _enable(monkeypatch)
    _mount(db, 3, "drive", "drive:drive1")
    item = _item(db)
    _binding(db, 7)
    _variant(db, item.id, fid="usercopy", scope="user", ref="7")
    gate = tv.ensure_user_copy_gate(db, item, _user())
    assert gate["verdict"] == "proceed"
    assert gate["reason"] == "has_ready_variant"


def test_gate_failed_dead_job(db, monkeypatch):
    _enable(monkeypatch)
    _mount(db, 3, "drive", "drive:drive1")
    item = _item(db)
    _binding(db, 7)
    job = _user_job(db, item.id, 7, status="failed")
    job.error_kind = "dead"
    db.commit()
    gate = tv.ensure_user_copy_gate(db, item, _user())
    assert gate == {"verdict": "failed", "reason": "dead",
                    "job_id": job.id}


def test_gate_done_job_copy_gone_rebuilds(db, monkeypatch):
    # 2026-10-29 生产实证：done 但副本已被清掉（无 ready 行），旧逻辑
    # 24h 内永久 throttled（99 用户 357 条卡死）。现在预算内重建一单。
    _enable(monkeypatch)
    _mount(db, 3, "drive", "drive:drive1")
    item = _item(db)
    _binding(db, 7)
    _user_job(db, item.id, 7, status="done")
    gate = tv.ensure_user_copy_gate(db, item, _user())
    assert gate["verdict"] == "preparing"
    assert gate["reason"] == "job_rebuilt"
    assert db.query(em.TransferJob).count() == 2
    new_job = db.query(em.TransferJob).order_by(
        em.TransferJob.id.desc()).first()
    assert new_job.status == "pending" and new_job.trigger == "playback"
    assert new_job.dest_drive_id == "userdrive:7"
    assert gate["job_id"] == new_job.id
    # 同一次播放的后续重查只见在途，不再建第 3 单。
    gate2 = tv.ensure_user_copy_gate(db, item, _user())
    assert gate2["reason"] == "job_in_flight"
    assert db.query(em.TransferJob).count() == 2


def test_gate_done_job_copy_gone_budget_exhausted(db, monkeypatch):
    # 24h 同桶已有 2 行（上次 done + 已重建过一次又 done）：不再建，
    # 仍 throttled，防播放器重试刷任务洪水。
    _enable(monkeypatch)
    _mount(db, 3, "drive", "drive:drive1")
    item = _item(db)
    _binding(db, 7)
    _user_job(db, item.id, 7, status="done", trigger="prefetch")
    job = _user_job(db, item.id, 7, status="done")
    gate = tv.ensure_user_copy_gate(db, item, _user())
    assert gate == {"verdict": "preparing", "reason": "throttled",
                    "job_id": job.id}
    assert db.query(em.TransferJob).count() == 2


def test_gate_done_job_with_ready_copy_proceeds(db, monkeypatch):
    # done 且副本仍 ready：直接播本人副本，不重建。
    _enable(monkeypatch)
    _mount(db, 3, "drive", "drive:drive1")
    item = _item(db)
    _binding(db, 7)
    _user_job(db, item.id, 7, status="done")
    _variant(db, item.id, fid="usercopy", scope="user", ref="7")
    gate = tv.ensure_user_copy_gate(db, item, _user())
    assert gate["verdict"] == "proceed"
    assert gate["reason"] == "has_ready_variant"
    assert db.query(em.TransferJob).count() == 1


def test_gate_cross_done_job_copy_gone_rebuilds(db, monkeypatch):
    # 跨 provider 同款：只绑 115 的用户，跨单 done 但 115 副本没了。
    _enable(monkeypatch)
    _mount(db, 3, "drive", "drive:drive1")
    item = _item(db)
    _binding(db, 7, provider="pan115")
    _user_job(db, item.id, 7, provider="pan115", status="done")
    gate = tv.ensure_user_copy_gate(db, item, _user())
    assert gate["verdict"] == "preparing"
    assert gate["reason"] == "job_rebuilt"
    new_job = db.query(em.TransferJob).order_by(
        em.TransferJob.id.desc()).first()
    assert new_job.status == "pending"
    assert new_job.dest_drive_id == "pan115:user-7"


def test_wait_for_transfer_ready_accepts_rebuilt():
    async def _re_gate():
        return {"verdict": "proceed", "reason": "has_ready_variant"}

    out = asyncio.run(tv.wait_for_transfer_ready(
        {"verdict": "preparing", "reason": "job_rebuilt"},
        _re_gate, max_sec=5, poll_sec=0.01))
    assert out["verdict"] == "proceed"


def test_gate_per_user_throttle_isolated(db, monkeypatch):
    _enable(monkeypatch)
    _mount(db, 3, "drive", "drive:drive1")
    item = _item(db)
    _binding(db, 7)
    _binding(db, 8)
    _user_job(db, item.id, 7)  # 用户 A 的任务绝不能堵住用户 B
    gate = tv.ensure_user_copy_gate(db, item, _user(8))
    assert gate["verdict"] == "preparing" and gate["reason"] == "job_created"
    assert db.query(em.TransferJob).count() == 2


def test_gate_byos_item_proceeds(db, monkeypatch):
    _enable(monkeypatch)
    item = _item(db, file_path="byos://2/电影/a.mkv")
    _binding(db, 7)
    gate = tv.ensure_user_copy_gate(db, item, _user())
    assert gate == {"verdict": "proceed", "reason": "byos_item"}


def test_gate_play_sign_consume_proceeds(db, monkeypatch):
    _enable(monkeypatch)
    _mount(db, 3, "drive", "drive:drive1")
    item = _item(db)
    _binding(db, 7)
    gate = tv.ensure_user_copy_gate(db, item, _user(),
                                    play_sign_consume=True)
    assert gate == {"verdict": "proceed", "reason": "play_sign_consume"}
    assert db.query(em.TransferJob).count() == 0


# ---------- 2026-10-29 #1453 file-only 回归修复 ----------

def _fake_drive_resp(status_code, files=None, text=""):
    return SimpleNamespace(status_code=status_code,
                           json=lambda: {"files": files or []},
                           text=text)


def test_ensure_folder_fileonly_root_list_403_finds_app_folder(monkeypatch):
    # 根枚举 403 → 按名查应用自创文件，查到即复用，不走创建。
    calls = []

    def fake_request(self, method, url, drive_id, **kwargs):
        q = (kwargs.get("params") or {}).get("q", "")
        calls.append((method, q))
        if method == "GET" and "'root' in parents" in q:
            return _fake_drive_resp(
                403, text='{"error": {"code": 403, "message": "forbidden", '
                          '"errors": [{"reason": "insufficientPermissions"}]}}')
        if method == "GET":
            return _fake_drive_resp(200, files=[{"id": "appfolder1",
                                                "name": "aetrix-transfer"}])
        raise AssertionError("unexpected drive call")

    monkeypatch.setattr(tv.DriveApiClient, "_request", fake_request)
    assert (tv.DriveApiClient().ensure_folder("myDrive:u7", "aetrix-transfer")
            == "appfolder1")
    assert len(calls) == 2 and all(m == "GET" for m, _ in calls)


def test_ensure_folder_fileonly_root_list_403_creates_when_absent(monkeypatch):
    # 根枚举 403 + 按名查 200 无匹配 = 目录确实不存在，才走创建。
    def fake_request(self, method, url, drive_id, **kwargs):
        if method == "GET":
            q = (kwargs.get("params") or {}).get("q", "")
            if "'root' in parents" in q:
                return _fake_drive_resp(403, text='{"error": {"code": 403}}')
            return _fake_drive_resp(200, files=[])
        if method == "POST":
            return SimpleNamespace(status_code=201,
                                   json=lambda: {"id": "newfolder"},
                                   text="")
        raise AssertionError("unexpected drive call")

    monkeypatch.setattr(tv.DriveApiClient, "_request", fake_request)
    assert (tv.DriveApiClient().ensure_folder("myDrive:u7", "aetrix-transfer")
            == "newfolder")


def test_ensure_folder_fileonly_both_lists_403_raises_with_body(monkeypatch):
    # 两次列举都 403 时不盲建（会重名），且异常带 Drive 错误体。
    def fake_request(self, method, url, drive_id, **kwargs):
        assert method == "GET"
        return _fake_drive_resp(
            403, text='{"error": {"code": 403, "errors": '
                      '[{"reason": "insufficientPermissions"}]}}')

    monkeypatch.setattr(tv.DriveApiClient, "_request", fake_request)
    with pytest.raises(tv._Retriable) as excinfo:
        tv.DriveApiClient().ensure_folder("myDrive:u7", "aetrix-transfer")
    assert "insufficientPermissions" in str(excinfo.value)


def test_ensure_folder_primary_200_unchanged(monkeypatch):
    # 双范围／SA 旧路径：首查 200 即回，不碰回落。
    calls = []

    def fake_request(self, method, url, drive_id, **kwargs):
        calls.append(method)
        return _fake_drive_resp(200, files=[{"id": "rootfolder",
                                            "name": "aetrix-transfer"}])

    monkeypatch.setattr(tv.DriveApiClient, "_request", fake_request)
    assert (tv.DriveApiClient().ensure_folder("myDrive:u7", "aetrix-transfer")
            == "rootfolder")
    assert calls == ["GET"]


def test_gate_failed_retriable_retriggers_once(db, monkeypatch):
    # job 422 原型：24h 内失败的可重试任务，播放时重建一单走准备中，
    # 不再永久 409；同请求内重查只见在途任务，不重复建单。
    _enable(monkeypatch)
    _mount(db, 3, "drive", "drive:drive1")
    item = _item(db)
    _binding(db, 7)
    job = _user_job(db, item.id, 7, status="failed")
    job.error_kind = "retriable"
    job.error_detail = "列转存目录失败：403"
    db.commit()
    gate = tv.ensure_user_copy_gate(db, item, _user())
    assert gate["verdict"] == "preparing"
    assert gate["reason"] == "job_retriggered"
    assert db.query(em.TransferJob).count() == 2
    new_job = db.query(em.TransferJob).order_by(
        em.TransferJob.id.desc()).first()
    assert new_job.status == "pending" and new_job.trigger == "playback"
    assert gate["job_id"] == new_job.id
    gate2 = tv.ensure_user_copy_gate(db, item, _user())
    assert gate2["verdict"] == "preparing"
    assert gate2["reason"] == "job_in_flight"
    assert db.query(em.TransferJob).count() == 2


def test_gate_failed_retriable_budget_exhausted(db, monkeypatch):
    # 桶内 24h 已有 2 行（失败 + 已重触发过一次）：不再建第 3 单，
    # 仍判失败 409，不无限重试。
    _enable(monkeypatch)
    _mount(db, 3, "drive", "drive:drive1")
    item = _item(db)
    _binding(db, 7)
    _user_job(db, item.id, 7, status="failed", trigger="prefetch")
    job = _user_job(db, item.id, 7, status="failed")
    job.error_kind = "retriable"
    db.commit()
    gate = tv.ensure_user_copy_gate(db, item, _user())
    assert gate == {"verdict": "failed", "reason": "retriable",
                    "job_id": job.id}
    assert db.query(em.TransferJob).count() == 2


def test_gate_failed_retriable_old_job_outside_window(db, monkeypatch):
    # 失败行已出 24h 窗口：预算未耗，重触发仍放行。
    _enable(monkeypatch)
    _mount(db, 3, "drive", "drive:drive1")
    item = _item(db)
    _binding(db, 7)
    job = _user_job(db, item.id, 7, status="failed")
    job.error_kind = "retriable"
    job.created_at = datetime.now() - timedelta(days=2)
    db.commit()
    gate = tv.ensure_user_copy_gate(db, item, _user())
    assert gate["verdict"] == "preparing"
    assert gate["reason"] == "job_retriggered"
    assert db.query(em.TransferJob).count() == 2


def test_gate_failed_retriable_cross_retriggers(db, monkeypatch):
    # 跨 provider 分支同款：只绑 115 的用户播 Drive 条目，跨单失败
    # 可重试时同样重触发跨单，不永久 409。
    _enable(monkeypatch)
    _mount(db, 3, "drive", "drive:drive1")
    item = _item(db)
    _binding(db, 7, provider="pan115")
    job = _user_job(db, item.id, 7, provider="pan115", status="failed")
    job.error_kind = "retriable"
    db.commit()
    gate = tv.ensure_user_copy_gate(db, item, _user())
    assert gate["verdict"] == "preparing"
    assert gate["reason"] == "job_retriggered"
    assert db.query(em.TransferJob).count() == 2
    new_job = db.query(em.TransferJob).order_by(
        em.TransferJob.id.desc()).first()
    assert new_job.status == "pending" and new_job.provider == "drive"
    assert new_job.dest_drive_id == "pan115:user-7"
    assert tv._is_cross_job(new_job) is True


def test_wait_for_transfer_ready_accepts_retriggered():
    # 等流循环须等 job_retriggered（否则重触发当次请求不等流，
    # 直接 409，白白多一次播放器重试）。
    async def _re_gate():
        return {"verdict": "proceed", "reason": "has_ready_variant"}

    out = asyncio.run(tv.wait_for_transfer_ready(
        {"verdict": "preparing", "reason": "job_retriggered"},
        _re_gate, max_sec=5, poll_sec=0.01))
    assert out["verdict"] == "proceed"


def test_release_ready_rows_scoped_by_owner(db):
    item = _item(db)
    row_a = _variant(db, item.id, fid="copy-a", scope="user", ref="7")
    row_b = _variant(db, item.id, fid="copy-b", scope="user", ref="8")
    assert tv._release_ready_rows(db, item.id, "user", "7") == 1
    db.commit()  # 释放函数约定调用方 commit，提交后刷新才可见。
    db.refresh(row_a)
    db.refresh(row_b)
    assert row_a.status == "released" and row_b.status == "ready"


# ---------- Drive 用户盘工人（Path A / Path B） ----------

class _FakeUserDrive:
    """UserDriveApiClient 桩：mode 决定 copy 行为。

    dir_ids：list_file_ids_by_name 的返回值；None 时默认返回 copy
    的 id（即目录核对通过）。传 [] 表示目录里核不到，传其它列表
    可模拟目录 id 胜出 / 多同名冲突。
    """

    def __init__(self, mode="path_a", dir_ids=None):
        self.mode = mode
        self.dir_ids = dir_ids
        self.copy_calls = []
        self.perm_calls = []
        self._tmp_forbidden_once = True

    def ensure_folder(self, drive_id, dir_name):
        return "ufolder"

    def list_file_ids_by_name(self, drive_id, folder_id, name):
        if self.dir_ids is not None:
            return list(self.dir_ids)
        return ["usercopy"]

    def copy_file(self, drive_id, source_fid, folder_id, name):
        self.copy_calls.append(source_fid)
        if self.mode == "path_a":
            return {"id": "usercopy", "size": 1000}
        if source_fid == "origfid":
            # Path A：用户无权源，403 转 Path B。
            raise tv._CopyForbidden("copy 被拒：403")
        if self.mode == "path_b_flaky_tmp" and self._tmp_forbidden_once:
            self._tmp_forbidden_once = False
            raise tv._CopyForbidden("copy 被拒：403")
        return {"id": "usercopy", "size": 1000}

    def add_anyone_reader(self, drive_id, fid):
        self.perm_calls.append(fid)


class _StubDriveSA:
    """Path B 的 SA 侧桩：暂存建/复用/删全程可观测。"""

    def __init__(self, copy_behavior="ok", tmp_exists=False):
        self.copy_behavior = copy_behavior
        self.tmp_exists = tmp_exists
        self.copy_calls = 0
        self.deleted = []

    def ensure_folder(self, drive_id, dir_name):
        return "tfolder"

    def find_file_by_name(self, drive_id, folder_id, name):
        return "tmpfid" if self.tmp_exists else None

    def copy_file(self, drive_id, source_fid, folder_id, name):
        self.copy_calls += 1
        if self.copy_behavior == "forbidden":
            raise tv._CopyForbidden("copy 被拒：403")
        return {"id": "tmpfid", "size": 1000}

    def add_anyone_reader(self, drive_id, fid):
        pass

    def delete_file(self, drive_id, fid):
        self.deleted.append(fid)


def _user_pending_job(db, item_id, user_id=7, drive_id="drive1"):
    return _user_job(db, item_id, user_id, status="pending",
                     drive_id=drive_id, trigger="playback")


def test_worker_user_path_a_success(db, monkeypatch):
    item = _item(db)
    _binding(db, 7)
    _variant(db, item.id, fid="other-copy", scope="user", ref="8")
    job = _user_pending_job(db, item.id)
    fake = _FakeUserDrive("path_a")
    monkeypatch.setattr(tv, "UserDriveApiClient",
                        lambda db_arg, binding: fake)
    result = tv.run_transfer_job_once(db, job, _StubDriveSA())
    assert result["outcome"] == "done" and job.status == "done"
    rows = db.query(em.TransferVariant).filter(
        em.TransferVariant.status == "ready").all()
    assert len(rows) == 2  # 用户 8 的行不受影响（按 owner 释放）
    mine = [r for r in rows if r.owner_ref == "7"]
    assert len(mine) == 1 and mine[0].variant_file_id == "usercopy"
    assert mine[0].variant_drive_id == "userdrive:7"
    assert fake.perm_calls == ["usercopy"]


def test_worker_user_path_b_success_and_tmp_cleanup(db, monkeypatch):
    item = _item(db)
    _binding(db, 7)
    job = _user_pending_job(db, item.id)
    fake = _FakeUserDrive("path_b")
    monkeypatch.setattr(tv, "UserDriveApiClient",
                        lambda db_arg, binding: fake)
    sa = _StubDriveSA()
    result = tv.run_transfer_job_once(db, job, sa)
    assert result["outcome"] == "done"
    assert sa.copy_calls == 1 and sa.deleted == ["tmpfid"]
    row = db.query(em.TransferVariant).filter(
        em.TransferVariant.status == "ready").one()
    assert row.variant_file_id == "usercopy" and row.owner_ref == "7"


def test_worker_user_path_b_sa_dead(db, monkeypatch):
    item = _item(db)
    _binding(db, 7)
    job = _user_pending_job(db, item.id)
    fake = _FakeUserDrive("path_b")
    monkeypatch.setattr(tv, "UserDriveApiClient",
                        lambda db_arg, binding: fake)
    result = tv.run_transfer_job_once(db, job,
                                      _StubDriveSA("forbidden"))
    assert result["outcome"] == "failed" and result["error_kind"] == "dead"
    assert db.query(em.TransferVariant).count() == 0


def test_worker_user_no_binding(db):
    item = _item(db)
    job = _user_pending_job(db, item.id)
    result = tv.run_transfer_job_once(db, job, _StubDriveSA())
    assert result["outcome"] == "failed"
    assert result["error_kind"] == "no_binding"


def test_worker_user_path_b_tmp_reused_on_retry(db, monkeypatch):
    item = _item(db)
    _binding(db, 7)
    job = _user_pending_job(db, item.id)
    fake = _FakeUserDrive("path_b_flaky_tmp")
    monkeypatch.setattr(tv, "UserDriveApiClient",
                        lambda db_arg, binding: fake)
    sa = _StubDriveSA()
    # 第 1 次：暂存分享后用户 copy 403（传播延迟）→ retried。
    assert tv.run_transfer_job_once(db, job, sa)["outcome"] == "retried"
    assert sa.copy_calls == 1
    # 第 2 次：暂存已存在，复用不重建，SA copy 不再调用。
    job.next_retry_at = None
    sa.tmp_exists = True
    db.commit()
    assert tv.run_transfer_job_once(db, job, sa)["outcome"] == "done"
    assert sa.copy_calls == 1 and sa.deleted == ["tmpfid"]


# ---------- 转存后目录核对（FMBY discover_transfer_target 口径） ----------

def test_worker_user_path_a_dir_missing_copy_unresolved(db, monkeypatch):
    item = _item(db)
    _binding(db, 7)
    job = _user_pending_job(db, item.id)
    fake = _FakeUserDrive("path_a", dir_ids=[])
    monkeypatch.setattr(tv, "UserDriveApiClient",
                        lambda db_arg, binding: fake)
    result = tv.run_transfer_job_once(db, job, _StubDriveSA())
    assert result["outcome"] == "failed"
    assert result["error_kind"] == "copy_unresolved"
    assert job.status == "failed"
    assert db.query(em.TransferVariant).count() == 0
    assert fake.perm_calls == []  # 核不到不补权限、不登记、不回落


def test_worker_user_path_a_dir_id_wins(db, monkeypatch):
    item = _item(db)
    _binding(db, 7)
    job = _user_pending_job(db, item.id)
    fake = _FakeUserDrive("path_a", dir_ids=["dir-456"])
    monkeypatch.setattr(tv, "UserDriveApiClient",
                        lambda db_arg, binding: fake)
    result = tv.run_transfer_job_once(db, job, _StubDriveSA())
    assert result["outcome"] == "done"
    row = db.query(em.TransferVariant).filter(
        em.TransferVariant.status == "ready").one()
    assert row.variant_file_id == "dir-456"  # 目录核到的 id 为准
    assert fake.perm_calls == ["dir-456"]  # 权限补在核到的 id 上


def test_worker_user_path_a_dir_exact_fid_wins(db, monkeypatch):
    item = _item(db)
    _binding(db, 7)
    job = _user_pending_job(db, item.id)
    fake = _FakeUserDrive("path_a", dir_ids=["usercopy", "other"])
    monkeypatch.setattr(tv, "UserDriveApiClient",
                        lambda db_arg, binding: fake)
    result = tv.run_transfer_job_once(db, job, _StubDriveSA())
    assert result["outcome"] == "done"
    row = db.query(em.TransferVariant).filter(
        em.TransferVariant.status == "ready").one()
    assert row.variant_file_id == "usercopy"  # 回执 id 在目录中即核到


def test_worker_user_path_a_dir_conflict_copy_unresolved(db, monkeypatch):
    item = _item(db)
    _binding(db, 7)
    job = _user_pending_job(db, item.id)
    fake = _FakeUserDrive("path_a", dir_ids=["x1", "x2"])
    monkeypatch.setattr(tv, "UserDriveApiClient",
                        lambda db_arg, binding: fake)
    result = tv.run_transfer_job_once(db, job, _StubDriveSA())
    assert result["outcome"] == "failed"
    assert result["error_kind"] == "copy_unresolved"
    assert db.query(em.TransferVariant).count() == 0  # 多同名冲突不猜


def test_worker_user_path_a_dir_list_failure_retried(db, monkeypatch):
    item = _item(db)
    _binding(db, 7)
    job = _user_pending_job(db, item.id)
    fake = _FakeUserDrive("path_a")

    def _boom(*args):
        raise tv._Retriable("按名列出文件失败：500")

    fake.list_file_ids_by_name = _boom
    monkeypatch.setattr(tv, "UserDriveApiClient",
                        lambda db_arg, binding: fake)
    result = tv.run_transfer_job_once(db, job, _StubDriveSA())
    assert result["outcome"] == "retried"  # 目录列举失败按可重试处理
    assert db.query(em.TransferVariant).count() == 0


def test_worker_user_path_b_dir_missing_copy_unresolved(db, monkeypatch):
    item = _item(db)
    _binding(db, 7)
    job = _user_pending_job(db, item.id)
    fake = _FakeUserDrive("path_b", dir_ids=[])
    monkeypatch.setattr(tv, "UserDriveApiClient",
                        lambda db_arg, binding: fake)
    sa = _StubDriveSA()
    result = tv.run_transfer_job_once(db, job, sa)
    assert result["outcome"] == "failed"
    assert result["error_kind"] == "copy_unresolved"
    assert db.query(em.TransferVariant).count() == 0
    assert sa.deleted == []  # 核不到失败时暂存不删，下次按名复用


# ---------- Quota 信号 BYOS 分支 ----------

def test_quota_byos_creates_user_job(db, monkeypatch):
    _enable(monkeypatch, auto=True)
    _mount(db, 3, "drive", "drive:drive1")
    item = _item(db)
    _binding(db, 7)
    result = tv.note_quota_signal(db, item, _user(), None, "drive1")
    assert result["action"] == "queued"
    job = db.query(em.TransferJob).one()
    assert job.provider == "drive" and job.owner_scope == "user"
    assert job.owner_ref == "7" and job.trigger == "auto"


def test_quota_byos_throttled_per_user(db, monkeypatch):
    _enable(monkeypatch, auto=True)
    _mount(db, 3, "drive", "drive:drive1")
    item = _item(db)
    _binding(db, 7)
    _binding(db, 8)
    _user_job(db, item.id, 7, status="pending", trigger="auto")
    assert tv.note_quota_signal(
        db, item, _user(7), None, "drive1")["action"] == "throttled"
    assert tv.note_quota_signal(
        db, item, _user(8), None, "drive1")["action"] == "queued"


# ---------- 准备中态覆盖 ----------

def _base_source():
    return {"Name": "测试片", "SupportsDirectPlay": True,
            "SupportsDirectStream": True, "DirectPlayUrl": "http://x/s",
            "DirectStreamUrl": "http://x/s", "SupportsTranscoding": False}


def _variants():
    return [play_variants.Variant(name="drive_relay", label="中继",
                                   protocol="Http", is_remote=False,
                                   needs_relay=True)]


def test_build_media_sources_preparing_overlay():
    sources = play_variants.build_media_sources(
        _base_source(), _variants(), guid="g1",
        stream_url="http://x/s", direct_stream_ok=True,
        transfer_status="preparing")
    assert len(sources) == 1
    src = sources[0]
    assert src["TransferStatus"] == "preparing"
    assert src["SupportsDirectPlay"] is False
    assert src["SupportsDirectStream"] is False
    assert "DirectPlayUrl" not in src and "DirectStreamUrl" not in src


def test_build_media_sources_ready_unchanged():
    sources = play_variants.build_media_sources(
        _base_source(), _variants(), guid="g1",
        stream_url="http://x/s", direct_stream_ok=True,
        transfer_status="ready")
    src = sources[0]
    assert "TransferStatus" not in src
    assert src["SupportsDirectPlay"] is True
    assert src["DirectPlayUrl"].startswith("http://x/s")
    assert "variant=drive_relay" in src["DirectPlayUrl"]


def test_apply_transfer_status_failed_overlay():
    sources = [_base_source()]
    play_variants.apply_transfer_status(sources, "failed")
    assert sources[0]["TransferStatus"] == "failed"
    assert sources[0]["SupportsDirectPlay"] is False
    assert "DirectPlayUrl" not in sources[0]


# ---------- 跨 provider 转存（2026-10-26 20:54 用户亲定“绑哪播哪”） ----------
# 全部离线：SA 下载、115 上传、用户 Drive 上传一律用桩替身；
# 真实跨 provider 搬字节（真实 Drive copy/上传、真实 115 上传口）
# 未实测，见 PR 描述的未验证项，本文件不编造闭环。

_PAYLOAD = bytes(range(256)) * 4  # 1024 字节


class _FakeUser115:
    """用户侧 115 桩：转存目录预建，simple_upload 吃掉读取器字节。"""

    def __init__(self, uploaded_name="a.mkv"):
        self.dirs = {
            "0": [{"name": "aetrix-transfer", "is_dir": True, "cid": "900"}],
            "900": [],
        }
        self.uploaded_name = uploaded_name
        self.uploaded = None

    def list_dir(self, cid="0", limit=200):
        return list(self.dirs.get(str(cid), []))

    def mkdir(self, parent_cid, name):
        self.dirs.setdefault("0", []).append(
            {"name": name, "is_dir": True, "cid": "900"})
        return {}

    def simple_upload(self, fileobj, filename, pid):
        self.uploaded = (filename, pid, fileobj.read())
        self.dirs["900"] = [{"name": filename, "is_dir": False,
                             "fid": "newfid115", "pickcode": "pc115",
                             "size": len(self.uploaded[2])}]
        return {"fid": "newfid115", "pickcode": "pc115", "raw": {}}


class _FakeSA:
    """SA 桩：get_file 报大小，download_range 按段切字节。"""

    def __init__(self, payload=_PAYLOAD):
        self.payload = payload

    def get_file(self, drive_id, fid):
        return {"id": fid, "size": len(self.payload)}

    def download_range(self, drive_id, fid, start, end):
        self.download_calls = getattr(self, "download_calls", 0) + 1
        return self.payload[start:end + 1]


def _cross_job(db, item_id, user_id, provider, dest, status="pending",
               drive_id=None):
    row = em.TransferJob(item_id=item_id, trigger="playback",
                         drive_id=drive_id, provider=provider,
                         owner_scope="user", owner_ref=str(user_id),
                         dest_drive_id=dest, status=status)
    db.add(row)
    db.commit()
    return row


def test_is_cross_job_shapes():
    drive_cross = SimpleNamespace(provider="drive",
                                  dest_drive_id="pan115:user-7")
    pan_cross = SimpleNamespace(provider="pan115",
                                dest_drive_id="userdrive:7")
    same_drive = SimpleNamespace(provider="drive",
                                 dest_drive_id="userdrive:7")
    same_pan = SimpleNamespace(provider="pan115",
                               dest_drive_id="pan115:user-7")
    assert tv._is_cross_job(drive_cross) is True
    assert tv._is_cross_job(pan_cross) is True
    assert tv._is_cross_job(same_drive) is False
    assert tv._is_cross_job(same_pan) is False


def test_gate_cross_drive_to_115_flow(db, monkeypatch):
    _enable(monkeypatch)
    _mount(db, 3, "drive", "drive:shared1")
    item = _item(db, file_path="mount://3/电影/a.mkv", size=len(_PAYLOAD))
    _binding(db, 7, provider="pan115")  # 只绑 115，条目是 Drive
    out = tv.ensure_user_copy_gate(db, item, _user(7))
    assert out["verdict"] == "preparing" and out["reason"] == "job_created"
    job = db.query(em.TransferJob).filter(
        em.TransferJob.id == out["job_id"]).first()
    assert job.provider == "drive"
    assert job.dest_drive_id == "pan115:user-7"
    out2 = tv.ensure_user_copy_gate(db, item, _user(7))
    assert out2["verdict"] == "preparing"
    assert out2["reason"] == "job_in_flight"
    fake115 = _FakeUser115()
    monkeypatch.setattr(tv, "_make_pan115_client", lambda cookie: fake115)
    monkeypatch.setattr("backend.emby_server.user_bindings.binding_115_cookie",
                        lambda db_, uid, bid: "cookie-115")
    res = tv.run_transfer_job_once(db, job, _FakeSA())
    assert res["outcome"] == "done"
    assert fake115.uploaded[2] == _PAYLOAD  # 字节真从读取器流过
    row = db.query(em.TransferVariant).filter(
        em.TransferVariant.item_id == item.id).first()
    assert row.status == "ready"
    assert row.variant_drive_id == "pan115:user-7"
    assert row.pickcode == "pc115"
    out3 = tv.ensure_user_copy_gate(db, item, _user(7))
    assert out3["verdict"] == "serve_115_copy"
    assert out3["variant"].id == row.id


def test_gate_cross_115_to_drive_flow(db, monkeypatch):
    _enable(monkeypatch)
    item = _item(db, file_path="115:/电影/a.mkv", size=len(_PAYLOAD),
                 drive_file_id="srcfid")
    _binding(db, 7, provider="drive")  # 只绑 Drive，条目是 115
    out = tv.ensure_user_copy_gate(db, item, _user(7))
    assert out["verdict"] == "preparing" and out["reason"] == "job_created"
    job = db.query(em.TransferJob).filter(
        em.TransferJob.id == out["job_id"]).first()
    assert job.provider == "pan115"
    assert job.dest_drive_id == "userdrive:7"

    class _FakeServer115:
        def __init__(self):
            self.cookie = "server-cookie"
            self.ua = "ua-test"

        def list_dir(self, cid="0", limit=200):
            if str(cid) == "0":
                return [{"name": "电影", "is_dir": True, "cid": "11"}]
            return [{"name": "a.mkv", "is_dir": False, "pickcode": "srccode",
                     "size": len(_PAYLOAD), "fid": "srcfid"}]

        def download_url(self, pickcode):
            assert pickcode == "srccode"
            return "https://dl.example/file"

    monkeypatch.setattr("backend.emby_server.transfer115.resolve_cookie",
                        lambda db_, library=None: ("server-cookie", "test"))
    monkeypatch.setattr(tv, "_make_pan115_client",
                        lambda cookie: _FakeServer115())

    def _fake_request(method, url, headers=None, timeout=None, **kw):
        assert method == "GET"
        rng = headers["Range"]
        start, end = (int(x) for x in rng[len("bytes="):].split("-"))
        return SimpleNamespace(status_code=206,
                               content=_PAYLOAD[start:end + 1])

    monkeypatch.setattr(tv.httpx, "request", _fake_request)
    uploaded = {}

    def _fake_upload(self, drive_id, name, folder_id, size, reader):
        uploaded["bytes"] = reader.read()
        uploaded["name"] = name
        return "newdrivefid"

    monkeypatch.setattr(tv.UserDriveApiClient, "ensure_folder",
                        lambda self, d, n: "ufolder")
    monkeypatch.setattr(tv.UserDriveApiClient, "resumable_upload",
                        _fake_upload)
    monkeypatch.setattr(tv.UserDriveApiClient, "add_anyone_reader",
                        lambda self, d, f: None)
    monkeypatch.setattr(tv.UserDriveApiClient, "list_file_ids_by_name",
                        lambda self, d, f, n: ["newdrivefid"])
    res = tv.run_transfer_job_once(db, job, _FakeSA())
    assert res["outcome"] == "done"
    assert uploaded["bytes"] == _PAYLOAD
    row = db.query(em.TransferVariant).filter(
        em.TransferVariant.item_id == item.id).first()
    assert row.status == "ready"
    assert row.variant_drive_id == "userdrive:7"
    out2 = tv.ensure_user_copy_gate(db, item, _user(7))
    assert out2["verdict"] == "proceed"
    assert out2["reason"] == "has_ready_variant"


def test_gate_no_binding_still_proceeds(db, monkeypatch):
    _enable(monkeypatch)
    _mount(db, 3, "drive", "drive:shared1")
    item = _item(db, file_path="mount://3/电影/a.mkv")
    out = tv.ensure_user_copy_gate(db, item, _user(7))
    assert out == {"verdict": "proceed", "reason": "no_matching_binding"}


def test_gate_cross_ready_scheme_filter(db, monkeypatch):
    _enable(monkeypatch)
    item = _item(db, file_path="115:/电影/a.mkv", drive_file_id="srcfid")
    _binding(db, 7, provider="drive")  # 只绑 Drive，条目是 115
    # 错盘的 ready 行（pan115 方案）不能被 Drive 侧门禁认领
    _variant(db, item.id, fid="wrongfid", scope="user", ref="7")
    db.query(em.TransferVariant).filter(
        em.TransferVariant.item_id == item.id).update(
        {"variant_drive_id": "pan115:user-7"})
    db.commit()
    out = tv.ensure_user_copy_gate(db, item, _user(7))
    assert out["verdict"] == "preparing" and out["reason"] == "job_created"
    # 对盘的 ready 行才认领
    _variant(db, item.id, fid="rightfid", scope="user", ref="7")
    db.query(em.TransferVariant).filter(
        em.TransferVariant.variant_file_id == "rightfid").update(
        {"variant_drive_id": "userdrive:7"})
    db.commit()
    out2 = tv.ensure_user_copy_gate(db, item, _user(7))
    assert out2["verdict"] == "proceed"
    assert out2["reason"] == "has_ready_variant"


def test_segment_reader_window_refill(monkeypatch):
    monkeypatch.setattr(tv, "_CROSS_SEGMENT", 4)
    data = b"0123456789"
    reader = tv._SegmentReader(len(data), lambda s, e: data[s:e + 1])
    assert reader.read(3) == b"012"
    assert reader.tell() == 3
    assert reader.read(5) == b"34567"  # 跨窗口
    reader.seek(0)
    assert reader.read() == data
    reader.seek(-2, 2)
    assert reader.read() == b"89"
    with pytest.raises(ValueError):
        reader.seek(len(data) + 1)


def test_cross_drive_to_115_over_limit_unsupported(db, monkeypatch):
    _enable(monkeypatch)
    _mount(db, 3, "drive", "drive:shared1")
    item = _item(db, file_path="mount://3/电影/a.mkv", size=1)
    _binding(db, 7, provider="pan115")
    job = _cross_job(db, item.id, 7, "drive", "pan115:user-7",
                     drive_id="shared1")
    monkeypatch.setattr("backend.emby_server.user_bindings.binding_115_cookie",
                        lambda db_, uid, bid: "cookie-115")

    class _BigSA(_FakeSA):
        def get_file(self, drive_id, fid):
            return {"id": fid, "size": tv._115_SIMPLE_UPLOAD_LIMIT + 1}

    res = tv.run_transfer_job_once(db, job, _BigSA())
    assert res["outcome"] == "failed"
    assert res["error_kind"] == "unsupported"


def test_cross_drive_to_115_unresolved(db, monkeypatch):
    _enable(monkeypatch)
    _mount(db, 3, "drive", "drive:shared1")
    item = _item(db, file_path="mount://3/电影/a.mkv", size=len(_PAYLOAD))
    _binding(db, 7, provider="pan115")
    job = _cross_job(db, item.id, 7, "drive", "pan115:user-7",
                     drive_id="shared1")

    class _EmptyDir115(_FakeUser115):
        def simple_upload(self, fileobj, filename, pid):
            fileobj.read()  # 字节流过，但目录里不出现新文件
            return {"fid": "ghostfid", "pickcode": "", "raw": {}}

    monkeypatch.setattr(tv, "_make_pan115_client",
                        lambda cookie: _EmptyDir115())
    monkeypatch.setattr("backend.emby_server.user_bindings.binding_115_cookie",
                        lambda db_, uid, bid: "cookie-115")
    res = tv.run_transfer_job_once(db, job, _FakeSA())
    assert res["outcome"] == "failed"
    assert res["error_kind"] == "copy_unresolved"


def test_cross_115_to_drive_source_gone(db, monkeypatch):
    _enable(monkeypatch)
    item = _item(db, file_path="115:/电影/a.mkv", drive_file_id="srcfid")
    _binding(db, 7, provider="drive")
    job = _cross_job(db, item.id, 7, "pan115", "userdrive:7")

    class _EmptyServer115:
        def list_dir(self, cid="0", limit=200):
            return []

    monkeypatch.setattr("backend.emby_server.transfer115.resolve_cookie",
                        lambda db_, library=None: ("server-cookie", "test"))
    monkeypatch.setattr(tv, "_make_pan115_client",
                        lambda cookie: _EmptyServer115())
    res = tv.run_transfer_job_once(db, job, _FakeSA())
    assert res["outcome"] == "failed"
    assert res["error_kind"] == "dead"


def test_resumable_upload_308_resume(monkeypatch):
    monkeypatch.setattr(tv, "_CROSS_SEGMENT", 8)
    data = b"0123456789ABCDEFGHIJ"  # 20 字节 → 3 段
    import io as _io
    reader = _io.BytesIO(data)
    calls = []

    def _fake_request(method, url, params=None, json=None, headers=None,
                      content=None, timeout=None, **kw):
        if method == "POST":
            return SimpleNamespace(status_code=200,
                                   headers={"location": "https://sess/x"},
                                   content=b"")
        calls.append((headers.get("Content-Range"), bytes(content or b"")))
        if len(calls) < 3:
            ranged = f"bytes=0-{len(calls) * 8 - 1}"
            return SimpleNamespace(status_code=308,
                                   headers={"range": ranged}, content=b"")
        return SimpleNamespace(status_code=200, headers={},
                               content=b'{"id": "upfid"}',
                               json=lambda: {"id": "upfid"})

    monkeypatch.setattr(tv.httpx, "request", _fake_request)
    monkeypatch.setattr(tv.UserDriveApiClient, "_headers",
                        lambda self, d, force_refresh=False:
                        {"Authorization": "Bearer t"})
    client = tv.UserDriveApiClient.__new__(tv.UserDriveApiClient)
    fid = client.resumable_upload("myDrive:7", "a.mkv", "folder", len(data),
                                  reader)
    assert fid == "upfid"
    assert calls[0][0] == "bytes 0-7/20"
    assert calls[1][0] == "bytes 8-15/20"
    assert calls[2][0] == "bytes 16-19/20"
    assert b"".join(c[1] for c in calls) == data


# ---------- 方案 2：绑定用户不静默播共享原件 / Quota 不换号 ----------

def _clear_quota_flags(monkeypatch):
    monkeypatch.delenv("TRANSFER_VARIANT_AUTO_ENABLED", raising=False)
    monkeypatch.delenv("TRANSFER_VARIANT_QUOTA_AUTO_ENQUEUE", raising=False)


def test_quota_auto_enqueue_defaults_off(monkeypatch):
    _clear_quota_flags(monkeypatch)
    assert tv.quota_auto_enqueue_enabled() is False
    assert tv.auto_enabled() is False


def test_quota_auto_enqueue_either_key(monkeypatch):
    _clear_quota_flags(monkeypatch)
    monkeypatch.setenv("TRANSFER_VARIANT_QUOTA_AUTO_ENQUEUE", "1")
    assert tv.quota_auto_enqueue_enabled() is True
    _clear_quota_flags(monkeypatch)
    monkeypatch.setenv("TRANSFER_VARIANT_AUTO_ENABLED", "1")
    assert tv.quota_auto_enqueue_enabled() is True


def test_gate_bound_without_ready_never_proceeds(db, monkeypatch, caplog):
    """有绑定、没有本人 ready 副本：不能 proceed 共享原件，并入队。"""
    _enable(monkeypatch)
    _mount(db, 3, "drive", "drive:drive1")
    item = _item(db)
    _binding(db, 7)
    with caplog.at_level("INFO", logger="backend.emby_server.transfer_variant"):
        gate = tv.ensure_user_copy_gate(db, item, _user())
    assert gate["verdict"] == "preparing"
    assert gate["reason"] == "job_created"
    assert gate["verdict"] != "proceed"
    assert "byos_gate_block" in caplog.text
    assert "byos_enqueue" in caplog.text


def test_gate_bound_no_drive_file_failed(db, monkeypatch):
    _enable(monkeypatch)
    _mount(db, 3, "drive", "drive:drive1")
    item = _item(db, drive_file_id="")
    _binding(db, 7)
    gate = tv.ensure_user_copy_gate(db, item, _user())
    assert gate == {"verdict": "failed", "reason": "no_drive_file"}
    assert db.query(em.TransferJob).count() == 0


def test_gate_cross_bound_no_drive_file_failed(db, monkeypatch):
    _enable(monkeypatch)
    _mount(db, 3, "drive", "drive:drive1")
    item = _item(db, drive_file_id="")
    _binding(db, 7, provider="pan115")
    gate = tv.ensure_user_copy_gate(db, item, _user())
    assert gate["verdict"] == "failed"
    assert gate["reason"] == "no_drive_file"
    assert db.query(em.TransferJob).count() == 0


def test_gate_bound_missing_item_id_failed(db, monkeypatch):
    _enable(monkeypatch)
    _mount(db, 3, "drive", "drive:drive1")
    _binding(db, 7)
    item = SimpleNamespace(id=None, drive_file_id="origfid",
                           file_path="mount://3/电影/a.mkv")
    gate = tv.ensure_user_copy_gate(db, item, _user())
    assert gate == {"verdict": "failed", "reason": "no_item_id"}
    assert db.query(em.TransferJob).count() == 0


def test_gate_ready_logs_ready_play(db, monkeypatch, caplog):
    _enable(monkeypatch)
    _mount(db, 3, "drive", "drive:drive1")
    item = _item(db)
    _binding(db, 7)
    _variant(db, item.id, fid="usercopy", scope="user", ref="7")
    with caplog.at_level("INFO", logger="backend.emby_server.transfer_variant"):
        gate = tv.ensure_user_copy_gate(db, item, _user())
    assert gate["reason"] == "has_ready_variant"
    assert "byos_ready_play" in caplog.text


def test_prefer_user_variant_replaces_shared_original():
    shared = SimpleNamespace(owner_scope="source", variant_file_id="origfid")
    copy = SimpleNamespace(owner_scope="user", variant_file_id="usercopy")
    gate = {"verdict": "proceed", "reason": "has_ready_variant",
            "variant": copy}
    fid, row = tv.prefer_user_variant("origfid", shared, gate)
    assert fid == "usercopy" and row is copy
    # 明示放行没有用户副本行时，保留调用方已经解析的 fid。
    fid2, row2 = tv.prefer_user_variant(
        "origfid", None, {"verdict": "proceed", "reason": "master_off"})
    assert fid2 == "origfid" and row2 is None


def test_bound_shared_play_blocked_matrix():
    copy = SimpleNamespace(owner_scope="user", variant_file_id="usercopy")
    assert tv.bound_shared_play_blocked(
        {"verdict": "proceed", "reason": "no_drive_file"},
        has_binding=True, play_variant=None) is True
    assert tv.bound_shared_play_blocked(
        {"verdict": "proceed", "reason": "master_off"},
        has_binding=True, play_variant=None) is False
    assert tv.bound_shared_play_blocked(
        {"verdict": "proceed", "reason": "play_sign_consume"},
        has_binding=True, play_variant=None) is False
    assert tv.bound_shared_play_blocked(
        {"verdict": "proceed", "reason": "has_ready_variant"},
        has_binding=True, play_variant=copy) is False
    assert tv.bound_shared_play_blocked(
        {"verdict": "proceed", "reason": "has_ready_variant"},
        has_binding=True, play_variant=None) is True
    assert tv.bound_shared_play_blocked(
        {"verdict": "preparing", "reason": "job_created"},
        has_binding=True, play_variant=None) is False
    assert tv.bound_shared_play_blocked(
        {"verdict": "proceed", "reason": "no_drive_file"},
        has_binding=False, play_variant=None) is False


def test_quota_bound_auto_off_records_only(db, monkeypatch, caplog):
    _enable(monkeypatch)
    _clear_quota_flags(monkeypatch)
    _mount(db, 3, "drive", "drive:drive1")
    item = _item(db)
    _binding(db, 7)
    with caplog.at_level("INFO", logger="backend.emby_server.transfer_variant"):
        result = tv.note_quota_signal(db, item, _user(), None, "drive1")
    assert result == {"action": "skipped", "reason": "auto_off"}
    assert db.query(em.TransferJob).count() == 0
    assert "byos_quota_force" in caplog.text
    assert "byos_enqueue" not in caplog.text
    decision = tv.quota_playback_decision(result, bound=True)
    assert decision["failover"] is False
    assert decision["status"] == 409
    assert decision["detail"] == tv.QUOTA_AUTO_OFF_DETAIL


def test_quota_new_key_enqueues_once(db, monkeypatch, caplog):
    _enable(monkeypatch)
    _clear_quota_flags(monkeypatch)
    monkeypatch.setenv("TRANSFER_VARIANT_QUOTA_AUTO_ENQUEUE", "1")
    _mount(db, 3, "drive", "drive:drive1")
    item = _item(db)
    _binding(db, 7)
    with caplog.at_level("INFO", logger="backend.emby_server.transfer_variant"):
        first = tv.note_quota_signal(db, item, _user(), None, "drive1")
    assert first["action"] == "queued"
    second = tv.note_quota_signal(db, item, _user(), None, "drive1")
    assert second["action"] == "throttled"
    assert db.query(em.TransferJob).count() == 1
    assert "byos_enqueue" in caplog.text
    assert "byos_quota_force" in caplog.text
    pending = tv.quota_playback_decision(first, bound=True)
    assert pending == {"failover": False, "status": 409,
                       "detail": tv.QUOTA_COPY_PENDING_DETAIL}
    assert tv.quota_playback_decision(
        {"action": "ignored", "reason": "master_off"}, bound=True)["status"] == 503
    assert tv.quota_playback_decision(
        {"action": "skipped", "reason": "auto_off"}, bound=False)["failover"] is True


def test_quota_pan115_bound_user_job_not_source(db, monkeypatch):
    _enable(monkeypatch)
    _clear_quota_flags(monkeypatch)
    monkeypatch.setenv("TRANSFER_VARIANT_QUOTA_AUTO_ENQUEUE", "1")
    _mount(db, 3, "drive", "drive:drive1")
    item = _item(db)
    _binding(db, 7, provider="pan115")
    result = tv.note_quota_signal(db, item, _user(), None, "drive1")
    # 2026-10-30 重开恢复：quota 信号给只绑 115 的用户建跨单
    # （用户副本），绝不退回建服主 source 任务。
    assert result["action"] == "queued"
    job = db.query(em.TransferJob).one()
    assert job.owner_scope == "user"
    assert job.owner_ref == "7"
    assert job.dest_drive_id == "pan115:user-7"
    assert job.provider == "drive"
    assert job.trigger == "auto"


def test_quota_pan115_bound_auto_off_no_source_job(db, monkeypatch):
    _enable(monkeypatch)
    _clear_quota_flags(monkeypatch)
    _mount(db, 3, "drive", "drive:drive1")
    item = _item(db)
    _binding(db, 7, provider="pan115")
    result = tv.note_quota_signal(db, item, _user(), None, "drive1")
    assert result == {"action": "skipped", "reason": "auto_off"}
    assert db.query(em.TransferJob).count() == 0


# ---------- 2026-10-30 跨 provider 重开：开关/存量/跨道上限 ----------

def _set_sysconfig(db, key, value):
    row = (db.query(base_models.SystemConfig)
           .filter(base_models.SystemConfig.key == key).first())
    if row is None:
        db.add(base_models.SystemConfig(key=key, value=value))
    else:
        row.value = value
    db.commit()
    store.invalidate()


def test_gate_provider_mismatch_switch_off(db, monkeypatch):
    # 总开关关 = 完整回 #1487 关停口径：不建单、failed 明示、
    # detail 就是停用说明、且不回落共享（verdict 不是 proceed）。
    _enable(monkeypatch)
    _set_sysconfig(db, "cross_provider_transfer_enabled", "false")
    _mount(db, 3, "drive", "drive:drive1")
    item = _item(db)
    _binding(db, 7, provider="pan115")
    gate = tv.ensure_user_copy_gate(db, item, _user())
    assert gate["verdict"] == "failed"
    assert gate["reason"] == "cross_provider_relay_disabled"
    assert gate["detail"] == tv.CROSS_PROVIDER_DISABLED_DETAIL
    assert db.query(em.TransferJob).count() == 0


def test_cross_worker_switch_off_terminals_without_moving_bytes(
        db, monkeypatch):
    # 开关关时在途跨单被工人领取直接终态（回关停口径），且零搬运：
    # SA 下载/115 上传桩必须零调用。
    _enable(monkeypatch)
    _set_sysconfig(db, "cross_provider_transfer_enabled", "false")
    _mount(db, 3, "drive", "drive:shared1")
    item = _item(db, file_path="mount://3/电影/a.mkv", size=len(_PAYLOAD))
    _binding(db, 7, provider="pan115")
    job = _cross_job(db, item.id, 7, "drive", "pan115:user-7",
                     drive_id="shared1")
    fake115 = _FakeUser115()
    monkeypatch.setattr(tv, "_make_pan115_client", lambda cookie: fake115)
    monkeypatch.setattr(
        "backend.emby_server.user_bindings.binding_115_cookie",
        lambda db_, uid, bid: "cookie-115")
    sa = _FakeSA()
    res = tv.run_transfer_job_once(db, job, sa)
    assert res["outcome"] == "failed"
    assert res["error_kind"] == "cross_provider_relay_disabled"
    assert job.status == "failed"
    assert fake115.uploaded is None
    assert getattr(sa, "download_calls", 0) == 0
    assert db.query(em.TransferVariant).count() == 0


def test_gate_legacy_disabled_cross_row_retriggers(db, monkeypatch):
    # 关停期遗留行：failed/cross_provider_relay_disabled 不是真
    # 失败，是路线关停的产物。重开后不自动复活（无点播时行数
    # 不变），用户再点播走到门禁才在预算内重触发建新跨单。
    _enable(monkeypatch)
    _mount(db, 3, "drive", "drive:drive1")
    item = _item(db)
    _binding(db, 7, provider="pan115")
    job = _cross_job(db, item.id, 7, "drive", "pan115:user-7",
                     status="failed", drive_id="drive1")
    job.error_kind = "cross_provider_relay_disabled"
    job.error_detail = tv.CROSS_PROVIDER_DISABLED_DETAIL
    db.commit()
    assert db.query(em.TransferJob).count() == 1  # 未点播：不复活
    gate = tv.ensure_user_copy_gate(db, item, _user())
    assert gate["verdict"] == "preparing"
    assert gate["reason"] == "job_retriggered"
    assert "detail" in gate and "115 网盘" in gate["detail"]
    assert db.query(em.TransferJob).count() == 2
    new_job = db.query(em.TransferJob).order_by(
        em.TransferJob.id.desc()).first()
    assert new_job.status == "pending"
    assert tv._is_cross_job(new_job) is True


def test_gate_legacy_disabled_cross_row_switch_off(db, monkeypatch):
    # 同一张遗留行，总开关关时维持关停口径：failed 明示、不建单。
    _enable(monkeypatch)
    _set_sysconfig(db, "cross_provider_transfer_enabled", "false")
    _mount(db, 3, "drive", "drive:drive1")
    item = _item(db)
    _binding(db, 7, provider="pan115")
    job = _cross_job(db, item.id, 7, "drive", "pan115:user-7",
                     status="failed", drive_id="drive1")
    job.error_kind = "cross_provider_relay_disabled"
    job.error_detail = tv.CROSS_PROVIDER_DISABLED_DETAIL
    db.commit()
    gate = tv.ensure_user_copy_gate(db, item, _user())
    assert gate["verdict"] == "failed"
    assert gate["reason"] == "cross_provider_relay_disabled"
    assert gate["detail"] == tv.CROSS_PROVIDER_DISABLED_DETAIL
    assert db.query(em.TransferJob).count() == 1


def test_gate_cross_terminal_failed_carries_reason_detail(db, monkeypatch):
    # 跨单真终态失败：409 文案底料（gate detail）必须带任务行上
    # 的人话原因 + 目标盘名，不许只剩笼统"转存失败"。
    _enable(monkeypatch)
    _mount(db, 3, "drive", "drive:drive1")
    item = _item(db)
    _binding(db, 7, provider="pan115")
    job = _cross_job(db, item.id, 7, "drive", "pan115:user-7",
                     status="failed", drive_id="drive1")
    job.error_kind = "file_only_copy_unavailable"
    job.error_detail = "115 源文件已不存在"
    db.commit()
    gate = tv.ensure_user_copy_gate(db, item, _user())
    assert gate["verdict"] == "failed"
    assert gate["reason"] == "file_only_copy_unavailable"
    assert "115 源文件已不存在" in gate["detail"]
    assert "115 网盘" in gate["detail"]


def test_claim_cross_lane_cap_keeps_same_provider_flowing(db):
    # 硬要求回归：跨单单独限在途（cap=1）——2 张跨单只认领 1 张，
    # 同 provider 单照常被认领；跨单在途达 cap 后再认领只出同
    # provider，慢单不许堵住整轮。
    _set_sysconfig(db, "transfer_cross_lane_concurrency", "1")
    item = _item(db)
    c1 = _cross_job(db, item.id, 7, "drive", "pan115:user-7",
                    drive_id="shared1")
    c2 = _cross_job(db, item.id, 8, "drive", "pan115:user-8",
                    drive_id="shared1")
    s1 = _user_job(db, item.id, 9)
    claimed = tv.claim_due_jobs(db, limit=10)
    assert s1.id in claimed
    assert len([j for j in (c1.id, c2.id) if j in claimed]) == 1
    s2 = _user_job(db, item.id, 10)
    claimed2 = tv.claim_due_jobs(db, limit=10)
    assert claimed2 == [s2.id]


def test_cross_config_defaults_clamps_and_write(db):
    # 默认开、跨道默认 2；SystemConfig 覆盖后读取侧钳制 [1, 8]；
    # 管理端写口（write_transfer_config）走 transfer 家族校验，
    # 越界值整批拒绝（ValueError），合法值写完即生效。
    assert tv.cross_provider_enabled(db) is True
    assert tv.cross_lane_concurrency(db) == 2
    _set_sysconfig(db, "transfer_cross_lane_concurrency", "99")
    assert tv.cross_lane_concurrency(db) == 8
    _set_sysconfig(db, "transfer_cross_lane_concurrency", "0")
    # _system_config_int 同口径：<=0 视为未配，回默认（管理端
    # schema 下限 1，0 本来也写不进来）
    assert tv.cross_lane_concurrency(db) == 2
    payload = tv.write_transfer_config(
        db, cross_provider_transfer_enabled=False)
    assert payload["cross_provider_transfer_enabled"] is False
    assert tv.cross_provider_enabled(db) is False
    with pytest.raises(ValueError):
        tv.write_transfer_config(db, transfer_cross_lane_concurrency=99)
