"""Telegram bot 的账号、求片和群运营功能。

这里是 Telegram 交互适配层：业务规则优先复用现有模块，避免在 bot 内复制
订阅、积分和求片的判定口径。
"""
from __future__ import annotations

import html
import logging
from datetime import datetime

from fastapi import HTTPException

from backend import models
from backend.api.user import MediaSeekRequest, _create_media_seek_sync
from backend.tg_bot.identity import resolve

logger = logging.getLogger(__name__)


def _esc(value) -> str:
    return html.escape(str(value or ""))


def _bound_user(db, tg_user: dict):
    return resolve(db, int(tg_user.get("id") or 0))


def handle_account(db, tg_user: dict, chat_id: int, args: str) -> str:
    """账号中心：余额、活力、订阅和最近设备。"""
    user = _bound_user(db, tg_user)
    if user is None:
        return "🔒 账号中心需要先绑定网站账号。\n\n发送 /bind 查看绑定方法。"

    now = datetime.now()
    subs = (
        db.query(models.UserSubscription)
        .filter(models.UserSubscription.user_id == user.id)
        .order_by(models.UserSubscription.end_date.desc())
        .limit(5).all()
    )
    devices = (
        db.query(models.UserDevice)
        .filter(models.UserDevice.user_id == user.id)
        .order_by(models.UserDevice.last_seen_at.desc())
        .limit(3).all()
    )
    lines = [
        "👤 <b>账号中心</b>",
        "",
        f"账号：<b>{_esc(user.username)}</b>",
        f"积分：<b>{int(user.points or 0)}</b>",
    ]
    if bool(getattr(user, "is_welfare", False)):
        welfare_end = getattr(user, "welfare_expires_at", None)
        suffix = welfare_end.strftime("%Y-%m-%d") if welfare_end else "长期有效"
        lines.append(f"公益资格：有效至 {suffix}")
    vitality = getattr(user, "vitality", None)
    if vitality is not None:
        lines.append(f"活力值：<b>{int(vitality)}</b>")

    lines += ["", "📦 <b>订阅</b>"]
    if not subs:
        lines.append("暂无订阅")
    else:
        for sub in subs:
            plan = getattr(sub, "plan", None)
            name = _esc(getattr(plan, "name", None) or "会员")
            end = sub.end_date.strftime("%Y-%m-%d") if sub.end_date else "未知"
            active = sub.status == "active" and sub.end_date and sub.end_date > now
            status = "生效中" if active else "已结束"
            days = max(0, (sub.end_date - now).days) if active else 0
            lines.append(f"• {name}：{status}，{end}（剩 {days} 天）")

    lines += ["", f"📱 <b>最近设备</b>（共显示 {len(devices)} 台）"]
    if not devices:
        lines.append("暂无设备记录")
    else:
        for device in devices:
            label = _esc(device.client or device.name or "未知客户端")
            seen = device.last_seen_at.strftime("%m-%d %H:%M") if device.last_seen_at else "未知"
            blocked = "，已停用" if device.is_blocked else ""
            lines.append(f"• {label}：{seen}{blocked}")
    return "\n".join(lines)


def _request_help() -> str:
    return (
        "🎬 <b>求片</b>\n\n"
        "私聊发送：\n"
        "<code>/request 片名</code>\n\n"
        "也可以附加年份和备注：\n"
        "<code>/request 片名 | 2026 | 想看国语版</code>\n\n"
        "查看自己的求片：<code>/myrequests</code>"
    )


def _notify_staff_about_request(db, request_id: int, user, name: str) -> None:
    """给已绑定 Telegram 的管理员发一条轻量通知，失败不影响求片落库。"""
    from backend.tg_bot import sender

    staff = db.query(models.WebUser).filter(
        models.WebUser.is_staff.is_(True),
        models.WebUser.is_active.is_(True),
        models.WebUser.telegram_id.isnot(None),
    ).all()
    text = (
        "📥 <b>新的求片请求</b>\n\n"
        f"用户：{_esc(user.username)}\n"
        f"片名：<b>{_esc(name)}</b>\n"
        f"请求编号：<code>#{request_id}</code>"
    )
    for admin in staff:
        try:
            sender.send_message(db, int(admin.telegram_id), text)
        except Exception:
            logger.warning("TG 求片管理员通知失败 admin=%s request=%s", admin.id, request_id, exc_info=True)


def handle_request(db, tg_user: dict, chat_id: int, args: str) -> str:
    """通过现有 media_seek 核心提交求片。"""
    user = _bound_user(db, tg_user)
    if user is None:
        return "🔒 求片需要先绑定网站账号。\n\n发送 /bind 查看绑定方法。"
    raw = (args or "").strip()
    if not raw:
        return _request_help()
    parts = [p.strip() for p in raw.split("|", 2)]
    name = parts[0]
    if not name:
        return "请填写片名。\n\n" + _request_help()
    year = parts[1] if len(parts) >= 2 and parts[1] else None
    note = parts[2] if len(parts) >= 3 and parts[2] else None
    if len(name) > 255:
        return "片名太长，请控制在 255 个字符以内。"
    request = MediaSeekRequest(movie_name=name, year=year, type="movie", note=note)
    try:
        from backend import media_seek
        if not media_seek.seek_enabled(db):
            return "⛔ 求片功能目前已关闭。"
        request_id = _create_media_seek_sync(db, user, name, request)
    except HTTPException as exc:
        return _esc(exc.detail)
    except ValueError as exc:
        return _esc(exc)
    except Exception:
        db.rollback()
        logger.exception("TG 求片提交失败 user=%s", user.id)
        return "求片提交失败，请稍后再试。"
    _notify_staff_about_request(db, request_id, user, name)
    return (
        "✅ <b>求片已提交</b>\n\n"
        f"《{_esc(name)}》\n"
        f"请求编号：<code>#{request_id}</code>\n\n"
        "后续状态会在网站和 Telegram 通知中更新。"
    )


def handle_my_requests(db, tg_user: dict, chat_id: int, args: str) -> str:
    """列出当前账号最近的求片。"""
    user = _bound_user(db, tg_user)
    if user is None:
        return "🔒 请先绑定网站账号。发送 /bind 查看方法。"
    rows = (
        db.query(models.MovieRequest)
        .filter(models.MovieRequest.user_id == user.id,
                models.MovieRequest.status != "withdrawn")
        .order_by(models.MovieRequest.created_at.desc())
        .limit(10).all()
    )
    if not rows:
        return "🎬 你还没有求片记录。\n\n发送 /request 片名 提交第一条求片。"
    labels = {
        "pending": "排队中", "approved": "处理中", "completed": "已入库",
        "rejected": "未通过", "cancelled": "已取消",
    }
    lines = ["🎬 <b>我的求片</b>", ""]
    for row in rows:
        status = labels.get(str(row.status), str(row.status or "未知"))
        title = _esc(row.movie_name)
        date = row.created_at.strftime("%m-%d") if row.created_at else ""
        lines.append(f"• <b>#{row.id}</b> {title}：{status} <i>{date}</i>")
        if row.admin_note:
            lines.append(f"  备注：{_esc(row.admin_note)[:120]}")
    return "\n".join(lines)


def handle_login(db, tg_user: dict, chat_id: int, args: str):
    """仅私聊生成一次性快捷登录链接。"""
    user = _bound_user(db, tg_user)
    if user is None:
        return "🔒 请先绑定网站账号。发送 /bind 查看方法。"
    from backend.tg_bot import login_token
    url = login_token.build_login_url(db, user)
    if not url:
        return "暂时无法生成登录链接，请联系管理员检查站点地址配置。"
    return (
        "🚀 <b>快捷登录</b>\n\n"
        "链接 10 分钟内有效，只能使用一次。请不要转发给别人。",
        {"inline_keyboard": [[{"text": "打开控制面板", "url": url}]]},
    )


def handle_request_board(db, tg_user: dict, chat_id: int, args: str) -> str:
    """群运营求片榜：只展示片名和热度，不泄露申请人身份。"""
    rows = (
        db.query(models.MovieRequest)
        .filter(models.MovieRequest.status == "pending")
        .order_by(models.MovieRequest.vote_count.desc(),
                  models.MovieRequest.created_at.desc())
        .limit(10).all()
    )
    if not rows:
        return "🔥 <b>求片榜</b>\n\n当前没有排队中的求片。"
    lines = ["🔥 <b>求片榜</b>", "", "按附议数排序："]
    for index, row in enumerate(rows, 1):
        title = _esc(row.movie_name)
        season = f"（{_esc(row.season)}）" if row.season and row.season != "all" else ""
        lines.append(f"{index}. {title}{season} · {int(row.vote_count or 0)} 人想看")
    return "\n".join(lines)


def handle_rules(db, tg_user: dict, chat_id: int, args: str) -> str:
    """群规：允许管理员在系统配置中自定义文案。"""
    from backend.integrations import store
    configured = store.get_value(db, "tg_group_rules", "") or ""
    rules = str(configured).strip() or (
        "1. 请勿刷屏、广告或发布与影视服务无关的链接。\n"
        "2. 兑换码、账号和续费等隐私操作请私聊机器人。\n"
        "3. 求片请使用 /request，重复求片会被合并。\n"
        "4. 请遵守群公告和管理员安排。"
    )
    return "📜 <b>群规与使用说明</b>\n\n" + _esc(rules).replace("\n", "\n")


def handle_rank(db, tg_user: dict, chat_id: int, args: str) -> str:
    """积分排行榜，复用 WebUser.points 单一积分余额。"""
    rows = (
        db.query(models.WebUser)
        .filter(models.WebUser.is_active.is_(True),
                models.WebUser.telegram_id.isnot(None),
                models.WebUser.show_on_leaderboard.is_(True))
        .order_by(models.WebUser.points.desc(), models.WebUser.id.asc())
        .limit(10).all()
    )
    if not rows:
        return "🏆 暂时还没有排行榜数据。"
    my_id = int(tg_user.get("id") or 0)
    lines = ["🏆 <b>全站积分排行榜</b>", "", "仅显示已绑定且允许上榜的用户。"]
    medals = ["🥇", "🥈", "🥉"]
    for index, row in enumerate(rows, 1):
        mark = medals[index - 1] if index <= 3 else f"{index}."
        mine = " ◀️" if row.telegram_id == my_id else ""
        lines.append(f"{mark} {_esc(row.username)}：<b>{int(row.points or 0)}</b>{mine}")
    return "\n".join(lines)
