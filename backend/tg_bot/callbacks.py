"""callback_query 处理：主菜单按钮（menu:*）+ 抢红包按钮（redpacket_claim:{id}）+ 抽奖参加按钮（lottery_join:{round_id}）。"""
from __future__ import annotations

import html
import logging
from datetime import datetime

from backend import models
from . import redpacket_common, sender
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
    if data.startswith("redpacket_claim:"):
        _handle_redpacket_claim(db, callback_query, redpacket_enabled)
        return
    if data.startswith("lottery_join:"):
        _handle_lottery_join(db, callback_query)
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

    ok, err = sender.edit_message_text(db, chat_id, message_id, text, markup)
    if not ok:
        logger.debug("tg menu edit failed: action=%s err=%s", action, err)


def _render_menu(db, tg_user: dict, tg_id: int, chat_id: int, is_private: bool,
                 action: str) -> tuple[str, dict | None]:
    """渲染菜单页面，返回 (text, markup)。未知 action 返回 None 标记。"""
    from . import handlers

    text: str
    markup: dict | None
    if action == "main":
        text, markup = handlers.menu_main(db, tg_user, is_private)
    elif action == "checkin":
        text = handlers.handle_checkin(db, tg_user, chat_id, "")
        markup = handlers.back_markup()
    elif action == "points":
        text = handlers.handle_points(db, tg_user, chat_id, "")
        markup = handlers.back_markup()
    elif action == "lottery":
        reply = handlers.handle_lottery(db, tg_user, chat_id, "", False)
        if isinstance(reply, tuple):
            text, markup = reply
        else:
            text, markup = reply, handlers.back_markup()
    elif action == "bind":
        user = resolve(db, int(tg_id))
        if user is not None:
            text = "✅ 你的账号已绑定，无需重复绑定；如需更换绑定请联系客服解绑。"
        else:
            text = handlers._bind_guide_text()
        markup = handlers.back_markup()
    elif action == "help":
        text = handlers.handle_help(db, tg_user, chat_id, "")
        markup = handlers.back_markup()
    elif action == "redpacket":
        text = (
            "🧧 <b>发红包</b>\n"
            "\n"
            f"用法：{handlers._cmd('/redpacket')} <i>总积分 个数</i>\n"
            f"例如：{handlers._cmd('/redpacket 100 10')}（100 积分分成 10 个）\n"
            "\n"
            "💡 请直接发送命令发红包（按钮不支持输入参数）"
        )
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
        elif reason == "already":
            _answer_callback(db, cb_id, "你已参加过本轮抽奖")
        elif reason == "full":
            _answer_callback(db, cb_id, "名额已满，下次再来")
        elif reason in ("closed", "disabled"):
            _answer_callback(db, cb_id, "本轮抽奖已结束")
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
    同一用户重复发送口令只算一次（join_round 返回 already 时静默，不刷屏）。
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
        sender.send_message(db, chat_id, f"🎉 {display_name} 参加成功，祝你好运！")
    elif reason == "already":
        pass  # 重复发送口令：静默，不刷屏
    elif reason == "full":
        sender.send_message(db, chat_id, f"名额已满，{display_name} 下次再来")
    elif reason in ("closed", "disabled"):
        pass  # 活动已结束：静默
    elif reason in ("blacklisted", "too_new", "rate_limited"):
        pass  # 资格不符：静默，不打草惊蛇
    else:
        logger.warning("lottery password join unexpected reason=%s", reason)
    return True


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
    _answer(f"🧧 恭喜抢到 {amount} 积分！")
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
