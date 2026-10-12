"""P0-2 回归测试：兑换码/卡码"已使用禁删"检查与删除必须是原子的。

背景：此前两处删除都是先查 use_count 再按主键删，检查与删除之间无锁无约束。
并发兑换可在检查之后提交使用次数，删除仍按主键执行，删掉已使用记录及其痕迹。

修复：带 COALESCE(use_count,0)=0 条件的原子 DELETE，0 行即"不存在或已使用"。

全部离线：内存 SQLite + 进程内路由，不发起真实网络请求。
"""
from __future__ import annotations

import os
import pathlib

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, func
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend import models
from backend.emby_server import models as em
from backend.security import create_access_token, hash_password

TEST_SECRET = "test-only-secret-key-0123456789abcdef"


@pytest.fixture(autouse=True)
def _secret_key(monkeypatch):
    if not (os.environ.get("SECRET_KEY") or "").strip():
        monkeypatch.setenv("SECRET_KEY", TEST_SECRET)


@pytest.fixture()
def db():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False},
                           poolclass=StaticPool)
    models.Base.metadata.create_all(engine)
    em.Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    yield session
    session.close()


def _admin(db):
    u = models.WebUser(username="boss", password_hash=hash_password("x"),
                       is_staff=True, is_active=True, admin_role="super",
                       token_version=0)
    db.add(u)
    db.commit()
    return u


@pytest.fixture()
def admin_client(db):
    from backend.api import admin as admin_api  # noqa: F401 — 注册 economy 路由
    from backend.api import admin_ops  # noqa: F401 — 注册 registration-codes 路由
    from backend.api.admin_core import admin_router
    from backend.api.admin_ops import admin_ops_router
    from backend.database import get_db

    app = FastAPI()
    app.include_router(admin_router)
    app.include_router(admin_ops_router)

    def _get_db():
        yield db

    app.dependency_overrides[get_db] = _get_db
    with TestClient(app) as c:
        yield c


def _bearer(user) -> dict:
    token = create_access_token(user.id, {"username": user.username, "staff": True},
                                token_version=user.token_version or 0)
    return {"Authorization": f"Bearer {token}"}


def _exchange_code(db, code="TEST-CODE-001", use_count=0):
    c = models.ExchangeCode(code=code, use_count=use_count, is_active=True,
                            type="points", points_value=10)
    db.add(c)
    db.commit()
    return c


def _reg_code(db, code="REG-CODE-001", use_count=0):
    c = models.RegistrationCode(code=code, use_count=use_count, is_active=True)
    db.add(c)
    db.commit()
    return c


# ===========================================================================
# 兑换码删除
# ===========================================================================

def test_delete_unused_exchange_code_ok(db, admin_client):
    admin = _admin(db)
    c = _exchange_code(db, use_count=0)
    code_id = c.id

    r = admin_client.delete(f"/api/admin/economy/exchange-codes/{code_id}",
                            headers=_bearer(admin))
    assert r.status_code == 200, r.text
    assert db.query(models.ExchangeCode).filter_by(id=code_id).first() is None


def test_delete_nonexistent_exchange_code_404(db, admin_client):
    admin = _admin(db)
    r = admin_client.delete("/api/admin/economy/exchange-codes/999999",
                            headers=_bearer(admin))
    assert r.status_code == 404, r.text


def test_delete_used_exchange_code_rejected(db, admin_client):
    admin = _admin(db)
    c = _exchange_code(db, use_count=3)

    r = admin_client.delete(f"/api/admin/economy/exchange-codes/{c.id}",
                            headers=_bearer(admin))
    assert r.status_code == 400, r.text
    # 记录必须还在
    assert db.query(models.ExchangeCode).filter_by(id=c.id).first() is not None


def test_delete_exchange_code_atomicity(db, admin_client):
    """模拟并发：在删除的 WHERE 条件里 use_count 已 > 0，必须删不掉。"""
    admin = _admin(db)
    c = _exchange_code(db, use_count=0)

    # 模拟"检查之后、删除之前"有并发兑换提交了使用次数
    db.query(models.ExchangeCode).filter_by(id=c.id).update(
        {models.ExchangeCode.use_count: 1}, synchronize_session=False)
    db.commit()

    # 此时走删除接口：原子条件 DELETE 必须 0 行 → 400，且记录保留
    r = admin_client.delete(f"/api/admin/economy/exchange-codes/{c.id}",
                            headers=_bearer(admin))
    assert r.status_code == 400, r.text
    row = db.query(models.ExchangeCode).filter_by(id=c.id).first()
    assert row is not None and row.use_count == 1


# ===========================================================================
# 注册卡码删除
# ===========================================================================

def test_delete_unused_reg_code_ok(db, admin_client):
    admin = _admin(db)
    c = _reg_code(db, use_count=0)
    code_id = c.id

    r = admin_client.delete(f"/api/admin/registration-codes/{code_id}",
                            headers=_bearer(admin))
    assert r.status_code == 200, r.text
    assert db.query(models.RegistrationCode).filter_by(id=code_id).first() is None


def test_delete_nonexistent_reg_code_404(db, admin_client):
    admin = _admin(db)
    r = admin_client.delete("/api/admin/registration-codes/999999",
                            headers=_bearer(admin))
    assert r.status_code == 404, r.text


def test_delete_used_reg_code_rejected(db, admin_client):
    admin = _admin(db)
    c = _reg_code(db, use_count=2)

    r = admin_client.delete(f"/api/admin/registration-codes/{c.id}",
                            headers=_bearer(admin))
    assert r.status_code == 400, r.text
    assert db.query(models.RegistrationCode).filter_by(id=c.id).first() is not None


def test_delete_reg_code_atomicity(db, admin_client):
    """模拟并发核销穿透：条件 DELETE 必须挡住。"""
    admin = _admin(db)
    c = _reg_code(db, use_count=0)

    db.query(models.RegistrationCode).filter_by(id=c.id).update(
        {models.RegistrationCode.use_count: 1}, synchronize_session=False)
    db.commit()

    r = admin_client.delete(f"/api/admin/registration-codes/{c.id}",
                            headers=_bearer(admin))
    assert r.status_code == 400, r.text
    row = db.query(models.RegistrationCode).filter_by(id=c.id).first()
    assert row is not None and row.use_count == 1
