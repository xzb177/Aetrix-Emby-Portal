"""消息自毁一期测试：发送层 message_id 通道＋自毁调度＋抢红包群内反馈。

覆盖（用户 2026-10-30 口径）：
- send_message_ex / send_photo_ex 能拿到 Telegram 返回的 message_id，
  旧 send_message / send_photo 签名与返回一字不变；
- 抢红包成功在群里新发一条反馈（谁抢到多少、还剩几个），并给反馈消息
  注册 bot_msg_autodelete_seconds（默认 30 秒）自毁调度；
- 私聊抢成功不发反馈；反馈发送失败不影响领取；
- 红包消息本体永不被调度自毁；延时配置为 0 时不调度。
"""
from __future__ import annotations

import sys

import pytest

sys.path.insert(0, ".")

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend import models
from backend.integrations import store
from backend.tg_bot import callbacks, sender


@pytest.fixture()
def db():
    engine = create_engine("sqlite:///:memory:")
    models.WebUser.__table__.create(engine, checkfirst=True)
    models.TgBindCode.__table__.create(engine, checkfirst=True)
    models.SystemConfig.__table__.create(engine, checkfirst=True)
    models.RedPacket.__table__.create(engine, checkfirst=True)
    models.RedPacketClaim.__table__.create(engine, checkfirst=True)
    models.PointsLog.__table__.create(engine, checkfirst=True)
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


# ---------- 1. 发送层 message_id 通道 ----------

def test_send_message_ex_returns_message_id(db, monkeypatch):
    seen = {}

    def fake_call_ex(db_, method, payload):
        seen["method"] = method
        seen["payload"] = payload
        return True, None, {"message_id": 777, "chat": {"id": payload["chat_id"]}}

    monkeypatch.setattr(sender, "_call_ex", fake_call_ex)
    ok, err, message_id = sender.send_message_ex(db, 555, "你好")
    assert (ok, err, message_id) == (True, None, 777)
    assert seen["method"] == "sendMessage"
    assert seen["payload"]["chat_id"] == 555
    assert seen["payload"]["text"] == "你好"

    # 旧接口签名与返回不变（仍走 _call、仍是二元组）
    monkeypatch.setattr(sender, "_call", lambda db_, method, payload: (True, None))
    assert sender.send_message(db, 555, "你好") == (True, None)


def test_send_message_ex_failure_returns_none_id(db, monkeypatch):
    monkeypatch.setattr(sender, "_call_ex", lambda db_, method, payload: (False, "网络请求失败", None))
    ok, err, message_id = sender.send_message_ex(db, 555, "你好")
    assert ok is False
    assert err == "网络请求失败"
    assert message_id is None


def test_send_photo_ex_returns_message_id(db, monkeypatch):
    monkeypatch.setattr(
        sender, "_call_multipart_ex",
        lambda db_, method, payload, files: (True, None, {"message_id": 888}))
    ok, err, message_id = sender.send_photo_ex(db, 555, "▎标题")
    assert (ok, err, message_id) == (True, None, 888)

    # 旧 send_photo 仍走 _call_multipart、仍是二元组
    monkeypatch.setattr(
        sender, "_call_multipart",
        lambda db_, method, payload, files: (True, None))
    assert sender.send_photo(db, 555, "▎标题") == (True, None)


# ---------- 2. 自毁调度模块 ----------

def test_ephemeral_defaults_and_timer(monkeypatch):
    from backend.tg_bot import ephemeral

    timers = []

    class FakeTimer:
        def __init__(self, interval, function, args=None, kwargs=None):
            self.interval = interval
            self.function = function
            self.args = args or ()
            self.daemon = False
            self.started = False
            timers.append(self)

        def start(self):
            self.started = True

    monkeypatch.setattr(ephemeral, "Timer", FakeTimer)

    # 无任何配置行时：机器人消息默认 30 秒、用户命令默认 3 秒
    # （db 夹具外单独建会话读默认值）
    engine = create_engine("sqlite:///:memory:")
    models.SystemConfig.__table__.create(engine, checkfirst=True)
    store.invalidate()
    session = sessionmaker(bind=engine)()
    try:
        assert ephemeral.bot_msg_delay_seconds(session) == 30
        assert ephemeral.bot_cmd_delay_seconds(session) == 3
    finally:
        session.close()

    assert ephemeral.schedule_delete(-1001, 55, 30) is True
    assert len(timers) == 1
    assert timers[0].interval == 30
    assert timers[0].args == (-1001, 55)
    assert timers[0].started is True

    # 延时 0 = 关闭，不起定时器
    assert ephemeral.schedule_delete(-1001, 56, 0) is False
    assert len(timers) == 1


def test_ephemeral_timer_uses_delete_callable_captured_at_schedule(monkeypatch):
    """调度后替换 sender.delete_message，到期仍调调度时快照的旧 callable。

    生产 sender 不热替换删除函数，快照与旧的到期现查等价；快照口径
    钉死的是测试卫生：迟到定时器不许落到后来测试装的记录器头上
    （b3 的 3 秒命令自毁串扰 channel_guard 记录器的实锤污染）。
    """
    from backend.tg_bot import ephemeral

    timers = []

    class FakeTimer:
        def __init__(self, interval, function, args=None, kwargs=None):
            self.interval = interval
            self.function = function
            self.args = args or ()
            self.kwargs = kwargs or {}
            self.daemon = False
            timers.append(self)

        def start(self):
            pass

        def fire(self):
            self.function(*self.args, **self.kwargs)

    class DummySession:
        def close(self):
            pass

    old_calls, new_calls = [], []

    def old_delete(db_, chat_id, message_id):
        old_calls.append((chat_id, message_id))
        return True, None

    def new_delete(db_, chat_id, message_id):
        new_calls.append((chat_id, message_id))
        return True, None

    monkeypatch.setattr(ephemeral, "Timer", FakeTimer)
    monkeypatch.setattr("backend.database.SessionLocal", lambda: DummySession())
    monkeypatch.setattr(sender, "delete_message", old_delete)

    assert ephemeral.schedule_delete(-1002, 57, 30) is True
    assert timers[0].kwargs["delete_message"] is old_delete

    # 调度之后 sender.delete_message 被换掉（模拟后来测试装记录器）
    monkeypatch.setattr(sender, "delete_message", new_delete)
    timers[0].fire()

    assert old_calls == [(-1002, 57)]
    assert new_calls == []
    assert (-1002, 57) not in ephemeral._timers


def test_ephemeral_delay_reads_system_config(db):
    from backend.tg_bot import ephemeral

    db.add(models.SystemConfig(key="bot_msg_autodelete_seconds", value="45"))
    db.add(models.SystemConfig(key="bot_cmd_autodelete_seconds", value="0"))
    db.commit()
    store.invalidate("bot_msg_autodelete_seconds")
    store.invalidate("bot_cmd_autodelete_seconds")
    assert ephemeral.bot_msg_delay_seconds(db) == 45
    assert ephemeral.bot_cmd_delay_seconds(db) == 0


# ---------- 3. 抢红包群内反馈 ----------

FEEDBACK_MESSAGE_ID = 4242
PACKET_MESSAGE_ID = 99
GROUP_CHAT_ID = -100123


def _patch_claim_io(db, monkeypatch, send_result=(True, None, FEEDBACK_MESSAGE_ID)):
    """打桩 claim 路径的发送层；返回 (answers, edits, sends, schedules)。"""
    from backend.tg_bot import ephemeral

    answers, edits, sends, schedules = [], [], [], []
    monkeypatch.setattr(
        sender, "answer_callback_query",
        lambda db_, cq_id, text=None, show_alert=False:
            answers.append((cq_id, text, show_alert)) or (True, None))
    monkeypatch.setattr(
        sender, "edit_message_text",
        lambda db_, chat_id, message_id, text, reply_markup=None:
            edits.append((chat_id, message_id, text, reply_markup)) or (True, None))
    monkeypatch.setattr(
        sender, "send_message_ex",
        lambda db_, chat_id, text, reply_markup=None, parse_mode="HTML":
            sends.append((chat_id, text)) or send_result)
    monkeypatch.setattr(
        ephemeral, "schedule_delete",
        lambda chat_id, message_id, delay: schedules.append((chat_id, message_id, delay)) or True)
    return answers, edits, sends, schedules


def _setup_packet(db):
    from backend import welfare_redpacket

    sender_user = _make_user(db, "s_fb", 700101, 1000)
    packet = welfare_redpacket.send_packet(db, sender_user, 50, 5)
    claimer = _make_user(db, "c_fb", 700102, 0)
    return packet, claimer


def _claim_cq(packet_id, chat):
    return {
        "id": "cq-fb",
        "from": {"id": 700102},
        "data": f"redpacket_claim:{packet_id}",
        "message": {"message_id": PACKET_MESSAGE_ID, "chat": chat},
    }


def test_claim_success_group_sends_feedback_and_schedules_delete(db, monkeypatch):
    answers, edits, sends, schedules = _patch_claim_io(db, monkeypatch)
    packet, _claimer = _setup_packet(db)
    cq = _claim_cq(packet.id, {"id": GROUP_CHAT_ID, "type": "supergroup"})
    callbacks.handle_callback(db, cq)

    claim = db.query(models.RedPacketClaim).first()
    assert claim is not None
    # toast 维持原样
    assert len(answers) == 1 and "抢到" in (answers[0][1] or "")
    # 群里新发一条反馈：含用户名、金额、剩余数
    assert len(sends) == 1
    chat_id, text = sends[0]
    assert chat_id == GROUP_CHAT_ID
    assert "c_fb" in text
    assert str(claim.amount) in text
    assert "还剩" in text and "4" in text
    # 反馈消息注册 30 秒自毁（默认延时）
    assert schedules == [(GROUP_CHAT_ID, FEEDBACK_MESSAGE_ID, 30)]
    # 红包消息编辑维持原样
    assert len(edits) == 1 and "剩余 4 个" in edits[0][2]


def test_claim_feedback_never_schedules_packet_message_delete(db, monkeypatch):
    _answers, _edits, _sends, schedules = _patch_claim_io(db, monkeypatch)
    packet, _claimer = _setup_packet(db)
    cq = _claim_cq(packet.id, {"id": GROUP_CHAT_ID, "type": "supergroup"})
    callbacks.handle_callback(db, cq)
    # 红包消息本体（PACKET_MESSAGE_ID）永不自毁
    assert all(message_id != PACKET_MESSAGE_ID for (_c, message_id, _d) in schedules)


def test_claim_success_private_no_feedback(db, monkeypatch):
    answers, edits, sends, schedules = _patch_claim_io(db, monkeypatch)
    packet, _claimer = _setup_packet(db)
    cq = _claim_cq(packet.id, {"id": 700102, "type": "private"})
    callbacks.handle_callback(db, cq)
    assert db.query(models.RedPacketClaim).count() == 1
    assert len(answers) == 1 and "抢到" in (answers[0][1] or "")
    assert sends == []
    assert schedules == []


def test_claim_feedback_send_failure_does_not_break_claim(db, monkeypatch):
    answers, edits, sends, schedules = _patch_claim_io(
        db, monkeypatch, send_result=(False, "boom", None))
    packet, _claimer = _setup_packet(db)
    cq = _claim_cq(packet.id, {"id": GROUP_CHAT_ID, "type": "supergroup"})
    callbacks.handle_callback(db, cq)  # 不许抛
    assert db.query(models.RedPacketClaim).count() == 1
    assert len(answers) == 1 and "抢到" in (answers[0][1] or "")
    assert len(edits) == 1
    assert schedules == []


def test_claim_feedback_delay_zero_no_schedule(db, monkeypatch):
    db.add(models.SystemConfig(key="bot_msg_autodelete_seconds", value="0"))
    db.commit()
    store.invalidate("bot_msg_autodelete_seconds")
    _answers, _edits, sends, schedules = _patch_claim_io(db, monkeypatch)
    packet, _claimer = _setup_packet(db)
    cq = _claim_cq(packet.id, {"id": GROUP_CHAT_ID, "type": "supergroup"})
    callbacks.handle_callback(db, cq)
    assert len(sends) == 1  # 反馈照发
    assert schedules == []  # 延时 0 = 不调度自毁
