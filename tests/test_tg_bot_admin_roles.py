# -*- coding: utf-8 -*-
"""TG Bot 管理权限对齐网站三级角色（super/operator/viewer）＋面板补全＋群权限自检。

口径（用户 2026-11-07 亲点「按推荐全做」）：
- 网站管理员三级定义在 backend/admin_roles.py：viewer 只读（can_write
  为假）。机器人此前所有管理入口只查 is_staff，viewer 绑 TG 即可绕过
  网站只读限制去封号/赠送/取消资格/发红包雨/推榜——本单在执行层堵死：
  whois_panel.execute、reconcile 对账处置、handle_rain、handle_rankpush
  全部加角色门，手输命令与旧回调都拦得住；读口对 viewer 不变。
- operator 日常写全放行；whois 既有超管守卫（PA-056）不回归。
- 命令菜单按角色下发：viewer 只拿只读子集（不含 rankpush/rain）；
  绑定成功当场补发管理员菜单，不等 poller 重启。
- 管理面板显示真实角色名，按钮按角色裁剪；新增机器人群权限自检卡。
"""
from __future__ import annotations

import sys
from datetime import datetime, timedelta

import pytest

sys.path.insert(0, ".")

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend import models
from backend.emby_server import models as em
from backend.tg_bot import callbacks, command_scopes, handlers, poller, sender

VIEWER_TG = 710001
OPERATOR_TG = 710002
SUPER_TG = 710003
TARGET_TG = 710004
GROUP = -100710
PERMS_GROUP = -100999

READONLY_MARK = "只读审计"


@pytest.fixture()
def db():
    engine = create_engine("sqlite:///:memory:")
    models.Base.metadata.create_all(engine)
    em.Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


def _user(db, tg, name, staff=False, role=None, **kw):
    fields = {"password_hash": "x", "telegram_id": tg, "points": 10,
              "vitality": 14, "is_active": True, "is_staff": staff}
    if role is not None:
        fields["admin_role"] = role
    fields.update(kw)
    u = models.WebUser(username=name, **fields)
    db.add(u)
    db.commit()
    return u


def _staff(db):
    viewer = _user(db, VIEWER_TG, "viewer1", staff=True, role="viewer")
    operator = _user(db, OPERATOR_TG, "op1", staff=True, role="operator")
    target = _user(db, TARGET_TG, "targety")
    return viewer, operator, target


def _datas(markup):
    return [b.get("callback_data")
            for row in (markup or {}).get("inline_keyboard", [])
            for b in row if b.get("callback_data")]


def _cfg(db, key, value):
    db.add(models.SystemConfig(key=key, value=str(value)))
    db.commit()


# ---------- 一、whois 面板写操作：viewer 执行层被拒且零落账 ----------

def test_viewer_ban_via_execute_refused_zero_effect(db):
    from backend.tg_bot import whois_panel

    viewer, _op, target = _staff(db)
    note = whois_panel.execute(db, viewer, target, "ban")
    assert READONLY_MARK in note and "不能修改" in note
    db.refresh(target)
    assert target.is_active is True


def test_viewer_gift_via_execute_refused_zero_effect(db):
    from backend.tg_bot import whois_panel

    viewer, _op, target = _staff(db)
    before = int(target.points or 0)
    note = whois_panel.execute(db, viewer, target, "gp100")
    assert READONLY_MARK in note
    db.refresh(target)
    assert int(target.points or 0) == before
    assert db.query(models.PointsLog).count() == 0


def test_viewer_unban_and_welfare_gift_refused(db):
    from backend.tg_bot import whois_panel

    viewer, _op, target = _staff(db)
    target.is_active = False
    db.commit()
    note = whois_panel.execute(db, viewer, target, "unban")
    assert READONLY_MARK in note
    db.refresh(target)
    assert target.is_active is False
    note = whois_panel.execute(db, viewer, target, "gw30")
    assert READONLY_MARK in note
    db.refresh(target)
    assert not target.is_welfare


def test_operator_ban_and_gift_via_execute_allowed(db):
    from backend.tg_bot import whois_panel

    _viewer, operator, target = _staff(db)
    note = whois_panel.execute(db, operator, target, "ban")
    assert "已封禁" in note
    db.refresh(target)
    assert target.is_active is False
    # 赠送走 ask 确认页拿一次性 nonce 后执行（PA-058 口径不变）
    _text, markup = whois_panel.confirm_page(db, target, "gp100")
    do_data = [d for d in _datas(markup)
               if d.startswith(f"whois:do:{target.id}:gp100")][0]
    nonce = do_data.split(":")[-1]
    before = int(target.points or 0)
    note = whois_panel.execute(db, operator, target, "gp100", nonce=nonce)
    assert "已给" in note or "赠送 100" in note
    db.refresh(target)
    assert int(target.points or 0) == before + 100


def test_operator_cannot_ban_other_admin_guard_kept(db):
    from backend.tg_bot import whois_panel

    _viewer, operator, _target = _staff(db)
    other_op = _user(db, 710005, "op2", staff=True, role="operator")
    note = whois_panel.execute(db, operator, other_op, "ban")
    assert "超级管理员" in note
    db.refresh(other_op)
    assert other_op.is_active is True


# ---------- 一、对账处置：viewer 经 reconcile:do 被拒零处置 ----------

def _reconcile_seed(db):
    viewer, operator, target = _staff(db)
    target.is_welfare = True
    target.welfare_expires_at = datetime.now() + timedelta(days=30)
    db.commit()
    _cfg(db, "bot_group_ids", str(GROUP))
    return viewer, operator, target


def test_viewer_reconcile_execute_refused_zero_effect(db):
    from backend.tg_bot import reconcile

    viewer, _op, target = _reconcile_seed(db)
    result = reconcile.ReconcileResult(
        groups=[GROUP], welfare_targets=[target])
    done, _skipped = reconcile.execute_cancel_welfare(
        db, viewer, result, checker=lambda *a: (False, "left"))
    assert done == []
    db.refresh(target)
    assert target.is_welfare is True
    assert db.query(models.WelfareGrantLog).count() == 0


def test_operator_reconcile_execute_allowed(db):
    from backend.tg_bot import reconcile

    _viewer, operator, target = _reconcile_seed(db)
    result = reconcile.ReconcileResult(
        groups=[GROUP], welfare_targets=[target])
    done, _skipped = reconcile.execute_cancel_welfare(
        db, operator, result, checker=lambda *a: (False, "left"))
    assert [u.id for u in done] == [target.id]
    db.refresh(target)
    assert target.is_welfare is False


def test_viewer_reconcile_do_callback_refused(db, monkeypatch):
    from backend.tg_bot import reconcile

    viewer, _op, target = _reconcile_seed(db)
    monkeypatch.setattr(sender, "get_chat_member",
                        lambda *a: (False, "left"))
    monkeypatch.setattr(sender, "answer_callback_query",
                        lambda *a, **kw: (True, None))
    edited = []
    monkeypatch.setattr(
        sender, "edit_panel",
        lambda db_, cid, mid, text, markup=None, **kw:
            edited.append(text) or (True, None))
    # 先经命令出报告并缓存结果（读口对 viewer 放行）
    handlers.handle_reconcile(db, {"id": VIEWER_TG}, VIEWER_TG, "")
    assert reconcile.cached_result(VIEWER_TG) is not None
    cq = {"id": "cq9", "data": "reconcile:do",
          "from": {"id": VIEWER_TG, "first_name": "V"},
          "message": {"message_id": 7, "chat": {"id": VIEWER_TG,
                                                "type": "private"}}}
    callbacks.handle_callback(db, cq)
    assert edited and READONLY_MARK in edited[0]
    db.refresh(target)
    assert target.is_welfare is True


# ---------- 一、红包雨 / 推榜：viewer 被拒、operator 放行 ----------

def test_viewer_rain_refused(db, monkeypatch):
    from backend import rain as rain_svc

    viewer, _op, _t = _staff(db)
    _cfg(db, "bot_rain_enabled", "true")
    called = []
    monkeypatch.setattr(rain_svc, "start_rain",
                        lambda *a, **kw: called.append(1) or {"ok": False})
    reply = handlers.handle_rain(
        db, {"id": VIEWER_TG}, GROUP, "", in_group=True)
    assert isinstance(reply, str) and READONLY_MARK in reply
    assert called == []
    assert db.query(models.RedPacketRain).count() == 0


def test_operator_rain_reaches_service(db, monkeypatch):
    from backend import rain as rain_svc

    _viewer, operator, _t = _staff(db)
    _cfg(db, "bot_rain_enabled", "true")
    monkeypatch.setattr(rain_svc, "start_rain",
                        lambda *a, **kw: {"ok": False, "reason": "busy"})
    reply = handlers.handle_rain(
        db, {"id": OPERATOR_TG}, GROUP, "", in_group=True)
    assert reply == rain_svc.reason_message("busy")


def test_viewer_rankpush_refused_push_not_called(db, monkeypatch):
    from backend.tg_bot import rankpush

    viewer, _op, _t = _staff(db)
    called = []
    monkeypatch.setattr(rankpush, "push_rank_board",
                        lambda *a, **kw: called.append(1) or {"pushed": True})
    text = handlers.handle_rankpush(db, {"id": VIEWER_TG}, VIEWER_TG, "")
    assert READONLY_MARK in text
    assert called == []


def test_operator_rankpush_reaches_service(db, monkeypatch):
    from backend.tg_bot import rankpush

    _viewer, operator, _t = _staff(db)
    called = []
    monkeypatch.setattr(
        rankpush, "push_rank_board",
        lambda *a, **kw: called.append(1) or
        {"pushed": False, "reason": "empty", "rows": []})
    text = handlers.handle_rankpush(db, {"id": OPERATOR_TG}, OPERATOR_TG, "")
    assert called == [1]
    assert "没人观影" in text


# ---------- 一、命令菜单按角色下发 ----------

def _capture_scopes(db, monkeypatch):
    calls = []

    def fake_set(db_, commands, scope=None):
        calls.append((list(commands), scope))
        return (True, None)

    monkeypatch.setattr(sender, "set_my_commands", fake_set)
    return calls


def test_viewer_menu_excludes_write_commands(db, monkeypatch):
    _user(db, VIEWER_TG, "viewer1", staff=True, role="viewer")
    calls = _capture_scopes(db, monkeypatch)
    command_scopes.register_command_scopes(db, poller.BOT_COMMANDS)
    mine = [cmds for cmds, scope in calls
            if (scope or {}).get("chat_id") == VIEWER_TG]
    assert mine, "viewer 管理员应有单独下发"
    names = [n for n, _d in mine[0]]
    assert "whois" in names and "reconcile" in names and "govlog" in names
    assert "rankpush" not in names and "rain" not in names


def test_operator_menu_gets_full_list(db, monkeypatch):
    _user(db, OPERATOR_TG, "op1", staff=True, role="operator")
    calls = _capture_scopes(db, monkeypatch)
    command_scopes.register_command_scopes(db, poller.BOT_COMMANDS)
    mine = [cmds for cmds, scope in calls
            if (scope or {}).get("chat_id") == OPERATOR_TG]
    assert mine
    assert [n for n, _d in mine[0]] == [n for n, _d in poller.BOT_COMMANDS]


# ---------- 一、绑定成功当场补发管理员菜单 ----------

def _bind_staff(db, monkeypatch, tg, name, role, code_text):
    user = _user(db, None, name, staff=True, role=role)
    user.telegram_id = None
    db.commit()
    db.add(models.TgBindCode(
        code=code_text, user_id=user.id,
        expires_at=datetime.now() + timedelta(minutes=10)))
    db.commit()
    calls = _capture_scopes(db, monkeypatch)
    text = handlers.handle_bind(db, {"id": tg}, tg, code_text)
    assert "绑定成功" in text
    mine = [cmds for cmds, scope in calls
            if (scope or {}).get("chat_id") == tg]
    return mine


def test_bind_operator_gets_admin_scope_immediately(db, monkeypatch):
    mine = _bind_staff(db, monkeypatch, OPERATOR_TG, "opbind", "operator",
                       "810001")
    assert mine, "绑定成功后应立即补发管理员 scope"
    names = [n for n, _d in mine[0]]
    assert "rankpush" in names and "whois" in names


def test_bind_viewer_gets_readonly_scope_immediately(db, monkeypatch):
    mine = _bind_staff(db, monkeypatch, VIEWER_TG, "vbind", "viewer",
                       "810002")
    assert mine, "绑定成功后应立即补发管理员 scope"
    names = [n for n, _d in mine[0]]
    assert "whois" in names and "rankpush" not in names


# ---------- 二、管理面板：真实角色 + 按角色裁剪按钮 ----------

def test_handle_admin_shows_real_role_label(db):
    _staff(db)
    text = handlers.handle_admin(db, {"id": OPERATOR_TG}, OPERATOR_TG, "")
    assert "运营" in text and "网站管理员" not in text
    text = handlers.handle_admin(db, {"id": VIEWER_TG}, VIEWER_TG, "")
    assert "只读审计" in text


def test_admin_markup_by_role(db):
    viewer, operator, _t = _staff(db)
    v_datas = _datas(handlers.admin_markup(viewer))
    assert "menu:admin_whois" in v_datas
    assert "menu:admin_reconcile" in v_datas
    assert "menu:admin_botperms" in v_datas
    assert "menu:server" in v_datas and "menu:govlog" in v_datas
    assert "menu:admin_rankpush" not in v_datas
    assert "menu:admin_rain_info" not in v_datas
    o_datas = _datas(handlers.admin_markup(operator))
    assert "menu:admin_rankpush" in o_datas
    assert "menu:admin_rain_info" in o_datas
    assert "menu:admin_botperms" in o_datas


def test_admin_panel_render_and_rankpush_choice_card(db):
    _staff(db)
    text, markup = callbacks._render_menu(
        db, {"id": OPERATOR_TG}, OPERATOR_TG, OPERATOR_TG, True, "admin")
    assert "管理面板" in text and "运营" in text
    assert "menu:admin_whois" in _datas(markup)
    text, markup = callbacks._render_menu(
        db, {"id": OPERATOR_TG}, OPERATOR_TG, OPERATOR_TG, True,
        "admin_rankpush")
    assert "menu:admin_rankpush_day" in _datas(markup)
    assert "menu:admin_rankpush_week" in _datas(markup)


def test_viewer_rankpush_button_execution_blocked(db, monkeypatch):
    from backend.tg_bot import rankpush

    _staff(db)
    called = []
    monkeypatch.setattr(rankpush, "push_rank_board",
                        lambda *a, **kw: called.append(1) or {"pushed": True})
    text, _markup = callbacks._render_menu(
        db, {"id": VIEWER_TG}, VIEWER_TG, VIEWER_TG, True,
        "admin_rankpush_day")
    assert READONLY_MARK in text
    assert called == []


# ---------- 三、机器人群权限自检卡：三种形态 ----------

def _perms_db(db):
    _cfg(db, "bot_group_ids", str(PERMS_GROUP))


def test_bot_perms_card_admin_full(db, monkeypatch):
    from backend.tg_bot import bot_perms

    _perms_db(db)
    monkeypatch.setattr(bot_perms.sender, "get_me",
                        lambda db_: (True, None, {"id": 4242}))
    monkeypatch.setattr(
        bot_perms.sender, "get_chat_member_raw",
        lambda db_, cid, uid: (True, None, {
            "status": "administrator",
            "can_delete_messages": True, "can_pin_messages": True}))
    text = bot_perms.render_card(db)
    assert str(PERMS_GROUP) in text
    assert "群管理员" in text and "✅" in text
    assert "删不掉" not in text


def test_bot_perms_card_plain_member_warns(db, monkeypatch):
    from backend.tg_bot import bot_perms

    _perms_db(db)
    monkeypatch.setattr(bot_perms.sender, "get_me",
                        lambda db_: (True, None, {"id": 4242}))
    monkeypatch.setattr(
        bot_perms.sender, "get_chat_member_raw",
        lambda db_, cid, uid: (True, None, {"status": "member"}))
    text = bot_perms.render_card(db)
    assert "普通成员" in text
    assert "频道马甲消息删不掉" in text
    assert "请在群设置里把机器人设为管理员" in text


def test_bot_perms_card_query_failure_honest(db, monkeypatch):
    from backend.tg_bot import bot_perms

    _perms_db(db)
    monkeypatch.setattr(bot_perms.sender, "get_me",
                        lambda db_: (True, None, {"id": 4242}))
    monkeypatch.setattr(
        bot_perms.sender, "get_chat_member_raw",
        lambda db_, cid, uid: (False, "boom", None))
    text = bot_perms.render_card(db)
    assert "查询失败" in text


# ---------- 三、频道马甲删消息失败留 warning 痕 ----------

def test_channel_mask_delete_failure_logs_warning(db, monkeypatch, caplog):
    import logging

    from backend.tg_bot import ephemeral, policy, router

    _cfg(db, "bot_group_ids", str(GROUP))
    monkeypatch.setattr(sender, "delete_message",
                        lambda *a, **kw: (False, "message can't be deleted"))
    monkeypatch.setattr(sender, "send_message_ex",
                        lambda *a, **kw: (True, None, 9001))
    monkeypatch.setattr(ephemeral, "schedule_delete", lambda *a, **kw: True)
    router._group_windows.clear()
    msg = {
        "message_id": 42,
        "from": {"id": TARGET_TG, "first_name": "Fan"},
        "sender_chat": {"id": -100999888, "type": "channel", "title": "某频道"},
        "chat": {"id": GROUP, "type": "supergroup"},
        "text": "hi",
    }
    cfg = policy.read_bot_config(db)
    with caplog.at_level(logging.WARNING, logger="backend.tg_bot.router"):
        assert router._intercept_channel_mask(db, cfg, msg) is True
    hits = [r for r in caplog.records
            if r.levelno >= logging.WARNING and str(GROUP) in r.getMessage()]
    assert hits, "删消息失败必须留 warning（带 chat_id 与错误）"
