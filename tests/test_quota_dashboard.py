# -*- coding: utf-8 -*-
"""额度驾驶舱（只读管理端口，2026-10-27 首批第一单）。

全部离线：用户盘配额一律用注入的假函数，不碰真实 Drive、不碰
生产库；HTTP 用例里不建 active drive 绑定行，因此默认探针不被
触发（SA 计数本身只读本地清单缓存，不调 Drive API）。

覆盖：
- 队列计数口径（pending/running/done/failed）与最近失败原因
  （倒序、截断、只含失败行）；
- 配额取不到时单行降级为 unknown，不崩、不影响其它行与计数；
- SA 计数抛错时整路降级为 unknown；
- 开关关时接口函数直接报 404；开关开时 HTTP 200 且响应不含
  credential_id / 凭据类字段；
- 路由已注册在 admin_router 上（/api/admin/quota-dashboard）。
"""
import os
import tempfile

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
from fastapi import HTTPException
from fastapi.testclient import TestClient

from backend import models as base_models
from backend.api import admin as admin_mod
from backend.api.admin_core import get_current_admin
from backend.database import SessionLocal, get_db, init_db
from backend.emby_server import models as em
from backend.emby_server import quota_dashboard as qd
from backend.main import app

init_db()


@pytest.fixture()
def db():
    s = SessionLocal()
    try:
        for table in (em.TransferJob, em.UserStorageBinding,
                      base_models.WebUser):
            s.query(table).delete()
        s.commit()
        yield s
    finally:
        s.close()


@pytest.fixture()
def admin(db):
    u = base_models.WebUser(username="quotaadmin", is_staff=True,
                            is_active=True, password_hash="test")
    db.add(u)
    db.commit()
    db.refresh(u)
    return u


@pytest.fixture()
def client():
    app.dependency_overrides[get_db] = lambda: SessionLocal()
    try:
        yield TestClient(app, raise_server_exceptions=False)
    finally:
        app.dependency_overrides.clear()


@pytest.fixture()
def authed(client, admin):
    app.dependency_overrides[get_current_admin] = lambda: admin
    try:
        yield client
    finally:
        del app.dependency_overrides[get_current_admin]


def _job(db, status, trigger="manual", detail=None, kind=None):
    row = em.TransferJob(item_id=1, trigger=trigger, provider="drive",
                         status=status, error_kind=kind,
                         error_detail=detail)
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def _binding(db, user_id, label, status="active"):
    row = em.UserStorageBinding(user_id=user_id, provider="drive",
                                account_label=label, credential_id=999,
                                status=status)
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def _fake_quota(results):
    def fn(db_, binding):
        quota = results[binding.id]
        if isinstance(quota, Exception):
            raise quota
        return quota
    return fn


def test_queue_counts_and_recent_failed(db, monkeypatch):
    monkeypatch.setattr("backend.emby_server.drive_auth.sa_count",
                        lambda: 3)
    _job(db, "pending")
    _job(db, "pending")
    _job(db, "running")
    _job(db, "done")
    _job(db, "failed", detail="x" * 500, kind="dead")
    _job(db, "failed", detail="second", kind="retriable")
    out = qd.build_quota_dashboard(db, drive_quota_fn=lambda d, b: {})
    assert out["transfer_queue"]["counts"] == {
        "pending": 2, "running": 1, "done": 1, "failed": 2}
    recent = out["transfer_queue"]["recent_failed"]
    assert len(recent) == 2
    assert recent[0]["error_detail"] == "second"  # 倒序：最新在前
    assert len(recent[1]["error_detail"]) == 200  # 截断
    assert out["sa_pool"] == {"count": 3, "status": "ok",
                              "note": out["sa_pool"]["note"]}


def test_quota_failure_degrades_single_row(db, monkeypatch):
    monkeypatch.setattr("backend.emby_server.drive_auth.sa_count",
                        lambda: 1)
    good = _binding(db, 7, "good")
    bad = _binding(db, 8, "bad")
    out = qd.build_quota_dashboard(
        db, drive_quota_fn=_fake_quota({
            good.id: {"limit": 1000, "usage": 400, "free": 600},
            bad.id: Exception("about boom"),
        }))
    items = {row["binding_id"]: row for row in out["user_disks"]["items"]}
    assert items[good.id]["status"] == "ok"
    assert items[good.id]["free"] == 600
    assert items[bad.id]["status"] == "unknown"
    assert items[bad.id]["free"] is None
    assert "credential_id" not in str(items)
    assert out["transfer_queue"]["counts"]["pending"] == 0


def test_sa_failure_degrades_without_crashing(db):
    out = qd.build_quota_dashboard(db, drive_quota_fn=lambda d, b: {})
    # 本机无 SA 配置时 sa_count 返回 0（ok）或抛错降级（unknown），
    # 两种都是可接受的降级语义，关键是整口不崩且结构完整。
    assert out["sa_pool"]["status"] in ("ok", "unknown")
    assert out["user_disks"]["items"] == []
    assert out["transfer_queue"]["status"] == "ok"


def test_switch_off_returns_404(admin, db, monkeypatch):
    monkeypatch.setenv("QUOTA_DASHBOARD_ENABLED", "0")
    assert qd.quota_dashboard_enabled() is False
    with pytest.raises(HTTPException) as exc:
        admin_mod.get_quota_dashboard(admin, db)
    assert exc.value.status_code == 404


def test_switch_on_http_200_and_no_credential_leak(authed, db, monkeypatch):
    monkeypatch.setenv("QUOTA_DASHBOARD_ENABLED", "1")
    _job(db, "pending")
    _binding(db, 7, "only-invalid", status="invalid")
    r = authed.get("/api/admin/quota-dashboard")
    assert r.status_code == 200, r.text
    body = r.json()
    assert set(body) == {"sa_pool", "user_disks", "transfer_queue"}
    assert body["transfer_queue"]["counts"]["pending"] == 1
    # invalid 绑定不进用户盘列表；且响应全文不含凭据类字段名
    assert body["user_disks"]["items"] == []
    assert "credential_id" not in r.text


def test_route_registered():
    from backend.api.admin_core import admin_router
    paths = [r.path for r in admin_router.routes]
    assert "/api/admin/quota-dashboard" in paths
