"""callback_query 处理：主菜单按钮（menu:*）+ 抢红包按钮（redpacket_claim:{id}）+ 抽奖参加按钮（lottery_join:{round_id}）+ 老虎机下注按钮（slots_bet:{tier}）。"""
from __future__ import annotations

import html
import logging
from datetime import datetime

from backend import models
from . import ephemeral, layout, policy, redpacket_common, sender
from .identity import resolve

logger = logging.getLogger(__name__)


def handle_callback(db, callback_query: dict, redpacket_enabled: bool | None = None) -> None:
    """处理 callback_query：菜单按钮（menu:*）+ 抢红包按钮 + 抽奖参加按钮。

    redpacket_enabled：调用方（router.dispatch）已批量读到的红包开关值；
    传 None 时回退为自行读取（兼容直接调用）。
    """
    data = (callback_query.get("data") or "")
    if data.startswith("menu:"):
        _handle_menu(db, callback_query)
        return
    if data.startswith("whois:"):
        _handle_whois_callback(db, callback_query)
        return
    if data.startswith("redpacket_claim:"):
        _handle_redpacket_claim(db, callback_query, redpacket_enabled)
        return
    if data.startswith("lottery_join:"):
        _handle_lottery_join(db, callback_query)
        return
    if data.startswith("reconcile:"):
        _handle_reconcile_callback(db, callback_query)
    if data.startswith("slots_bet:"):
        _handle_slots_bet(db, callback_query)
        return


def _handle_menu(db, callback_query: dict) -> None:
    """处理主菜单 inline 按钮（menu:*）：原地编辑消息展示对应页面。"""
    from . import handlers

    data = callback_query.get("data") or ""
    action = data.split(":", 1)[1] if ":" in data else ""
    cq_id = callback_query.get("id")
    from_user = callback_query.get("from") or {}
    tg_id = from_user.get("id")
    msg = callback_query.get("message") or {}
    chat = msg.get("chat") or {}
    chat_id = chat.get("id")
    message_id = msg.get("message_id")
    if not chat_id or not message_id or not tg_id:
        return
    is_private = (chat_id == tg_id)
    tg_user = {"id": tg_id, "first_name": from_user.get("first_name")}

    # 先消除按钮 loading
    sender.answer_callback_query(db, cq_id)

    try:
        text, markup = _render_menu(db, tg_user, tg_id, chat_id, is_private, action)
    except _UnknownMenuAction:
        return  # 未知菜单：静默忽略
    except Exception:
        logger.exception("tg menu render failed: action=%s", action)
        text, markup = "系统繁忙，请稍后再试", handlers.back_markup()

    has_photo = bool(msg.get("photo"))
    fallback_photo = None
    if action == "redpacket" and has_photo:
        # 发红包发起面板用专属封面图：caption 编辑换不了图，走
        # editMessageMedia 换图；失败（图不可用/API 拒绝）回退普通
        # caption 编辑，面板照常打开（仍是品牌图）。
        fallback_photo = layout.REDPACKET_IMAGE
        ok, err = sender.edit_message_media(
            db, chat_id, message_id, text, markup,
            photo_path=layout.REDPACKET_IMAGE)
        if not ok:
            logger.debug("tg redpacket media edit failed: err=%s", err)
            ok, err = sender.edit_panel(
                db, chat_id, message_id, text, markup, has_photo=True)
    else:
        ok, err = sender.edit_panel(
            db, chat_id, message_id, text, markup, has_photo=has_photo)
    if not ok:
        logger.debug("tg menu edit failed: action=%s err=%s", action, err)
        # 导航回落：原地编辑被 TG 拒绝（消息不可编辑/内容未变/消息
        # 过旧等）时，删掉旧消息、新发目标面板，保证导航一定落到
        # 用户点的页面。旧行为只记日志就结束，用户被困在原面板上
        # 反复点、面板纹丝不动（2026-10-30 老虎机主界面实证）。
        # 回落发的是目标面板，绝不许重发当前面板。
        sender.delete_message(db, chat_id, message_id)
        sender.send_panel(
            db, chat_id, text, markup,
            prefer_photo=has_photo, photo_path=fallback_photo)
    elif not is_private:
        # 群面板滑动自毁（PR-2）：编辑成功落地＝一次互动，把自毁
        # 重挂为「最后互动＋bot_msg 延时」，正点着不会准点消失。
        # 编辑失败走上面的删旧发新回落，旧消息已删、无可重挂，不进这里。
        _rearm_group_panel_autodelete(db, chat_id, message_id)


def _handle_whois_callback(db, callback_query: dict) -> None:
    """处理 /whois 管理员面板按钮（whois:<page>:<user_id>[:<op>]）。

    每次点击都重新校验点击者：未绑定或非管理员一律回拒绝卡（不带
    目标任何数据）；目标不存在回明示卡。渲染与写操作全在
    whois_panel（写操作二次确认在 ask/do 两步里，do 才执行）。
    """
    from . import whois_panel

    data = callback_query.get("data") or ""
    parts = data.split(":")
    cq_id = callback_query.get("id")
    from_user = callback_query.get("from") or {}
    tg_id = from_user.get("id")
    msg = callback_query.get("message") or {}
    chat = msg.get("chat") or {}
    chat_id = chat.get("id")
    message_id = msg.get("message_id")
    if not chat_id or not message_id or not tg_id or len(parts) < 3:
        return
    sender.answer_callback_query(db, cq_id)
    try:
        page = parts[1]
        target_id = int(parts[2])
        op = parts[3] if len(parts) > 3 else None
    except (IndexError, ValueError):
        return
    caller = resolve(db, int(tg_id))
    if caller is None or not bool(getattr(caller, "is_staff", False)):
        text = layout.card(
            layout.title("账号反查"),
            [layout.row("🔒", "访问状态", "仅管理员可用")])
        sender.edit_message_text(db, chat_id, message_id, text, None)
        return
    target = (
        db.query(models.WebUser)
        .filter(models.WebUser.id == target_id)
        .first()
    )
    if target is None:
        text = layout.card(
            layout.title("账号速览"),
            [layout.row("📭", "结果", "目标账号不存在")])
        sender.edit_message_text(db, chat_id, message_id, text, None)
        return
    try:
        text, markup = whois_panel.render(db, caller, target, page, op)
    except Exception:
        logger.warning("tg whois panel render failed: %s", data, exc_info=True)
        return
    ok, _err = sender.edit_message_text(db, chat_id, message_id, text, markup)
    if ok and chat_id != int(tg_id):
        # 群面板续命：与 menu 面板同一口径，互动后重挂自毁定时。
        _rearm_group_panel_autodelete(db, chat_id, message_id)


def _rearm_group_panel_autodelete(db, chat_id: int, message_id: int) -> None:
    """群面板互动续命接线：按当前配置重挂该面板消息的自毁定时器。

    只由 _handle_menu 在群聊且面板编辑成功后调用；私聊不自毁。
    旁路纪律：重挂失败只记 debug 日志，绝不影响面板本身。
    """
    try:
        ephemeral.rearm_delete(db, chat_id, message_id)
    except Exception:
        logger.debug(
            "tg panel autodelete rearm failed: chat=%s message=%s",
            chat_id, message_id, exc_info=True)


def _render_menu(db, tg_user: dict, tg_id: int, chat_id: int, is_private: bool,
                 action: str) -> tuple[str, dict | None]:
    """渲染菜单页面，返回 (text, markup)。未知 action 返回 None 标记。"""
    from . import handlers

    text: str
    markup: dict | None
    if action in ("main", "home", "panel"):
        text, markup = handlers.menu_main(db, tg_user, is_private)
    elif action == "user":
        user = resolve(db, int(tg_id))
        if user is None:
            text, markup = handlers._bind_guide_text(), handlers.back_markup()
        elif not is_private:
            text = layout.card(
                layout.title("用户面板"),
                [layout.row("🔒", "使用方式", "这个功能请私聊机器人使用")])
            markup = handlers.back_markup()
        else:
            from . import features

            text = features.user_panel(db, user, display_name=tg_user.get("first_name"))
            markup = handlers.user_panel_markup()
    elif action.startswith("group_"):
        # 用户面板二级分组：只是翻页，不查身份、不设门禁；
        # 组内功能按钮各自的私聊/绑定守卫在它们自己的渲染路径上。
        group_key = action[len("group_"):]
        if group_key not in handlers.USER_PANEL_GROUPS:
            raise _UnknownMenuAction(action)
        text = handlers.user_panel_group_text(group_key)
        markup = handlers.user_panel_group_markup(group_key)
    elif action == "server":
        text = handlers.handle_server(db, tg_user, chat_id, "")
        markup = handlers.back_markup()
    elif action == "admin":
        text = handlers.handle_admin(db, tg_user, chat_id, "")
        admin_user = resolve(db, int(tg_id))
        if admin_user is not None and bool(getattr(admin_user, "is_staff", False)):
            markup = handlers.admin_markup()
        else:
            markup = handlers.back_markup()
    elif action == "renewcode":
        text = handlers.handle_redeem(db, tg_user, chat_id, "")
        markup = handlers.back_markup()
    elif action == "checkin":
        text = handlers.handle_checkin(db, tg_user, chat_id, "")
        markup = handlers.back_markup()
    elif action == "points":
        text = handlers.handle_points(db, tg_user, chat_id, "")
        markup = handlers.back_markup()
    elif action == "chatpoints":
        # 发言能量已并入 /points 输出：按钮入口一律指向合并后面板。
        text = handlers.handle_points(db, tg_user, chat_id, "")
        markup = handlers.back_markup()
    elif action in ("account", "request", "myrequests", "rank", "requestboard", "rules", "login", "govlog", "level", "report", "propose", "launch"):
        # 凭据类与求片类仍私聊限定；账号展示（account/level）群里放行
        # （2026-10-28 用户亲定、2026-11-02 重申）：落下面正常渲染分支
        # 出点击者本人的账号卡，与命令层 /me 口径一致。
        if not is_private and action in ("request", "myrequests", "login", "govlog", "report", "propose", "launch"):
            text = layout.card(
                layout.title("私聊使用"),
                [layout.row("🔒", "使用方式", "这个功能请私聊机器人使用")])
            markup = handlers.back_markup()
        else:
            fn = {
                "account": handlers.handle_account,
                "request": handlers.handle_request,
                "myrequests": handlers.handle_my_requests,
                "rank": handlers.handle_rank,
                "requestboard": handlers.handle_request_board,
                "rules": handlers.handle_rules,
                "login": handlers.handle_login,
                "govlog": handlers.handle_govlog,
                "level": handlers.handle_account,
                "report": handlers.handle_report,
                "propose": handlers.handle_propose,
                "launch": handlers.handle_direct_launch,
            }[action]
            reply = fn(db, tg_user, chat_id, "")
            if isinstance(reply, tuple):
                text, markup = reply
            else:
                text, markup = reply, handlers.back_markup()
    elif action == "lottery":
        # PA-030：群/私聊分流。群里走群轮次卡（is_group=True），绝不把
        # 点击者个人抽奖记录编辑进群共享消息；私聊保持个人记录分支。
        reply = handlers.handle_lottery(db, tg_user, chat_id, "", not is_private)
        if isinstance(reply, tuple):
            text, markup = reply
        else:
            text, markup = reply, handlers.back_markup()
    elif action in ("bindings", "transfers", "whyplay", "wants"):
        # 个人查询私聊限定（与命令层口径一致）：群里点到只回私聊卡，
        # 绝不把点击者本人的绑定/转存/想看数据编辑进群共享消息。
        if not is_private:
            text = handlers.private_only_card()
            markup = handlers.private_only_markup(db)
        else:
            fn = {
                "bindings": handlers.handle_bindings,
                "transfers": handlers.handle_transfers,
                "whyplay": handlers.handle_whyplay,
                "wants": handlers.handle_wants,
            }[action]
            text = fn(db, tg_user, chat_id, "")
            markup = handlers.back_markup()
    elif action == "bind":
        user = resolve(db, int(tg_id))
        if user is not None:
            text = layout.card(
                layout.title("账号绑定"),
                [layout.row("✅", "绑定状态", "你的账号已绑定，无需重复绑定；如需更换绑定请联系客服解绑")])
        else:
            text = handlers._bind_guide_text()
        markup = handlers.back_markup()
    elif action == "help":
        text = handlers.handle_help(db, tg_user, chat_id, "")
        markup = handlers.back_markup()
    elif action == "redpacket":
        text = layout.card(
            layout.title("发红包"),
            [
                layout.row("🧧", "使用方法", f"{handlers._cmd('/redpacket')} <i>总能量 个数</i>"),
                layout.row("💡", "红包示例", f"{handlers._cmd('/redpacket 100 10')}（100 能量分成 10 个）"),
            ],
            ["💡 请直接发送命令发红包（按钮不支持输入参数）"],
        )
        markup = handlers.back_markup()
    elif action == "slots":
        if not is_private:
            # 群里可玩（2026-10-30 用户亲令推翻仅私聊口径）：渲染公共
            # 面板，注额按钮谁点谁下注；总开关或群开关关闭时回落私聊
            # 提示卡（与私聊限定功能同口径）。
            from backend import slots as slots_svc

            if slots_svc.is_enabled(db) and slots_svc.group_enabled(db):
                text, markup = handlers.handle_slots_group(db, tg_user, chat_id, "")
                if markup is None:
                    markup = handlers.back_markup()
            else:
                text = layout.card(
                    layout.title("老虎机"),
                    [layout.row("🔒", "使用方式", "老虎机请私聊机器人使用")])
                markup = handlers.back_markup()
        else:
            slots_user = resolve(db, int(tg_id))
            if slots_user is None:
                text, markup = handlers._bind_guide_text(), handlers.back_markup()
            else:
                text, markup = handlers.handle_slots(db, tg_user, chat_id, "")
                if markup is None:
                    markup = handlers.back_markup()
    else:
        raise _UnknownMenuAction(action)
    return text, markup


class _UnknownMenuAction(Exception):
    """未知菜单 action：调用方静默忽略。"""
def _answer_callback(db, callback_id, text: str, show_alert: bool = False) -> None:
    """调用 answerCallbackQuery 给用户 toast 提示（幂等、可重复调用）。"""
    if not callback_id:
        return
    try:
        from backend.integrations.telegram import token as get_token

        token = get_token(db)
    except Exception:
        logger.exception("get telegram token failed")
        return
    if not token:
        logger.debug("telegram token empty, skip answerCallbackQuery")
        return
    try:
        import httpx

        httpx.post(
            f"https://api.telegram.org/bot{token}/answerCallbackQuery",
            json={
                "callback_query_id": callback_id,
                "text": text[:180],
                "show_alert": show_alert,
            },
            timeout=10.0,
        )
    except Exception:
        logger.exception("answerCallbackQuery failed")


def _lottery():
    """惰性导入 lottery 模块（G1 契约），不可用时返回 None。"""
    try:
        from backend import lottery

        return lottery
    except ImportError:
        return None


def _send_lottery_feedback(db, chat_id: int, text: str) -> None:
    """抽奖群内反馈（旁路：失败只记日志，绝不影响参加结果）。

    与红包领取反馈同口径：send_message_ex 拿 message_id 后按
    bot_msg_autodelete_seconds（默认 30 秒）自毁，不新起无自毁的刷屏源。
    """
    try:
        ok, err, feedback_id = sender.send_message_ex(db, chat_id, text)
        if not ok:
            logger.warning("tg lottery feedback send failed: chat=%s err=%s", chat_id, err)
            return
        if feedback_id:
            delay = ephemeral.bot_msg_delay_seconds(db)
            if delay > 0:
                ephemeral.schedule_delete(chat_id, feedback_id, delay)
    except Exception:
        logger.warning("tg lottery feedback failed: chat=%s", chat_id, exc_info=True)


def _refresh_lottery_announce(db, lot, round_id: int) -> None:
    """参加成功后节流编辑公告卡人数（旁路，旧版 lottery 无此函数时跳过）。"""
    refresh = getattr(lot, "refresh_announce_card", None)
    if refresh is None:
        return
    try:
        refresh(db, round_id)
    except Exception:
        logger.warning("tg lottery announce refresh failed: round=%s", round_id, exc_info=True)


def _handle_lottery_join(db, callback_query: dict) -> None:
    """处理抽奖参加按钮点击（幂等）。"""
    cb_id = callback_query.get("id")
    try:
        data = callback_query.get("data") or ""
        from_user = callback_query.get("from") or {}
        telegram_id = from_user.get("id")
        msg = callback_query.get("message") or {}
        chat_id = (msg.get("chat") or {}).get("id")

        # 1. 解析 round_id
        try:
            round_id = int(data.split(":", 1)[1])
        except (ValueError, IndexError):
            _answer_callback(db, cb_id, "请求无效，请重新点击")
            return
        if not cb_id:
            return

        # 2. 功能开关
        lot = _lottery()
        if lot is None:
            _answer_callback(db, cb_id, "🎲 抽奖功能暂未开启")
            return
        try:
            enabled = bool(lot.is_enabled(db))
        except Exception:
            logger.exception("lottery.is_enabled failed")
            enabled = False
        if not enabled:
            _answer_callback(db, cb_id, "🎲 抽奖功能暂未开启")
            return

        # 3. 取 round（兜底链）
        round_obj = None
        if hasattr(lot, "get_round"):
            try:
                round_obj = lot.get_round(db, round_id)
            except Exception:
                logger.exception("lottery.get_round failed")
                round_obj = None
        if round_obj is None and chat_id is not None:
            try:
                active = lot.get_active_round(db, chat_id)
            except Exception:
                logger.exception("lottery.get_active_round failed")
                active = None
            if active is not None and getattr(active, "id", None) == round_id:
                round_obj = active
        if round_obj is None:
            _answer_callback(db, cb_id, "本轮抽奖已结束")
            return

        # 4. 状态与开奖时间校验
        status = str(getattr(round_obj, "status", "open") or "open")
        if status != "open":
            _answer_callback(db, cb_id, "本轮抽奖已结束")
            return
        draw_at = getattr(round_obj, "draw_at", None)
        if draw_at is not None:
            from datetime import datetime

            # draw_at 按上海 naive 入库，比较用同一口径
            if isinstance(draw_at, datetime) and draw_at <= lot.shanghai_now_naive():
                _answer_callback(db, cb_id, "本轮抽奖已结束")
                return

        # 5. 群组 allowlist
        allowed = None
        if hasattr(lot, "allowed_group_ids"):
            try:
                allowed = lot.allowed_group_ids(db)
            except Exception:
                logger.exception("lottery.allowed_group_ids failed")
                allowed = None
        if allowed is None:
            from backend.integrations import store

            raw = store.get_value(db, "lottery_group_ids", "") or ""
            allowed = {int(p) for p in raw.split(",") if p.strip().isdigit()}
        if allowed and chat_id not in allowed:
            _answer_callback(db, cb_id, "本群暂未开放抽奖")
            return

        # 6. 用户身份
        user = resolve(db, int(telegram_id)) if telegram_id else None
        if user is None:
            _answer_callback(
                db,
                cb_id,
                "请先绑定账号后再参加（发送 /bind 查看指引）",
                show_alert=True,
            )
            return

        # 6.5 三重门·身份门：参赛者必须在 TG 群里（防群外薅奖）。
        # fail-open：API 失败时放行（记日志），可用性优先，不因 TG 抖动误伤用户。
        require_member = True
        if hasattr(lot, "require_group_member"):
            try:
                require_member = bool(lot.require_group_member(db))
            except Exception:
                logger.exception("lottery.require_group_member failed")
                require_member = True
        if require_member:
            try:
                is_member, _status = sender.get_chat_member(db, chat_id, int(telegram_id))
            except Exception:
                logger.exception("getChatMember failed, fail-open")
                is_member = None
            if is_member is False:
                _answer_callback(
                    db,
                    cb_id,
                    "请先加入本群后再参加抽奖",
                    show_alert=True,
                )
                return

        # 7. 参加（幂等核心）
        try:
            result = lot.join_round(db, round_obj, user, int(telegram_id))
        except Exception:
            logger.exception("lottery_join failed")
            _answer_callback(db, cb_id, "参加失败，请稍后再试")
            return
        ok = bool(result.get("ok")) if isinstance(result, dict) else bool(result)
        reason = result.get("reason") if isinstance(result, dict) else ""
        if ok:
            _answer_callback(db, cb_id, "🎉 参加成功，祝你好运！")
            _refresh_lottery_announce(db, lot, round_id)
        elif reason == "already":
            _answer_callback(db, cb_id, "你已参加过本轮抽奖")
        elif reason == "full":
            _answer_callback(db, cb_id, "名额已满，下次再来")
        elif reason == "creator":
            _answer_callback(db, cb_id, "发起人不能参加自己发起的活动", show_alert=True)
        elif reason in ("closed", "disabled"):
            _answer_callback(db, cb_id, "本轮抽奖已结束")
        elif reason == "account_disabled":
            _answer_callback(db, cb_id, "账号已被停用，无法参加抽奖", show_alert=True)
        elif reason == "blacklisted":
            _answer_callback(db, cb_id, "你暂无抽奖资格", show_alert=True)
        elif reason == "too_new":
            _answer_callback(
                db, cb_id,
                f"账号注册满 {result.get('min_days', 0)} 天后才能参加",
                show_alert=True,
            )
        elif reason == "rate_limited":
            _answer_callback(db, cb_id, "今日参加次数已用完，明天再来")
        else:
            _answer_callback(db, cb_id, "参加失败，请稍后再试")
    except Exception:
        logger.exception("handle lottery_join callback failed")
        _answer_callback(db, cb_id, "系统繁忙，请稍后再试")


def handle_lottery_password(db, chat_id: int, tg_user: dict, text: str) -> bool:
    """口令抽奖：群消息命中口令关键词时自动登记参加。

    返回 True 表示本消息被口令抽奖消费（调用方不再继续分发）；
    返回 False 表示无匹配的口令活动，调用方继续原有分发逻辑。

    三重门校验与按钮参加一致：身份门（getChatMember，fail-open）+
    资格门（join_round 内集中检查：黑名单/新号/限流/已参加/名额）。
    同一用户重复发送口令只算一次；参加成功与重复参加都发一条带人数的
    自毁反馈（2026-10-31 根治：旧版重复静默、成功反馈无人数且发送失败
    被吞，用户亲报「发了口令群里没反馈」）。
    """
    lot = _lottery()
    if lot is None:
        return False
    try:
        enabled = bool(lot.is_enabled(db))
    except Exception:
        logger.exception("lottery.is_enabled failed")
        return False
    if not enabled:
        return False

    keyword = (text or "").strip()
    if not keyword:
        return False
    try:
        round_obj = lot.get_password_round(db, chat_id, keyword)
    except Exception:
        logger.exception("lottery.get_password_round failed")
        return False
    if round_obj is None:
        return False

    telegram_id = tg_user.get("id")
    if not telegram_id:
        return True  # 消费掉，避免继续分发

    # 用户身份：必须已绑定账号
    user = resolve(db, int(telegram_id))
    if user is None:
        return True  # 未绑定则静默消费，不在群里刷屏提示

    # 三重门·身份门：参赛者必须在 TG 群里（fail-open）
    require_member = True
    if hasattr(lot, "require_group_member"):
        try:
            require_member = bool(lot.require_group_member(db))
        except Exception:
            logger.exception("lottery.require_group_member failed")
            require_member = True
    if require_member:
        try:
            is_member, _status = sender.get_chat_member(db, chat_id, int(telegram_id))
        except Exception:
            logger.exception("getChatMember failed, fail-open")
            is_member = None
        if is_member is False:
            return True  # 非群成员静默消费，不刷屏

    # 参加（幂等：重复发送口令只算一次）
    try:
        result = lot.join_round(db, round_obj, user, int(telegram_id))
    except Exception:
        logger.exception("lottery password join failed")
        return True
    ok = bool(result.get("ok")) if isinstance(result, dict) else bool(result)
    reason = result.get("reason") if isinstance(result, dict) else ""

    display_name = html.escape(str(tg_user.get("first_name") or tg_user.get("username") or telegram_id))
    if ok:
        # 参加反馈（2026-10-31 根治）：带当前总人数 + 公告卡节流刷新；
        # 反馈消息走自毁，不留刷屏。旧行为「发了口令群里没反应」是用户亲报缺陷。
        count = None
        counter = getattr(lot, "participant_count", None)
        if counter is not None:
            try:
                count = counter(db, round_obj.id)
            except Exception:
                logger.exception("lottery participant_count failed")
        count_text = f"，当前共 {count} 人参与" if count is not None else ""
        _send_lottery_feedback(db, chat_id, f"🎉 {display_name} 参加成功{count_text}，祝你好运！")
        _refresh_lottery_announce(db, lot, round_obj.id)
    elif reason == "already":
        # 重复发口令也要有反馈（用户亲定），同样带人数、同样自毁
        count = None
        counter = getattr(lot, "participant_count", None)
        if counter is not None:
            try:
                count = counter(db, round_obj.id)
            except Exception:
                logger.exception("lottery participant_count failed")
        count_text = f"，当前共 {count} 人参与" if count is not None else ""
        _send_lottery_feedback(db, chat_id, f"🎲 {display_name} 已参加过本轮抽奖{count_text}，请等开奖啦")
    elif reason == "full":
        _send_lottery_feedback(db, chat_id, f"名额已满，{display_name} 下次再来")
    elif reason in ("closed", "disabled"):
        pass  # 活动已结束：静默
    elif reason in ("blacklisted", "too_new", "rate_limited", "creator"):
        pass  # 资格不符：静默，不打草惊蛇
    else:
        logger.warning("lottery password join unexpected reason=%s", reason)
    return True


def _send_claim_feedback(db, chat_id, user, amount: int, remaining_count: int) -> None:
    """抢红包成功后的群内反馈（旁路：失败只记日志，绝不影响领取结果）。

    反馈消息按 bot_msg_autodelete_seconds（默认 30 秒）自毁；
    红包消息本体永不在此调度自毁。
    """
    try:
        claimer_name = html.escape(str(user.username or "朋友"))
        text = redpacket_common.claim_feedback_text(claimer_name, amount, remaining_count)
        ok, err, feedback_id = sender.send_message_ex(db, chat_id, text)
        if not ok:
            logger.warning("tg redpacket claim feedback send failed: chat=%s err=%s", chat_id, err)
            return
        if feedback_id:
            delay = ephemeral.bot_msg_delay_seconds(db)
            if delay > 0:
                ephemeral.schedule_delete(chat_id, feedback_id, delay)
    except Exception:
        logger.warning("tg redpacket claim feedback failed: chat=%s", chat_id, exc_info=True)


def _handle_redpacket_claim(db, callback_query: dict, redpacket_enabled: bool | None = None) -> None:
    """处理抢红包按钮点击（B3）。

    redpacket_enabled：调用方已批量读到的开关值；None 时自行读取。
    """
    packet_id = redpacket_common.parse_claim_data(callback_query.get("data"))
    if packet_id is None:
        logger.debug("tg unknown callback data: %s", callback_query.get("data"))
        return

    cq_id = callback_query.get("id")
    message = callback_query.get("message") or {}
    chat = message.get("chat") or {}
    chat_id = chat.get("id")
    message_id = message.get("message_id")

    def _answer(text: str, show_alert: bool = False) -> None:
        if not cq_id:
            return
        sender.answer_callback_query(db, cq_id, text, show_alert)

    if redpacket_enabled is None:
        redpacket_enabled = redpacket_common.enabled(db)
    if not redpacket_enabled:
        _answer("🧧 红包功能已关闭")
        return

    user = resolve(db, int((callback_query.get("from") or {}).get("id") or 0))
    if user is None:
        _answer("请先绑定 Telegram 后再抢红包（发送 /bind 获取绑定码）", show_alert=True)
        return

    packet = db.query(models.RedPacket).filter(models.RedPacket.id == packet_id).first()
    if packet is None:
        _answer("红包不存在")
        return

    sender_user = db.query(models.WebUser).filter(models.WebUser.id == packet.sender_id).first()
    sender_name = html.escape(str(sender_user.username)) if sender_user and sender_user.username else "未知"

    def _update_message(text: str, reply_markup: dict) -> None:
        if not chat_id or not message_id:
            logger.warning("tg redpacket message update skipped: chat_id=%s message_id=%s", chat_id, message_id)
            return
        # 本体形态分发（按回调消息形态判定，不落库记形态）：新本体是
        # 封面照片消息（message 带 photo）→ editMessageCaption；存量
        # 纯文本本体 → editMessageText 原路不变。照片路失败时回落
        # 文本编辑兜底，剩余/抢完/过期等状态更新绝不因形态问题丢失。
        if message.get("photo"):
            ok, err = sender.edit_message_caption(db, chat_id, message_id, text, reply_markup)
            if not ok:
                logger.warning("tg redpacket caption update failed, falling back to text edit: chat_id=%s message_id=%s err=%s", chat_id, message_id, err)
                ok, err = sender.edit_message_text(db, chat_id, message_id, text, reply_markup)
        else:
            ok, err = sender.edit_message_text(db, chat_id, message_id, text, reply_markup)
        if not ok:
            logger.warning("tg redpacket message update failed: chat_id=%s message_id=%s err=%s", chat_id, message_id, err)

    now = datetime.now()
    if packet.expires_at and packet.expires_at < now:
        _answer("🧧 红包已过期")
        _update_message(redpacket_common.finished_text(packet, sender_name, "expired"), {"inline_keyboard": []})
        return

    if packet.remaining_count <= 0:
        _answer("🧧 红包已被抢完")
        _update_message(redpacket_common.finished_text(packet, sender_name, "empty"), {"inline_keyboard": []})
        return

    from backend import welfare_redpacket

    try:
        result = welfare_redpacket.claim_packet(db, user, packet_id)
    except ValueError as e:
        err = str(e)
        logger.warning("tg redpacket claim rejected: packet=%s user=%s err=%s", packet_id, user.id, err)
        _answer(f"🧧 {err}")
        if "已被抢完" in err:
            _update_message(redpacket_common.finished_text(packet, sender_name, "empty"), {"inline_keyboard": []})
        elif "已过期" in err:
            _update_message(redpacket_common.finished_text(packet, sender_name, "expired"), {"inline_keyboard": []})
        return
    except Exception:
        logger.exception("tg redpacket claim failed: packet=%s user=%s", packet_id, user.id)
        _answer("🧧 抢红包失败，请稍后再试")
        return

    amount = result["amount"]
    _answer(f"🧧 恭喜抢到 {amount} 能量！")
    if chat.get("type") in ("group", "supergroup"):
        _send_claim_feedback(db, chat_id, user, amount, packet.remaining_count)
    logger.info(
        "tg redpacket claimed: packet=%s user=%s amount=%s remaining=%s",
        packet_id,
        user.id,
        amount,
        packet.remaining_count,
    )
    if packet.remaining_count <= 0:
        _update_message(redpacket_common.finished_text(packet, sender_name, "empty"), {"inline_keyboard": []})
    else:
        _update_message(redpacket_common.packet_text(packet, sender_name), redpacket_common.claim_markup(packet_id))


def _handle_reconcile_callback(db, callback_query: dict) -> None:
    """对账处置二次确认（reconcile:ask / reconcile:do / reconcile:abort）。

    每一步都重新验门：仅私聊点击 + 已绑定 + is_staff（现行口径），
    任一不满足只回拒绝卡、绝不动数据。ask 只渲染确认卡（零写入）；
    do 才执行取消资格（reconcile.execute_cancel_welfare，内部对
    每个目标再逐一复核在群状态）；abort 只回放弃卡。
    """
    from . import handlers, reconcile

    data = callback_query.get("data") or ""
    step = data.split(":", 1)[1] if ":" in data else ""
    if step not in ("ask", "do", "abort"):
        return
    cq_id = callback_query.get("id")
    from_user = callback_query.get("from") or {}
    tg_id = from_user.get("id")
    msg = callback_query.get("message") or {}
    chat = msg.get("chat") or {}
    chat_id = chat.get("id")
    message_id = msg.get("message_id")
    if not chat_id or not message_id or not tg_id:
        return
    sender.answer_callback_query(db, cq_id)
    if chat_id != int(tg_id):
        return  # 只认私聊里的点击：对账与处置都是私聊管理员动作
    user = resolve(db, int(tg_id))
    if user is None or not bool(getattr(user, "is_staff", False)):
        text = layout.card(
            layout.title("群号一致性对账"),
            [layout.row("🔒", "访问状态", "仅管理员可用")],
        )
        sender.edit_panel(db, chat_id, message_id, text,
                          handlers.back_markup(),
                          has_photo=bool(msg.get("photo")))
        return
    if step == "ask":
        text, markup = reconcile.render_confirm(db, int(tg_id))
    elif step == "abort":
        text, markup = reconcile.render_abort()
    else:
        result = reconcile.cached_result(int(tg_id))
        if result is None:
            result = reconcile.run_reconcile(db)
            reconcile.cache_result(int(tg_id), result)
        done, skipped = reconcile.execute_cancel_welfare(db, user, result)
        text, markup = reconcile.render_receipt(done, skipped)
    sender.edit_panel(db, chat_id, message_id, text, markup,
                      has_photo=bool(msg.get("photo")))

def _send_slots_group_result(db, chat_id, player_name: str, spin, dice_message_id) -> None:
    """群老虎机结算后的公开播报（旁路：失败只记日志，绝不影响结算结果）。

    结果消息与本局骰子消息都按 bot_msg_autodelete_seconds（默认 30 秒）
    自毁，与抢红包群反馈同口径；公共面板不编辑。player_name 在此转义。
    """
    from . import handlers

    try:
        delay = ephemeral.bot_msg_delay_seconds(db)
        if dice_message_id and delay > 0:
            ephemeral.schedule_delete(chat_id, dice_message_id, delay)
        text = handlers.slots_group_result_text(
            db, spin, html.escape(str(player_name)))
        ok, err, result_id = sender.send_message_ex(db, chat_id, text)
        if not ok:
            logger.warning(
                "tg slots group result send failed: chat=%s err=%s", chat_id, err)
            return
        if result_id and delay > 0:
            ephemeral.schedule_delete(chat_id, result_id, delay)
    except Exception:
        logger.warning("tg slots group result failed: chat=%s", chat_id, exc_info=True)


def _handle_slots_bet(db, callback_query: dict) -> None:
    """处理老虎机下注按钮（slots_bet:{tier}）。

    私聊：begin_spin（幂等扣注）→ sender.send_dice（TG 原生老虎机，
    结果只认返回的 dice.value）→ settle_spin，结果走 send_panel
    结果卡（原流程不变）。
    群聊（名单群；非名单群的 callback 在 router 已静默忽略）：公共
    面板谁点谁下注——扣点击者本人的能量、赢归点击者、封顶按点击者
    计，spin_key 仍是该次 callback id。骰子发到群里，结算后另发一条
    公开结果消息（点注人/注额/结果/赢额/头奖池），结果消息与骰子
    消息都按机器人消息自毁口径调度删除，绝不编辑公共面板，多人
    同点互不打架。骰子发送失败走 fail_spin 全额退注；同一 callback
    重放时 begin 返回既有局：私聊重发结果卡、群里只回 toast 摘要，
    绝不重摇、不重复扣款/赔付、不刷第二条公开消息。
    """
    from backend import slots as slots_svc
    from . import handlers

    cb_id = callback_query.get("id")
    data = callback_query.get("data") or ""
    from_user = callback_query.get("from") or {}
    tg_id = from_user.get("id")
    msg = callback_query.get("message") or {}
    chat_id = (msg.get("chat") or {}).get("id")
    if not cb_id or not tg_id or not chat_id:
        return

    def _answer(text: str, show_alert: bool = False) -> None:
        sender.answer_callback_query(db, cb_id, text=text, show_alert=show_alert)

    in_group = int(chat_id) != int(tg_id)
    origin = "group" if in_group else "private"

    if in_group:
        from backend.tg_bot import router as bot_router

        # 匿名管理员点按钮：callback 的 from 是 GroupAnonymousBot 固定
        # id（同 router 消息路径 _is_anonymous_admin 的 from 判据），
        # 认不出本人是谁——不扣钱、不发骰子，回匿名说明。
        if int(tg_id) == bot_router.GROUP_ANONYMOUS_BOT_ID:
            _answer(bot_router.ANON_ADMIN_TEXT, show_alert=True)
            return
        if not slots_svc.group_enabled(db):
            _answer("🎰 群里老虎机已关闭，可以私聊我玩", show_alert=True)
            return
        # 限流：复用 router 的用户级命令限流口径，按点击者计（防连点刷注）
        cfg = policy.read_bot_config(db)
        if not bot_router._user_cmd_allow(
                int(tg_id), "slots_bet", policy.rate_limit_seconds(cfg)):
            _answer("操作太快了，稍后再试", show_alert=True)
            return

    try:
        tier = int(data.split(":", 1)[1])
    except (IndexError, ValueError):
        _answer("注额档位无效", show_alert=True)
        return

    user = resolve(db, int(tg_id))
    if user is None:
        if in_group:
            _answer("请先私聊机器人绑定账号后再玩老虎机", show_alert=True)
        else:
            _answer("请先绑定账号后再玩老虎机", show_alert=True)
        return

    spin_key = f"tgcq:{cb_id}"
    try:
        begin = slots_svc.begin_spin(db, user, tier, spin_key, origin=origin)
    except Exception:
        logger.exception("slots begin failed: user=%s tier=%s", user.id, tier)
        db.rollback()
        _answer("系统繁忙，请稍后再试", show_alert=True)
        return
    if not begin.get("ok"):
        extra = {k: v for k, v in begin.items() if k not in ("ok", "reason")}
        _answer(
            slots_svc.reason_message(begin.get("reason") or "", **extra),
            show_alert=True)
        return

    spin = begin["spin"]
    if begin.get("duplicate"):
        if spin.status != "settled":
            _answer("上一局结算中，请稍候", show_alert=True)
            return
        if in_group:
            total = int(spin.base_payout or 0) + int(spin.pool_payout or 0)
            summary = f"+{total} 能量" if total > 0 else "未中奖"
            _answer(f"本局已结算（{summary}）")
        else:
            # 私聊重放只回 toast，绝不再发结果卡：结果卡在首轮处理时
            # 已经发过，重投递整批时再发一份，用户看到的就是「点主
            # 界面又来一张一模一样的老虎机卡」的死循环假象
            # （2026-10-30 真机实证：一局 spin、两张同卡）。
            _answer("本局已结算")
        return

    ok, err, dice_value, dice_mid = sender.send_dice(db, chat_id, "🎰")
    if not ok or dice_value is None:
        logger.warning("slots sendDice failed: spin=%s err=%s", spin.id, err)
        try:
            slots_svc.fail_spin(db, spin, "dice_failed", origin=origin)
        except Exception:
            logger.exception("slots fail_spin failed: spin=%s", spin.id)
            db.rollback()
        _answer("老虎机发送失败，注额已退回", show_alert=True)
        return

    try:
        result = slots_svc.settle_spin(db, spin, dice_value, origin=origin)
    except Exception:
        logger.exception("slots settle failed: spin=%s", spin.id)
        db.rollback()
        _answer("结算异常，请联系管理员", show_alert=True)
        return
    spin = result["spin"]
    total = int(result["base_payout"]) + int(result["pool_payout"])
    if result["pool_payout"] > 0:
        _answer(f"🎉 777 头奖！+{total} 能量")
    elif total > 0:
        _answer(f"🎉 中奖 +{total} 能量")
    else:
        _answer("未中奖，再接再厉")
    if in_group:
        player_name = str(
            from_user.get("first_name") or from_user.get("username") or tg_id)
        _send_slots_group_result(db, chat_id, player_name, spin, dice_mid)
    else:
        sender.send_panel(
            db, chat_id, handlers.slots_result_text(db, spin),
            handlers.slots_markup(db))
