# -*- coding: utf-8 -*-
"""TG Bot 群匿名管理员：身份命令拦截回归。

背景（2026-10-30 站长截图坐实）：站长在群里开「匿名发送」发
/redpacket 50 5，机器人回了「🔗 如何绑定帐号」——匿名管理员消息的
from 是 GroupAnonymousBot（id=1087968824）且带 sender_chat，协议上
认不出是哪位管理员，handler 里 resolve→None 被误当成未绑定。

口径：router 层识别匿名后，对需要身份的命令统一回匿名说明；
公开命令（不查发送者身份）照常；匿名非命令发言不回复、不计分；
匿名发 6 位绑定码回匿名说明、不走绑定验证。
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
from backend.tg_bot import ephemeral, handlers, router, sender

ANON_ID = 1087968824
GROUP_ID = -100777


@pytest.fixture()
def db():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool,
    )
    models.Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    store.invalidate()
    router._user_cmd_limits.clear()
    router._group_windows.clear()
    yield session
    session.close()
    store.invalidate()


@pytest.fixture()
def sent(monkeypatch):
    out = []
    monkeypatch.setattr(
        sender, "send_message",
        lambda db_, chat_id, text, reply_markup=None, **kw: out.append(text) or (True, None),
    )
    # 自毁二期接线后，群里匿名说明改走 send_message_ex 拿 message_id 挂自毁：
    # ex 通道同样记进 out（断言口径不变），自毁调度打桩成空操作不真挂定时器。
    monkeypatch.setattr(
        sender, "send_message_ex",
        lambda db_, chat_id, text, reply_markup=None, **kw: out.append(text) or (True, None, 9001),
    )
    monkeypatch.setattr(ephemeral, "schedule_delete", lambda *a, **kw: True)
    return out


def _make_user(db, username="boss", tg_id=700001, points=1000):
    u = models.WebUser(username=username, telegram_id=tg_id)
    if hasattr(u, "password_hash") and getattr(u, "password_hash") is None:
        u.password_hash = ""
    u.points = points
    db.add(u)
    db.commit()
    return u


def _anon_update(text, chat_id=GROUP_ID, from_id=ANON_ID):
    return {"update_id": 99, "message": {
        "message_id": 7,
        "from": {"id": from_id, "first_name": "Group", "is_bot": True},
        "sender_chat": {"id": chat_id, "type": "supergroup", "title": "交流群"},
        "chat": {"id": chat_id, "type": "supergroup"},
        "text": text,
    }}


def _user_update(text, tg_id=700001, chat_id=GROUP_ID):
    return {"update_id": 100, "message": {
        "message_id": 8,
        "from": {"id": tg_id, "first_name": "Boss"},
        "chat": {"id": chat_id, "type": "supergroup"},
        "text": text,
    }}


# ---------- ① 匿名身份命令：回匿名说明，不进 handler、不回绑定指引 ----------

def test_anon_redpacket_gets_anon_notice_not_bind_guide(db, sent, monkeypatch):
    called = []
    monkeypatch.setattr(handlers, "handle_redpacket",
                        lambda *a, **kw: called.append(a) or "不应到达")
    router.dispatch(db, _anon_update("/redpacket 50 5"))
    assert called == []
    assert len(sent) == 1
    assert "匿名" in sent[0]
    assert "不用重新绑定" in sent[0]
    assert "如何绑定" not in sent[0]
    assert db.query(models.RedPacket).count() == 0


def test_anon_checkin_gets_anon_notice(db, sent, monkeypatch):
    called = []
    monkeypatch.setattr(handlers, "handle_checkin",
                        lambda *a, **kw: called.append(a) or "不应到达")
    router.dispatch(db, _anon_update("/checkin"))
    assert called == []
    assert len(sent) == 1
    assert "匿名" in sent[0]
    assert "如何绑定" not in sent[0]


def test_sender_chat_with_personal_from_also_anon(db, sent, monkeypatch):
    # 以群/频道身份发言时 from 也可能带着个人号，但消息带 sender_chat
    # 就没有个人身份：哪怕这个人绑过，也不能按他本人执行身份命令。
    called = []
    monkeypatch.setattr(handlers, "handle_points",
                        lambda *a, **kw: called.append(a) or "不应到达")
    _make_user(db)
    router.dispatch(db, _anon_update("/points", from_id=700001))
    assert called == []
    assert len(sent) == 1
    assert "匿名" in sent[0]


# ---------- ③ 匿名公开命令照常 ----------

def test_anon_public_command_rules_still_works(db, sent):
    router.dispatch(db, _anon_update("/rules"))
    assert len(sent) == 1
    assert "群规" in sent[0]
    assert "匿名管理员身份" not in sent[0]


# ---------- ④ 实名用户红包流程不变 ----------

def test_real_user_redpacket_flow_unchanged(db, sent):
    _make_user(db)
    router.dispatch(db, _user_update("/redpacket 50 5"))
    assert len(sent) == 1
    assert "总额 50 能量" in sent[0]
    assert db.query(models.RedPacket).count() == 1


# ---------- ⑤ 匿名非命令发言：无回复、不计分 ----------

def test_anon_plain_text_no_reply_no_points(db, sent):
    _make_user(db)
    router.dispatch(db, _anon_update("影迷加分多啊"))
    assert sent == []
    chat_logs = (db.query(models.PointsLog)
                 .filter(models.PointsLog.type == "chat").count())
    assert chat_logs == 0


# ---------- ⑥ 匿名发 6 位数字：回匿名说明、不走绑定验证 ----------

def test_anon_six_digit_gets_anon_notice_not_bind_verify(db, sent, monkeypatch):
    called = []
    monkeypatch.setattr(handlers, "verify_bind_code",
                        lambda *a, **kw: called.append(a) or None)
    router.dispatch(db, _anon_update("123456"))
    assert called == []
    assert len(sent) == 1
    assert "匿名" in sent[0]
