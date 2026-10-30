"""P0 统一货币体系：红包手续费与频率限制测试

全部用隔离的内存 SQLite，不碰生产库。
"""
import os

os.environ.setdefault("DATABASE_TYPE", "sqlite")
os.environ.setdefault("REDIS_ENABLED", "false")

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend import models
from backend import welfare_redpacket as rp


@pytest.fixture()
def db():
    engine = create_engine("sqlite://")
    models.Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    yield session
    session.close()


def _make_user(db, username, points=10000, is_staff=False):
    u = models.WebUser(
        username=username, password_hash="x", points=points, is_staff=is_staff)
    db.add(u)
    db.commit()
    db.refresh(u)
    return u


def _set_config(db, key, value):
    cfg = db.query(models.SystemConfig).filter(
        models.SystemConfig.key == key).first()
    if cfg:
        cfg.value = str(value)
    else:
        db.add(models.SystemConfig(key=key, value=str(value)))
    db.commit()


def _fee_logs(db, user_id):
    return db.query(models.PointsLog).filter(
        models.PointsLog.user_id == user_id,
        models.PointsLog.type == "redpacket_fee",
    ).all()


# ---------- 手续费 ----------

def test_send_deducts_fee(db):
    """默认 5% 手续费：发 100 实际扣 105，手续费单独记账"""
    u = _make_user(db, "sender1")
    rp.send_packet(db, u, 100, 2)
    db.refresh(u)
    assert u.points == 10000 - 105
    fees = _fee_logs(db, u.id)
    assert len(fees) == 1
    assert fees[0].amount == -5


def test_send_fee_rounds_up(db):
    """手续费向上取整：发 10 按 5% 应收 0.5，实际收 1"""
    u = _make_user(db, "sender2")
    rp.send_packet(db, u, 10, 1)
    db.refresh(u)
    assert u.points == 10000 - 11
    fees = _fee_logs(db, u.id)
    assert fees[0].amount == -1


def test_send_no_fee_when_disabled(db):
    """redpacket_fee_pct=0 时不收手续费"""
    _set_config(db, "redpacket_fee_pct", "0")
    u = _make_user(db, "sender3")
    rp.send_packet(db, u, 100, 2)
    db.refresh(u)
    assert u.points == 10000 - 100
    assert _fee_logs(db, u.id) == []


def test_insufficient_balance_includes_fee(db):
    """余额不足提示要包含手续费"""
    u = _make_user(db, "sender4", points=102)
    with pytest.raises(ValueError, match="能量不足"):
        rp.send_packet(db, u, 100, 2)


# ---------- 发送频率限制 ----------

def test_send_limit_blocks(db):
    """7 天内发送达上限后拒绝"""
    _set_config(db, "redpacket_send_limit_7d", "2")
    u = _make_user(db, "sender5")
    rp.send_packet(db, u, 10, 1)
    rp.send_packet(db, u, 10, 1)
    with pytest.raises(ValueError, match="最多发送"):
        rp.send_packet(db, u, 10, 1)


def test_send_limit_zero_unlimited(db):
    """发送限制填 0 表示不限"""
    _set_config(db, "redpacket_send_limit_7d", "0")
    u = _make_user(db, "sender6")
    for _ in range(3):
        rp.send_packet(db, u, 10, 1)
    # 不抛异常即通过


# ---------- 领取频率限制 ----------

def _make_claimable_packet(db, sender, amount=10):
    """创建一个可领的红包（1 个名额，金额确定）"""
    return rp.send_packet(db, sender, amount, 1)


def test_claim_limit_blocks(db):
    """7 天内领取达上限后拒绝"""
    _set_config(db, "redpacket_recv_limit_7d", "2")
    sender = _make_user(db, "csender1")
    user = _make_user(db, "claimer1", points=0)
    p1 = _make_claimable_packet(db, sender)
    p2 = _make_claimable_packet(db, sender)
    p3 = _make_claimable_packet(db, sender)
    rp.claim_packet(db, user, p1.id)
    rp.claim_packet(db, user, p2.id)
    with pytest.raises(ValueError, match="最多领取"):
        rp.claim_packet(db, user, p3.id)


def test_claim_admin_packets_not_counted(db):
    """管理员发出的红包不计入领取频率"""
    _set_config(db, "redpacket_recv_limit_7d", "1")
    admin = _make_user(db, "admin1", is_staff=True)
    sender = _make_user(db, "csender2")
    user = _make_user(db, "claimer2", points=0)
    # 领管理员的红包（不计入）
    pa = _make_claimable_packet(db, admin)
    rp.claim_packet(db, user, pa.id)
    # 再领普通用户的（计入 1 次，未超限）
    p1 = _make_claimable_packet(db, sender)
    rp.claim_packet(db, user, p1.id)
    # 第 2 个普通红包才超限
    p2 = _make_claimable_packet(db, sender)
    with pytest.raises(ValueError, match="最多领取"):
        rp.claim_packet(db, user, p2.id)


def test_claim_limit_zero_unlimited(db):
    """领取限制填 0 表示不限"""
    _set_config(db, "redpacket_recv_limit_7d", "0")
    sender = _make_user(db, "csender3")
    user = _make_user(db, "claimer3", points=0)
    for _ in range(3):
        p = _make_claimable_packet(db, sender)
        rp.claim_packet(db, user, p.id)
    # 不抛异常即通过
