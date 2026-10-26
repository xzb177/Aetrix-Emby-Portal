# -*- coding: utf-8 -*-
"""115 线转存（2026-10-26 用户亲定：与谷歌盘同一口径，同 provider 内转存）。

全部离线：115 API 一律用桩替身；凭据口用桩 Cookie（binding_115_cookie
整函数 monkeypatch，不碰真实密封）。所有验证均为 mock：真实 115
copy/mkdir 请求形态、copy 响应新 fid 字段、秒传语义均未实测（生产
pan115 绑定现为 0 条，等用户给 115 测试账号），见 PR 描述的未验证项，
本文件不编造闭环。
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


def _item115(db, **kw):
    _counter[0] += 1
    row = em.MediaItem(guid=f"tv115-{_counter[0]}", library_id=1,
                       item_type="movie", name="测试片",
                       drive_file_id="srcfid",
                       file_path="mount://9/电影/a.mkv", size=1000)
    for key, value in kw.items():
        setattr(row, key, value)
    db.add(row)
    db.commit()
    return row


def _mount115(db):
    row = em.StorageMount(id=9, name="m9", mount_type="115",
                          path="115:/")
    db.add(row)
    db.commit()
    return row


def _binding115(db, user_id, status="active"):
    cred = em.MountCredential(scope="user", owner_user_id=user_id,
                              status="active")
    db.add(cred)
    db.commit()
    row = em.UserStorageBinding(user_id=user_id, provider="pan115",
                                account_label="test115",
                                credential_id=cred.id,
                                status=status)
    db.add(row)
    db.commit()
    return row


def _user(user_id=7):
    return SimpleNamespace(id=user_id)


def _enable(monkeypatch):
    monkeypatch.setenv("TRANSFER_VARIANT_ENABLED", "1")


class _FakePan115:
    """Pan115Client 桩：目录树 + copy/download 行为全程可观测。"""

    def __init__(self, copy_result="ok", download_behavior="ok"):
        # root("0") 下已有转存目录（cid "d1"），内空；copy 后落文件。
        self.dirs = {"0": [{"cid": "d1", "name": "aetrix-transfer",
                            "is_dir": True}],
                     "d1": []}
        self.copy_result = copy_result
        self.download_behavior = download_behavior
        self.copy_calls = []
        self.mkdir_calls = []
        self.download_calls = []
        self._next_cid = 100

    def list_dir(self, cid="0", limit=200):
        return [dict(e) for e in self.dirs.get(cid or "0", [])]

    def mkdir(self, parent_cid, name):
        self.mkdir_calls.append((parent_cid, name))
        self._next_cid += 1
        cid = f"d{self._next_cid}"
        self.dirs.setdefault(parent_cid or "0", []).append(
            {"cid": cid, "name": name, "is_dir": True})
        self.dirs[cid] = []
        return {"cid": cid}

    def copy_file(self, source_fid, dest_cid):
        self.copy_calls.append((source_fid, dest_cid))
        if self.copy_result == "auth":
            raise transfer115.Pan115AuthError("Cookie 已失效")
        if self.copy_result == "error":
            raise transfer115.Pan115Error("115 业务错误")
        if self.copy_result == "ghost":
            # 回执有 fid，但目标目录里没产生文件：目录核对必须拦下。
            return {"fid": "newfid", "pickcode": "PC-NEW"}
        if self.copy_result == "dir_wins":
            # 回执 fid 与目录里核到的 id 不一致：以目录为准。
            self.dirs.setdefault(dest_cid, []).append(
                {"fid": "dirfid", "cid": "c-dir", "name": "a.mkv",
                 "is_dir": False, "size": 1000, "pickcode": "PC-DIR"})
            return {"fid": "newfid", "pickcode": "PC-NEW"}
        # 真实 copy 会在目标目录产生新文件（fid 不同、内容同）。
        self.dirs.setdefault(dest_cid, []).append(
            {"fid": "newfid", "cid": "c-new", "name": "a.mkv",
             "is_dir": False, "size": 1000, "pickcode": "PC-NEW"})
        if self.copy_result == "no_fid":
            return {}  # 响应无 fid：调用方走列目录匹配回落。
        return {"fid": "newfid", "pickcode": "PC-NEW"}

    def download_url(self, pickcode):
        self.download_calls.append(pickcode)
        if self.download_behavior == "auth":
            raise transfer115.Pan115AuthError("Cookie 已失效")
        if self.download_behavior == "gone":
            raise transfer115.Pan115Error("文件不存在")
        return f"https://example.com/dl/{pickcode}"


def _patch_115(monkeypatch, fake):
    monkeypatch.setattr(tv, "_make_pan115_client", lambda cookie: fake)
    monkeypatch.setattr(ub, "binding_115_cookie",
                        lambda db_arg, user_id, binding_id: "COOKIE")


def _job115(db, item_id, user_id=7, status="pending"):
    row = em.TransferJob(item_id=item_id, trigger="playback",
                         drive_id="pan115:mount-9", provider="pan115",
                         owner_scope="user", owner_ref=str(user_id),
                         dest_drive_id=f"pan115:user-{user_id}",
                         status=status)
    db.add(row)
    db.commit()
    return row


# ---------- 客户端请求形态 ----------

def test_115_copy_request_shape(monkeypatch):
    seen = {}

    def _recorder(method, url, *, params=None, data=None, cookie=None,
                  timeout=None, ua=""):
        seen.update(method=method, url=url, data=data)
        return {"state": True, "data": {}}

    monkeypatch.setattr(transfer115, "_http_json", _recorder)
    client = transfer115.Pan115Client("COOKIE")
    client.copy_file("srcfid", "d1")
    assert seen["method"] == "POST"
    assert seen["url"].endswith("/files/copy")
    assert seen["data"] == {"fid": "srcfid", "pid": "d1"}


def test_115_mkdir_request_shape(monkeypatch):
    seen = {}

    def _recorder(method, url, *, params=None, data=None, cookie=None,
                  timeout=None, ua=""):
        seen.update(method=method, url=url, data=data)
        return {"state": True, "data": {}}

    monkeypatch.setattr(transfer115, "_http_json", _recorder)
    client = transfer115.Pan115Client("COOKIE")
    client.mkdir("0", "aetrix-transfer")
    assert seen["method"] == "POST"
    assert seen["url"].endswith("/files/add")
    assert seen["data"] == {"pid": "0", "cname": "aetrix-transfer"}


def test_115_list_dir_sha1_passthrough(monkeypatch):
    def _recorder(method, url, *, params=None, data=None, cookie=None,
                  timeout=None, ua=""):
        return {"state": True, "data": [
            {"fid": "f1", "cid": "c1", "n": "a.mkv", "s": 100,
             "pc": "PC1", "sha1": "abc123", "fc": "1"},
            {"fid": "f2", "cid": "c2", "n": "b.mkv", "s": 200,
             "pc": "PC2", "fc": "1"},
        ]}

    monkeypatch.setattr(transfer115, "_http_json", _recorder)
    client = transfer115.Pan115Client("COOKIE")
    entries = client.list_dir("0")
    assert entries[0]["sha1"] == "abc123"  # 响应里有才取
    assert entries[1]["sha1"] == ""  # 缺时为空，绝不冒充


# ---------- 工人 ----------

def test_115_worker_success_fid_pickcode(db, monkeypatch):
    _mount115(db)
    item = _item115(db)
    _binding115(db, 7)
    job = _job115(db, item.id)
    fake = _FakePan115()
    _patch_115(monkeypatch, fake)
    result = tv.run_transfer_job_once(db, job, object())
    assert result["outcome"] == "done" and job.status == "done"
    assert fake.copy_calls == [("srcfid", "d1")]
    row = db.query(em.TransferVariant).one()
    assert row.status == "ready" and row.variant_file_id == "newfid"
    assert row.pickcode == "PC-NEW"
    assert row.variant_drive_id == "pan115:user-7"
    assert row.owner_scope == "user" and row.owner_ref == "7"


def test_115_worker_copy_response_no_fid_dir_match(db, monkeypatch):
    _mount115(db)
    item = _item115(db)
    _binding115(db, 7)
    job = _job115(db, item.id)
    fake = _FakePan115(copy_result="no_fid")
    _patch_115(monkeypatch, fake)
    result = tv.run_transfer_job_once(db, job, object())
    assert result["outcome"] == "done"
    row = db.query(em.TransferVariant).one()
    assert row.variant_file_id == "newfid" and row.pickcode == "PC-NEW"


# ---------- 转存后目录核对（FMBY discover_transfer_target 口径） ----------

def test_115_worker_copy_receipt_unverified_copy_unresolved(db, monkeypatch):
    _mount115(db)
    item = _item115(db)
    _binding115(db, 7)
    job = _job115(db, item.id)
    fake = _FakePan115(copy_result="ghost")
    _patch_115(monkeypatch, fake)
    result = tv.run_transfer_job_once(db, job, object())
    assert result["outcome"] == "failed"
    assert result["error_kind"] == "copy_unresolved"
    assert db.query(em.TransferVariant).count() == 0  # 核不到不登记


def test_115_worker_dir_id_wins_over_receipt(db, monkeypatch):
    _mount115(db)
    item = _item115(db)
    _binding115(db, 7)
    job = _job115(db, item.id)
    fake = _FakePan115(copy_result="dir_wins")
    _patch_115(monkeypatch, fake)
    result = tv.run_transfer_job_once(db, job, object())
    assert result["outcome"] == "done"
    row = db.query(em.TransferVariant).one()
    assert row.variant_file_id == "dirfid"  # 目录核到的 id 为准
    assert row.pickcode == "PC-DIR"


def test_115_worker_auth_error_no_retry_marks_binding(db, monkeypatch):
    _mount115(db)
    item = _item115(db)
    binding = _binding115(db, 7)
    job = _job115(db, item.id)
    fake = _FakePan115(copy_result="auth")
    _patch_115(monkeypatch, fake)
    result = tv.run_transfer_job_once(db, job, object())
    assert result["outcome"] == "failed"
    assert result["error_kind"] == "auth_error"
    db.refresh(binding)
    assert binding.status != "active"
    assert binding.last_error_kind == "expired"


def test_115_worker_no_binding(db, monkeypatch):
    _mount115(db)
    item = _item115(db)
    job = _job115(db, item.id)
    fake = _FakePan115()
    _patch_115(monkeypatch, fake)
    result = tv.run_transfer_job_once(db, job, object())
    assert result["outcome"] == "failed"
    assert result["error_kind"] == "no_binding"


def test_115_worker_no_source_fid(db, monkeypatch):
    # 工人分发器先拦无 fid 条目（item_gone）；门禁层的 no_source_fid
    # 语义由 test_115_gate_115_path_no_fid_failed 覆盖（不建任务）。
    _mount115(db)
    item = _item115(db, drive_file_id="")
    _binding115(db, 7)
    job = _job115(db, item.id)
    fake = _FakePan115()
    _patch_115(monkeypatch, fake)
    result = tv.run_transfer_job_once(db, job, object())
    assert result["outcome"] == "failed"
    assert result["error_kind"] == "item_gone"
    assert fake.copy_calls == []


def test_115_worker_source_scope_unsupported(db, monkeypatch):
    _mount115(db)
    item = _item115(db)
    row = em.TransferJob(item_id=item.id, trigger="manual",
                         drive_id="pan115:mount-9", provider="pan115",
                         owner_scope="source", status="pending")
    db.add(row)
    db.commit()
    fake = _FakePan115()
    _patch_115(monkeypatch, fake)
    result = tv.run_transfer_job_once(db, row, object())
    assert result["outcome"] == "failed"
    assert result["error_kind"] == "unsupported"
    assert fake.copy_calls == []


# ---------- 门禁 ----------

def test_115_gate_115_path_no_fid_failed(db, monkeypatch):
    _enable(monkeypatch)
    _mount115(db)
    item = _item115(db, drive_file_id="")
    _binding115(db, 7)
    gate = tv.ensure_user_copy_gate(db, item, _user())
    assert gate == {"verdict": "failed", "reason": "no_source_fid"}
    assert db.query(em.TransferJob).count() == 0


def test_115_gate_creates_job_preparing(db, monkeypatch):
    _enable(monkeypatch)
    _mount115(db)
    item = _item115(db)
    _binding115(db, 7)
    gate = tv.ensure_user_copy_gate(db, item, _user())
    assert gate["verdict"] == "preparing" and gate["reason"] == "job_created"
    job = db.query(em.TransferJob).one()
    assert job.provider == "pan115" and job.owner_scope == "user"
    assert job.dest_drive_id == "pan115:user-7"


def test_115_gate_ready_serve_copy(db, monkeypatch):
    _enable(monkeypatch)
    _mount115(db)
    item = _item115(db)
    _binding115(db, 7)
    row = em.TransferVariant(item_id=item.id, source_file_id="srcfid",
                             variant_file_id="newfid",
                             variant_drive_id="pan115:user-7",
                             owner_scope="user", owner_ref="7",
                             status="ready", pickcode="PC-NEW")
    db.add(row)
    db.commit()
    gate = tv.ensure_user_copy_gate(db, item, _user())
    assert gate["verdict"] == "serve_115_copy"
    assert gate["variant"].pickcode == "PC-NEW"


def test_115_gate_no_duplicate_job(db, monkeypatch):
    _enable(monkeypatch)
    _mount115(db)
    item = _item115(db)
    _binding115(db, 7)
    _job115(db, item.id)
    gate = tv.ensure_user_copy_gate(db, item, _user())
    assert gate["verdict"] == "preparing" and gate["reason"] == "job_in_flight"
    assert db.query(em.TransferJob).count() == 1


# ---------- 巡检 ----------

def _ready_115_row(db, item_id, pickcode="PC-NEW", fid="newfid"):
    row = em.TransferVariant(item_id=item_id, source_file_id="srcfid",
                             variant_file_id=fid,
                             variant_drive_id="pan115:user-7",
                             owner_scope="user", owner_ref="7",
                             status="ready", pickcode=pickcode)
    db.add(row)
    db.commit()
    return row


def test_115_patrol_download_url_ok_clears_strike(db, monkeypatch):
    item = _item115(db)
    _binding115(db, 7)
    row = _ready_115_row(db, item.id)
    row.stale_count = 1
    db.commit()
    fake = _FakePan115()
    _patch_115(monkeypatch, fake)
    tv.patrol_once(db, lambda: object())
    db.refresh(row)
    assert row.status == "ready" and row.stale_count == 0
    assert fake.download_calls == ["PC-NEW"]


def test_115_patrol_auth_no_strike(db, monkeypatch):
    item = _item115(db)
    _binding115(db, 7)
    row = _ready_115_row(db, item.id)
    fake = _FakePan115(download_behavior="auth")
    _patch_115(monkeypatch, fake)
    tv.patrol_once(db, lambda: object())
    db.refresh(row)
    assert row.status == "ready" and row.stale_count == 0


def test_115_patrol_gone_strikes_twice(db, monkeypatch):
    item = _item115(db)
    _binding115(db, 7)
    row = _ready_115_row(db, item.id)
    fake = _FakePan115(download_behavior="gone")
    _patch_115(monkeypatch, fake)
    tv.patrol_once(db, lambda: object())
    db.refresh(row)
    assert row.status == "ready" and row.stale_count == 1
    tv.patrol_once(db, lambda: object())
    db.refresh(row)
    assert row.status == "stale" and row.stale_count == 2


def test_115_patrol_pickcode_fallback_fills(db, monkeypatch):
    item = _item115(db)
    _binding115(db, 7)
    row = _ready_115_row(db, item.id, pickcode="")
    fake = _FakePan115()
    # 目标目录里已有该 fid 的文件（带 pickcode），供回落匹配。
    fake.dirs["d1"] = [{"fid": "newfid", "cid": "c-new", "name": "a.mkv",
                        "is_dir": False, "size": 1000,
                        "pickcode": "PC-NEW"}]
    _patch_115(monkeypatch, fake)
    tv.patrol_once(db, lambda: object())
    db.refresh(row)
    assert row.status == "ready" and row.pickcode == "PC-NEW"
    assert fake.download_calls == ["PC-NEW"]


# ---------- 115 简单上传（跨 provider Drive→115 用，115cli 口径） ----------

class _FakeOSSResp:
    def __init__(self, status_code=200, body=None):
        self.status_code = status_code
        self._body = body if body is not None else {}

    def json(self):
        if isinstance(self._body, Exception):
            raise self._body
        return self._body


class _FakeOSSClient:
    """替 httpx.Client：只录表单字段与文件体，返回预设回调。"""

    def __init__(self, resp):
        self.resp = resp
        self.posted = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def post(self, url, data=None, files=None, headers=None):
        name, fileobj = files["file"]
        self.posted = {"url": url, "data": dict(data or {}),
                       "filename": name, "body": fileobj.read()}
        return self.resp


def _client115():
    return transfer115.Pan115Client("cookie-test")


def test_simple_upload_happy(monkeypatch):
    import httpx as _httpx
    seen = {}

    def _fake_call(method, url, params=None, data=None, cookie=None, ua=""):
        seen["init"] = (method, url, data)
        return {"host": "https://bucket.oss-cn-shenzhen.aliyuncs.com",
                "object": "obj-key", "policy": "pol", "accessid": "acc",
                "callback": "cb", "signature": "sig"}

    monkeypatch.setattr(transfer115, "_http_json", _fake_call)
    oss = _FakeOSSClient(_FakeOSSResp(200, {
        "data": {"file_id": "fid9", "file_name": "a.mkv",
                 "pick_code": "pc9", "sha1": "x", "file_size": 5}}))
    monkeypatch.setattr(_httpx, "Client", lambda *a, **k: oss)
    out = _client115().simple_upload(__import__("io").BytesIO(b"hello"),
                                     "a.mkv", "900")
    assert out["fid"] == "fid9" and out["pickcode"] == "pc9"
    assert seen["init"][1].endswith("/3.0/sampleinitupload.php")
    assert seen["init"][2] == {"filename": "a.mkv", "target": "U_1_900"}
    assert oss.posted["url"].startswith("https://bucket.")
    assert oss.posted["data"]["key"] == "obj-key"
    assert oss.posted["data"]["success_action_status"] == "200"
    assert oss.posted["body"] == b"hello"


def test_simple_upload_missing_oss_field(monkeypatch):
    def _fake_call(method, url, params=None, data=None, cookie=None, ua=""):
        return {"host": "https://bucket.example", "object": "",
                "policy": "pol", "accessid": "acc", "callback": "cb",
                "signature": "sig"}

    monkeypatch.setattr(transfer115, "_http_json", _fake_call)
    with pytest.raises(transfer115.Pan115Error):
        _client115().simple_upload(__import__("io").BytesIO(b"x"),
                                   "a.mkv", "900")


def test_simple_upload_oss_401_is_auth(monkeypatch):
    import httpx as _httpx

    def _fake_call(method, url, params=None, data=None, cookie=None, ua=""):
        return {"host": "https://bucket.example", "object": "o",
                "policy": "p", "accessid": "a", "callback": "c",
                "signature": "s"}

    monkeypatch.setattr(transfer115, "_http_json", _fake_call)
    oss = _FakeOSSClient(_FakeOSSResp(401, {}))
    monkeypatch.setattr(_httpx, "Client", lambda *a, **k: oss)
    with pytest.raises(transfer115.Pan115AuthError):
        _client115().simple_upload(__import__("io").BytesIO(b"x"),
                                   "a.mkv", "900")
