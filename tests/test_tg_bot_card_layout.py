"""TG Bot 卡片式版式（2026-10-30 用户批准样稿）：layout 骨架、图卡发送与分流、新菜单动作。

只测本模块：DB 用内存 SQLite；Telegram 发送全部 patch 掉，不发真实请求。
"""
from __future__ import annotations

import sys
from datetime import datetime

import pytest

sys.path.insert(0, ".")

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend import models
from backend.tg_bot import callbacks, ephemeral, handlers, layout, sender

TG = 900001


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


def _user(db, tg=TG, name="alice", points=42, is_staff=False):
    row = models.WebUser(
        username=name, password_hash="x", telegram_id=tg,
        points=points, vitality=14, is_staff=is_staff,
    )
    db.add(row)
    db.commit()
    return row


def _tg(tg=TG):
    return {"id": tg, "first_name": "Alice"}


# ---------- layout 骨架 ----------

def test_layout_title_variants():
    assert layout.title("签到") == "▎<b>签到</b>"
    assert layout.title("欢迎进入用户面板！", "Shun") == "▎<b>欢迎进入用户面板！</b>Shun"
    # 用户名做 HTML 转义，不许注入
    assert "&lt;b&gt;" in layout.title("面板", "<b>x</b>")


def test_layout_row_and_card():
    assert layout.row("🍒", "能量", 16) == "· 🍒 <b>能量</b> | 16"
    text = layout.card("▎标题", [layout.row("🍒", "能量", 16)], ["备注行"])
    assert text == "▎标题\n· 🍒 <b>能量</b> | 16\n\n备注行"


def test_caption_limit_boundary():
    assert layout.caption_fits("x" * layout.CAPTION_LIMIT)
    assert not layout.caption_fits("x" * (layout.CAPTION_LIMIT + 1))


def test_home_row_and_back_markup():
    assert layout.home_row() == [
        {"text": "♻️ 主界面", "callback_data": "menu:main"}]
    markup = layout.back_markup()
    assert markup["inline_keyboard"] == [layout.home_row()]
    markup_in = {"inline_keyboard": [[{"text": "a", "callback_data": "menu:points"}]]}
    merged = layout.append_home_row(markup_in)
    assert merged["inline_keyboard"][-1] == layout.home_row()
    assert len(markup_in["inline_keyboard"]) == 1  # 不改原 markup
    again = layout.append_home_row(merged)
    assert len(again["inline_keyboard"]) == 2  # 已有主界面行时不重复追加


def test_brand_image_asset_exists():
    assert layout.BRAND_IMAGE.is_file()
    assert layout.BRAND_IMAGE.read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"


# ---------- 主界面样稿 ----------

def test_main_card_text_matches_sample():
    text = handlers.main_card_text(_tg(), type("U", (), {"username": "alice"})())
    assert text == (
        "✨ 想看的片，喊一声就来\n"
        '🍉 你好呀，<a href="tg://user?id=900001">Alice</a> 请选择功能 👇'
    )


def test_main_markup_structure(monkeypatch):
    from backend.tg_bot import login_token
    monkeypatch.setattr(login_token, "build_login_url", lambda db_, user: None)
    engine_user = type("U", (), {"username": "alice", "is_staff": False})()
    markup = handlers.main_markup(None, engine_user, True)
    rows = markup["inline_keyboard"]
    assert [b["callback_data"] for b in rows[0]] == ["menu:user", "menu:server"]
    assert rows[1] == [{"text": "🎟️ 使用兑换码", "callback_data": "menu:renewcode"}]
    assert len(rows) == 2  # 非管理员无管理行


def test_main_markup_no_renewcode_wording(monkeypatch):
    # 改名守卫（2026-10-30 用户亲点）：核销按钮叫「使用兑换码」，三态主界面
    # 按钮里不许再出现「续期码」；独立续期功能是另一颗按钮，不在此列。
    from backend.tg_bot import login_token
    monkeypatch.setattr(login_token, "build_login_url", lambda db_, user: None)
    engine_user = type("U", (), {"username": "alice", "is_staff": False})()
    for markup in (
        handlers.main_markup(None, engine_user, True),
        handlers.main_markup(None, engine_user, False),
        handlers.main_markup(None, None, True),
    ):
        texts = [b["text"] for row in markup["inline_keyboard"] for b in row]
        assert not any("续期码" in t for t in texts)
    private_rows = handlers.main_markup(None, engine_user, True)["inline_keyboard"]
    assert private_rows[1] == [{"text": "🎟️ 使用兑换码", "callback_data": "menu:renewcode"}]


def test_main_markup_admin_row_and_login_first(monkeypatch):
    from backend.tg_bot import login_token
    staff = type("U", (), {"username": "boss", "is_staff": True})()
    monkeypatch.setattr(
        login_token, "build_login_url", lambda db_, user: "https://x.test/tg-login?token=T")
    markup = handlers.main_markup(None, staff, True)
    rows = markup["inline_keyboard"]
    assert rows[0][0]["url"].startswith("https://x.test/tg-login?token=")
    assert rows[-1] == [{"text": "👮 管理", "callback_data": "menu:admin"}]


def test_user_panel_markup_ends_with_home():
    markup = handlers.user_panel_markup()
    rows = markup["inline_keyboard"]
    assert rows[-1] == layout.home_row()
    flat = [b["callback_data"] for r in rows for b in r]
    # 一级只剩签到直达＋五个分组入口（按钮分组，不再平铺）
    for action in ("menu:checkin", "menu:group_media", "menu:group_drive",
                   "menu:group_account", "menu:group_welfare", "menu:group_help"):
        assert action in flat
    # 原功能按钮收进各自的二级分组面板
    grouped = [
        b["callback_data"]
        for key in ("media", "drive", "account", "welfare", "help")
        for r in handlers.user_panel_group_markup(key)["inline_keyboard"]
        for b in r
    ]
    for action in ("menu:request", "menu:myrequests",
                   "menu:transfers", "menu:bindings", "menu:login"):
        assert action in grouped


# ---------- sender 图卡与分流 ----------

def test_send_photo_uses_multipart(monkeypatch, db):
    calls = []
    monkeypatch.setattr(
        sender, "_call_multipart",
        lambda db_, method, payload, files: calls.append((method, payload, files)) or (True, None))
    ok, err = sender.send_photo(db, 123, "▎标题", {"inline_keyboard": []})
    assert ok and err is None
    method, payload, files = calls[0]
    assert method == "sendPhoto"
    assert payload["chat_id"] == "123" and payload["caption"] == "▎标题"
    assert "photo" in files


def test_send_photo_rejects_overlong_caption(monkeypatch, db):
    calls = []
    monkeypatch.setattr(
        sender, "_call_multipart",
        lambda db_, method, payload, files: calls.append(1) or (True, None))
    ok, err = sender.send_photo(db, 123, "x" * 1025)
    assert not ok and "1024" in err
    assert calls == []


def test_send_panel_falls_back_to_text_when_photo_fails(monkeypatch, db):
    monkeypatch.setattr(sender, "send_photo", lambda *a, **k: (False, "boom"))
    sent = []
    monkeypatch.setattr(
        sender, "send_message",
        lambda db_, chat_id, text, markup=None, parse_mode="HTML":
            sent.append(text) or (True, None))
    ok, _ = sender.send_panel(db, 1, "短文本")
    assert ok and sent == ["短文本"]


def test_send_panel_long_text_goes_text_directly(monkeypatch, db):
    photo_calls = []
    monkeypatch.setattr(
        sender, "send_photo", lambda *a, **k: photo_calls.append(1) or (True, None))
    sent = []
    monkeypatch.setattr(
        sender, "send_message",
        lambda db_, chat_id, text, markup=None, parse_mode="HTML":
            sent.append(text) or (True, None))
    ok, _ = sender.send_panel(db, 1, "x" * 2000)
    assert ok and photo_calls == [] and len(sent) == 1


def test_edit_panel_caption_for_photo_message(monkeypatch, db):
    calls = []
    monkeypatch.setattr(
        sender, "_call", lambda db_, method, payload: calls.append((method, payload)) or (True, None))
    ok, _ = sender.edit_panel(db, 1, 2, "新卡片", None, has_photo=True)
    assert ok and calls[0][0] == "editMessageCaption"
    assert calls[0][1]["caption"] == "新卡片"


def test_edit_panel_text_for_legacy_message(monkeypatch, db):
    calls = []
    monkeypatch.setattr(
        sender, "_call", lambda db_, method, payload: calls.append((method, payload)) or (True, None))
    ok, _ = sender.edit_panel(db, 1, 2, "新文本", None, has_photo=False)
    assert ok and calls[0][0] == "editMessageText"


def test_edit_panel_photo_overlong_sends_new_text(monkeypatch, db):
    sent = []
    monkeypatch.setattr(
        sender, "send_message",
        lambda db_, chat_id, text, markup=None, parse_mode="HTML":
            sent.append(text) or (True, None))
    calls = []
    monkeypatch.setattr(
        sender, "_call", lambda db_, method, payload: calls.append(method) or (True, None))
    ok, _ = sender.edit_panel(db, 1, 2, "x" * 2000, None, has_photo=True)
    assert ok and len(sent) == 1 and calls == []


# ---------- callbacks：按原消息形态分流 + 新动作 ----------

def _patch_callback_sender(monkeypatch):
    state = {"caption": [], "text": [], "send": []}
    monkeypatch.setattr(
        sender, "answer_callback_query", lambda db_, cq_id, text=None, show_alert=False: (True, None))
    monkeypatch.setattr(
        sender, "edit_message_caption",
        lambda db_, chat_id, mid, caption, markup=None, parse_mode="HTML":
            state["caption"].append(caption) or (True, None))
    monkeypatch.setattr(
        sender, "edit_message_text",
        lambda db_, chat_id, mid, text, markup=None, parse_mode="HTML":
            state["text"].append(text) or (True, None))
    monkeypatch.setattr(
        sender, "send_message",
        lambda db_, chat_id, text, markup=None, parse_mode="HTML":
            state["send"].append(text) or (True, None))
    return state


def _cq(action, photo=False, tg=TG, chat=None):
    msg = {"message_id": 55, "chat": chat or {"id": tg}}
    if photo:
        msg["photo"] = [{"file_id": "p1"}]
    return {"id": "cq-card-1", "from": _tg(tg), "data": f"menu:{action}", "message": msg}


def test_callback_photo_message_edits_caption(db, monkeypatch):
    _user(db)
    state = _patch_callback_sender(monkeypatch)
    callbacks.handle_callback(db, _cq("points", photo=True))
    assert state["caption"] and "能量余额" in state["caption"][0]
    assert state["text"] == []


def test_callback_legacy_text_message_edits_text(db, monkeypatch):
    _user(db)
    state = _patch_callback_sender(monkeypatch)
    callbacks.handle_callback(db, _cq("points", photo=False))
    assert state["text"] and "能量余额" in state["text"][0]
    assert state["caption"] == []


def test_callback_user_action_renders_panel(db, monkeypatch):
    _user(db)
    state = _patch_callback_sender(monkeypatch)
    callbacks.handle_callback(db, _cq("user"))
    assert state["text"] and "欢迎进入用户面板" in state["text"][0]
    assert "用户编号" in state["text"][0]


def test_callback_server_and_renewcode_actions(db, monkeypatch):
    _user(db)
    state = _patch_callback_sender(monkeypatch)
    callbacks.handle_callback(db, _cq("server"))
    assert "服务器" in state["text"][0]
    state["text"].clear()
    callbacks.handle_callback(db, _cq("renewcode"))
    assert "兑换码兑换" in state["text"][0]


def test_callback_admin_action_staff_only(db, monkeypatch):
    _user(db)  # 非 staff
    state = _patch_callback_sender(monkeypatch)
    callbacks.handle_callback(db, _cq("admin"))
    assert "仅管理员可用" in state["text"][0]
    staff = _user(db, tg=TG + 7, name="boss", is_staff=True)
    state["text"].clear()
    callbacks.handle_callback(db, _cq("admin", tg=TG + 7))
    assert "管理面板" in state["text"][0]


def test_callback_home_alias_renders_main(db, monkeypatch):
    _user(db)
    state = _patch_callback_sender(monkeypatch)
    callbacks.handle_callback(db, _cq("home"))
    assert state["text"] and "想看的片，喊一声就来" in state["text"][0]


def test_callback_user_action_group_blocked(db, monkeypatch):
    _user(db)
    state = _patch_callback_sender(monkeypatch)
    callbacks.handle_callback(db, _cq("user", chat={"id": -100999}))
    assert state["text"] and "私聊" in state["text"][0]
    assert "用户编号" not in state["text"][0]


# ---------- 用户面板与查询文案 ----------

def test_user_panel_fits_caption(db):
    row = _user(db)
    from backend.tg_bot import features
    text = features.user_panel(db, row, display_name="Alice")
    assert text.startswith("▎<b>欢迎进入用户面板！</b>Alice")
    assert layout.caption_fits(text)


def test_private_only_text_is_card():
    from backend.tg_bot import queries
    assert queries.PRIVATE_ONLY_TEXT.startswith("▎<b>私聊查询</b>")
    assert "私聊" in queries.PRIVATE_ONLY_TEXT


def test_unbound_card_text(db):
    text = handlers.unbound_card_text(_tg(), "Aetrix")
    assert "想看的片，喊一声就来" in text
    assert "<b>账号绑定</b> | 未绑定" in text
    assert "Aetrix" in text


def test_handle_server_panel(db):
    _user(db)
    text = handlers.handle_server(db, _tg(), TG, "")
    assert text.startswith("▎<b>服务器</b>")
    assert "<b>站点名称</b> | Aetrix" in text


def test_handle_admin_denies_non_staff(db):
    _user(db)
    text = handlers.handle_admin(db, _tg(), TG, "")
    assert "仅管理员可用" in text
