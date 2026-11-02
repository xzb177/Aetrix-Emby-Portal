"""PA-054：网页端图片 key 跨标签页粘连（账号卡每次读都轮换）。

根因链：rotate_web_portal_token 每次调用都吊销全部旧专用行再签发；
前端 imageKey 无 storage 事件同步；收藏页 brokenPosters 粘连。
本文件只覆盖后端 rotate 的复用分支（前端两处另由 type-check/build 与
imageKey 装置脚本验证，见 PR 正文）。

口径（PA-054 修订，PA-036 鉴权面不动）：
- 带回来的 key 对得上本用户未吊销专用行、token_is_usable、剩余有效期
  大于 24h → 原样返回，不吊销、不新增行；
- 带不回来 / 他人 key / 已吊销 / 已过期 / 剩余不足 24h → 走轮换老路：
  吊销全部旧专用行、签发新行（既有 test_account_card_issues_web_api_key
  的「无头轮换、旧 key 立即 401」断言仍成立）。
"""
import os

os.environ.setdefault("DATABASE_TYPE", "sqlite")
os.environ.setdefault("REDIS_ENABLED", "false")

import uuid
from datetime import datetime, timedelta

import pytest

from backend import models  # noqa: E402
from backend.database import SessionLocal, init_db  # noqa: E402
from backend.emby_server import models as em  # noqa: E402
from backend.emby_server.auth import (  # noqa: E402
    WEB_PORTAL_DEVICE_ID,
    hash_emby_token,
    rotate_web_portal_token,
)

init_db()

_SUFFIX = uuid.uuid4().hex[:8]


@pytest.fixture()
def user():
    db = SessionLocal()
    u = models.WebUser(username=f"pa54img_{_SUFFIX}_{uuid.uuid4().hex[:6]}",
                        password_hash="x", is_active=True)
    db.add(u)
    db.commit()
    db.refresh(u)
    yield u
    db2 = SessionLocal()
    try:
        db2.query(em.EmbyApiToken).filter(
            em.EmbyApiToken.user_id == u.id,
            em.EmbyApiToken.device_id == WEB_PORTAL_DEVICE_ID).delete(
            synchronize_session=False)
        db2.query(models.WebUser).filter(models.WebUser.id == u.id).delete(
            synchronize_session=False)
        db2.commit()
    finally:
        db2.close()
        db.close()


def _portal_rows(user_id):
    db = SessionLocal()
    try:
        return db.query(em.EmbyApiToken).filter(
            em.EmbyApiToken.user_id == user_id,
            em.EmbyApiToken.device_id == WEB_PORTAL_DEVICE_ID).all()
    finally:
        db.close()


def _set_expiry(key, expires_at):
    db = SessionLocal()
    try:
        row = db.query(em.EmbyApiToken).filter(
            em.EmbyApiToken.token == hash_emby_token(key)).first()
        assert row is not None
        row.expires_at = expires_at
        db.commit()
    finally:
        db.close()


def _revoke(key):
    db = SessionLocal()
    try:
        row = db.query(em.EmbyApiToken).filter(
            em.EmbyApiToken.token == hash_emby_token(key)).first()
        assert row is not None
        row.is_revoked = True
        db.commit()
    finally:
        db.close()


def _fresh_session():
    return SessionLocal()


def test_first_rotate_issues_key(user):
    db = _fresh_session()
    try:
        key = rotate_web_portal_token(db, user)
    finally:
        db.close()
    assert key and len(key) == 40
    rows = _portal_rows(user.id)
    assert len(rows) == 1
    assert rows[0].token == hash_emby_token(key)
    assert rows[0].is_revoked is False


def test_reuse_returns_same_key_without_new_row(user):
    db = _fresh_session()
    try:
        key1 = rotate_web_portal_token(db, user)
        key2 = rotate_web_portal_token(db, user, current_key=key1)
    finally:
        db.close()
    assert key2 == key1
    assert len(_portal_rows(user.id)) == 1


def test_rotate_without_key_revokes_old(user):
    db = _fresh_session()
    try:
        key1 = rotate_web_portal_token(db, user)
        key2 = rotate_web_portal_token(db, user)
    finally:
        db.close()
    assert key2 and key2 != key1
    rows = _portal_rows(user.id)
    by_token = {r.token: r for r in rows}
    assert by_token[hash_emby_token(key1)].is_revoked is True
    assert by_token[hash_emby_token(key2)].is_revoked is False


def test_reuse_rejected_near_expiry(user):
    db = _fresh_session()
    try:
        key1 = rotate_web_portal_token(db, user)
    finally:
        db.close()
    _set_expiry(key1, datetime.now() + timedelta(hours=1))
    db = _fresh_session()
    try:
        key2 = rotate_web_portal_token(db, user, current_key=key1)
    finally:
        db.close()
    assert key2 != key1
    rows = {r.token: r for r in _portal_rows(user.id)}
    assert rows[hash_emby_token(key1)].is_revoked is True


def test_reuse_rejected_for_revoked(user):
    db = _fresh_session()
    try:
        key1 = rotate_web_portal_token(db, user)
    finally:
        db.close()
    _revoke(key1)
    db = _fresh_session()
    try:
        key2 = rotate_web_portal_token(db, user, current_key=key1)
    finally:
        db.close()
    assert key2 != key1


def test_reuse_rejected_for_expired(user):
    db = _fresh_session()
    try:
        key1 = rotate_web_portal_token(db, user)
    finally:
        db.close()
    _set_expiry(key1, datetime.now() - timedelta(hours=1))
    db = _fresh_session()
    try:
        key2 = rotate_web_portal_token(db, user, current_key=key1)
    finally:
        db.close()
    assert key2 != key1


def test_reuse_rejected_for_foreign_key(user):
    db = _fresh_session()
    try:
        other = models.WebUser(
            username=f"pa54other_{_SUFFIX}_{uuid.uuid4().hex[:6]}",
            password_hash="x", is_active=True)
        db.add(other)
        db.commit()
        db.refresh(other)
        other_id = other.id
        foreign_key = rotate_web_portal_token(db, other)
        mine = rotate_web_portal_token(db, user, current_key=foreign_key)
    finally:
        db.close()
    assert mine != foreign_key
    foreign_rows = _portal_rows(other_id)
    assert len(foreign_rows) == 1
    assert foreign_rows[0].is_revoked is False
    db2 = SessionLocal()
    try:
        db2.query(em.EmbyApiToken).filter(
            em.EmbyApiToken.user_id == other_id).delete(
            synchronize_session=False)
        db2.query(models.WebUser).filter(
            models.WebUser.id == other_id).delete(synchronize_session=False)
        db2.commit()
    finally:
        db2.close()


def test_reuse_accepts_permanent_row(user):
    db = _fresh_session()
    try:
        key1 = rotate_web_portal_token(db, user)
    finally:
        db.close()
    _set_expiry(key1, None)
    db = _fresh_session()
    try:
        key2 = rotate_web_portal_token(db, user, current_key=key1)
    finally:
        db.close()
    assert key2 == key1
