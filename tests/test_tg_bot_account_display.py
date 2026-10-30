"""TG Bot 账号展示：命令进原生菜单＋账号加成就馆灵＋群里放行。

只测本模块：DB 用内存 SQLite；Telegram 发送全部 patch 掉，不发真实请求。
"""
from __future__ import annotations

import sys

import pytest

sys.path.insert(0, ".")

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend import models
from backend.emby_server import models as em
from backend.tg_bot import callbacks, features, handlers, poller, sender

TICKS = 10_000_000
GROUP = -100123
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


def _user(db, tg=TG, name="alice", points=42, vitality=14):
    row = models.WebUser(
        username=name, password_hash="x", telegram_id=tg,
        points=points, vitality=vitality,
    )
    db.add(row)
    db.commit()
    return row


def _tg(tg=TG):
    return {"id": tg, "first_name": "Alice"}


def _watch(db, user_id, hours_list):
    for i, h in enumerate(hours_list):
        item = em.MediaItem(
            guid=f"test-item-{user_id}-{i}", library_id=1,
            item_type="movie", name=f"片{i}",
            duration_ticks=int(h * 3600 * TICKS),
        )
        db.add(item)
        db.flush()
        db.add(em.PlaybackSession(
            user_id=user_id, item_id=item.id,
            position_ticks=int(h * 3600 * TICKS),
            session_key=f"test-sess-{user_id}-{i}",
        ))
    db.commit()


def _achievements(db, user_id):
    db.add(models.PanelAchievementDef(
        key="a1", name="首夜", desc="d", icon="🌙", sort=1, enabled=True))
    db.add(models.PanelAchievementDef(
        key="b1", name="追剧达人", desc="d", icon="🎬", sort=2, enabled=True))
    db.add(models.PanelAchievementDef(
        key="c1", name="隐藏", desc="d", icon="🙈", sort=3, enabled=False))
    db.add(models.PanelAchievementUnlock(user_id=user_id, ach_key="a1"))
    db.add(models.PanelAchievementUnlock(user_id=user_id, ach_key="c1"))
    db.commit()


# ---------- 改动 1：命令不出消息 ----------

def test_help_has_no_command_list(db):
    text = handlers.handle_help(db, _tg(), TG, "")
    assert "可用命令" not in text
    assert "/account" not in text
    assert "左下角" in text


def test_start_bound_private_has_dashboard_no_command_list(db):
    row = _user(db)
    _watch(db, row.id, [100, 20])
    _achievements(db, row.id)
    reply = handlers.handle_start(db, _tg(), TG, "")
    assert isinstance(reply, tuple)
    text, _markup = reply
    assert "可用命令" not in text
    assert "左下角" not in text  # 末尾菜单指引行已删
    # 私聊 /start 已按批准样稿改主卡（仪表盘在用户面板/群聊 /start）
    assert "✨ 想看的片，喊一声就来" in text and "请选择功能" in text


def test_start_bound_group_has_dashboard_no_login_link(db):
    row = _user(db)
    # 群 /start 现返回 (正文, 群菜单) 元组：正文仍是 dashboard 九行卡，
    # 登录链接不进正文、也不进群菜单按钮（群里无一键登录行）。
    reply = handlers.handle_start(db, _tg(), GROUP, "")
    assert isinstance(reply, tuple)
    text, markup = reply
    assert "可用命令" not in text
    assert "能量" in text
    assert "一键免密登录请私聊" not in text  # 末尾私聊登录指引行已删
    assert "左下角" not in text  # 末尾菜单指引行已删
    assert "token=" not in text
    assert not [b for r in markup["inline_keyboard"] for b in r if b.get("url")]


def test_start_unbound_has_no_command_list(db):
    reply = handlers.handle_start(db, _tg(), TG, "")
    assert isinstance(reply, tuple)
    text, _markup = reply
    assert "可用命令" not in text
    assert "左下角" in text
    assert "绑定" in text


# ---------- 改动 2：账号展示加成就＋馆灵 ----------

def test_account_full_display(db):
    row = _user(db)
    _watch(db, row.id, [100, 20])
    _achievements(db, row.id)
    text = features.handle_account(db, _tg(), TG, "")
    assert "🍒 <b>能量余额</b> |" in text and "积分：" not in text
    assert "活力数值" in text
    # 成就：只展示 enabled 且已解锁的定义；总数 Y 只计 enabled
    assert "已解锁 1／共 2 个" in text
    assert "🌙 首夜" in text
    assert "追剧达人" not in text  # 未解锁不展示
    assert "隐藏" not in text  # disabled 定义不展示
    # 馆灵：120 小时 → 夜枭（50），下一阶段守夜者（200），还差 80
    assert "夜枭" in text and "120.0" in text
    assert "守夜者" in text and "80.0" in text


def test_account_empty_achievements_and_pet_egg(db):
    _user(db)
    text = features.handle_account(db, _tg(), TG, "")
    assert "暂无成就" in text
    assert "已解锁 0／共 0 个" in text
    assert "蛋" in text and "50.0" in text  # 0 小时 → 蛋，下一阶段夜枭还差 50


def test_account_pet_max_stage(db):
    row = _user(db)
    _watch(db, row.id, [1200])
    text = features.handle_account(db, _tg(), TG, "")
    assert "馆灵" in text and "已满级" in text


def test_compact_dashboard_counts(db):
    row = _user(db)
    _watch(db, row.id, [100, 20])
    _achievements(db, row.id)
    text = features.compact_dashboard(db, row)
    assert all(x in text for x in ("alice", "能量", "活力", "夜枭", "120.0"))
    assert "<b>成就进度</b> | 1/2" in text
    assert "订阅" not in text  # 精简版不展开订阅


# ---------- 改动 3：群里放行账号展示 ----------

def _dispatch_group(db, monkeypatch, text):
    sent = []

    def fake_send(db_, chat_id, text_, reply_markup=None, parse_mode="HTML"):
        sent.append(text_)
        return (True, None)

    monkeypatch.setattr(sender, "send_message", fake_send)

    def fake_send_ex(db_, chat_id, text_, reply_markup=None, parse_mode="HTML"):
        sent.append(text_)
        return (True, None, 1)

    monkeypatch.setattr(sender, "send_message_ex", fake_send_ex)
    monkeypatch.setattr(
        "backend.tg_bot.ephemeral.schedule_delete", lambda *a, **k: True)
    update = {
        "update_id": 1,
        "message": {"message_id": 1, "from": _tg(), "chat": {"id": GROUP, "type": "group"},
                    "text": text},
    }
    import backend.tg_bot.router as router_mod
    router_mod.dispatch(db, update)
    return sent


def test_account_and_me_allowed_in_group(db, monkeypatch):
    _user(db)
    for cmd in ("/account", "/me"):
        sent = _dispatch_group(db, monkeypatch, cmd)
        assert sent, cmd
        assert "账号中心" in sent[0] and "能量" in sent[0]
        assert "请私聊" not in sent[0]


def test_login_still_blocked_in_group(db, monkeypatch):
    _user(db)
    sent = _dispatch_group(db, monkeypatch, "/login")
    assert sent and "请私聊" in sent[0]


def test_p1_still_blocked_in_group(db, monkeypatch):
    _user(db)
    sent = _dispatch_group(db, monkeypatch, "/bindings")
    assert sent and "私聊" in sent[0]


def test_menu_callback_account_allowed_in_group(db, monkeypatch):
    _user(db)
    edited = []

    def fake_answer(db_, cq_id):
        return (True, None)

    def fake_edit(db_, chat_id, message_id, text_, reply_markup=None, parse_mode="HTML"):
        edited.append(text_)
        return (True, None)

    monkeypatch.setattr(sender, "answer_callback_query", fake_answer)
    monkeypatch.setattr(sender, "edit_message_text", fake_edit)
    cq = {
        "id": "cq1", "data": "menu:account", "from": _tg(),
        "message": {"message_id": 5, "chat": {"id": GROUP, "type": "group"}},
    }
    callbacks.handle_callback(db, cq)
    assert edited and "账号中心" in edited[0]
    assert "请私聊" not in edited[0]


# ---------- 注册表覆盖路由命令 ----------

def test_bot_commands_cover_router_commands():
    registered = {cmd for cmd, _desc in poller.BOT_COMMANDS}
    router_commands = {
        "start", "help", "bind", "checkin", "points", "account", "me",
        "request", "myrequests", "rank", "requestboard", "rules",
        "login", "renew", "redeem", "lottery", "redpacket", "slots",
        "bindings", "transfers", "whyplay", "wants",
        "level", "report", "propose", "launch",
    }
    assert router_commands <= registered
    assert ("me", "[群/私聊] 账号中心") in poller.BOT_COMMANDS
    assert ("renew", "[私聊] 快捷登录") in poller.BOT_COMMANDS
