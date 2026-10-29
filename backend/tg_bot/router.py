"""Telegram 消息路由：把 getUpdates 的单个 update 分发到命令处理器。

B4 策略（全部可配置，见 backend/tg_bot/policy.py）：
- 总开关 bot_enabled：关闭后静默丢弃所有 update（poller 继续跑）
- 群白名单 bot_group_ids：非名单群聊的消息/callback 静默忽略，私聊不受影响
- 命令开关：关闭的命令回复"该功能已关闭"（/start /help 常开）
- 限流：每用户每命令间隔 bot_rate_limit_seconds 秒；每群每分钟最多
  bot_group_rate_limit 条；超限静默丢弃（用户级限流仍提示一句话）
"""
from __future__ import annotations

import logging
import threading
import time
from collections import deque

from backend.tg_bot import policy

logger = logging.getLogger(__name__)

# 命令级限流：(tg_user_id, cmd) -> 上次通过时间的 monotonic 时间戳
_user_cmd_limits: dict[tuple[int, str], float] = {}
# 群级限流：chat_id -> 最近 60 秒内已处理消息的时间戳滑动窗口
_group_windows: dict[int, deque[float]] = {}
_rate_lock = threading.Lock()

# 内存结构上限：超过后触发一次清理，防止长期运行泄漏
_MAX_TRACKED_KEYS = 10000
# 清理时保留多久的记录（单调时钟秒）；远大于任何合理限流窗口即可
_RETENTION_SECONDS = 3600.0
_GROUP_WINDOW_SECONDS = 60.0

_GROUP_CHAT_TYPES = ("group", "supergroup")


def _is_group_chat(chat: dict) -> bool:
    """是否为群聊（group/supergroup）；type 缺失或未知时按私聊处理（不拦截）。"""
    return chat.get("type") in _GROUP_CHAT_TYPES


def _user_cmd_allow(tg_user_id: int, cmd: str, seconds: float) -> bool:
    """用户级命令限流：距离上次通过不足 seconds 秒则返回 False。"""
    now = time.monotonic()
    with _rate_lock:
        last = _user_cmd_limits.get((tg_user_id, cmd))
        if last is not None and now - last < seconds:
            return False
        _user_cmd_limits[(tg_user_id, cmd)] = now
        if len(_user_cmd_limits) > _MAX_TRACKED_KEYS:
            cutoff = now - max(seconds, _RETENTION_SECONDS)
            for k, v in list(_user_cmd_limits.items()):
                if v < cutoff:
                    del _user_cmd_limits[k]
        return True


def _group_allow(chat_id: int, per_minute: int) -> bool:
    """群级滑动窗口限流：60 秒内已处理数达到上限则返回 False（静默丢弃）。"""
    now = time.monotonic()
    window_start = now - _GROUP_WINDOW_SECONDS
    with _rate_lock:
        dq = _group_windows.get(chat_id)
        if dq is None:
            dq = deque()
            _group_windows[chat_id] = dq
        while dq and dq[0] < window_start:
            dq.popleft()
        if len(dq) >= per_minute:
            return False
        dq.append(now)
        if len(_group_windows) > _MAX_TRACKED_KEYS:
            for cid, old_dq in list(_group_windows.items()):
                while old_dq and old_dq[0] < window_start:
                    old_dq.popleft()
                if not old_dq:
                    del _group_windows[cid]
        return True


def _verify_bind_code(db, update: dict) -> None:
    """验证网页端发起的绑定码（流程 A）：纯 6 位数字消息走共享验证函数。"""
    from backend.tg_bot import handlers, sender

    msg = update.get("message") or {}
    text = (msg.get("text") or "").strip()
    tg_user = msg.get("from") or {}
    tg_user_id = tg_user.get("id")
    chat_id = (msg.get("chat") or {}).get("id")
    if not tg_user_id or not chat_id:
        return
    reply = handlers.verify_bind_code(db, tg_user_id, chat_id, text)
    if reply:
        sender.send_message(db, chat_id, reply)
    # reply 为 None → 不是有效的绑定码，静默忽略


def _award_group_chat_points(db, tg_user: dict) -> None:
    """群发言奖励：已绑定账号的发送者按发言计分。

    直接进程内调用 points.award_chat_points（与发表影评同款），
    走 points_chat_* 默认口径（1 分/条、每天 20 分封顶）。
    未绑定发送者静默跳过；发分失败只记日志，绝不中断消息处理。
    """
    tg_user_id = (tg_user or {}).get("id")
    if not tg_user_id:
        return
    try:
        from backend import points as points_mod
        from backend.tg_bot.identity import resolve

        user = resolve(db, int(tg_user_id))
        if user is None:
            return
        points_mod.award_chat_points(db, user.id)
    except Exception:
        logger.exception("group chat points award failed")


# P1 自助查询命令：个人数据，群聊里不执行（handler 层另有私聊 guard 双保险）
_P1_COMMANDS = ("/bindings", "/transfers", "/whyplay", "/wants")


def dispatch(db, update: dict) -> None:
    """分发单个 update 到对应的处理器。

    P0：处理器抛异常时不再只记日志——尽力给用户回一句看得见的失败提示
    （2026-10-21 教训：失败只记日志、用户端看着像成功）。
    """
    from backend.tg_bot import handlers, sender

    # 先取 chat_id：异常发生在任何一步时失败提示都有地方发
    if "callback_query" in update:
        _cq = update["callback_query"] or {}
        chat_id = ((_cq.get("message") or {}).get("chat") or {}).get("id")
    else:
        chat_id = ((update.get("message") or {}).get("chat") or {}).get("id")

    try:
        # B4：批量读取全部 Bot 策略配置（1 次 DB 查询）
        cfg = policy.read_bot_config(db)

        # 总开关：关闭后完全不响应（poller 继续跑，offset 照常推进）
        if not policy.is_enabled(cfg):
            return

        # callback_query 走 B3（群白名单同样适用：在非名单群点的抢红包按钮静默忽略）
        if "callback_query" in update:
            cq = update["callback_query"]
            chat = ((cq.get("message") or {}).get("chat") or {})
            if _is_group_chat(chat) and not policy.is_group_allowed(cfg, chat.get("id")):
                return
            from backend.tg_bot import callbacks
            callbacks.handle_callback(
                db, cq, redpacket_enabled=policy.is_redpacket_enabled(cfg)
            )
            return

        msg = update.get("message") or {}
        text = (msg.get("text") or "").strip()
        if not text:
            return
        chat = msg.get("chat") or {}
        chat_id = chat.get("id")
        tg_user = msg.get("from") or {}
        if not chat_id:
            return
        in_group = _is_group_chat(chat)

        # 群白名单：非名单群聊静默忽略（私聊不受影响）
        if in_group and not policy.is_group_allowed(cfg, chat_id):
            return

        group_limit = policy.group_rate_limit(cfg)

        # 6 位纯数字 → 绑定码验证
        if text.isdigit() and len(text) == 6:
            if in_group and not _group_allow(chat_id, group_limit):
                return
            _verify_bind_code(db, update)
            return

        # 口令抽奖：群内非命令文本先尝试匹配口令关键词
        # 注意：此处不消耗群级限流预算（普通聊天不计入），防刷由 handler 内部的
        # 幂等逻辑保证（重复口令静默，已参加/资格不符均不刷屏）。
        # （2026-10-28 接回群发言奖励：口令未命中后按发言计分，见 _award_group_chat_points；
        # 群白名单已在上方 is_group_allowed 处拦截，未放行群到不了这里；
        # 普通聊天不消耗 _group_allow 群级限流预算，保持原有口径。）
        if in_group and not text.startswith("/"):
            from backend.tg_bot import callbacks

            try:
                if callbacks.handle_lottery_password(db, chat_id, tg_user, text):
                    return
            except Exception:
                logger.exception("lottery password dispatch failed")
                return
            _award_group_chat_points(db, tg_user)
            return

        # 非命令文本忽略
        if not text.startswith("/"):
            return

        # 解析命令（去掉 @bot 后缀）
        first_token = text.split()[0]
        cmd = first_token.split("@")[0].lower()
        args = text[len(first_token):].strip()

        commands = {
            "/start": handlers.handle_start,
            "/help": handlers.handle_help,
            "/bind": handlers.handle_bind,
            "/checkin": handlers.handle_checkin,
            "/points": handlers.handle_points,
            "/account": handlers.handle_account,
            "/me": handlers.handle_account,
            "/request": handlers.handle_request,
            "/myrequests": handlers.handle_my_requests,
            "/rank": handlers.handle_rank,
            "/govlog": handlers.handle_govlog,
            "/level": handlers.handle_level,
            "/report": handlers.handle_report,
            "/propose": handlers.handle_propose,
            "/launch": handlers.handle_direct_launch,
            "/requestboard": handlers.handle_request_board,
            "/rules": handlers.handle_rules,
            "/login": handlers.handle_login,
            "/renew": handlers.handle_login,
            "/redeem": handlers.handle_redeem,
            "/lottery": handlers.handle_lottery,
            "/redpacket": handlers.handle_redpacket,
            "/bindings": handlers.handle_bindings,
            "/transfers": handlers.handle_transfers,
            "/whyplay": handlers.handle_whyplay,
            "/wants": handlers.handle_wants,
        }
        fn = commands.get(cmd)
        if fn is None:
            sender.send_message(db, chat_id, "未知命令，发送 /help 查看可用命令")
            return

        # 账号、求片和免密登录都可能泄露隐私/凭据，强制私聊。
        # 账号展示（/account、/me）已放行群聊：只展示能量/活力/馆灵/成就等
        # 个人面板信息，不含任何凭据；登录与求片仍私聊限定。
        private_only = {"/request", "/myrequests", "/login", "/renew", "/govlog",
                        "/report", "/propose", "/launch"}
        if cmd in private_only and in_group:
            sender.send_message(db, chat_id, "🔒 这个功能请私聊机器人使用")
            return

        # P1 个人查询在群里不执行（结果会公开给全群看）
        if cmd in _P1_COMMANDS and in_group:
            sender.send_message(db, chat_id, "🔒 个人查询请私聊我发送，群里不查")
            return

        # /redeem 在群里发 = 把兑换码公开给全群：拒绝执行，尽量删掉原消息（没权限就算了），提示私聊
        if cmd == "/redeem" and in_group:
            try:
                sender.delete_message(db, chat_id, msg.get("message_id"))
            except Exception as exc:  # noqa: BLE001
                logger.debug("删除群内 /redeem 消息失败（忽略）: %s", exc)
            if _group_allow(chat_id, group_limit):
                sender.send_message(db, chat_id, "🔒 为保护兑换码，请私聊我发送 /redeem 兑换码，群里不执行兑换")
            return

        # 群级限流：超限静默丢弃，避免刷屏
        if in_group and not _group_allow(chat_id, group_limit):
            return

        # 红包开关：关闭后发红包提示已关闭（与 B3 文案一致）
        if cmd == "/redpacket" and not policy.is_redpacket_enabled(cfg):
            sender.send_message(db, chat_id, "🧧 红包功能已关闭")
            return

        # 各命令开关：关闭后统一回复"该功能已关闭"（/start /help 常开）
        if not policy.is_command_enabled(cfg, cmd):
            sender.send_message(db, chat_id, "该功能已关闭")
            return

        # 用户级命令限流：间隔可配置
        tg_uid = tg_user.get("id") or 0
        if not _user_cmd_allow(tg_uid, cmd, policy.rate_limit_seconds(cfg)):
            sender.send_message(db, chat_id, "操作太快了，稍后再试")
            return

        if cmd == "/lottery":
            # /lottery 需要群聊/私聊上下文（chat.type），单独分发
            chat_type = (chat.get("type") or "")
            reply = handlers.handle_lottery(
                db, tg_user, chat_id, args, chat_type in ("group", "supergroup"))
        elif cmd == "/redpacket":
            # P1 修复：把 update_id 传给 handler 做发红包幂等（防 poller 重放双花）
            reply = handlers.handle_redpacket(
                db, tg_user, chat_id, args, update_id=update.get("update_id"))
        else:
            reply = fn(db, tg_user, chat_id, args)
        # handler 可返回 str 或 (text, reply_markup) 元组（一键登录按钮）
        if isinstance(reply, tuple):
            text, markup = reply
        else:
            text, markup = reply, None
        sender.send_message(db, chat_id, text, reply_markup=markup)
    except Exception as exc:  # noqa: BLE001
        logger.error("tg dispatch failed: %s", exc, exc_info=True)
        if chat_id:
            try:
                sender.send_message(db, chat_id, "系统繁忙，请稍后再试")
            except Exception:  # noqa: BLE001
                logger.debug("tg failure reply failed", exc_info=True)
