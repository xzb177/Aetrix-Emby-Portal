# -*- coding: utf-8 -*-
"""TG Bot 命令菜单三级下发（BotCommandScope）：普通 / 管理员 / 群。

现状问题：poller 启动时一条 setMyCommands 把全部命令一刀切下发给所有人，
普通用户在菜单里就能看到管理员级命令（点了会被门禁拒绝，但菜单先暴露了）。

本模块按 Telegram 的 BotCommandScope 分三级下发：
- default scope（不带 scope 参数）：普通用户集 = BOT_COMMANDS 去掉管理员级；
- 管理员 scope（BotCommandScopeChat，chat_id=管理员 TG 号）：全量下发，
  管理员名单 = web_users 里 is_staff 且在用且已绑 telegram_id（与
  features._notify_staff_about_request 的管理员口径一致，不新造名单配置）；
- 群 scope（BotCommandScopeChat，chat_id=名单群 id）：只含 router 现行
  群内可执行命令（名单 = policy 的 bot_group_ids）。

重要：scope 只决定「菜单里看不看得见」，不是安全边界。任何命令的实际
权限判定仍在 router/handler（私聊限定、P1 拦截、Lv.6 门禁等一字未动），
菜单里看不见的命令手动输入照样会被现行门禁拦下。

限制：下发只在 poller 启动时做一次；新管理员绑定 TG 后要等 poller 重启
才拿到管理员菜单（其命令权限本身不受影响，门禁认的是账号 is_staff）。
"""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

# 管理员级命令（不带斜杠）：绝不进 default 菜单，只随管理员 scope 全量下发。
# - govlog：现行命令，治理审计日志查看（features.handle_govlog：仅私聊、
#   仅公益服、仅 Lv.6），属特权查看，按更严一级归管理员级；
# - whois / reconcile / rankpush：管理员命令（审计反查 / 群号对账 /
#   观影榜手动推送），其实现在独立草稿 PR 中，尚未合入 BOT_COMMANDS；
#   在此预注册分类——它们进入 BOT_COMMANDS 之日自动归管理员级，
#   在此之前本模块只从 BOT_COMMANDS 取交集下发，不会下发不存在的命令。
ADMIN_ONLY_COMMANDS: frozenset[str] = frozenset({
    "govlog",
    "whois",
    "reconcile",
    "rankpush",
})

# 群内可执行命令（不带斜杠）：逐条对 router.dispatch 的现行群内拦截口径——
# 不在 private_only、不在 _P1_COMMANDS、且不被群内专门拒执行。
# 排除项（13)：private_only 8 (request/myrequests/login/renew/govlog/
# report/propose/launch) + P1 私人查询 4 (bindings/transfers/whyplay/
# wants) + redeem（群内拒执行：删原消息并提示私聊）。
# slots：/slots 命令群里出公共面板（不带个人数据，绑定点注时才判），
# 与 menu:slots 面板入口同口径，属群内可执行。
GROUP_COMMANDS: frozenset[str] = frozenset({
    "start", "help", "bind", "checkin", "points", "chatpoints",
    "account", "me", "rank", "level", "requestboard", "rules",
    "redpacket", "lottery", "slots",
})


def default_commands(commands: list[tuple[str, str]]) -> list[tuple[str, str]]:
    """default scope 命令集：全量去掉管理员级。"""
    return [c for c in commands if c[0] not in ADMIN_ONLY_COMMANDS]


def admin_commands(commands: list[tuple[str, str]]) -> list[tuple[str, str]]:
    """管理员 scope 命令集：全量（含管理员级）。"""
    return list(commands)


def group_commands(commands: list[tuple[str, str]]) -> list[tuple[str, str]]:
    """群 scope 命令集：只含群内可执行命令（保持 BOT_COMMANDS 原顺序）。"""
    return [c for c in commands if c[0] in GROUP_COMMANDS]


def admin_only_names(commands: list[tuple[str, str]]) -> list[str]:
    """给定命令表里实际存在的管理员级命令名（按命令表顺序）。"""
    return [name for name, _desc in commands if name in ADMIN_ONLY_COMMANDS]


def staff_chat_ids(db) -> list[int]:
    """管理员 TG 号名单：is_staff + 在用 + 已绑 telegram_id（现行口径）。"""
    from backend import models

    rows = (
        db.query(models.WebUser.telegram_id)
        .filter(
            models.WebUser.is_staff.is_(True),
            models.WebUser.is_active.is_(True),
            models.WebUser.telegram_id.isnot(None),
        )
        .all()
    )
    result: list[int] = []
    for (tg_id,) in rows:
        try:
            result.append(int(tg_id))
        except (TypeError, ValueError):
            continue
    return result


def _deliver(db, sender, commands, scope) -> bool:
    """单级下发：失败（返回 False 或抛异常）只记日志，返回 False。"""
    try:
        ok, err = sender.set_my_commands(db, commands, scope=scope)
    except Exception as exc:  # noqa: BLE001
        logger.warning("tg setMyCommands scope=%s raised: %s", scope, exc)
        return False
    if not ok:
        logger.warning("tg setMyCommands scope=%s failed: %s", scope, err)
        return False
    return True


def register_command_scopes(db, commands: list[tuple[str, str]]) -> dict:
    """三级下发全部命令菜单；单级失败不影响其他级与调用方（poller 启动）。

    :return: {"default": bool, "staff": {tg_id: bool}, "groups": {gid: bool}}
    """
    from backend.tg_bot import policy, sender

    summary: dict = {"default": False, "staff": {}, "groups": {}}

    summary["default"] = _deliver(db, sender, default_commands(commands), None)

    for tg_id in staff_chat_ids(db):
        scope = {"type": "chat", "chat_id": tg_id}
        summary["staff"][tg_id] = _deliver(
            db, sender, admin_commands(commands), scope)

    try:
        cfg = policy.read_bot_config(db)
        group_ids = sorted(policy.parse_group_ids(cfg.get("bot_group_ids", "")))
    except Exception as exc:  # noqa: BLE001
        logger.warning("tg command scopes: read group ids failed: %s", exc)
        group_ids = []
    group_cmds = group_commands(commands)
    for gid in group_ids:
        scope = {"type": "chat", "chat_id": gid}
        summary["groups"][gid] = _deliver(db, sender, group_cmds, scope)

    return summary
