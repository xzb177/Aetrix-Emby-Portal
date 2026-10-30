"""C2 测试：积分转账。"""
from __future__ import annotations

import sys

import pytest

sys.path.insert(0, ".")

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend import models
from backend.api import economy


@pytest.fixture()
def db():
    engine = create_engine("sqlite:///:memory:")
    models.WebUser.__table__.create(engine, checkfirst=True)
    models.SystemConfig.__table__.create(engine, checkfirst=True)
    models.PointsLog.__table__.create(engine, checkfirst=True)
    Session = sessionmaker(bind=engine)
    session = Session()
    yield session
    session.close()


def _make_user(db, username="u1", points=1000, is_active=True):
    u = models.WebUser(username=username, password_hash="", points=points, is_active=is_active)
    db.add(u)
    db.commit()
    return u


def _set_cfg(db, key, value):
    cfg = db.query(models.SystemConfig).filter(models.SystemConfig.key == key).first()
    if cfg:
        cfg.value = str(value)
    else:
        db.add(models.SystemConfig(key=key, value=str(value)))
    db.commit()


def _logs_by_type(db):
    return {log.type: log.amount for log in db.query(models.PointsLog).all()}


def test_transfer_success(db):
    u1 = _make_user(db, "u1", 1000)
    u2 = _make_user(db, "u2", 0)
    res = economy.transfer_points_core(db, u1, "u2", 100)
    assert res["amount"] == 100
    assert res["fee"] == 5  # 5% 向上取整
    assert res["recipient"] == "u2"
    assert res["balance"] == 895
    db.refresh(u1)
    db.refresh(u2)
    assert u1.points == 895
    assert u2.points == 100
    logs = db.query(models.PointsLog).all()
    assert len(logs) == 3
    ref_ids = {log.ref_id for log in logs}
    assert len(ref_ids) == 1 and all(ref_ids)
    by_type = _logs_by_type(db)
    assert by_type["transfer_out"] == -100
    assert by_type["transfer_fee"] == -5
    assert by_type["transfer_in"] == 100


def test_transfer_fee_zero(db):
    _set_cfg(db, "points_transfer_fee_pct", 0)
    u1 = _make_user(db, "u1", 1000)
    u2 = _make_user(db, "u2", 0)
    res = economy.transfer_points_core(db, u1, "u2", 100)
    assert res["fee"] == 0
    assert res["balance"] == 900
    assert len(db.query(models.PointsLog).all()) == 2


def test_transfer_disabled(db):
    _set_cfg(db, "points_transfer_enabled", "0")
    u1 = _make_user(db, "u1", 1000)
    _make_user(db, "u2", 0)
    with pytest.raises(ValueError, match="已关闭"):
        economy.transfer_points_core(db, u1, "u2", 100)


def test_transfer_to_self(db):
    u1 = _make_user(db, "u1", 1000)
    with pytest.raises(ValueError, match="自己"):
        economy.transfer_points_core(db, u1, "U1", 100)


def test_transfer_nonexistent(db):
    u1 = _make_user(db, "u1", 1000)
    with pytest.raises(ValueError, match="不存在"):
        economy.transfer_points_core(db, u1, "ghost", 100)


def test_transfer_inactive(db):
    u1 = _make_user(db, "u1", 1000)
    _make_user(db, "u2", 0, is_active=False)
    with pytest.raises(ValueError, match="不存在"):
        economy.transfer_points_core(db, u1, "u2", 100)


def test_transfer_insufficient(db):
    u1 = _make_user(db, "u1", 100)
    _make_user(db, "u2", 0)
    with pytest.raises(ValueError, match="能量不足"):
        economy.transfer_points_core(db, u1, "u2", 100)  # 需 105


def test_transfer_min(db):
    _set_cfg(db, "points_transfer_min", 10)
    u1 = _make_user(db, "u1", 1000)
    _make_user(db, "u2", 0)
    with pytest.raises(ValueError, match="最少"):
        economy.transfer_points_core(db, u1, "u2", 5)


def test_transfer_max(db):
    _set_cfg(db, "points_transfer_max", 50)
    u1 = _make_user(db, "u1", 1000)
    _make_user(db, "u2", 0)
    with pytest.raises(ValueError, match="最多"):
        economy.transfer_points_core(db, u1, "u2", 60)
    res = economy.transfer_points_core(db, u1, "u2", 50)  # fee=3，需 53
    assert res["balance"] == 1000 - 53


def test_transfer_daily_cap(db):
    _set_cfg(db, "points_transfer_daily_cap", 100)
    u1 = _make_user(db, "u1", 1000)
    _make_user(db, "u2", 0)
    economy.transfer_points_core(db, u1, "u2", 60)  # 本金 60
    with pytest.raises(ValueError, match="额度"):
        economy.transfer_points_core(db, u1, "u2", 50)  # 60+50 > 100
    economy.transfer_points_core(db, u1, "u2", 40)  # 60+40 <= 100
    db.refresh(u1)
    assert u1.points == 1000 - (60 + 3) - (40 + 2)


def test_transfer_non_positive(db):
    u1 = _make_user(db, "u1", 1000)
    _make_user(db, "u2", 0)
    with pytest.raises(ValueError, match="正整数"):
        economy.transfer_points_core(db, u1, "u2", 0)
    with pytest.raises(ValueError, match="正整数"):
        economy.transfer_points_core(db, u1, "u2", -5)


def test_transfer_config_defaults(db):
    u1 = _make_user(db, "u1", 1000)
    _make_user(db, "u2", 0)
    res = economy.transfer_points_core(db, u1, "u2", 200)
    assert res["fee"] == 10
    assert res["balance"] == 790
