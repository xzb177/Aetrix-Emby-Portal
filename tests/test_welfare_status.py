"""用户侧福利状态口：只返回本人福利资格与到期日。"""
import os
import sys
from datetime import datetime

os.environ.setdefault("DATABASE_TYPE", "sqlite")
os.environ.setdefault("REDIS_ENABLED", "false")
sys.path.insert(0, ".")

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend import models
from backend import points as points_mod


def _db():
    engine = create_engine("sqlite://")
    models.Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)()


def _user(db, name, **kw):
    u = models.WebUser(username=name, password_hash="x", **kw)
    db.add(u)
    db.commit()
    return u


def test_welfare_user_with_expiry():
    db = _db()
    exp = datetime(2026, 11, 27, 10, 0, 0)
    u = _user(db, "w", is_welfare=True, welfare_expires_at=exp)
    res = points_mod.get_welfare_status(db, u)
    assert res["is_welfare"] is True
    assert res["welfare_expires_at"] == exp.isoformat()
    db.close()


def test_welfare_user_without_expiry():
    db = _db()
    u = _user(db, "w2", is_welfare=True, welfare_expires_at=None)
    res = points_mod.get_welfare_status(db, u)
    assert res["is_welfare"] is True
    assert res["welfare_expires_at"] is None
    db.close()


def test_non_welfare_user():
    db = _db()
    u = _user(db, "n", is_welfare=False, welfare_expires_at=None)
    res = points_mod.get_welfare_status(db, u)
    assert res["is_welfare"] is False
    assert res["welfare_expires_at"] is None
    db.close()


def test_missing_user_row():
    db = _db()
    ghost = models.WebUser(id=999999, username="ghost", password_hash="x")
    res = points_mod.get_welfare_status(db, ghost)
    assert res == {"is_welfare": False, "welfare_expires_at": None}
    db.close()
