"""红包雨：系统出资、复用抢包、份数按间隔落下、幂等、不退给管理员。"""
from __future__ import annotations

import re
import sys
from datetime import datetime, timedelta

import pytest

sys.path.insert(0, ".")

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend import models, rain, welfare_redpacket
from backend.api import welfare_admin
from backend import config_schema
from backend.tg_bot import callbacks, command_scopes, ephemeral, poller, rain_ui, router, sender

TG_ADMIN = 940001
TG_USER = 940002
GROUP = -100940001
CJK4 = re.compile(r"^[一-鿿]{4}$")
LABEL_RE = re.compile(r"· \S+ <b>([^<]+)</b> \|")


class _FakeTimer:
    def __init__(self, interval, function, args=None, kwargs=None):
        self.interval = interval
        self.function = function
        self.args = args or ()
        self.kwargs = kwargs or {}
        self.daemon = False

    def start(self):
        self.started = True

    def cancel(self):
        self.cancelled = True


@pytest.fixture()
def db():
    engine = create_engine("sqlite:///:memory:")
    models.Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    session.add(models.ServerRealm(name="公益服", slug="main", access_mode="free"))
    session.commit()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


@pytest.fixture(autouse=True)
def _quiet(monkeypatch):
    monkeypatch.setattr(ephemeral, "Timer", _FakeTimer)
    router._user_cmd_limits.clear()
    router._group_windows.clear()
    yield
    router._user_cmd_limits.clear()
    router._group_windows.clear()


def _user(db, tg, name, points=1000, staff=False):
    row = models.WebUser(
        username=name, password_hash="x", telegram_id=tg,
        points=points, vitality=14, is_staff=staff, is_active=True,
        # 生产 staff 必有角色；/rain 是写操作，测试管理员默认 operator
        # （角色门用例见 tests/test_tg_bot_admin_roles.py）。
        admin_role="operator" if staff else None,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def _cfg(db, key, value):
    row = db.query(models.SystemConfig).filter(models.SystemConfig.key == key).first()
    if row is None:
        db.add(models.SystemConfig(key=key, value=str(value)))
    else:
        row.value = str(value)
    db.commit()


def _enable(db, **extra):
    _cfg(db, "bot_rain_enabled", "true")
    for key, value in extra.items():
        _cfg(db, key, value)


def _balance(db, user_id):
    db.expire_all()
    return int(db.query(models.WebUser.points).filter(models.WebUser.id == user_id).scalar() or 0)


def test_switch_defaults_off():
    assert welfare_admin.WELFARE_CONFIG_KEYS["bot_rain_enabled"] == "false"
    assert welfare_admin.WELFARE_CONFIG_KEYS["bot_rain_total"] == "100"
    schema = config_schema.WELFARE_CONFIG_SCHEMA
    assert schema["bot_rain_enabled"] == ("bool",)
    assert schema["bot_rain_count"][1:] == (1, 100)


def test_disabled_does_not_mint(db):
    admin = _user(db, TG_ADMIN, "admin", staff=True)
    res = rain.start_rain(db, admin, GROUP, idempotency_key="tg:1")
    assert res["reason"] == "disabled"
    assert _balance(db, admin.id) == 1000
    assert db.query(models.RedPacket).count() == 0


def test_system_funded_replay_and_panel_copy(db):
    _enable(db)
    admin = _user(db, TG_ADMIN, "admin", staff=True, points=80)
    first = rain.start_rain(db, admin, GROUP, idempotency_key="tg:2")
    second = rain.start_rain(db, admin, GROUP, idempotency_key="tg:2")
    assert first["ok"] and second["duplicate"]
    assert first["rain"].id == second["rain"].id
    assert _balance(db, admin.id) == 80
    packet = db.query(models.RedPacket).one()
    assert packet.funding == "system"
    assert packet.total_amount == 100 and packet.remaining_amount == 100
    text = rain_ui.open_text(db, first["rain"])
    assert text.startswith("▎<b>红包雨：总额 100，共 10 份，已落下 1 份")
    for label in LABEL_RE.findall(text):
        assert CJK4.match(label), label
    assert rain_ui.open_markup(first["rain"].id)["inline_keyboard"][0][0]["text"] == "抢"


def test_does_not_consume_user_send_limit(db):
    _enable(db)
    _cfg(db, "redpacket_send_limit_7d", "1")
    admin = _user(db, TG_ADMIN, "admin", staff=True, points=500)
    assert rain.start_rain(db, admin, GROUP, idempotency_key="tg:lim")["ok"]
    packet = welfare_redpacket.send_packet(db, admin, 10, 2)
    assert packet.funding == "user"
    assert _balance(db, admin.id) == 500 - 10 - 1  # 本体 10 + 默认 5% 手续费向上取整
    with pytest.raises(ValueError):
        welfare_redpacket.send_packet(db, admin, 10, 2)


def test_release_gate_and_reuse_claim(db):
    _enable(db, bot_rain_total="20", bot_rain_count="2", bot_rain_interval_sec="3600")
    admin = _user(db, TG_ADMIN, "admin", staff=True, points=50)
    bob = _user(db, TG_USER, "bob", points=0)
    cara = _user(db, TG_USER + 1, "cara", points=0)
    started = rain.start_rain(db, admin, GROUP, idempotency_key="tg:rel")
    packet_id = started["rain"].packet_id
    # 管理员自己也能抢（钱不是他出的）
    got = welfare_redpacket.claim_packet(db, admin, packet_id)
    assert got["amount"] >= 1
    assert _balance(db, admin.id) == 50 + got["amount"]
    with pytest.raises(ValueError, match="还没落下"):
        welfare_redpacket.claim_packet(db, bob, packet_id)
    assert _balance(db, bob.id) == 0
    # 间隔走完，第二份可以抢，两人合计正好是总额
    rain_row = started["rain"]
    rain_row.started_at = datetime.now() - timedelta(seconds=3600)
    db.commit()
    got2 = welfare_redpacket.claim_packet(db, bob, packet_id)
    assert got["amount"] + got2["amount"] == 20
    assert db.query(models.RedPacket).one().remaining_count == 0
    logs = db.query(models.PointsLog).filter(models.PointsLog.type == "redpacket_rain").all()
    assert sum(row.amount for row in logs) == 20
    with pytest.raises(ValueError):
        welfare_redpacket.claim_packet(db, cara, packet_id)


def test_user_packet_still_blocks_self_claim(db):
    alice = _user(db, TG_USER, "alice", points=100)
    packet = welfare_redpacket.send_packet(db, alice, 10, 2)
    with pytest.raises(ValueError, match="不能抢自己"):
        welfare_redpacket.claim_packet(db, alice, packet.id)


def test_expired_system_packet_is_void_not_refunded(db):
    _enable(db, bot_rain_total="30", bot_rain_count="3", bot_rain_interval_sec="0")
    admin = _user(db, TG_ADMIN, "admin", staff=True, points=40)
    rain.start_rain(db, admin, GROUP, idempotency_key="tg:exp")
    packet = db.query(models.RedPacket).one()
    packet.expires_at = datetime.now() - timedelta(seconds=5)
    db.commit()
    assert welfare_redpacket.refund_expired(db) == 1
    assert _balance(db, admin.id) == 40
    fresh = db.query(models.RedPacket).one()
    assert fresh.remaining_amount == 0 and fresh.remaining_count == 0


def test_one_rain_per_chat(db):
    _enable(db)
    admin = _user(db, TG_ADMIN, "admin", staff=True)
    other = _user(db, TG_ADMIN + 3, "boss", staff=True)
    assert rain.start_rain(db, admin, GROUP, idempotency_key="tg:a")["ok"]
    busy = rain.start_rain(db, other, GROUP, idempotency_key="tg:b")
    assert busy["reason"] == "busy"
    assert _balance(db, admin.id) == 1000


def test_daily_cap_rejects_the_whole_share(db):
    _enable(db, bot_rain_daily_win_cap="5", bot_rain_total="10", bot_rain_count="1", bot_rain_interval_sec="0")
    admin = _user(db, TG_ADMIN, "admin", staff=True)
    bob = _user(db, TG_USER, "bob", points=0)
    started = rain.start_rain(db, admin, GROUP, idempotency_key="tg:cap")
    with pytest.raises(ValueError, match="上限"):
        welfare_redpacket.claim_packet(db, bob, started["rain"].packet_id)
    assert _balance(db, bob.id) == 0
    packet = db.query(models.RedPacket).one()
    assert packet.remaining_count == 1 and packet.remaining_amount == 10


def test_callback_unbound_is_toast_only(db, monkeypatch):
    _enable(db, bot_rain_interval_sec="0")
    admin = _user(db, TG_ADMIN, "admin", staff=True)
    started = rain.start_rain(db, admin, GROUP, idempotency_key="tg:cb")
    answers = []
    monkeypatch.setattr(
        sender, "answer_callback_query",
        lambda *a, **kw: answers.append(kw.get("text") or "") or (True, None),
    )
    callbacks.handle_callback(db, {
        "id": "cq-rain",
        "data": f"rain_grab:{started['rain'].id}",
        "from": {"id": 999001, "first_name": "X"},
        "message": {"message_id": 3, "chat": {"id": GROUP, "type": "supergroup"}},
    })
    assert answers and "绑定" in answers[-1]
    assert _balance(db, admin.id) == 1000
    assert db.query(models.RedPacket).one().remaining_count == 10


def test_callback_claim_toasts_and_collapses_when_finished(db, monkeypatch):
    _enable(db, bot_rain_total="10", bot_rain_count="1", bot_rain_interval_sec="0")
    admin = _user(db, TG_ADMIN, "admin", staff=True, points=10)
    bob = _user(db, TG_USER, "bob", points=0)
    started = rain.start_rain(db, admin, GROUP, idempotency_key="tg:fin")
    rain.attach_panel(db, started["rain"].id, 8)
    answers = []
    edited = []
    monkeypatch.setattr(
        sender, "answer_callback_query",
        lambda *a, **kw: answers.append(kw.get("text") or "") or (True, None),
    )
    monkeypatch.setattr(
        sender, "edit_message_text",
        lambda *a, **kw: edited.append(a[3]) or (True, None),
    )
    callbacks.handle_callback(db, {
        "id": "cq-fin",
        "data": f"rain_grab:{started['rain'].id}",
        "from": {"id": TG_USER, "first_name": "Bob"},
        "message": {"message_id": 8, "chat": {"id": GROUP, "type": "supergroup"}},
    })
    assert answers and answers[-1].startswith("抢到 ")
    assert _balance(db, bob.id) == 10
    assert _balance(db, admin.id) == 10
    assert edited and "红包雨结束" in edited[-1] and "\n" not in edited[-1]
    assert db.query(models.RedPacketRainLive).count() == 0


def _wire_feedback_capture(monkeypatch):
    """录下群消息发送与自毁调度，分开返回：反馈消息 (chat_id, text, msg_id) 与
    自毁记录 (chat_id, msg_id, delay)。反馈经 send_message_ex 发、自毁经
    ephemeral.schedule_delete 到期删；面板编辑走 edit_message_text 不是新消息。"""
    sent = []
    deleted = []

    def fake_send(db_, chat_id, text, reply_markup=None):
        mid = 700 + len(sent)
        sent.append({"chat_id": chat_id, "text": text, "msg_id": mid})
        return True, None, mid

    monkeypatch.setattr(sender, "send_message_ex", fake_send)
    monkeypatch.setattr(
        ephemeral, "schedule_delete",
        lambda chat_id, message_id, delay: deleted.append((chat_id, message_id, delay)) or True,
    )
    return sent, deleted


def test_rain_grab_success_sends_group_feedback(db, monkeypatch):
    """红包雨抢成功：群里补发 1 条反馈（谁抢到多少、剩余几份），反馈按默认
    延时自毁，雨面板消息永不被调度自毁（用户口径：抢了有反馈）。"""
    _enable(db, bot_rain_total="100", bot_rain_count="10", bot_rain_interval_sec="0")
    admin = _user(db, TG_ADMIN, "admin", staff=True)
    _user(db, TG_USER, "bob", points=0)
    started = rain.start_rain(db, admin, GROUP, idempotency_key="tg:fb-rain")
    rain.attach_panel(db, started["rain"].id, 8)
    answers = []
    monkeypatch.setattr(
        sender, "answer_callback_query",
        lambda *a, **kw: answers.append(a[3] if len(a) > 3 else kw.get("text") or "") or (True, None),
    )
    monkeypatch.setattr(sender, "edit_message_text", lambda *a, **kw: (True, None))
    sent, deleted = _wire_feedback_capture(monkeypatch)
    callbacks.handle_callback(db, {
        "id": "cq-fb-rain",
        "data": f"rain_grab:{started['rain'].id}",
        "from": {"id": TG_USER, "first_name": "Bob"},
        "message": {"message_id": 8, "chat": {"id": GROUP, "type": "supergroup"}},
    })
    assert answers and answers[-1].startswith("抢到 ")
    amount = int(answers[-1].split(" ")[1])
    assert len(sent) == 1
    fb = sent[0]
    assert fb["chat_id"] == GROUP
    assert "<b>bob</b>" in fb["text"] and f"<b>{amount} 能量</b>" in fb["text"]
    assert "还剩 <b>9 个</b>" in fb["text"]
    assert deleted == [(GROUP, fb["msg_id"], 30)]
    assert (GROUP, 8, 30) not in deleted


def test_system_packet_claim_via_panel_button_sends_group_feedback(db, monkeypatch):
    """走普通 claim 按钮领系统出资红包（红包雨的包）：同样补发 1 条群反馈。"""
    _enable(db, bot_rain_total="100", bot_rain_count="10", bot_rain_interval_sec="0")
    admin = _user(db, TG_ADMIN, "admin", staff=True)
    _user(db, TG_USER, "bob", points=0)
    started = rain.start_rain(db, admin, GROUP, idempotency_key="tg:fb-sys")
    packet_id = started["rain"].packet_id
    answers = []
    monkeypatch.setattr(
        sender, "answer_callback_query",
        lambda db_, cq_id, text=None, show_alert=False: answers.append(text or "") or (True, None),
    )
    monkeypatch.setattr(sender, "edit_message_text", lambda *a, **kw: (True, None))
    sent, deleted = _wire_feedback_capture(monkeypatch)
    callbacks.handle_callback(db, {
        "id": "cq-fb-sys",
        "data": f"redpacket_claim:{packet_id}",
        "from": {"id": TG_USER, "first_name": "Bob"},
        "message": {"message_id": 9, "chat": {"id": GROUP, "type": "supergroup"}},
    })
    assert answers and answers[-1].startswith("🧧 恭喜抢到 ")
    amount = int(re.search(r"(\d+)", answers[-1]).group(1))
    assert len(sent) == 1
    fb = sent[0]
    assert fb["chat_id"] == GROUP
    assert "<b>bob</b>" in fb["text"] and f"<b>{amount} 能量</b>" in fb["text"]
    assert "还剩 <b>9 个</b>" in fb["text"]
    assert deleted == [(GROUP, fb["msg_id"], 30)]


def test_rain_grab_not_open_sends_no_group_feedback(db, monkeypatch):
    """失败路径（下一份还没落下）：只有 alert 弹窗，0 条群反馈。"""
    _enable(db, bot_rain_total="20", bot_rain_count="2", bot_rain_interval_sec="3600")
    admin = _user(db, TG_ADMIN, "admin", staff=True, points=50)
    _user(db, TG_USER, "bob", points=0)
    started = rain.start_rain(db, admin, GROUP, idempotency_key="tg:fb-gate")
    packet_id = started["rain"].packet_id
    # 第一份先被管理员领走，第二份要等间隔走完才落下
    welfare_redpacket.claim_packet(db, admin, packet_id)
    answers = []
    alerts = []
    monkeypatch.setattr(
        sender, "answer_callback_query",
        lambda *a, **kw: (
            answers.append(kw.get("text") or ""),
            alerts.append(bool(kw.get("show_alert"))),
        )[-1] or (True, None),
    )
    sent, deleted = _wire_feedback_capture(monkeypatch)
    callbacks.handle_callback(db, {
        "id": "cq-fb-gate",
        "data": f"rain_grab:{started['rain'].id}",
        "from": {"id": TG_USER, "first_name": "Bob"},
        "message": {"message_id": 8, "chat": {"id": GROUP, "type": "supergroup"}},
    })
    assert answers and "还没落下" in answers[-1] and alerts[-1] is True
    assert sent == [] and deleted == []


def test_router_staff_only_and_replay(db, monkeypatch):
    _enable(db)
    _user(db, TG_ADMIN, "admin", staff=True, points=20)
    sent = []
    monkeypatch.setattr(
        sender, "send_message_ex",
        lambda *a, **kw: sent.append((a[2], a[3] if len(a) > 3 else None)) or (True, None, 15),
    )
    monkeypatch.setattr(sender, "send_message", lambda *a, **kw: sent.append((a[2], None)) or (True, None))
    monkeypatch.setattr(ephemeral, "schedule_delete", lambda *a, **kw: True)
    plain = _user(db, TG_USER, "bob", staff=False)
    router.dispatch(db, {
        "update_id": 7,
        "message": {
            "message_id": 1, "text": "/rain",
            "from": {"id": TG_USER, "first_name": "B"},
            "chat": {"id": GROUP, "type": "supergroup"},
        },
    })
    assert sent and "仅管理员" in sent[-1][0]
    assert db.query(models.RedPacketRain).count() == 0
    router._user_cmd_limits.clear()
    sent.clear()
    update = {
        "update_id": 8,
        "message": {
            "message_id": 2, "text": "/rain",
            "from": {"id": TG_ADMIN, "first_name": "A"},
            "chat": {"id": GROUP, "type": "supergroup"},
        },
    }
    router.dispatch(db, update)
    assert len(sent) == 1
    assert "红包雨" in sent[0][0]
    assert sent[0][1]["inline_keyboard"][0][0]["text"] == "抢"
    assert _balance(db, plain.id) == 1000
    admin_points = _balance(db, db.query(models.WebUser).filter_by(telegram_id=TG_ADMIN).one().id)
    router._user_cmd_limits.clear()
    router.dispatch(db, update)
    assert len(sent) == 1
    assert _balance(db, db.query(models.WebUser).filter_by(telegram_id=TG_ADMIN).one().id) == admin_points


def test_schedule_once_per_day(db, monkeypatch):
    _enable(db, bot_rain_schedule="21:00")
    _cfg(db, "bot_group_ids", str(GROUP))
    _user(db, TG_ADMIN, "admin", staff=True)
    sent = []
    monkeypatch.setattr(
        sender, "send_message_ex",
        lambda *a, **kw: sent.append(a[1]) or (True, None, 21),
    )
    when = datetime(2026, 11, 1, 21, 5)
    assert any(item.startswith("start:") for item in rain_ui.tick(db, when))
    assert db.query(models.RedPacketRain).count() == 1
    assert sent == [GROUP]
    again = rain_ui.tick(db, when)
    assert not any(str(item).startswith("start:") for item in again)
    assert db.query(models.RedPacketRain).count() == 1


def test_command_registration():
    assert ("rain", "[群/私聊] 红包雨（管理员）") in poller.BOT_COMMANDS
    assert "rain" in command_scopes.ADMIN_ONLY_COMMANDS
    assert "rain" not in command_scopes.GROUP_COMMANDS
