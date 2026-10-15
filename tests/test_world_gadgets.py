# -*- coding: utf-8 -*-
"""探险世界 · 留言墙 / 抽一签 / 隐私 / 告示牌回归测试。

隔离：内存 SQLite + 直接调用路由函数，不碰开发库/生产库。
"""
import os

os.environ.setdefault("DATABASE_TYPE", "sqlite")
os.environ.setdefault("REDIS_ENABLED", "false")

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend import models
from backend.emby_server import models as em
from backend.api import panel as panel_api
from backend.integrations import world_fortune, world_board


@pytest.fixture()
def db():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    models.Base.metadata.create_all(engine)
    em.Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    try:
        yield session
    finally:
        session.rollback()
        session.close()
        engine.dispose()


def _user(db, name, n=0):
    from backend.ratelimit import _limiter
    u = models.WebUser(username=f"{name}_{n}", password_hash="x", is_active=True)
    db.add(u)
    db.commit()
    db.refresh(u)
    # 限流器是进程全局的：测试各自用新库但 user.id 会重复，清掉本用户的墙限流键防串扰
    _limiter.reset(f"wall:{u.id}")
    return u


def _ok_data(res):
    assert res["code"] == 0
    return res["data"]


# ---------- 留言墙 ----------

def test_wall_post_and_list(db):
    u = _user(db, "wall", 1)
    res = _ok_data(panel_api.wall_post(panel_api.WallPostIn(content="到此一游"), db=db, user=u))
    assert res["id"]
    data = _ok_data(panel_api.wall_list(db=db, user=u))
    assert len(data["posts"]) == 1
    p = data["posts"][0]
    assert p["content"] == "到此一游" and p["nickname"] == u.username and p["mine"] is True


def test_wall_post_validation(db):
    u = _user(db, "wall", 2)
    with pytest.raises(HTTPException) as e:
        panel_api.wall_post(panel_api.WallPostIn(content="   "), db=db, user=u)
    assert e.value.status_code == 400
    with pytest.raises(HTTPException) as e:
        panel_api.wall_post(panel_api.WallPostIn(content="x" * 61), db=db, user=u)
    assert e.value.status_code == 400


def test_wall_rate_limit_10min_1(db):
    u = _user(db, "wall", 3)
    _ok_data(panel_api.wall_post(panel_api.WallPostIn(content="第一条"), db=db, user=u))
    with pytest.raises(HTTPException) as e:
        panel_api.wall_post(panel_api.WallPostIn(content="第二条"), db=db, user=u)
    assert e.value.status_code == 429


def test_wall_post_60_chars_ok(db):
    u = _user(db, "wall", 4)
    res = _ok_data(panel_api.wall_post(panel_api.WallPostIn(content="x" * 60), db=db, user=u))
    assert res["id"]


# ---------- 抽一签 ----------

def test_fortune_draw_once_per_day(db):
    u = _user(db, "fort", 1)
    assert _ok_data(panel_api.fortune_today(db=db, user=u))["drew"] is False
    d1 = _ok_data(panel_api.fortune_draw(db=db, user=u))
    assert d1["drew"] is True and d1["level"] and d1["text"]
    assert _ok_data(panel_api.fortune_today(db=db, user=u))["drew"] is True
    # 当天再抽：幂等，同一条
    d2 = _ok_data(panel_api.fortune_draw(db=db, user=u))
    assert (d2["level"], d2["text"]) == (d1["level"], d1["text"])
    n = db.query(em.WorldFortuneDraw).filter_by(user_id=u.id).count()
    assert n == 1


def test_fortune_pool_parses_30(db):
    pool = world_fortune.parse_pool(world_fortune.DEFAULT_POOL)
    assert len(pool) == 30
    assert all(p["level"] in world_fortune.ALLOWED_LEVELS and p["text"] for p in pool)
    # 非法行被跳过
    assert world_fortune.parse_pool("瞎写一行\n上上签 | 好 | 宜 | 忌") == [
        {"level": "上上签", "text": "好", "yi": "宜", "ji": "忌"}
    ]


# ---------- 隐私 ----------

def test_privacy_default_on_and_toggle(db):
    u = _user(db, "priv", 1)
    assert _ok_data(panel_api.get_privacy(db=db, user=u))["show_on_leaderboard"] is True
    _ok_data(panel_api.set_privacy(panel_api.PrivacyIn(show_on_leaderboard=False), db=db, user=u))
    db.refresh(u)
    assert _ok_data(panel_api.get_privacy(db=db, user=u))["show_on_leaderboard"] is False


# ---------- 告示牌 ----------

def test_board_default_hidden(db):
    info = world_board.public_info(db)
    assert info["visible"] is False and info["text"] == ""
