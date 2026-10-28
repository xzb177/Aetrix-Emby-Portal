"""公益到期检查：不活跃取消资格 + 过期保留期（worker 接线后实际执行的路径）。"""
import os
import sys
from datetime import datetime, timedelta

os.environ.setdefault("DATABASE_TYPE", "sqlite")
os.environ.setdefault("REDIS_ENABLED", "false")
sys.path.insert(0, ".")

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend import models, welfare_expiry
from backend.integrations import store as _store


def _db():
    engine = create_engine("sqlite://")
    models.Base.metadata.create_all(engine)
    s = sessionmaker(bind=engine)()
    _store.invalidate("welfare_inactive_days", "welfare_grace_days")
    return s


def _user(db, name, **kw):
    u = models.WebUser(username=name, password_hash="x", is_welfare=True, **kw)
    db.add(u)
    db.commit()
    return u


def test_inactive_30_days_loses_welfare():
    """30 天未登录 → 取消公益资格并记 auto_inactive；其余三类用户不动。"""
    db = _db()
    now = datetime.now()
    old = _user(db, "old", last_login_at=now - timedelta(days=40),
                welfare_expires_at=now + timedelta(days=30),
                welfare_granted_at=now - timedelta(days=40))
    recent = _user(db, "recent", last_login_at=now - timedelta(days=5),
                   welfare_expires_at=now + timedelta(days=30),
                   welfare_granted_at=now - timedelta(days=40))
    expired = _user(db, "expired", last_login_at=now - timedelta(days=40),
                    welfare_expires_at=now - timedelta(days=1),
                    welfare_granted_at=now - timedelta(days=40))
    never_login = _user(db, "never", last_login_at=None,
                        welfare_expires_at=now + timedelta(days=30),
                        welfare_granted_at=now)
    res = welfare_expiry.check_inactive_users(db)
    assert res["disabled"] == [old.id]
    assert db.query(models.WebUser).filter_by(id=old.id).one().is_welfare is False
    assert db.query(models.WebUser).filter_by(id=recent.id).one().is_welfare is True
    # 已过期用户由到期检查处理，不活跃检查跳过
    assert db.query(models.WebUser).filter_by(id=expired.id).one().is_welfare is True
    # 无登录记录时回落到资格授予日（今天），不算不活跃
    assert db.query(models.WebUser).filter_by(id=never_login.id).one().is_welfare is True
    log = db.query(models.WelfareGrantLog).filter_by(
        user_id=old.id, channel="auto_inactive").one()
    assert log.days == 0
    db.close()


def test_expiry_grace_days_then_auto_expire():
    """过期 7 天保留期内只记录不取消；超保留期自动取消并记 auto_expire。"""
    db = _db()
    now = datetime.now()
    beyond = _user(db, "beyond", last_login_at=now,
                   welfare_expires_at=now - timedelta(days=8),
                   welfare_granted_at=now - timedelta(days=40))
    within = _user(db, "within", last_login_at=now,
                   welfare_expires_at=now - timedelta(days=3),
                   welfare_granted_at=now - timedelta(days=40))
    res = welfare_expiry.check_welfare_expiry(db)
    assert res["expired"] == [beyond.id]
    assert res["grace"] == [within.id]
    assert db.query(models.WebUser).filter_by(id=beyond.id).one().is_welfare is False
    assert db.query(models.WebUser).filter_by(id=within.id).one().is_welfare is True
    assert db.query(models.WelfareGrantLog).filter_by(
        user_id=beyond.id, channel="auto_expire").count() == 1
    db.close()
