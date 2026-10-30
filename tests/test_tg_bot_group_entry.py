"""TG Bot 群内入口修复（PR-1）：群 /start 带按钮菜单、老虎机进第一屏。

旧断点：handle_start 群分支对已绑定用户只 return compact_dashboard
纯文本（无 markup 元组），群里没有任何按钮能点到 menu:slots；
_group_menu_markup 是孤儿菜单且混有四项个人查询按钮（点开会把点击者
个人数据编辑进群共享消息，与命令层私聊限定口径冲突）。

本文件断言定版行为：
- 群 /start 已绑定 ＝ 九行信息卡正文（compact_dashboard 一字不改）
  ＋新群菜单（含 menu:slots、无四项个人查询、无绑定钮）；
- 群 /start 未绑定 ＝ 绑定卡 ＋ 绑定/帮助/老虎机围观钮；
- 私聊 /start 与私聊未绑定菜单一字不变（无 menu:slots 混入）；
- menu:bindings/transfers/whyplay/wants 在群里回「私聊使用」卡
  ＋「💬 去私聊」深链按钮，私聊仍走原渲染。
"""
from __future__ import annotations

import sys

import pytest

sys.path.insert(0, ".")

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend import models
from backend.tg_bot import callbacks, features, handlers, sender

GROUP = -1001234567
TG = 900001

PERSONAL_ACTIONS = ("bindings", "transfers", "whyplay", "wants")


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


def _user(db, tg=TG, name="alice", points=42):
    row = models.WebUser(
        username=name, password_hash="x", telegram_id=tg, points=points)
    db.add(row)
    db.commit()
    return row


def _tg(tg=TG):
    return {"id": tg, "first_name": "Alice"}


def _all_buttons(markup):
    return [b for row in (markup or {}).get("inline_keyboard", []) for b in row]


def _callback_datas(markup):
    return [b.get("callback_data") for b in _all_buttons(markup)]


def test_group_start_bound_returns_card_with_group_menu(db):
    user = _user(db)
    result = handlers.handle_start(db, _tg(), GROUP, "")
    assert isinstance(result, tuple)
    text, markup = result
    # 正文就是现行信息卡，一字不改
    assert text == features.compact_dashboard(db, user, display_name="Alice")
    # 群菜单定版五行的 callback 序列
    rows = markup["inline_keyboard"]
    assert [[b.get("callback_data") for b in row] for row in rows] == [
        ["menu:checkin", "menu:points"],
        ["menu:slots", "menu:lottery"],
        ["menu:redpacket", "menu:rank"],
        ["menu:requestboard", "menu:rules"],
        ["menu:account", "menu:help"],
    ]
    # 老虎机钮文案在第一屏
    slots_btn = rows[1][0]
    assert "老虎机" in slots_btn["text"]
    # 四项个人查询与绑定钮不进群菜单
    for gone in ("menu:bindings", "menu:transfers", "menu:whyplay",
                 "menu:wants", "menu:bind"):
        assert gone not in _callback_datas(markup)


def test_group_start_unbound_has_slots_watch_button(db):
    result = handlers.handle_start(db, {"id": 900099, "first_name": "New"},
                                   GROUP, "")
    assert isinstance(result, tuple)
    text, markup = result
    assert "账号绑定" in text  # 仍是绑定卡
    datas = _callback_datas(markup)
    assert "menu:bind" in datas
    assert "menu:help" in datas
    assert "menu:slots" in datas  # 围观入口


def test_private_start_bound_unchanged(db):
    user = _user(db)
    result = handlers.handle_start(db, _tg(), TG, "")
    assert isinstance(result, tuple)
    text, markup = result
    assert text == handlers.main_card_text(_tg(), user)
    assert markup == handlers.main_markup(db, user, True)
    assert "menu:slots" not in _callback_datas(markup)


def test_private_start_unbound_has_no_slots_button(db):
    result = handlers.handle_start(db, {"id": 900099, "first_name": "New"},
                                   900099, "")
    text, markup = result
    assert _callback_datas(markup) == ["menu:bind", "menu:help"]


def test_group_menu_main_callback_contains_slots(db):
    _user(db)
    text, markup = callbacks._render_menu(
        db, _tg(), TG, GROUP, False, "main")
    assert "menu:slots" in _callback_datas(markup)


@pytest.mark.parametrize("action", PERSONAL_ACTIONS)
def test_personal_query_in_group_returns_private_only_card(db, monkeypatch,
                                                           action):
    _user(db)
    monkeypatch.setattr(
        "backend.tg_bind.get_bot_username", lambda db_: "aetrix_test_bot")
    text, markup = callbacks._render_menu(
        db, _tg(), TG, GROUP, False, action)
    assert "请私聊机器人使用" in text
    urls = [b.get("url") for b in _all_buttons(markup) if b.get("url")]
    assert urls == ["https://t.me/aetrix_test_bot"]
    link_btn = [b for b in _all_buttons(markup) if b.get("url")][0]
    assert "去私聊" in link_btn["text"]


def test_personal_query_group_card_without_username_has_no_url(db,
                                                                monkeypatch):
    _user(db)
    monkeypatch.setattr("backend.tg_bind.get_bot_username", lambda db_: "")
    text, markup = callbacks._render_menu(
        db, _tg(), TG, GROUP, False, "bindings")
    assert "请私聊机器人使用" in text
    assert not [b for b in _all_buttons(markup) if b.get("url")]
    assert "menu:main" in _callback_datas(markup)  # 仍有回主界面


@pytest.mark.parametrize("action", PERSONAL_ACTIONS)
def test_personal_query_in_private_still_renders(db, action):
    _user(db)
    text, markup = callbacks._render_menu(db, _tg(), TG, TG, True, action)
    assert "请私聊机器人使用" not in text


def test_group_bindings_callback_edits_private_only_card(db, monkeypatch):
    """整链一遍：群里点「我的绑定」旧按钮（存量消息）→ 编辑成私聊卡。"""
    _user(db)
    monkeypatch.setattr(
        "backend.tg_bind.get_bot_username", lambda db_: "aetrix_test_bot")
    answered, edits = [], []

    def fake_answer(db_, cq_id, text=None, show_alert=False):
        answered.append(cq_id)
        return (True, None)

    def fake_edit(db_, chat_id, message_id, text, reply_markup=None,
                  parse_mode="HTML"):
        edits.append((chat_id, message_id, text, reply_markup))
        return (True, None)

    monkeypatch.setattr(sender, "answer_callback_query", fake_answer)
    monkeypatch.setattr(sender, "edit_message_text", fake_edit)
    cq = {
        "id": "cq-group-bindings",
        "from": {"id": TG, "first_name": "Alice"},
        "data": "menu:bindings",
        "message": {"message_id": 77, "chat": {"id": GROUP}},
    }
    callbacks.handle_callback(db, cq)
    assert len(edits) == 1
    chat_id, _mid, text, markup = edits[0]
    assert chat_id == GROUP
    assert "请私聊机器人使用" in text
    urls = [b.get("url") for b in _all_buttons(markup) if b.get("url")]
    assert urls == ["https://t.me/aetrix_test_bot"]
