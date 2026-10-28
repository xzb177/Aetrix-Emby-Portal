"""群发言接回发言奖励（2026-10-28）：群内普通消息按发言计分。

接线位置：router.dispatch 群内非命令文本路径，口令抽奖未命中后调
points.award_chat_points（默认 1 分/条、每天 20 分封顶，生产无需改配置）。
群白名单由 dispatch 上游 is_group_allowed 拦截；普通聊天不消耗
_group_allow 群级限流预算；命令、私聊、口令命中、未绑定、未放行群均不发分。
"""
from __future__ import annotations

import os
import sys

os.environ.setdefault("DATABASE_TYPE", "sqlite")
os.environ.setdefault("REDIS_ENABLED", "false")
sys.path.insert(0, ".")

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend import models
from backend.integrations import store
from backend.tg_bot import callbacks, router, sender


@pytest.fixture()
def db():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool,
    )
    models.Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    store.invalidate()
    yield session
    session.close()
    store.invalidate()


@pytest.fixture(autouse=True)
def _no_send(monkeypatch):
    monkeypatch.setattr(
        sender, "send_message",
        lambda db_, chat_id, text, reply_markup=None, **k: (True, None),
    )


def _make_user(db, username, tg_id, points=0):
    u = models.WebUser(username=username, telegram_id=tg_id)
    if hasattr(u, "password_hash") and getattr(u, "password_hash") is None:
        u.password_hash = ""
    u.points = points
    db.add(u)
    db.commit()
    return u


def _update(chat_type, chat_id, from_id, text):
    return {"update_id": 1, "message": {
        "message_id": 77, "text": text,
        "chat": {"id": chat_id, "type": chat_type},
        "from": {"id": from_id, "first_name": "a"},
    }}


def _chat_rows(db, user_id):
    return db.query(models.PointsLog).filter(
        models.PointsLog.user_id == user_id,
        models.PointsLog.type == "chat",
    ).all()


def _balance(db, user_id):
    db.expire_all()
    return db.query(models.WebUser.points).filter(
        models.WebUser.id == user_id).scalar()


def test_group_message_awards_bound_user(db):
    u = _make_user(db, "chat1", 4242)
    router.dispatch(db, _update("supergroup", -100123, 4242, "hello 大家"))
    assert _balance(db, u.id) == 1
    rows = _chat_rows(db, u.id)
    assert len(rows) == 1 and rows[0].amount == 1


def test_daily_cap_stops_at_20(db):
    u = _make_user(db, "chat2", 4243)
    for _ in range(21):
        router.dispatch(db, _update("supergroup", -100123, 4243, "水群"))
    assert _balance(db, u.id) == 20
    assert len(_chat_rows(db, u.id)) == 20


def test_lottery_hit_no_double_award(db, monkeypatch):
    u = _make_user(db, "chat3", 4244)
    monkeypatch.setattr(callbacks, "handle_lottery_password", lambda *a, **k: True)
    router.dispatch(db, _update("supergroup", -100123, 4244, "口令"))
    assert _balance(db, u.id) == 0
    assert _chat_rows(db, u.id) == []


def test_command_message_no_award(db):
    u = _make_user(db, "chat4", 4245)
    router.dispatch(db, _update("supergroup", -100123, 4245, "/help"))
    assert _balance(db, u.id) == 0
    assert _chat_rows(db, u.id) == []


def test_private_message_no_award(db):
    u = _make_user(db, "chat5", 4246)
    router.dispatch(db, _update("private", 4246, 4246, "hello"))
    assert _balance(db, u.id) == 0
    assert _chat_rows(db, u.id) == []


def test_unbound_sender_no_award_no_error(db):
    router.dispatch(db, _update("supergroup", -100123, 999999, "hello"))
    assert db.query(models.PointsLog).filter(
        models.PointsLog.type == "chat").all() == []


def test_unapproved_group_no_award(db):
    u = _make_user(db, "chat6", 4247)
    db.add(models.SystemConfig(key="bot_group_ids", value="-999"))
    db.commit()
    router.dispatch(db, _update("supergroup", -100123, 4247, "hello"))
    assert _balance(db, u.id) == 0
    assert _chat_rows(db, u.id) == []


def test_award_failure_does_not_break_dispatch(db, monkeypatch):
    from backend import points as points_mod

    u = _make_user(db, "chat7", 4248)

    def _boom(db_, user_id):
        raise RuntimeError("award down")

    monkeypatch.setattr(points_mod, "award_chat_points", _boom)
    router.dispatch(db, _update("supergroup", -100123, 4248, "hello"))
    assert _balance(db, u.id) == 0
    assert _chat_rows(db, u.id) == []
