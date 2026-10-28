"""welfare_expiry 键集分页回归测试。

check_welfare_expiry / check_inactive_users 改为键集分页（id > last_id）后，
必须保证：跨批时因 is_welfare=False 而从结果集消失的用户不会导致漏扫。
用小 _SCAN_BATCH 强制多批，过期用户交错分布在批次边界上。
"""
from datetime import datetime, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend import models
from backend.models import Base
from backend import welfare_expiry


@pytest.fixture()
def db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    s = sessionmaker(bind=engine)()
    yield s
    s.close()


def _seed_expiry(db, n):
    """n 个公益用户，i%4==0 的已过期超保留期（应被取消），其余不动。"""
    now = datetime.now()
    ids_should_expire = []
    for i in range(n):
        mod = i % 4
        exp = (now - timedelta(days=30) if mod == 0
               else now - timedelta(days=3) if mod == 1
               else now + timedelta(days=30) if mod == 2
               else None)
        u = models.WebUser(username=f"kx{i}", password_hash="x",
                           is_welfare=True, welfare_expires_at=exp)
        db.add(u)
        db.flush()
        if mod == 0:
            ids_should_expire.append(u.id)
    db.commit()
    return ids_should_expire


def test_expiry_keyset_scan_no_missed_users(db, monkeypatch):
    monkeypatch.setattr(welfare_expiry, "_SCAN_BATCH", 50)
    should_expire = _seed_expiry(db, 130)  # 3 批，过期用户横跨批次边界
    res = welfare_expiry.check_welfare_expiry(db)
    assert res["checked"] == 130
    assert sorted(res["expired"]) == sorted(should_expire)
    assert len(res["grace"]) == len(should_expire)  # i%4==1 与 i%4==0 同数（130=4*32+2）
    # DB 侧二次确认：没有任何应过期用户残留
    now = datetime.now()
    missed = db.query(models.WebUser).filter(
        models.WebUser.is_welfare == True,
        models.WebUser.welfare_expires_at.isnot(None),
        models.WebUser.welfare_expires_at <= now - timedelta(days=7),
    ).count()
    assert missed == 0
    # 未过期 / 永久用户不受影响
    assert db.query(models.WebUser).filter(models.WebUser.is_welfare == True).count() == 130 - len(should_expire)


def test_inactive_keyset_scan_no_missed_users(db, monkeypatch):
    monkeypatch.setattr(welfare_expiry, "_SCAN_BATCH", 50)
    now = datetime.now()
    should_disable = []
    for i in range(130):
        old = (i % 3 == 0)
        u = models.WebUser(
            username=f"ki{i}", password_hash="x", is_welfare=True,
            welfare_expires_at=now + timedelta(days=30),
            last_login_at=now - timedelta(days=60) if old else now,
            welfare_granted_at=now - timedelta(days=60),
        )
        db.add(u)
        db.flush()
        if old:
            should_disable.append(u.id)
    db.commit()
    res = welfare_expiry.check_inactive_users(db)
    assert res["checked"] == 130
    assert sorted(res["disabled"]) == sorted(should_disable)
    missed = db.query(models.WebUser).filter(
        models.WebUser.is_welfare == True,
        models.WebUser.last_login_at <= now - timedelta(days=30),
    ).count()
    assert missed == 0


def test_expiry_result_shape_unchanged(db):
    """返回结构：checked/grace/expired + 连签保资格新增的 renewed。

    2026-10-28 保号 A+B：形状有意多一个 renewed（本轮续期的用户 id），
    供 worker tick 日志观测续期；其余三键语义不变。
    """
    _seed_expiry(db, 10)
    res = welfare_expiry.check_welfare_expiry(db)
    assert set(res.keys()) == {"checked", "grace", "expired", "renewed"}
    res2 = welfare_expiry.check_inactive_users(db)
    assert set(res2.keys()) == {"checked", "disabled"}
