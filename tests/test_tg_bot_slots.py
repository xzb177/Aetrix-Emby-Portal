"""TG Bot 能量老虎机（用户 2026-10-30 亲批设计）。

结算只认 Telegram 原生 sendDice 返回的 dice.value（1-64）：
v=value-1，三轴 v//16、(v//4)%4、v%4，符号序 [BAR, 葡萄, 柠檬, 7]。
赔付：777×20 / BAR 三连×10 / 葡萄·柠檬三连×5 / 恰好两个 7 返注，
默认注额 5/10/20、每注抽 5% 进头奖池、777 抱走整池后重置种子 100、
每日下注封顶 200 / 每日赢分（基础赔付）封顶 1000。
"""
from __future__ import annotations

import sys
from datetime import datetime, timedelta

import pytest

sys.path.insert(0, ".")

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend import models
from backend.tg_bot import callbacks, ephemeral, handlers, sender

TG = 920001


@pytest.fixture()
def db():
    engine = create_engine("sqlite:///:memory:")
    models.Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    # 公益服（free realm）：老虎机的公益口径前置
    session.add(models.ServerRealm(
        name="公益服", slug="main", access_mode="free"))
    session.commit()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


class _FakeTimer:
    """假 Timer：只登记、永不真挂线程（堵群面板自毁真 Timer 泄漏，
    同 test_tg_bot_account_display 的修法：群回调编辑成功会重挂真实
    30 秒 Timer，到期删除会落进后续测试的 sender 打补丁窗口）。"""

    def __init__(self, interval, function, args=None, kwargs=None):
        self.interval = interval
        self.function = function
        self.args = args or ()
        self.kwargs = kwargs or {}
        self.daemon = False
        self.started = False
        self.cancelled = False

    def start(self):
        self.started = True

    def cancel(self):
        self.cancelled = True


@pytest.fixture(autouse=True)
def _no_real_timers(monkeypatch):
    monkeypatch.setattr(ephemeral, "Timer", _FakeTimer)


def _user(db, tg=TG, name="alice", points=1000, created_at=None):
    row = models.WebUser(
        username=name, password_hash="x", telegram_id=tg,
        points=points, vitality=14, is_staff=False,
    )
    if created_at is not None:
        row.created_at = created_at
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


# ---------- 解码与赔付（纯函数） ----------

def test_decode_mapping_all_64():
    from backend import slots
    symbols = ("bar", "grape", "lemon", "seven")
    for value in range(1, 65):
        v = value - 1
        expect = (symbols[v // 16], symbols[(v // 4) % 4], symbols[v % 4])
        assert slots.decode_dice(value) == expect


def test_decode_counts():
    from backend import slots
    triples = [v for v in range(1, 65)
               if len(set(slots.decode_dice(v))) == 1]
    assert len(triples) == 4  # 四种三连各一个值
    assert slots.decode_dice(64) == ("seven", "seven", "seven")
    assert slots.decode_dice(1) == ("bar", "bar", "bar")
    two_seven = [v for v in range(1, 65)
                 if slots.decode_dice(v).count("seven") == 2]
    assert len(two_seven) == 9  # 空位 3 种 × 非 7 符号 3 种


def test_default_payout_table_and_ev():
    from backend import slots
    pays = slots.default_pays()
    assert pays == {"seven": 20, "bar": 10, "fruit": 5, "two_seven": 1}
    ev = slots.expected_return(pays)
    # 全 64 值精确期望：(20+10+5+5+9×1)/64 = 49/64
    assert ev == pytest.approx(49 / 64)
    assert 0.70 <= ev <= 0.85


def test_payout_for_values():
    from backend import slots
    pays = slots.default_pays()
    assert slots.payout_for(slots.decode_dice(64), 10, pays) == (200, True)
    assert slots.payout_for(slots.decode_dice(1), 10, pays) == (100, False)
    # 葡萄三连 value=22、柠檬三连 value=43
    assert slots.payout_for(slots.decode_dice(22), 10, pays) == (50, False)
    assert slots.payout_for(slots.decode_dice(43), 10, pays) == (50, False)
    # 恰好两个 7（value=61）返注
    assert slots.payout_for(slots.decode_dice(61), 10, pays) == (10, False)
    # 三个各不同（value=7）不赔
    assert slots.payout_for(slots.decode_dice(7), 10, pays) == (0, False)


# ---------- 池与结算（服务层） ----------

def test_jackpot_flow_pool_and_ledger(db):
    from backend import slots
    user = _user(db, points=100)
    begin = slots.begin_spin(db, user, 10, "spin-jp-1")
    assert begin["ok"] and not begin["duplicate"]
    assert _balance(db, user.id) == 90
    result = slots.settle_spin(db, begin["spin"], 64)
    assert result["ok"]
    assert result["base_payout"] == 200
    assert result["pool_payout"] == 100  # 种子池 100（本注抽水 0.5 未满 1 不入整池）
    assert _balance(db, user.id) == 100 - 10 + 200 + 100
    # 池重置为种子额
    assert slots.pool_balance(db) == 100
    # 台账三类打标各一条、ref 带 spin 标识
    assert len(_logs(db, user.id, "slots_bet")) == 1
    assert len(_logs(db, user.id, "slots_win")) == 1
    assert len(_logs(db, user.id, "slots_jackpot")) == 1
    assert _logs(db, user.id, "slots_bet")[0].amount == -10


def test_rake_accrues_to_pool(db):
    from backend import slots
    user = _user(db, points=100)
    begin = slots.begin_spin(db, user, 20, "spin-rake-1")
    assert begin["ok"]
    result = slots.settle_spin(db, begin["spin"], 7)  # 不中奖
    assert result["base_payout"] == 0 and result["pool_payout"] == 0
    # 20 × 5% = 1 整个能量进池：100 → 101
    assert slots.pool_balance(db) == 101
    assert _balance(db, user.id) == 80


def test_two_seven_returns_bet(db):
    from backend import slots
    user = _user(db, points=50)
    begin = slots.begin_spin(db, user, 10, "spin-two7")
    result = slots.settle_spin(db, begin["spin"], 61)
    assert result["base_payout"] == 10
    assert _balance(db, user.id) == 50


def test_daily_bet_cap(db):
    from backend import slots
    user = _user(db, points=100000)
    for i in range(10):  # 10 × 20 = 200 达到默认日下注封顶
        begin = slots.begin_spin(db, user, 20, f"spin-cap-{i}")
        assert begin["ok"]
        slots.settle_spin(db, begin["spin"], 7)
    before = _balance(db, user.id)
    blocked = slots.begin_spin(db, user, 5, "spin-cap-over")
    assert not blocked["ok"] and blocked["reason"] == "daily_bet_cap"
    assert _balance(db, user.id) == before  # 没扣款


def test_daily_win_cap_excludes_pool(db):
    from backend import slots
    _set_config(db, "slots_daily_win_cap", 150)
    user = _user(db, points=100)
    begin = slots.begin_spin(db, user, 10, "spin-wincap")
    result = slots.settle_spin(db, begin["spin"], 64)
    assert result["base_payout"] == 150  # 基础赔付 200 被封顶到 150
    assert result["pool_payout"] == 100  # 头奖池部分不计封顶、照付
    assert _balance(db, user.id) == 100 - 10 + 150 + 100


def test_begin_idempotent_same_key(db):
    from backend import slots
    user = _user(db, points=100)
    first = slots.begin_spin(db, user, 10, "spin-dup")
    second = slots.begin_spin(db, user, 10, "spin-dup")
    assert first["ok"] and second["ok"] and second["duplicate"]
    assert _balance(db, user.id) == 90  # 只扣一次
    assert len(_logs(db, user.id, "slots_bet")) == 1
    slots.settle_spin(db, first["spin"], 64)
    again = slots.settle_spin(db, first["spin"], 64)
    assert again["duplicate"]
    assert len(_logs(db, user.id, "slots_win")) == 1  # 只赔一次


def test_insufficient_balance_no_charge(db):
    from backend import slots
    user = _user(db, points=3)
    res = slots.begin_spin(db, user, 5, "spin-poor")
    assert not res["ok"] and res["reason"] == "insufficient"
    assert _balance(db, user.id) == 3
    assert _logs(db, user.id, "slots_bet") == []
    # 不留 pending 阻塞后续
    user.points = 100
    db.commit()
    assert slots.begin_spin(db, user, 5, "spin-poor-2")["ok"]


def test_in_flight_blocks_second_spin(db):
    from backend import slots
    user = _user(db, points=100)
    assert slots.begin_spin(db, user, 10, "spin-fly-1")["ok"]
    blocked = slots.begin_spin(db, user, 10, "spin-fly-2")
    assert not blocked["ok"] and blocked["reason"] == "in_flight"
    assert _balance(db, user.id) == 90


def test_stale_pending_refunded(db):
    from backend import slots
    user = _user(db, points=100)
    first = slots.begin_spin(db, user, 10, "spin-stale")
    spin = first["spin"]
    spin.created_at = datetime.now() - timedelta(seconds=3600)
    db.commit()
    nxt = slots.begin_spin(db, user, 10, "spin-after-stale")
    assert nxt["ok"]
    db.refresh(spin)
    assert spin.status == "failed"
    assert len(_logs(db, user.id, "slots_refund")) == 1
    assert _balance(db, user.id) == 90  # 旧注退回、新注扣掉


def test_fail_spin_refunds(db):
    from backend import slots
    user = _user(db, points=100)
    begin = slots.begin_spin(db, user, 20, "spin-fail")
    slots.fail_spin(db, begin["spin"], "dice_failed")
    assert _balance(db, user.id) == 100
    assert len(_logs(db, user.id, "slots_refund")) == 1
    # 重复 fail 不重复退
    slots.fail_spin(db, begin["spin"], "dice_failed")
    assert len(_logs(db, user.id, "slots_refund")) == 1
    assert slots.pool_balance(db) == 100  # 抽水也已回滚


# ---------- 资格镜像（抽奖口径） ----------

def test_disabled_rejected(db):
    from backend import slots
    _set_config(db, "slots_enabled", "false")
    user = _user(db)
    res = slots.begin_spin(db, user, 5, "spin-off")
    assert not res["ok"] and res["reason"] == "disabled"


def test_blacklist_mirror(db):
    from backend import slots
    user = _user(db)
    db.add(models.LotteryBlacklist(user_id=user.id, reason="test"))
    db.commit()
    res = slots.begin_spin(db, user, 5, "spin-bl")
    assert not res["ok"] and res["reason"] == "blacklisted"


def test_too_new_mirror(db):
    from backend import slots
    _set_config(db, "lottery_min_account_age_days", 30)
    user = _user(db, created_at=datetime.now())
    res = slots.begin_spin(db, user, 5, "spin-new")
    assert not res["ok"] and res["reason"] == "too_new"
    old = _user(db, tg=TG + 1, name="bob",
                created_at=datetime.now() - timedelta(days=60))
    assert slots.begin_spin(db, old, 5, "spin-old")["ok"]


def test_not_free_realm_rejected(db):
    from backend import slots
    realm = db.query(models.ServerRealm).first()
    realm.access_mode = "paid"
    db.commit()
    user = _user(db)
    res = slots.begin_spin(db, user, 5, "spin-paid")
    assert not res["ok"] and res["reason"] == "not_free_realm"


# ---------- sender.send_dice ----------

def test_send_dice_parses_value(monkeypatch):
    captured = {}

    def fake_call_ex(db_, method, payload):
        captured["method"] = method
        captured["payload"] = payload
        return True, None, {"message_id": 9, "dice": {"emoji": "🎰", "value": 42}}

    monkeypatch.setattr(sender, "_call_ex", fake_call_ex)
    ok, err, value, message_id = sender.send_dice(None, 123)
    assert ok and err is None
    assert value == 42 and message_id == 9
    assert captured["method"] == "sendDice"
    assert captured["payload"]["emoji"] == "🎰"
    assert captured["payload"]["chat_id"] == 123


def test_send_dice_failure(monkeypatch):
    monkeypatch.setattr(
        sender, "_call_ex", lambda db_, m, p: (False, "boom", None))
    ok, err, value, message_id = sender.send_dice(None, 123)
    assert not ok and value is None and message_id is None


# ---------- 面板与回调流 ----------

def _patch_bot(monkeypatch, dice_value=64):
    state = {"dice_calls": 0, "panels": [], "answers": []}

    monkeypatch.setattr(
        sender, "answer_callback_query",
        lambda db_, cq_id, text=None, show_alert=False:
            state["answers"].append((text, show_alert)) or (True, None))

    def fake_dice(db_, chat_id, emoji="🎰"):
        state["dice_calls"] += 1
        return True, None, dice_value, 777

    monkeypatch.setattr(sender, "send_dice", fake_dice)
    monkeypatch.setattr(
        sender, "send_panel",
        lambda db_, chat_id, text, markup=None, **kw:
            state["panels"].append((text, markup)) or (True, None))
    monkeypatch.setattr(
        sender, "edit_panel",
        lambda db_, chat_id, mid, text, markup=None, has_photo=False:
            state["panels"].append((text, markup)) or (True, None))
    return state


def _cq(data, cq_id="cq-slots-1", tg=TG, chat=None):
    return {
        "id": cq_id,
        "from": {"id": tg, "first_name": "Alice"},
        "data": data,
        "message": {"message_id": 55, "chat": chat or {"id": tg}},
    }


def test_welfare_group_has_slots_button():
    markup = handlers.user_panel_group_markup("welfare")
    flat = [b for row in markup["inline_keyboard"] for b in row]
    assert {"text": "🎰 老虎机", "callback_data": "menu:slots"} in flat


def test_slots_panel_private(db, monkeypatch):
    _user(db)
    state = _patch_bot(monkeypatch)
    callbacks.handle_callback(db, _cq("menu:slots"))
    assert len(state["panels"]) == 1
    text, markup = state["panels"][0]
    assert "老虎机" in text and "头奖池" in text
    flat = [b["callback_data"] for row in markup["inline_keyboard"] for b in row]
    assert "slots_bet:5" in flat and "slots_bet:10" in flat
    assert "slots_bet:20" in flat


def test_slots_panel_group_chat_blocked(db, monkeypatch):
    # 群开关关闭时，群里点入口仍只回私聊提示（群里可玩为默认开，
    # 见 test_tg_bot_slots_group.py 的公共面板用例）
    _user(db)
    _set_config(db, "slots_group_enabled", "false")
    state = _patch_bot(monkeypatch)
    callbacks.handle_callback(db, _cq("menu:slots", chat={"id": -100999}))
    assert len(state["panels"]) == 1
    assert "私聊" in state["panels"][0][0]


def test_slots_panel_unbound(db, monkeypatch):
    state = _patch_bot(monkeypatch)
    callbacks.handle_callback(db, _cq("menu:slots", tg=TG + 55))
    assert len(state["panels"]) == 1
    assert "绑定" in state["panels"][0][0]


def test_bet_flow_end_to_end_and_idempotent(db, monkeypatch):
    user = _user(db, points=100)
    state = _patch_bot(monkeypatch, dice_value=64)
    cq = _cq("slots_bet:10")
    callbacks.handle_callback(db, cq)
    assert state["dice_calls"] == 1
    assert _balance(db, user.id) == 100 - 10 + 200 + 100
    result_texts = [t for t, _m in state["panels"]]
    assert any("777" in t or "7️⃣" in t for t in result_texts)
    assert any("头奖池" in t for t in result_texts)
    # 同一 callback 重放：不再摇、不再扣、不再赔
    callbacks.handle_callback(db, cq)
    assert state["dice_calls"] == 1
    assert _balance(db, user.id) == 390
    assert len(_logs(db, user.id, "slots_bet")) == 1


def test_bet_flow_group_chat_plays(db, monkeypatch):
    """群里可玩（2026-10-30 用户亲令推翻仅私聊口径）：群里点注额按钮
    照常开摇，扣点击者本人；细账（多人各扣各的/自毁/门禁）在
    test_tg_bot_slots_group.py 里逐项锁。"""
    user = _user(db, points=100)
    state = _patch_bot(monkeypatch, dice_value=64)
    monkeypatch.setattr(
        sender, "send_message_ex",
        lambda db_, chat_id, text, markup=None, **kw: (True, None, 6001))
    monkeypatch.setattr(
        "backend.tg_bot.ephemeral.schedule_delete",
        lambda chat_id, message_id, delay: True)
    callbacks.handle_callback(db, _cq("slots_bet:10", chat={"id": -100999}))
    assert state["dice_calls"] == 1
    assert _balance(db, user.id) == 100 - 10 + 200 + 100
    assert len(_logs(db, user.id, "slots_bet")) == 1


def test_bet_flow_insufficient_toast(db, monkeypatch):
    user = _user(db, points=3)
    state = _patch_bot(monkeypatch)
    callbacks.handle_callback(db, _cq("slots_bet:5"))
    assert state["dice_calls"] == 0
    assert _balance(db, user.id) == 3
    assert any("能量不足" in (t or "") for t, _a in state["answers"])


def test_bet_flow_dice_failure_refunds(db, monkeypatch):
    user = _user(db, points=100)
    state = _patch_bot(monkeypatch)
    monkeypatch.setattr(
        sender, "send_dice",
        lambda db_, chat_id, emoji="🎰": (False, "boom", None, None))
    callbacks.handle_callback(db, _cq("slots_bet:10"))
    assert _balance(db, user.id) == 100  # 已全额退回
    assert len(_logs(db, user.id, "slots_refund")) == 1
