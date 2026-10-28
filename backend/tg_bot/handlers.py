from __future__ import annotations

import html
import logging
from datetime import datetime

from backend.integrations import store
from backend.models import SubscriptionPlan, TgBindCode, UserSubscription, WebUser
from backend.tg_bot import login_token
from backend.tg_bot import redpacket_common
from backend.tg_bot.identity import resolve

logger = logging.getLogger(__name__)


def _lottery():
    """惰性导入 lottery 模块（G1 契约，并行任务尚未合入时返回 None）。"""
    try:
        from backend import lottery

        return lottery
    except ImportError:
        return None


def _cmd(code: str) -> str:
    """命令 chip：<code> 包裹后 Telegram 客户端可一键点击发送。"""
    return f"<code>{code}</code>"


def _command_list() -> str:
    return (
        "📋 <b>可用命令</b>\n"
        f"▪️ {_cmd('/start')}　欢迎与快捷入口\n"
        f"▪️ {_cmd('/help')}　帮助\n"
        f"▪️ {_cmd('/bind')}　绑定 Telegram\n"
        f"▪️ {_cmd('/checkin')}　每日签到\n"
        f"▪️ {_cmd('/points')}　查积分\n"
        f"▪️ {_cmd('/redpacket')}　发红包：{_cmd('/redpacket')} <i>总积分 个数</i>\n"
        f"▪️ {_cmd('/lottery')}　抽奖\n"
        f"▪️ {_cmd('/bindings')}　查网盘绑定\n"
        f"▪️ {_cmd('/transfers')}　查转存进度\n"
        f"▪️ {_cmd('/whyplay')}　为什么播不了：{_cmd('/whyplay')} <i>片名</i>\n"
        f"▪️ {_cmd('/wants')}　查想看"
    )


def _bind_guide_text() -> str:
    return (
        "🔗 <b>如何绑定账号</b>\n"
        "\n"
        "1️⃣ 登录网站 Dashboard\n"
        "2️⃣ 点击「<b>绑定 Telegram</b>」按钮\n"
        "3️⃣ 复制弹窗中显示的 <b>6 位绑定码</b>\n"
        "4️⃣ 回到这里发送：/bind 绑定码\n"
        "\n"
        "🎁 <b>绑定后可收到</b>\n"
        "• 🔔 求片进度通知\n"
        "• ⏰ 订阅到期提醒\n"
        "• 📣 系统公告推送"
    )


def _bind_success_text(db, user) -> str:
    username = html.escape(str(user.username or ""))
    now = datetime.now()
    sub = (
        db.query(UserSubscription)
        .filter(
            UserSubscription.user_id == user.id,
            UserSubscription.status == "active",
            UserSubscription.end_date > now,
        )
        .order_by(UserSubscription.end_date.desc())
        .first()
    )
    plan_name = "无"
    if sub:
        plan = db.query(SubscriptionPlan).filter(SubscriptionPlan.id == sub.plan_id).first()
        if plan:
            plan_name = html.escape(str(plan.name))
    has_sub = sub is not None
    return (
        "✅ <b>绑定成功！</b>\n"
        "\n"
        f"👤 用户名：{username}\n"
        f"📦 订阅服务：{plan_name}\n"
        f"{'✅' if has_sub else '❌'} 订阅状态：{'有' if has_sub else '无'}有效订阅\n"
        "\n"
        "🔔 <b>您将收到</b>\n"
        "• 求片进度通知\n"
        "• 订阅到期提醒\n"
        "• 系统公告推送"
    )


# 绑定码只有 6 位数字：每个 Telegram 账号 10 分钟内最多尝试这么多次，防止枚举他人绑定码
# （绑定成功即可用 /start 一键免密登录该网页账号，等同账号接管）
BIND_ATTEMPTS_MAX = 5
BIND_ATTEMPTS_WINDOW = 600
# 单 TG 账号限流挡不住「多个 TG 账号分布式枚举」：每个待验证的网页绑定码累计全站猜错次数，
# 达到上限即作废（expires_at 置为现在），网页端验证时提示「已失效，请重新生成」，不会静默 DoS。
BIND_GLOBAL_FAIL_MAX = 20


def _record_global_bind_failure(db, now) -> None:
    """一次猜错：所有待验证的网页绑定码 fail_count+1，超限的直接作废。"""
    from sqlalchemy import func

    pending = (
        TgBindCode.user_id.isnot(None),
        TgBindCode.telegram_id.is_(None),
        TgBindCode.used_at.is_(None),
        TgBindCode.expires_at > now,
    )
    try:
        db.query(TgBindCode).filter(*pending).update(
            {TgBindCode.fail_count: func.coalesce(TgBindCode.fail_count, 0) + 1},
            synchronize_session=False,
        )
        n = db.query(TgBindCode).filter(
            *pending, TgBindCode.fail_count >= BIND_GLOBAL_FAIL_MAX,
        ).update({TgBindCode.expires_at: now}, synchronize_session=False)
        db.commit()
        if n:
            logger.warning("tg 绑定码全站猜错次数超限，已作废 %s 个待验证绑定码", n)
    except Exception as exc:  # noqa: BLE001
        db.rollback()
        logger.error("记录绑定码猜错次数失败：%s", exc)


def verify_bind_code(db, tg_user_id: int, chat_id: int, code: str) -> str | None:
    from backend.ratelimit import check_rate_limit

    allowed, _retry = check_rate_limit(
        f"tg_bind_attempt:{tg_user_id}", BIND_ATTEMPTS_MAX, BIND_ATTEMPTS_WINDOW)
    if not allowed:
        logger.warning("tg 绑定码尝试过于频繁，已拒绝 tg_user_id=%s", tg_user_id)
        return "绑定码尝试过于频繁，请 10 分钟后再试"
    now = datetime.now()
    record = (
        db.query(TgBindCode)
        .filter(
            TgBindCode.code == code.strip(),
            TgBindCode.user_id.isnot(None),
            TgBindCode.telegram_id.is_(None),
            TgBindCode.used_at.is_(None),
            TgBindCode.expires_at > now,
        )
        .order_by(TgBindCode.id.desc())
        .first()
    )
    if not record:
        _record_global_bind_failure(db, now)
        return None
    user = db.query(WebUser).filter(WebUser.id == record.user_id).first()
    if not user:
        return None
    other = (
        db.query(WebUser)
        .filter(WebUser.telegram_id == tg_user_id, WebUser.id != user.id)
        .first()
    )
    if other:
        return "该 Telegram 账号已被其他用户绑定"
    user.telegram_id = tg_user_id
    record.telegram_id = tg_user_id
    record.used_at = now
    db.commit()
    return _bind_success_text(db, user)


def _menu_markup(db, web_user, is_private: bool) -> dict:
    """主菜单 inline 按钮。私聊时首行是一键免密登录（URL 按钮必须在 [0][0]）。"""
    rows: list[list[dict]] = []
    if is_private:
        url = login_token.build_login_url(db, web_user)
        if url:
            rows.append([{"text": "🚀 一键免密进入控制面板", "url": url}])
    rows.append([
        {"text": "📅 签到", "callback_data": "menu:checkin"},
        {"text": "✨ 积分", "callback_data": "menu:points"},
    ])
    rows.append([
        {"text": "🎲 抽奖", "callback_data": "menu:lottery"},
        {"text": "🧧 发红包", "callback_data": "menu:redpacket"},
    ])
    rows.append([
        {"text": "🔗 我的绑定", "callback_data": "menu:bindings"},
        {"text": "📦 转存进度", "callback_data": "menu:transfers"},
    ])
    rows.append([
        {"text": "❓ 为什么播不了", "callback_data": "menu:whyplay"},
        {"text": "⭐ 我的想看", "callback_data": "menu:wants"},
    ])
    rows.append([
        {"text": "🔗 绑定", "callback_data": "menu:bind"},
        {"text": "❓ 帮助", "callback_data": "menu:help"},
    ])
    return {"inline_keyboard": rows}


def back_markup() -> dict:
    """「返回主菜单」按钮，供菜单子页面复用。"""
    return {"inline_keyboard": [[{"text": "‹ 返回主菜单", "callback_data": "menu:main"}]]}


def menu_main(db, tg_user: dict, is_private: bool) -> tuple[str, dict]:
    """主菜单卡片（文本, 按钮），供 /start 与 menu:main 回调复用。"""
    web_user = resolve(db, int(tg_user.get("id") or 0))
    if web_user is None:
        text = (
            "🏠 <b>主菜单</b>\n"
            "\n"
            "还没有绑定账号，绑定后解锁签到、积分、抽奖、红包等功能。"
        )
        markup = {"inline_keyboard": [
            [{"text": "🔗 绑定账号", "callback_data": "menu:bind"}],
            [{"text": "❓ 帮助", "callback_data": "menu:help"}],
        ]}
        return text, markup
    username = html.escape(str(web_user.username or ""))
    text = (
        "🏠 <b>主菜单</b>\n"
        "\n"
        f"👤 当前账号：{username}\n"
        "\n"
        "请选择功能："
    )
    return text, _menu_markup(db, web_user, is_private)


def handle_start(db, tg_user: dict, chat_id: int, args: str) -> str | tuple[str, dict | None]:
    site_name = html.escape(str(store.get_value(db, "site_name", "Aetrix")))
    name = html.escape(str(tg_user.get("first_name") or "朋友"))
    telegram_id = tg_user.get("id")
    web_user = resolve(db, int(telegram_id)) if telegram_id else None
    is_private = (chat_id == telegram_id)

    if web_user:
        username = html.escape(str(web_user.username or ""))
        text = (
            f"✨ <b>欢迎回到{site_name}</b> ✨\n"
            "\n"
            f"👋 亲爱的 <b>{name}</b>\n"
            f"👤 当前账号：{username}\n"
            "\n"
            f"{_command_list()}"
        )
        if not is_private:
            # 一键免密登录链接 = 账号凭据：只在与本人的私聊里下发（私聊 chat_id == 用户 id）。
            # 群里发出去，群内任何人先点就以该用户身份登录。
            text += "\n\n🔐 一键免密登录请私聊我发送 /start"
            return text
        return text, _menu_markup(db, web_user, is_private=True)

    text = (
        f"✨ <b>欢迎回到{site_name}</b> ✨\n"
        "\n"
        f"👋 亲爱的 <b>{name}</b>，你的积分、抽奖、签到，都在这里等你。\n"
        "\n"
        "🎁 <b>公益服功能</b>（签到 / 积分 / 红包 / 抽奖）需要先绑定 Telegram，1 分钟搞定：\n"
        "1️⃣ 在网页端登录 → <b>个人中心</b> → <b>绑定 Telegram</b> 获取 6 位绑定码\n"
        "2️⃣ 把绑定码发给我即可完成绑定\n"
        "\n"
        f"{_command_list()}"
    )
    markup = {"inline_keyboard": [
        [{"text": "🔗 绑定账号", "callback_data": "menu:bind"}],
        [{"text": "❓ 帮助", "callback_data": "menu:help"}],
    ]}
    return text, markup


def handle_help(db, tg_user: dict, chat_id: int, args: str) -> str:
    return (
        "📖 <b>使用帮助</b>\n"
        "\n"
        "🤖 本机器人用于公益服快捷操作：签到领积分、查积分、发红包、抽奖，以及接收求片进度、订阅到期等通知。\n"
        "\n"
        f"{_command_list()}\n"
        "\n"
        f"{_bind_guide_text()}\n"
        "\n"
        "💬 如遇问题，请在网页端联系客服。"
    )


def handle_bind(db, tg_user: dict, chat_id: int, args: str) -> str:
    telegram_id = tg_user.get("id")
    if not telegram_id:
        return "暂时无法获取你的 Telegram ID，请稍后再试"
    if resolve(db, int(telegram_id)):
        return "✅ 你的账号已绑定，无需重复绑定；如需更换绑定请联系客服解绑。"
    if not args.strip():
        return _bind_guide_text()
    msg = verify_bind_code(db, int(telegram_id), chat_id, args.strip())
    return msg if msg else "绑定码无效或已过期，请在网页端重新获取绑定码"


def handle_checkin(db, tg_user: dict, chat_id: int, args: str) -> str:
    from backend.api.economy import _do_checkin_core
    from fastapi import HTTPException

    user = resolve(db, int(tg_user.get("id") or 0))
    if user is None:
        return _bind_guide_text()
    try:
        award = _do_checkin_core(db, user)
    except HTTPException as e:
        return html.escape(str(e.detail))
    except Exception:
        return "签到失败，请稍后再试"
    if award["points_awarded"] > 0:
        text = (
            "📅 <b>签到成功！</b>\n"
            "\n"
            f"💰 积分 <b>+{award['points_awarded']}</b>\n"
            f"🔥 已连续签到 <b>{award['streak']}</b> 天"
        )
    else:
        text = (
            "📅 <b>签到成功！</b>\n"
            "\n"
            f"🔥 已连续签到 <b>{award['streak']}</b> 天\n"
            "💡 积分仅限公益服用户领取"
        )
    if award.get("vitality_gained", 0) > 0:
        text += f"\n⚡ 活力值 +{award['vitality_gained']}"
    return text


def handle_points(db, tg_user: dict, chat_id: int, args: str) -> str:
    user = resolve(db, int(tg_user.get("id") or 0))
    if user is None:
        return _bind_guide_text()
    points = int(user.points or 0)
    text = f"✨ 当前积分：{points}"
    try:
        from backend import vitality as _vitality

        cfg = _vitality.get_vitality_config(db)
        if cfg.get("enabled") and getattr(user, "is_welfare", False):
            v = int(getattr(user, "vitality", None) or 0)
            vmax = int(cfg.get("max", 14) or 14)
            filled = round(v / vmax * 10) if vmax > 0 else 0
            bar = "▓" * filled + "░" * (10 - filled)
            text += (
                f"\n\n⚡ <b>活力值</b> {v}/{vmax}\n"
                f"<code>{bar}</code>"
            )
    except Exception:
        pass
    return text


def handle_redeem(db, tg_user: dict, chat_id: int, args: str) -> str:
    from backend.api.economy import _redeem_exchange_core
    from fastapi import HTTPException

    user = resolve(db, int(tg_user.get("id") or 0))
    if user is None:
        return _bind_guide_text()
    code_str = (args or "").strip()
    if not code_str:
        return (
            "🎟️ <b>兑换码兑换</b>\n"
            "\n"
            f"用法：{_cmd('/redeem')} <i>兑换码</i>\n"
            f"例如：{_cmd('/redeem ABCD1234')}"
        )
    try:
        result = _redeem_exchange_core(db, user, code_str)
    except HTTPException as e:
        return html.escape(str(e.detail))
    except Exception:
        return "兑换失败，请稍后再试"
    return "🎁 " + html.escape(str(result.get("message", "兑换成功")))


def handle_lottery(db, tg_user: dict, chat_id: int, args: str, is_group: bool) -> str | tuple[str, dict | None]:
    user = resolve(db, int(tg_user.get("id") or 0))
    if user is None:
        return _bind_guide_text()

    lot = _lottery()
    try:
        enabled = bool(lot.is_enabled(db)) if lot is not None else False
    except Exception:
        logger.warning("lottery.is_enabled failed", exc_info=True)
        enabled = False
    if not enabled:
        return "🎲 抽奖功能暂未开启"

    if is_group:
        try:
            round = lot.get_active_round(db, chat_id)
        except Exception:
            logger.warning("获取群抽奖轮次失败", exc_info=True)
            return "🎲 抽奖功能暂未开启"
        if round is None:
            return "🎲 本群暂无进行中的抽奖，敬请期待"

        round_id = getattr(round, "id", None)
        title = html.escape(str(getattr(round, "title", None) or "未命名抽奖"))
        prize_raw = getattr(round, "prize_name", None) or getattr(round, "prize_desc", None) or "待定"
        prize = html.escape(str(prize_raw))
        draw_at = getattr(round, "draw_at", None)
        if isinstance(draw_at, datetime):
            draw_at_text = draw_at.strftime("%m-%d %H:%M")
        else:
            draw_at_text = "待定"
        entry_count = getattr(round, "entry_count", None)
        if entry_count is None:
            entries = getattr(round, "entries", None)
            if entries is not None:
                try:
                    entry_count = len(entries)
                except TypeError:
                    entry_count = None
        entry_text = "—" if entry_count is None else str(entry_count)
        max_entries = getattr(round, "max_entries", None)
        suffix = f"/{max_entries}" if max_entries else ""

        text = (
            "🎲 <b>群抽奖</b>\n"
            "\n"
            f"📌 <b>{title}</b>\n"
            f"🎁 奖品：{prize}\n"
            f"⏰ 开奖：{draw_at_text}\n"
            f"👥 已参加：<b>{entry_text}{suffix}</b>\n"
            "\n"
            "👇 点击下方按钮参加"
        )
        reply_markup = {
            "inline_keyboard": [
                [{"text": "🎲 参加抽奖", "callback_data": f"lottery_join:{round_id}"}]
            ]
        }
        return (text, reply_markup)

    from sqlalchemy import text

    sql = (
        "SELECT r.id, r.title, r.status, r.draw_at, "
        "(SELECT COUNT(*) FROM lottery_round_winners w WHERE w.round_id = r.id AND w.user_id = :uid) AS won "
        "FROM lottery_rounds r "
        "JOIN lottery_round_entries e ON e.round_id = r.id AND e.user_id = :uid "
        "ORDER BY r.id DESC LIMIT 10"
    )
    try:
        rows = db.execute(text(sql), {"uid": user.id}).fetchall()
    except Exception:
        return "🎲 你还没有参加过抽奖"
    if not rows:
        return "🎲 你还没有参加过抽奖"

    status_map = {
        "open": "进行中",
        "drawing": "开奖中",
        "done": "已结束",
        "cancelled": "已取消",
    }
    lines = ["🎲 <b>我的抽奖记录</b>", ""]
    for row in rows:
        row_title = html.escape(str(getattr(row, "title", None) or "未命名抽奖"))
        row_status = getattr(row, "status", None)
        status_text = status_map.get(row_status, html.escape(str(row_status)))
        won = getattr(row, "won", 0) or 0
        result = "🏆 <b>已中奖</b>" if won > 0 else "未中奖/待开奖"
        lines.append(f"📌 <b>{row_title}</b>\n　　状态：{status_text} ｜ {result}")
    return "\n".join(lines)


def handle_redpacket(db, tg_user: dict, chat_id: int, args: str,
                     update_id: int | None = None) -> str | tuple[str, dict | None]:
    # 1. 总开关：关闭时直接提示
    if not redpacket_common.enabled(db):
        return "🧧 红包功能已关闭"
    # 2. 身份：未绑定则引导绑定
    user = resolve(db, int(tg_user.get("id") or 0))
    if user is None:
        return _bind_guide_text()
    # 3. 参数：取前两个并转 int，数量不对或转换失败则提示用法
    parts = args.split()[:2]
    if len(parts) != 2:
        return (
            "用法：/redpacket <i>总积分 个数</i>\n"
            f"例如：{_cmd('/redpacket 100 10')}（100 积分分成 10 个）"
        )
    try:
        total, count = int(parts[0]), int(parts[1])
    except ValueError:
        return (
            "用法：/redpacket <i>总积分 个数</i>\n"
            f"例如：{_cmd('/redpacket 100 10')}"
        )
    # 4. 发红包：复用后端校验与扣减逻辑
    # P1 幂等：以 tg:<update_id> 作为幂等键（后端唯一约束），
    # TG poller at-least-once 重放同一 update 时，第二次 insert 撞唯一约束
    # → 回滚并返回已存在的包，不再重复扣款（防重放双花）。
    # update_id 为 None（旧调用方/非 TG 场景）时不做幂等，保持向后兼容。
    from backend import welfare_redpacket
    idempotency_key = f"tg:{update_id}" if update_id is not None else None
    try:
        packet = welfare_redpacket.send_packet(
            db, user, total, count, idempotency_key=idempotency_key)
    except ValueError as e:
        return f"🧧 {html.escape(str(e))}"
    except Exception:
        return "🧧 发红包失败，请稍后再试"
    # 5. 成功：返回红包正文与领取按钮
    sender_name = html.escape(str(user.username or "朋友"))
    text = redpacket_common.packet_text(packet, sender_name)
    return (text, redpacket_common.claim_markup(packet.id))


# ---- P1 自助查询（只读）：统一先拦群聊、再验绑定，最后调 queries 只读渲染 ----
# 群聊里查个人数据会把结果公开给全群：一律只许私聊（私聊 chat_id == tg id）。
def _p1_user(db, tg_user: dict, chat_id: int):
    """P1 前置：返回 (user, error_text)，error_text 非空时直接回复它。"""
    from backend.tg_bot import queries

    tg_id = tg_user.get("id")
    if not tg_id or chat_id != tg_id:
        return None, queries.PRIVATE_ONLY_TEXT
    user = resolve(db, int(tg_id))
    if user is None:
        return None, _bind_guide_text()
    return user, None


def handle_bindings(db, tg_user: dict, chat_id: int, args: str) -> str:
    from backend.tg_bot import queries

    user, err = _p1_user(db, tg_user, chat_id)
    if err:
        return err
    return queries.render_bindings(db, user)


def handle_transfers(db, tg_user: dict, chat_id: int, args: str) -> str:
    from backend.tg_bot import queries

    user, err = _p1_user(db, tg_user, chat_id)
    if err:
        return err
    return queries.render_transfers(db, user)


def handle_wants(db, tg_user: dict, chat_id: int, args: str) -> str:
    from backend.tg_bot import queries

    user, err = _p1_user(db, tg_user, chat_id)
    if err:
        return err
    return queries.render_wants(db, user)


def handle_whyplay(db, tg_user: dict, chat_id: int, args: str) -> str:
    from backend.tg_bot import queries

    user, err = _p1_user(db, tg_user, chat_id)
    if err:
        return err
    return queries.diagnose_whyplay(db, user, args)
