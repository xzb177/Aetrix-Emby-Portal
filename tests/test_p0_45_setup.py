"""P0-4/P0-5 回归测试：初始化入口的部署者凭据与跨进程互斥。

P0-4：初始化入口无部署者凭据，未初始化时可被任意访问者抢占管理员。
修复：SETUP_BOOTSTRAP_TOKEN 环境变量；设置时请求必须携带一致的 bootstrap_token。

P0-5：threading.Lock 只在进程内有效，多 worker 下两个进程可同时通过检查
并创建不同用户名的管理员。修复：PG 用 advisory lock，SQLite 沿用进程内锁。

全部离线：内存 SQLite + 进程内路由，不发起真实网络请求。
"""
from __future__ import annotations

import os

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend import models
from backend.emby_server import models as em


@pytest.fixture()
def db():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False},
                           poolclass=StaticPool)
    models.Base.metadata.create_all(engine)
    em.Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    yield session
    session.close()


@pytest.fixture()
def setup_client(db):
    from backend.api import setup as setup_api
    from backend.database import get_db

    app = FastAPI()
    app.include_router(setup_api.router)

    def _get_db():
        yield db

    app.dependency_overrides[get_db] = _get_db
    with TestClient(app) as c:
        yield c


def _status(client):
    return client.get("/api/admin/setup/status").json()["setup_completed"]


# ===========================================================================
# P0-4：bootstrap token
# ===========================================================================

def test_bootstrap_token_required_when_set(db, setup_client, monkeypatch):
    """设置了 SETUP_BOOTSTRAP_TOKEN 时，不带 token 必须 403。"""
    monkeypatch.setenv("SETUP_BOOTSTRAP_TOKEN", "deploy-secret-123")
    assert not _status(setup_client)

    r = setup_client.post("/api/admin/setup", json={
        "username": "attacker", "password": "Attacker123!@#",
    })
    assert r.status_code == 403, r.text
    assert "bootstrap" in r.text.lower()
    # 攻击者没有得逞：没有管理员被创建
    assert db.query(models.WebUser).filter_by(is_staff=True).count() == 0
    assert not _status(setup_client)


def test_bootstrap_token_wrong_rejected(db, setup_client, monkeypatch):
    """token 错误必须 403。"""
    monkeypatch.setenv("SETUP_BOOTSTRAP_TOKEN", "deploy-secret-123")
    r = setup_client.post("/api/admin/setup", json={
        "username": "attacker", "password": "Attacker123!@#",
        "bootstrap_token": "wrong-token",
    })
    assert r.status_code == 403, r.text
    assert db.query(models.WebUser).filter_by(is_staff=True).count() == 0


def test_bootstrap_token_correct_allows_setup(db, setup_client, monkeypatch):
    """token 正确时可以完成初始化，之后入口永久关闭。"""
    monkeypatch.setenv("SETUP_BOOTSTRAP_TOKEN", "deploy-secret-123")
    r = setup_client.post("/api/admin/setup", json={
        "username": "realadmin", "password": "RealAdmin123!@#",
        "bootstrap_token": "deploy-secret-123",
    })
    assert r.status_code == 200, r.text
    assert r.json()["setup_completed"] is True
    assert _status(setup_client)

    # 之后再调用一律 403（即使带对的 token）
    r = setup_client.post("/api/admin/setup", json={
        "username": "attacker2", "password": "Attacker123!@#",
        "bootstrap_token": "deploy-secret-123",
    })
    assert r.status_code == 403, r.text
    # 只有一个管理员
    assert db.query(models.WebUser).filter_by(is_staff=True).count() == 1


def test_no_bootstrap_token_env_keeps_old_behavior(db, setup_client, monkeypatch):
    """未设置环境变量时保持旧行为（兼容），但能完成初始化。"""
    monkeypatch.delenv("SETUP_BOOTSTRAP_TOKEN", raising=False)
    r = setup_client.post("/api/admin/setup", json={
        "username": "legacyadmin", "password": "LegacyAdmin123!@#",
    })
    assert r.status_code == 200, r.text
    assert _status(setup_client)


# ===========================================================================
# P0-5：跨进程互斥（SQLite 下验证锁的基本语义）
# ===========================================================================

def test_setup_lock_is_reentrant_safe(db, setup_client, monkeypatch):
    """锁的基本语义：串行执行两次 setup，第二次 403，只有一个管理员。"""
    monkeypatch.delenv("SETUP_BOOTSTRAP_TOKEN", raising=False)
    r1 = setup_client.post("/api/admin/setup", json={
        "username": "first", "password": "FirstAdmin123!@#",
    })
    assert r1.status_code == 200, r1.text
    r2 = setup_client.post("/api/admin/setup", json={
        "username": "second", "password": "SecondAdmin123!@#",
    })
    assert r2.status_code == 403, r2.text
    assert db.query(models.WebUser).filter_by(is_staff=True).count() == 1


def test_setup_lock_class_exists_and_works(db):
    """_SetupLock 上下文管理器在 SQLite 下能正常加锁/解锁（不抛异常）。"""
    from backend.api.setup import _SetupLock
    with _SetupLock(db):
        pass
    # 二次进入也正常（锁已释放）
    with _SetupLock(db):
        pass


def test_pg_lock_key_is_stable():
    """advisory lock key 稳定（重启后一致），且为有符号 64 位。"""
    from backend.api.setup import _SETUP_PG_LOCK_KEY
    assert -(2 ** 63) <= _SETUP_PG_LOCK_KEY < 2 ** 63
    # 同一进程内多次导入一致
    import importlib
    import backend.api.setup as setup_mod
    importlib.reload(setup_mod)
    assert setup_mod._SETUP_PG_LOCK_KEY == _SETUP_PG_LOCK_KEY
