"""TG Bot 精美化：菜单回调（menu:*）与 parse_mode 测试。"""
from __future__ import annotations

import sys

import pytest

sys.path.insert(0, ".")

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend import models
from backend.tg_bot import callbacks, handlers, sender


@pytest.fixture()
def db():
    engine = create_engine("sqlite:///:memory:")
    models.WebUser.__table__.create(engine, checkfirst=True)
    models.SystemConfig.__table__.create(engine, checkfirst=True)
    models.CheckinRecord.__table__.create(engine, checkfirst=True)
    models.PointsLog.__table__.create(engine, checkfirst=True)
    models.VitalityLog.__table__.create(engine, checkfirst=True)
    Session = sessionmaker(bind=engine)
    session = Session()
    yield session
    session.close()


def _make_user(db, username="menutest", tg_id=800001, points=100):
    u = models.WebUser(username=username, telegram_id=tg_id, points=points)
    for col in ("password_hash",):
        if hasattr(u, col) and getattr(u, col) is None:
            try:
                setattr(u, col, "")
            except Exception:
                pass
    db.add(u)
    db.commit()
    return u


def _cq(action, tg_id=800001, chat_id=800001, message_id=55):
    return {
        "id": "cq-menu-1",
        "from": {"id": tg_id, "first_name": "Menu"},
        "data": f"menu:{action}",
        "message": {"message_id": message_id, "chat": {"id": chat_id}},
    }


def _patch_sender(monkeypatch):
    answered, edits = [], []

    def fake_answer(db_, cq_id, text=None, show_alert=False):
        answered.append((cq_id, text, show_alert))
        return (True, None)

    def fake_edit(db_, chat_id, message_id, text, reply_markup=None, parse_mode="HTML"):
        edits.append((chat_id, message_id, text, reply_markup, parse_mode))
        return (True, None)

    monkeypatch.setattr(sender, "answer_callback_query", fake_answer)
    monkeypatch.setattr(sender, "edit_message_text", fake_edit)
    return answered, edits


def test_menu_main_bound_edits_message(db, monkeypatch):
    _make_user(db)
    answered, edits = _patch_sender(monkeypatch)
    callbacks.handle_callback(db, _cq("main"))
    assert len(answered) == 1  # loading 被消除
    assert len(edits) == 1
    chat_id, message_id, text, markup, parse_mode = edits[0]
    assert chat_id == 800001 and message_id == 55
    assert "✨ 想看的片，喊一声就来" in text and "请选择功能" in text
    assert parse_mode == "HTML"
    # 私聊：首行是一键登录 URL 按钮（无 token 时可能没有首行，只断言结构合法）
    rows = markup["inline_keyboard"]
    assert any("menu:user" in b.get("callback_data", "") for r in rows for b in r)


def test_menu_main_unbound_shows_bind(db, monkeypatch):
    answered, edits = _patch_sender(monkeypatch)
    callbacks.handle_callback(db, _cq("main", tg_id=800099, chat_id=800099))
    assert len(edits) == 1
    text = edits[0][2]
    assert "绑定" in text


def test_menu_checkin_flow(db, monkeypatch):
    from backend.integrations import store
    store.write_values(db, {"checkin_enabled": "true"})
    db.commit()
    _make_user(db)
    answered, edits = _patch_sender(monkeypatch)
    callbacks.handle_callback(db, _cq("checkin"))
    assert len(edits) == 1
    text, markup = edits[0][2], edits[0][3]
    assert "签到" in text
    # 有返回主菜单按钮
    assert markup["inline_keyboard"][0][0]["callback_data"] == "menu:main"


def test_menu_points_shows_card(db, monkeypatch):
    _make_user(db, points=250)
    answered, edits = _patch_sender(monkeypatch)
    callbacks.handle_callback(db, _cq("points"))
    assert len(edits) == 1
    assert "✨ <b>能量余额</b> | 250" in edits[0][2]


def test_menu_unknown_action_ignored(db, monkeypatch):
    _make_user(db)
    answered, edits = _patch_sender(monkeypatch)
    callbacks.handle_callback(db, _cq("nosuchaction"))
    assert edits == []


def test_menu_missing_ids_ignored(db, monkeypatch):
    answered, edits = _patch_sender(monkeypatch)
    cq = {"id": "x", "from": {}, "data": "menu:main", "message": {}}
    callbacks.handle_callback(db, cq)
    assert edits == []


def test_send_message_default_parse_mode_html(db, monkeypatch):
    calls = []
    monkeypatch.setattr(
        sender, "_call", lambda db_, method, payload: calls.append((method, payload)) or (True, None))
    sender.send_message(db, 123, "hi")
    assert calls[0][0] == "sendMessage"
    assert calls[0][1]["parse_mode"] == "HTML"


def test_send_message_parse_mode_none_omits(db, monkeypatch):
    calls = []
    monkeypatch.setattr(
        sender, "_call", lambda db_, method, payload: calls.append((method, payload)) or (True, None))
    sender.send_message(db, 123, "hi", parse_mode=None)
    assert "parse_mode" not in calls[0][1]


def test_set_my_commands_payload(db, monkeypatch):
    calls = []
    monkeypatch.setattr(
        sender, "_call", lambda db_, method, payload: calls.append((method, payload)) or (True, None))
    sender.set_my_commands(db, [("start", "欢迎"), ("help", "帮助")])
    method, payload = calls[0]
    assert method == "setMyCommands"
    assert payload["commands"] == [
        {"command": "start", "description": "欢迎"},
        {"command": "help", "description": "帮助"},
    ]


def test_handle_start_unbound_returns_menu_markup(db):
    result = handlers.handle_start(db, {"id": 800111, "first_name": "N"}, 800111, "")
    assert isinstance(result, tuple)
    text, markup = result
    assert "左下角" in text
    rows = markup["inline_keyboard"]
    assert rows[0][0]["callback_data"] == "menu:bind"


def test_menu_main_private_login_button_first(db, monkeypatch):
    """私聊主菜单：若有登录 URL，必须在 [0][0]（与 test_tg_start_login_private 同一契约）。"""
    from backend.tg_bot import login_token
    _make_user(db, username="urluser", tg_id=800222)
    monkeypatch.setattr(login_token, "build_login_url",
                        lambda db_, user: "https://x.test/login?token=TOKEN")
    text, markup = handlers.menu_main(db, {"id": 800222}, True)
    assert "token=TOKEN" in markup["inline_keyboard"][0][0]["url"]
