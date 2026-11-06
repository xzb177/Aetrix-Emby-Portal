"""代理分销完整版测试（2026-11-06 用户亲点：分级 / 折扣 / 进货 / 销售统计）。

用内存 SQLite，不碰生产库。金额路径（计价取整、余额扣减原子性、
余额不足整单拒绝、核销归属）是本文件的重点。
"""
from decimal import Decimal

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend import agents as agent_service
from backend import codes, models
from backend.api import agent_center, agents_admin


@pytest.fixture()
def db():
    engine = create_engine("sqlite://")
    models.Base.metadata.create_all(bind=engine)
    session = sessionmaker(bind=engine)()
    session.add(models.ServerRealm(id=1, name="默认服", slug="main", is_active=True))
    session.commit()
    try:
        yield session
    finally:
        session.close()


def _user(db, username, **kw):
    user = models.WebUser(username=username, password_hash="x",
                          is_active=True, **kw)
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


@pytest.fixture()
def admin(db):
    return _user(db, "admin_test", is_staff=True)


@pytest.fixture()
def plan(db):
    row = models.SubscriptionPlan(
        name="月卡", price=Decimal("100.00"), points_price=None,
        duration_days=30, realm_id=1, is_active=True)
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def _level(db, name="金牌代理", rate="0.8"):
    resp = agents_admin.create_level(
        agents_admin.LevelCreateRequest(
            name=name, discount_rate=float(rate), sort_order=1),
        _admin(db), db)
    return resp["level"]


def _admin(db):
    return db.query(models.WebUser).filter(
        models.WebUser.username == "admin_test").first()


@pytest.fixture()
def agent_user(db):
    return _user(db, "agent_a")


def _assign(db, user, level_id):
    return agents_admin.assign_agent(
        agents_admin.AgentAssignRequest(user_id=user.id, level_id=level_id),
        _admin(db), db)["agent"]


def _fund(db, agent_id, amount):
    return agents_admin.adjust_balance(
        agent_id,
        agents_admin.BalanceAdjustRequest(amount=str(amount), note="进货款"),
        _admin(db), db)


# ==================== 计价取整 ====================

def test_discounted_price_rounding_half_up():
    """进货单价 = 面值 × 折扣率，按分四舍五入（与全站 Numeric(10,2) 同口径）。"""
    assert agent_service.discounted_unit_price(
        Decimal("100"), Decimal("0.8")) == Decimal("80.00")
    assert agent_service.discounted_unit_price(
        Decimal("99.99"), Decimal("0.8")) == Decimal("79.99")
    # 16.665 → 16.67（HALF_UP，不是银行家舍入的 16.66）
    assert agent_service.discounted_unit_price(
        Decimal("33.33"), Decimal("0.5")) == Decimal("16.67")


# ==================== 等级管理 ====================

def test_level_crud_and_duplicate_rejected(db, admin):
    level = _level(db)
    assert level["name"] == "金牌代理"
    assert abs(level["discount_rate"] - 0.8) < 1e-9
    with pytest.raises(HTTPException) as exc:
        _level(db)
    assert exc.value.status_code == 400
    # 折扣率必须在 (0, 1]
    with pytest.raises(HTTPException):
        agents_admin.create_level(
            agents_admin.LevelCreateRequest(
                name="坏等级", discount_rate=1.5, sort_order=0),
            admin, db)


def test_level_delete_blocked_while_in_use(db, admin, agent_user):
    level = _level(db)
    _assign(db, agent_user, level["id"])
    with pytest.raises(HTTPException) as exc:
        agents_admin.delete_level(level["id"], admin, db)
    assert exc.value.status_code == 400


# ==================== 代理指定 / 取消 ====================

def test_assign_and_cancel(db, admin, agent_user):
    level = _level(db)
    agent = _assign(db, agent_user, level["id"])
    assert agent["is_active"] is True
    me = agent_center.agent_me(agent_user, db)
    assert me["is_agent"] is True
    assert me["agent"]["level_name"] == "金牌代理"

    agents_admin.update_agent(
        agent["id"],
        agents_admin.AgentUpdateRequest(is_active=False), admin, db)
    me = agent_center.agent_me(agent_user, db)
    assert me["is_agent"] is False
    # 取消后进货口被 403 挡住
    with pytest.raises(HTTPException) as exc:
        agent_center.list_products(agent_user, db)
    assert exc.value.status_code == 403


def test_non_agent_sees_nothing(db):
    plain = _user(db, "plain_user")
    me = agent_center.agent_me(plain, db)
    assert me["is_agent"] is False
    assert me["agent"] is None
    with pytest.raises(HTTPException) as exc:
        agent_center.my_codes(current_user=plain, db=db)
    assert exc.value.status_code == 403


# ==================== 余额与进货（金额路径） ====================

def test_purchase_deducts_balance_and_generates_codes(db, admin, agent_user, plan):
    level = _level(db)
    agent = _assign(db, agent_user, level["id"])
    _fund(db, agent["id"], "500")

    resp = agent_center.purchase(
        agent_center.PurchaseRequest(plan_id=plan.id, code_type=2, quantity=3),
        agent_user, db)
    assert len(resp["codes"]) == 3
    assert resp["purchase"]["total_cost"] == 240.0  # 3 × 100 × 0.8

    me = agent_center.agent_me(agent_user, db)
    assert me["agent"]["balance"] == 260.0

    rows = db.query(models.RegistrationCode).filter(
        models.RegistrationCode.agent_id == agent["id"]).all()
    assert len(rows) == 3
    for row in rows:
        assert row.source == "agent"
        assert row.days == 30
        assert row.realm_id == 1
        assert row.code_type == 2
        assert row.reward_type == "subscription"

    logs = db.query(models.AgentBalanceLog).filter(
        models.AgentBalanceLog.agent_id == agent["id"]).order_by(
        models.AgentBalanceLog.id).all()
    assert [(float(l.amount), float(l.balance_after), l.kind) for l in logs] == [
        (500.0, 500.0, "admin_adjust"), (-240.0, 260.0, "purchase")]


def test_purchase_insufficient_balance_rejected_atomically(db, admin, agent_user, plan):
    level = _level(db)
    agent = _assign(db, agent_user, level["id"])
    _fund(db, agent["id"], "100")  # 只够 1 张（单价 80），买 2 张必须整单拒绝

    with pytest.raises(HTTPException) as exc:
        agent_center.purchase(
            agent_center.PurchaseRequest(plan_id=plan.id, code_type=1, quantity=2),
            agent_user, db)
    assert exc.value.status_code == 400

    # 整单拒绝：没码、没进货单、余额没动、没 purchase 流水
    assert db.query(models.RegistrationCode).count() == 0
    assert db.query(models.AgentPurchase).count() == 0
    me = agent_center.agent_me(agent_user, db)
    assert me["agent"]["balance"] == 100.0
    kinds = [l.kind for l in db.query(models.AgentBalanceLog).all()]
    assert kinds == ["admin_adjust"]


def test_double_spend_cannot_go_negative(db, admin, agent_user, plan):
    level = _level(db)
    agent = _assign(db, agent_user, level["id"])
    _fund(db, agent["id"], "80")  # 正好够 1 张

    agent_center.purchase(
        agent_center.PurchaseRequest(plan_id=plan.id, code_type=1, quantity=1),
        agent_user, db)
    with pytest.raises(HTTPException):
        agent_center.purchase(
            agent_center.PurchaseRequest(plan_id=plan.id, code_type=1, quantity=1),
            agent_user, db)
    row = db.query(models.Agent).filter(models.Agent.id == agent["id"]).first()
    assert float(row.balance) == 0.0


def test_balance_adjust_below_zero_rejected(db, admin, agent_user):
    level = _level(db)
    agent = _assign(db, agent_user, level["id"])
    _fund(db, agent["id"], "50")
    with pytest.raises(HTTPException) as exc:
        _fund(db, agent["id"], "-80")
    assert exc.value.status_code == 400
    me = agent_center.agent_me(agent_user, db)
    assert me["agent"]["balance"] == 50.0


# ==================== 核销归属 ====================

def test_redeem_agent_code_records_sale(db, admin, agent_user, plan):
    level = _level(db)
    agent = _assign(db, agent_user, level["id"])
    _fund(db, agent["id"], "500")
    resp = agent_center.purchase(
        agent_center.PurchaseRequest(plan_id=plan.id, code_type=1, quantity=1),
        agent_user, db)
    raw = resp["codes"][0]["code"]

    buyer = _user(db, "buyer_1")
    result = codes.redeem_code(db, buyer, raw)
    assert result["success"] is True
    db.commit()

    sales = db.query(models.AgentSale).all()
    assert len(sales) == 1
    sale = sales[0]
    assert sale.agent_id == agent["id"]
    assert sale.buyer_user_id == buyer.id
    assert float(sale.face_value) == 100.0
    assert float(sale.unit_cost) == 80.0

    stats = agent_center.my_stats(agent_user, db)
    assert stats["summary"]["sales_count"] == 1
    assert stats["summary"]["sales_face_total"] == 100.0


def test_redeem_admin_code_records_no_sale(db, admin):
    code = models.RegistrationCode(
        code="REG-ADMIN-TEST-0001", code_type=2, days=30, realm_id=1,
        source="admin", max_uses=1, use_count=0, is_active=True)
    db.add(code)
    db.commit()
    buyer = _user(db, "buyer_2")
    result = codes.redeem_code(db, buyer, "REG-ADMIN-TEST-0001")
    assert result["success"] is True
    db.commit()
    assert db.query(models.AgentSale).count() == 0


# ==================== 数据隔离 ====================

def test_agent_data_isolated(db, admin, agent_user, plan):
    level = _level(db)
    agent_a = _assign(db, agent_user, level["id"])
    other = _user(db, "agent_b")
    agent_b = _assign(db, other, level["id"])
    _fund(db, agent_a["id"], "500")
    _fund(db, agent_b["id"], "500")
    agent_center.purchase(
        agent_center.PurchaseRequest(plan_id=plan.id, code_type=1, quantity=2),
        agent_user, db)

    mine = agent_center.my_codes(current_user=agent_user, db=db)
    assert mine["total"] == 2
    theirs = agent_center.my_codes(current_user=other, db=db)
    assert theirs["total"] == 0
    # 管理端能看到全量
    overview = agents_admin.overview(admin, db)
    assert overview["agent_count"] == 2
    assert overview["purchase_quantity"] == 2
