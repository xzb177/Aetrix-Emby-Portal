# -*- coding: utf-8 -*-
"""大 MP4 faststart：资格、失败保留原文件、存量默认不改。不调用真 ffmpeg、不打网盘。"""
import importlib.util
import os
import struct

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend.database import Base
from backend.emby_server import models as em
from backend.emby_server import mp4_faststart as fs


@pytest.fixture(autouse=True)
def _flags_off(monkeypatch):
    monkeypatch.delenv(fs.ENV_INGEST, raising=False)
    monkeypatch.delenv(fs.ENV_BACKFILL, raising=False)
    monkeypatch.delenv(fs.ENV_MIN_BYTES, raising=False)


def _atom(typ: bytes, payload: bytes = b"") -> bytes:
    return struct.pack(">I4s", 8 + len(payload), typ) + payload


def test_detect_moov_front_and_back(tmp_path):
    front = tmp_path / "front.mp4"
    back = tmp_path / "back.mp4"
    front.write_bytes(_atom(b"ftyp", b"isom") + _atom(b"moov", b"\x00\x00") + _atom(b"mdat", b"xx"))
    back.write_bytes(_atom(b"ftyp", b"isom") + _atom(b"mdat", b"xx") + _atom(b"moov", b"\x00\x00"))
    assert fs.detect_moov_position(str(front)) == "front"
    assert fs.detect_moov_position(str(back)) == "back"
    assert fs.detect_moov_bytes(front.read_bytes()) == "front"
    assert fs.detect_moov_bytes(back.read_bytes()) == "back"


def test_eligible_threshold_and_containers(monkeypatch):
    monkeypatch.setenv(fs.ENV_MIN_BYTES, "1000")
    assert fs.eligible(container="mkv", size=99999) == (False, "not_mp4")
    assert fs.eligible(container="mp4", size=99999, moov_position="front") == (False, "already_front")
    assert fs.eligible(container="mp4", size=10, moov_position=None) == (False, "below_threshold")
    assert fs.eligible(container="mp4", size=1000, moov_position=None) == (True, "large")
    assert fs.eligible(container="m4v", size=10, moov_position="back") == (True, "moov_back")
    assert fs.eligible(path="/data/movie.mov", size=5000) == (True, "large")


def test_ingest_off_does_not_queue(monkeypatch):
    queued = []
    monkeypatch.setattr(fs, "_enqueue", queued.append)
    assert fs.ingest_enabled() is False
    assert fs.note_new_ingest(
        item_id=1, file_path="/a.mp4", size=fs.DEFAULT_MIN_BYTES,
        container="mp4", moov_position="back") is False
    assert queued == []


def test_ingest_on_queues_only_eligible(monkeypatch):
    monkeypatch.setenv(fs.ENV_INGEST, "1")
    monkeypatch.setenv(fs.ENV_MIN_BYTES, "100")
    queued = []
    monkeypatch.setattr(fs, "_enqueue", queued.append)
    assert fs.note_new_ingest(
        item_id=7, file_path="mount://1/a.mp4", size=50,
        container="mp4", moov_position="back", drive_file_id="fid") is True
    assert fs.note_new_ingest(
        item_id=8, file_path="/b.mkv", size=10_000, container="mkv") is False
    assert fs.note_new_ingest(
        item_id=9, file_path="/c.mp4", size=10_000,
        container="mp4", moov_position="front") is False
    assert fs.note_new_ingest(
        item_id=10, file_path="/d.mp4", size=10, container="mp4") is False
    assert [job["item_id"] for job in queued] == [7]
    assert queued[0]["drive_file_id"] == "fid"
    assert queued[0]["reason"] == "moov_back"


def test_local_failure_keeps_original(tmp_path):
    src = tmp_path / "movie.mp4"
    src.write_bytes(b"ORIGINAL-BYTES")

    def _run(cmd, **kwargs):
        return type("P", (), {"returncode": 1, "stderr": b"ffmpeg nope"})()

    result = fs.rewrite_local(str(src), run=_run, detect=lambda path: "back")
    assert result["ok"] is False
    assert src.read_bytes() == b"ORIGINAL-BYTES"
    assert not os.path.exists(str(src) + ".faststart-tmp")


def test_local_success_replaces_only_after_moov_front(tmp_path):
    src = tmp_path / "movie.mp4"
    src.write_bytes(b"ORIGINAL-BYTES")

    def _detect(path):
        return "front" if path.endswith(".faststart-tmp") else "back"

    def _run(cmd, **kwargs):
        with open(cmd[-1], "wb") as out:
            out.write(b"REWRITTEN")
        return type("P", (), {"returncode": 0, "stderr": b""})()

    result = fs.rewrite_local(str(src), run=_run, detect=_detect)
    assert result["ok"] is True
    assert result["reason"] == "rewritten"
    assert src.read_bytes() == b"REWRITTEN"


def test_local_already_front_does_not_call_ffmpeg(tmp_path):
    src = tmp_path / "movie.mp4"
    src.write_bytes(b"ORIGINAL-BYTES")
    calls = []

    def _run(cmd, **kwargs):
        calls.append(cmd)
        return type("P", (), {"returncode": 0, "stderr": b""})()

    result = fs.rewrite_local(str(src), run=_run, detect=lambda path: "front")
    assert result["reason"] == "already_front"
    assert calls == []
    assert src.read_bytes() == b"ORIGINAL-BYTES"


def test_output_not_front_keeps_original(tmp_path):
    src = tmp_path / "movie.mp4"
    src.write_bytes(b"ORIGINAL-BYTES")

    def _run(cmd, **kwargs):
        with open(cmd[-1], "wb") as out:
            out.write(b"NOT-FRONT")
        return type("P", (), {"returncode": 0, "stderr": b""})()

    result = fs.rewrite_local(str(src), run=_run, detect=lambda path: "back")
    assert result["ok"] is False
    assert src.read_bytes() == b"ORIGINAL-BYTES"


def test_drive_failure_does_not_upload(tmp_path, monkeypatch):
    work = tmp_path / "job"
    work.mkdir()
    monkeypatch.setattr(fs, "_work_dir", lambda: str(work))
    calls = []

    def _download(file_id, token, dest):
        calls.append("dl")
        with open(dest, "wb") as out:
            out.write(b"src")

    def _upload(file_id, token, src):
        calls.append("up")

    result = fs.rewrite_drive_file(
        "fid-1", size_hint=20, token="tok",
        peek=lambda *args: "back",
        download=_download, upload=_upload,
        rewrite_local_fn=lambda path: {"ok": False, "reason": "ffmpeg"})
    assert result["ok"] is False
    assert result["reason"] == "ffmpeg"
    assert calls == ["dl"]


def test_drive_already_front_skips_download():
    calls = []
    result = fs.rewrite_drive_file(
        "fid-1", size_hint=20, token="tok",
        peek=lambda *args: "front",
        download=lambda *args: calls.append("dl"),
        upload=lambda *args: calls.append("up"))
    assert result["reason"] == "already_front"
    assert calls == []


def test_drive_low_disk_skips_download(tmp_path, monkeypatch):
    work = tmp_path / "job"
    work.mkdir()
    monkeypatch.setattr(fs, "_work_dir", lambda: str(work))
    monkeypatch.setattr(fs, "_enough_disk", lambda directory, need: False)
    calls = []
    result = fs.rewrite_drive_file(
        "fid-1", size_hint=20, token="tok",
        peek=lambda *args: "back",
        download=lambda *args: calls.append("dl"),
        upload=lambda *args: calls.append("up"))
    assert result["reason"] == "low_disk"
    assert calls == []


def test_no_token_keeps_remote(monkeypatch):
    monkeypatch.setattr(
        "backend.emby_server.drive_auth.get_drive_bearer_token",
        lambda *args, **kwargs: None)
    result = fs.rewrite_drive_file("fid-1", size_hint=20)
    assert result == {"ok": False, "reason": "no_token"}


def test_upload_session_then_put(tmp_path):
    src = tmp_path / "out.mp4"
    src.write_bytes(b"abcdef")
    seen = []

    class _Resp:
        def __init__(self, code, headers=None):
            self.status_code = code
            self.headers = headers or {}

    def _request(method, url, **kwargs):
        seen.append((method, url, kwargs.get("headers") or {}))
        if method == "PATCH":
            return _Resp(200, {"location": "https://upload.example/session"})
        return _Resp(200)

    fs.upload_drive_replace("fid-9", "tok", str(src), request=_request)
    assert seen[0][0] == "PATCH"
    assert "fid-9" in seen[0][1]
    assert seen[1][0] == "PUT"
    assert seen[1][1] == "https://upload.example/session"
    assert seen[1][2]["Content-Range"] == "bytes 0-5/6"


def test_strm_is_not_rewritten_locally(tmp_path):
    strm = tmp_path / "movie.strm"
    strm.write_bytes(b"https://example.invalid/a")
    marked = []
    result = fs.process_job(
        {"item_id": 1, "file_path": str(strm), "container": "mp4",
         "drive_file_id": "", "size": 10},
        rewrite_local_fn=lambda path: {"ok": True, "reason": "rewritten", "size": 1},
        mark_fn=lambda item_id, size: marked.append(item_id))
    assert result["reason"] == "unsupported_location"
    assert marked == []
    assert strm.read_bytes().startswith(b"https://")


def test_process_failure_does_not_mark(tmp_path):
    src = tmp_path / "movie.mp4"
    src.write_bytes(b"ORIGINAL")
    marked = []
    result = fs.process_job(
        {"item_id": 4, "file_path": str(src), "container": "mp4",
         "drive_file_id": "fid", "size": 8},
        rewrite_local_fn=lambda path: {"ok": False, "reason": "ffmpeg"},
        mark_fn=lambda item_id, size: marked.append((item_id, size)))
    assert result["ok"] is False
    assert marked == []
    assert src.read_bytes() == b"ORIGINAL"


@pytest.fixture()
def db():
    # emby_user_media_data 外键指向 web_users，建表前要先登记那张表。
    import backend.models  # noqa: F401

    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    lib = em.Library(guid="glib", name="电影", collection_type="movies",
                     paths="", storage_backends="")
    session.add(lib)
    session.commit()
    yield session, lib
    session.close()


def _item(lib, **kwargs):
    guid = kwargs.pop("guid")
    base = dict(
        guid=guid, library_id=lib.id, item_type="movie",
        name=guid, file_path="/data/x.mp4", container="mp4",
        size=10, moov_position=None, drive_file_id="")
    base.update(kwargs)
    return em.MediaItem(**base)


def test_backfill_selection_and_default_does_not_rewrite(db, monkeypatch):
    session, lib = db
    session.add(_item(lib, guid="front", size=9_000, moov_position="front"))
    session.add(_item(lib, guid="back-small", size=10, moov_position="back",
                      file_path="/data/small.mp4"))
    session.add(_item(lib, guid="large", size=5_000, moov_position=None,
                      file_path="mount://1/big.mp4", drive_file_id="D1"))
    session.add(_item(lib, guid="small", size=10, moov_position=None))
    session.add(_item(lib, guid="mkv", size=9_000, container="mkv",
                      file_path="/data/a.mkv"))
    session.commit()
    monkeypatch.setenv(fs.ENV_MIN_BYTES, "1000")
    jobs = fs.select_backfill_jobs(session)
    reasons = {job["reason"] for job in jobs}
    paths = {job["file_path"] for job in jobs}
    assert "/data/small.mp4" in paths
    assert "mount://1/big.mp4" in paths
    assert "/data/x.mp4" not in paths  # front 与未达阈值的 small 都不进
    assert "/data/a.mkv" not in paths
    assert reasons <= {"moov_back", "large"}

    called = []
    summary = fs.run_backfill(
        session, apply=True, process=lambda job: called.append(job) or {"ok": False, "reason": "no"})
    assert summary["ran"] is False
    assert summary["reason"] == "backfill_disabled"
    assert called == []

    monkeypatch.setenv(fs.ENV_BACKFILL, "1")
    dry = fs.run_backfill(session, apply=False, process=lambda job: called.append(job))
    assert dry["ran"] is False
    assert dry["reason"] == "dry_run"
    assert called == []
    assert dry["candidates"] == 2

    applied = fs.run_backfill(
        session, apply=True,
        process=lambda job: {"ok": True, "reason": "rewritten", "size": 1})
    assert applied["ran"] is True
    assert applied["rewritten"] == 2
    assert fs.backfill_will_rewrite(apply=False) == (False, "dry_run")
    assert fs.backfill_will_rewrite(apply=True) == (True, "apply")


def test_script_disabled_exits_without_touching_db(monkeypatch):
    monkeypatch.delenv(fs.ENV_BACKFILL, raising=False)
    path = os.path.join(os.path.dirname(__file__), "..", "scripts", "mp4_faststart_backfill.py")
    spec = importlib.util.spec_from_file_location("mp4_faststart_backfill", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    assert mod.main([]) == 0


def test_scanner_only_enqueues_new_items():
    import inspect

    from backend.emby_server.scanner import _scan_library_body

    text = inspect.getsource(_scan_library_body)
    idx = text.index("note_new_ingest")
    # drive-share 注释夹在 commit 和入队之间，窗口要盖住这段说明。
    window = text[max(0, idx - 2500):idx]
    assert "if is_new:" in window
    assert window.rfind("db.commit()") >= 0
