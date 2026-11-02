# -*- coding: utf-8 -*-
"""夸克二期：转存 + 点播中继（客户端扩链 / 凭据口 / 门禁 / 执行器 / 配置）。

钉死的范围：
- QuarkClient 扩链（list_dir 分页归一、download_url、mkdir、
  upload 秒传命中零字节、未命中分片 auth+PUT+commit+finish 全链、
  Cookie 轮换吸收、401 定性 Auth、pre 缺上传主机响亮报错）——
  全部桩 _send，不碰真实夸克；API 形态未经活体验证，测试只锁
  本客户端的请求构造与信封处理口径，不冒充上游已验。
- user_bindings：binding_quark_cookie 唯一解封口（本人/quark
  行/归属校验）、resolve_user_quark_copy_url（现取直链 + 轮换
  回写 + 失效落 expired）。
- 首播门禁：只绑夸克的用户走夸克跨 provider 流（preparing /
  serve_quark_copy / 开关关 failed 明示不回落）；counterpart
  （drive/115 互绑）优先于夸克，既有行为不变。
- 执行器 _run_cross_to_quark：秒传命中不搬字节、未命中搬字节
  后目录核对 + 登记 ready、用户 Cookie 失效落绑定 expired、
  源死 dead、核不到 copy_unresolved。
- 配置：quark_play_enabled 进 transfer payload/write/schema。

全部离线：sqlite 内存库 + 桩客户端；monkeypatch 一律 patch
类/模块属性（仓库纪律）；fixture 自带 store 缓存失效。
"""
import os

os.environ.setdefault("DATABASE_TYPE", "sqlite")
os.environ.setdefault("REDIS_ENABLED", "false")

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend import models as base_models
from backend.emby_server import models as em
from backend.emby_server import quarkdrive
from backend.emby_server import transfer_variant as tv
from backend.emby_server import user_bindings as ub
from backend.emby_server.upstream_sources import seal_secret, unseal_secret
from backend.integrations import store


# ---------------------------------------------------------------------------
# DB 夹具与行工厂
# ---------------------------------------------------------------------------

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


@pytest.fixture(autouse=True)
def _enable_transfer(monkeypatch):
    monkeypatch.setenv("TRANSFER_VARIANT_ENABLED", "1")


_counter = [0]


def _item(db, **kw):
    _counter[0] += 1
    row = em.MediaItem(guid=f"qk-{_counter[0]}", library_id=1,
                       item_type="movie", name="测试片",
                       drive_file_id="origfid",
                       file_path="mount://3/电影/a.mkv", size=1000)
    for key, value in kw.items():
        setattr(row, key, value)
    db.add(row)
    db.commit()
    return row


def _mount(db, mount_id, mount_type, path):
    row = em.StorageMount(id=mount_id, name=f"m{mount_id}",
                          mount_type=mount_type, path=path)
    db.add(row)
    db.commit()
    return row


def _binding(db, user_id, provider="quark", status="active",
             cookie="__puus=tok; __pus=x"):
    cred = em.MountCredential(scope="user", owner_user_id=user_id,
                              status="active",
                              sealed_credential=seal_secret(cookie),
                              key_id="fernet-v1")
    db.add(cred)
    db.commit()
    row = em.UserStorageBinding(user_id=user_id, provider=provider,
                                account_label="test",
                                credential_id=cred.id, status=status)
    db.add(row)
    db.commit()
    return row


from types import SimpleNamespace


def _user(user_id=7):
    return SimpleNamespace(id=user_id)


# ---------------------------------------------------------------------------
# QuarkClient 扩链：_send 桩
# ---------------------------------------------------------------------------

class _Headers:
    def __init__(self, items=None):
        self._m = {}
        for key, value in (items or {}).items():
            self._m[key.lower()] = value

    def get(self, name, default=None):
        return self._m.get(name.lower(), default)

    def get_list(self, name):
        value = self._m.get(name.lower())
        if value is None:
            return []
        return value if isinstance(value, list) else [value]


class _Resp:
    def __init__(self, status=200, body=None, headers=None, text=""):
        self.status_code = status
        self._body = body
        self.headers = _Headers(headers)
        self.text = text

    def json(self):
        return self._body


def _ok(data):
    return {"status": 200, "code": 0, "message": "", "data": data}


class _SendStub:
    """quarkdrive._send 桩：按 (method, 路径片段) 路由，记录全部调用。"""

    def __init__(self):
        self.calls = []
        self.routes = {}

    def route(self, method, needle, resp):
        self.routes[(method, needle)] = resp
        return self

    def __call__(self, method, url, **kwargs):
        self.calls.append({"method": method, "url": url, **kwargs})
        for (m, needle), resp in self.routes.items():
            if m == method and needle in url:
                return resp(self.calls[-1]) if callable(resp) else resp
        raise AssertionError(f"未桩的请求：{method} {url}")

    def find(self, method, needle):
        return [c for c in self.calls
                if c["method"] == method and needle in c["url"]]


@pytest.fixture()
def send_stub(monkeypatch):
    stub = _SendStub()
    monkeypatch.setattr(quarkdrive, "_send", stub)
    return stub


class _BytesReader:
    def __init__(self, data):
        self._data = data
        self._pos = 0

    def seek(self, offset, whence=0):
        self._pos = offset if whence == 0 else self._pos + offset
        return self._pos

    def tell(self):
        return self._pos

    def read(self, n=-1):
        if n is None or n < 0:
            n = len(self._data) - self._pos
        chunk = self._data[self._pos:self._pos + n]
        self._pos += len(chunk)
        return chunk


def test_list_dir_paginates_and_normalizes(send_stub):
    page1 = [{"fid": f"f{i}", "file_name": f"n{i}", "size": i,
              "file": True} for i in range(100)]
    page2 = [{"fid": "d1", "file_name": "dir", "size": 0, "file": False}]
    send_stub.route("GET", "/file/sort",
                    lambda call: _Resp(body=_ok({"list": page1}))
                    if call["params"]["_page"] == "1"
                    else _Resp(body=_ok({"list": page2})))
    client = quarkdrive.QuarkClient("cookie")
    entries = client.list_dir("0", page_size=100)
    assert len(entries) == 101
    assert entries[0] == {"fid": "f0", "name": "n0", "size": 0,
                          "is_dir": False}
    assert entries[-1]["is_dir"] is True


def test_download_url_and_auth_classification(send_stub):
    send_stub.route("POST", "/file/download",
                    _Resp(body=_ok([{"download_url": "https://cdn/x",
                                     "file_name": "a.mkv", "size": 5}])))
    client = quarkdrive.QuarkClient("cookie")
    assert client.download_url("fid1") == "https://cdn/x"
    call = send_stub.find("POST", "/file/download")[0]
    assert call["json"] == {"fids": ["fid1"]}

    send_stub.route("POST", "/file/download", _Resp(status=401))
    with pytest.raises(quarkdrive.QuarkAuthError):
        client.download_url("fid1")


def test_download_url_missing_raises(send_stub):
    send_stub.route("POST", "/file/download", _Resp(body=_ok([{}])))
    client = quarkdrive.QuarkClient("cookie")
    with pytest.raises(quarkdrive.QuarkError):
        client.download_url("fid1")


def test_cookie_rotation_absorbed(send_stub):
    send_stub.route("GET", "/file/sort", _Resp(
        body=_ok({"list": []}),
        headers={"set-cookie": ["__puus=newtok; Path=/"]}))
    client = quarkdrive.QuarkClient("__puus=old; __pus=x")
    client.list_dir("0")
    assert "__puus=newtok" in client.cookie
    assert "__pus=x" in client.cookie


def test_mkdir_body_and_conflict_hint(send_stub):
    send_stub.route("POST", "/file", _Resp(body=_ok({})))
    client = quarkdrive.QuarkClient("cookie")
    client.mkdir("0", "aetrix-transfer")
    call = send_stub.find("POST", "/file")[0]
    assert call["json"] == {"pdir_fid": "0", "file_name": "aetrix-transfer",
                            "dir_path": "", "dir_init_lock": False}
    assert quarkdrive.is_name_conflict_error(
        quarkdrive.QuarkError("夸克返回 code=31001：file is doloading[同名冲突]"))
    assert not quarkdrive.is_name_conflict_error(
        quarkdrive.QuarkError("别的失败"))


_PRE = {"task_id": "task1", "upload_id": "up1", "obj_key": "obj/key.mkv",
        "bucket": "bkt", "part_size": 4, "auth_info": "ai",
        "upload_host": "oss.example.com"}


def _wire_upload(send_stub, *, hash_finish=False, pre=None):
    send_stub.route("POST", "/file/upload/pre",
                    _Resp(body=_ok(pre or _PRE)))
    send_stub.route("POST", "/file/update/hash",
                    _Resp(body=_ok({"finish": hash_finish})))
    send_stub.route("POST", "/file/upload/auth",
                    _Resp(body=_ok({"auth_key": "AUTHKEY"})))
    counter = {"n": 0}

    def _put(call):
        counter["n"] += 1
        return _Resp(headers={"ETag": f'"etag{counter["n"]}"'})

    send_stub.route("PUT", "oss.example.com", _put)
    send_stub.route("POST", "oss.example.com", _Resp(text=""))
    send_stub.route("POST", "/file/upload/finish",
                    _Resp(body=_ok({"fid": "newfid"})))


def test_upload_rapid_hit_moves_no_bytes(send_stub):
    _wire_upload(send_stub, hash_finish=True)
    client = quarkdrive.QuarkClient("cookie")
    reader = _BytesReader(b"0123456789")
    out = client.upload_file(reader, "a.mkv", 10, "dirfid",
                             md5="m" * 32, sha1="s" * 40)
    assert out == {"outcome": "rapid", "fid": "newfid"}
    assert send_stub.find("PUT", "oss.example.com") == []
    assert reader.tell() == 0


def test_upload_full_chain(send_stub):
    _wire_upload(send_stub, hash_finish=False)
    client = quarkdrive.QuarkClient("cookie")
    data = b"0123456789"
    out = client.upload_file(_BytesReader(data), "a.mkv", 10, "dirfid",
                             md5="m" * 32, sha1="s" * 40)
    assert out == {"outcome": "uploaded", "fid": "newfid"}
    puts = send_stub.find("PUT", "oss.example.com")
    assert len(puts) == 3  # part_size=4 → 4+4+2
    assert b"".join(c["content"] for c in puts) == data
    assert all(c["headers"]["Authorization"] == "AUTHKEY" for c in puts)
    assert "partNumber=1&uploadId=up1" in puts[0]["url"]
    commit = [c for c in send_stub.find("POST", "oss.example.com")]
    assert len(commit) == 1
    body = commit[0]["content"].decode("utf-8")
    for etag in ("etag1", "etag2", "etag3"):
        assert etag in body
    finish = send_stub.find("POST", "/file/upload/finish")
    assert finish[0]["json"] == {"task_id": "task1",
                                 "obj_key": "obj/key.mkv"}


def test_upload_without_hashes_skips_rapid(send_stub):
    _wire_upload(send_stub, hash_finish=True)
    client = quarkdrive.QuarkClient("cookie")
    out = client.upload_file(_BytesReader(b"ab"), "a.mkv", 2, "dirfid")
    assert out["outcome"] == "uploaded"
    assert send_stub.find("POST", "/file/update/hash") == []


def test_upload_pre_nested_metadata_part_size(send_stub):
    pre = dict(_PRE)
    pre.pop("part_size")
    pre["metadata"] = {"part_size": 4}
    _wire_upload(send_stub, hash_finish=False, pre=pre)
    client = quarkdrive.QuarkClient("cookie")
    out = client.upload_file(_BytesReader(b"0123456789"), "a.mkv", 10,
                             "dirfid")
    assert out["outcome"] == "uploaded"
    assert len(send_stub.find("PUT", "oss.example.com")) == 3


def test_upload_missing_host_fails_loudly(send_stub):
    pre = dict(_PRE)
    pre.pop("upload_host")
    _wire_upload(send_stub, hash_finish=False, pre=pre)
    client = quarkdrive.QuarkClient("cookie")
    with pytest.raises(quarkdrive.QuarkError) as excinfo:
        client.upload_file(_BytesReader(b"0123"), "a.mkv", 4, "dirfid")
    assert "主机" in str(excinfo.value)


# ---------------------------------------------------------------------------
# user_bindings 夸克凭据口
# ---------------------------------------------------------------------------

def test_binding_quark_cookie_scoping(db):
    mine = _binding(db, 7, "quark", cookie="__puus=mine")
    others = _binding(db, 8, "quark", cookie="__puus=other")
    p115 = _binding(db, 7, "pan115", cookie="x=1")
    assert ub.binding_quark_cookie(db, 7, mine.id) == "__puus=mine"
    with pytest.raises(ub.BindingError) as excinfo:
        ub.binding_quark_cookie(db, 7, others.id)
    assert excinfo.value.kind == "not_found"
    with pytest.raises(ub.BindingError) as excinfo:
        ub.binding_quark_cookie(db, 7, p115.id)
    assert excinfo.value.kind == "not_found"


class _FakeQuarkClient:
    behavior = "ok"
    rotated = None
    instances = []

    def __init__(self, cookie):
        self.cookie = cookie
        type(self).instances.append(self)

    def download_url(self, fid):
        if type(self).behavior == "auth":
            raise quarkdrive.QuarkAuthError("夸克登录态失效")
        if type(self).rotated:
            self.cookie = type(self).rotated
        return "https://cdn/quark-file"

    # 判死复核的验活三步（resolve 取链单次鉴权失败后走）：死
    # Cookie 在验活面同样失败，活 Cookie 全过。
    def check(self):
        if type(self).behavior == "auth":
            raise quarkdrive.QuarkAuthError("夸克登录态失效")
        return {"ok": True}

    def list_root(self):
        if type(self).behavior == "auth":
            raise quarkdrive.QuarkAuthError("夸克登录态失效")
        return []

    def list_dir(self, pdir_fid, page_size=100):
        if type(self).behavior == "auth":
            raise quarkdrive.QuarkAuthError("夸克登录态失效")
        return []


@pytest.fixture()
def fake_quark(monkeypatch):
    _FakeQuarkClient.behavior = "ok"
    _FakeQuarkClient.rotated = None
    _FakeQuarkClient.instances = []
    monkeypatch.setattr(quarkdrive, "QuarkClient", _FakeQuarkClient)
    return _FakeQuarkClient


def test_resolve_quark_copy_url_and_rotation_reseal(db, fake_quark):
    binding = _binding(db, 7, "quark", cookie="__puus=old; __pus=x")
    fake_quark.rotated = "__puus=new; __pus=x"
    url, cookie = ub.resolve_user_quark_copy_url(db, 7, "qfid9")
    assert url == "https://cdn/quark-file"
    assert cookie == "__puus=new; __pus=x"
    cred = (db.query(em.MountCredential)
            .filter(em.MountCredential.id == binding.credential_id).first())
    assert unseal_secret(cred.sealed_credential) == "__puus=new; __pus=x"


def test_resolve_quark_copy_url_auth_expires_binding(db, fake_quark):
    binding = _binding(db, 7, "quark")
    fake_quark.behavior = "auth"
    with pytest.raises(ub.BindingError) as excinfo:
        ub.resolve_user_quark_copy_url(db, 7, "qfid9")
    assert excinfo.value.kind == "expired"
    db.refresh(binding)
    assert binding.status == "expired"


def test_resolve_quark_copy_url_no_binding(db, fake_quark):
    with pytest.raises(ub.BindingError) as excinfo:
        ub.resolve_user_quark_copy_url(db, 7, "qfid9")
    assert excinfo.value.kind == "not_found"


# ---------------------------------------------------------------------------
# 首播门禁：夸克跨 provider 流
# ---------------------------------------------------------------------------

def _set_config(db, key, value):
    db.add(base_models.SystemConfig(key=key, value=value))
    db.commit()
    store.invalidate(key)


def _drive_item(db):
    _mount(db, 3, "drive", "drive:drive1")
    return _item(db)


def test_gate_quark_only_user_creates_quark_job(db):
    item = _drive_item(db)
    _binding(db, 7, "quark")
    out = tv.ensure_user_copy_gate(db, item, _user(7))
    assert out["verdict"] == "preparing"
    assert out["reason"] == "job_created"
    assert "aetrix-transfer" in out["detail"]
    job = db.query(em.TransferJob).first()
    assert job is not None
    assert job.dest_drive_id == "quark:user-7"
    assert job.provider == "drive"
    assert tv._is_cross_job(job)
    # 再点一次：在途单，不重复建
    out2 = tv.ensure_user_copy_gate(db, item, _user(7))
    assert out2["verdict"] == "preparing"
    assert out2["reason"] == "job_in_flight"
    assert db.query(em.TransferJob).count() == 1


def test_gate_quark_ready_variant_serves_quark_copy(db):
    item = _drive_item(db)
    _binding(db, 7, "quark")
    row = em.TransferVariant(
        item_id=item.id, source_file_id="origfid",
        variant_file_id="qfid1", variant_drive_id="quark:user-7",
        owner_scope="user", owner_ref="7", status="ready",
        size_bytes=1000)
    db.add(row)
    db.commit()
    out = tv.ensure_user_copy_gate(db, item, _user(7))
    assert out["verdict"] == "serve_quark_copy"
    assert out["variant"].variant_file_id == "qfid1"
    assert db.query(em.TransferJob).count() == 0


def test_gate_quark_switch_off_fails_without_fallback(db):
    item = _drive_item(db)
    _binding(db, 7, "quark")
    _set_config(db, tv.KEY_QUARK_PLAY_ENABLED, "false")
    out = tv.ensure_user_copy_gate(db, item, _user(7))
    assert out["verdict"] == "failed"
    assert out["reason"] == "quark_disabled"
    assert db.query(em.TransferJob).count() == 0


def test_gate_counterpart_binding_wins_over_quark(db):
    item = _drive_item(db)
    _binding(db, 7, "quark")
    _binding(db, 7, "pan115")
    out = tv.ensure_user_copy_gate(db, item, _user(7))
    assert out["verdict"] == "preparing"
    job = db.query(em.TransferJob).first()
    assert job.dest_drive_id == "pan115:user-7"


def test_gate_quark_only_user_115_item(db):
    item = _item(db, file_path="115:/电影/b.mkv")
    _binding(db, 7, "quark")
    out = tv.ensure_user_copy_gate(db, item, _user(7))
    assert out["verdict"] == "preparing"
    job = db.query(em.TransferJob).first()
    assert job.dest_drive_id == "quark:user-7"
    assert job.provider == "pan115"


def test_gate_no_binding_still_proceeds(db):
    item = _drive_item(db)
    out = tv.ensure_user_copy_gate(db, item, _user(7))
    assert out["verdict"] == "proceed"
    assert out["reason"] == "no_matching_binding"


def test_gate_quark_failed_job_retrigger_keeps_quark_dest(db):
    item = _drive_item(db)
    _binding(db, 7, "quark")
    old = em.TransferJob(
        item_id=item.id, trigger="playback", drive_id="drive1",
        provider="drive", owner_scope="user", owner_ref="7",
        dest_drive_id="quark:user-7", status="failed",
        error_kind="retriable", error_detail="网络抖动")
    db.add(old)
    db.commit()
    out = tv.ensure_user_copy_gate(db, item, _user(7))
    assert out["verdict"] == "preparing"
    assert out["reason"] == "job_retriggered"
    jobs = (db.query(em.TransferJob)
            .order_by(em.TransferJob.id).all())
    assert jobs[-1].dest_drive_id == "quark:user-7"
    assert jobs[-1].status == "pending"


def test_prefetch_quark_only_user(db):
    item = _drive_item(db)
    _binding(db, 7, "quark")
    out = tv.maybe_prefetch_transfer(db, item, _user(7))
    assert out["triggered"] is True
    job = db.query(em.TransferJob).first()
    assert job.dest_drive_id == "quark:user-7"
    assert job.trigger == "prefetch"


def test_claim_recheck_fails_quark_job_without_binding(db):
    item = _drive_item(db)
    job = em.TransferJob(
        item_id=item.id, trigger="playback", drive_id="drive1",
        provider="drive", owner_scope="user", owner_ref="7",
        dest_drive_id="quark:user-7", status="pending")
    db.add(job)
    db.commit()
    kept = tv._fail_user_jobs_without_binding(db, [job])
    assert kept == []
    db.refresh(job)
    assert job.status == "failed"
    assert job.error_kind == "no_binding"


# ---------------------------------------------------------------------------
# 执行器：源 → 用户夸克盘
# ---------------------------------------------------------------------------

from backend.emby_server import transfer_quark as tq


class _FakeDriveApi:
    def __init__(self, data, *, dead=False):
        self._data = data
        self._dead = dead
        self.range_calls = 0

    def get_file(self, drive_id, fid):
        if self._dead:
            raise tv._NotFound("源文件已删")
        return {"size": len(self._data)}

    def download_range(self, drive_id, fid, start, end):
        self.range_calls += 1
        return self._data[start:end + 1]


class _FakeQuarkExecClient:
    behavior = "ok"  # ok | rapid | auth | unresolved
    instances = []

    def __init__(self, cookie):
        self.cookie = cookie
        self.files = []  # 转存目录内文件项
        self.dir_fid = "qdir1"
        self.uploaded_bytes = None
        self.seen_hashes = None
        type(self).instances.append(self)

    def list_dir(self, fid):
        if fid == "0":
            return [{"fid": self.dir_fid, "name": "aetrix-transfer",
                     "size": 0, "is_dir": True}]
        return list(self.files)

    # 判死复核的验活步（转存单次鉴权失败后走）：auth 行为 =
    # Cookie 真死，验活同样抛鉴权错；其余行为验活通过。
    def check(self):
        if type(self).behavior == "auth":
            raise quarkdrive.QuarkAuthError("夸克登录态失效")
        return {"ok": True}

    def list_root(self):
        if type(self).behavior == "auth":
            raise quarkdrive.QuarkAuthError("夸克登录态失效")
        return []

    def mkdir(self, pdir_fid, name):  # pragma: no cover - 目录已备好
        raise AssertionError("目录已存在，不应再建")

    def upload_file(self, reader, name, size, pdir_fid, *, md5="", sha1=""):
        behavior = type(self).behavior
        if behavior == "auth":
            raise quarkdrive.QuarkAuthError("夸克登录态失效")
        self.seen_hashes = (md5, sha1)
        if behavior == "rapid":
            self.files.append({"fid": "qfid-rapid", "name": name,
                               "size": size, "is_dir": False})
            return {"outcome": "rapid", "fid": "qfid-rapid"}
        data = reader.read(size)
        self.uploaded_bytes = data
        if behavior == "unresolved":
            return {"outcome": "uploaded", "fid": "ghost-fid"}
        self.files.append({"fid": "qfid-up", "name": name,
                           "size": size, "is_dir": False})
        return {"outcome": "uploaded", "fid": "qfid-up"}


@pytest.fixture()
def fake_exec(monkeypatch):
    _FakeQuarkExecClient.behavior = "ok"
    _FakeQuarkExecClient.instances = []
    monkeypatch.setattr(tq, "_make_quark_client", _FakeQuarkExecClient)
    return _FakeQuarkExecClient


def _quark_job(db, item, user_id=7, provider="drive"):
    row = em.TransferJob(
        item_id=item.id, trigger="playback", drive_id="drive1",
        provider=provider, owner_scope="user", owner_ref=str(user_id),
        dest_drive_id=f"quark:user-{user_id}", status="running",
        attempts=1)
    db.add(row)
    db.commit()
    return row


def test_executor_full_upload_registers_ready(db, fake_exec):
    item = _drive_item(db)
    _binding(db, 7, "quark")
    job = _quark_job(db, item)
    data = b"hello-quark-world" * 100
    item.size = len(data)
    db.commit()
    api = _FakeDriveApi(data)
    out = tq.run_cross_to_quark(db, job, item, api)
    assert out["outcome"] == "done"
    client = fake_exec.instances[-1]
    assert client.uploaded_bytes == data
    md5, sha1 = client.seen_hashes
    import hashlib
    assert md5 == hashlib.md5(data).hexdigest()
    assert sha1 == hashlib.sha1(data).hexdigest()
    variant = db.query(em.TransferVariant).first()
    assert variant.status == "ready"
    assert variant.variant_drive_id == "quark:user-7"
    assert variant.variant_file_id == "qfid-up"
    db.refresh(job)
    assert job.status == "done"
    db.refresh(item)
    assert item.content_hash == hashlib.sha1(data).hexdigest()


def test_executor_rapid_hit_still_registers(db, fake_exec):
    fake_exec.behavior = "rapid"
    item = _drive_item(db)
    _binding(db, 7, "quark")
    job = _quark_job(db, item)
    api = _FakeDriveApi(b"x" * 1000)
    out = tq.run_cross_to_quark(db, job, item, api)
    assert out["outcome"] == "done"
    variant = db.query(em.TransferVariant).first()
    assert variant.variant_file_id == "qfid-rapid"


def test_executor_user_auth_expires_binding(db, fake_exec):
    fake_exec.behavior = "auth"
    item = _drive_item(db)
    binding = _binding(db, 7, "quark")
    job = _quark_job(db, item)
    out = tq.run_cross_to_quark(db, job, item, _FakeDriveApi(b"x" * 1000))
    assert out["outcome"] == "failed"
    assert out["error_kind"] == "auth_error"
    db.refresh(binding)
    assert binding.status == "expired"
    db.refresh(job)
    assert job.status == "failed"


def test_executor_source_dead(db, fake_exec):
    item = _drive_item(db)
    _binding(db, 7, "quark")
    job = _quark_job(db, item)
    out = tq.run_cross_to_quark(db, job, item,
                                _FakeDriveApi(b"", dead=True))
    assert out["outcome"] == "failed"
    assert out["error_kind"] == "dead"


def test_executor_unresolved_copy(db, fake_exec):
    fake_exec.behavior = "unresolved"
    item = _drive_item(db)
    _binding(db, 7, "quark")
    job = _quark_job(db, item)
    out = tq.run_cross_to_quark(db, job, item, _FakeDriveApi(b"x" * 1000))
    assert out["outcome"] == "failed"
    assert out["error_kind"] == "copy_unresolved"
    assert db.query(em.TransferVariant).count() == 0


def test_executor_no_binding(db, fake_exec):
    item = _drive_item(db)
    job = _quark_job(db, item)
    out = tq.run_cross_to_quark(db, job, item, _FakeDriveApi(b"x"))
    assert out["outcome"] == "failed"
    assert out["error_kind"] == "no_binding"


def test_run_cross_job_dispatches_quark(db, fake_exec):
    item = _drive_item(db)
    _binding(db, 7, "quark")
    job = _quark_job(db, item)
    out = tv._run_cross_job(db, job, item, _FakeDriveApi(b"x" * 1000))
    assert out["outcome"] == "done"


# ---------------------------------------------------------------------------
# 配置四齐：payload / write / schema
# ---------------------------------------------------------------------------

def test_transfer_config_quark_field_roundtrip(db):
    payload = tv.transfer_config_payload(db)
    assert payload["quark_play_enabled"] is True
    out = tv.write_transfer_config(db, quark_play_enabled=False)
    assert out["quark_play_enabled"] is False
    store.invalidate(tv.KEY_QUARK_PLAY_ENABLED)
    assert tv.quark_play_enabled(db) is False
    out = tv.write_transfer_config(db, quark_play_enabled=True)
    assert out["quark_play_enabled"] is True


def test_transfer_schema_accepts_quark_key(db):
    from backend import config_service
    config_service.save_config(
        db, {"quark_play_enabled": "false"}, schema="transfer")
    row = (db.query(base_models.SystemConfig)
           .filter(base_models.SystemConfig.key
                   == "quark_play_enabled").first())
    assert row is not None and row.value == "false"
    store.invalidate(tv.KEY_QUARK_PLAY_ENABLED)
    assert tv.quark_play_enabled(db) is False
    from backend.config_schema import ConfigValueError
    with pytest.raises(ConfigValueError):
        config_service.save_config(
            db, {"quark_play_enabled": "maybe"}, schema="transfer")


# ---------------------------------------------------------------------------
# video_stream serve_quark_copy 分支：中继出流（绝不 302）
# ---------------------------------------------------------------------------

import asyncio
from types import SimpleNamespace as _SN

from fastapi import HTTPException


def _run(coro):
    return asyncio.run(coro)


class _FakeQuery:
    def filter(self, *args, **kwargs):
        return self

    def order_by(self, *args, **kwargs):
        return self

    def first(self):
        return None

    def one_or_none(self):
        return None

    def all(self):
        return []


class _FakeDB:
    def query(self, *args, **kwargs):
        return _FakeQuery()


def _stream_setup_quark(monkeypatch, *, switch_on=True,
                        resolve_error=None):
    """桩通 video_stream 前置链路，停在 serve_quark_copy 分支门口。

    接线与 test_playback_115_stale_and_quota_log 的 115 分支
    桩法同款：门禁直接给 serve_quark_copy verdict，取链口与
    中继口都换桩，捕获实际传给中继的 URL 与请求头。
    """
    from starlette.requests import Request
    from starlette.responses import Response

    from backend.emby_server import api
    from backend.emby_server import playback_strategy as _strategy
    from backend.emby_server import streaming as _streaming

    item = _SN(id=123, drive_file_id="origfid", container="mkv",
               file_path="mount://3/电影/a.mkv")
    monkeypatch.setattr(api, "_require_visible_item",
                        lambda db, user, item_id: item)
    monkeypatch.setattr(api, "ensure_playback_allowed",
                        lambda db, user: None)
    monkeypatch.setattr(
        "backend.emby_server.playback_gate.ensure_binding_playback_allowed",
        lambda db, u, r=None: None)
    monkeypatch.setattr(
        "backend.emby_server.playback_gate._request_uses_issued_play_sign",
        lambda request, db: False)
    monkeypatch.setattr(api, "_observe_traffic", lambda resp: resp)
    monkeypatch.setattr(api.play_sign, "check_referer",
                        lambda request, db: None)
    monkeypatch.setattr(api, "_note_play_start_safe",
                        lambda item, **kwargs: None)

    def fake_get(db, key, default="", **kwargs):
        return default

    monkeypatch.setattr(_strategy.store, "get_value", fake_get)
    monkeypatch.setattr(tv, "resolve_play_file_id",
                        lambda db, item, user: ("origfid", None))
    monkeypatch.setattr(tv, "ensure_user_copy_gate",
                        lambda db, item, user, **kwargs: {
                            "verdict": "serve_quark_copy",
                            "variant": _SN(variant_file_id="qfid1",
                                           id=55)})
    monkeypatch.setattr(tv, "quark_play_enabled",
                        lambda db=None: switch_on)

    def _resolve(db, user_id, fid):
        if resolve_error is not None:
            raise resolve_error
        assert fid == "qfid1"
        return "https://quark-cdn/file", "__puus=tok; __pus=x"

    monkeypatch.setattr(ub, "resolve_user_quark_copy_url", _resolve)
    captured = {}

    async def _fake_relay(url, request, headers=None,
                          media_type="video/mp4", pt=None, **kwargs):
        captured["url"] = url
        captured["headers"] = headers
        return Response(content=b"stream", status_code=200,
                        media_type="video/mp4")

    monkeypatch.setattr(_streaming, "serve_mount_relay_async",
                        _fake_relay)
    scope = {"type": "http", "method": "GET",
             "path": "/Videos/item/stream",
             "raw_path": b"/Videos/item/stream",
             "query_string": b"", "headers": [], "scheme": "http",
             "server": ("testserver", 80),
             "client": ("testclient", 50000), "root_path": ""}
    return api, Request(scope), _SN(id=7), _FakeDB(), captured


def test_video_stream_quark_copy_relays_not_redirects(monkeypatch):
    api, req, user, db, captured = _stream_setup_quark(monkeypatch)
    resp = _run(api.video_stream("item", req, user, db))
    # 中继：200 直出、绝不 302 裸发直链
    assert resp.status_code == 200
    assert "location" not in resp.headers
    assert captured["url"] == "https://quark-cdn/file"
    headers = captured["headers"]
    assert headers["Cookie"] == "__puus=tok; __pus=x"
    assert headers["Referer"] == quarkdrive.QUARK_REFERER
    assert headers["User-Agent"] == quarkdrive.QUARK_UA


def test_video_stream_quark_copy_switch_off_503(monkeypatch):
    api, req, user, db, captured = _stream_setup_quark(
        monkeypatch, switch_on=False)
    with pytest.raises(HTTPException) as excinfo:
        _run(api.video_stream("item", req, user, db))
    assert excinfo.value.status_code == 503
    assert captured == {}


def test_video_stream_quark_copy_expired_cookie_409(monkeypatch):
    api, req, user, db, captured = _stream_setup_quark(
        monkeypatch,
        resolve_error=ub.BindingError("expired", "登录已过期，请重新绑定。"))
    with pytest.raises(HTTPException) as excinfo:
        _run(api.video_stream("item", req, user, db))
    assert excinfo.value.status_code == 409
    assert "过期" in excinfo.value.detail
    assert captured == {}


def test_executor_115_source_to_quark(db, fake_exec, monkeypatch):
    item = _item(db, file_path="115:/电影/b.mkv")
    _binding(db, 7, "quark")
    job = _quark_job(db, item, provider="pan115")
    data = b"pan115-source-bytes" * 50
    item.size = len(data)
    db.commit()
    fake115 = _SN(ua="UA115", cookie="c115=1")
    fake115.download_url = lambda pickcode: "https://115.example/dl"
    monkeypatch.setattr(
        tv, "_resolve_115_source",
        lambda db_, it: (fake115, "pc1", len(data), ""))

    def _range_get(url, headers):
        rng = headers["Range"].removeprefix("bytes=")
        start, end = (int(x) for x in rng.split("-"))
        return _SN(status_code=206, content=data[start:end + 1])

    monkeypatch.setattr(tq, "_http_get", _range_get)
    out = tq.run_cross_to_quark(db, job, item, None)
    assert out["outcome"] == "done"
    client = fake_exec.instances[-1]
    assert client.uploaded_bytes == data
    variant = db.query(em.TransferVariant).first()
    assert variant.variant_drive_id == "quark:user-7"
    assert variant.variant_file_id == "qfid-up"
