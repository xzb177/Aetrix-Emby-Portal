"""360° 审计第二遍 R25-01：A-085（P1）改密/重置吊销 Emby 令牌；A-086（P2）WS 建连后复验。

先红后绿：本文件在旧码上应全红（A-085 为断言失败；A-086 为新 API 缺失/旧行为放行）。
"""
import os

os.environ.setdefault("DATABASE_TYPE", "sqlite")
os.environ.setdefault("REDIS_ENABLED", "false")
os.environ.setdefault("SECRET_KEY", "test-secret-a085-a086")

import asyncio
from datetime import datetime, timedelta

from fastapi import Request, WebSocketDisconnect

from backend import models, security
from backend.api.admin import reset_user_password, UserResetPasswordRequest
from backend.api.admin_auth import admin_change_password, AdminChangePasswordRequest
from backend.api.emby_portal import change_password, ChangePasswordRequest, logout
from backend.database import SessionLocal, init_db
from backend.emby_server import models as emby_models
from backend.emby_server.auth import hash_emby_token, resolve_token
from backend.websocket import manager as ws_manager

init_db()


def _req(client_ip="127.0.0.1", headers=None):
    scope = {
        "type": "http",
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": "/",
        "raw_path": b"/",
        "query_string": b"",
        "headers": headers or [],
        "client": (client_ip, 12345),
        "server": ("testserver", 80),
    }
    return Request(scope)


def _make_user(username, password="old-pass-123", is_staff=False, admin_role=None):
    db = SessionLocal()
    try:
        old = db.query(models.WebUser).filter(models.WebUser.username == username).first()
        if old:
            db.query(models.WebUser).filter(models.WebUser.id == old.id).delete()
            db.commit()
        u = models.WebUser(
            username=username,
            password_hash=security.hash_password(password),
            is_active=True,
            is_staff=is_staff,
            admin_role=admin_role,
            token_version=0,
        )
        db.add(u)
        db.commit()
        return u.id
    finally:
        db.close()


def _emby_token(uid, plain, expires):
    db = SessionLocal()
    try:
        db.query(emby_models.EmbyApiToken).filter(
            emby_models.EmbyApiToken.token == hash_emby_token(plain)).delete()
        db.commit()
        row = emby_models.EmbyApiToken(
            token=hash_emby_token(plain), user_id=uid,
            device_id="a085-dev-" + plain[-4:], is_revoked=False, expires_at=expires)
        db.add(row)
        db.commit()
        return row.id
    finally:
        db.close()


def _revoked_map(uid):
    db = SessionLocal()
    try:
        rows = db.query(emby_models.EmbyApiToken).filter(
            emby_models.EmbyApiToken.user_id == uid).all()
        return {r.id: bool(r.is_revoked) for r in rows}
    finally:
        db.close()


# ---------------- A-085 ----------------

def test_change_password_revokes_emby_tokens():
    """自助改密后，旧 Emby 令牌（含 expires_at 为空的历史令牌）必须全部吊销。"""
    uid = _make_user("a085_self")
    legacy = _emby_token(uid, "a085-self-legacy", None)
    fresh = _emby_token(uid, "a085-self-fresh", datetime.now() + timedelta(days=30))
    db = SessionLocal()
    try:
        user = db.query(models.WebUser).filter(models.WebUser.id == uid).first()
        out = change_password(
            _req(client_ip="10.85.0.1"), ChangePasswordRequest(
                old_password="old-pass-123", new_password="new-pass-456"),
            user, db)
        assert out["success"] is True
    finally:
        db.close()
    m = _revoked_map(uid)
    assert m[legacy] is True and m[fresh] is True


def test_reset_password_revokes_emby_tokens():
    """管理员重置后，该用户旧 Emby 令牌必须全部吊销。"""
    admin_id = _make_user("a085_admin", is_staff=True, admin_role="super")
    uid = _make_user("a085_target")
    t1 = _emby_token(uid, "a085-reset-1", None)
    t2 = _emby_token(uid, "a085-reset-2", datetime.now() + timedelta(days=30))
    db = SessionLocal()
    try:
        admin = db.query(models.WebUser).filter(models.WebUser.id == admin_id).first()
        out = reset_user_password(uid, UserResetPasswordRequest(new_password="reset-999"),
                                  admin, db)
        assert out["success"] is True
    finally:
        db.close()
    m = _revoked_map(uid)
    assert m[t1] is True and m[t2] is True


def test_admin_change_password_revokes_emby_tokens():
    """管理员自改密后，其名下旧 Emby 令牌必须全部吊销。"""
    admin_id = _make_user("a085_selfadmin", is_staff=True, admin_role="super")
    t1 = _emby_token(admin_id, "a085-admin-1", None)
    db = SessionLocal()
    try:
        admin = db.query(models.WebUser).filter(models.WebUser.id == admin_id).first()
        out = admin_change_password(
            _req(client_ip="10.85.0.2"), AdminChangePasswordRequest(
                old_password="old-pass-123", new_password="admin-new-1"),
            admin, db)
        assert out["success"] is True
    finally:
        db.close()
    m = _revoked_map(admin_id)
    assert m[t1] is True


def test_new_emby_token_after_change_still_works():
    """改密后新签发的 Emby 令牌不受影响（吊销只针对改密前已存在的行）。"""
    uid = _make_user("a085_after")
    db = SessionLocal()
    try:
        user = db.query(models.WebUser).filter(models.WebUser.id == uid).first()
        change_password(
            _req(client_ip="10.85.0.3"), ChangePasswordRequest(
                old_password="old-pass-123", new_password="new-pass-789"),
            user, db)
    finally:
        db.close()
    _emby_token(uid, "a085-after-new", datetime.now() + timedelta(days=30))
    req = _req(headers=[(b"x-emby-token", b"a085-after-new")])
    db = SessionLocal()
    try:
        got = resolve_token(db, req)
        assert got is not None and got[0].id == uid
    finally:
        db.close()


def test_logout_does_not_revoke_emby_tokens():
    """parent 保守裁决锁定：登出路不改——Emby 令牌登出后仍存活（只 bump JWT）。"""
    uid = _make_user("a085_logout")
    t1 = _emby_token(uid, "a085-logout-1", datetime.now() + timedelta(days=30))
    db = SessionLocal()
    try:
        user = db.query(models.WebUser).filter(models.WebUser.id == uid).first()
        out = logout(_req(client_ip="10.85.0.4"), user, db)
        assert out["success"] is True
        assert int(user.token_version or 0) == 1
    finally:
        db.close()
    m = _revoked_map(uid)
    assert m[t1] is False


# ---------------- A-086 ----------------

class _FakeWS:
    def __init__(self):
        self.sent = []
        self.closed = None
        self.headers = {}
        self.client = ("127.0.0.1", 1)

    async def send_json(self, msg):
        self.sent.append(msg)

    async def close(self, code=None):
        self.closed = code

    async def accept(self):
        pass

    async def receive_json(self):
        raise WebSocketDisconnect()


def _ws_user(username):
    uid = _make_user(username)
    token = security.create_access_token(uid, token_version=0)
    payload = security.decode_token(token, expected_type="access")
    return uid, token, payload["jti"]


def _ws_cleanup(uid, ws):
    try:
        ws_manager.disconnect(ws, uid)
    except Exception:
        pass
    ws_manager.active_connections.pop(uid, None)


def test_ws_blocked_after_token_version_bump():
    """改密（token_version+1）后，已建连的旧连接再收到私信时必须被踢、不投递。"""
    from backend.websocket import authenticate_ws_token_with_snapshot
    uid, token, jti = _ws_user("a086_bump")
    snap = authenticate_ws_token_with_snapshot(token)
    assert snap is not None and snap[0] == uid and snap[1] == jti
    ws = _FakeWS()
    try:
        asyncio.run(ws_manager.connect(ws, uid, token_jti=snap[1], token_version=snap[2]))
        db = SessionLocal()
        try:
            user = db.query(models.WebUser).filter(models.WebUser.id == uid).first()
            user.token_version = 1
            db.commit()
        finally:
            db.close()
        asyncio.run(ws_manager.send_personal({"type": "t", "message": "secret"}, uid))
        assert ws.sent == []
        assert ws.closed == 1008
    finally:
        _ws_cleanup(uid, ws)


def test_ws_blocked_after_jti_revoke():
    """登出吊销 jti 后，旧连接必须被踢、不投递。"""
    from backend.websocket import authenticate_ws_token_with_snapshot
    uid, token, jti = _ws_user("a086_revoke")
    snap = authenticate_ws_token_with_snapshot(token)
    ws = _FakeWS()
    try:
        asyncio.run(ws_manager.connect(ws, uid, token_jti=snap[1], token_version=snap[2]))
        db = SessionLocal()
        try:
            security.revoke_jti(db, jti, uid, reason="logout")
        finally:
            db.close()
        asyncio.run(ws_manager.send_personal({"type": "t", "message": "secret"}, uid))
        assert ws.sent == []
        assert ws.closed == 1008
    finally:
        _ws_cleanup(uid, ws)


def test_ws_blocked_after_deactivate():
    """停用账号后，旧连接必须被踢、不投递。"""
    from backend.websocket import authenticate_ws_token_with_snapshot
    uid, token, jti = _ws_user("a086_deact")
    snap = authenticate_ws_token_with_snapshot(token)
    ws = _FakeWS()
    try:
        asyncio.run(ws_manager.connect(ws, uid, token_jti=snap[1], token_version=snap[2]))
        db = SessionLocal()
        try:
            user = db.query(models.WebUser).filter(models.WebUser.id == uid).first()
            user.is_active = False
            db.commit()
        finally:
            db.close()
        asyncio.run(ws_manager.send_personal({"type": "t", "message": "secret"}, uid))
        assert ws.sent == []
        assert ws.closed == 1008
    finally:
        _ws_cleanup(uid, ws)


def test_ws_healthy_connection_still_delivered():
    """正常长连不受影响：快照未过期时投递照常。"""
    from backend.websocket import authenticate_ws_token_with_snapshot
    uid, token, jti = _ws_user("a086_ok")
    snap = authenticate_ws_token_with_snapshot(token)
    ws = _FakeWS()
    try:
        asyncio.run(ws_manager.connect(ws, uid, token_jti=snap[1], token_version=snap[2]))
        asyncio.run(ws_manager.send_personal({"type": "t", "message": "hi"}, uid))
        assert ws.sent == [{"type": "t", "message": "hi"}]
        assert ws.closed is None
    finally:
        _ws_cleanup(uid, ws)


def test_ws_legacy_connection_without_snapshot_still_delivered():
    """无快照的老调用方（只传 user_id）保持旧行为：健康用户照常投递。"""
    uid, _token, _jti = _ws_user("a086_legacy")
    ws = _FakeWS()
    try:
        asyncio.run(ws_manager.connect(ws, uid))
        asyncio.run(ws_manager.send_personal({"type": "t", "message": "hi"}, uid))
        assert ws.sent == [{"type": "t", "message": "hi"}]
    finally:
        _ws_cleanup(uid, ws)


def test_ws_endpoint_stores_snapshot():
    """端点接线：建连时把 token 快照存进连接，投递复验才有据可查。"""
    from backend.websocket import websocket_endpoint
    uid, token, jti = _ws_user("a086_ep")
    ws = _FakeWS()
    seen = {}

    async def _capture(msg):
        seen.update(dict(ws_manager.connection_auth))
        ws.sent.append(msg)

    ws.send_json = _capture
    try:
        asyncio.run(websocket_endpoint(ws, uid, token))
    finally:
        _ws_cleanup(uid, ws)
    assert ws in seen, "建连瞬间连接上应带有 token 快照"
    assert seen[ws][0] == jti and seen[ws][1] == 0
