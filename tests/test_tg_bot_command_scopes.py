"""TG Bot 命令三级下发（BotCommandScope）：分类表 + 分级注册测试。

口径（与 backend/tg_bot/command_scopes.py 一致，parent 按表核对）：
- default：二期面板优先口径，只下发 start/me/help 三条入口命令
- 管理员私聊 scope（BotCommandScopeChat=管理员 TG 号）：全量
- 群 scope（BotCommandScopeChat=名单群 id）：只含 router 现行群内可执行命令
scope 只决定菜单可见性，不是安全边界：实际门禁仍在 router/handler。
"""
from __future__ import annotations

import sys

import pytest

sys.path.insert(0, ".")

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend import models
from backend.tg_bot import command_scopes, poller, sender


@pytest.fixture()
def db():
    engine = create_engine("sqlite:///:memory:")
    models.WebUser.__table__.create(engine, checkfirst=True)
    models.SystemConfig.__table__.create(engine, checkfirst=True)
    Session = sessionmaker(bind=engine)
    session = Session()
    yield session
    session.close()


def _names(commands):
    return [name for name, _desc in commands]


def _make_user(db, username, tg_id=None, is_staff=False, is_active=True):
    u = models.WebUser(
        username=username, telegram_id=tg_id, is_staff=is_staff, is_active=is_active
    )
    if hasattr(u, "password_hash") and getattr(u, "password_hash") is None:
        try:
            u.password_hash = ""
        except Exception:
            pass
    db.add(u)
    db.commit()
    return u


def _set_config(db, key, value):
    db.add(models.SystemConfig(key=key, value=value))
    db.commit()


# ---------- 分类表：覆盖 / 互斥 ----------

def test_default_scope_is_three_entry_commands():
    # 二期面板优先口径：default 菜单只留 start/me/help 三条入口命令；
    # 其余命令（含管理员级）只在管理员 scope 全量里，靠手输触发。
    all_names = set(_names(poller.BOT_COMMANDS))
    default_names = set(_names(command_scopes.default_commands(poller.BOT_COMMANDS)))
    assert default_names == {"start", "me", "help"}
    assert default_names <= all_names
    admin_names = set(_names(command_scopes.admin_commands(poller.BOT_COMMANDS)))
    assert admin_names == all_names  # 管理员 scope 仍全量覆盖


def test_admin_commands_never_in_default():
    default_names = set(_names(command_scopes.default_commands(poller.BOT_COMMANDS)))
    for name in command_scopes.ADMIN_ONLY_COMMANDS:
        assert name not in default_names
    # 现行基线里真实存在的管理员级命令：/govlog（治理审计，Lv.6 门禁）
    assert "govlog" in command_scopes.ADMIN_ONLY_COMMANDS
    assert "govlog" not in default_names


def test_admin_scope_gets_full_list():
    full = _names(command_scopes.admin_commands(poller.BOT_COMMANDS))
    assert full == _names(poller.BOT_COMMANDS)


def test_group_scope_only_group_executable():
    group_names = _names(command_scopes.group_commands(poller.BOT_COMMANDS))
    group_set = set(group_names)
    # router private_only：群里强制私聊，绝不进群菜单
    for name in ("request", "myrequests", "login", "renew", "govlog",
                 "report", "propose", "launch"):
        assert name not in group_set
    # 账号展示一家群里放行（2026-10-28 用户亲定、2026-11-02 重申）：
    # /me 在 BOT_COMMANDS 里、要进群菜单；/account、/level 是手输别名
    # 不在菜单表，但 GROUP_COMMANDS 集合口径要含（与 router 一致）
    assert "me" in group_set
    for name in ("account", "level"):
        assert name in command_scopes.GROUP_COMMANDS
        assert name not in group_set
    # P1 个人查询：群里不执行
    for name in ("bindings", "transfers", "whyplay", "wants"):
        assert name not in group_set
    # /redeem 群里拒执行（删消息+提示私聊），不进群菜单
    assert "redeem" not in group_set
    # 群内确实可执行的要在群菜单里（chatpoints 已并入 points，不单列）
    for name in ("checkin", "redpacket", "lottery", "rank", "requestboard",
                 "rules", "slots", "points"):
        assert name in group_set
    # 群菜单是全量的子集
    assert group_set <= set(_names(poller.BOT_COMMANDS))


# ---------- sender：scope 进 payload ----------

def test_set_my_commands_with_scope_payload(db, monkeypatch):
    calls = []
    monkeypatch.setattr(
        sender, "_call",
        lambda db_, method, payload: calls.append((method, payload)) or (True, None))
    scope = {"type": "chat", "chat_id": 424242}
    sender.set_my_commands(db, [("start", "欢迎")], scope=scope)
    method, payload = calls[0]
    assert method == "setMyCommands"
    assert payload["scope"] == scope
    assert payload["commands"] == [{"command": "start", "description": "欢迎"}]


def test_set_my_commands_without_scope_omits_key(db, monkeypatch):
    calls = []
    monkeypatch.setattr(
        sender, "_call",
        lambda db_, method, payload: calls.append((method, payload)) or (True, None))
    sender.set_my_commands(db, [("start", "欢迎")])
    _method, payload = calls[0]
    assert "scope" not in payload


# ---------- 分级注册：参数正确 + 失败隔离 ----------

def _capture(monkeypatch, fail_chat_ids=()):
    calls = []

    def fake_set(db_, commands, scope=None):
        calls.append((list(commands), scope))
        chat_id = (scope or {}).get("chat_id")
        if chat_id in fail_chat_ids:
            raise RuntimeError("boom")
        return (True, None)

    monkeypatch.setattr(sender, "set_my_commands", fake_set)
    return calls


def test_register_scopes_targets_and_payloads(db, monkeypatch):
    _make_user(db, "boss", tg_id=910001, is_staff=True)
    _make_user(db, "staff_no_tg", tg_id=None, is_staff=True)  # 没绑 TG：跳过
    _make_user(db, "plain", tg_id=910002, is_staff=False)      # 普通用户：不单独下发
    _set_config(db, "bot_group_ids", "-100111, -100222")
    calls = _capture(monkeypatch)

    summary = command_scopes.register_command_scopes(db, poller.BOT_COMMANDS)

    by_scope = {}
    for commands, scope in calls:
        by_scope[(scope or {}).get("type"), (scope or {}).get("chat_id")] = commands
    # default：无 scope，普通集（不含 govlog）
    default_cmds = by_scope[(None, None)]
    assert "govlog" not in _names(default_cmds)
    assert "start" in _names(default_cmds)
    # 管理员私聊：scope=chat/管理员 TG 号，全量（含 govlog）
    admin_cmds = by_scope[("chat", 910001)]
    assert _names(admin_cmds) == _names(poller.BOT_COMMANDS)
    # 没绑 TG 的管理员与普通用户不产生单独下发
    assert ("chat", 910002) not in by_scope
    assert len([k for k in by_scope if k[0] == "chat" and k[1] and k[1] > 0]) == 1
    # 名单群：scope=chat/群 id，群集（不含 login 等私聊命令）
    for gid in (-100111, -100222):
        group_cmds = by_scope[("chat", gid)]
        assert "checkin" in _names(group_cmds)
        assert "login" not in _names(group_cmds)
        assert "govlog" not in _names(group_cmds)
    assert summary["default"] is True
    assert summary["staff"] == {910001: True}
    assert summary["groups"] == {-100111: True, -100222: True}


def test_register_one_scope_failure_does_not_block_others(db, monkeypatch):
    _make_user(db, "boss", tg_id=910001, is_staff=True)
    _set_config(db, "bot_group_ids", "-100111")
    calls = _capture(monkeypatch, fail_chat_ids={910001})

    summary = command_scopes.register_command_scopes(db, poller.BOT_COMMANDS)

    scopes = [(scope or {}).get("chat_id") for _cmds, scope in calls]
    assert None in scopes          # default 照常下发（scope=None 的那次）
    assert -100111 in scopes       # 群 scope 不受管理员失败影响
    assert summary["staff"] == {910001: False}
    assert summary["groups"] == {-100111: True}
    assert summary["default"] is True
