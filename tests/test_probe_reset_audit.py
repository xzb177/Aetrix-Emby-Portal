# -*- coding: utf-8 -*-
"""probe reset 审计日志测试。

覆盖：
1. ACTION_TABLE 给 /scrape/probe/reset 语义动作名（不再回落成无语义的 emby_admin_write）
2. 写操作审计中间件把 request.state.audit_extra 并进操作日志 details
3. reset_probe_queue 端点在成功后把 scope 与影响行数挂到 audit_extra
"""
import os

os.environ.setdefault("DATABASE_TYPE", "sqlite")
os.environ.setdefault("REDIS_ENABLED", "false")

import asyncio
import uuid
from datetime import datetime
from types import SimpleNamespace
from unittest import mock

import pytest
from starlette.requests import Request

from backend.database import SessionLocal, init_db
from backend.emby_server import audit, models as em, probe_worker

init_db()


def _guid():
    return uuid.uuid4().hex


@pytest.fixture()
def db():
    s = SessionLocal()
    try:
        yield s
    finally:
        s.close()


def _make_failed_item(db, n=3):
    lib = em.Library(guid=_guid(), name=f"audit_{_guid()[:8]}",
                     collection_type="movies", paths="/tmp/audit_test")
    db.add(lib)
    db.commit()
    db.refresh(lib)
    for _ in range(n):
        db.add(em.MediaItem(
            guid=_guid(), library_id=lib.id, name=f"f_{_guid()[:8]}",
            item_type="movie", file_path="/tmp/audit_test/a.mkv",
            probe_status="failed", probe_attempts=3,
            probe_last_error="boom", date_added=datetime.now(),
        ))
    db.commit()
    return lib


def _cleanup(db, lib):
    ids = [r[0] for r in db.query(em.MediaItem.id)
           .filter(em.MediaItem.library_id == lib.id).all()]
    if ids:
        db.query(em.MediaItem).filter(em.MediaItem.id.in_(ids)) \
            .delete(synchronize_session=False)
    db.query(em.Library).filter(em.Library.id == lib.id).delete()
    db.commit()


class TestActionTable:
    def test_reset_has_semantic_action(self):
        action, target = audit.lookup("POST", "/api/admin/emby/scrape/probe/reset")
        assert action == "emby_probe_queue_reset"
        assert target == "probe"

    def test_lookup_still_falls_back_for_unknown(self):
        action, _ = audit.lookup("POST", "/api/admin/emby/scrape/some-new-thing")
        assert action == audit.FALLBACK_ACTION


class TestAuditExtraMerge:
    def _run_middleware(self, monkeypatch, scope_state_extra):
        async def fake_app(scope, receive, send):
            if scope_state_extra is not None:
                scope.setdefault("state", {})["audit_extra"] = scope_state_extra
            await send({"type": "http.response.start", "status": 200, "headers": []})
            await send({"type": "http.response.body", "body": b"{}"})

        captured = {}

        def fake_record_write(action, target_type, path, details):
            captured["action"] = action
            captured["details"] = details

        fake_user = SimpleNamespace(id=42)
        monkeypatch.setattr(audit, "_admin_from_token", lambda db, headers: fake_user)
        monkeypatch.setattr(audit, "record_write", fake_record_write)

        scope = {
            "type": "http",
            "method": "POST",
            "path": "/api/admin/emby/scrape/probe/reset",
            "headers": [],
            "client": ("127.0.0.1", 5000),
            "server": ("test", 80),
            "scheme": "http",
            "query_string": b"",
            "state": {},
        }
        mw = audit.AdminWriteAuditMiddleware(fake_app)
        asyncio.run(mw(scope, None, lambda msg: asyncio.sleep(0)))
        return captured

    def test_extra_merged_into_details(self, monkeypatch):
        captured = self._run_middleware(monkeypatch, {
            "scope": "failed",
            "reset": {"stuck": 0, "failed": 3, "retrying": 0},
            "reset_total": 3,
        })
        assert captured["action"] == "emby_probe_queue_reset"
        d = captured["details"]
        # 中间件的权威字段
        assert d["method"] == "POST"
        assert d["path"] == "/scrape/probe/reset"
        assert d["admin_user_id"] == 42
        assert d["client_ip"] == "127.0.0.1"
        # 端点富化的业务细节
        assert d["scope"] == "failed"
        assert d["reset"] == {"stuck": 0, "failed": 3, "retrying": 0}
        assert d["reset_total"] == 3

    def test_no_extra_still_audits(self, monkeypatch):
        captured = self._run_middleware(monkeypatch, None)
        assert captured["action"] == "emby_probe_queue_reset"
        assert "scope" not in captured["details"]

    def test_reserved_keys_cannot_be_overridden(self, monkeypatch):
        captured = self._run_middleware(monkeypatch, {
            "method": "GET", "path": "/evil", "admin_user_id": 999,
            "client_ip": "9.9.9.9", "scope": "all",
        })
        d = captured["details"]
        assert d["method"] == "POST"
        assert d["path"] == "/scrape/probe/reset"
        assert d["admin_user_id"] == 42
        assert d["client_ip"] == "127.0.0.1"
        assert d["scope"] == "all"


class TestResetEndpointSetsAuditExtra:
    def test_endpoint_attaches_scope_and_counts(self, db):
        from backend.api.admin_scrape import ProbeResetRequest, reset_probe_queue

        lib = _make_failed_item(db, n=3)
        try:
            scope = {
                "type": "http", "method": "POST", "path": "/x", "headers": [],
                "query_string": b"", "server": ("t", 80), "scheme": "http",
                "client": ("127.0.0.1", 1), "state": {},
            }
            request = Request(scope)
            staff = SimpleNamespace(id=7)
            out = reset_probe_queue(ProbeResetRequest(scope="failed"), request, staff, db)
            assert out["success"] is True
            assert out["reset"]["failed"] == 3
            extra = request.scope["state"]["audit_extra"]
            assert extra["scope"] == "failed"
            assert extra["reset"]["failed"] == 3
            assert extra["reset_total"] == 3
            # 失败条目确实被放回 pending
            assert db.query(em.MediaItem).filter(
                em.MediaItem.library_id == lib.id,
                em.MediaItem.probe_status == "failed").count() == 0
        finally:
            _cleanup(db, lib)

    def test_invalid_scope_raises_before_audit(self, db):
        from backend.api.admin_scrape import ProbeResetRequest, reset_probe_queue
        from fastapi import HTTPException

        scope = {
            "type": "http", "method": "POST", "path": "/x", "headers": [],
            "query_string": b"", "server": ("t", 80), "scheme": "http",
            "client": ("127.0.0.1", 1), "state": {},
        }
        request = Request(scope)
        staff = SimpleNamespace(id=7)
        # pydantic 先拦非法值；直接调 worker 的 ValueError 路径也要保证不记审计
        with mock.patch.object(probe_worker, "reset",
                               side_effect=ValueError("未知 scope: xx")):
            with pytest.raises(HTTPException):
                reset_probe_queue(ProbeResetRequest(scope="stuck"), request, staff, db)
        assert "audit_extra" not in request.scope["state"]
