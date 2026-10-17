# -*- coding: utf-8 -*-
"""成就时区口径回归测试（2026-10-17 用户拍板：成就按北京时间算）。

复现证据：播放会话 start_time 以服务器本地时间（UTC）naive 入库；
北京时间 02:00（= UTC 前一天 18:00）这个时刻，在旧代码里 hour 取 18、
date 取 UTC 日期，在新代码里 hour 取 2、date 取北京时间日期。
本文件钉住新口径。
"""
import os

os.environ.setdefault("DATABASE_TYPE", "sqlite")
os.environ.setdefault("REDIS_ENABLED", "false")

from datetime import datetime, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

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
    try:
        from backend.emby_server import models as em
        em.Base.metadata.create_all(engine)
    except Exception:
        pass
    session = sessionmaker(bind=engine)()
    try:
        yield session
    finally:
        session.rollback()
        session.close()
        engine.dispose()


@pytest.fixture()
def user(db):
    u = models.WebUser(username="tz_shanghai_test", password_hash="x", is_active=True)
    db.add(u)
    db.commit()
    db.refresh(u)
    return u


def _bj_to_storage(bj_hour, bj_minute=0, days_ago=0):
    """北京时间墙钟 → 入库存储帧（UTC naive），与生产写入帧同。"""
    bj = datetime.now() + timedelta(hours=8)
    t = bj.replace(hour=bj_hour, minute=bj_minute, second=0, microsecond=0)
    if days_ago:
        t -= timedelta(days=days_ago)
    return t - timedelta(hours=8)


def _mk_session(db, user, name, start, duration):
    s = models.EmbySession(
        id=f"sess-tz-{name}-{start.isoformat()}",
        user_id=user.id,
        item_name=name,
        item_type="movie",
        start_time=start,
        play_duration=duration,
    )
    db.add(s)
    db.commit()
    return s


def test_beijing_helper_fixed_conversion():
    """固定复现时刻：北京时间 2026-01-15 02:00 = UTC 2026-01-14 18:00。"""
    assert pa._beijing(datetime(2026, 1, 14, 18, 0)) == datetime(2026, 1, 15, 2, 0)
    assert pa._beijing(None) is None


def test_first_night_uses_beijing_hour(db, user):
    """北京时间 02:00 开始播放（入库 UTC 前一天 18:00）→ 初夜应授予。

    旧口径下 hour 取 18，不会授予——这是本轮要修的错位。
    """
    pa.seed_achievement_defs(db)
    _mk_session(db, user, "凌晨电影", _bj_to_storage(2, 0), 3600)
    new = pa.evaluate(user.id, db)
    assert "first_night" in new


def test_night_owl_uses_beijing_day_boundary(db, user):
    """两段 20000 秒播放：北京时间今天 01:00 与昨天 20:00，
    在 UTC 帧里落在同一天（都会被旧口径计入同一天授予熬夜），
    在北京时间里跨天 → 熬夜不应授予。
    """
    pa.seed_achievement_defs(db)
    _mk_session(db, user, "跨日A", _bj_to_storage(1, 0, days_ago=0), 20000)
    _mk_session(db, user, "跨日B", _bj_to_storage(20, 0, days_ago=1), 20000)
    new = pa.evaluate(user.id, db)
    assert "night_owl" not in new
    # 同北京时间自然日的两段播放应计入同一天
    _mk_session(db, user, "同日A", _bj_to_storage(14, 0), 20000)
    _mk_session(db, user, "同日B", _bj_to_storage(18, 0), 20000)
    new2 = pa.evaluate(user.id, db)
    assert "night_owl" in new2


def test_early_bird_uses_beijing_hour(db, user):
    """北京时间 06:30（入库 UTC 前一天 22:30）→ 早鸟应授予。"""
    pa.seed_achievement_defs(db)
    _mk_session(db, user, "清晨电影", _bj_to_storage(6, 30), 3600)
    new = pa.evaluate(user.id, db)
    assert "early_bird" in new
