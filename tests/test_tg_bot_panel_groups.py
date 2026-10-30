"""TG Bot 用户面板按钮分组（2026-10-30 用户亲批「按钮分一下，不要全铺开」）。

一级只放：签到直达＋五个分组入口＋整行主界面；原 15 颗功能按钮收进
二级分组面板（同一条消息编辑切换），各功能 callback 与私聊门禁不变，
只新增分组翻页 callback（menu:group_* 与返回 menu:user）。
"""
from __future__ import annotations

import sys

import pytest

sys.path.insert(0, ".")

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend import models
from backend.tg_bot import callbacks, handlers, layout, sender

TG = 910001

LEVEL1 = [
    [
        {"text": "✅ 签到", "callback_data": "menu:checkin"},
        {"text": "🎬 求片", "callback_data": "menu:group_media"},
    ],
    [
        {"text": "📦 网盘转存", "callback_data": "menu:group_drive"},
        {"text": "🎟️ 账号权益", "callback_data": "menu:group_account"},
    ],
    [
        {"text": "🎁 福利活动", "callback_data": "menu:group_welfare"},
        {"text": "❓ 帮助中心", "callback_data": "menu:group_help"},
    ],
    [{"text": "♻️ 主界面", "callback_data": "menu:main"}],
]

BACK_ROW = [{"text": "↩ 返回用户面板", "callback_data": "menu:user"}]

GROUPS = {
    "media": ("🎬 求片", [
        [
            {"text": "🎬 求片", "callback_data": "menu:request"},
            {"text": "📋 我的求片", "callback_data": "menu:myrequests"},
        ],
        [{"text": "⭐ 我的想看", "callback_data": "menu:wants"}],
    ]),
    "drive": ("📦 网盘转存", [
        [
            {"text": "🔗 绑定网盘", "callback_data": "menu:bindings"},
            {"text": "📦 我的转存", "callback_data": "menu:transfers"},
        ],
    ]),
    "account": ("🎟️ 账号权益", [
        [
            {"text": "✨ 能量", "callback_data": "menu:points"},
            {"text": "🏆 能量榜", "callback_data": "menu:rank"},
        ],
        [
            {"text": "🎟️ 续期", "callback_data": "menu:login"},
            {"text": "🔗 绑定账号", "callback_data": "menu:bind"},
        ],
    ]),
    "welfare": ("🎁 福利活动", [
        [
            {"text": "🎲 抽奖", "callback_data": "menu:lottery"},
            {"text": "🧧 发红包", "callback_data": "menu:redpacket"},
        ],
        [{"text": "🎰 老虎机", "callback_data": "menu:slots"}],
    ]),
    "help": ("❓ 帮助中心", [
        [
            {"text": "❓ 为什么播不了", "callback_data": "menu:whyplay"},
            {"text": "❓ 帮助", "callback_data": "menu:help"},
        ],
    ]),
}

# 原面板平铺的 15 颗按钮的 callback（签到与主界面在一级，其余收进分组）
ORIGINAL_15 = {
    "menu:checkin", "menu:request", "menu:myrequests", "menu:transfers",
    "menu:bindings", "menu:login", "menu:points", "menu:rank",
    "menu:lottery", "menu:redpacket", "menu:whyplay", "menu:wants",
    "menu:bind", "menu:help", "menu:main",
}
FUNCTION_CALLBACKS = ORIGINAL_15 - {"menu:checkin", "menu:main"}


@pytest.fixture()
def db():
    engine = create_engine("sqlite:///:memory:")
    models.Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


def _user(db, tg=TG, name="alice"):
    row = models.WebUser(
        username=name, password_hash="x", telegram_id=tg,
        points=42, vitality=14, is_staff=False,
    )
    db.add(row)
    db.commit()
    return row


def _flat(markup):
    return [b["callback_data"] for r in markup["inline_keyboard"] for b in r
            if "callback_data" in b]


# ---------- 一级结构 ----------

def test_level1_matches_approved_sample():
    assert handlers.user_panel_markup()["inline_keyboard"] == LEVEL1


def test_level1_no_flat_function_buttons():
    flat = set(_flat(handlers.user_panel_markup()))
    assert flat == {
        "menu:checkin", "menu:group_media", "menu:group_drive",
        "menu:group_account", "menu:group_welfare", "menu:group_help",
        "menu:main",
    }
    assert not (flat & FUNCTION_CALLBACKS)


def test_all_original_buttons_reachable_within_two_clicks():
    level1 = handlers.user_panel_markup()
    reachable = set(_flat(level1))
    for key in GROUPS:
        reachable |= set(_flat(handlers.user_panel_group_markup(key)))
    assert ORIGINAL_15 <= reachable


# ---------- 二级分组面板（handlers 层） ----------

@pytest.mark.parametrize("key", sorted(GROUPS))
def test_group_markup_and_title(key):
    label, rows = GROUPS[key]
    assert handlers.user_panel_group_markup(key)["inline_keyboard"] == rows + [BACK_ROW]
    text = handlers.user_panel_group_text(key)
    assert text == f"▎<b>{label}</b>"
    assert "用户编号" not in text  # 不重复信息行
    assert layout.caption_fits(text)


# ---------- callbacks 层：编辑同一条消息切换 ----------

def _patch_panel(monkeypatch):
    state = {"edits": []}
    monkeypatch.setattr(
        sender, "answer_callback_query",
        lambda db_, cq_id, text=None, show_alert=False: (True, None))
    monkeypatch.setattr(
        sender, "edit_panel",
        lambda db_, chat_id, mid, text, markup=None, has_photo=False:
            state["edits"].append((text, markup)) or (True, None))
    return state


def _cq(action, tg=TG, chat=None):
    return {
        "id": "cq-grp-1",
        "from": {"id": tg, "first_name": "Alice"},
        "data": f"menu:{action}",
        "message": {"message_id": 55, "chat": chat or {"id": tg}},
    }


@pytest.mark.parametrize("key", sorted(GROUPS))
def test_group_callback_renders_group_panel(db, monkeypatch, key):
    _user(db)
    state = _patch_panel(monkeypatch)
    callbacks.handle_callback(db, _cq(f"group_{key}"))
    label, rows = GROUPS[key]
    assert len(state["edits"]) == 1
    text, markup = state["edits"][0]
    assert text == f"▎<b>{label}</b>"
    assert markup["inline_keyboard"] == rows + [BACK_ROW]


def test_group_callback_needs_no_identity(db, monkeypatch):
    # 未绑定用户点分组按钮也只翻页：不回绑定指引、不查身份
    state = _patch_panel(monkeypatch)
    callbacks.handle_callback(db, _cq("group_drive", tg=TG + 99))
    assert len(state["edits"]) == 1
    text, _markup = state["edits"][0]
    assert "网盘转存" in text and "如何绑定" not in text


def test_group_callback_renders_in_group_chat(db, monkeypatch):
    # 分组翻页不设门禁：群里点分组也渲染分组面板本体
    _user(db)
    state = _patch_panel(monkeypatch)
    callbacks.handle_callback(db, _cq("group_media", chat={"id": -100999}))
    assert len(state["edits"]) == 1
    assert "求片" in state["edits"][0][0]


def test_back_button_returns_full_user_panel(db, monkeypatch):
    _user(db)
    state = _patch_panel(monkeypatch)
    callbacks.handle_callback(db, _cq("user"))
    assert len(state["edits"]) == 1
    text, markup = state["edits"][0]
    assert "欢迎进入用户面板" in text and "用户编号" in text
    assert markup == handlers.user_panel_markup()


def test_unknown_group_ignored(db, monkeypatch):
    _user(db)
    state = _patch_panel(monkeypatch)
    callbacks.handle_callback(db, _cq("group_nope"))
    assert state["edits"] == []


# ---------- 二级里的私聊门禁仍生效 ----------

def test_group_functions_keep_private_only_guard_in_group_chat(db, monkeypatch):
    _user(db)
    state = _patch_panel(monkeypatch)
    group_chat = {"id": -100999}
    # 求片（media 组）：原有私聊限定卡
    callbacks.handle_callback(db, _cq("request", chat=group_chat))
    assert "私聊" in state["edits"][-1][0]
    # 绑定网盘 / 为什么播不了（drive / help 组）：P1 私聊查询口径
    callbacks.handle_callback(db, _cq("bindings", chat=group_chat))
    assert "私聊" in state["edits"][-1][0]
    callbacks.handle_callback(db, _cq("whyplay", chat=group_chat))
    assert "私聊" in state["edits"][-1][0]
