"""图床镜像（批一）：指纹、镜像层、工人、服务端点与管理口。

隔离临时 SQLite + 临时图片目录（EMBY_IMAGE_DIR 指过去）；鉴权用
TestClient dependency_overrides（与 test_media_library_endpoints 同款）。
"""

import os
import tempfile
import threading
from datetime import datetime, timedelta

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
from backend.emby_server import cover_mirror
from backend.emby_server import models as em

init_db()

_JPG = b"\xff\xd8\xff\x00fake-jpg"
_PNG = b"\x89PNG\r\n\x1a\nfake-png"


@pytest.fixture()
def db():
    s = SessionLocal()
    try:
        for table in (em.CoverMirror, em.MediaItem, em.Library,
                      base_models.WebUser):
            s.query(table).delete()
        s.commit()
        yield s
    finally:
        s.close()


@pytest.fixture()
def imgdir(tmp_path, monkeypatch):
    monkeypatch.setenv("EMBY_IMAGE_DIR", str(tmp_path))
    return tmp_path


@pytest.fixture()
def enabled(monkeypatch):
    monkeypatch.setattr(cover_mirror, "COVER_MIRROR_ENABLED", 1)
    monkeypatch.setenv("COVER_MIRROR_BACKEND", "local")
    return True


def _row(db, url, source, local_path=None, status="pending", **kw):
    h = cover_mirror.source_hash(url, local_path)
    attempts = kw.pop("attempts", 0)
    row = em.CoverMirror(source_hash=h, source_url=url, source=source,
                         backend="local", status=status,
                         local_path_hint=local_path, attempts=attempts, **kw)
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def _mirror_file(imgdir, row, data=_JPG):
    path = os.path.join(cover_mirror.mirror_dir(), row.mirror_rel)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as handle:
        handle.write(data)
    return path


# ---------------------------------------------------------------- 指纹与可镜像判定


def test_source_hash_local_changes_with_file(imgdir):
    f = imgdir / "a.jpg"
    f.write_bytes(_JPG)
    h1 = cover_mirror.source_hash("https://x/y.jpg", str(f))
    f.write_bytes(_JPG + b"more-bytes")
    h2 = cover_mirror.source_hash("https://x/y.jpg", str(f))
    assert h1 != h2
    assert cover_mirror.source_hash("https://x/y.jpg") == cover_mirror.source_hash(
        "https://x/y.jpg")


@pytest.mark.parametrize(
    ("source", "url", "borrow_ok", "want"),
    [
        ("render", "/emby/Items/g/Images/Primary", False, True),
        ("uploaded", "/emby/Items/g/Images/Primary", False, True),
        ("collage", "/api/sets/1/cover", False, True),
        ("borrow", "https://img.example/p.jpg", True, True),
        ("borrow", "https://img.example/p.jpg", False, False),
        ("borrow", "/emby/Items/g/Images/Primary", True, False),
        ("svg", "/api/user/media-library/libraries/1/cover-placeholder.svg", True, False),
        ("none", None, True, False),
        ("render", "", False, False),
    ])
def test_is_mirrorable(source, url, borrow_ok, want):
    assert cover_mirror.is_mirrorable(source, url, borrow_ok) is want


# ---------------------------------------------------------------- 读路径镜像层


def test_apply_mirror_disabled_returns_identical(db, imgdir):
    result = {"url": "/emby/Items/g/Images/Primary", "source": "render"}
    assert cover_mirror.apply_mirror(db, result) == result
    assert db.query(em.CoverMirror).count() == 0


def test_apply_mirror_success_swaps_url_keeps_source(db, imgdir, enabled):
    h = cover_mirror.source_hash("/emby/Items/g/Images/Primary")
    row = _row(db, "/emby/Items/g/Images/Primary", "render", status="success",
               mirror_rel=f"{h}.jpg", content_type="image/jpeg")
    _mirror_file(imgdir, row)
    out = cover_mirror.apply_mirror(db, {"url": "/emby/Items/g/Images/Primary",
                                         "source": "render"})
    assert out == {"url": f"/cdn/covers/{h}", "source": "render"}


def test_apply_mirror_success_but_file_missing_falls_back(db, imgdir, enabled):
    h = cover_mirror.source_hash("/emby/Items/g/Images/Primary")
    _row(db, "/emby/Items/g/Images/Primary", "render", status="success",
         mirror_rel=f"{h}.jpg", content_type="image/jpeg")
    result = {"url": "/emby/Items/g/Images/Primary", "source": "render"}
    assert cover_mirror.apply_mirror(db, result) == result


def test_apply_mirror_no_row_enqueues_pending(db, imgdir, enabled):
    result = {"url": "/emby/Items/g/Images/Primary", "source": "render"}
    assert cover_mirror.apply_mirror(db, result) == result
    row = db.query(em.CoverMirror).one()
    assert row.status == "pending" and row.source == "render"


def test_apply_mirror_svg_and_borrow_off_never_enqueue(db, imgdir, enabled):
    svg = {"url": "/api/user/media-library/libraries/1/cover-placeholder.svg",
           "source": "svg"}
    assert cover_mirror.apply_mirror(db, svg) == svg
    borrow = {"url": "https://img.example/p.jpg", "source": "borrow"}
    assert cover_mirror.apply_mirror(db, borrow) == borrow
    assert db.query(em.CoverMirror).count() == 0


def test_apply_mirror_replaced_local_file_enqueues_new_fingerprint(db, imgdir, enabled):
    f = imgdir / "cover.webp"
    f.write_bytes(b"v1-bytes")
    url = "/emby/Items/g/Images/Primary"
    old = _row(db, url, "render", local_path=str(f), status="success",
               mirror_rel=f"{cover_mirror.source_hash(url, str(f))}.webp",
               content_type="image/webp")
    _mirror_file(imgdir, old, b"v1-bytes")
    f.write_bytes(b"v2-bytes-much-longer")
    out = cover_mirror.apply_mirror(db, {"url": url, "source": "render"},
                                    local_path=str(f))
    assert out == {"url": url, "source": "render"}
    rows = db.query(em.CoverMirror).order_by(em.CoverMirror.created_at).all()
    assert len(rows) == 2
    assert rows[1].status == "pending"


def test_enqueue_idempotent_and_backend_gate(db, imgdir, enabled, monkeypatch):
    url = "/emby/Items/g/Images/Primary"
    assert cover_mirror.enqueue_mirror(db, url, "render") is True
    assert cover_mirror.enqueue_mirror(db, url, "render") is False
    assert db.query(em.CoverMirror).count() == 1
    monkeypatch.setenv("COVER_MIRROR_BACKEND", "115")
    assert cover_mirror.enqueue_mirror(db, "https://img.example/q.jpg",
                                       "borrow") is False


def test_enqueue_disk_cap_blocks(db, imgdir, enabled, monkeypatch):
    monkeypatch.setattr(cover_mirror, "_MIRROR_DIR_SIZE_CAP", 0)
    assert cover_mirror.enqueue_mirror(db, "/emby/Items/g/Images/Primary",
                                       "render") is False
    assert db.query(em.CoverMirror).count() == 0


def test_mirror_url_with_and_without_base(monkeypatch):
    monkeypatch.setenv("COVER_MIRROR_BASE_URL", "")
    assert cover_mirror.mirror_url("ab" * 32) == "/cdn/covers/" + "ab" * 32
    monkeypatch.setenv("COVER_MIRROR_BASE_URL", "https://cdn.example/")
    assert cover_mirror.mirror_url("ab" * 32) == "https://cdn.example/cdn/covers/" + "ab" * 32


def test_mirror_file_path_rejects_traversal(imgdir):
    row = em.CoverMirror(source_hash="ab" * 32, mirror_rel="../../etc/passwd")
    assert cover_mirror.mirror_file_path(row) is None


# ---------------------------------------------------------------- 工人


def test_process_row_local_copy_success(db, imgdir, enabled):
    f = imgdir / "src.jpg"
    f.write_bytes(_JPG)
    row = _row(db, "/emby/Items/g/Images/Primary", "render", local_path=str(f))
    assert cover_mirror.process_row(db, row) == "success"
    db.refresh(row)
    assert row.status == "success" and row.mirror_rel.endswith(".jpg")
    assert row.content_type == "image/jpeg"
    with open(os.path.join(cover_mirror.mirror_dir(), row.mirror_rel), "rb") as h:
        assert h.read() == _JPG


def test_process_row_source_changed_fails_closed(db, imgdir, enabled):
    f = imgdir / "src.jpg"
    f.write_bytes(_JPG)
    row = _row(db, "/emby/Items/g/Images/Primary", "render", local_path=str(f))
    f.write_bytes(_JPG + b"changed")
    assert cover_mirror.process_row(db, row) == "failed"
    db.refresh(row)
    assert row.status == "failed" and row.last_error_kind == "source_changed"
    assert row.attempts == 1 and row.next_attempt_at is not None


def test_process_row_http_stub_success_and_ext(db, imgdir, enabled):
    def fake_get(url):
        assert url == "https://img.example/p.png"
        return _PNG, "image/png"

    row = _row(db, "https://img.example/p.png", "borrow")
    assert cover_mirror.process_row(db, row, http_get=fake_get) == "success"
    db.refresh(row)
    assert row.status == "success" and row.mirror_rel.endswith(".png")
    assert row.content_type == "image/png"


def test_process_row_http_failure_then_dead(db, imgdir, enabled):
    def boom(url):
        raise RuntimeError("down")

    row = _row(db, "https://img.example/p.png", "borrow", attempts=2)
    assert cover_mirror.process_row(db, row, http_get=boom) == "failed"
    db.refresh(row)
    assert row.status == "dead" and row.attempts == 3
    assert row.next_attempt_at is None


def test_claim_skips_not_due_failed_rows(db, imgdir, enabled):
    due = _row(db, "/emby/Items/a/Images/Primary", "render")
    future = _row(db, "/emby/Items/b/Images/Primary", "render", status="failed",
                  next_attempt_at=datetime.now() + timedelta(hours=1), attempts=1)
    claimed = cover_mirror.claim_due_rows(db, 10)
    assert [r.source_hash for r in claimed] == [due.source_hash]
    db.refresh(future)
    assert future.status == "failed"


def test_process_batch_disabled_claims_nothing(db, imgdir):
    _row(db, "/emby/Items/a/Images/Primary", "render")
    assert cover_mirror.process_mirror_batch(db) == {"claimed": 0, "success": 0,
                                                     "failed": 0}
    row = db.query(em.CoverMirror).one()
    assert row.status == "pending"


def test_retry_mirror_states(db, imgdir):
    dead = _row(db, "https://img.example/a.jpg", "borrow", status="dead", attempts=3)
    assert cover_mirror.retry_mirror(db, dead.source_hash) is True
    db.refresh(dead)
    assert dead.status == "pending" and dead.attempts == 0
    ok = _row(db, "https://img.example/b.jpg", "borrow", status="success")
    assert cover_mirror.retry_mirror(db, ok.source_hash) is False
    assert cover_mirror.retry_mirror(db, "f" * 64) is None


def test_mirror_stats(db, imgdir):
    _row(db, "https://img.example/a.jpg", "borrow", status="success")
    _row(db, "https://img.example/b.jpg", "borrow", status="failed", attempts=1)
    stats = cover_mirror.mirror_stats(db)
    assert stats["total"] == 2
    assert stats["by_status"] == {"success": 1, "failed": 1}
    assert stats["total_bytes"] == 0


def test_daemon_starts_and_stops_without_work():
    stop = threading.Event()
    thread = cover_mirror.start_mirror_daemon(stop_event=stop)
    assert thread.is_alive()
    stop.set()
    thread.join(timeout=5)
    assert not thread.is_alive()


# ---------------------------------------------------------------- 端点（TestClient）


@pytest.fixture()
def client(db):
    from fastapi.testclient import TestClient

    from backend.database import get_db
    from backend.main import app

    app.dependency_overrides[get_db] = lambda: SessionLocal()
    try:
        yield TestClient(app, raise_server_exceptions=False)
    finally:
        app.dependency_overrides.clear()


@pytest.fixture()
def staff_user(db):
    u = base_models.WebUser(username="mirrorstaff", is_staff=True,
                            is_active=True, password_hash="test")
    db.add(u)
    db.commit()
    db.refresh(u)
    return u


@pytest.fixture()
def admin_client(client, staff_user):
    from backend.emby_server.portal import require_staff
    from backend.main import app

    app.dependency_overrides[require_staff] = lambda: staff_user
    try:
        yield client
    finally:
        del app.dependency_overrides[require_staff]


def test_serve_mirror_success_and_404s(client, imgdir, enabled):
    h = cover_mirror.source_hash("https://img.example/p.jpg")
    s = SessionLocal()
    row = em.CoverMirror(source_hash=h, source_url="https://img.example/p.jpg",
                         source="borrow", backend="local", status="success",
                         mirror_rel=f"{h}.jpg", content_type="image/jpeg",
                         attempts=0)
    s.add(row)
    s.commit()
    s.close()
    path = os.path.join(cover_mirror.mirror_dir(), f"{h}.jpg")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as handle:
        handle.write(_JPG)
    resp = client.get(f"/cdn/covers/{h}")
    assert resp.status_code == 200
    assert resp.content == _JPG
    assert resp.headers["content-type"].startswith("image/jpeg")
    assert client.get("/cdn/covers/" + "0" * 64).status_code == 404
    assert client.get("/cdn/covers/not-a-hash").status_code == 404


def test_admin_stats_and_retry(admin_client, imgdir):
    s = SessionLocal()
    dead = em.CoverMirror(source_hash="c" * 64, source_url="https://img.example/a.jpg",
                          source="borrow", backend="local", status="dead",
                          attempts=3)
    s.add(dead)
    s.commit()
    s.close()
    resp = admin_client.get("/api/admin/media-library/cover-mirrors")
    assert resp.status_code == 200
    assert resp.json()["by_status"] == {"dead": 1}
    resp = admin_client.post("/api/admin/media-library/cover-mirrors/retry",
                             json={"source_hash": "c" * 64})
    assert resp.status_code == 200 and resp.json()["ok"] is True
    s = SessionLocal()
    row = s.query(em.CoverMirror).filter(
        em.CoverMirror.source_hash == "c" * 64).one()
    assert row.status == "pending" and row.attempts == 0
    s.close()
    assert admin_client.post("/api/admin/media-library/cover-mirrors/retry",
                             json={"source_hash": "d" * 64}).status_code == 404
