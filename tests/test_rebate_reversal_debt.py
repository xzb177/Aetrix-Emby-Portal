"""r53 ⑧：订单退款时返利撤回失败留下持久化欠账。

失败时落一条 pending 欠账（RebateReversalDebt，同一订单只一条）；
重复尝试不重复记账；后来撤回成功时把 pending 欠账标记 recovered；
管理端有只读列表＋豁免端点＋重试收回端点（r55：refund_order 对已退款订单
直接 400，_reverse_rebate 的成功分支在生产走不到，欠账只进不出，
重试端点补上"收回"路径）。
"""
import os

os.environ.setdefault("DATABASE_TYPE", "sqlite")
os.environ.setdefault("REDIS_ENABLED", "false")

from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend import models
from backend.api import orders_admin
from backend.api.admin_core import get_current_admin
from backend.database import get_db


@pytest.fixture()
def db():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    models.Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    try:
        yield session
    finally:
        session.rollback()
        session.close()
        engine.dispose()


def _make_user(db, username="u1", points=1000):
    u = models.WebUser(username=username, password_hash="", points=points,
                       is_active=True, member_xp=0, member_level=1)
    db.add(u)
    db.commit()
    return u


def _seed_rebate(db, inviter_id, order_id="O1", amount=200):
    """履约时发给邀请人的返利账本行（ref_id = rebate:{order_id}）"""
    db.add(models.PointsLog(user_id=inviter_id, amount=amount,
                            balance_after=amount, type="rebate",
                            description="邀请返利", ref_id=f"rebate:{order_id}"))
    db.commit()


def _debts(db, order_id="O1"):
    return db.query(models.RebateReversalDebt).filter(
        models.RebateReversalDebt.order_id == order_id).all()


# ---------------- 失败 → 落库 ----------------

def test_reverse_rebate_failure_records_pending_debt(db):
    inviter = _make_user(db, "inviter", points=50)  # 余额 50 < 返利 200
    _seed_rebate(db, inviter.id, "O1", amount=200)

    reversed_amount, owed = orders_admin._reverse_rebate(db, "O1", allow_negative=False)
    db.commit()

    assert (reversed_amount, owed) == (0, True)
    rows = _debts(db, "O1")
    assert len(rows) == 1
    d = rows[0]
    assert d.user_id == inviter.id
    assert d.amount == 200
    assert d.status == "pending"
    assert d.reason  # 失败原因留下来了
    assert d.created_at is not None
    assert d.resolved_at is None


def test_reverse_rebate_failure_idempotent_no_dup_debt(db):
    """重复尝试不重复记账：同一订单只许一条欠账"""
    inviter = _make_user(db, "inviter", points=50)
    _seed_rebate(db, inviter.id, "O2", amount=200)

    first = orders_admin._reverse_rebate(db, "O2", allow_negative=False)
    db.commit()
    second = orders_admin._reverse_rebate(db, "O2", allow_negative=False)
    db.commit()

    assert first == (0, True)
    assert second == (0, True)
    assert len(_debts(db, "O2")) == 1


def test_reverse_rebate_later_success_marks_recovered(db):
    """后来撤回成功（允许负数）：pending 欠账标记 recovered"""
    inviter = _make_user(db, "inviter", points=50)
    _seed_rebate(db, inviter.id, "O3", amount=200)

    assert orders_admin._reverse_rebate(db, "O3", allow_negative=False) == (0, True)
    db.commit()
    assert _debts(db, "O3")[0].status == "pending"

    # 管理员勾选「允许余额为负」后重试：撤回成功
    reversed_amount, owed = orders_admin._reverse_rebate(db, "O3", allow_negative=True)
    db.commit()
    assert (reversed_amount, owed) == (200, False)

    rows = _debts(db, "O3")
    assert len(rows) == 1
    assert rows[0].status == "recovered"
    assert rows[0].resolved_at is not None


def test_reverse_rebate_success_without_debt_noop(db):
    """本来就没有欠账时成功撤回：不报错、也不凭空造行"""
    inviter = _make_user(db, "inviter", points=1000)
    _seed_rebate(db, inviter.id, "O4", amount=200)

    assert orders_admin._reverse_rebate(db, "O4", allow_negative=False) == (200, False)
    db.commit()
    assert _debts(db, "O4") == []


def test_reverse_rebate_no_rebate_entry_no_debt(db):
    """这笔订单本来就没返利：不记账"""
    _make_user(db, "buyer", points=1000)
    assert orders_admin._reverse_rebate(db, "O5", allow_negative=False) == (0, False)
    db.commit()
    assert _debts(db, "O5") == []


# ---------------- 管理端只读列表＋豁免 ----------------

def _admin_app(db):
    app = FastAPI()
    app.include_router(orders_admin.admin_orders_router)
    app.dependency_overrides[get_current_admin] = lambda: SimpleNamespace(id=999)
    app.dependency_overrides[get_db] = lambda: db
    return TestClient(app)


def test_list_and_waive_rebate_debt(db):
    inviter = _make_user(db, "inviter", points=50)
    _seed_rebate(db, inviter.id, "O6", amount=200)
    assert orders_admin._reverse_rebate(db, "O6", allow_negative=False) == (0, True)
    db.commit()

    client = _admin_app(db)
    resp = client.get("/api/admin/economy/rebate-debts", params={"status": "pending"})
    assert resp.status_code == 200
    recs = resp.json()["records"]
    assert len(recs) == 1
    assert recs[0]["order_id"] == "O6"
    assert recs[0]["amount"] == 200
    assert recs[0]["username"] == "inviter"
    debt_id = recs[0]["id"]

    w = client.post(f"/api/admin/economy/rebate-debts/{debt_id}/waive")
    assert w.status_code == 200
    assert w.json()["status"] == "waived"

    d = db.query(models.RebateReversalDebt).filter(
        models.RebateReversalDebt.id == debt_id).one()
    assert d.status == "waived"
    assert d.resolved_at is not None

    # 豁免过的欠账不能再豁免
    assert client.post(f"/api/admin/economy/rebate-debts/{debt_id}/waive").status_code == 404


# ---------------- r55：欠账重试收回端点 ----------------
# 背景：refund_order 对已退款订单直接 400，_reverse_rebate 的成功分支在生产
# 走不到，pending 欠账只进不出；本端点补上"收回"路径，必须走 HTTP 端点测。

def _seed_pending_debt(db, order_id="R1", amount=200, inviter_points=50):
    inviter = _make_user(db, f"inviter_{order_id}", points=inviter_points)
    _seed_rebate(db, inviter.id, order_id, amount=amount)
    assert orders_admin._reverse_rebate(db, order_id, allow_negative=False) == (0, True)
    db.commit()
    debts = _debts(db, order_id)
    assert len(debts) == 1 and debts[0].status == "pending"
    return inviter, debts[0].id


def test_retry_rebate_debt_success_marks_recovered(db):
    """允许负数后重试：撤回成功，pending → recovered，邀请人被扣成负数"""
    inviter, debt_id = _seed_pending_debt(db, "R1")
    client = _admin_app(db)

    resp = client.post(f"/api/admin/economy/rebate-debts/{debt_id}/retry",
                       json={"allow_negative": True})
    assert resp.status_code == 200
    body = resp.json()
    assert body["success"] is True
    assert body["status"] == "recovered"
    assert body["reversed_amount"] == 200

    db.expire_all()
    assert _debts(db, "R1")[0].status == "recovered"
    assert _debts(db, "R1")[0].resolved_at is not None
    assert db.query(models.WebUser).filter(
        models.WebUser.id == inviter.id).first().points == -150


def test_retry_rebate_debt_insufficient_stays_pending(db):
    """不允许负数时重试：余额仍不足 → success=False，保持 pending，账不动"""
    inviter, debt_id = _seed_pending_debt(db, "R2")
    client = _admin_app(db)

    resp = client.post(f"/api/admin/economy/rebate-debts/{debt_id}/retry",
                       json={"allow_negative": False})
    assert resp.status_code == 200
    body = resp.json()
    assert body["success"] is False
    assert body["status"] == "pending"
    assert "不足" in (body["reason"] or "")

    db.expire_all()
    assert _debts(db, "R2")[0].status == "pending"
    assert db.query(models.WebUser).filter(
        models.WebUser.id == inviter.id).first().points == 50


def test_retry_rebate_debt_second_retry_404(db):
    """已收回的欠账再重试：404，不重复扣"""
    inviter, debt_id = _seed_pending_debt(db, "R3")
    client = _admin_app(db)

    assert client.post(f"/api/admin/economy/rebate-debts/{debt_id}/retry",
                       json={"allow_negative": True}).status_code == 200
    resp = client.post(f"/api/admin/economy/rebate-debts/{debt_id}/retry",
                       json={"allow_negative": True})
    assert resp.status_code == 404

    db.expire_all()
    assert db.query(models.WebUser).filter(
        models.WebUser.id == inviter.id).first().points == -150  # 只扣了一次


def test_retry_rebate_debt_nonexistent_404(db):
    client = _admin_app(db)
    resp = client.post("/api/admin/economy/rebate-debts/999999/retry",
                       json={"allow_negative": True})
    assert resp.status_code == 404
