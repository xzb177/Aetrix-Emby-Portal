# -*- coding: utf-8 -*-
"""Tests for the 2026-10-19 probe login-page fix (A+B).

背景：挂载层对一批 Drive 文件返回 ``drive.usercontent.google.com/download``
直链，该端点对这批文件不认 SA 令牌、返回 HTTP 200 的 Google 登录页 HTML；
ffprobe 读 HTML 拿不到 format，3 次尝试后判 terminal failed（9,493 个冤判）。

A：drive_api_media_url 把 usercontent/download?id=<fid> 转成 alt=media
   （带 supportsAllDrives=true）；legacy uc 输出保持字节一致。
B：scanner.probe_metadata 预检 Content-Type，text/html → _error="login_page"；
   probe_one / probe_worker.process_item 一律走 24h 长退避，永不判死、
   不消耗普通重试次数。
"""
import os

os.environ.setdefault("DATABASE_TYPE", "sqlite")
os.environ.setdefault("REDIS_ENABLED", "false")

import uuid
from datetime import datetime, timedelta
from unittest import mock

from backend.database import SessionLocal, init_db
from backend.emby_server import models as em
from backend.emby_server import media_probe, probe_worker
from backend.emby_server import scanner
from backend.emby_server.drive_auth import drive_api_media_url

init_db()


def _guid():
    return uuid.uuid4().hex


def _make_lib(db, name=None):
    lib = em.Library(guid=_guid(), name=name or f"loginpage_{_guid()[:8]}",
                     collection_type="movies", paths="/tmp/loginpage_test")
    db.add(lib)
    db.commit()
    db.refresh(lib)
    return lib


def _make_item(db, lib, probe_status="pending", probe_attempts=0,
               file_path="/tmp/loginpage_test/a.mkv"):
    item = em.MediaItem(
        guid=_guid(), library_id=lib.id, name=f"item_{_guid()[:8]}",
        item_type="movie", file_path=file_path,
        probe_status=probe_status, probe_attempts=probe_attempts,
        date_added=datetime.now(),
    )
    db.add(item)
    db.commit()
    db.refresh(item)
    return item


def _cleanup(db, lib):
    ids = [r[0] for r in db.query(em.MediaItem.id)
           .filter(em.MediaItem.library_id == lib.id).all()]
    if ids:
        db.query(em.MediaStream).filter(em.MediaStream.item_id.in_(ids)) \
            .delete(synchronize_session=False)
    db.query(em.MediaItem).filter(em.MediaItem.library_id == lib.id) \
        .delete(synchronize_session=False)
    db.query(em.Library).filter(em.Library.id == lib.id).delete()
    db.commit()


def _cleanup_by_id(db, lib_id):
    ids = [r[0] for r in db.query(em.MediaItem.id)
           .filter(em.MediaItem.library_id == lib_id).all()]
    if ids:
        db.query(em.MediaStream).filter(em.MediaStream.item_id.in_(ids)) \
            .delete(synchronize_session=False)
    db.query(em.MediaItem).filter(em.MediaItem.library_id == lib_id) \
        .delete(synchronize_session=False)
    db.query(em.Library).filter(em.Library.id == lib_id).delete()
    db.commit()


class TestUsercontentConversion:
    """A：usercontent 新域名转换。"""

    def test_usercontent_basic_converts(self):
        url = ("https://drive.usercontent.google.com/download"
               "?id=ABC123&export=download&confirm=t")
        assert drive_api_media_url(url) == (
            "https://www.googleapis.com/drive/v3/files/ABC123"
            "?alt=media&supportsAllDrives=true")

    def test_usercontent_id_only_converts(self):
        url = "https://drive.usercontent.google.com/download?id=XYZ"
        assert drive_api_media_url(url) == (
            "https://www.googleapis.com/drive/v3/files/XYZ"
            "?alt=media&supportsAllDrives=true")

    def test_usercontent_missing_id_passthrough(self):
        url = "https://drive.usercontent.google.com/download?export=download"
        assert drive_api_media_url(url) == url

    def test_usercontent_wrong_path_passthrough(self):
        url = "https://drive.usercontent.google.com/other?id=ABC123"
        assert drive_api_media_url(url) == url

    def test_uc_legacy_output_unchanged(self):
        # 旧 uc 分支输出保持字节一致（不许回归）
        url = "https://drive.google.com/uc?export=download&id=ABC123xyz&confirm=t"
        assert drive_api_media_url(url) == (
            "https://www.googleapis.com/drive/v3/files/ABC123xyz?alt=media")

    def test_non_drive_passthrough(self):
        url = "https://cdn.example.com/video/a.mkv"
        assert drive_api_media_url(url) == url

    def test_empty_passthrough(self):
        assert drive_api_media_url("") == ""
        assert drive_api_media_url(None) is None

    def test_probe_url_converts_usercontent(self):
        url = "https://drive.usercontent.google.com/download?id=FILEID99&confirm=t"
        assert media_probe._probe_url(url) == (
            "https://www.googleapis.com/drive/v3/files/FILEID99"
            "?alt=media&supportsAllDrives=true")


class _FakeResp:
    def __init__(self, status_code=200, content_type="text/html"):
        self.status_code = status_code
        self.headers = {"content-type": content_type}

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _FakeClient:
    def __init__(self, resp=None, exc=None):
        self._resp = resp
        self._exc = exc
        self.stream_calls = []

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def stream(self, method, url, headers=None):
        self.stream_calls.append((method, url, headers))
        if self._exc is not None:
            raise self._exc
        return self._resp


def _patch_httpx(resp=None, exc=None):
    client = _FakeClient(resp=resp, exc=exc)
    return mock.patch("httpx.Client", return_value=client), client


class TestLoginPagePreflight:
    """B：预检 helper 的判定。"""

    def test_html_returns_true(self):
        p, _ = _patch_httpx(resp=_FakeResp(200, "text/html"))
        with p:
            assert scanner._remote_returns_login_page(
                "https://x/a.mkv", {"Authorization": "Bearer t"}) is True

    def test_html_with_charset_returns_true(self):
        p, _ = _patch_httpx(resp=_FakeResp(200, "text/html; charset=utf-8"))
        with p:
            assert scanner._remote_returns_login_page("https://x/a.mkv", {}) is True

    def test_video_returns_false(self):
        p, _ = _patch_httpx(resp=_FakeResp(206, "video/mp4"))
        with p:
            assert scanner._remote_returns_login_page("https://x/a.mkv", {}) is False

    def test_non_2xx_returns_false(self):
        p, _ = _patch_httpx(resp=_FakeResp(403, "text/html"))
        with p:
            # 4xx 按既有错误分类走，不归守卫管
            assert scanner._remote_returns_login_page("https://x/a.mkv", {}) is False

    def test_exception_returns_false(self):
        p, _ = _patch_httpx(exc=RuntimeError("boom"))
        with p:
            assert scanner._remote_returns_login_page("https://x/a.mkv", {}) is False

    def test_local_path_returns_false_without_request(self):
        p, client = _patch_httpx(resp=_FakeResp(200, "text/html"))
        with p:
            assert scanner._remote_returns_login_page("/mnt/a.mkv", {}) is False
            assert client.stream_calls == []


class TestProbeMetadataLoginGuard:
    """B：probe_metadata 命中登录页时短路，不喂 ffprobe。"""

    def test_login_page_short_circuits(self):
        with mock.patch.object(scanner, "_remote_returns_login_page",
                               return_value=True), \
             mock.patch.object(scanner, "_ffprobe") as ff:
            out = scanner.probe_metadata("https://x/a.mkv",
                                         {"Authorization": "Bearer t"},
                                         size=10**9, container="mkv")
        assert out["_error"] == "login_page"
        assert out["_http_code"] == 200
        assert "登录页" in out["_error_detail"]
        ff.assert_not_called()

    def test_normal_proceeds(self):
        with mock.patch.object(scanner, "_remote_returns_login_page",
                               return_value=False), \
             mock.patch.object(scanner, "_ffprobe",
                               return_value={"format": {"duration": "60.0"}}):
            out = scanner.probe_metadata("https://x/a.mkv", {}, size=100,
                                         container="mkv")
        assert out.get("_error") != "login_page"


class TestProbeOneLoginPage:
    """B：probe_one 遇到登录页 → 长退避，不烧尝试次数。"""

    def test_long_retry_without_attempt_burn(self):
        db = SessionLocal()
        lib = _make_lib(db)
        lib_id = lib.id
        try:
            item = _make_item(db, lib, probe_status="pending", probe_attempts=0)
            item_id = item.id
            db.close()
            with mock.patch.object(media_probe, "resolve_probe_input",
                                   return_value=(("https://x/a.mkv",
                                                  {"Authorization": "Bearer t"},
                                                  1000, "mkv"), None)), \
                 mock.patch.object(media_probe, "probe_metadata",
                                   return_value={"_error": "login_page",
                                                 "_http_code": 200,
                                                 "_error_detail": "登录页"}), \
                 mock.patch.object(media_probe.persist_lib, "deserialize",
                                   return_value=False):
                for _ in range(3):
                    sdb = SessionLocal()
                    it = sdb.query(em.MediaItem).get(item_id)
                    assert media_probe.probe_one(sdb, it) == "pending"
                    sdb.close()
            db = SessionLocal()
            it = db.query(em.MediaItem).get(item_id)
            assert it.probe_status == "pending"
            assert it.probe_attempts == 0  # 3 次都没烧
            assert it.probe_next_retry_at is not None
            assert it.probe_next_retry_at > datetime.now() + timedelta(hours=12)
            assert "[auth]" in (it.probe_last_error or "")
            db.close()
        finally:
            db = SessionLocal()
            _cleanup_by_id(db, lib_id)
            db.close()


class TestProcessItemLoginPage:
    """B：worker 主链路遇到登录页 → retry（长退避），永不判死。"""

    def test_process_item_login_page_is_retry_not_failed(self):
        db = SessionLocal()
        lib = _make_lib(db)
        lib_id = lib.id
        try:
            item = _make_item(db, lib, probe_status="probing")
            item.probe_claimed_at = datetime.now()
            item.file_path = "mount://8/m/x.mkv"
            db.commit()
            item_id = item.id
            db.close()

            with mock.patch.object(media_probe, "resolve_probe_input",
                                   return_value=(("http://x/a.mkv", {}, 1, "mkv"),
                                                 None)), \
                 mock.patch.object(media_probe, "probe_metadata",
                                   return_value={"_error": "login_page",
                                                 "_http_code": 200,
                                                 "_error_detail": "登录页"}), \
                 mock.patch.object(media_probe.persist_lib, "deserialize",
                                   return_value=False):
                for _ in range(3):
                    sdb = SessionLocal()
                    it = sdb.query(em.MediaItem).get(item_id)
                    it.probe_status = "probing"
                    it.probe_claimed_at = datetime.now()
                    sdb.commit()
                    sdb.close()
                    assert probe_worker.process_item(item_id) == "retry"

            db = SessionLocal()
            it = db.query(em.MediaItem).get(item_id)
            assert it.probe_status == "pending"
            assert it.probe_attempts == 0
            assert "[auth]" in (it.probe_last_error or "")
            db.close()
        finally:
            db = SessionLocal()
            _cleanup_by_id(db, lib_id)
            db.close()
