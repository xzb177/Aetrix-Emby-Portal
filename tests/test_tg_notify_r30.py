"""r30 N1/N2：TG 通知渠道改查 WebUser.telegram_id；群发言积分接线到分发器。"""
import os
os.environ.setdefault("DATABASE_TYPE", "sqlite")
os.environ.setdefault("REDIS_ENABLED", "false")

import asyncio

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend import models
from backend.notifications import TelegramChannel
from backend.emby_server import new_media_notify as nmn


@pytest.fixture()
def db():
    engine = create_engine("sqlite://")
    models.Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    yield session
    session.close()


_uid_seq = [0]

def _user(db, telegram_id=None):
    _uid_seq[0] += 1
    u = models.WebUser(username=f"u_r30_{_uid_seq[0]}", password_hash="x",
                       telegram_id=telegram_id)
    db.add(u)
    db.commit()
    db.refresh(u)
    return u


# ---------- N1 ----------

def test_staff_chat_ids_from_web_user(db):
    """_staff_chat_ids 改查 WebUser.telegram_id；未绑定的排除。"""
    bound = _user(db, telegram_id=777001)
    _user(db, telegram_id=None)
    assert nmn._staff_chat_ids(db, [bound.id, 999999]) == {bound.id: 777001}


def test_telegram_channel_send_uses_web_user_chat_id(db, monkeypatch):
    """send 投递到 WebUser.telegram_id；历史 target 为该 chat_id。"""
    u = _user(db, telegram_id=777002)
    ch = TelegramChannel(bot_token="dummy")
    seen = {}
    async def _fake_deliver(chat_id, title, content):
        seen["chat_id"] = chat_id
        return True, None
    monkeypatch.setattr(ch, "_deliver", _fake_deliver)
    ok = asyncio.run(ch.send(u.id, "标题", "内容", db=db))
    assert ok is True
    assert seen["chat_id"] == 777002
    hist = db.query(models.NotificationHistory).one()
    assert hist.target == "777002"
    assert hist.status == "sent"


def test_telegram_channel_send_unbound_returns_false(db, monkeypatch):
    """未绑定 TG 的用户：warning + False，不投递。"""
    u = _user(db, telegram_id=None)
    ch = TelegramChannel(bot_token="dummy")
    async def _boom(chat_id, title, content):
        raise AssertionError("未绑定用户不应投递")
    monkeypatch.setattr(ch, "_deliver", _boom)
    assert asyncio.run(ch.send(u.id, "标题", "内容", db=db)) is False


# ---------- N2 ----------

def _group_text_update():
    return {"update_id": 9, "message": {
        "message_id": 7,
        "from": {"id": 4242, "is_bot": False},
        "chat": {"id": -100123, "type": "supergroup"},
        "text": "hello world",
        "date": 1700000000,
    }}


def _db_minimal():
    engine = create_engine("sqlite://")
    models.SystemConfig.__table__.create(engine, checkfirst=True)
    return sessionmaker(bind=engine)()


def test_dispatch_group_text_after_lottery_miss_returns_quietly(monkeypatch):
    """群内非命令文本、口令未命中 → dispatch 安静返回（不抛错）。

    原 r30 N2 的「未命中接群发言积分」已于 2026-10-23 去重退役
    （tg_bot.chat_points 与 points.award_chat_points 重复，只留后者），
    这里只钉退役后的行为：口令未命中不再有后续动作。
    """
    from unittest import mock
    from backend.tg_bot import router, callbacks

    db = mock.MagicMock()
    monkeypatch.setattr(callbacks, "handle_lottery_password",
                        lambda *a, **k: False)
    router.dispatch(db, _group_text_update())  # 不抛错即通过


def test_dispatch_lottery_consumed_returns(monkeypatch):
    """口令命中消费后直接返回。"""
    from unittest import mock
    from backend.tg_bot import router, callbacks

    db = mock.MagicMock()
    monkeypatch.setattr(callbacks, "handle_lottery_password",
                        lambda *a, **k: True)
    router.dispatch(db, _group_text_update())  # 不抛错即通过
