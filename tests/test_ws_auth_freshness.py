"""r47：WebSocket 通知鉴权的新鲜度一致性回归测试

横切纪律：所有 JWT 鉴权点必须调 security.enforce_token_freshness，
不许出现"改密/登出后部分接口仍认旧 token"。
- F1：require_staff_user（/api/notifications/send、/broadcast、/online-users）
  此前只验签，管理员改密/登出后旧 token 仍可向全站广播。
- F2：authenticate_ws_token 建连时不校验新鲜度，旧 token 可建新连接收推送。
- F3：economy.py 删掉未使用的 resolve_jwt_user_id 导入（AST 静态断言）。

全部离线：内存 SQLite + 直接调依赖函数，不发起真实网络请求。
"""
from __future__ import annotations

import ast
import pathlib

import pytest
from fastapi import HTTPException
from fastapi.security import HTTPAuthorizationCredentials
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from starlette.requests import Request

from backend import models
from backend.security import create_access_token, decode_token, revoke_jti
from backend.websocket import authenticate_ws_token, require_staff_user

ROOT = pathlib.Path(__file__).resolve().parents[1]


@pytest.fixture()
def db():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False},
                           poolclass=StaticPool)
    models.Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    yield session
    session.close()


def _user(db, username, *, is_staff=False, role=None):
    u = models.WebUser(username=username, password_hash="x", is_staff=is_staff,
                       admin_role=role, is_active=True, token_version=0)
    db.add(u)
    db.commit()
    return u


def _token(user, token_version=0):
    return create_access_token(user.id, {"username": user.username},
                               token_version=token_version)


def _request(path="/api/notifications/send", method="POST"):
    scope = {
        "type": "http", "method": method, "path": path, "query_string": b"",
        "scheme": "http", "server": ("test", 80),
        "headers": [], "client": ("1.2.3.4", 1), "path_params": {},
    }
    return Request(scope)


def _creds(token):
    return HTTPAuthorizationCredentials(scheme="Bearer", credentials=token)


def _bump_token_version(db, user):
    """模拟改密/登出：token_version +1 使旧 token 的 tv 对不上"""
    user.token_version = (user.token_version or 0) + 1
    db.commit()


# ===========================================================================
# F1：require_staff_user 新鲜度
# ===========================================================================

def test_require_staff_user_accepts_fresh_admin(db):
    admin = _user(db, "boss", is_staff=True, role="super")
    got = require_staff_user(_request(), _creds(_token(admin)), db)
    assert got.id == admin.id


def test_require_staff_user_rejects_stale_after_password_change(db):
    """改密（token_version bump）后，旧 token 调发送/广播接口必须 401"""
    admin = _user(db, "boss", is_staff=True, role="super")
    stale = _token(admin)  # tv=0 签发
    _bump_token_version(db, admin)  # 改密 → tv=1
    with pytest.raises(HTTPException) as ei:
        require_staff_user(_request(), _creds(stale), db)
    assert ei.value.status_code == 401
    assert "密码已变更" in ei.value.detail


def test_require_staff_user_rejects_revoked_jti(db):
    """登出（jti 进吊销表）后，旧 token 必须 401"""
    admin = _user(db, "boss", is_staff=True, role="super")
    token = _token(admin)
    jti = decode_token(token, expected_type="access")["jti"]
    revoke_jti(db, jti, admin.id, reason="logout")
    with pytest.raises(HTTPException) as ei:
        require_staff_user(_request(), _creds(token), db)
    assert ei.value.status_code == 401
    assert "吊销" in ei.value.detail


def test_require_staff_user_still_rejects_non_staff(db):
    """行为守恒：新鲜 token 的非管理员仍 403"""
    user = _user(db, "pawn", is_staff=False)
    with pytest.raises(HTTPException) as ei:
        require_staff_user(_request(), _creds(_token(user)), db)
    assert ei.value.status_code == 403


def test_require_staff_user_still_rejects_garbage(db):
    """行为守恒：伪造 token 仍 401"""
    with pytest.raises(HTTPException) as ei:
        require_staff_user(_request(), _creds("not.a.jwt"), db)
    assert ei.value.status_code == 401


# ===========================================================================
# F2：authenticate_ws_token 建连新鲜度
#
# 注意：authenticate_ws_token 内部用 backend.database.SessionLocal 自建会话，
# 测试里把它 patch 到内存库会话上。
# ===========================================================================

@pytest.fixture()
def _ws_session_local(db, monkeypatch):
    monkeypatch.setattr("backend.database.SessionLocal", lambda: db)
    return db


def test_authenticate_ws_token_accepts_fresh(db, _ws_session_local):
    user = _user(db, "wsuser")
    assert authenticate_ws_token(_token(user)) == user.id


def test_authenticate_ws_token_rejects_stale_after_password_change(db, _ws_session_local):
    user = _user(db, "wsuser")
    stale = _token(user)
    _bump_token_version(db, user)
    assert authenticate_ws_token(stale) is None


def test_authenticate_ws_token_rejects_revoked_jti(db, _ws_session_local):
    user = _user(db, "wsuser")
    token = _token(user)
    jti = decode_token(token, expected_type="access")["jti"]
    revoke_jti(db, jti, user.id, reason="logout")
    assert authenticate_ws_token(token) is None


def test_authenticate_ws_token_still_rejects_garbage(db, _ws_session_local):
    assert authenticate_ws_token("not.a.jwt") is None
    assert authenticate_ws_token(None) is None
    assert authenticate_ws_token("") is None


# ===========================================================================
# F3：economy.py 死导入清理（静态断言）
# ===========================================================================

def test_economy_has_no_dead_security_import():
    src = (ROOT / "backend" / "api" / "economy.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            for a in node.names:
                imported.add(a.asname or a.name)
    used = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
    dead = sorted(n for n in ("resolve_jwt_user_id", "client_ip") if n in imported and n not in used)
    assert not dead, f"economy.py 存在未使用的导入: {dead}"
    assert "resolve_jwt_user_id" not in src
