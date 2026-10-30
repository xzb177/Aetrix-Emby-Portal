# -*- coding: utf-8 -*-
"""TG Bot 频道马甲治理＋退群处置回归（2026-10-30 借鉴 embyboss 第 1 项）。

马甲口径：
- sender_chat 是外部频道（id != 当前群）→ 删消息＋回说明（30 秒自毁口径）
- sender_chat.id == 当前群 = 匿名管理员形态（#1516），绝不按马甲处理
- is_automatic_forward（关联频道自动转发进讨论群）是系统行为，不删
- 白名单频道（bot_channel_whitelist）放行；总开关可关

退群口径（bot_leave_action，默认 cancel_welfare）：
- 已绑定用户在名单群内状态变为 left/kicked → 只取消公益资格
  （is_welfare=False + WelfareGrantLog channel="group_leave"，与
  welfare_expiry 的 auto_expire 同一口径）；账号、能量、查看凭证不动
- 状态真正变化才私聊通知本人；无资格/未绑定/动作=none 一律不动
"""
from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta

os.environ.setdefault("DATABASE_TYPE", "sqlite")
os.environ.setdefault("REDIS_ENABLED", "false")
sys.path.insert(0, ".")

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend import models
from backend.integrations import store
from backend.tg_bot import ephemeral, handlers, policy, router, sender

GROUP_ID = -100777
OTHER_GROUP_ID = -100555
CHAN_ID = -100999888
ANON_ID = 1087968824
USER_TG = 700001


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
    """捕获 (chat_id, text) 发送列表（普通 + ex 两通道同口径）。"""
    out = []
    monkeypatch.setattr(
        sender, "send_message",
        lambda db_, chat_id, text, reply_markup=None, **kw: out.append((chat_id, text)) or (True, None),
    )
    monkeypatch.setattr(
        sender, "send_message_ex",
        lambda db_, chat_id, text, reply_markup=None, **kw: out.append((chat_id, text)) or (True, None, 9001),
    )
    monkeypatch.setattr(ephemeral, "schedule_delete", lambda *a, **kw: True)
    return out


@pytest.fixture()
def deleted(monkeypatch):
    out = []
    monkeypatch.setattr(
        sender, "delete_message",
        lambda db_, chat_id, message_id, **kw: out.append((chat_id, message_id)) or (True, None),
    )
    return out


def _set_config(db, **kv):
    for k, v in kv.items():
        db.add(models.SystemConfig(key=k, value=v))
    db.commit()
    store.invalidate()


def _make_user(db, username="fan", tg_id=USER_TG, welfare=False, points=500):
    u = models.WebUser(username=username, telegram_id=tg_id)
    if hasattr(u, "password_hash") and getattr(u, "password_hash") is None:
        u.password_hash = ""
    u.points = points
    u.is_welfare = welfare
    if welfare:
        u.welfare_expires_at = datetime.now() + timedelta(days=30)
    db.add(u)
    db.commit()
    return u


def _channel_update(text, chan_id=CHAN_ID, from_id=USER_TG, chat_id=GROUP_ID,
                    message_id=42, **extra):
    msg = {
        "message_id": message_id,
        "from": {"id": from_id, "first_name": "Fan"},
        "sender_chat": {"id": chan_id, "type": "channel", "title": "某频道"},
        "chat": {"id": chat_id, "type": "supergroup"},
        "text": text,
    }
    msg.update(extra)
    return {"update_id": 11, "message": msg}


def _member_update(tg_id=USER_TG, old="member", new="left", chat_id=GROUP_ID,
                   chat_type="supergroup"):
    return {"update_id": 12, "chat_member": {
        "chat": {"id": chat_id, "type": chat_type},
        "from": {"id": 1, "first_name": "Admin"},
        "date": 1700000000,
        "old_chat_member": {"user": {"id": tg_id, "is_bot": False}, "status": old},
        "new_chat_member": {"user": {"id": tg_id, "is_bot": False, "first_name": "Fan"},
                            "status": new},
    }}


def _leave_logs(db):
    return (db.query(models.WelfareGrantLog)
            .filter(models.WelfareGrantLog.channel == "group_leave").all())


# ---------- ① 外部频道发言：删＋说明，不计分、不进 handler ----------

def test_external_channel_text_deleted_with_notice(db, sent, deleted):
    _make_user(db)
    router.dispatch(db, _channel_update("大家好，新片上线了"))
    assert deleted == [(GROUP_ID, 42)]
    group_msgs = [t for cid, t in sent if cid == GROUP_ID]
    assert len(group_msgs) == 1
    assert "频道" in group_msgs[0]
    assert db.query(models.PointsLog).filter(
        models.PointsLog.type == "chat").count() == 0


def test_external_channel_command_deleted_not_executed(db, sent, deleted, monkeypatch):
    called = []
    monkeypatch.setattr(handlers, "handle_points",
                        lambda *a, **kw: called.append(a) or "不应到达")
    router.dispatch(db, _channel_update("/points"))
    assert called == []
    assert deleted == [(GROUP_ID, 42)]
    assert any("频道" in t for _, t in sent)


# ---------- ② 白名单频道放行 ----------

def test_whitelisted_channel_passes(db, sent, deleted):
    _set_config(db, bot_channel_whitelist=str(CHAN_ID))
    router.dispatch(db, _channel_update("频道公告"))
    assert deleted == []
    assert all("频道身份" not in t for _, t in sent)


# ---------- ③ 匿名管理员（sender_chat == 本群）不被马甲逻辑碰 ----------

def test_anon_admin_not_touched_by_guard(db, sent, deleted, monkeypatch):
    called = []
    monkeypatch.setattr(handlers, "handle_redpacket",
                        lambda *a, **kw: called.append(a) or "不应到达")
    upd = {"update_id": 13, "message": {
        "message_id": 7,
        "from": {"id": ANON_ID, "first_name": "Group", "is_bot": True},
        "sender_chat": {"id": GROUP_ID, "type": "supergroup", "title": "交流群"},
        "chat": {"id": GROUP_ID, "type": "supergroup"},
        "text": "/redpacket 50 5",
    }}
    router.dispatch(db, upd)
    assert deleted == []
    assert called == []
    assert len(sent) == 1
    assert "匿名" in sent[0][1]


# ---------- ④ 系统/服务类 sender_chat 不删 ----------

def test_automatic_forward_not_deleted(db, sent, deleted):
    router.dispatch(db, _channel_update("关联频道新帖", is_automatic_forward=True))
    assert deleted == []
    assert all("频道身份" not in t for _, t in sent)


# ---------- ⑤ 总开关关闭时不治理 ----------

def test_guard_disabled_config(db, sent, deleted):
    _set_config(db, bot_channel_guard_enabled="false")
    router.dispatch(db, _channel_update("随便发"))
    assert deleted == []


# ---------- ⑥ 私聊零影响 ----------

def test_private_chat_unaffected(db, sent, deleted):
    upd = {"update_id": 14, "message": {
        "message_id": 3,
        "from": {"id": USER_TG, "first_name": "Fan"},
        "chat": {"id": USER_TG, "type": "private"},
        "text": "你好",
    }}
    router.dispatch(db, upd)
    assert deleted == []
    assert sent == []


# ---------- ⑦ 退群：只取消公益资格，凭证/能量不动，通知本人 ----------

def test_leave_cancels_welfare_only(db, sent):
    u = _make_user(db, welfare=True, points=500)
    db.add(models.EmbyViewUnlock(
        user_id=u.id, realm_id=1,
        expires_at=datetime.now() + timedelta(days=10), points_spent=50))
    db.commit()
    router.dispatch(db, _member_update())
    db.refresh(u)
    assert u.is_welfare is False
    assert u.points == 500
    assert db.query(models.EmbyViewUnlock).count() == 1
    logs = _leave_logs(db)
    assert len(logs) == 1 and logs[0].user_id == u.id and logs[0].days == 0
    notices = [t for cid, t in sent if cid == USER_TG]
    assert len(notices) == 1
    assert "公益资格" in notices[0] and "重新进群" in notices[0]


def test_kicked_also_cancels(db, sent):
    u = _make_user(db, welfare=True)
    router.dispatch(db, _member_update(new="kicked"))
    db.refresh(u)
    assert u.is_welfare is False
    assert len(_leave_logs(db)) == 1


# ---------- ⑧ bot_leave_action=none 不处置 ----------

def test_leave_action_none_noop(db, sent):
    _set_config(db, bot_leave_action="none")
    u = _make_user(db, welfare=True)
    router.dispatch(db, _member_update())
    db.refresh(u)
    assert u.is_welfare is True
    assert _leave_logs(db) == []
    assert sent == []


# ---------- ⑨ 未绑定 / 无资格 / 无状态变化：一律不动 ----------

def test_leave_unbound_user_noop(db, sent):
    router.dispatch(db, _member_update(tg_id=799999))
    assert _leave_logs(db) == []
    assert sent == []


def test_leave_non_welfare_user_noop(db, sent):
    _make_user(db, welfare=False)
    router.dispatch(db, _member_update())
    assert _leave_logs(db) == []
    assert sent == []


def test_leave_no_transition_noop(db, sent):
    u = _make_user(db, welfare=True)
    router.dispatch(db, _member_update(old="left", new="left"))
    db.refresh(u)
    assert u.is_welfare is True
    assert _leave_logs(db) == []


def test_join_noop(db, sent):
    u = _make_user(db, welfare=True)
    router.dispatch(db, _member_update(old="left", new="member"))
    db.refresh(u)
    assert u.is_welfare is True
    assert _leave_logs(db) == []


def test_leave_in_unlisted_group_noop(db, sent):
    _set_config(db, bot_group_ids=str(OTHER_GROUP_ID))
    u = _make_user(db, welfare=True)
    router.dispatch(db, _member_update())
    db.refresh(u)
    assert u.is_welfare is True
    assert _leave_logs(db) == []


# ---------- ⑩ 配置缺省与判定口径 ----------

def test_policy_defaults_and_getters(db):
    assert policy.DEFAULTS["bot_channel_guard_enabled"] == "true"
    assert policy.DEFAULTS["bot_channel_whitelist"] == ""
    assert policy.DEFAULTS["bot_leave_action"] == "cancel_welfare"
    cfg = policy.read_bot_config(db)
    assert policy.is_channel_guard_enabled(cfg) is True
    assert policy.is_channel_whitelisted(cfg, CHAN_ID) is False
    assert policy.leave_action(cfg) == "cancel_welfare"
    _set_config(db, bot_channel_whitelist=f"{CHAN_ID}, -1001",
                bot_leave_action="乱写")
    cfg = policy.read_bot_config(db)
    assert policy.is_channel_whitelisted(cfg, CHAN_ID) is True
    assert policy.leave_action(cfg) == "cancel_welfare"
