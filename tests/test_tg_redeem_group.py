"""/redeem 在群里发等于公开兑换码：拒绝执行、提示私聊，并尽量删掉原消息（删不掉忽略）。"""
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
from backend.tg_bot import handlers, policy, router, sender


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


def _patch(monkeypatch, delete_ok=True):
    sent, deleted, redeemed = [], [], []
    monkeypatch.setattr(sender, "send_message",
                        lambda db_, chat_id, text, reply_markup=None, **k: sent.append((chat_id, text)) or (True, None))
    # 二期自毁接线：群内护码提示改走 send_message_ex 拿 message_id，同样记录；
    # schedule_delete 打桩成空操作，避免测试里起真定时器
    monkeypatch.setattr(sender, "send_message_ex",
                        lambda db_, chat_id, text, reply_markup=None, **k: sent.append((chat_id, text)) or (True, None, 1))
    monkeypatch.setattr("backend.tg_bot.ephemeral.schedule_delete",
                        lambda *a, **k: True)

    def fake_delete(db_, chat_id, message_id):
        deleted.append((chat_id, message_id))
        if not delete_ok:
            raise RuntimeError("no rights")
        return True, None

    monkeypatch.setattr(sender, "delete_message", fake_delete, raising=False)
    monkeypatch.setattr(handlers, "handle_redeem",
                        lambda *a, **k: redeemed.append(a) or "🎁 ok")
    monkeypatch.setattr(policy, "is_group_allowed", lambda cfg, cid: True)
    return sent, deleted, redeemed


def _update(chat_type, chat_id, from_id=4242):
    return {"update_id": 1, "message": {
        "message_id": 77, "text": "/redeem SECRET123",
        "chat": {"id": chat_id, "type": chat_type},
        "from": {"id": from_id, "first_name": "a"},
    }}


@pytest.mark.parametrize("delete_ok", [True, False])
def test_redeem_refused_in_group(db, monkeypatch, delete_ok):
    sent, deleted, redeemed = _patch(monkeypatch, delete_ok)
    router.dispatch(db, _update("supergroup", -100123, 4242 + int(delete_ok)))
    assert not redeemed, "群里不能执行兑换"
    assert deleted == [(-100123, 77)]
    assert sent and "私聊" in sent[-1][1]


def test_redeem_works_in_private(db, monkeypatch):
    sent, deleted, redeemed = _patch(monkeypatch)
    router.dispatch(db, _update("private", 4343, 4343))
    assert redeemed
    assert not deleted
