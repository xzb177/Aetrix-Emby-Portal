"""P0-2 回归测试：兑换码/卡码"已使用禁删"检查与删除必须是原子的。

背景：此前两处删除都是先查 use_count 再按主键删，检查与删除之间无锁无约束。
并发兑换可在检查之后提交使用次数，删除仍按主键执行，删掉已使用记录及其痕迹。

修复：带 COALESCE(use_count,0)=0 条件的原子 DELETE，0 行即"不存在或已使用"。

全部离线：内存 SQLite + 进程内路由，不发起真实网络请求。
并发用例用文件型 SQLite + WAL（内存库单连接会把并发串行化，测不出竞态）。
"""
from __future__ import annotations

import os
import pathlib
import random
import tempfile
import threading
import time

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event, func, text
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


# ===========================================================================
# 真并发用例（P2 补齐）：内存库单连接会把并发串行化，这里用文件型 SQLite + WAL
# ===========================================================================

def _concurrent_client():
    """文件型 SQLite + WAL + 每次请求新建 session 的测试客户端。

    返回 (client, engine)。多线程不能共享同一个 SQLAlchemy session，
    get_db 的 override 必须每次请求新建。
    """
    from backend.api import admin as admin_api  # noqa: F401 — 注册 economy 路由
    from backend.api import admin_ops  # noqa: F401 — 注册 registration-codes 路由
    from backend.api.admin_core import admin_router
    from backend.api.admin_ops import admin_ops_router
    from backend.database import get_db

    tmpdir = tempfile.mkdtemp(prefix="p0_2_concurrent_")
    engine = create_engine(
        f"sqlite:///{tmpdir}/test.db",
        connect_args={"check_same_thread": False},
    )

    @event.listens_for(engine, "connect")
    def _set_sqlite_pragma(dbapi_connection, _record):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA busy_timeout=10000")
        cursor.close()

    models.Base.metadata.create_all(engine)
    em.Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, expire_on_commit=False)

    def _get_db():
        s = Session()
        try:
            yield s
        finally:
            s.close()

    app = FastAPI()
    app.include_router(admin_router)
    app.include_router(admin_ops_router)
    app.dependency_overrides[get_db] = _get_db
    # raise_server_exceptions=False：并发下的 500 会变成响应，由断言显式检查
    client = TestClient(app, raise_server_exceptions=False)
    return client, engine


def test_concurrent_double_delete_single_winner():
    """真并发：8 线程同时删除同一个未使用兑换码，恰好一个成功，无 5xx。"""
    client, engine = _concurrent_client()
    Session = sessionmaker(bind=engine, expire_on_commit=False)

    db = Session()
    admin = _admin(db)
    headers = _bearer(admin)
    code = models.ExchangeCode(code="CONC-DOUBLE-DELETE", use_count=0, is_active=True,
                               type="points", points_value=10)
    db.add(code)
    db.flush()
    code_id = code.id
    db.commit()
    db.close()

    barrier = threading.Barrier(8)
    statuses: list = []
    errors: list = []
    lock = threading.Lock()

    def worker():
        try:
            barrier.wait()  # 同步起跑，挤进同一个竞态窗口
            resp = client.delete(f"/api/admin/economy/exchange-codes/{code_id}",
                                 headers=headers)
            with lock:
                statuses.append(resp.status_code)
        except Exception as exc:  # noqa: BLE001
            with lock:
                errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors, errors
    assert len(statuses) == 8, statuses
    assert statuses.count(200) == 1, f"必须恰好一个删除成功: {statuses}"
    assert all(s in (200, 400, 404) for s in statuses), statuses
    assert not any(s >= 500 for s in statuses), statuses

    db = Session()
    try:
        assert db.query(models.ExchangeCode).filter_by(id=code_id).first() is None
    finally:
        db.close()


def test_concurrent_use_vs_delete_never_deletes_used():
    """真并发：删除与核销（use_count+1）竞态，绝不删掉已被使用过的码。

    每轮一个删除线程与一个核销线程经 barrier 同步起跑，25 轮覆盖两种交错：
    安全不变式——不允许出现（删除返回 200 且核销 UPDATE 命中了 1 行）。
    """
    client, engine = _concurrent_client()
    Session = sessionmaker(bind=engine, expire_on_commit=False)

    db = Session()
    admin = _admin(db)
    headers = _bearer(admin)
    db.close()

    for round_no in range(25):
        db = Session()
        code = models.ExchangeCode(code=f"CONC-USE-DEL-{round_no}", use_count=0,
                                   is_active=True, type="points", points_value=10)
        db.add(code)
        db.flush()
        code_id = code.id
        db.commit()
        db.close()

        barrier = threading.Barrier(2)
        results: dict = {}
        errors: list = []
        lock = threading.Lock()
        # 每轮随机让一方慢 30ms，覆盖"删除先赢"和"核销先赢"两种交错
        slow_side = random.choice(("use", "delete"))

        def use_worker():
            # 模拟一次并发核销提交：独立事务把 use_count + 1
            try:
                barrier.wait()
                if slow_side == "use":
                    time.sleep(0.03)
                s = Session()
                try:
                    res = s.execute(
                        text("UPDATE exchange_codes SET use_count = use_count + 1 "
                             "WHERE id = :id"),
                        {"id": code_id},
                    )
                    s.commit()
                    rowcount = res.rowcount
                finally:
                    s.close()
                with lock:
                    results["use_rowcount"] = rowcount
            except Exception as exc:  # noqa: BLE001
                with lock:
                    errors.append(exc)

        def delete_worker():
            try:
                barrier.wait()
                if slow_side == "delete":
                    time.sleep(0.03)
                resp = client.delete(f"/api/admin/economy/exchange-codes/{code_id}",
                                     headers=headers)
                with lock:
                    results["delete_status"] = resp.status_code
            except Exception as exc:  # noqa: BLE001
                with lock:
                    errors.append(exc)

        t_use = threading.Thread(target=use_worker)
        t_del = threading.Thread(target=delete_worker)
        t_use.start()
        t_del.start()
        t_use.join()
        t_del.join()

        assert not errors, (round_no, errors)
        d_status = results.get("delete_status")
        u_rowcount = results.get("use_rowcount")
        assert d_status is not None and u_rowcount is not None, (round_no, results)
        # 两种合法交错：删除赢（核销 UPDATE 命中 0 行）或核销赢（删除 400）；
        # 非法：删除 200 但核销已命中（删掉了已使用的码）
        assert not (d_status == 200 and u_rowcount == 1), \
            f"第 {round_no} 轮：删掉了已被使用过的码！"
