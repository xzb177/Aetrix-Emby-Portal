"""r54 横切复查回归：WebSocket 鉴权两处补 token 新鲜度检查。

F1（中·安全）：``backend/websocket.py::require_staff_user`` 曾是第二套手写
管理鉴权（纯验签，不查 jti 吊销 / token_version）——改密/登出后旧 access
token 在 120 分钟有效期内仍能调 ``POST /api/notifications/send``、
``POST /api/notifications/broadcast``（全站广播）、
``GET /api/notifications/online-users``。违反
``security.enforce_token_freshness``「所有 JWT 鉴权依赖都必须调这里」的
横切纪律。已改为直接复用 ``admin_core.get_current_admin``。

F2（低·安全）：``authenticate_ws_token``（``/ws/{user_id}`` 建连鉴权）
同样纯验签，旧 token 登出/改密后仍可建连收推送。已补新鲜度检查
（fail-closed：enforce 抛 HTTPException 时转 None）。
"""
import os

os.environ.setdefault("DATABASE_TYPE", "sqlite")
os.environ.setdefault("REDIS_ENABLED", "false")
os.environ.setdefault("SECRET_KEY", "test-secret-r54-ws-auth")

import pytest
from fastapi import HTTPException, Request
from fastapi.security import HTTPAuthorizationCredentials

from backend import models, security
from backend.database import SessionLocal, init_db
from backend.websocket import authenticate_ws_token, require_staff_user

init_db()


def _make_admin(username: str, role: str = "super") -> int:
    db = SessionLocal()
    try:
        old = db.query(models.WebUser).filter(models.WebUser.username == username).first()
        if old:
            db.query(models.WebUser).filter(models.WebUser.id == old.id).delete()
            db.commit()
        u = models.WebUser(
            username=username,
            password_hash="x",
            is_active=True,
            is_staff=True,
            admin_role=role,
            token_version=0,
        )
        db.add(u)
        db.commit()
        return u.id
    finally:
        db.close()


def _req(path: str = "/api/notifications/broadcast", method: str = "POST") -> Request:
    return Request(
        {
            "type": "http",
            "http_version": "1.1",
            "method": method,
            "scheme": "http",
            "path": path,
            "raw_path": path.encode(),
            "query_string": b"",
            "headers": [],
            "server": ("testserver", 80),
        }
    )


def _creds(token: str) -> HTTPAuthorizationCredentials:
    return HTTPAuthorizationCredentials(scheme="Bearer", credentials=token)


# ---------- F1: require_staff_user ----------

def test_require_staff_user_valid_admin_ok():
    """正常管理员 token 放行（super 角色避开角色拦截）。"""
    uid = _make_admin("r54_ws_admin_ok")
    token = security.create_access_token(uid)
    db = SessionLocal()
    try:
        got = require_staff_user(_req(), _creds(token), db)
        assert got.id == uid
    finally:
        db.close()


def test_require_staff_user_revoked_jti_rejected():
    """登出吊销 jti 后，旧 token 必须 401（修前：纯验签放行）。"""
    uid = _make_admin("r54_ws_admin_revoke")
    token = security.create_access_token(uid)
    payload = security.decode_token(token, expected_type="access")
    db = SessionLocal()
    try:
        security.revoke_jti(db, payload["jti"], uid, reason="logout")
        with pytest.raises(HTTPException) as exc:
            require_staff_user(_req(), _creds(token), db)
        assert exc.value.status_code == 401
    finally:
        db.close()


def test_require_staff_user_stale_token_version_rejected():
    """改密（token_version+1）后旧 token 必须 401（修前：放行）。"""
    uid = _make_admin("r54_ws_admin_tv")
    token = security.create_access_token(uid)
    db = SessionLocal()
    try:
        user = db.query(models.WebUser).filter(models.WebUser.id == uid).first()
        user.token_version = 1
        db.commit()
        with pytest.raises(HTTPException) as exc:
            require_staff_user(_req(), _creds(token), db)
        assert exc.value.status_code == 401
    finally:
        db.close()


def test_require_staff_user_no_credentials_401():
    db = SessionLocal()
    try:
        with pytest.raises(HTTPException) as exc:
            require_staff_user(_req(), None, db)
        assert exc.value.status_code == 401
    finally:
        db.close()


def test_require_staff_user_non_staff_forbidden():
    """普通用户（非管理员）仍 403。"""
    db = SessionLocal()
    try:
        old = db.query(models.WebUser).filter(models.WebUser.username == "r54_ws_plain").first()
        if old:
            db.query(models.WebUser).filter(models.WebUser.id == old.id).delete()
            db.commit()
        u = models.WebUser(username="r54_ws_plain", password_hash="x",
                           is_active=True, is_staff=False, token_version=0)
        db.add(u)
        db.commit()
        uid = u.id
        token = security.create_access_token(uid)
        with pytest.raises(HTTPException) as exc:
            require_staff_user(_req(), _creds(token), db)
        assert exc.value.status_code == 403
    finally:
        db.close()


# ---------- F2: authenticate_ws_token ----------

def test_authenticate_ws_token_valid():
    uid = _make_admin("r54_ws_conn_ok")
    token = security.create_access_token(uid)
    assert authenticate_ws_token(token) == uid


def test_authenticate_ws_token_revoked_returns_none():
    """吊销后建连即拒（修前：返回 user_id）。"""
    uid = _make_admin("r54_ws_conn_revoke")
    token = security.create_access_token(uid)
    payload = security.decode_token(token, expected_type="access")
    db = SessionLocal()
    try:
        security.revoke_jti(db, payload["jti"], uid, reason="logout")
    finally:
        db.close()
    assert authenticate_ws_token(token) is None


def test_authenticate_ws_token_stale_tv_returns_none():
    uid = _make_admin("r54_ws_conn_tv")
    token = security.create_access_token(uid)
    db = SessionLocal()
    try:
        user = db.query(models.WebUser).filter(models.WebUser.id == uid).first()
        user.token_version = 1
        db.commit()
    finally:
        db.close()
    assert authenticate_ws_token(token) is None


def test_authenticate_ws_token_garbage_returns_none():
    assert authenticate_ws_token("") is None
    assert authenticate_ws_token(None) is None
    assert authenticate_ws_token("not-a-jwt") is None
