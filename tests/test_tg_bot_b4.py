"""B4 测试：TG Bot 群聊策略、限流、后台开关。"""
from __future__ import annotations

import sys
import time

import pytest

sys.path.insert(0, ".")

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend import models
from backend.tg_bot import policy, router, sender


@pytest.fixture()
def db():
    engine = create_engine("sqlite:///:memory:")
    models.WebUser.__table__.create(engine, checkfirst=True)
    models.TgBindCode.__table__.create(engine, checkfirst=True)
    models.SystemConfig.__table__.create(engine, checkfirst=True)
    models.UserSubscription.__table__.create(engine, checkfirst=True)
    models.SubscriptionPlan.__table__.create(engine, checkfirst=True)
    Session = sessionmaker(bind=engine)
    session = Session()
    yield session
    session.close()


def _make_user(db, username="u1", tg_id=None):
    u = models.WebUser(username=username, telegram_id=tg_id)
    for col in ("password_hash",):
        if hasattr(u, col) and getattr(u, col) is None:
            try:
                setattr(u, col, "")
            except Exception:
                pass
    db.add(u)
    db.commit()
    return u


def _set_cfg(db, key, value):
    db.add(models.SystemConfig(key=key, value=value))
    db.commit()


def _msg(tg_id, chat_id, text, chat_type="private"):
    return {
        "update_id": 1,
        "message": {
            "message_id": 1,
            "from": {"id": tg_id, "first_name": "T"},
            "chat": {"id": chat_id, "type": chat_type},
            "text": text,
        },
    }


@pytest.fixture()
def sent(monkeypatch):
    """劫持 sender.send_message，记录所有发出的消息。"""
    out = []
    monkeypatch.setattr(
        sender,
        "send_message",
        lambda db_, chat_id, text, reply_markup=None, **kw: out.append(text) or (True, None),
    )
    # 二期自毁接线：群回复改走 send_message_ex 拿 message_id，同样记录；
    # schedule_delete 打桩成空操作，避免测试里起真定时器
    monkeypatch.setattr(
        sender,
        "send_message_ex",
        lambda db_, chat_id, text, reply_markup=None, **kw: out.append(text) or (True, None, 1),
    )
    monkeypatch.setattr(
        "backend.tg_bot.ephemeral.schedule_delete", lambda *a, **k: True)
    return out


# ---------- policy 单元测试 ----------

def test_policy_defaults_empty_db(db):
    cfg = policy.read_bot_config(db)
    assert policy.is_enabled(cfg) is True
    assert policy.is_group_allowed(cfg, -100999) is True  # 白名单为空=所有群启用
    assert policy.is_group_allowed(cfg, None) is False
    assert policy.rate_limit_seconds(cfg) == 3.0
    assert policy.group_rate_limit(cfg) == 20
    assert policy.is_command_enabled(cfg, "/checkin") is True
    assert policy.is_command_enabled(cfg, "/start") is True  # 不在开关表=常开
    assert policy.is_redpacket_enabled(cfg) is True


def test_policy_parse_group_ids():
    assert policy.parse_group_ids("") == set()
    assert policy.parse_group_ids("   ") == set()
    assert policy.parse_group_ids("-100123, 456") == {-100123, 456}
    assert policy.parse_group_ids("abc,123,,  456 ") == {123, 456}  # 非法片段跳过
    assert policy.parse_group_ids(None) == set()


def test_policy_invalid_values_fall_back(db):
    _set_cfg(db, "bot_rate_limit_seconds", "abc")
    _set_cfg(db, "bot_group_rate_limit", "-5")
    _set_cfg(db, "bot_enabled", "yes")  # 非 true → 关闭
    cfg = policy.read_bot_config(db)
    assert policy.rate_limit_seconds(cfg) == 3.0
    assert policy.group_rate_limit(cfg) == 20
    assert policy.is_enabled(cfg) is False


def test_policy_bool_tolerant(db):
    _set_cfg(db, "bot_enabled", " True ")
    _set_cfg(db, "bot_cmd_checkin", "FALSE")
    cfg = policy.read_bot_config(db)
    assert policy.is_enabled(cfg) is True
    assert policy.is_command_enabled(cfg, "/checkin") is False


def test_policy_decimal_rate_limit(db):
    _set_cfg(db, "bot_rate_limit_seconds", "1.5")
    cfg = policy.read_bot_config(db)
    assert policy.rate_limit_seconds(cfg) == 1.5


# ---------- 群白名单 ----------

def test_group_whitelist_blocks(db, sent):
    _set_cfg(db, "bot_group_ids", "-100111")
    _make_user(db, "g1", 610001)
    router.dispatch(db, _msg(610001, -100222, "/points", "supergroup"))
    assert sent == []  # 非名单群静默忽略


def test_group_whitelist_allows(db, sent):
    _set_cfg(db, "bot_group_ids", "-100111, -100222")
    _make_user(db, "g2", 610002)
    router.dispatch(db, _msg(610002, -100222, "/points", "group"))
    assert len(sent) == 1 and "当前能量" in sent[0]


def test_private_chat_unaffected_by_whitelist(db, sent):
    _set_cfg(db, "bot_group_ids", "-100111")
    _make_user(db, "p1", 610003)
    router.dispatch(db, _msg(610003, 610003, "/points", "private"))
    assert len(sent) == 1 and "当前能量" in sent[0]


def test_bind_code_in_blocked_group_ignored(db, sent):
    _set_cfg(db, "bot_group_ids", "-100111")
    router.dispatch(db, _msg(610004, -100222, "123456", "group"))
    assert sent == []


def test_callback_in_blocked_group_ignored(db, monkeypatch):
    _set_cfg(db, "bot_group_ids", "-100111")
    answers = []
    monkeypatch.setattr(
        sender, "answer_callback_query",
        lambda db_, cq_id, text=None, show_alert=False: answers.append(text) or (True, None),
    )
    cq = {"id": "cq-b4", "from": {"id": 610005}, "data": "redpacket_claim:1",
          "message": {"message_id": 7, "chat": {"id": -100222, "type": "group"}}}
    router.dispatch(db, {"update_id": 2, "callback_query": cq})
    assert answers == []


# ---------- 总开关 ----------

def test_master_switch_off_message(db, sent):
    _set_cfg(db, "bot_enabled", "false")
    _make_user(db, "m1", 620001)
    router.dispatch(db, _msg(620001, 620001, "/points"))
    assert sent == []


def test_master_switch_off_callback(db, monkeypatch):
    _set_cfg(db, "bot_enabled", "false")
    answers = []
    monkeypatch.setattr(
        sender, "answer_callback_query",
        lambda db_, cq_id, text=None, show_alert=False: answers.append(text) or (True, None),
    )
    cq = {"id": "cq-b4m", "from": {"id": 620002}, "data": "redpacket_claim:1",
          "message": {"message_id": 7, "chat": {"id": 620002, "type": "private"}}}
    router.dispatch(db, {"update_id": 3, "callback_query": cq})
    assert answers == []


# ---------- 单命令开关 ----------

def test_command_switch_off(db, sent):
    _set_cfg(db, "bot_cmd_checkin", "false")
    _make_user(db, "c1", 630001)
    router.dispatch(db, _msg(630001, 630001, "/checkin"))
    assert sent == ["该功能已关闭"]


def test_command_switch_start_always_on(db, sent):
    _set_cfg(db, "bot_cmd_checkin", "false")
    _set_cfg(db, "bot_cmd_bind", "false")
    router.dispatch(db, _msg(630002, 630002, "/start"))
    assert len(sent) == 1 and "想看的片，喊一声就来" in sent[0]


def test_redpacket_switch_off_dispatch(db, sent):
    _set_cfg(db, "bot_redpacket_enabled", "false")
    _make_user(db, "r1", 630003)
    router.dispatch(db, _msg(630003, 630003, "/redpacket 100 5"))
    assert sent == ["🧧 红包功能已关闭"]


def test_callback_redpacket_switch_off(db, monkeypatch):
    _set_cfg(db, "bot_redpacket_enabled", "false")
    answers = []
    monkeypatch.setattr(
        sender, "answer_callback_query",
        lambda db_, cq_id, text=None, show_alert=False: answers.append(text) or (True, None),
    )
    _make_user(db, "r2", 630004)
    cq = {"id": "cq-b4r", "from": {"id": 630004}, "data": "redpacket_claim:1",
          "message": {"message_id": 7, "chat": {"id": 630004, "type": "private"}}}
    router.dispatch(db, {"update_id": 4, "callback_query": cq})
    assert answers == ["🧧 红包功能已关闭"]


# ---------- 限流 ----------

def test_user_rate_limit_triggers(db, sent):
    _make_user(db, "l1", 640001)
    router.dispatch(db, _msg(640001, 640001, "/points"))
    router.dispatch(db, _msg(640001, 640001, "/points"))
    assert len(sent) == 2
    assert "当前能量" in sent[0]
    assert sent[1] == "操作太快了，稍后再试"


def test_user_rate_limit_configurable(db):
    _set_cfg(db, "bot_rate_limit_seconds", "100")
    assert router._user_cmd_allow(640011, "/points", policy.rate_limit_seconds(policy.read_bot_config(db))) is True
    assert router._user_cmd_allow(640011, "/points", policy.rate_limit_seconds(policy.read_bot_config(db))) is False
    # 不同命令互不影响
    assert router._user_cmd_allow(640011, "/checkin", 100.0) is True


def test_user_rate_limit_window_expires():
    assert router._user_cmd_allow(640021, "/points", 0.05) is True
    assert router._user_cmd_allow(640021, "/points", 0.05) is False
    time.sleep(0.06)
    assert router._user_cmd_allow(640021, "/points", 0.05) is True


def test_group_rate_limit_drops_silently(db, sent):
    _set_cfg(db, "bot_group_rate_limit", "2")
    for i, tg_id in enumerate((650001, 650002, 650003)):
        _make_user(db, f"gl{i}", tg_id)
        router.dispatch(db, _msg(tg_id, -100650, "/points", "group"))
    # 前两条正常处理，第三条静默丢弃（无任何回复）
    assert len(sent) == 2
    assert all("当前能量" in s for s in sent)


def test_group_chatter_does_not_consume_budget(db, sent):
    _set_cfg(db, "bot_group_rate_limit", "2")
    _make_user(db, "gc1", 650011)
    for _ in range(25):
        router.dispatch(db, _msg(650011, -100651, "普通聊天内容", "group"))
    router.dispatch(db, _msg(650011, -100651, "/points", "group"))
    assert len(sent) == 1 and "当前能量" in sent[0]


def test_group_window_slides():
    assert router._group_allow(660001, 1) is True
    assert router._group_allow(660001, 1) is False
