"""管理后台只读观测口（专线 B 第一阶段：补口）。

覆盖 5 个新建 GET 口：绑定列表、采样进度、sidecar 状态、映射列表、
139 池列表。全部在隔离临时 SQLite 里跑，不连生产库；sidecar 成功
路径用本地假 UDS 服务端，不碰真实侧车。

总线两口（/api/admin/playback-bus/snapshot 与 /commands）已在
playback_bus_admin.py 存在，本单不重复建、不重复测。
"""

import json
import os
import socket
import tempfile
import threading

os.environ.setdefault("DATABASE_TYPE", "sqlite")
os.environ.setdefault("REDIS_ENABLED", "false")
_fd, _tmppath = tempfile.mkstemp(suffix=".db")
os.close(_fd)
os.environ["DATABASE_URL"] = f"sqlite:///{_tmppath}"

from backend import database as _dbmod
from sqlalchemy import create_engine as _ce
from sqlalchemy.orm import sessionmaker as _sm

_dbmod.engine = _ce(os.environ["DATABASE_URL"])
_dbmod.configure_session_local(_sm(bind=_dbmod.engine))

import pytest

from backend import models as base_models
from backend.database import SessionLocal, init_db
from backend.emby_server import models as em

init_db()


@pytest.fixture()
def db():
    s = SessionLocal()
    try:
        for table in (em.UserStorageBinding, em.UpstreamItemMapping,
                      em.Yun139AccountPoolMember, em.Yun139AccountPool,
                      em.MediaItem, base_models.WebUser):
            s.query(table).delete()
        s.commit()
        yield s
    finally:
        s.close()


@pytest.fixture()
def admin(db):
    u = base_models.WebUser(username="readonlyadmin", is_staff=True,
                            is_active=True, password_hash="test")
    db.add(u)
    db.commit()
    db.refresh(u)
    return u


@pytest.fixture()
def client(db):
    """真实 app 的 TestClient：get_db 指向隔离库，鉴权逐用例覆盖。"""
    from fastapi.testclient import TestClient

    from backend.database import get_db
    from backend.main import app

    app.dependency_overrides[get_db] = lambda: SessionLocal()
    try:
        yield TestClient(app, raise_server_exceptions=False)
    finally:
        app.dependency_overrides.clear()


@pytest.fixture()
def authed(client, admin):
    from backend.api.admin_core import get_current_admin
    from backend.main import app

    app.dependency_overrides[get_current_admin] = lambda: admin
    try:
        yield client
    finally:
        del app.dependency_overrides[get_current_admin]


READ_PATHS = [
    "/api/admin/storage-bindings",
    "/api/admin/sample-hash/progress",
    "/api/admin/providers/sidecar",
    "/api/admin/upstream-mappings",
    "/api/admin/yun139/pools",
]


def test_read_paths_require_auth(client):
    """5 口无鉴权头 → 401/403（与 playback_admin 同口径）。"""
    for path in READ_PATHS:
        r = client.get(path)
        assert r.status_code in (401, 403), path


def _binding(db, user_id, provider="pan115", label="b1", status="active"):
    row = em.UserStorageBinding(user_id=user_id, provider=provider,
                                account_label=label, credential_id=999,
                                status=status)
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def test_bindings_list_filters_and_no_credential_leak(db, authed):
    _binding(db, 1, label="a", status="active")
    _binding(db, 2, label="b", status="invalid")
    _binding(db, 1, label="c", status="active")
    r = authed.get("/api/admin/storage-bindings")
    assert r.status_code == 200
    body = r.json()
    assert body["total"] == 3
    assert all("credential_id" not in item for item in body["items"])
    assert all(item["has_credential"] is True for item in body["items"])
    r = authed.get("/api/admin/storage-bindings", params={"user_id": 1})
    assert r.json()["total"] == 2
    assert all(item["user_id"] == 1 for item in r.json()["items"])
    r = authed.get("/api/admin/storage-bindings", params={"status": "invalid"})
    assert r.json()["total"] == 1
    r = authed.get("/api/admin/storage-bindings",
                   params={"limit": 999, "offset": -5})
    assert r.json()["limit"] == 200
    assert r.json()["offset"] == 0


def _lib(db):
    row = em.Library(guid="g-readonly", name="电影库",
                     collection_type="movies", paths="", storage_backends="")
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def test_sample_hash_progress_counts_only_no_write(db, authed):
    lib = _lib(db)
    pending = em.MediaItem(guid="p1", library_id=lib.id, item_type="movie",
                           name="p1", file_path="/data/a.mkv")
    sampled = em.MediaItem(guid="s1", library_id=lib.id, item_type="movie",
                           name="s1", file_path="/data/b.mkv",
                           content_hash="ab" * 16,
                           content_hash_algo="sample-sha256")
    db.add_all([pending, sampled])
    db.commit()
    r = authed.get("/api/admin/sample-hash/progress")
    assert r.status_code == 200
    body = r.json()
    assert body["pending"] == 1
    assert body["sampled"] == 1
    # 只读：调用后待补行仍为空，没被触发补填
    db.refresh(pending)
    assert pending.content_hash in (None, "")


def test_sidecar_unavailable_is_error_not_500(authed, monkeypatch):
    """测试环境无侧车：available=False + remote_unavailable，整口仍 200。"""
    monkeypatch.setenv("PROVIDER_RUNTIME_SOCK_DIR", "/nonexistent-dir-xyz")
    r = authed.get("/api/admin/providers/sidecar")
    assert r.status_code == 200
    providers = {p["provider"]: p for p in r.json()["providers"]}
    assert set(providers) == {"p115", "yun139"}
    for p in providers.values():
        assert p["available"] is False
        assert p["error"]["kind"] == "remote_unavailable"
    r = authed.get("/api/admin/providers/sidecar",
                   params={"provider": "nope"})
    assert r.status_code == 400


class _FakeSidecar(threading.Thread):
    """最小 UDS JSON-RPC 服务端：一连接一请求，行收发。"""

    def __init__(self, sock_path):
        super().__init__(daemon=True)
        self.sock_path = sock_path
        self.stop_event = threading.Event()

    def run(self):
        srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        srv.bind(self.sock_path)
        srv.listen(8)
        srv.settimeout(0.5)
        try:
            while not self.stop_event.is_set():
                try:
                    conn, _ = srv.accept()
                except socket.timeout:
                    continue
                try:
                    chunks = []
                    while True:
                        data = conn.recv(65536)
                        if not data:
                            break
                        chunks.append(data)
                    raw = b"".join(chunks)
                    if not raw.strip():
                        continue  # 停表时的空探活连接，无请求可解析
                    req = json.loads(raw.decode("utf-8"))
                    method = req.get("method")
                    if method == "manifest":
                        result = {"protocol": "external-provider-runtime-v1",
                                  "provider": "p115"}
                    elif method == "health":
                        result = {"ok": True}
                    else:
                        result = {}
                    resp = {"jsonrpc": "2.0", "id": req.get("id"),
                            "result": result}
                    conn.sendall((json.dumps(resp) + "\n").encode("utf-8"))
                finally:
                    conn.close()
        finally:
            srv.close()

    def stop(self):
        self.stop_event.set()
        try:
            dummy = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            dummy.connect(self.sock_path)
            dummy.close()
        except OSError:
            pass
        self.join(timeout=5)


def test_sidecar_available_via_fake_uds(authed, monkeypatch, tmp_path):
    monkeypatch.setenv("PROVIDER_RUNTIME_SOCK_DIR", str(tmp_path))
    sock_dir = tmp_path / "p115"
    sock_dir.mkdir()
    server = _FakeSidecar(str(sock_dir / "runtime.sock"))
    server.start()
    try:
        r = authed.get("/api/admin/providers/sidecar",
                       params={"provider": "p115"})
        assert r.status_code == 200
        item = r.json()["providers"][0]
        assert item["available"] is True
        assert item["manifest"]["provider"] == "p115"
        assert item["health"] == {"ok": True}
        assert item["error"] is None
    finally:
        server.stop()


def test_mappings_list_filters(db, authed):
    for local_id, source_id, upstream_id in (
            (10, 1, "emby-1"), (10, 2, "emby-2"), (11, 1, "emby-3")):
        db.add(em.UpstreamItemMapping(local_item_id=local_id,
                                      source_id=source_id,
                                      upstream_item_id=upstream_id,
                                      confidence=0.9, evidence="tmdb"))
    db.commit()
    r = authed.get("/api/admin/upstream-mappings")
    assert r.json()["total"] == 3
    r = authed.get("/api/admin/upstream-mappings",
                   params={"local_item_id": 10})
    assert r.json()["total"] == 2
    r = authed.get("/api/admin/upstream-mappings", params={"source_id": 1})
    assert r.json()["total"] == 2
    item = r.json()["items"][0]
    assert item["confidence"] == 0.9
    assert item["evidence"] == "tmdb"


def test_yun139_pools_members_no_credential_id(db, authed):
    pool = em.Yun139AccountPool(name="p1", strategy="weighted",
                                cooldown_seconds=60, max_concurrent=0)
    db.add(pool)
    db.commit()
    db.add(em.Yun139AccountPoolMember(pool_id=pool.id, label="a",
                                      weight=2, fail_count=3,
                                      credential_id=None))
    db.add(em.Yun139AccountPoolMember(pool_id=pool.id, label="b",
                                      weight=1, fail_count=0,
                                      credential_id=999))
    db.commit()
    r = authed.get("/api/admin/yun139/pools")
    assert r.status_code == 200
    body = r.json()
    assert body["total"] == 1
    item = body["items"][0]
    assert item["name"] == "p1"
    assert item["member_count"] == 2
    members = {m["label"]: m for m in item["members"]}
    assert members["a"]["has_credential"] is False
    assert members["b"]["has_credential"] is True
    assert members["b"]["fail_count"] == 0
    assert all("credential_id" not in m for m in item["members"])
