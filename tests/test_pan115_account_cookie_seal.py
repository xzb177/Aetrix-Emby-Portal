"""前后端完美审计 R4·B4：PA-005（P2）115 服务器账号配置档 Cookie 明文入库。

先红后绿：旧码上写侧密封与读侧解封断言应红，修复后全绿。
只用假凭据（fake- 前缀），不读生产。
"""
import asyncio
import os

os.environ.setdefault("DATABASE_TYPE", "sqlite")
os.environ.setdefault("REDIS_ENABLED", "false")
os.environ.setdefault("SECRET_KEY", "test-secret-pa005")

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend import models as base_models
from backend.emby_server import models as em
from backend.emby_server import portal as portal_mod
from backend.emby_server import transfer115 as t115
from backend.emby_server.upstream_sources import seal_secret

PLAINTEXT = "fake-uid-12345_fake-cookie-value-abcdef"


@pytest.fixture()
def db():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False},
                           poolclass=StaticPool)
    base_models.Base.metadata.create_all(engine)
    em.Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    yield session
    session.close()


def _sealed_row(db, name, plaintext):
    row = em.Pan115Account(name=name, cookie=seal_secret(plaintext),
                           is_default=False, is_enabled=True)
    db.add(row)
    db.commit()
    return row


def test_create_seals_cookie(db):
    """写侧：创建时 Cookie 必须密封入库，不得存明文。"""
    portal_mod.create_pan115_account(
        portal_mod.Pan115AccountCreate(name="seal-one", cookie=PLAINTEXT),
        object(), db)
    row = db.query(em.Pan115Account).filter(
        em.Pan115Account.name == "seal-one").first()
    assert row is not None
    assert row.cookie != PLAINTEXT
    assert row.cookie.startswith("fernet-v1:")


def test_update_reseals_cookie(db):
    """写侧：更新时 Cookie 同样密封入库。"""
    portal_mod.create_pan115_account(
        portal_mod.Pan115AccountCreate(name="seal-upd", cookie=PLAINTEXT),
        object(), db)
    row = db.query(em.Pan115Account).filter(
        em.Pan115Account.name == "seal-upd").first()
    new_value = "fake-uid-99999_fake-cookie-value-zzzzzz"
    portal_mod.update_pan115_account(
        row.id,
        portal_mod.Pan115AccountUpdate(cookie=new_value),
        object(), db)
    db.refresh(row)
    assert row.cookie != new_value
    assert row.cookie.startswith("fernet-v1:")
    cookie, _src = t115.resolve_cookie(db, account_id=row.id)
    assert cookie == new_value


def test_sealed_row_resolves_to_plaintext(db):
    """读侧：已密封行经 resolve_cookie 必须回到原文。"""
    row = _sealed_row(db, "sealed-one", PLAINTEXT)
    cookie, _src = t115.resolve_cookie(db, account_id=row.id)
    assert cookie == PLAINTEXT


def test_sealed_row_serialize_masks_plaintext(db):
    """管理端序列化：预览必须是原文尾 6 位，永不含全文。"""
    row = _sealed_row(db, "sealed-two", PLAINTEXT)
    data = portal_mod._serialize_account(row)
    assert data["cookie_preview"] == "…" + PLAINTEXT[-6:]
    assert PLAINTEXT not in str(data)


def test_legacy_plaintext_row_migrates(db):
    """存量迁移：预置明文行跑迁移后读侧仍得原文、且行已变密封格式；二次迁移无变化。"""
    row = em.Pan115Account(name="legacy-one", cookie=PLAINTEXT,
                           is_default=False, is_enabled=True)
    db.add(row)
    db.commit()
    first = t115.migrate_pan115_account_cookies(db)
    assert first["migrated"] == 1
    db.refresh(row)
    assert row.cookie != PLAINTEXT
    assert row.cookie.startswith("fernet-v1:")
    cookie, _src = t115.resolve_cookie(db, account_id=row.id)
    assert cookie == PLAINTEXT
    sealed_value = row.cookie
    second = t115.migrate_pan115_account_cookies(db)
    assert second["migrated"] == 0
    db.refresh(row)
    assert row.cookie == sealed_value


def test_migrate_skips_sealed_and_empty_rows(db):
    """迁移跳过已密封行与空行（幂等、可重跑）。"""
    sealed = _sealed_row(db, "already-sealed", PLAINTEXT)
    empty = em.Pan115Account(name="empty-one", cookie="",
                             is_default=False, is_enabled=True)
    db.add(empty)
    db.commit()
    result = t115.migrate_pan115_account_cookies(db)
    assert result["migrated"] == 0
    assert result["skipped"] == 2
    db.refresh(sealed)
    cookie, _src = t115.resolve_cookie(db, account_id=sealed.id)
    assert cookie == PLAINTEXT


def test_corrupt_sealed_row_raises_loudly(db):
    """损坏的密封值不得静默回落：resolve 响亮报错，不返回密封串本身。"""
    row = em.Pan115Account(name="corrupt-one", cookie="fernet-v1:not-a-token",  # secret-scan: allow: synthetic damaged sealed fixture for corrupt-value test, not a real credential
                           is_default=False, is_enabled=True)
    db.add(row)
    db.commit()
    with pytest.raises(t115.Pan115Error):
        t115.resolve_cookie(db, account_id=row.id)


def test_sealed_row_verify_uses_plaintext(db, monkeypatch):
    """verify 端点：必须用解封后值校验，不得把密封串当 Cookie 发出去。"""
    row = _sealed_row(db, "sealed-three", PLAINTEXT)
    seen = {}

    def _fake_verify(cookie):
        seen["cookie"] = cookie
        return {"ok": True, "message": "有效"}

    monkeypatch.setattr(t115, "verify_account", _fake_verify)
    asyncio.run(portal_mod.verify_pan115_account(row.id, object(), db))
    assert seen["cookie"] == PLAINTEXT
