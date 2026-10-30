"""B3 测试：TG Bot 红包（/redpacket 发红包 + 抢红包按钮）。"""
from __future__ import annotations

import sys
from datetime import datetime, timedelta

import pytest

sys.path.insert(0, ".")

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend import models
from backend.integrations import store
from backend.tg_bot import handlers, redpacket_common, sender


@pytest.fixture()
def db():
    engine = create_engine("sqlite:///:memory:")
    models.WebUser.__table__.create(engine, checkfirst=True)
    models.TgBindCode.__table__.create(engine, checkfirst=True)
    models.SystemConfig.__table__.create(engine, checkfirst=True)
    models.RedPacket.__table__.create(engine, checkfirst=True)
    models.RedPacketClaim.__table__.create(engine, checkfirst=True)
    models.PointsLog.__table__.create(engine, checkfirst=True)
    # store.get_value 有进程级 60 秒缓存，fixture 开头清掉防止用例间串味
    store.invalidate()
    Session = sessionmaker(bind=engine)
    session = Session()
    yield session
    session.close()


def _make_user(db, username="u1", tg_id=None, points=0):
    u = models.WebUser(username=username, telegram_id=tg_id)
    for col in ("password_hash",):
        if hasattr(u, col) and getattr(u, col) is None:
            try:
                setattr(u, col, "")
            except Exception:
                pass
    u.points = points
    db.add(u)
    db.commit()
    return u


def _text(result):
    """统一从返回值（str 或 (text, markup) 元组）中取出文本。"""
    if isinstance(result, tuple):
        return result[0]
    return result


def test_redpacket_disabled(db):
    db.add(models.SystemConfig(key="bot_redpacket_enabled", value="false"))
    db.commit()
    store.invalidate("bot_redpacket_enabled")
    result = handlers.handle_redpacket(db, {"id": 1001}, 1, "100 10")
    assert _text(result) == "🧧 红包功能已关闭"


def test_redpacket_unbound(db):
    result = handlers.handle_redpacket(db, {"id": 999999}, 1, "10 1")
    assert "/bind" in _text(result)


def test_redpacket_missing_args(db):
    _make_user(db, username="arg1", tg_id=1011, points=1000)
    result = handlers.handle_redpacket(db, {"id": 1011}, 1, "")
    assert "🧧 用法 | /redpacket" in _text(result)


def test_redpacket_non_numeric_args(db):
    _make_user(db, username="arg2", tg_id=1012, points=1000)
    result = handlers.handle_redpacket(db, {"id": 1012}, 1, "abc xyz")
    assert "🧧 用法 | /redpacket" in _text(result)


def test_redpacket_amount_less_than_count(db):
    _make_user(db, username="dave", tg_id=1004, points=1000)
    result = handlers.handle_redpacket(db, {"id": 1004}, 1, "10 100")
    assert "不能小于个数" in _text(result)


def test_redpacket_insufficient_points(db):
    _make_user(db, username="erin", tg_id=1005, points=50)
    result = handlers.handle_redpacket(db, {"id": 1005}, 1, "100 10")
    assert "能量不足" in _text(result)


def test_redpacket_success(db):
    user = _make_user(db, username="alice", tg_id=1001, points=1000)
    result = handlers.handle_redpacket(db, {"id": 1001}, 1, "100 10")
    assert isinstance(result, tuple)
    text, reply_markup = result
    assert "总额 100 能量" in text
    assert "共 10 个" in text
    assert "剩余 10 个" in text
    assert isinstance(reply_markup, dict)
    keyboard = reply_markup["inline_keyboard"]
    assert len(keyboard) == 1
    assert len(keyboard[0]) == 1
    button = keyboard[0][0]
    assert button["text"] == "🧧 抢红包"
    callback_data = button["callback_data"]
    assert callback_data.startswith("redpacket_claim:")
    packet_id = int(callback_data.split(":", 1)[1])
    assert reply_markup == {
        "inline_keyboard": [
            [{"text": "🧧 抢红包", "callback_data": f"redpacket_claim:{packet_id}"}]
        ]
    }
    db.refresh(user)
    assert user.points == 895
    packet = db.query(models.RedPacket).filter(models.RedPacket.id == packet_id).first()
    assert packet is not None
    assert packet.total_amount == 100


def test_redpacket_exact_balance(db):
    user = _make_user(db, username="bob", tg_id=1002, points=105)
    result = handlers.handle_redpacket(db, {"id": 1002}, 1, "100 1")
    assert isinstance(result, tuple)
    db.refresh(user)
    assert user.points == 0


def test_redpacket_zero_fee(db):
    db.add(models.SystemConfig(key="redpacket_fee_pct", value="0"))
    db.commit()
    store.invalidate("redpacket_fee_pct")
    user = _make_user(db, username="carol", tg_id=1003, points=100)
    result = handlers.handle_redpacket(db, {"id": 1003}, 1, "100 10")
    assert isinstance(result, tuple)
    db.refresh(user)
    assert user.points == 0


def test_parse_claim_data_valid():
    assert redpacket_common.parse_claim_data("redpacket_claim:42") == 42


def test_parse_claim_data_non_numeric():
    assert redpacket_common.parse_claim_data("redpacket_claim:abc") is None


def test_parse_claim_data_unknown_prefix():
    assert redpacket_common.parse_claim_data("xxx") is None


def test_parse_claim_data_none():
    assert redpacket_common.parse_claim_data(None) is None


def test_parse_claim_data_empty():
    assert redpacket_common.parse_claim_data("") is None


def test_callback_unbound_click(db, monkeypatch):
    from backend.tg_bot import callbacks
    from backend import welfare_redpacket
    answers, edits = [], []
    monkeypatch.setattr(sender, "answer_callback_query",
                        lambda db_, cq_id, text=None, show_alert=False: answers.append((cq_id, text, show_alert)) or (True, None))
    monkeypatch.setattr(sender, "edit_message_text",
                        lambda db_, chat_id, message_id, text, reply_markup=None: edits.append((chat_id, message_id, text, reply_markup)) or (True, None))
    sender_user = _make_user(db, "s_unbound", 700010, 1000)
    welfare_redpacket.send_packet(db, sender_user, 50, 5)
    packet = db.query(models.RedPacket).filter_by(sender_id=sender_user.id).first()
    cq = {"id": "cq1", "from": {"id": 700009}, "data": f"redpacket_claim:{packet.id}",
          "message": {"message_id": 99, "chat": {"id": -100123}}}
    callbacks.handle_callback(db, cq)
    assert len(answers) == 1
    assert "绑定" in (answers[0][1] or "")
    assert answers[0][2] is True
    assert edits == []
    assert db.query(models.RedPacketClaim).count() == 0


def test_callback_bot_disabled(db, monkeypatch):
    from backend.tg_bot import callbacks
    from backend import welfare_redpacket
    answers, edits = [], []
    monkeypatch.setattr(sender, "answer_callback_query",
                        lambda db_, cq_id, text=None, show_alert=False: answers.append((cq_id, text, show_alert)) or (True, None))
    monkeypatch.setattr(sender, "edit_message_text",
                        lambda db_, chat_id, message_id, text, reply_markup=None: edits.append((chat_id, message_id, text, reply_markup)) or (True, None))
    sender_user = _make_user(db, "s_off", 700011, 1000)
    welfare_redpacket.send_packet(db, sender_user, 50, 5)
    packet = db.query(models.RedPacket).filter_by(sender_id=sender_user.id).first()
    _make_user(db, "c_off", 700012, 0)
    db.add(models.SystemConfig(key="bot_redpacket_enabled", value="false"))
    db.commit()
    store.invalidate("bot_redpacket_enabled")
    cq = {"id": "cq2", "from": {"id": 700012}, "data": f"redpacket_claim:{packet.id}",
          "message": {"message_id": 99, "chat": {"id": -100123}}}
    callbacks.handle_callback(db, cq)
    assert len(answers) == 1
    assert "已关闭" in (answers[0][1] or "")
    assert edits == []


def test_callback_claim_success(db, monkeypatch):
    from backend.tg_bot import callbacks
    from backend import welfare_redpacket
    answers, edits = [], []
    monkeypatch.setattr(sender, "answer_callback_query",
                        lambda db_, cq_id, text=None, show_alert=False: answers.append((cq_id, text, show_alert)) or (True, None))
    monkeypatch.setattr(sender, "edit_message_text",
                        lambda db_, chat_id, message_id, text, reply_markup=None: edits.append((chat_id, message_id, text, reply_markup)) or (True, None))
    sender_user = _make_user(db, "s_ok", 700021, 1000)
    packet = welfare_redpacket.send_packet(db, sender_user, 50, 5)
    claimer = _make_user(db, "c_ok", 700022, 0)
    cq = {"id": "cq-ok", "from": {"id": 700022}, "data": f"redpacket_claim:{packet.id}", "message": {"message_id": 99, "chat": {"id": -100123}}}
    callbacks.handle_callback(db, cq)
    assert len(answers) == 1
    assert "抢到" in answers[0][1]
    assert len(edits) == 1
    assert "剩余 4 个" in edits[0][2]
    assert edits[0][3]["inline_keyboard"][0][0]["text"] == "🧧 抢红包"
    assert db.query(models.RedPacketClaim).count() == 1
    db.refresh(claimer)
    assert claimer.points > 0


def test_callback_duplicate_claim(db, monkeypatch):
    from backend.tg_bot import callbacks
    from backend import welfare_redpacket
    answers, edits = [], []
    monkeypatch.setattr(sender, "answer_callback_query",
                        lambda db_, cq_id, text=None, show_alert=False: answers.append((cq_id, text, show_alert)) or (True, None))
    monkeypatch.setattr(sender, "edit_message_text",
                        lambda db_, chat_id, message_id, text, reply_markup=None: edits.append((chat_id, message_id, text, reply_markup)) or (True, None))
    sender_user = _make_user(db, "s_dup", 700023, 1000)
    packet = welfare_redpacket.send_packet(db, sender_user, 50, 5)
    claimer = _make_user(db, "c_dup", 700024, 0)
    cq = {"id": "cq-dup", "from": {"id": 700024}, "data": f"redpacket_claim:{packet.id}", "message": {"message_id": 98, "chat": {"id": -100123}}}
    callbacks.handle_callback(db, cq)
    callbacks.handle_callback(db, cq)
    assert len(answers) == 2
    assert "已经领过" in answers[1][1]
    assert len(edits) == 1
    assert db.query(models.RedPacketClaim).count() == 1


def test_callback_last_claim_finishes(db, monkeypatch):
    from backend.tg_bot import callbacks
    from backend import welfare_redpacket
    answers, edits = [], []
    monkeypatch.setattr(sender, "answer_callback_query",
                        lambda db_, cq_id, text=None, show_alert=False: answers.append((cq_id, text, show_alert)) or (True, None))
    monkeypatch.setattr(sender, "edit_message_text",
                        lambda db_, chat_id, message_id, text, reply_markup=None: edits.append((chat_id, message_id, text, reply_markup)) or (True, None))
    sender_user = _make_user(db, "s_last", 700025, 1000)
    packet = welfare_redpacket.send_packet(db, sender_user, 30, 1)
    claimer = _make_user(db, "c_last", 700026, 0)
    cq = {"id": "cq-last", "from": {"id": 700026}, "data": f"redpacket_claim:{packet.id}", "message": {"message_id": 97, "chat": {"id": -100123}}}
    callbacks.handle_callback(db, cq)
    assert len(edits) == 1
    assert "已抢完" in edits[0][2]
    assert edits[0][3] == {"inline_keyboard": []}
    assert "抢到" in answers[0][1]


def test_callback_expired_packet(db, monkeypatch):
    from backend.tg_bot import callbacks
    answers, edits = [], []
    monkeypatch.setattr(sender, "answer_callback_query",
                        lambda db_, cq_id, text=None, show_alert=False: answers.append((cq_id, text, show_alert)) or (True, None))
    monkeypatch.setattr(sender, "edit_message_text",
                        lambda db_, chat_id, message_id, text, reply_markup=None: edits.append((chat_id, message_id, text, reply_markup)) or (True, None))
    sender_user = _make_user(db, "s_exp", 700027, 1000)
    packet = models.RedPacket(sender_id=sender_user.id, total_amount=50, total_count=2, remaining_amount=50, remaining_count=2, expires_at=datetime.now() - timedelta(hours=1))
    db.add(packet)
    db.commit()
    claimer = _make_user(db, "c_exp", 700028, 0)
    cq = {"id": "cq-exp", "from": {"id": 700028}, "data": f"redpacket_claim:{packet.id}", "message": {"message_id": 96, "chat": {"id": -100123}}}
    callbacks.handle_callback(db, cq)
    assert len(answers) == 1
    assert "已过期" in answers[0][1]
    assert len(edits) == 1
    assert "已过期" in edits[0][2]
    assert edits[0][3] == {"inline_keyboard": []}
    assert db.query(models.RedPacketClaim).count() == 0


def test_callback_unknown_data_ignored(db, monkeypatch):
    from backend.tg_bot import callbacks
    answers, edits = [], []
    monkeypatch.setattr(sender, "answer_callback_query",
                        lambda db_, cq_id, text=None, show_alert=False: answers.append((cq_id, text, show_alert)) or (True, None))
    monkeypatch.setattr(sender, "edit_message_text",
                        lambda db_, chat_id, message_id, text, reply_markup=None: edits.append((chat_id, message_id, text, reply_markup)) or (True, None))
    cq = {"id": "cq-x", "from": {"id": 700041}, "data": "hello", "message": {"message_id": 1, "chat": {"id": 2}}}
    callbacks.handle_callback(db, cq)
    assert answers == []
    assert edits == []


def test_callback_packet_not_found(db, monkeypatch):
    from backend.tg_bot import callbacks
    answers, edits = [], []
    monkeypatch.setattr(sender, "answer_callback_query",
                        lambda db_, cq_id, text=None, show_alert=False: answers.append((cq_id, text, show_alert)) or (True, None))
    monkeypatch.setattr(sender, "edit_message_text",
                        lambda db_, chat_id, message_id, text, reply_markup=None: edits.append((chat_id, message_id, text, reply_markup)) or (True, None))
    _make_user(db, "c_nf", 700042, 0)
    cq = {"id": "cq-nf", "from": {"id": 700042}, "data": "redpacket_claim:99999", "message": {"message_id": 1, "chat": {"id": 2}}}
    callbacks.handle_callback(db, cq)
    assert len(answers) == 1
    assert "红包不存在" in answers[0][1]
    assert edits == []


def test_router_dispatch_redpacket(db, monkeypatch):
    from backend.tg_bot import router
    _make_user(db, "r_dp", 700043, 1000)
    sent = []
    monkeypatch.setattr(sender, "send_message",
                        lambda db_, chat_id, text, reply_markup=None, **kw: sent.append((text, reply_markup)) or (True, None))
    update = {"update_id": 43, "message": {"message_id": 43, "from": {"id": 700043, "first_name": "R"}, "chat": {"id": -10043, "type": "group"}, "text": "/redpacket 100 5"}}
    router.dispatch(db, update)
    assert len(sent) == 1
    assert "总额 100 能量" in sent[0][0]
    assert sent[0][1]["inline_keyboard"][0][0]["text"] == "🧧 抢红包"


def test_rate_limit_redpacket():
    from backend.tg_bot.router import _user_cmd_allow
    assert _user_cmd_allow(930043, "/redpacket", 3.0) is True
    assert _user_cmd_allow(930043, "/redpacket", 3.0) is False
    assert _user_cmd_allow(930043, "/points", 3.0) is True


def test_router_dispatch_redpacket_replay_idempotent(db, monkeypatch):
    from backend.tg_bot import router

    _make_user(db, "r_dp_replay", 700044, 1000)
    sent = []
    monkeypatch.setattr(
        sender,
        "send_message",
        lambda db_, chat_id, text, reply_markup=None, **kw: sent.append((text, reply_markup)) or (True, None),
    )

    update = {
        "update_id": 44,
        "message": {
            "message_id": 44,
            "from": {"id": 700044, "first_name": "R"},
            "chat": {"id": -10044, "type": "group"},
            "text": "/redpacket 100 5",
        },
    }

    router.dispatch(db, update)
    assert len(sent) == 1
    assert "总额 100 能量" in sent[0][0]

    # 模拟 poller 崩溃重启：进程内限流状态全部清零，update 被重放
    router._user_cmd_limits.clear()
    router._group_windows.clear()

    router.dispatch(db, update)

    # 两次都发出消息
    assert len(sent) == 2
    assert "总额 100 能量" in sent[1][0]

    # 只创建了一个红包
    assert db.query(models.RedPacket).count() == 1

    # 只扣一次款：100 本体 + 5% 手续费 5 = 105
    db.expire_all()
    u = db.query(models.WebUser).filter(models.WebUser.telegram_id == 700044).one()
    assert u.points == 895
