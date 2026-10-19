# -*- coding: utf-8 -*-
"""revisit「回头」语义口径钉死测试：用户 2026-10-19 拍板——保持现行算法
（同一部片**相邻两次**播放间隔 ≥30 天才算沉寂回头，只改文案对齐行为）。

行为代码未动，这三例把口径锁死：
- 0/29/58 天各看一次（相邻间隔 29/28 天）→ 不发放
- 0/31 天两次（相邻间隔 31 天）→ 发放
- 0/120 天（120 天前那次已出 90 天窗口，只剩一次）→ 不发放
"""
import os

os.environ.setdefault("DATABASE_TYPE", "sqlite")
os.environ.setdefault("REDIS_ENABLED", "false")

from datetime import datetime, timedelta

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import pytest

from backend import models
from backend import panel_achievements as pa


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


@pytest.fixture()
def user(db):
    u = models.WebUser(username="revisit_test", password_hash="x", is_active=True)
    db.add(u)
    db.commit()
    return u


def _mk_session(db, user, name, days_ago, duration=300, tag=""):
    s = models.EmbySession(
        id=f"sess-revisit-{days_ago}-{tag or name}",
        user_id=user.id,
        item_name=name,
        item_type="movie",
        start_time=datetime.now() - timedelta(days=days_ago),
        play_duration=duration,
    )
    db.add(s)
    return s


def test_adjacent_29_28_days_no_grant(db, user):
    """0/29/58 天各看一次：相邻间隔 29/28 天，不满 30 天 → 不发放。"""
    pa.seed_achievement_defs(db)
    _mk_session(db, user, "回头测试片", days_ago=58, tag="a")
    _mk_session(db, user, "回头测试片", days_ago=29, tag="b")
    _mk_session(db, user, "回头测试片", days_ago=0, tag="c")
    db.commit()
    assert "revisit" not in pa.evaluate(user.id, db)


def test_adjacent_31_days_grant(db, user):
    """0/31 天两次：相邻间隔 31 天 → 发放。"""
    pa.seed_achievement_defs(db)
    _mk_session(db, user, "回头测试片", days_ago=31, tag="a")
    _mk_session(db, user, "回头测试片", days_ago=0, tag="b")
    db.commit()
    assert "revisit" in pa.evaluate(user.id, db)


def test_outside_90d_window_no_grant(db, user):
    """0/120 天两次：120 天前那次已出 90 天窗口 → 不发放。"""
    pa.seed_achievement_defs(db)
    _mk_session(db, user, "回头测试片", days_ago=120, tag="a")
    _mk_session(db, user, "回头测试片", days_ago=0, tag="b")
    db.commit()
    assert "revisit" not in pa.evaluate(user.id, db)
