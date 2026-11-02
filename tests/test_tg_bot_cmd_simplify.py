# -*- coding: utf-8 -*-
"""TG Bot 命令简化（方案三＋/start 群内入口卡）新口径回归。

口径（用户 2026-11-02 三次定夺）：
- 菜单 31→27：摘 renew/chatpoints/level/account，/me 留菜单且是唯一
  账号命令；被摘四条与 /level 手输保留为别名（router 照常认）。
- /start：群里只回「私密操作入口」简短卡＋群菜单按钮，不展示任何
  个人账号数据；私聊是欢迎＋快捷入口（主卡），同样不渲染账号卡。
- 账号卡只有一张（features.account_card 唯一构建器）：/me、/account、
  用户面板（user_panel/compact_dashboard 旧名）输出逐字一致；固定
  11 行汇总（编号/名称/等级带经验进度/能量/资格/凭证/活力/绑定/
  馆灵/成就进度/订阅状态），不逐条铺成就/订阅/设备，不带连签行。
- 账号展示一家（/me、/account、/level）群里放行（2026-10-28 用户
  亲定、2026-11-02 10:43 重申）：群里调用直接回账号卡（#1705 曾一并
  改成私聊限定，用户纠正后恢复）；其余私聊命令（private_only/P1/
  redeem）在群里仍回私聊提示形态。
- /points 输出并入群发言能量段（今日已得/累计能量/发言规则），
  /chatpoints 手输仍出原群发言能量卡。
"""
from __future__ import annotations

import sys

import pytest

sys.path.insert(0, ".")

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend import models
from backend.tg_bot import command_scopes, features, handlers, poller, sender

TG = 900001
GROUP = -100123

ACCOUNT_LABELS = [
    "用户编号", "账号名称", "当前等级", "能量余额", "公益资格",
    "查看凭证", "活力数值", "网盘绑定", "当前馆灵", "成就进度",
    "订阅状态",
]


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


def _dispatch(db, monkeypatch, text, chat_id, chat_type="private"):
    sent = []

    def fake_send(db_, cid, text_, reply_markup=None, parse_mode="HTML"):
        sent.append((text_, reply_markup))
        return (True, None)

    def fake_send_ex(db_, cid, text_, reply_markup=None, parse_mode="HTML"):
        sent.append((text_, reply_markup))
        return (True, None, 1)

    monkeypatch.setattr(sender, "send_message", fake_send)
    monkeypatch.setattr(sender, "send_message_ex", fake_send_ex)
    monkeypatch.setattr(
        "backend.tg_bot.ephemeral.schedule_delete", lambda *a, **k: True)
    update = {
        "update_id": 1,
        "message": {
            "message_id": 1, "from": _tg(),
            "chat": {"id": chat_id, "type": chat_type},
            "text": text,
        },
    }
    from backend.tg_bot import router as router_mod

    router_mod.dispatch(db, update)
    return sent


# ---------- 菜单 31→27 ----------

def test_menu_slimmed_to_27_with_me_kept():
    names = [n for n, _ in poller.BOT_COMMANDS]
    assert len(names) == 27
    assert ("me", "[群/私聊] 账号中心") in poller.BOT_COMMANDS
    for gone in ("account", "renew", "chatpoints", "level"):
        assert gone not in names


def test_group_scope_drops_private_and_merged_commands():
    group_names = {n for n, _ in command_scopes.group_commands(poller.BOT_COMMANDS)}
    for gone in ("chatpoints", "renew"):
        assert gone not in command_scopes.GROUP_COMMANDS
        assert gone not in group_names
    # 账号展示一家群里放行（2026-10-28 定、2026-11-02 重申）：me 在
    # 菜单表里、要进群菜单；account/level 是手输别名不在菜单表，但
    # GROUP_COMMANDS 集合口径要含（router 群里放行）
    assert "me" in command_scopes.GROUP_COMMANDS
    assert "me" in group_names
    for alias in ("account", "level"):
        assert alias in command_scopes.GROUP_COMMANDS
        assert alias not in group_names
    assert "start" in group_names


# ---------- 账号卡：唯一构建器＋11 行汇总 ----------

def test_account_card_single_builder_11_rows(db):
    user = _user(db)
    via_panel = features.user_panel(db, user)
    via_dashboard = features.compact_dashboard(db, user)
    via_account = features.handle_account(db, _tg(), TG, "")
    assert via_panel == via_dashboard == via_account
    rows = [ln for ln in via_account.split("\n") if ln.startswith("· ")]
    assert len(rows) == 11
    for label in ACCOUNT_LABELS:
        assert f"<b>{label}</b>" in via_account
    # 精简：不逐条铺成就/订阅/设备、无连签行
    assert "最近设备" not in via_account
    assert "连签天数" not in via_account
    assert "解锁成就" not in via_account
    # /level 的等级详情（经验进度）已并入当前等级行
    assert "经验" in via_account and "还差" in via_account


def test_me_and_account_alias_show_same_card_private(db, monkeypatch):
    _user(db)
    me = _dispatch(db, monkeypatch, "/me", TG)
    account = _dispatch(db, monkeypatch, "/account", TG)
    assert me and account
    assert me[0][0] == account[0][0]
    assert "账号名称" in me[0][0]


def test_level_alias_points_to_account_card_private(db, monkeypatch):
    _user(db)
    sent = _dispatch(db, monkeypatch, "/level", TG)
    assert sent
    assert "账号名称" in sent[0][0]


# ---------- /me 群里放行：账号展示一家在群里直接回账号卡 ----------
# 2026-10-28 用户亲定、2026-11-02 10:43 重申；登录/求片等其余私聊
# 命令在群里仍走私聊提示形态（下一节断言未动）。

def test_me_in_group_renders_account_card(db, monkeypatch):
    _user(db)
    # 同一用户刚在私聊用例里发过 /me：清掉 router 用户级限流的进程内
    # 状态，避免被「操作太快」挡下（限流本身的行为不在本例口径内）
    monkeypatch.setattr("backend.tg_bot.router._user_cmd_limits", {})
    sent = _dispatch(db, monkeypatch, "/me", GROUP, "group")
    assert sent
    text, _markup = sent[0]
    assert "账号名称" in text
    assert "能量余额" in text
    assert "这是一条私聊命令" not in text


def test_account_and_level_in_group_render_account_card(db, monkeypatch):
    _user(db)
    monkeypatch.setattr("backend.tg_bot.router._user_cmd_limits", {})
    for cmd in ("/account", "/level"):
        sent = _dispatch(db, monkeypatch, cmd, GROUP, "group")
        assert sent, cmd
        assert "账号名称" in sent[0][0], cmd
        assert sent[0][0] != handlers.private_only_card(), cmd


def test_other_private_commands_in_group_use_same_form(db, monkeypatch):
    _user(db)
    for cmd in ("/login", "/bindings", "/redeem 123456"):
        sent = _dispatch(db, monkeypatch, cmd, GROUP, "group")
        assert sent, cmd
        assert sent[0][0] == handlers.private_only_card(), cmd


# ---------- /start：群里入口卡、私聊欢迎卡，都不渲染账号卡 ----------

def test_start_group_bound_is_entry_card_no_account_data(db):
    _user(db)
    reply = handlers.handle_start(db, _tg(), GROUP, "")
    assert isinstance(reply, tuple)
    text, markup = reply
    assert "私密操作入口" in text
    assert "入库喜报" in text
    assert "能量余额" not in text and "用户编号" not in text
    datas = [b.get("callback_data") for row in markup["inline_keyboard"] for b in row]
    assert "menu:account" in datas  # 按钮可链到账号面板（群里直接渲染账号卡）


def test_start_group_unbound_is_entry_card_too(db):
    reply = handlers.handle_start(db, {"id": 900099, "first_name": "New"}, GROUP, "")
    assert isinstance(reply, tuple)
    text, _markup = reply
    assert "私密操作入口" in text


def test_start_private_bound_is_welcome_card(db):
    user = _user(db)
    reply = handlers.handle_start(db, _tg(), TG, "")
    assert isinstance(reply, tuple)
    text, _markup = reply
    assert text == handlers.main_card_text(_tg(), user)
    assert "用户编号" not in text


# ---------- /points 并入发言能量段；/chatpoints 别名不变 ----------

def test_points_include_chatpoints_section(db):
    _user(db)
    text = handlers.handle_points(db, _tg(), TG, "")
    assert "能量余额" in text
    assert "今日已得" in text
    assert "累计能量" in text
    assert "发言规则" in text


def test_chatpoints_alias_keeps_own_card(db, monkeypatch):
    _user(db)
    sent = _dispatch(db, monkeypatch, "/chatpoints", TG)
    assert sent
    assert "群发言能量" in sent[0][0]
    assert "今日已得" in sent[0][0]


# ---------- 二期：私聊用户菜单只留三条 ----------

def test_user_scope_menu_only_three():
    names = [n for n, _ in command_scopes.default_commands(poller.BOT_COMMANDS)]
    assert names == ["start", "help", "me"]  # 按 BOT_COMMANDS 注册顺序下发
    admin_names = [n for n, _ in command_scopes.admin_commands(poller.BOT_COMMANDS)]
    assert len(admin_names) == 27  # 管理员菜单维持全量 27 条


# ---------- 二期：/start 私聊面板按钮覆盖全部用户功能 ----------

# 功能 → 到达它的面板按钮（callback_data）。盘点口径：/start 私聊
# 面板（main_markup）→ 用户面板（user_panel_markup）→ 二级分组
# （user_panel_group_markup）整棵按钮树逐个对位用户侧命令清单；
# 手输命令保留可用不算数，必须有按钮路径。
PANEL_BUTTON_PATHS = {
    "用户面板/账号": "menu:user",
    "使用兑换码": "menu:renewcode",
    "签到": "menu:checkin",
    "能量": "menu:points",
    "快捷登录/续期": "menu:login",
    "绑定账号": "menu:bind",
    "抽奖": "menu:lottery",
    "我的抽奖": "menu:mylottery",
    "老虎机": "menu:slots",
    "红包": "menu:redpacket",
    "求片": "menu:request",
    "我的求片": "menu:myrequests",
    "求片榜": "menu:requestboard",
    "排行榜": "menu:rank",
    "网盘绑定": "menu:bindings",
    "转存进度": "menu:transfers",
    "播放诊断": "menu:whyplay",
    "我的想看": "menu:wants",
    "成就": "menu:achievements",
    "帮助": "menu:help",
}


def _panel_datas(markup):
    return {b.get("callback_data") for row in (markup or {}).get("inline_keyboard", [])
            for b in row if b.get("callback_data")}


def test_start_private_panel_covers_all_user_features(db):
    user = _user(db)
    _text, markup = handlers.handle_start(db, _tg(), TG, "")
    datas = _panel_datas(markup)
    datas |= _panel_datas(handlers.user_panel_markup())
    for group_key in handlers.USER_PANEL_GROUPS:
        datas |= _panel_datas(handlers.user_panel_group_markup(group_key))
    for feature, data in PANEL_BUTTON_PATHS.items():
        assert data in datas, f"{feature} 缺面板按钮 {data}"
