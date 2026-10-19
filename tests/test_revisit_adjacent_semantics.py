# -*- coding: utf-8 -*-
"""revisit「回头」语义口径钉死测试：用户 2026-10-19 拍板——保持现行算法
（同一部片**相邻两次**播放间隔 ≥30 天才算沉寂回头，只改文案对齐行为）。

2026-10-19 #1120 换源注：数据源从 emby_sessions（全仓无写入的死表）
换成 emby_playback_sessions，fixture 随之改写为「MediaItem＋PlaybackSession」；
三例的发放/不发放断言不变，口径仍按用户拍板锁死：
- 0/29/58 天各看一次（相邻间隔 29/28 天）→ 不发放
- 0/31 天两次（相邻间隔 31 天）→ 发放
- 0/120 天（120 天前那次已出 90 天窗口，只剩一次）→ 不发放

另注：生产里 emby_playback_sessions 每 (user, item) 只保留一行
（EA 兼容层 upsert 复用会话键），同一部片的多次播放时间点当前不可得——
发放那例在测试库里直插两行以锁定算法语义，不代表生产当前能触发
（待「逐条播放历史」能力，用户待拍板清单里仍挂着）。
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


_seq = [0]


def _mk_item(db, name):
    from backend.emby_server import models as em
    _seq[0] += 1
    n = _seq[0]
    item = em.MediaItem(
        guid=f"revisit-item-{n:06d}",
        library_id=1, item_type="movie", name=name,
        date_added=datetime.now(),
    )
    db.add(item)
    db.commit()
    db.refresh(item)
    return item


def _mk_play(db, user, item, days_ago, duration=300):
    from backend.emby_server import models as em
    _seq[0] += 1
    n = _seq[0]
    s = em.PlaybackSession(
        session_key=f"revisit-sess-{n:06d}",
        user_id=user.id,
        item_id=item.id,
        start_time=datetime.now() - timedelta(days=days_ago),
        last_update_at=datetime.now() - timedelta(days=days_ago),
        position_ticks=int(duration * 10_000_000),
    )
    db.add(s)
    db.commit()
    return s


def test_adjacent_29_28_days_no_grant(db, user):
    """0/29/58 天各看一次：相邻间隔 29/28 天，不满 30 天 → 不发放。"""
    pa.seed_achievement_defs(db)
    item = _mk_item(db, "回头测试片")
    _mk_play(db, user, item, days_ago=58)
    _mk_play(db, user, item, days_ago=29)
    _mk_play(db, user, item, days_ago=0)
    db.commit()
    assert "revisit" not in pa.evaluate(user.id, db)


def test_adjacent_31_days_grant(db, user):
    """0/31 天两次：相邻间隔 31 天 → 发放。"""
    pa.seed_achievement_defs(db)
    item = _mk_item(db, "回头测试片")
    _mk_play(db, user, item, days_ago=31)
    _mk_play(db, user, item, days_ago=0)
    db.commit()
    assert "revisit" in pa.evaluate(user.id, db)


def test_outside_90d_window_no_grant(db, user):
    """0/120 天两次：120 天前那次已出 90 天窗口 → 不发放。"""
    pa.seed_achievement_defs(db)
    item = _mk_item(db, "回头测试片")
    _mk_play(db, user, item, days_ago=120)
    _mk_play(db, user, item, days_ago=0)
    db.commit()
    assert "revisit" not in pa.evaluate(user.id, db)
