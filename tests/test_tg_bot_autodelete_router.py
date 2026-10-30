"""TG Bot 消息自毁二期测试：router 接线。

覆盖（用户 2026-10-30 口径，parent 定 scope 只作用于群聊）：
- 群里机器人命令回复走 ex 通道拿 message_id，按 bot_msg_autodelete_seconds
  （默认 30）挂自毁；用户命令消息本体按 bot_cmd_autodelete_seconds
  （默认 3）挂自毁；
- /redpacket 成功发出的红包本体（handler 返回 (text, claim_markup) 元组）
  永不挂自毁；红包用法卡（参数错误、返回 str）照常自毁；
- 私聊发送点一律不挂自毁；
- 延时配置为 0 时全不挂；
- 群内 /redeem 原命令消息走既有即时删除，不再重复挂定时自毁；
- 匿名说明（#1516 的 ANON_ADMIN_TEXT）发送点在本基线尚不存在，
  用例以 skipif 守卫：#1516 合并、本分支变基后自动激活。
"""
from __future__ import annotations

import sys

import pytest

sys.path.insert(0, ".")

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend import models
from backend.integrations import store
from backend.tg_bot import ephemeral, router, sender

GROUP_CHAT = -100777
BOT_MSG_ID = 9001


@pytest.fixture()
def db():
    engine = create_engine("sqlite:///:memory:")
    for table in (
        models.WebUser.__table__,
        models.TgBindCode.__table__,
        models.SystemConfig.__table__,
        models.RedPacket.__table__,
        models.RedPacketClaim.__table__,
        models.PointsLog.__table__,
        models.CheckinRecord.__table__,
        models.VitalityLog.__table__,
        models.UserSubscription.__table__,
        models.SubscriptionPlan.__table__,
    ):
        table.create(engine, checkfirst=True)
    # store.get_value 有进程级缓存，fixture 开头清掉防止用例间串味
    store.invalidate()
    Session = sessionmaker(bind=engine)
    session = Session()
    yield session
    session.close()


def _make_user(db, username="u1", tg_id=None, points=0):
    u = models.WebUser(username=username, telegram_id=tg_id)
    if hasattr(u, "password_hash") and getattr(u, "password_hash") is None:
        try:
            u.password_hash = ""
        except Exception:
            pass
    u.points = points
    db.add(u)
    db.commit()
    return u


def _msg(tg_id, chat_id, text, chat_type="supergroup", message_id=4242,
         update_id=99):
    return {
        "update_id": update_id,
        "message": {
            "message_id": message_id,
            "from": {"id": tg_id, "first_name": "T"},
            "chat": {"id": chat_id, "type": chat_type},
            "text": text,
        },
    }


@pytest.fixture()
def wires(monkeypatch):
    """劫持发送层与自毁调度，记录 (plain, ex, scheduled, deleted)。"""
    rec = {"plain": [], "ex": [], "scheduled": [], "deleted": []}

    def fake_plain(db_, chat_id, text, reply_markup=None, **kw):
        rec["plain"].append((chat_id, text, reply_markup))
        return (True, None)

    def fake_ex(db_, chat_id, text, reply_markup=None, **kw):
        rec["ex"].append((chat_id, text, reply_markup))
        return (True, None, BOT_MSG_ID)

    def fake_schedule(chat_id, message_id, delay):
        rec["scheduled"].append((chat_id, message_id, delay))
        return True

    def fake_delete(db_, chat_id, message_id):
        rec["deleted"].append((chat_id, message_id))
        return (True, None)

    monkeypatch.setattr(sender, "send_message", fake_plain)
    monkeypatch.setattr(sender, "send_message_ex", fake_ex)
    monkeypatch.setattr(sender, "delete_message", fake_delete)
    monkeypatch.setattr(ephemeral, "schedule_delete", fake_schedule)
    return rec


def _delays_for(rec, chat_id, message_id):
    return [d for (c, m, d) in rec["scheduled"] if c == chat_id and m == message_id]


# ---------- ① 群命令回复 30 秒自毁 + ③ 用户命令 3 秒自毁 ----------

def test_group_command_reply_and_cmd_autodelete(db, wires):
    _make_user(db, "g1", 710001, points=50)
    router.dispatch(db, _msg(710001, GROUP_CHAT, "/points", message_id=4242))
    # 机器人回复走 ex 通道并挂 30 秒，message_id 为 ex 返回值
    assert len(wires["ex"]) == 1 and wires["ex"][0][0] == GROUP_CHAT
    assert _delays_for(wires, GROUP_CHAT, BOT_MSG_ID) == [30]
    # 用户命令消息本体挂 3 秒
    assert _delays_for(wires, GROUP_CHAT, 4242) == [3]
    # 群回复不走普通发送
    assert wires["plain"] == []


def test_group_unknown_command_reply_autodelete(db, wires):
    _make_user(db, "g2", 710002)
    router.dispatch(db, _msg(710002, GROUP_CHAT, "/nosuchcmd", message_id=4243))
    assert len(wires["ex"]) == 1 and "未知命令" in wires["ex"][0][1]
    assert _delays_for(wires, GROUP_CHAT, BOT_MSG_ID) == [30]


def test_group_private_only_notice_autodelete(db, wires):
    _make_user(db, "g3", 710003)
    router.dispatch(db, _msg(710003, GROUP_CHAT, "/login", message_id=4244))
    assert len(wires["ex"]) == 1 and "请私聊" in wires["ex"][0][1]
    assert _delays_for(wires, GROUP_CHAT, BOT_MSG_ID) == [30]
    assert _delays_for(wires, GROUP_CHAT, 4244) == [3]


# ---------- ② 红包本体豁免 / 用法卡照常自毁 ----------

def test_group_redpacket_success_body_exempt(db, wires):
    _make_user(db, "g4", 710004, points=1000)
    router.dispatch(db, _msg(710004, GROUP_CHAT, "/redpacket 100 10",
                             message_id=4245))
    # 红包确实发出（落库）且正文经普通发送（不走 ex、不挂自毁）
    packet = db.query(models.RedPacket).first()
    assert packet is not None
    assert wires["ex"] == []
    assert any("总额 100 积分" in t for (_c, t, _m) in wires["plain"])
    # 唯一被挂的只有用户命令消息的 3 秒自毁
    assert wires["scheduled"] == [(GROUP_CHAT, 4245, 3)]


def test_group_redpacket_usage_card_autodelete(db, wires):
    _make_user(db, "g5", 710005, points=1000)
    router.dispatch(db, _msg(710005, GROUP_CHAT, "/redpacket", message_id=4246))
    assert len(wires["ex"]) == 1 and "用法" in wires["ex"][0][1]
    assert _delays_for(wires, GROUP_CHAT, BOT_MSG_ID) == [30]
    assert db.query(models.RedPacket).count() == 0


# ---------- 私聊一律不挂 ----------

def test_private_command_no_autodelete(db, wires):
    _make_user(db, "p1", 710006, points=50)
    router.dispatch(db, _msg(710006, 710006, "/points",
                             chat_type="private", message_id=4247))
    assert wires["ex"] == []
    assert len(wires["plain"]) == 1
    assert wires["scheduled"] == []


# ---------- /redeem 即时删除不重复挂 ----------

def test_group_redeem_immediate_delete_no_timer(db, wires):
    _make_user(db, "g6", 710007)
    router.dispatch(db, _msg(710007, GROUP_CHAT, "/redeem ABCD1234",
                             message_id=4248))
    # 原命令消息被即时删除，且没有再给它挂 3 秒定时
    assert wires["deleted"] == [(GROUP_CHAT, 4248)]
    assert _delays_for(wires, GROUP_CHAT, 4248) == []
    # 机器人的护码提示照常 30 秒自毁
    assert _delays_for(wires, GROUP_CHAT, BOT_MSG_ID) == [30]


# ---------- ④ 延时为 0 时全不挂 ----------

def test_zero_delay_disables_all(db, wires):
    db.add(models.SystemConfig(key="bot_msg_autodelete_seconds", value="0"))
    db.add(models.SystemConfig(key="bot_cmd_autodelete_seconds", value="0"))
    db.commit()
    store.invalidate("bot_msg_autodelete_seconds")
    store.invalidate("bot_cmd_autodelete_seconds")
    _make_user(db, "g7", 710008, points=50)
    router.dispatch(db, _msg(710008, GROUP_CHAT, "/points", message_id=4249))
    assert wires["scheduled"] == []
    assert wires["ex"] == []  # 关闭时回落普通发送
    assert len(wires["plain"]) == 1


# ---------- ⑤ 匿名说明（#1516 合并变基后激活） ----------

@pytest.mark.skipif(
    not hasattr(router, "ANON_ADMIN_TEXT"),
    reason="基线未含 #1516 匿名拦截发送点，变基到含 #1516 的 main 后自动激活",
)
def test_group_anon_admin_notice_autodelete(db, wires):
    update = _msg(1087968824, GROUP_CHAT, "/redpacket 50 5", message_id=4250)
    update["message"]["sender_chat"] = {"id": GROUP_CHAT, "type": "supergroup"}
    router.dispatch(db, update)
    assert len(wires["ex"]) == 1 and "匿名" in wires["ex"][0][1]
    assert _delays_for(wires, GROUP_CHAT, BOT_MSG_ID) == [30]
    assert db.query(models.RedPacket).count() == 0
