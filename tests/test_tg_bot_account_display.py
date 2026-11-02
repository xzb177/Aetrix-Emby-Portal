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
from backend.tg_bot import callbacks, ephemeral, features, handlers, poller, sender

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


class _FakeTimer:
    """假 Timer：只登记、永不真挂线程、永不到期。

    群菜单测试走 callbacks 面板编辑成功路径会调 ephemeral.rearm_delete
    → schedule_delete → 真 threading.Timer(30s)；真定时器会在之后别的
    测试的 sender 打补丁窗口内到期，把 deleteMessage(-100123, 5) 塞进
    别人的调用记录（slots 全量偶红实证，见仓库 AGENTS.md 2026-11-02
    条）。本文件所有测试一律把 ephemeral.Timer 换成这个假货，从源头
    堵泄漏；schedule_delete 的登记/顶替/取消语义照常走完。
    """

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
    # 群 /start 现返回 (入口卡, 群菜单) 元组：正文是「私密操作入口」
    # 简短卡（不展示任何个人账号数据），登录链接不进正文、也不进
    # 群菜单按钮（群里无一键登录行）。
    reply = handlers.handle_start(db, _tg(), GROUP, "")
    assert isinstance(reply, tuple)
    text, markup = reply
    assert "可用命令" not in text
    assert "私密操作入口" in text
    assert "能量余额" not in text and "用户编号" not in text  # 账号数据不铺群里
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
    # 成就：精简卡只出汇总行（总数 Y 只计 enabled），不再逐条铺成就
    assert "已解锁 1／共 2 个" in text
    assert "首夜" not in text  # 逐条行已砍，明细去网站账号页
    assert "追剧达人" not in text  # 未解锁不展示
    assert "隐藏" not in text  # disabled 定义不展示
    # 馆灵：120 小时 → 夜枭（50），下一阶段守夜者（200）差距并进同一行
    assert "夜枭" in text and "120.0" in text
    assert "守夜者" in text and "80.0" in text


def test_account_empty_achievements_and_pet_egg(db):
    _user(db)
    text = features.handle_account(db, _tg(), TG, "")
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
    assert "<b>成就进度</b> | 已解锁 1／共 2 个" in text
    assert "<b>订阅状态</b> | 暂无" in text  # 精简卡只出订阅汇总行


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


def test_account_and_me_render_account_card_in_group(db, monkeypatch):
    # 账号展示群里放行（2026-10-28 用户亲定、2026-11-02 10:43 重申）：
    # 群里输 /account、/me、/level 直接回发送者本人的账号卡；
    # #1705 曾一并改成私聊限定，用户纠正后恢复本口径。
    _user(db)
    for cmd in ("/account", "/me", "/level"):
        sent = _dispatch_group(db, monkeypatch, cmd)
        assert sent, cmd
        assert "能量余额" in sent[0], cmd  # 账号卡本体
        assert "私聊命令" not in sent[0], cmd


def test_login_still_blocked_in_group(db, monkeypatch):
    _user(db)
    sent = _dispatch_group(db, monkeypatch, "/login")
    assert sent and "请私聊" in sent[0]


def test_p1_still_blocked_in_group(db, monkeypatch):
    _user(db)
    sent = _dispatch_group(db, monkeypatch, "/bindings")
    assert sent and "私聊" in sent[0]


def test_menu_callback_account_renders_card_in_group(db, monkeypatch):
    # 群菜单「账号中心」按钮同口径：群里点到直接渲染点击者的账号卡
    # （2026-10-28 定、2026-11-02 重申），不再回私聊提示卡。
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
    assert edited and "能量余额" in edited[0]
    assert "私聊使用" not in edited[0]


# ---------- 注册表覆盖路由命令 ----------

def test_bot_commands_cover_router_commands():
    # 菜单简化后口径（2026-11-02）：BOT_COMMANDS 只收 27 条主命令；
    # router 命令集另含 4 条手输别名（account/renew/chatpoints/level），
    # 别名不在菜单但 router 照常认；菜单 ⊂ 路由命令全集。
    registered = {cmd for cmd, _desc in poller.BOT_COMMANDS}
    assert len(registered) == 27
    router_commands = {
        "start", "help", "bind", "checkin", "points", "account", "me",
        "request", "myrequests", "rank", "requestboard", "rules",
        "login", "renew", "redeem", "lottery", "redpacket", "slots",
        "bindings", "transfers", "whyplay", "wants", "chatpoints",
        "level", "report", "propose", "launch", "govlog",
        "reconcile", "rankpush", "whois",
    }
    aliases = {"account", "renew", "chatpoints", "level"}
    assert aliases <= router_commands
    assert registered | aliases == router_commands
    assert aliases.isdisjoint(registered)
    assert ("me", "[群/私聊] 账号中心") in poller.BOT_COMMANDS
