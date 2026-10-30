# -*- coding: utf-8 -*-
"""TG Bot /chatpoints 群发言能量查询（补菜单有、命令无的半截功能）。

口径：只读自助查询，只显示调用者本人数据（今日已得/日封顶进度/累计）；
私聊与群聊都可用（与 /points、/account 同款放行）；未绑定回绑定指引；
命令开关 bot_cmd_chatpoints 关闭时回「该功能已关闭」。
数据口径与发分侧 points.award_chat_points 一致：PointsLog type="chat"、
amount>0、上海自然日（economy._today_start_utc_naive）。
"""
from __future__ import annotations

import os
import sys
from datetime import timedelta

os.environ.setdefault("DATABASE_TYPE", "sqlite")
os.environ.setdefault("REDIS_ENABLED", "false")
sys.path.insert(0, ".")

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend import models
from backend.api import economy as economy_mod
from backend.integrations import store
from backend.tg_bot import handlers, router, sender


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
def sent(monkeypatch):
    messages = []

    def fake_send(db_, chat_id, text, reply_markup=None, **k):
        messages.append((chat_id, text))
        return (True, None)

    monkeypatch.setattr(sender, "send_message", fake_send)

    def fake_send_ex(db_, chat_id, text, reply_markup=None, **k):
        messages.append((chat_id, text))
        return (True, None, 1)

    monkeypatch.setattr(sender, "send_message_ex", fake_send_ex)
    monkeypatch.setattr(
        "backend.tg_bot.ephemeral.schedule_delete", lambda *a, **k: True)
    return messages


def _make_user(db, username, tg_id, points=0):
    u = models.WebUser(username=username, telegram_id=tg_id)
    if hasattr(u, "password_hash") and getattr(u, "password_hash") is None:
        u.password_hash = ""
    u.points = points
    db.add(u)
    db.commit()
    return u


def _add_log(db, user_id, amount, type_="chat", when=None, desc="发言奖励"):
    row = models.PointsLog(
        user_id=user_id, amount=amount, type=type_, description=desc,
    )
    if when is not None:
        row.created_at = when
    db.add(row)
    db.commit()
    return row


def _update(chat_type, chat_id, from_id, text):
    return {"update_id": 1, "message": {
        "message_id": 88, "text": text,
        "chat": {"id": chat_id, "type": chat_type},
        "from": {"id": from_id, "first_name": "a"},
    }}


def _seed_chat_history(db, user_id):
    """今日 chat +3（两条）、昨日 chat +5（只计累计）、今日签到 +10 与
    chat 扣分 -1（都不计入发言能量）。"""
    today_start = economy_mod._today_start_utc_naive()
    _add_log(db, user_id, 1, when=today_start + timedelta(hours=1))
    _add_log(db, user_id, 2, when=today_start + timedelta(hours=2))
    _add_log(db, user_id, 5, when=today_start - timedelta(hours=1))
    _add_log(db, user_id, 10, type_="checkin", desc="签到",
             when=today_start + timedelta(hours=1))
    _add_log(db, user_id, -1, when=today_start + timedelta(hours=3))


# ---------- handler 直测 ----------

def test_handler_bound_shows_today_progress_and_total(db):
    u = _make_user(db, "cp1", 6101)
    _seed_chat_history(db, u.id)

    from backend import points as points_mod

    status = points_mod.get_chat_points_status(db, u.id)
    assert status == {
        "earned_today": 3, "daily_cap": 20, "per_msg": 1, "total_earned": 8,
    }

    text = handlers.handle_chatpoints(db, {"id": 6101}, 6101, "")
    assert "群发言能量" in text
    assert "<b>今日已得</b> | <b>3</b> / 20 能量" in text
    assert "<b>累计能量</b> | <b>8</b> 能量" in text


def test_handler_unbound_returns_bind_guide(db):
    text = handlers.handle_chatpoints(db, {"id": 699999}, 699999, "")
    assert "如何绑定账号" in text


def test_handler_cap_reached_notes_full(db):
    u = _make_user(db, "cp2", 6102)
    today_start = economy_mod._today_start_utc_naive()
    _add_log(db, u.id, 20, when=today_start + timedelta(hours=1))
    text = handlers.handle_chatpoints(db, {"id": 6102}, 6102, "")
    assert "<b>今日已得</b> | <b>20</b> / 20 能量" in text
    assert "已达上限" in text


# ---------- dispatch 路由/策略 ----------

def test_dispatch_private_bound(db, sent):
    u = _make_user(db, "cp3", 6201)
    _seed_chat_history(db, u.id)
    router.dispatch(db, _update("private", 6201, 6201, "/chatpoints"))
    assert len(sent) == 1
    chat_id, text = sent[0]
    assert chat_id == 6201
    assert "群发言能量" in text and "今日已得" in text
    assert "未知命令" not in text


def test_dispatch_private_unbound(db, sent):
    router.dispatch(db, _update("private", 6299, 6299, "/chatpoints"))
    assert len(sent) == 1
    assert "如何绑定账号" in sent[0][1]
    assert "未知命令" not in sent[0][1]


def test_dispatch_group_bound_shows_only_own_data(db, sent):
    me = _make_user(db, "cp4", 6301)
    other = _make_user(db, "cp5", 6302)
    _seed_chat_history(db, me.id)  # 累计 8
    today_start = economy_mod._today_start_utc_naive()
    _add_log(db, other.id, 17, when=today_start + timedelta(hours=1))  # 别人累计 17

    router.dispatch(db, _update("supergroup", -100777, 6301, "/chatpoints"))
    assert len(sent) == 1
    chat_id, text = sent[0]
    assert chat_id == -100777  # 群里可用，回复发回群里
    assert "群发言能量" in text
    assert "<b>累计能量</b> | <b>8</b> 能量" in text
    assert "17" not in text  # 只显示调用者本人数据
    assert "请私聊" not in text and "未知命令" not in text


def test_dispatch_group_unbound_gets_bind_guide(db, sent):
    router.dispatch(db, _update("supergroup", -100888, 6399, "/chatpoints"))
    assert len(sent) == 1
    assert "如何绑定账号" in sent[0][1]
    assert "未知命令" not in sent[0][1]


def test_dispatch_switch_off_replies_closed(db, sent):
    _make_user(db, "cp6", 6401)
    db.add(models.SystemConfig(key="bot_cmd_chatpoints", value="false"))
    db.commit()
    router.dispatch(db, _update("private", 6401, 6401, "/chatpoints"))
    assert len(sent) == 1
    assert sent[0][1] == "该功能已关闭"
