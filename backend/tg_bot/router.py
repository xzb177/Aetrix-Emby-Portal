"""Telegram 消息路由：把 getUpdates 的单个 update 分发到命令处理器。

B4 策略（全部可配置，见 backend/tg_bot/policy.py）：
- 总开关 bot_enabled：关闭后静默丢弃所有 update（poller 继续跑）
- 群白名单 bot_group_ids：非名单群聊的消息/callback 静默忽略，私聊不受影响
- 命令开关：关闭的命令回复"该功能已关闭"（/start /help 常开）
- 限流：每用户每命令间隔 bot_rate_limit_seconds 秒；每群每分钟最多
  bot_group_rate_limit 条；超限静默丢弃（用户级限流仍提示一句话）
- 消息自毁（二期接线，见 ephemeral.py）：群聊里机器人回复默认 30 秒
  自毁（红包本体豁免）、用户命令消息默认 3 秒自毁；私聊不自毁。
- 频道马甲治理：群里以外部频道身份（sender_chat）发言的消息默认删除
  ＋回说明（匿名管理员形态不处理，判别见 policy.mask_sender_chat）。
- 退群处置：chat_member 更新里已绑定用户退群/被踢时，按
  bot_leave_action（默认 cancel_welfare）只取消其公益资格。
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
    from backend.tg_bot import handlers

    msg = update.get("message") or {}
    text = (msg.get("text") or "").strip()
    tg_user = msg.get("from") or {}
    tg_user_id = tg_user.get("id")
    chat_id = (msg.get("chat") or {}).get("id")
    if not tg_user_id or not chat_id:
        return
    reply = handlers.verify_bind_code(db, tg_user_id, chat_id, text)
    if reply:
        # 绑定码回复也是机器人群回复：群里照常挂 30 秒自毁，私聊不挂
        _send_reply(db, chat_id, reply,
                    autodelete=_is_group_chat(msg.get("chat") or {}))
    # reply 为 None → 不是有效的绑定码，静默忽略


def _award_group_chat_points(db, tg_user: dict) -> None:
    """群发言奖励：已绑定账号的发送者按发言计分。

    直接进程内调用 points.award_chat_points（与发表影评同款），
    走 points_chat_* 默认口径（0.1 能量/条小数累计、每天 20 能量封顶）。
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


def _send_reply(db, chat_id, text, reply_markup=None, *, autodelete=False):
    """机器人回复统一发送口（二期自毁接线）。

    autodelete=True 的发送点（仅群聊）走 send_message_ex 拿 message_id，
    再按 bot_msg_autodelete_seconds（默认 30 秒、0=关）挂自毁；
    延时为 0 时回落普通发送。私聊与豁免发送点（红包本体）不传此参数。
    发送失败或拿不到 message_id 时静默降级：自毁只是群面整洁手段，
    绝不让它影响回复本身。
    """
    from backend.tg_bot import sender

    if not autodelete:
        sender.send_message(db, chat_id, text, reply_markup)
        return
    from backend.tg_bot import ephemeral

    delay = ephemeral.bot_msg_delay_seconds(db)
    if delay <= 0:
        sender.send_message(db, chat_id, text, reply_markup)
        return
    ok, _err, message_id = sender.send_message_ex(db, chat_id, text, reply_markup)
    if ok and message_id:
        ephemeral.schedule_delete(chat_id, message_id, delay)


def _schedule_cmd_autodelete(db, chat_id, message_id):
    """群聊里用户命令消息本体挂 3 秒自毁（bot_cmd_autodelete_seconds）。

    延时为 0（关闭）时不挂。群内 /redeem 的原命令消息走既有即时删除、
    不经这里；万一某条消息已被即时删除又被挂上定时，到期删除失败也
    只在 ephemeral 内记一条日志，不外抛、不刷屏。
    """
    if not chat_id or not message_id:
        return
    from backend.tg_bot import ephemeral

    delay = ephemeral.bot_cmd_delay_seconds(db)
    if delay <= 0:
        return
    ephemeral.schedule_delete(chat_id, message_id, delay)


# P1 自助查询命令：个人数据，群聊里不执行（handler 层另有私聊 guard 双保险）
_P1_COMMANDS = ("/bindings", "/transfers", "/whyplay", "/wants")

# 群匿名管理员：以群/频道身份发言时，消息的 from 是 GroupAnonymousBot
# （固定 id 1087968824）且带 sender_chat——Telegram 协议上认不出背后是
# 哪位管理员，handler 里 resolve 永远查不到人。此前这类消息会被误当
# 未绑定用户回绑定指引（2026-10-30 站长群里匿名发 /redpacket 坐实）。
GROUP_ANONYMOUS_BOT_ID = 1087968824

ANON_ADMIN_TEXT = (
    "🎭 你现在是匿名管理员身份，我认不出是哪位管理员，这个操作办不了。\n"
    "请关掉匿名、用你本人身份再发一次就行；你的账号绑定是好的，不用重新绑定。"
)

# 需要发送者身份的命令全集：handler 内走 resolve / _bound_user /
# _p1_user，匿名发在 router 层统一拦下回 ANON_ADMIN_TEXT。
# 不在表内＝公开命令（/help /rank /requestboard /rules /slots：不查
# 发送者身份、只读全站或群级数据；/slots 群面板不带个人数据、绑定
# 在点注时才判），匿名发照常执行。
_IDENTITY_COMMANDS = {
    "/start", "/bind", "/checkin", "/points", "/chatpoints",
    "/account", "/me", "/request", "/myrequests", "/login", "/renew",
    "/govlog", "/level", "/report", "/propose", "/launch",
    "/redeem", "/lottery", "/redpacket",
    "/bindings", "/transfers", "/whyplay", "/wants", "/reconcile",
    "/rankpush",
    "/whois",
}


def _is_anonymous_admin(msg: dict) -> bool:
    """消息是否为匿名身份（群匿名管理员 / 以频道身份发言）。

    判据任一命中：消息带 sender_chat，或 from 是 GroupAnonymousBot
    固定 id。私聊没有这种消息，天然不受影响。
    """
    if msg.get("sender_chat"):
        return True
    return (msg.get("from") or {}).get("id") == GROUP_ANONYMOUS_BOT_ID


# 频道马甲治理的群内说明（走 _send_reply 自毁口径，群里 30 秒自毁）
CHANNEL_MASK_TEXT = (
    "🎭 本群不允许以频道身份发言，这条消息已删除。\n"
    "请用你本人账号发言；频道确需在本群发言，请联系管理员加白名单。"
)

# 退群取消公益资格后私聊通知本人的文案
LEAVE_WELFARE_TEXT = (
    "👋 你已退出 Aetrix 交流群，按群规设置，你的公益资格已随之取消。\n"
    "重新进群后联系管理员即可恢复；你的账号、能量与查看凭证都不受影响。"
)


def _intercept_channel_mask(db, cfg, msg) -> bool:
    """频道马甲治理：外部频道身份在群里发言 → 删消息＋回说明。

    返回 True 表示已拦截（调用方直接结束分发）。判别在
    policy.mask_sender_chat：匿名管理员（sender_chat=本群）、关联频道
    自动转发、白名单频道、总开关关闭时都放行，落回既有分发（身份命令
    仍走 #1516 匿名说明口径，本函数不碰）。删除失败（bot 非群管理员
    等）只记日志，说明照发但受群级限流约束，防频道刷屏变说明刷屏。
    """
    from backend.tg_bot import sender

    chat = msg.get("chat") or {}
    if not _is_group_chat(chat):
        return False
    chat_id = chat.get("id")
    if not chat_id or not policy.is_group_allowed(cfg, chat_id):
        return False
    sender_chat = policy.mask_sender_chat(msg, chat_id)
    if sender_chat is None:
        return False
    if not policy.is_channel_guard_enabled(cfg):
        return False
    if policy.is_channel_whitelisted(cfg, sender_chat.get("id")):
        return False
    try:
        sender.delete_message(db, chat_id, msg.get("message_id"))
    except Exception as exc:  # noqa: BLE001
        logger.debug("删除频道马甲消息失败（忽略）: %s", exc)
    logger.info("channel mask message intercepted: chat=%s sender_chat=%s",
                chat_id, sender_chat.get("id"))
    if _group_allow(chat_id, policy.group_rate_limit(cfg)):
        _send_reply(db, chat_id, CHANNEL_MASK_TEXT, autodelete=True)
    return True


def _apply_leave_cancel_welfare(db, tg_id: int, chat_id) -> None:
    """退群取消公益资格：与 welfare_expiry 的 auto_expire 同一口径——
    is_welfare=False + WelfareGrantLog(channel="group_leave", days=0)
    留痕；查看凭证、能量一律不动。原本就无资格的只记日志、不通知
    （避免 poller 重放或重复事件误报）；状态真正变化才私聊通知本人。
    """
    from backend import models
    from backend.tg_bot import sender
    from backend.tg_bot.identity import resolve

    user = resolve(db, int(tg_id))
    if user is None:
        return
    if not user.is_welfare:
        logger.info("group leave: user=%s 无公益资格，跳过 chat=%s",
                    user.id, chat_id)
        return
    user.is_welfare = False
    db.add(models.WelfareGrantLog(
        user_id=user.id, channel="group_leave", days=0, granted_by=None))
    db.commit()
    logger.info("group leave welfare cancelled: user=%s tg=%s chat=%s",
                user.id, tg_id, chat_id)
    try:
        sender.send_message(db, int(tg_id), LEAVE_WELFARE_TEXT)
    except Exception:  # noqa: BLE001
        logger.debug("退群取消资格通知发送失败（忽略）", exc_info=True)


def _handle_chat_member(db, cfg, cm: dict) -> None:
    """chat_member 更新：已绑定用户退群/被踢 → 按 bot_leave_action 处置。

    只处理名单内群、状态真正变为 left/kicked 的变动（old 已是
    left/kicked 的重复推送/重放直接跳过）。处置失败只记日志：
    成员事件不许把分发打崩、更不许给群里发"系统繁忙"。
    注意：Telegram 只在 bot 为群管理员时才稳定推送他人的成员变动，
    非管理员场景可能根本收不到这类 update（真机未验）。
    """
    try:
        chat = cm.get("chat") or {}
        if not _is_group_chat(chat):
            return
        chat_id = chat.get("id")
        if not chat_id or not policy.is_group_allowed(cfg, chat_id):
            return
        new_cm = cm.get("new_chat_member") or {}
        if new_cm.get("status") not in ("left", "kicked"):
            return
        old_status = (cm.get("old_chat_member") or {}).get("status")
        if old_status in ("left", "kicked"):
            return
        tg_user = new_cm.get("user") or {}
        tg_id = tg_user.get("id")
        if not tg_id or tg_user.get("is_bot"):
            return
        if policy.leave_action(cfg) != "cancel_welfare":
            logger.info("group leave not actioned (bot_leave_action=%s): "
                        "chat=%s tg=%s", policy.leave_action(cfg),
                        chat_id, tg_id)
            return
        # A-110：处置前须与对账用同一“在群”口径逐群复核——任一名单群
        # 仍在即跳过；有群接口失败且无群确认在群时 fail-closed 跳过
        # 本次处置（事件重放／下次对账再裁决），绝不在“未核验”时取消。
        # 名单为空时无法枚举他群，沿用事件群本身的判定（既有行为不变）。
        from backend.tg_bot import reconcile, sender

        groups = sorted(policy.parse_group_ids(cfg.get("bot_group_ids", "")))
        if groups:
            status = reconcile._member_status(
                db, groups, int(tg_id), sender.get_chat_member)
            if status is True:
                logger.info(
                    "group leave skipped (still member in another group): "
                    "chat=%s tg=%s", chat_id, tg_id)
                return
            if status is None:
                logger.info(
                    "group leave skipped (member status unverified): "
                    "chat=%s tg=%s", chat_id, tg_id)
                return
        _apply_leave_cancel_welfare(db, int(tg_id), chat_id)
    except Exception:
        logger.exception("chat_member handling failed")


def dispatch(db, update: dict) -> None:
    """分发单个 update 到对应的处理器。

    P0：处理器抛异常时不再只记日志——尽力给用户回一句看得见的失败提示
    （2026-10-21 教训：失败只记日志、用户端看着像成功）。
    """
    from backend.tg_bot import handlers, redpacket_common, sender

    # 先取 chat_id：异常发生在任何一步时失败提示都有地方发
    if "callback_query" in update:
        _cq = update["callback_query"] or {}
        chat_id = ((_cq.get("message") or {}).get("chat") or {}).get("id")
    else:
        chat_id = ((update.get("message") or {}).get("chat") or {}).get("id")
    # 消息路径内会重新赋值；异常兜底回复据此决定是否挂自毁
    in_group = False

    try:
        # B4：批量读取全部 Bot 策略配置（1 次 DB 查询）
        cfg = policy.read_bot_config(db)

        # 总开关：关闭后完全不响应（poller 继续跑，offset 照常推进）
        if not policy.is_enabled(cfg):
            return

        # chat_member 更新不带 message：退群处置单独处理
        if "chat_member" in update:
            _handle_chat_member(db, cfg, update.get("chat_member") or {})
            return

        # bot 自身的成员变动：poller 已订阅（口径见 poller），不处理
        if "my_chat_member" in update:
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

        # 内联搜片（@bot 片名）：没有 chat 上下文，结果走 answerInlineQuery
        if "inline_query" in update:
            from backend.tg_bot import inline_search

            inline_search.handle_inline_query(
                db, update["inline_query"] or {}, cfg)
            return

        msg = update.get("message") or {}
        # 频道马甲治理：频道身份的媒体消息没有 text 也要拦，故在判空前拦截
        if _intercept_channel_mask(db, cfg, msg):
            return
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
            # 匿名发的绑定码认不出是谁，验了也绑不到本人：回匿名说明
            if _is_anonymous_admin(msg):
                _send_reply(db, chat_id, ANON_ADMIN_TEXT, autodelete=in_group)
                return
            _verify_bind_code(db, update)
            return

        # 口令抽奖：群内非命令文本先尝试匹配口令关键词
        # 注意：此处不消耗群级限流预算（普通聊天不计入），防刷由 handler 内部的
        # 幂等逻辑保证（重复口令只算一次；参加/重复反馈走自毁消息，不常驻刷屏）。
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
            "/chatpoints": handlers.handle_chatpoints,
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
            "/slots": handlers.handle_slots_command,
            "/bindings": handlers.handle_bindings,
            "/transfers": handlers.handle_transfers,
            "/whyplay": handlers.handle_whyplay,
            "/wants": handlers.handle_wants,
            "/reconcile": handlers.handle_reconcile,
            "/rankpush": handlers.handle_rankpush,
            "/whois": handlers.handle_whois,
        }
        fn = commands.get(cmd)
        if fn is None:
            _send_reply(db, chat_id, "未知命令，发送 /help 查看可用命令",
                        autodelete=in_group)
            return

        # 匿名管理员拦截：需要身份的命令统一回匿名说明，不许落进
        # handler 被 resolve→None 误回绑定指引（对方其实早已绑定）。
        # 唯一例外：群内 /redeem 的删消息护码分支在下方先于 handler
        # 执行、给的是私聊提示而非绑定指引，匿名发继续走它不拦。
        if (
            _is_anonymous_admin(msg)
            and cmd in _IDENTITY_COMMANDS
            and not (cmd == "/redeem" and in_group)
        ):
            _send_reply(db, chat_id, ANON_ADMIN_TEXT, autodelete=True)
            return

        # 账号、求片和免密登录都可能泄露隐私/凭据，强制私聊。
        # 账号展示（/account、/me）已放行群聊：只展示能量/活力/馆灵/成就等
        # 个人面板信息，不含任何凭据；登录与求片仍私聊限定。
        # /whois 是刻意例外（2026-11-01）：不在本集合——群里走 handler
        # 的管理员精简卡模式（is_staff 门禁 + 字段白名单，无审计痕迹），
        # 私聊仍出审计全卡；见 handlers.handle_whois 的 in_group 路径。
        private_only = {"/request", "/myrequests", "/login", "/renew", "/govlog",
                        "/report", "/propose", "/launch", "/reconcile", "/rankpush"}
        if cmd in private_only and in_group:
            _send_reply(db, chat_id, "🔒 这个功能请私聊机器人使用",
                        autodelete=True)
            _schedule_cmd_autodelete(db, chat_id, msg.get("message_id"))
            return

        # P1 个人查询在群里不执行（结果会公开给全群看）
        if cmd in _P1_COMMANDS and in_group:
            from backend.tg_bot import queries

            _send_reply(db, chat_id, queries.PRIVATE_ONLY_TEXT,
                        autodelete=True)
            _schedule_cmd_autodelete(db, chat_id, msg.get("message_id"))
            return

        # /redeem 在群里发 = 把兑换码公开给全群：拒绝执行，尽量删掉原消息（没权限就算了），提示私聊
        if cmd == "/redeem" and in_group:
            try:
                sender.delete_message(db, chat_id, msg.get("message_id"))
            except Exception as exc:  # noqa: BLE001
                logger.debug("删除群内 /redeem 消息失败（忽略）: %s", exc)
            if _group_allow(chat_id, group_limit):
                _send_reply(db, chat_id, "🔒 为保护兑换码，请私聊我发送 /redeem 兑换码，群里不执行兑换",
                            autodelete=True)
            return

        # 群级限流：超限静默丢弃，避免刷屏
        if in_group and not _group_allow(chat_id, group_limit):
            return

        # 用户命令消息 3 秒自毁：命令已识别、已过群白名单与群级限流，
        # 在此统一定时删除（私聊不挂；/redeem 即时删除分支已先行返回，
        # 私聊限定/P1 拦截分支在各自回复处已单独挂过，不重复）
        if in_group:
            _schedule_cmd_autodelete(db, chat_id, msg.get("message_id"))

        # 红包开关：关闭后发红包提示已关闭（与 B3 文案一致）
        if cmd == "/redpacket" and not policy.is_redpacket_enabled(cfg):
            _send_reply(db, chat_id, "🧧 红包功能已关闭", autodelete=in_group)
            return

        # 各命令开关：关闭后统一回复"该功能已关闭"（/start /help 常开）
        if not policy.is_command_enabled(cfg, cmd):
            _send_reply(db, chat_id, "该功能已关闭", autodelete=in_group)
            return

        # 用户级命令限流：间隔可配置
        tg_uid = tg_user.get("id") or 0
        if not _user_cmd_allow(tg_uid, cmd, policy.rate_limit_seconds(cfg)):
            _send_reply(db, chat_id, "操作太快了，稍后再试", autodelete=in_group)
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
        elif cmd == "/slots":
            # /slots 需要群聊/私聊上下文（群公共面板 vs 私聊本人面板，
            # 开关口径与 menu:slots 面板入口一致），单独分发
            reply = handlers.handle_slots_command(
                db, tg_user, chat_id, args, in_group=in_group)
        elif cmd == "/whois":
            # /whois 需要群聊上下文（私聊全卡 vs 群内管理员精简卡）
            # 与被回复消息的发送者（群内回复定位），通用签名拿不到，
            # 与 /slots 同款单独分发；handler 默认参数保证私聊与
            # 直调行为逐字不变。
            reply_from = (msg.get("reply_to_message") or {}).get("from")
            reply = handlers.handle_whois(
                db, tg_user, chat_id, args,
                in_group=in_group, reply_from=reply_from)
        else:
            reply = fn(db, tg_user, chat_id, args)
        # handler 可返回 str 或 (text, reply_markup) 元组（一键登录按钮）
        if isinstance(reply, tuple):
            text, markup = reply
        else:
            text, markup = reply, None
        if cmd == "/start" and not in_group:
            # 私聊主界面走图卡（caption 超限/发图失败时 send_panel 自动回退文本）
            # 注：router 内 send_panel 仅此私聊一处，群聊没有图卡发送点，
            # 故本期无需为图卡另走 send_photo_ex（sender 未动）。
            sender.send_panel(db, chat_id, text, reply_markup=markup)
        elif cmd == "/redpacket" and isinstance(reply, tuple):
            # 红包本体豁免（按分发路径判定，不看文本）：handle_redpacket
            # 仅在发送成功时返回 (packet_text, claim_markup) 元组，这条
            # 带抢按钮的消息按用户口径永不自毁；参数错误/失败等 str
            # 回复走下面分支照常自毁。本体形态走封面照片消息
            # （send_packet_message 内部发不出时回落纯文本）。
            redpacket_common.send_packet_message(
                db, chat_id, text, reply_markup=markup)
        else:
            _send_reply(db, chat_id, text, reply_markup=markup,
                        autodelete=in_group)
    except Exception as exc:  # noqa: BLE001
        logger.error("tg dispatch failed: %s", exc, exc_info=True)
        if chat_id:
            try:
                _send_reply(db, chat_id, "系统繁忙，请稍后再试",
                            autodelete=in_group)
            except Exception:  # noqa: BLE001
                logger.debug("tg failure reply failed", exc_info=True)
