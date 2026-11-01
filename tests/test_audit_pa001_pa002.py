"""前后端完美审计 R1·B1：PA-001（P1）接线员页改播放密码不吊销 Emby 令牌；
PA-002（P2）管理员停用用户不吊销令牌，恢复启用后旧会话复活。

先红后绿：本文件在旧码上应红（吊销断言失败），修复后全绿。
"""
import os
from datetime import datetime, timedelta

os.environ.setdefault("DATABASE_TYPE", "sqlite")
os.environ.setdefault("REDIS_ENABLED", "false")
os.environ.setdefault("SECRET_KEY", "test-secret-pa001-pa002")

import pytest
from fastapi import HTTPException, Request
from fastapi.security import HTTPAuthorizationCredentials

from backend import models, security
from backend.api.admin import UserUpdateRequest, update_user
from backend.api.admins_admin import UpdateAdminRequest, update_admin
from backend.api.emby_portal import get_current_user_jwt
from backend.database import SessionLocal, init_db
from backend.emby_server import models as emby_models
from backend.emby_server.api import authenticate_by_name
from backend.emby_server.auth import (
    ensure_emby_credentials,
    hash_emby_token,
    resolve_token,
)
from backend.emby_server.portal import SetPasswordRequest, set_emby_password

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


def _make_user(username, password="old-pass-123", is_staff=False,
               admin_role=None, emby_username=None):
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
            emby_username=emby_username,
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
            device_id="pa-dev-" + plain[-4:], is_revoked=False, expires_at=expires)
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


def _fresh_user(uid):
    db = SessionLocal()
    try:
        return db.query(models.WebUser).filter(models.WebUser.id == uid).first()
    finally:
        db.close()


# ---------------- PA-001 ----------------

def test_pa001_set_emby_password_revokes_emby_tokens():
    """接线员页改播放密码后，旧 Emby 令牌（含 expires_at 为空的历史令牌）必须全部吊销。"""
    uid = _make_user("pa001_main", emby_username="pa001_emby")
    db = SessionLocal()
    try:
        user = db.query(models.WebUser).filter(models.WebUser.id == uid).first()
        ensure_emby_credentials(db, user, password="old-play-123")
    finally:
        db.close()
    legacy = _emby_token(uid, "pa001-legacy", None)
    fresh = _emby_token(uid, "pa001-fresh", datetime.now() + timedelta(days=30))
    db = SessionLocal()
    try:
        user = db.query(models.WebUser).filter(models.WebUser.id == uid).first()
        out = set_emby_password(SetPasswordRequest(password="new-play-999"), user, db)
        assert out["success"] is True
    finally:
        db.close()
    m = _revoked_map(uid)
    assert m[legacy] is True and m[fresh] is True


def test_pa001_new_play_password_authenticates_and_portal_jwt_kept():
    """改播放密码后：门户 JWT 仍有效；新播放密码经 AuthenticateByName 能拿新令牌；旧 Emby 令牌已失效。"""
    uid = _make_user("pa001_login", emby_username="pa001_login_emby")
    db = SessionLocal()
    try:
        user = db.query(models.WebUser).filter(models.WebUser.id == uid).first()
        ensure_emby_credentials(db, user, password="old-play-123")
    finally:
        db.close()
    old_plain = "pa001-old-token"
    _emby_token(uid, old_plain, datetime.now() + timedelta(days=30))
    portal_token = security.create_access_token(uid, token_version=0)
    db = SessionLocal()
    try:
        user = db.query(models.WebUser).filter(models.WebUser.id == uid).first()
        set_emby_password(SetPasswordRequest(password="new-play-999"), user, db)
    finally:
        db.close()
    user = _fresh_user(uid)
    assert int(user.token_version or 0) == 0, "只改播放密码不应 bump 门户 token_version"
    # 门户 JWT 仍有效
    db = SessionLocal()
    try:
        got = get_current_user_jwt(
            _req(), HTTPAuthorizationCredentials(scheme="Bearer", credentials=portal_token), db)
        assert got.id == uid
    finally:
        db.close()
    # 旧 Emby 令牌已失效
    db = SessionLocal()
    try:
        assert resolve_token(db, _req(headers=[(b"x-emby-token", old_plain.encode())])) is None
    finally:
        db.close()
    # 新播放密码经 AuthenticateByName 能拿新令牌；旧播放密码不再通过
    db = SessionLocal()
    try:
        out = authenticate_by_name(
            _req(client_ip="10.1.0.1", headers=[(b"user-agent", b"pa-test")]),
            {"Username": "pa001_login_emby", "Pw": "new-play-999"}, db)
    finally:
        db.close()
    assert isinstance(out, dict) and out.get("AccessToken"), out
    new_token = out["AccessToken"]
    db = SessionLocal()
    try:
        got = resolve_token(db, _req(headers=[(b"x-emby-token", new_token.encode())]))
        assert got is not None and got[0].id == uid
    finally:
        db.close()
    db = SessionLocal()
    try:
        fail = authenticate_by_name(
            _req(client_ip="10.1.0.2", headers=[(b"user-agent", b"pa-test")]),
            {"Username": "pa001_login_emby", "Pw": "old-play-123"}, db)
    finally:
        db.close()
    assert getattr(fail, "status_code", None) == 401


# ---------------- PA-002 ----------------

def _deactivate_target():
    admin_id = _make_user("pa002_admin", is_staff=True, admin_role="super")
    uid = _make_user("pa002_target")
    legacy = _emby_token(uid, "pa002-legacy", None)
    fresh = _emby_token(uid, "pa002-fresh", datetime.now() + timedelta(days=30))
    portal_token = security.create_access_token(uid, token_version=0)
    db = SessionLocal()
    try:
        admin = db.query(models.WebUser).filter(models.WebUser.id == admin_id).first()
        out = update_user(uid, UserUpdateRequest(is_active=False), admin, db)
        assert out["success"] is True
    finally:
        db.close()
    return uid, admin_id, legacy, fresh, portal_token


def test_pa002_deactivate_revokes_and_bumps():
    """停用后：旧 access（403）与旧 Emby 令牌（resolve 为空）均失效，token_version+1。"""
    uid, _admin_id, legacy, fresh, portal_token = _deactivate_target()
    user = _fresh_user(uid)
    assert user.is_active is False
    assert int(user.token_version or 0) == 1
    m = _revoked_map(uid)
    assert m[legacy] is True and m[fresh] is True
    db = SessionLocal()
    try:
        with pytest.raises(HTTPException):
            get_current_user_jwt(
                _req(), HTTPAuthorizationCredentials(scheme="Bearer", credentials=portal_token), db)
        assert resolve_token(db, _req(headers=[(b"x-emby-token", b"pa002-fresh")])) is None
    finally:
        db.close()


def test_pa002_reactivate_does_not_restore_old_sessions():
    """恢复启用后：旧令牌仍死（Emby 吊销不恢复、旧 JWT 因 tv 不符 401），新登录正常。"""
    uid, admin_id, _legacy, _fresh, portal_token = _deactivate_target()
    db = SessionLocal()
    try:
        admin = db.query(models.WebUser).filter(models.WebUser.id == admin_id).first()
        out = update_user(uid, UserUpdateRequest(is_active=True), admin, db)
        assert out["success"] is True
    finally:
        db.close()
    user = _fresh_user(uid)
    assert user.is_active is True
    assert int(user.token_version or 0) == 1, "恢复启用不应再 bump，也不恢复旧会话"
    m = _revoked_map(uid)
    assert all(m.values()), "恢复启用后旧 Emby 令牌必须仍为吊销态"
    db = SessionLocal()
    try:
        with pytest.raises(HTTPException) as exc:
            get_current_user_jwt(
                _req(), HTTPAuthorizationCredentials(scheme="Bearer", credentials=portal_token), db)
        assert exc.value.status_code == 401
        new_token = security.create_access_token(uid, token_version=1)
        got = get_current_user_jwt(
            _req(), HTTPAuthorizationCredentials(scheme="Bearer", credentials=new_token), db)
        assert got.id == uid
    finally:
        db.close()


def test_pa002_update_admin_deactivate_revokes_and_bumps():
    """管理员页停用分支（update_admin）同样吊销＋bump。"""
    admin_id = _make_user("pa002_admin2", is_staff=True, admin_role="super")
    uid = _make_user("pa002_staff", is_staff=True, admin_role="viewer")
    legacy = _emby_token(uid, "pa002-adm-legacy", None)
    db = SessionLocal()
    try:
        admin = db.query(models.WebUser).filter(models.WebUser.id == admin_id).first()
        out = update_admin(uid, UpdateAdminRequest(is_active=False),
                           _req(client_ip="10.2.0.1"), admin, db)
        assert out["success"] is True
    finally:
        db.close()
    user = _fresh_user(uid)
    assert user.is_active is False
    assert int(user.token_version or 0) == 1
    assert _revoked_map(uid)[legacy] is True


def test_pa002_super_guard_unaffected():
    """超级管理员保护计数不受影响：自己停用仍 400；停用普通管理员（viewer）照常通过。"""
    admin_id = _make_user("pa002_guard_admin", is_staff=True, admin_role="super")
    db = SessionLocal()
    try:
        admin = db.query(models.WebUser).filter(models.WebUser.id == admin_id).first()
        with pytest.raises(HTTPException) as exc:
            update_user(admin_id, UserUpdateRequest(is_active=False), admin, db)
        assert exc.value.status_code == 400
    finally:
        db.close()
    viewer_id = _make_user("pa002_viewer", is_staff=True, admin_role="viewer")
    db = SessionLocal()
    try:
        admin = db.query(models.WebUser).filter(models.WebUser.id == admin_id).first()
        out = update_user(viewer_id, UserUpdateRequest(is_active=False), admin, db)
        assert out["success"] is True
    finally:
        db.close()
    assert _fresh_user(viewer_id).is_active is False
