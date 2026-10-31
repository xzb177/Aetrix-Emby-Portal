"""TG Bot 群面板滑动自毁（PR-2，用户 2026-10-30 亲点方案第二单）。

口径：群聊里的面板类消息（menu:* 面板、老虎机公共面板）在每次成功
互动（回调编辑落到该消息上）后，把自毁重挂为「最后互动＋
bot_msg_autodelete_seconds」，正点着不会准点消失；无人互动则到点删。
不许动的既有口径逐条锁：红包本体永不自毁（test_tg_bot_ephemeral 已
锁，本文件锁其与滑动逻辑互不干扰）、骰子/结果固定自毁、非面板群回复
发送时固定自毁、用户命令消息 3 秒自毁、私聊不自毁。
"""
from __future__ import annotations

import sys

import pytest

sys.path.insert(0, ".")

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend import models
from backend.integrations import store
from backend.tg_bot import callbacks, ephemeral, sender

GROUP_CHAT = -1007771234
PANEL_MID = 55
TG_USER = 920101


class FakeTimer:
    """ephemeral.Timer 桩：记录实例，cancel/fire 由测试手动驱动。"""

    instances: list = []

    def __init__(self, interval, function, args=None, kwargs=None):
        self.interval = interval
        self.function = function
        self.args = args or ()
        self.kwargs = kwargs or {}
        self.daemon = False
        self.started = False
        self.cancelled = False
        FakeTimer.instances.append(self)

    def start(self):
        self.started = True

    def cancel(self):
        self.cancelled = True

    def fire(self):
        self.function(*self.args, **self.kwargs)


@pytest.fixture()
def rig(monkeypatch):
    """FakeTimer＋删除记录＋登记表清理，返回删除记录列表。"""
    FakeTimer.instances = []
    deleted = []
    monkeypatch.setattr(ephemeral, "Timer", FakeTimer)
    monkeypatch.setattr(
        ephemeral, "_delete_now",
        lambda c, m, delete_message=None: deleted.append((c, m)))
    ephemeral._timers.clear()
    yield deleted
    for timer in list(ephemeral._timers.values()):
        try:
            timer.cancel()
        except Exception:
            pass
    ephemeral._timers.clear()
    FakeTimer.instances = []


@pytest.fixture()
def db():
    engine = create_engine("sqlite:///:memory:")
    models.Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    session.add(models.ServerRealm(
        name="公益服", slug="main", access_mode="free"))
    session.commit()
    store.invalidate()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()
        store.invalidate()


def _set_config(db, key, value):
    row = db.query(models.SystemConfig).filter(
        models.SystemConfig.key == key).first()
    if row is None:
        db.add(models.SystemConfig(key=key, value=str(value)))
    else:
        row.value = str(value)
    db.commit()
    store.invalidate(key)


def _timers_for(chat_id, message_id):
    return ephemeral._timers.get((chat_id, message_id))


# ---------- ephemeral 层：登记表与重挂原语 ----------

def test_schedule_registers_and_fire_cleans_registry(rig):
    assert ephemeral.schedule_delete(GROUP_CHAT, PANEL_MID, 30) is True
    timer = _timers_for(GROUP_CHAT, PANEL_MID)
    assert timer is not None and timer.started
    timer.fire()
    assert rig == [(GROUP_CHAT, PANEL_MID)]  # 到点真删
    assert _timers_for(GROUP_CHAT, PANEL_MID) is None  # 登记不留死条目


def test_no_interaction_deletes_on_time(rig):
    ephemeral.schedule_delete(GROUP_CHAT, PANEL_MID, 30)
    timer = _timers_for(GROUP_CHAT, PANEL_MID)
    timer.fire()
    assert rig == [(GROUP_CHAT, PANEL_MID)]


def test_rearm_slides_deadline(db, rig):
    ephemeral.schedule_delete(GROUP_CHAT, PANEL_MID, 30)
    first = _timers_for(GROUP_CHAT, PANEL_MID)
    assert ephemeral.rearm_delete(db, GROUP_CHAT, PANEL_MID) is True
    assert first.cancelled is True  # 旧定时器被取消，不再准点删
    second = _timers_for(GROUP_CHAT, PANEL_MID)
    assert second is not None and second is not first
    assert second.interval == 30 and second.cancelled is False
    second.fire()
    assert rig == [(GROUP_CHAT, PANEL_MID)]


def test_rearm_without_prior_timer_starts_sliding(db, rig):
    assert ephemeral.rearm_delete(db, GROUP_CHAT, PANEL_MID) is True
    timer = _timers_for(GROUP_CHAT, PANEL_MID)
    assert timer is not None and timer.interval == 30


def test_rearm_uses_current_config(db, rig):
    _set_config(db, "bot_msg_autodelete_seconds", "45")
    assert ephemeral.rearm_delete(db, GROUP_CHAT, PANEL_MID) is True
    assert _timers_for(GROUP_CHAT, PANEL_MID).interval == 45


def test_rearm_config_zero_cancels_and_stays(db, rig):
    ephemeral.schedule_delete(GROUP_CHAT, PANEL_MID, 30)
    first = _timers_for(GROUP_CHAT, PANEL_MID)
    _set_config(db, "bot_msg_autodelete_seconds", "0")
    # 0=关：只取消不重挂（常驻），与发送口「延时 0 不调度」同语义
    assert ephemeral.rearm_delete(db, GROUP_CHAT, PANEL_MID) is False
    assert first.cancelled is True
    assert _timers_for(GROUP_CHAT, PANEL_MID) is None
    assert len(FakeTimer.instances) == 1  # 没起新定时器


def test_cancel_and_rearm_missing_ids_silent(db, rig):
    assert ephemeral.cancel_delete(GROUP_CHAT, 999999) is False
    assert ephemeral.cancel_delete(0, PANEL_MID) is False
    assert ephemeral.rearm_delete(db, 0, PANEL_MID) is False
    assert ephemeral.rearm_delete(db, GROUP_CHAT, 0) is False
    assert rig == []


def test_schedule_same_key_supersedes(db, rig):
    ephemeral.schedule_delete(GROUP_CHAT, PANEL_MID, 30)
    first = _timers_for(GROUP_CHAT, PANEL_MID)
    ephemeral.schedule_delete(GROUP_CHAT, PANEL_MID, 30)
    second = _timers_for(GROUP_CHAT, PANEL_MID)
    assert first.cancelled is True and second is not first


# ---------- 不许动的口径：与滑动逻辑互不干扰 ----------

def test_cmd_autodelete_entry_untouched_by_panel_rearm(db, rig):
    # 用户命令消息（3 秒口径）与面板消息各占各的登记键，滑动面板不误伤
    ephemeral.schedule_delete(GROUP_CHAT, 999, ephemeral.bot_cmd_delay_seconds(db))
    cmd_timer = _timers_for(GROUP_CHAT, 999)
    assert cmd_timer.interval == 3
    ephemeral.rearm_delete(db, GROUP_CHAT, PANEL_MID)
    assert cmd_timer.cancelled is False
    assert _timers_for(GROUP_CHAT, 999) is cmd_timer


def test_dice_and_result_fixed_schedule_not_slid(db, rig):
    # 骰子/结果消息（_send_slots_group_result 的固定调度）不被面板滑动重挂
    ephemeral.schedule_delete(GROUP_CHAT, 5001, 30)  # 骰子
    ephemeral.schedule_delete(GROUP_CHAT, 6001, 30)  # 结果
    dice_timer = _timers_for(GROUP_CHAT, 5001)
    result_timer = _timers_for(GROUP_CHAT, 6001)
    ephemeral.rearm_delete(db, GROUP_CHAT, PANEL_MID)  # 面板互动
    assert dice_timer.cancelled is False and result_timer.cancelled is False
    assert _timers_for(GROUP_CHAT, 5001) is dice_timer
    assert _timers_for(GROUP_CHAT, 6001) is result_timer


# ---------- callbacks 层：群面板互动接滑动自毁 ----------

def _user(db, tg=TG_USER, name="alice", points=1000):
    row = models.WebUser(
        username=name, password_hash="x", telegram_id=tg,
        points=points, vitality=14, is_staff=False)
    db.add(row)
    db.commit()
    return row


def _patch_menu_sender(monkeypatch, edit_ok=True):
    state = {"edits": [], "deletes": [], "panels": []}
    monkeypatch.setattr(
        sender, "answer_callback_query",
        lambda db_, cq_id, text=None, show_alert=False: (True, None))
    monkeypatch.setattr(
        sender, "edit_panel",
        lambda db_, chat_id, mid, text, markup=None, has_photo=False:
            state["edits"].append((chat_id, mid)) or
            ((True, None) if edit_ok else (False, "message can't be edited")))
    monkeypatch.setattr(
        sender, "delete_message",
        lambda db_, chat_id, mid:
            state["deletes"].append((chat_id, mid)) or (True, None))
    monkeypatch.setattr(
        sender, "send_panel",
        lambda db_, chat_id, text, markup=None, **kw:
            state["panels"].append((chat_id, text)) or (True, None))
    return state


def _cq(data, cq_id, tg=TG_USER, chat_id=GROUP_CHAT, mid=PANEL_MID):
    chat = {"id": chat_id}
    if chat_id < 0:
        chat["type"] = "supergroup"
    return {
        "id": cq_id,
        "from": {"id": tg, "first_name": "Alice"},
        "data": data,
        "message": {"message_id": mid, "chat": chat},
    }


def test_group_menu_interaction_rearms_panel(db, rig, monkeypatch):
    _user(db)
    state = _patch_menu_sender(monkeypatch)
    # 面板发出时已按发送口挂了固定自毁（模拟 _send_reply 的调度）
    ephemeral.schedule_delete(GROUP_CHAT, PANEL_MID, 30)
    first = _timers_for(GROUP_CHAT, PANEL_MID)
    callbacks.handle_callback(db, _cq("menu:main", "cq-slide-1"))
    assert state["edits"] == [(GROUP_CHAT, PANEL_MID)]
    assert first.cancelled is True  # 互动后续命：旧的准点删除取消
    second = _timers_for(GROUP_CHAT, PANEL_MID)
    assert second is not None and second is not first
    assert second.interval == 30
    second.fire()
    assert rig == [(GROUP_CHAT, PANEL_MID)]


def test_group_slots_panel_interaction_rearms(db, rig, monkeypatch):
    _user(db)
    state = _patch_menu_sender(monkeypatch)
    ephemeral.schedule_delete(GROUP_CHAT, PANEL_MID, 30)
    first = _timers_for(GROUP_CHAT, PANEL_MID)
    callbacks.handle_callback(db, _cq("menu:slots", "cq-slide-2"))
    assert state["edits"] == [(GROUP_CHAT, PANEL_MID)]
    assert first.cancelled is True
    assert _timers_for(GROUP_CHAT, PANEL_MID) is not first


def test_private_menu_interaction_no_rearm(db, rig, monkeypatch):
    _user(db)
    state = _patch_menu_sender(monkeypatch)
    callbacks.handle_callback(
        db, _cq("menu:main", "cq-slide-3", chat_id=TG_USER))
    assert state["edits"] == [(TG_USER, PANEL_MID)]
    assert ephemeral._timers == {}  # 私聊不自毁：不起、不挂
    assert FakeTimer.instances == []


def test_group_menu_edit_failure_no_rearm(db, rig, monkeypatch):
    _user(db)
    state = _patch_menu_sender(monkeypatch, edit_ok=False)
    ephemeral.schedule_delete(GROUP_CHAT, PANEL_MID, 30)
    first = _timers_for(GROUP_CHAT, PANEL_MID)
    callbacks.handle_callback(db, _cq("menu:main", "cq-slide-4"))
    # 编辑失败走删旧发新回落：旧消息的重挂静默放弃，不取消不新建
    assert state["deletes"] == [(GROUP_CHAT, PANEL_MID)]
    assert len(state["panels"]) == 1
    assert first.cancelled is False
    assert _timers_for(GROUP_CHAT, PANEL_MID) is first


def test_group_menu_interaction_config_zero_no_schedule(db, rig, monkeypatch):
    _user(db)
    _set_config(db, "bot_msg_autodelete_seconds", "0")
    state = _patch_menu_sender(monkeypatch)
    callbacks.handle_callback(db, _cq("menu:main", "cq-slide-5"))
    assert state["edits"] == [(GROUP_CHAT, PANEL_MID)]
    assert ephemeral._timers == {}  # 配置关：互动也不挂
