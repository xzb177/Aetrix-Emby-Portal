"""TG Bot 能量老虎机·群里可玩（用户 2026-10-30 亲令推翻 #1525 仅私聊口径）。

群面板是公共面板：谁点注额按钮谁下注——扣点击者本人的能量、赢归
点击者、封顶按点击者计；面板打开者的余额一动不动。每注在群里发
🎰 骰子 + 一条公开结果消息（点注人/注额/结果/赢额/头奖池），结果与
骰子消息都按机器人消息自毁口径（bot_msg_autodelete_seconds）调度
删除，不编辑公共面板。私聊流程不变（回归在 test_tg_bot_slots.py）。
"""
from __future__ import annotations

import sys

import pytest

sys.path.insert(0, ".")

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend import models
from backend.tg_bot import callbacks, ephemeral, router, sender

GROUP_CHAT = -1007771234

TG_A = 920101  # 面板打开者
TG_B = 920102
TG_C = 920103
TG_UNBOUND = 920199


@pytest.fixture()
def db():
    engine = create_engine("sqlite:///:memory:")
    models.Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    session.add(models.ServerRealm(
        name="公益服", slug="main", access_mode="free"))
    session.commit()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


@pytest.fixture(autouse=True)
def _clear_rate_limits():
    # router 的用户级限流是进程级状态，逐测试清理防串扰
    router._user_cmd_limits.clear()
    yield
    router._user_cmd_limits.clear()


def _user(db, tg, name, points=1000):
    row = models.WebUser(
        username=name, password_hash="x", telegram_id=tg,
        points=points, vitality=14, is_staff=False,
    )
    db.add(row)
    db.commit()
    return row


def _set_config(db, key, value):
    row = db.query(models.SystemConfig).filter(
        models.SystemConfig.key == key).first()
    if row is None:
        db.add(models.SystemConfig(key=key, value=str(value)))
    else:
        row.value = str(value)
    db.commit()


def _logs(db, user_id, type_):
    return db.query(models.PointsLog).filter(
        models.PointsLog.user_id == user_id,
        models.PointsLog.type == type_).all()


def _balance(db, user_id):
    return db.query(models.WebUser.points).filter(
        models.WebUser.id == user_id).scalar()


def _spins(db):
    return db.query(models.SlotsSpin).all()


def _patch_bot(monkeypatch, dice_values=(7,)):
    """打桩 sender/ephemeral：记录骰子、公开消息、自毁调度与面板编辑。"""
    state = {
        "dice_calls": 0, "dice_mids": [], "answers": [], "panels": [],
        "messages": [], "deletes": [],
    }
    values = list(dice_values)

    monkeypatch.setattr(
        sender, "answer_callback_query",
        lambda db_, cq_id, text=None, show_alert=False:
            state["answers"].append((text, show_alert)) or (True, None))

    def fake_dice(db_, chat_id, emoji="🎰"):
        state["dice_calls"] += 1
        mid = 5000 + state["dice_calls"]
        state["dice_mids"].append(mid)
        value = values.pop(0) if values else 7
        return True, None, value, mid

    monkeypatch.setattr(sender, "send_dice", fake_dice)
    monkeypatch.setattr(
        sender, "send_panel",
        lambda db_, chat_id, text, markup=None, **kw:
            state["panels"].append((text, markup)) or (True, None))
    monkeypatch.setattr(
        sender, "edit_panel",
        lambda db_, chat_id, mid, text, markup=None, has_photo=False:
            state["panels"].append((text, markup)) or (True, None))

    def fake_send_ex(db_, chat_id, text, markup=None, **kw):
        state["messages"].append((chat_id, text))
        return True, None, 6000 + len(state["messages"])

    monkeypatch.setattr(sender, "send_message_ex", fake_send_ex)
    monkeypatch.setattr(
        ephemeral, "schedule_delete",
        lambda chat_id, message_id, delay:
            state["deletes"].append((chat_id, message_id, delay)) or True)
    return state


def _cq(data, cq_id, tg, first_name="Alice"):
    return {
        "id": cq_id,
        "from": {"id": tg, "first_name": first_name},
        "data": data,
        "message": {"message_id": 55,
                    "chat": {"id": GROUP_CHAT, "type": "supergroup"}},
    }


# ---------- 群面板 ----------

def test_group_panel_renders_public_panel(db, monkeypatch):
    _user(db, TG_A, "alice")
    state = _patch_bot(monkeypatch)
    callbacks.handle_callback(db, _cq("menu:slots", "cq-panel", TG_A))
    assert len(state["panels"]) == 1
    text, markup = state["panels"][0]
    assert "头奖池" in text
    assert "我的能量" not in text  # 公共面板不带个人数据
    flat = [b["callback_data"] for row in markup["inline_keyboard"] for b in row]
    assert "slots_bet:5" in flat and "slots_bet:10" in flat
    assert "slots_bet:20" in flat


# ---------- 群下注：扣点击者本人 ----------

def test_group_bets_charge_clickers_not_opener(db, monkeypatch):
    opener = _user(db, TG_A, "alice", points=1000)
    bob = _user(db, TG_B, "bob", points=100)
    carol = _user(db, TG_C, "carol", points=100)
    # 骰子序列：Bob 出 7（三轴各不同，不中）、Carol 出 61（恰好两个 7，返注）
    state = _patch_bot(monkeypatch, dice_values=(7, 61))
    callbacks.handle_callback(db, _cq("menu:slots", "cq-panel", TG_A, "Alice"))
    callbacks.handle_callback(db, _cq("slots_bet:10", "cq-b1", TG_B, "Bob"))
    callbacks.handle_callback(db, _cq("slots_bet:5", "cq-c1", TG_C, "Carol"))
    assert state["dice_calls"] == 2
    # 打开者一分没动；两人各扣各的、各赢各的
    assert _balance(db, opener.id) == 1000
    assert _balance(db, bob.id) == 90
    assert _balance(db, carol.id) == 100  # -5 注 +5 返注
    spins = {s.user_id: s for s in _spins(db)}
    assert spins[bob.id].bet == 10 and spins[bob.id].status == "settled"
    assert spins[carol.id].bet == 5 and spins[carol.id].status == "settled"
    # 两条公开结果，各带点注人
    texts = [t for _c, t in state["messages"]]
    assert len(texts) == 2
    assert any("Bob" in t for t in texts)
    assert any("Carol" in t for t in texts)


def test_group_same_callback_replay_single_spin(db, monkeypatch):
    bob = _user(db, TG_B, "bob", points=100)
    state = _patch_bot(monkeypatch, dice_values=(64,))
    cq = _cq("slots_bet:10", "cq-replay", TG_B, "Bob")
    callbacks.handle_callback(db, cq)
    router._user_cmd_limits.clear()  # 模拟限流窗口已过，单独验幂等本身
    callbacks.handle_callback(db, cq)
    assert state["dice_calls"] == 1
    assert len(_spins(db)) == 1
    assert len(_logs(db, bob.id, "slots_bet")) == 1
    assert _balance(db, bob.id) == 100 - 10 + 200 + 100
    # 重放不再刷第二条公开结果消息
    assert len(state["messages"]) == 1


def test_group_result_message_and_autodelete(db, monkeypatch):
    _user(db, TG_B, "bob", points=100)
    state = _patch_bot(monkeypatch, dice_values=(64,))
    callbacks.handle_callback(db, _cq("slots_bet:10", "cq-r1", TG_B, "Bob"))
    assert len(state["messages"]) == 1
    chat_id, text = state["messages"][0]
    assert chat_id == GROUP_CHAT
    assert "Bob" in text and "头奖池" in text and "300" in text
    # 骰子消息与结果消息都挂 30 秒自毁（bot_msg 默认口径，与抢红包反馈同）
    deleted = {(c, m) for c, m, _d in state["deletes"]}
    assert (GROUP_CHAT, state["dice_mids"][0]) in deleted
    assert (GROUP_CHAT, 6001) in deleted
    assert all(d == 30 for _c, _m, d in state["deletes"])


def test_group_jackpot_ledger_marked_group(db, monkeypatch):
    from backend import slots
    bob = _user(db, TG_B, "bob", points=100)
    state = _patch_bot(monkeypatch, dice_values=(64,))
    callbacks.handle_callback(db, _cq("slots_bet:10", "cq-j1", TG_B, "Bob"))
    assert state["dice_calls"] == 1
    assert _balance(db, bob.id) == 100 - 10 + 200 + 100
    bet_logs = _logs(db, bob.id, "slots_bet")
    win_logs = _logs(db, bob.id, "slots_jackpot")
    assert len(bet_logs) == 1 and "群聊" in (bet_logs[0].description or "")
    assert len(win_logs) == 1 and "群聊" in (win_logs[0].description or "")
    assert slots.pool_balance(db) == 100  # 抱池后重置回种子


# ---------- 群门禁 ----------

def test_group_unbound_clicker_guided_no_charge_no_dice(db, monkeypatch):
    state = _patch_bot(monkeypatch)
    callbacks.handle_callback(db, _cq("slots_bet:10", "cq-u1", TG_UNBOUND, "Eve"))
    assert state["dice_calls"] == 0
    assert _spins(db) == []
    assert any("绑定" in (t or "") and "私聊" in (t or "")
               for t, _a in state["answers"])


def test_group_anonymous_admin_blocked(db, monkeypatch):
    state = _patch_bot(monkeypatch)
    callbacks.handle_callback(
        db, _cq("slots_bet:10", "cq-anon", 1087968824, "Group"))
    assert state["dice_calls"] == 0
    assert _spins(db) == []
    assert any("匿名" in (t or "") for t, _a in state["answers"])


def test_group_switch_off_blocks_bet_and_panel(db, monkeypatch):
    bob = _user(db, TG_B, "bob", points=100)
    _set_config(db, "slots_group_enabled", "false")
    state = _patch_bot(monkeypatch)
    callbacks.handle_callback(db, _cq("slots_bet:10", "cq-off", TG_B, "Bob"))
    assert state["dice_calls"] == 0
    assert _balance(db, bob.id) == 100
    assert any("群里老虎机已关闭" in (t or "") for t, _a in state["answers"])
    callbacks.handle_callback(db, _cq("menu:slots", "cq-off-panel", TG_B, "Bob"))
    assert state["panels"] and "私聊" in state["panels"][-1][0]


def test_group_rate_limit_blocks_fast_second_bet(db, monkeypatch):
    bob = _user(db, TG_B, "bob", points=1000)
    state = _patch_bot(monkeypatch, dice_values=(7, 7))
    callbacks.handle_callback(db, _cq("slots_bet:5", "cq-f1", TG_B, "Bob"))
    callbacks.handle_callback(db, _cq("slots_bet:5", "cq-f2", TG_B, "Bob"))
    assert state["dice_calls"] == 1
    assert len(_spins(db)) == 1
    assert _balance(db, bob.id) == 1000 - 5
    assert any("操作太快了" in (t or "") for t, _a in state["answers"])


# ---------- 服务层口径 ----------

def test_private_ledger_descriptions_unmarked(db):
    """私聊（默认 origin）的台账描述保持原文、无群聊标记。"""
    from backend import slots
    user = _user(db, TG_B, "bob", points=100)
    begin = slots.begin_spin(db, user, 10, "spin-priv-desc")
    assert begin["ok"]
    slots.settle_spin(db, begin["spin"], 64)
    for type_ in ("slots_bet", "slots_win", "slots_jackpot"):
        logs = _logs(db, user.id, type_)
        assert logs, type_
        for log in logs:
            assert "群聊" not in (log.description or "")
