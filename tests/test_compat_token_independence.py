"""兼容层令牌独立治理（FMBY 播放全换阶段 3-2，#22）回归测试。

先说清本阶段 #22 的真实结论（见阶段报告）：
- 独立表不是新造的：emby_api_tokens 早已是哈希-only、按设备分行、
  可吊销、带过期的独立表；再造第二张表只会分裂凭据域，故本阶段不造表，
  只补齐剩余缺口——TTL 可配（默认 30 不变）、单令牌吊销（此前只有整设备吊销）。
- 生命周期独立契约（本文件钉死）：
  1. 令牌值只存哈希，原文永不落库；
  2. 过期/吊销的令牌一律不可用；
  3. 单令牌吊销不碰同用户其它令牌；
  4. 门户 JWT 吊销（revoke_jti）不碰兼容层令牌行；
  5. TTL 默认 30 天（FMBY 180 天未经真实播放器隔期复验，不作默认值）。
全部用隔离的内存 SQLite，不碰网络、不碰生产库。
"""

from datetime import datetime, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from types import SimpleNamespace

from backend import devices, models, security
from backend.emby_server import auth, models as em
from backend.emby_server import play_sign
from backend.integrations import store


@pytest.fixture()
def db():
    engine = create_engine("sqlite://")
    models.Base.metadata.create_all(engine)
    em.Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    store.invalidate()
    yield session
    store.invalidate()
    session.close()


def _user(db, username="alice"):
    row = models.WebUser(username=username, password_hash="x")
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def _token_row(db, user_id, device_id, secret, **kw):
    row = em.EmbyApiToken(
        token=auth.hash_emby_token(secret), user_id=user_id,
        device_id=device_id, **kw)
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def _req_with_api_key(secret):
    return SimpleNamespace(query_params={"api_key": secret}, headers={})


def test_token_stored_as_hash_only_and_resolvable(db):
    user = _user(db)
    row = _token_row(db, user.id, "dev-1", "plain-secret-value")
    assert row.token != "plain-secret-value"
    assert row.token == auth.hash_emby_token("plain-secret-value")
    resolved = auth.resolve_token(db, _req_with_api_key("plain-secret-value"))
    assert resolved is not None and resolved[0].id == user.id
    assert auth.resolve_token(db, _req_with_api_key("wrong-secret")) is None


def test_expired_and_revoked_tokens_unusable(db):
    user = _user(db)
    expired = _token_row(db, user.id, "dev-1", "s-exp",
                         expires_at=datetime.now() - timedelta(days=1))
    revoked = _token_row(db, user.id, "dev-2", "s-rev", is_revoked=True)
    assert play_sign.token_is_usable(expired) is False
    assert play_sign.token_is_usable(revoked) is False
    assert auth.resolve_token(db, _req_with_api_key("s-exp")) is None
    assert auth.resolve_token(db, _req_with_api_key("s-rev")) is None


def test_single_token_revoke_leaves_siblings_alone(db):
    user = _user(db)
    keep = _token_row(db, user.id, "dev-1", "s-keep")
    drop = _token_row(db, user.id, "dev-2", "s-drop")
    assert devices.revoke_single_token(db, user.id, drop.id) is True
    assert devices.revoke_single_token(db, user.id, 999999) is False
    db.refresh(keep)
    db.refresh(drop)
    assert keep.is_revoked is False
    assert drop.is_revoked is True
    # 别人名下的令牌 id 即使猜到也不能被本用户吊销（user_id 双条件）。
    assert devices.revoke_single_token(db, user.id + 1000, keep.id) is False
    db.refresh(keep)
    assert keep.is_revoked is False


def test_portal_jwt_revoke_does_not_touch_compat_token(db):
    """门户 JWT 吊销与兼容层令牌分属两套存储：吊销 JWT 后兼容令牌仍可用。"""
    user = _user(db)
    row = _token_row(db, user.id, "dev-1", "s-jwt-test")
    security.revoke_jti(db, "some-portal-jti", user.id, reason="logout")
    db.refresh(row)
    assert row.is_revoked is False
    assert play_sign.token_is_usable(row) is True
    assert auth.resolve_token(db, _req_with_api_key("s-jwt-test")) is not None


def test_admin_single_token_endpoint_revokes_only_that_row(db):
    """管理端 DELETE /api/admin/emby-tokens/{id} 的函数本体（绕过 Depends
    直接调用）：只吊销该行，审计不抛错；不存在的 id 报 404。"""
    from fastapi import HTTPException

    from backend.api import admin_ops

    admin = _user(db, username="admin")
    user = _user(db, username="bob")
    row = _token_row(db, user.id, "dev-9", "s-endpoint")
    out = admin_ops.revoke_emby_token(row.id, current_admin=admin, db=db)
    assert out["success"] is True and out["user_id"] == user.id
    db.refresh(row)
    assert row.is_revoked is True
    with pytest.raises(HTTPException) as exc:
        admin_ops.revoke_emby_token(999999, current_admin=admin, db=db)
    assert exc.value.status_code == 404


def test_ttl_default_is_30_not_fmby_180(db):
    """默认值保持 30 天；180 天未经真实播放器隔期复验，不许悄悄进默认。"""
    assert play_sign.TOKEN_TTL_DAYS == 30
    expiry = play_sign.token_expiry_default()
    delta = expiry - datetime.now()
    assert timedelta(days=29) < delta <= timedelta(days=30, minutes=5)
