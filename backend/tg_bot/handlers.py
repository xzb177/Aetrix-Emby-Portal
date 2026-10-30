from __future__ import annotations

import html
import logging
from datetime import datetime

from backend.integrations import store
from backend.models import SubscriptionPlan, TgBindCode, UserSubscription, WebUser
from backend.tg_bot import layout, login_token
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


def _bind_guide_text() -> str:
    return layout.card(
        layout.title("如何绑定账号"),
        [
            layout.row("1️⃣", "第一步", "登录网站 Dashboard"),
            layout.row("2️⃣", "第二步", "点击「<b>绑定 Telegram</b>」按钮"),
            layout.row("3️⃣", "第三步", "复制弹窗中显示的 <b>6 位绑定码</b>"),
            layout.row("4️⃣", "第四步", "回到这里发送：/bind 绑定码"),
            layout.row("🔔", "绑定后可收到", "求片进度通知"),
            layout.row("⏰", "绑定后可收到", "订阅到期提醒"),
            layout.row("📣", "绑定后可收到", "系统公告推送"),
        ],
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
    return layout.card(
        layout.title("绑定成功！"),
        [
            layout.row("👤", "账号名称", username),
            layout.row("📦", "订阅服务", plan_name),
            layout.row("📊", "订阅状态", "有有效订阅" if has_sub else "无有效订阅"),
            layout.row("🔔", "您将收到", "求片进度通知、订阅到期提醒、系统公告推送"),
        ],
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


def _linked_name(tg_user: dict, fallback: str) -> str:
    """问候行用户名链接：有 Telegram id 时链到该用户，否则纯文本。"""
    name = html.escape(str(tg_user.get("first_name") or fallback or "朋友"))
    tg_id = tg_user.get("id")
    if tg_id:
        return f'<a href="tg://user?id={int(tg_id)}">{name}</a>'
    return name


def main_card_text(tg_user: dict, web_user) -> str:
    """主界面卡片正文（已批准样稿）：标语＋带用户名问候。"""
    fallback = getattr(web_user, "username", "") if web_user is not None else ""
    return (
        "✨ 想看的片，喊一声就来\n"
        f"🍉 你好呀，{_linked_name(tg_user, fallback)} 请选择功能 👇"
    )


def unbound_card_text(tg_user: dict, site_name: str) -> str:
    """未绑定主界面：同主卡骨架，信息行换成绑定状态与步骤。"""
    return layout.card(
        "✨ 想看的片，喊一声就来",
        [
            layout.row("🍉", "你好呀", f"{_linked_name(tg_user, '朋友')} 请选择功能 👇"),
            layout.row("🏠", "站点名称", site_name),
            layout.row("🔗", "账号绑定", "未绑定"),
            layout.row("1️⃣", "第一步", "在网页端登录 → <b>个人中心</b> → <b>绑定 Telegram</b> 获取 6 位绑定码"),
            layout.row("2️⃣", "第二步", "把绑定码发给我即可完成绑定"),
            layout.row("📋", "全部命令", "点左下角的 / 菜单查看"),
        ],
    )


def main_markup(db, web_user, is_private: bool) -> dict:
    """新主界面按钮：分类入口在前、重要动作整行、管理员入口仅 staff。

    私聊登录 URL 是既有硬契约：生成成功时仍必须位于 [0][0]。
    """
    if web_user is None:
        if not is_private:
            # 群里未绑定：绑定/帮助之外加老虎机围观入口（公共面板
            # 人人可看；点注时未绑定按既有逻辑只回绑定引导、不扣款）。
            return {"inline_keyboard": [
                [{"text": "🔗 绑定账号", "callback_data": "menu:bind"}],
                [
                    {"text": "🎰 老虎机", "callback_data": "menu:slots"},
                    {"text": "❓ 帮助", "callback_data": "menu:help"},
                ],
            ]}
        return {"inline_keyboard": [
            [{"text": "🔗 绑定账号", "callback_data": "menu:bind"}],
            [{"text": "❓ 帮助", "callback_data": "menu:help"}],
        ]}
    if not is_private:
        return _group_menu_markup()
    rows: list[list[dict]] = []
    url = login_token.build_login_url(db, web_user)
    if url:
        rows.append([{"text": "🚀 一键免密进入控制面板", "url": url}])
    rows.append([
        {"text": "👥 用户功能", "callback_data": "menu:user"},
        {"text": "🌐 服务器", "callback_data": "menu:server"},
    ])
    rows.append([{"text": "🎟️ 使用兑换码", "callback_data": "menu:renewcode"}])
    if bool(getattr(web_user, "is_staff", False)):
        rows.append([{"text": "👮 管理", "callback_data": "menu:admin"}])
    return {"inline_keyboard": rows}


def _group_menu_markup() -> dict:
    """群聊主菜单（2026-10-30 定版）：无登录行、无私聊专属入口。

    老虎机进第一屏；个人查询（我的绑定/转存进度/为什么播不了/
    我的想看）不进群菜单——点开会把点击者个人数据编辑进群里的
    共享消息，与命令层私聊限定口径冲突，统一回私聊（callbacks
    对这四项的群分支回「私聊使用」卡＋去私聊深链）。
    """
    rows: list[list[dict]] = [
        [
            {"text": "✅ 签到", "callback_data": "menu:checkin"},
            {"text": "✨ 能量", "callback_data": "menu:points"},
        ],
        [
            {"text": "🎰 老虎机", "callback_data": "menu:slots"},
            {"text": "🎲 抽奖", "callback_data": "menu:lottery"},
        ],
        [
            {"text": "🧧 发红包", "callback_data": "menu:redpacket"},
            {"text": "🏆 能量榜", "callback_data": "menu:rank"},
        ],
        [
            {"text": "🔥 求片榜", "callback_data": "menu:requestboard"},
            {"text": "📜 群规", "callback_data": "menu:rules"},
        ],
        [
            {"text": "👤 账号中心", "callback_data": "menu:account"},
            {"text": "❓ 帮助", "callback_data": "menu:help"},
        ],
    ]
    return {"inline_keyboard": rows}


def private_only_card() -> str:
    """私聊限定功能在群里被点到时的统一提示卡（与 callbacks 既有口径一致）。"""
    return layout.card(
        layout.title("私聊使用"),
        [layout.row("🔒", "使用方式", "这个功能请私聊机器人使用")])


def private_only_markup(db) -> dict:
    """私聊卡按钮：「💬 去私聊」深链（可解析时）＋回主界面。

    深链用户名走既有来源 tg_bind.get_bot_username（缓存配置
    telegram_bot_username，缺失时 getMe 回填）；解析不到就不挂
    URL 按钮，绝不写死或猜测用户名。
    """
    rows: list[list[dict]] = []
    username = ""
    try:
        from backend.tg_bind import get_bot_username

        username = str(get_bot_username(db) or "").lstrip("@")
    except Exception:
        username = ""
    if username:
        rows.append([{"text": "💬 去私聊", "url": f"https://t.me/{username}"}])
    rows.append(layout.home_row())
    return {"inline_keyboard": rows}


# 用户面板分组（2026-10-30 用户亲批「按钮分一下，不要全铺开」）：
# 一级只放签到直达＋五个分组入口＋整行主界面；原功能按钮按组收进二级
# 分组面板。各功能按钮的 callback 与既有门禁一律不变，只新增分组翻页
# callback（menu:group_*，不查身份、不设门禁）与返回（复用 menu:user）。
USER_PANEL_GROUPS: dict[str, dict] = {
    "media": {
        "label": "🎬 求片",
        "buttons": [
            {"text": "🎬 求片", "callback_data": "menu:request"},
            {"text": "📋 我的求片", "callback_data": "menu:myrequests"},
            {"text": "⭐ 我的想看", "callback_data": "menu:wants"},
        ],
    },
    "drive": {
        "label": "📦 网盘转存",
        "buttons": [
            {"text": "🔗 绑定网盘", "callback_data": "menu:bindings"},
            {"text": "📦 我的转存", "callback_data": "menu:transfers"},
        ],
    },
    "account": {
        "label": "🎟️ 账号权益",
        "buttons": [
            {"text": "✨ 能量", "callback_data": "menu:points"},
            {"text": "🏆 能量榜", "callback_data": "menu:rank"},
            {"text": "🎟️ 续期", "callback_data": "menu:login"},
            {"text": "🔗 绑定账号", "callback_data": "menu:bind"},
        ],
    },
    "welfare": {
        "label": "🎁 福利活动",
        "buttons": [
            {"text": "🎲 抽奖", "callback_data": "menu:lottery"},
            {"text": "🧧 发红包", "callback_data": "menu:redpacket"},
            {"text": "🎰 老虎机", "callback_data": "menu:slots"},
        ],
    },
    "help": {
        "label": "❓ 帮助中心",
        "buttons": [
            {"text": "❓ 为什么播不了", "callback_data": "menu:whyplay"},
            {"text": "❓ 帮助", "callback_data": "menu:help"},
        ],
    },
}


def _two_col(buttons: list[dict]) -> list[list[dict]]:
    """按钮按两列网格排布，落单的最后一个独占一行。"""
    return [list(buttons[i:i + 2]) for i in range(0, len(buttons), 2)]


def user_panel_markup() -> dict:
    """用户面板一级按钮：签到直达＋五个分组入口两列排，末行回主界面。"""
    rows: list[list[dict]] = [
        [
            {"text": "✅ 签到", "callback_data": "menu:checkin"},
            {"text": USER_PANEL_GROUPS["media"]["label"],
             "callback_data": "menu:group_media"},
        ],
        [
            {"text": USER_PANEL_GROUPS["drive"]["label"],
             "callback_data": "menu:group_drive"},
            {"text": USER_PANEL_GROUPS["account"]["label"],
             "callback_data": "menu:group_account"},
        ],
        [
            {"text": USER_PANEL_GROUPS["welfare"]["label"],
             "callback_data": "menu:group_welfare"},
            {"text": USER_PANEL_GROUPS["help"]["label"],
             "callback_data": "menu:group_help"},
        ],
        layout.home_row(),
    ]
    return {"inline_keyboard": rows}


def user_panel_group_text(group_key: str) -> str:
    """二级分组面板正文：只有标题行，不重复用户面板的信息行。"""
    return layout.card(layout.title(USER_PANEL_GROUPS[group_key]["label"]))


def user_panel_group_markup(group_key: str) -> dict:
    """二级分组面板按钮：该组功能按钮（两列）＋整行返回用户面板。"""
    rows = _two_col(USER_PANEL_GROUPS[group_key]["buttons"])
    rows.append([{"text": "↩ 返回用户面板", "callback_data": "menu:user"}])
    return {"inline_keyboard": rows}


def _menu_markup(db, web_user, is_private: bool) -> dict:
    """兼容旧调用：私聊返回新主界面网格，群聊返回群聊网格。"""
    return main_markup(db, web_user, is_private)


def back_markup() -> dict:
    """菜单子页统一返回入口：整行主界面按钮。"""
    return layout.back_markup()


def menu_main(db, tg_user: dict, is_private: bool) -> tuple[str, dict]:
    """主界面卡片（文本, 按钮），供 /start 与 menu:main 回调复用。"""
    web_user = resolve(db, int(tg_user.get("id") or 0))
    if web_user is None:
        site_name = html.escape(str(store.get_value(db, "site_name", "Aetrix")))
        return unbound_card_text(tg_user, site_name), main_markup(db, None, is_private)
    return main_card_text(tg_user, web_user), main_markup(db, web_user, is_private)


def handle_server(db, tg_user: dict, chat_id: int, args: str) -> str:
    """服务器面板：只读复用管理端 bot_status，不新增状态口径。"""
    site_name = html.escape(str(store.get_value(db, "site_name", "Aetrix")))
    rows = [layout.row("🏠", "站点名称", site_name)]
    try:
        from backend.api.tg_bot_admin import bot_status

        status = bot_status(db)
        rows += [
            layout.row("🤖", "机器人", "运行中" if status.get("poller_active") else "未运行"),
            layout.row("👥", "已绑定用户", status.get("bound_users", 0)),
            layout.row("💓", "最近心跳", status.get("last_heartbeat") or "暂无"),
        ]
        if status.get("last_error"):
            rows.append(layout.row("⚠️", "最近错误", html.escape(str(status["last_error"]))[:300]))
    except Exception:
        rows.append(layout.row("🤖", "机器人", "状态暂不可用"))
    return layout.card(layout.title("服务器"), rows)


def handle_admin(db, tg_user: dict, chat_id: int, args: str) -> str:
    """管理面板：仅网站 staff 可见，入口只挂既有只读/治理动作。"""
    user = resolve(db, int(tg_user.get("id") or 0))
    if user is None:
        return _bind_guide_text()
    if not bool(getattr(user, "is_staff", False)):
        return layout.card(
            layout.title("管理面板"),
            [layout.row("🔒", "访问状态", "仅管理员可用")],
        )
    return layout.card(
        layout.title("管理面板！", user.username),
        [
            layout.row("👤", "管理员账号", html.escape(str(user.username or ""))),
            layout.row("🛡️", "权限", "网站管理员"),
            layout.row("🌐", "服务器", "可从下方按钮查看机器人状态"),
            layout.row("📋", "治理审计", "私聊发送 /govlog（仅 Lv.6 且公益服）"),
        ],
    )


def handle_rankpush(db, tg_user: dict, chat_id: int, args: str) -> str:
    """管理员手动推送观影榜海报到交流群（/rankpush [week]），供验收。

    身份门照 /admin 口径：TG 绑定的站账号且 is_staff；未绑定或非管理员
    一律回同一张拒绝卡、不推送、不生成海报。默认推今日榜，args=week 推周榜。
    实际推送走 tg_bot.rankpush.push_rank_board（与定时推送同一条路，
    含总开关/空榜/未配群判定与发分幂等）。
    """
    user = resolve(db, int(tg_user.get("id") or 0))
    if user is None or not bool(getattr(user, "is_staff", False)):
        return layout.card(
            layout.title("观影榜推送"),
            [layout.row("🔒", "访问状态", "仅管理员可用")],
        )
    from backend.tg_bot import rankpush

    period = "week" if str(args or "").strip().lower() == "week" else "day"
    label = "本周观影榜" if period == "week" else "今日观影榜"
    res = rankpush.push_rank_board(db, period)
    reason = res.get("reason")
    if res.get("pushed"):
        rows = [
            layout.row("✅", "推送", f"{label}已推送到交流群"),
            layout.row("👥", "上榜", f"{len(res.get('rows') or [])} 人"),
        ]
        if res.get("rewards"):
            total = sum(g["amount"] for g in res["rewards"])
            rows.append(layout.row("🎁", "榜单奖励", f"已发放 {total} 能量"))
        return layout.card(layout.title("观影榜推送", user.username), rows)
    hint = {
        "disabled": "海报推送已关闭（bot_rankpush_enabled），先在管理后台打开",
        "empty": f"{label}暂时没人观影，榜是空的，没有推送",
        "no_group": "还没配置交流群（bot_group_ids），无法推送",
    }.get(reason or "", "推送失败，请看服务日志")
    return layout.card(
        layout.title("观影榜推送", user.username),
        [layout.row("⚠️", "未推送", hint)],
    )


def admin_markup() -> dict:
    return {"inline_keyboard": [
        [
            {"text": "🌐 服务器", "callback_data": "menu:server"},
            {"text": "📋 治理审计", "callback_data": "menu:govlog"},
        ],
        layout.home_row(),
    ]}


def handle_start(db, tg_user: dict, chat_id: int, args: str) -> str | tuple[str, dict | None]:
    site_name = html.escape(str(store.get_value(db, "site_name", "Aetrix")))
    telegram_id = tg_user.get("id")
    web_user = resolve(db, int(telegram_id)) if telegram_id else None
    is_private = (chat_id == telegram_id)

    if web_user:
        if not is_private:
            from backend.tg_bot.features import compact_dashboard

            greet = tg_user.get("first_name") or web_user.username
            # 正文仍是现行九行信息卡（一字不改），补挂群菜单按钮：
            # 此前只回纯文本，群里没有任何按钮能点进功能。
            return (compact_dashboard(db, web_user, display_name=greet),
                    main_markup(db, web_user, False))
        return main_card_text(tg_user, web_user), main_markup(db, web_user, True)

    return unbound_card_text(tg_user, site_name), main_markup(db, None, is_private)


def handle_help(db, tg_user: dict, chat_id: int, args: str) -> str:
    return layout.card(
        layout.title("使用帮助"),
        [
            layout.row("🤖", "主要用途", "公益服快捷操作：签到领能量、查能量、发红包、抽奖，以及接收求片进度、订阅到期等通知"),
            layout.row("📋", "全部命令", "点左下角的 / 菜单查看"),
            layout.row("💬", "遇到问题", "请在网页端联系客服"),
        ],
        _bind_guide_text().split("\n"),
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
    rows = [layout.row("📅", "连签天数", f"已连续签到 <b>{award['streak']}</b> 天")]
    if award["points_awarded"] > 0:
        rows.insert(0, layout.row("💰", "本次能量", f"<b>+{award['points_awarded']}</b>"))
    else:
        rows.append(layout.row("💡", "本次能量", "能量仅限公益服用户领取"))
    if award.get("vitality_gained", 0) > 0:
        rows.append(layout.row("⚡", "活力数值", f"+{award['vitality_gained']}"))
    return layout.card(layout.title("签到成功！"), rows)


def handle_points(db, tg_user: dict, chat_id: int, args: str) -> str:
    user = resolve(db, int(tg_user.get("id") or 0))
    if user is None:
        return _bind_guide_text()
    points = int(user.points or 0)
    rows = [layout.row("✨", "能量余额", points)]
    try:
        from backend import vitality as _vitality

        cfg = _vitality.get_vitality_config(db)
        if cfg.get("enabled") and getattr(user, "is_welfare", False):
            v = int(getattr(user, "vitality", None) or 0)
            vmax = int(cfg.get("max", 14) or 14)
            filled = round(v / vmax * 10) if vmax > 0 else 0
            bar = "▓" * filled + "░" * (10 - filled)
            rows.append(layout.row("⚡", "活力数值", f"{v}/{vmax} <code>{bar}</code>"))
    except Exception:
        pass
    return layout.card(layout.title("我的能量"), rows)


def handle_chatpoints(db, tg_user: dict, chat_id: int, args: str) -> str:
    user = resolve(db, int(tg_user.get("id") or 0))
    if user is None:
        return _bind_guide_text()
    from backend import points as points_mod

    status = points_mod.get_chat_points_status(db, user.id)
    earned = status["earned_today"]
    cap = status["daily_cap"]
    per_msg = status["per_msg"]
    if per_msg == 0:
        return layout.card(
            layout.title("群发言能量"),
            [
                layout.row("💬", "发言奖励", "当前已关闭，在群里发言暂不发放能量"),
                layout.row("🏆", "累计能量", f"<b>{status['total_earned']}</b> 能量"),
            ],
        )
    filled = round(earned / cap * 10) if cap > 0 else 0
    bar = "▓" * filled + "░" * (10 - filled)
    rows = [
        layout.row("📅", "今日已得", f"<b>{earned}</b> / {cap} 能量 <code>{bar}</code>"),
        layout.row("🏆", "累计能量", f"<b>{status['total_earned']}</b> 能量"),
        layout.row("📏", "发言规则", f"在公益群里发言每条 +{per_msg} 能量，每天最多 {cap} 能量"),
    ]
    notes = []
    if earned >= cap:
        notes.append("✅ 今日发言能量已达上限，明天再继续。")
    return layout.card(layout.title("群发言能量"), rows, notes)


def handle_redeem(db, tg_user: dict, chat_id: int, args: str) -> str:
    from backend.api.economy import _redeem_exchange_core
    from fastapi import HTTPException

    user = resolve(db, int(tg_user.get("id") or 0))
    if user is None:
        return _bind_guide_text()
    code_str = (args or "").strip()
    if not code_str:
        return layout.card(
            layout.title("兑换码兑换"),
            [
                layout.row("🎟️", "使用方法", f"{_cmd('/redeem')} <i>兑换码</i>"),
                layout.row("💡", "兑换示例", _cmd('/redeem ABCD1234')),
            ],
        )
    try:
        result = _redeem_exchange_core(db, user, code_str)
    except HTTPException as e:
        return html.escape(str(e.detail))
    except Exception:
        return "兑换失败，请稍后再试"
    return layout.card(
        layout.title("兑换结果"),
        [layout.row("🎁", "兑换结果", html.escape(str(result.get("message", "兑换成功"))))],
    )


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

        text = layout.card(
            layout.title("群抽奖"),
            [
                layout.row("📌", "活动名称", f"<b>{title}</b>"),
                layout.row("🎁", "活动奖品", prize),
                layout.row("⏰", "开奖时间", draw_at_text),
                layout.row("👥", "参加人数", f"<b>{entry_text}{suffix}</b>"),
            ],
            ["👇 点击下方按钮参加"],
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
    card_rows = []
    for row in rows:
        row_title = html.escape(str(getattr(row, "title", None) or "未命名抽奖"))
        row_status = getattr(row, "status", None)
        status_text = status_map.get(row_status, html.escape(str(row_status)))
        won = getattr(row, "won", 0) or 0
        result = "🏆 <b>已中奖</b>" if won > 0 else "未中奖/待开奖"
        card_rows.append(layout.row("📌", row_title, f"状态：{status_text} ｜ {result}"))
    return layout.card(layout.title("我的抽奖记录"), card_rows)


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
        return layout.card(
            layout.title("发红包"),
            [
                layout.row("🧧", "使用方法", "/redpacket <i>总能量 个数</i>"),
                layout.row("💡", "红包示例", f"{_cmd('/redpacket 100 10')}（100 能量分成 10 个）"),
            ],
        )
    try:
        total, count = int(parts[0]), int(parts[1])
    except ValueError:
        return layout.card(
            layout.title("发红包"),
            [
                layout.row("🧧", "使用方法", "/redpacket <i>总能量 个数</i>"),
                layout.row("💡", "红包示例", _cmd('/redpacket 100 10')),
            ],
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


def slots_markup(db) -> dict:
    """老虎机注额按钮（一行三档）＋回主界面行；陪玩与结果卡共用。"""
    from backend import slots as slots_svc

    tier_row = [
        {"text": f"🎰 {tier} 能量", "callback_data": f"slots_bet:{tier}"}
        for tier in slots_svc.bet_tiers(db)
    ]
    return {"inline_keyboard": [tier_row, layout.home_row()]}


def handle_slots(db, tg_user: dict, chat_id: int, args: str) -> str | tuple[str, dict | None]:
    """老虎机私聊面板（展示本人能量；群面板见 handle_slots_group）。"""
    from backend import slots as slots_svc

    user = resolve(db, int(tg_user.get("id") or 0))
    if user is None:
        return _bind_guide_text()
    if not slots_svc.is_enabled(db):
        return "🎰 老虎机暂未开启"
    pays = slots_svc.pay_table(db)
    tiers = " / ".join(str(t) for t in slots_svc.bet_tiers(db))
    text = layout.card(
        layout.title("老虎机"),
        [
            layout.row("🏆", "头奖池", f"<b>{slots_svc.pool_balance(db)} 能量</b>"),
            layout.row("✨", "我的能量", f"<b>{int(user.points or 0)}</b>"),
            layout.row("🎯", "注额", f"{tiers} 能量三档，点下方按钮开摇"),
        ],
        [
            f"赔付：777 ×{pays['seven']} · BAR 三连 ×{pays['bar']} · "
            f"葡萄/柠檬三连 ×{pays['fruit']} · 双 7 返注",
            "结果由 Telegram 原生老虎机决定，公平随机",
        ],
    )
    return (text, slots_markup(db))


def handle_slots_group(db, tg_user: dict, chat_id: int, args: str) -> str | tuple[str, dict | None]:
    """老虎机群面板（公共面板）：只展示头奖池/注额/赔付与注额按钮。

    面板是群里公共消息，不展示任何个人数据：点注额按钮的人用自己
    的账号下注（扣点击者、赢归点击者，见 callbacks._handle_slots_bet），
    未绑定与资格校验都在下注时按点击者逐个判，面板本身人人可看。
    """
    from backend import slots as slots_svc

    if not slots_svc.is_enabled(db):
        return "🎰 老虎机暂未开启"
    pays = slots_svc.pay_table(db)
    tiers = " / ".join(str(t) for t in slots_svc.bet_tiers(db))
    text = layout.card(
        layout.title("老虎机"),
        [
            layout.row("🏆", "头奖池", f"<b>{slots_svc.pool_balance(db)} 能量</b>"),
            layout.row("🎯", "注额", f"{tiers} 能量三档，点下方按钮开摇"),
            layout.row("👤", "下注", "扣点按钮的人自己的能量，中奖也归他"),
        ],
        [
            f"赔付：777 ×{pays['seven']} · BAR 三连 ×{pays['bar']} · "
            f"葡萄/柠檬三连 ×{pays['fruit']} · 双 7 返注",
            "结果由 Telegram 原生老虎机决定，公平随机",
        ],
    )
    return (text, slots_markup(db))


def slots_group_result_text(db, spin, player_name: str) -> str:
    """老虎机群公开结果：点注人 / 结果 / 注额 / 奖金 / 头奖池。

    与私聊结果卡的区别：群里结果另发新消息播报（不编辑公共面板），
    带点注人、不带个人余额（公共场合不晒个人账）。player_name 由
    调用方转义后传入。
    """
    from backend import slots as slots_svc

    if spin.reels:
        reels = tuple(spin.reels.split(","))
    elif spin.dice_value:
        reels = slots_svc.decode_dice(int(spin.dice_value))
    else:
        reels = ()
    base = int(spin.base_payout or 0)
    pool = int(spin.pool_payout or 0)
    rows = [
        layout.row("👤", "点注人", f"<b>{player_name}</b>"),
        layout.row("🎰", "结果", f"<b>{slots_svc.reels_text(reels)}</b>"),
        layout.row("🎯", "注额", f"{int(spin.bet or 0)} 能量"),
    ]
    if pool > 0:
        rows.append(layout.row(
            "🏆", "头奖", f"<b>+{base + pool} 能量</b>（基础 {base}＋头奖池 {pool}）"))
    elif base > 0:
        rows.append(layout.row("🎉", "奖金", f"<b>+{base} 能量</b>"))
    else:
        rows.append(layout.row("💨", "奖金", "未中奖，下次好运"))
    rows.append(layout.row("🏆", "头奖池", f"{slots_svc.pool_balance(db)} 能量"))
    title = "老虎机 · 777 头奖！" if pool > 0 else "老虎机"
    return layout.card(layout.title(title), rows)


def slots_result_text(db, spin) -> str:
    """老虎机结算卡（#1515 卡片骨架）：结果 / 注额 / 奖金 / 余额 / 头奖池。"""
    from backend import slots as slots_svc

    if spin.reels:
        reels = tuple(spin.reels.split(","))
    elif spin.dice_value:
        reels = slots_svc.decode_dice(int(spin.dice_value))
    else:
        reels = ()
    base = int(spin.base_payout or 0)
    pool = int(spin.pool_payout or 0)
    rows = [
        layout.row("🎰", "结果", f"<b>{slots_svc.reels_text(reels)}</b>"),
        layout.row("🎯", "注额", f"{int(spin.bet or 0)} 能量"),
    ]
    if pool > 0:
        rows.append(layout.row(
            "🏆", "头奖", f"<b>+{base + pool} 能量</b>（基础 {base}＋头奖池 {pool}）"))
    elif base > 0:
        rows.append(layout.row("🎉", "奖金", f"<b>+{base} 能量</b>"))
    else:
        rows.append(layout.row("💨", "奖金", "未中奖，下次好运"))
    user = db.query(WebUser).filter(WebUser.id == spin.user_id).first()
    if user is not None:
        rows.append(layout.row("✨", "余额", f"{int(user.points or 0)} 能量"))
    rows.append(layout.row("🏆", "头奖池", f"{slots_svc.pool_balance(db)} 能量"))
    title = "老虎机 · 777 头奖！" if pool > 0 else "老虎机"
    return layout.card(layout.title(title), rows, ["👇 点下方注额再来一局"])


# 扩展功能适配器：业务实现独立于旧命令，避免 handlers.py 继续膨胀。
def handle_account(db, tg_user, chat_id, args):
    from backend.tg_bot.features import handle_account as _impl
    return _impl(db, tg_user, chat_id, args)

def handle_request(db, tg_user, chat_id, args):
    from backend.tg_bot.features import handle_request as _impl
    return _impl(db, tg_user, chat_id, args)

def handle_my_requests(db, tg_user, chat_id, args):
    from backend.tg_bot.features import handle_my_requests as _impl
    return _impl(db, tg_user, chat_id, args)

def handle_login(db, tg_user, chat_id, args):
    from backend.tg_bot.features import handle_login as _impl
    return _impl(db, tg_user, chat_id, args)

def handle_rank(db, tg_user, chat_id, args):
    from backend.tg_bot.features import handle_rank as _impl
    return _impl(db, tg_user, chat_id, args)

def handle_govlog(db, tg_user, chat_id, args):
    from backend.tg_bot.features import handle_govlog as _impl
    return _impl(db, tg_user, chat_id, args)

def handle_request_board(db, tg_user, chat_id, args):
    from backend.tg_bot.features import handle_request_board as _impl
    return _impl(db, tg_user, chat_id, args)

def handle_rules(db, tg_user, chat_id, args):
    from backend.tg_bot.features import handle_rules as _impl
    return _impl(db, tg_user, chat_id, args)

def handle_level(db, tg_user, chat_id, args):
    from backend.tg_bot.features import handle_level as _impl
    return _impl(db, tg_user, chat_id, args)

def handle_report(db, tg_user, chat_id, args):
    from backend.tg_bot.features import handle_report as _impl
    return _impl(db, tg_user, chat_id, args)

def handle_propose(db, tg_user, chat_id, args):
    from backend.tg_bot.features import handle_propose as _impl
    return _impl(db, tg_user, chat_id, args)

def handle_direct_launch(db, tg_user, chat_id, args):
    from backend.tg_bot.features import handle_direct_launch as _impl
    return _impl(db, tg_user, chat_id, args)
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


def handle_reconcile(db, tg_user: dict, chat_id: int, args: str):
    """群-号一致性对账（仅管理员，embyboss 借鉴第 3 项）。

    双门与 handle_whois 同一口径：私聊限定（handler 层 guard，
    router 的 private_only 另有一层）+ resolve 后 is_staff 判定，
    一字不改判定逻辑；非管理员回现行拒绝卡，未绑定回绑定指引。
    本命令只出报告卡（全只读，零处置）；处置走报告卡按钮的
    二次确认（callbacks 的 reconcile:ask / reconcile:do）。
    """
    from backend.tg_bot import queries, reconcile

    tg_id = tg_user.get("id")
    if not tg_id or chat_id != int(tg_id):
        return queries.PRIVATE_ONLY_TEXT
    user = resolve(db, int(tg_id))
    if user is None:
        return _bind_guide_text()
    if not bool(getattr(user, "is_staff", False)):
        return layout.card(
            layout.title("群号一致性对账"),
            [layout.row("🔒", "访问状态", "仅管理员可用")],
        )
    result = reconcile.run_reconcile(db)
    reconcile.cache_result(int(tg_id), result)
    return reconcile.render_report(result)


def handle_whois(db, tg_user: dict, chat_id: int, args: str) -> str:
    """管理员审计反查（只读，embyboss 借鉴第 2 项）。

    双门与 handle_admin 同一口径：私聊限定（handler 层 guard，
    router 的 private_only 另有一层）+ resolve 后 is_staff 判定，
    一字不改判定逻辑；非管理员回现行拒绝卡，未绑定回绑定指引。
    查询与渲染全在 whois.py（只读，只查现有表）。
    """
    from backend.tg_bot import queries, whois

    tg_id = tg_user.get("id")
    if not tg_id or chat_id != int(tg_id):
        return queries.PRIVATE_ONLY_TEXT
    user = resolve(db, int(tg_id))
    if user is None:
        return _bind_guide_text()
    if not bool(getattr(user, "is_staff", False)):
        return layout.card(
            layout.title("账号反查"),
            [layout.row("🔒", "访问状态", "仅管理员可用")],
        )
    keyword = (args or "").strip()
    if not keyword:
        return layout.card(
            layout.title("账号反查"),
            [
                layout.row("🔍", "用法", "私聊发送 /whois 关键词"),
                layout.row("🧭", "可查", "IP 地址、TG 号、站内用户名 / Emby 名、设备或客户端名"),
            ],
        )
    return whois.render_whois(db, keyword)
