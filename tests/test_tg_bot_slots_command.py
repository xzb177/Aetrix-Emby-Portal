"""TG Bot /slots 直达命令＋命令说明场景标签（PR-3）。

/slots 与 menu:slots 面板按钮走同一条路径：群里出公共面板
（handlers.handle_slots_group，谁点注额谁下注），私聊出本人面板
（handlers.handle_slots）。开关口径与面板入口完全一致：群里要
slots_enabled 与 slots_group_enabled 都开，任一关闭回落「请私聊
使用」卡，绝不出现命令这条第二通道绕过开关。

场景标签：BOT_COMMANDS 的说明文案统一带 [群/私聊] 或 [私聊] 前缀，
逐条与 router 现行群内拦截事实（private_only / P1 / redeem 拒执行）
和 command_scopes.GROUP_COMMANDS 对齐；本文件用事实集反推期望标签
做双向断言，任何一处漂移（改 router 不改标签、改标签不改 scope）
都会在这里变红。
"""
from __future__ import annotations

import sys

import pytest

sys.path.insert(0, ".")

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend import models
from backend.tg_bot import command_scopes, ephemeral, poller, router, sender

GROUP_CHAT = -1007771234
TG_BOUND = 920201
TG_UNBOUND = 920299


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
    router._user_cmd_limits.clear()
    router._group_windows.clear()
    yield
    router._user_cmd_limits.clear()
    router._group_windows.clear()


@pytest.fixture()
def sent(monkeypatch):
    """捕获 router 回复（text 与 markup），自毁调度打桩。"""
    out = []

    def fake_send(db_, chat_id, text, reply_markup=None, **kw):
        out.append((chat_id, text, reply_markup))
        return (True, None)

    def fake_send_ex(db_, chat_id, text, reply_markup=None, **kw):
        out.append((chat_id, text, reply_markup))
        return (True, None, 9001)

    monkeypatch.setattr(sender, "send_message", fake_send)
    monkeypatch.setattr(sender, "send_message_ex", fake_send_ex)
    monkeypatch.setattr(
        sender, "send_panel",
        lambda db_, chat_id, text, reply_markup=None, **kw:
            out.append((chat_id, text, reply_markup)) or (True, None))
    monkeypatch.setattr(ephemeral, "schedule_delete", lambda *a, **kw: True)
    return out


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


def _set_group(db):
    _set_config(db, "bot_group_ids", str(GROUP_CHAT))


def _msg(tg_id, chat_id, text, chat_type):
    return {
        "update_id": 1,
        "message": {
            "message_id": 10,
            "text": text,
            "from": {"id": tg_id, "first_name": "T"},
            "chat": {"id": chat_id, "type": chat_type},
        },
    }


def _bet_buttons(markup):
    if not markup:
        return []
    return [b.get("callback_data", "")
            for row in markup.get("inline_keyboard", []) for b in row
            if b.get("callback_data", "").startswith("slots_bet:")]


# ---------- 群 /slots：公共面板 ----------

def test_group_slots_command_renders_public_panel(db, sent):
    _set_group(db)
    _user(db, TG_BOUND, "alice")
    router.dispatch(db, _msg(TG_BOUND, GROUP_CHAT, "/slots", "supergroup"))
    assert len(sent) == 1
    _chat, text, markup = sent[0]
    assert "未知命令" not in text
    assert "头奖池" in text
    assert "我的能量" not in text  # 公共面板不带个人数据
    assert set(_bet_buttons(markup)) == {
        "slots_bet:5", "slots_bet:10", "slots_bet:20"}


def test_group_slots_command_unbound_sender_still_public_panel(db, sent):
    """未绑定者在群里发 /slots 也能看到公共面板（绑定在点注时才判）。"""
    _set_group(db)
    router.dispatch(db, _msg(TG_UNBOUND, GROUP_CHAT, "/slots", "supergroup"))
    assert len(sent) == 1
    _chat, text, markup = sent[0]
    assert "头奖池" in text
    assert _bet_buttons(markup)


# ---------- 私聊 /slots：本人面板不回归 ----------

def test_private_slots_command_bound_panel(db, sent):
    _user(db, TG_BOUND, "alice")
    router.dispatch(db, _msg(TG_BOUND, TG_BOUND, "/slots", "private"))
    assert len(sent) == 1
    _chat, text, markup = sent[0]
    assert "未知命令" not in text
    assert "头奖池" in text and "我的能量" in text
    assert _bet_buttons(markup)


def test_private_slots_command_unbound_bind_guide(db, sent):
    router.dispatch(db, _msg(TG_UNBOUND, TG_UNBOUND, "/slots", "private"))
    assert len(sent) == 1
    _chat, text, markup = sent[0]
    assert "绑定" in text
    assert not _bet_buttons(markup)


# ---------- 开关口径：与面板入口一致 ----------

def test_group_slots_command_group_switch_off(db, sent):
    """slots_group_enabled 关 → 群 /slots 与面板同口径回私聊卡，不出注额钮。"""
    _set_group(db)
    _set_config(db, "slots_group_enabled", "false")
    _user(db, TG_BOUND, "alice")
    router.dispatch(db, _msg(TG_BOUND, GROUP_CHAT, "/slots", "supergroup"))
    assert len(sent) == 1
    _chat, text, markup = sent[0]
    assert "老虎机请私聊机器人使用" in text
    assert not _bet_buttons(markup)


def test_group_slots_command_master_switch_off(db, sent):
    """slots_enabled 关 → 群 /slots 同样回私聊卡（不开第二通道）。"""
    _set_group(db)
    _set_config(db, "slots_enabled", "false")
    _user(db, TG_BOUND, "alice")
    router.dispatch(db, _msg(TG_BOUND, GROUP_CHAT, "/slots", "supergroup"))
    assert len(sent) == 1
    _chat, text, markup = sent[0]
    assert "老虎机请私聊机器人使用" in text
    assert not _bet_buttons(markup)


def test_private_slots_command_master_switch_off(db, sent):
    _set_config(db, "slots_enabled", "false")
    _user(db, TG_BOUND, "alice")
    router.dispatch(db, _msg(TG_BOUND, TG_BOUND, "/slots", "private"))
    assert len(sent) == 1
    _chat, text, markup = sent[0]
    assert "暂未开启" in text
    assert not _bet_buttons(markup)


# ---------- 三处注册对齐 ----------

def test_slots_registered_in_all_three_places():
    assert ("slots", "[群/私聊] 老虎机") in poller.BOT_COMMANDS
    assert "slots" in command_scopes.GROUP_COMMANDS
    group_names = {n for n, _ in command_scopes.group_commands(poller.BOT_COMMANDS)}
    assert "slots" in group_names
    default_names = {n for n, _ in command_scopes.default_commands(poller.BOT_COMMANDS)}
    # 二期面板优先：default 菜单只留 start/help/me，slots 靠面板按钮与
    # 群菜单点达，不再进普通用户私聊菜单（手输仍可用）
    assert "slots" not in default_names
    admin_names = {n for n, _ in command_scopes.admin_commands(poller.BOT_COMMANDS)}
    assert "slots" in admin_names
    assert "slots" not in command_scopes.ADMIN_ONLY_COMMANDS


# ---------- 场景标签与 scope 事实双向对齐 ----------

# router 现行群内拦截事实（dispatch 亲读）：以下命令在群里不执行。
_PRIVATE_ONLY_FACT = {
    # private_only 集合（me/account/level 是账号展示一家、群里放行，
    # 2026-10-28 定、2026-11-02 重申，不在本集）
    "request", "myrequests", "login", "renew", "govlog",
    "report", "propose", "launch", "reconcile", "rankpush",
    # P1 个人查询：群里不执行
    "bindings", "transfers", "whyplay", "wants",
    # 群里拒执行（删原消息＋提示私聊）
    "redeem",
}

# 群内可执行但仅管理员、且刻意不进群菜单 scope 的命令（2026-11-01）：
# /whois 群里出管理员精简卡——GROUP_COMMANDS 是给全群看的菜单，
# 管理员命令不进；标签仍按「群里能用」的事实记 [群/私聊]。
_GROUP_ADMIN_FACT = {"whois"}


def test_scene_tags_match_scope_facts():
    tagged_group_ok = set()
    for name, desc in poller.BOT_COMMANDS:
        assert desc.startswith(("[群] ", "[私聊] ", "[群/私聊] ")), (name, desc)
        if name in command_scopes.GROUP_COMMANDS:
            expected = "[群/私聊] "
            tagged_group_ok.add(name)
        elif name in _GROUP_ADMIN_FACT:
            expected = "[群/私聊] "
        else:
            expected = "[私聊] "
            # 不在群 scope 的命令必须能在 router 拦截事实里找到依据；
            # 找不到依据说明标签/scope 与 router 漂移了。
            assert name in _PRIVATE_ONLY_FACT, name
        assert desc.startswith(expected), (name, desc, expected)
    # 双向：群 scope 里登记的每条命令都必须在 BOT_COMMANDS 里带 [群/私聊]
    bot_names = {n for n, _ in poller.BOT_COMMANDS}
    assert tagged_group_ok == set(command_scopes.GROUP_COMMANDS) & bot_names
