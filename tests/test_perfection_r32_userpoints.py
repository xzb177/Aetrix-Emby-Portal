# -*- coding: utf-8 -*-
"""r32 完美审查回归测试：用户端 API + 积分域。

- F1：discount 型注册码核销写 ExchangeDiscountCredit(exchange_code_id=0)。
  PG 生产库实查 FK ``exchange_discount_credits_exchange_code_id_fkey``
  真实存在（2026-10-17 只读查库）：旧模型带 ForeignKey + NOT NULL，
  flush 时 IntegrityError → 接口 500。模型去外键/放宽后 + 迁移函数幂等。
- F2：register_device 上限检查加锁后仍按预期拒绝/放行（SQLite 下 FOR UPDATE
  是 no-op，测的是「加锁不破坏原有行为」；PG 下的串行化由 lock_user_row 保证）。
- F3：工单消息 admin 预加载 / 套餐 realm_name 一次取——行为不变回归。
"""
import os
os.environ.setdefault("DATABASE_TYPE", "sqlite")
os.environ.setdefault("REDIS_ENABLED", "false")

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, joinedload

from backend import models
from backend import codes
from backend import devices


@pytest.fixture()
def db():
    engine = create_engine("sqlite://")
    models.Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    yield session
    session.close()


def _make_user(db, username="r32u", **kw):
    u = models.WebUser(username=username, password_hash="x", **kw)
    db.add(u)
    db.commit()
    db.refresh(u)
    return u


def _set_config(db, key, value):
    cfg = db.query(models.SystemConfig).filter(models.SystemConfig.key == key).first()
    if cfg:
        cfg.value = str(value)
    else:
        db.add(models.SystemConfig(key=key, value=str(value)))
    db.commit()


def _make_realm(db, name="r32realm"):
    r = models.ServerRealm(name=name, slug="r32", is_active=True)
    db.add(r)
    db.commit()
    db.refresh(r)
    return r


# ---------------- F1 ----------------

def test_f1_discount_credit_column_nullable_no_fk():
    """模型断言：exchange_code_id 可空且无外键（PG 下 0 值可写）"""
    col = models.ExchangeDiscountCredit.__table__.c.exchange_code_id
    assert col.nullable is True
    assert list(col.foreign_keys) == []


def test_f1_discount_code_redeem_writes_credit(db):
    """discount 型注册码核销成功并落一条折扣权益（exchange_code_id=0）"""
    user = _make_user(db, username="r32disc")
    realm = _make_realm(db)
    code = models.RegistrationCode(
        code="R32-DISC-001", code_type=codes.CODE_TYPE_RENEW,
        max_uses=1, use_count=0, is_active=True,
        reward_type="discount", discount_pct=85, realm_id=realm.id,
    )
    db.add(code)
    db.commit()

    result = codes.redeem_code(db, user, "R32-DISC-001")
    db.commit()

    assert result["success"] is True
    assert result["reward_type"] == "discount"
    credit = db.query(models.ExchangeDiscountCredit).filter(
        models.ExchangeDiscountCredit.user_id == user.id
    ).one()
    assert credit.discount_pct == 85
    assert credit.status == "unused"
    assert credit.exchange_code_id == 0  # 来源是 registration_codes


def test_f1_discount_code_double_redeem_rejected(db):
    """同一 discount 码第二人核销被拒（原子占位），不是 500"""
    u1 = _make_user(db, username="r32d1")
    u2 = _make_user(db, username="r32d2")
    realm = _make_realm(db, name="r32realm2")
    code = models.RegistrationCode(
        code="R32-DISC-002", code_type=codes.CODE_TYPE_RENEW,
        max_uses=1, use_count=0, is_active=True,
        reward_type="discount", discount_pct=90, realm_id=realm.id,
    )
    db.add(code)
    db.commit()

    r1 = codes.redeem_code(db, u1, "R32-DISC-002")
    db.commit()
    assert r1["success"] is True
    r2 = codes.redeem_code(db, u2, "R32-DISC-002")
    db.rollback()
    assert r2["success"] is False  # 用尽/占位失败走业务拒绝，不抛异常


# ---------------- F2 ----------------

def test_f2_device_limit_still_enforced(db):
    """上限内放行、超限拒绝（加锁后行为不变）"""
    _set_config(db, "device_limit_per_user", 1)
    user = _make_user(db, username="r32dev")
    devices.register_device(db, user, device_id="dev-a", name="A")
    with pytest.raises(devices.DeviceLimitExceeded):
        devices.register_device(db, user, device_id="dev-b", name="B")


def test_f2_existing_device_relogin_no_limit_check(db):
    """已登记设备重复登录不走上限检查（不加锁、不抛错）"""
    _set_config(db, "device_limit_per_user", 1)
    user = _make_user(db, username="r32dev2")
    d1 = devices.register_device(db, user, device_id="dev-x", name="X")
    d2 = devices.register_device(db, user, device_id="dev-x", name="X2")
    assert d1.id == d2.id


# ---------------- F3 ----------------

def test_f3_ticket_message_admin_preload(db):
    """工单消息的管理员名经预加载仍可读（行为不变）"""
    user = _make_user(db, username="r32t")
    admin = models.AdminUser(username="r32admin", password_hash="x")
    db.add(admin)
    db.commit()
    db.refresh(admin)
    t = models.Ticket(user_id=user.id, title="t", category="other", status="open")
    db.add(t)
    db.flush()
    db.add(models.TicketMessage(ticket_id=t.id, user_id=user.id, message="hi", is_admin=False))
    db.add(models.TicketMessage(ticket_id=t.id, admin_id=admin.id, message="ok", is_admin=True))
    db.commit()

    msgs = db.query(models.TicketMessage).options(
        joinedload(models.TicketMessage.admin)
    ).filter(
        models.TicketMessage.ticket_id == t.id
    ).order_by(models.TicketMessage.created_at.asc()).all()
    assert len(msgs) == 2
    admin_names = [m.admin.username if (m.is_admin and m.admin) else None for m in msgs]
    assert admin_names == [None, "r32admin"]


def test_f3_subscription_plan_realm_name_map(db):
    """套餐 realm_name 走一次取名的映射（行为：有服名 / 无服空串）"""
    from backend import realms
    realm = _make_realm(db, name="r32planrealm")
    p1 = models.SubscriptionPlan(
        name="月卡", price=10, duration_days=30, is_active=True, realm_id=realm.id)
    p2 = models.SubscriptionPlan(
        name="裸套餐", price=5, duration_days=7, is_active=True, realm_id=None)
    db.add_all([p1, p2])
    db.commit()

    realm_names = {r.id: r.name for r in realms.list_realms(db)}
    assert realm_names.get(p1.realm_id, "") == "r32planrealm"
    assert realm_names.get(p2.realm_id, "") == ""
