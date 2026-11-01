# -*- coding: utf-8 -*-
"""TG Bot 管理员审计反查（只读，embyboss 借鉴第 2 项）。

用途：管理员拿到一个可疑信号（IP / 设备 / 客户端 / Emby 名 / TG 号 /
站内用户名），反查「这可能是谁」——关联账号、最近登录、最近播放、
绑定状态，一张卡看完。

硬规矩：
- 只读：本模块只 SELECT，不建行、不改行、不提交事务。
- 只复用现有表，不新造日志采集：web_users、login_logs、
  emby_playback_sessions、user_storage_bindings、emby_items（补片名）。
- 秘密字段绝不进结果：不读 password_hash / emby_password / 凭据行，
  绑定只出 label/provider/status（与 P1 render_bindings 同口径）。
- IP 对管理员完整显示：这是管理员自查工具，IP 本就是反查的输入
  信号，打码反而没法核对；命令本身仅私聊 + is_staff 双门（见
  handlers.handle_whois），普通用户看不到任何结果。

本文件下半段另有群内精简卡（find_lite_user / render_whois_lite）：
管理员在群里查人时只出字段白名单，审计痕迹一律不进，口径见该段注释。
"""
from __future__ import annotations

import html
import ipaddress
import logging
from datetime import datetime

from backend import models
from backend.emby_server import models as em
from backend.tg_bot import layout
from backend.tg_bot.queries import BINDING_STATUS_TEXT, PROVIDER_TEXT

logger = logging.getLogger(__name__)

RECENT_LIMIT = 5
MAX_USERS = 10
_UA_SHORT = 42

HIT_LABELS = {
    "ip_login": "登录 IP",
    "ip_play": "播放 IP",
    "tg": "TG 号",
    "name": "用户名",
    "device": "设备/客户端",
    "ua": "登录设备",
}


def _fmt_time(dt) -> str:
    if isinstance(dt, datetime):
        return dt.strftime("%Y-%m-%d %H:%M")
    return "—"


def _classify(keyword: str) -> str:
    """关键词形态判别：纯数字→TG 号；可解析为 IP→IP；其余→文本模糊查。"""
    if keyword.isdigit():
        return "tg"
    try:
        ipaddress.ip_address(keyword)
        return "ip"
    except ValueError:
        return "text"


def _collect(found: dict, user, hit: str) -> None:
    if user is None:
        return
    entry = found.setdefault(user.id, [user, set()])
    entry[1].add(hit)


def _users_by_ids(db, ids) -> dict:
    if not ids:
        return {}
    return {
        u.id: u
        for u in db.query(models.WebUser)
        .filter(models.WebUser.id.in_(list(ids)))
        .all()
    }


def find_users(db, keyword: str) -> list:
    """按关键词形态查关联账号，返回 [(WebUser, {命中方式, ...}), ...]。

    IP：login_logs.ip 精确 + emby_playback_sessions.remote_addr 精确；
    TG：web_users.telegram_id 精确；
    文本：web_users.username / emby_username 模糊 + 播放 device_id 精确、
    device_name / client_name 模糊 + login_logs.user_agent 模糊。
    login_logs 里 user_id 为空的失败登录行按 username 尽力归户。
    """
    kind = _classify(keyword)
    found: dict[int, list] = {}
    if kind == "tg":
        user = (
            db.query(models.WebUser)
            .filter(models.WebUser.telegram_id == int(keyword))
            .first()
        )
        _collect(found, user, "tg")
    elif kind == "ip":
        login_uids = {
            uid for (uid,) in db.query(models.LoginLog.user_id)
            .filter(models.LoginLog.ip == keyword,
                    models.LoginLog.user_id.isnot(None))
            .distinct().all()
        }
        orphan_names = {
            name for (name,) in db.query(models.LoginLog.username)
            .filter(models.LoginLog.ip == keyword,
                    models.LoginLog.user_id.is_(None),
                    models.LoginLog.username.isnot(None))
            .distinct().all()
        }
        if orphan_names:
            for u in db.query(models.WebUser).filter(
                    models.WebUser.username.in_(list(orphan_names))).all():
                login_uids.add(u.id)
        for u in _users_by_ids(db, login_uids).values():
            _collect(found, u, "ip_login")
        play_uids = {
            uid for (uid,) in db.query(em.PlaybackSession.user_id)
            .filter(em.PlaybackSession.remote_addr == keyword,
                    em.PlaybackSession.user_id.isnot(None))
            .distinct().all()
        }
        for u in _users_by_ids(db, play_uids).values():
            _collect(found, u, "ip_play")
    else:
        like = f"%{keyword}%"
        for u in db.query(models.WebUser).filter(
                models.WebUser.username.ilike(like)
                | models.WebUser.emby_username.ilike(like)).all():
            _collect(found, u, "name")
        device_uids = {
            uid for (uid,) in db.query(em.PlaybackSession.user_id)
            .filter(em.PlaybackSession.user_id.isnot(None),
                    (em.PlaybackSession.device_id == keyword)
                    | em.PlaybackSession.device_name.ilike(like)
                    | em.PlaybackSession.client_name.ilike(like))
            .distinct().all()
        }
        for u in _users_by_ids(db, device_uids).values():
            _collect(found, u, "device")
        ua_uids = {
            uid for (uid,) in db.query(models.LoginLog.user_id)
            .filter(models.LoginLog.user_agent.ilike(like),
                    models.LoginLog.user_id.isnot(None))
            .distinct().all()
        }
        for u in _users_by_ids(db, ua_uids).values():
            _collect(found, u, "ua")
    return [found[k] for k in sorted(found.keys())]


def _login_lines(db, user_id: int) -> list[str]:
    rows = (
        db.query(models.LoginLog)
        .filter(models.LoginLog.user_id == user_id)
        .order_by(models.LoginLog.created_at.desc(), models.LoginLog.id.desc())
        .limit(RECENT_LIMIT)
        .all()
    )
    lines = []
    for r in rows:
        ua = str(r.user_agent or "").strip()
        if len(ua) > _UA_SHORT:
            ua = ua[:_UA_SHORT] + "…"
        line = f"{_fmt_time(r.created_at)} · {html.escape(str(r.ip or '—'))}"
        if ua:
            line += f" · {html.escape(ua)}"
        if not r.success:
            line += f" · 失败（{html.escape(str(r.reason or '未知'))}）"
        lines.append(line)
    return lines


def _play_lines(db, user_id: int) -> list[str]:
    rows = (
        db.query(em.PlaybackSession)
        .filter(em.PlaybackSession.user_id == user_id)
        .order_by(em.PlaybackSession.start_time.desc(),
                  em.PlaybackSession.id.desc())
        .limit(RECENT_LIMIT)
        .all()
    )
    if not rows:
        return []
    item_ids = {r.item_id for r in rows if r.item_id}
    names = {}
    if item_ids:
        names = {
            it.id: it.name
            for it in db.query(em.MediaItem)
            .filter(em.MediaItem.id.in_(list(item_ids))).all()
        }
    lines = []
    for r in rows:
        name = names.get(r.item_id) or "未知条目"
        client = str(r.client_name or r.device_name or "").strip()
        line = f"{_fmt_time(r.start_time)} · {html.escape(str(name))}"
        if client:
            line += f" · {html.escape(client)}"
        lines.append(line)
    return lines


def _binding_line(db, user_id: int) -> str:
    rows = (
        db.query(em.UserStorageBinding)
        .filter(em.UserStorageBinding.user_id == user_id)
        .order_by(em.UserStorageBinding.id)
        .all()
    )
    if not rows:
        return "未绑定网盘"
    parts = []
    for row in rows:
        provider = PROVIDER_TEXT.get(row.provider,
                                     html.escape(str(row.provider)))
        status = BINDING_STATUS_TEXT.get(row.status,
                                         html.escape(str(row.status)))
        parts.append(
            f"{provider}「{html.escape(str(row.account_label))}」{status}")
    return "；".join(parts)


def _user_rows(db, user, hits: set) -> list[str]:
    badges = ""
    if bool(getattr(user, "is_staff", False)):
        badges += "｜管理员"
    if not bool(getattr(user, "is_active", True)):
        badges += "｜已停用"
    hit_text = "、".join(HIT_LABELS[h] for h in sorted(hits))
    logins = _login_lines(db, user.id)
    plays = _play_lines(db, user.id)
    return [
        layout.row("👤", "账号",
                   f"<b>{html.escape(str(user.username or ''))}</b>"
                   f"（#{user.id}）{badges}"),
        layout.row("🎯", "命中", hit_text),
        layout.row("✈️", "TG 绑定",
                   str(user.telegram_id) if user.telegram_id else "未绑定"),
        layout.row("🎬", "Emby 名",
                   html.escape(str(user.emby_username)) if user.emby_username else "—"),
        layout.row("🕐", "最近登录", "\n".join(logins) if logins else "暂无记录"),
        layout.row("▶️", "最近播放", "\n".join(plays) if plays else "暂无记录"),
        layout.row("🔗", "网盘绑定", _binding_line(db, user.id)),
    ]


def render_whois(db, keyword: str) -> str:
    """反查结果卡：命中账号全列（上限 MAX_USERS，超出注明截断）。"""
    keyword = (keyword or "").strip()
    matches = find_users(db, keyword)
    if not matches:
        return layout.card(
            layout.title("账号反查"),
            [
                layout.row("🔍", "关键词", html.escape(keyword)),
                layout.row("📭", "结果", "没查到匹配的账号"),
            ],
        )
    rows = [
        layout.row("🔍", "关键词", html.escape(keyword)),
        layout.row("👥", "命中账号", f"{len(matches)} 个"),
    ]
    shown = matches[:MAX_USERS]
    for user, hits in shown:
        rows.extend(_user_rows(db, user, hits))
    if len(matches) > MAX_USERS:
        rows.append(layout.row(
            "⚠️", "已截断",
            f"命中过多，只显示前 {MAX_USERS} 个，其余 {len(matches) - MAX_USERS} 个未列"))
    text = layout.card(layout.title("账号反查"), rows)
    if len(text) > 3900:
        text = text[:3900] + "\n…（结果过长已截断）"
    return text


# ---------------------------------------------------------------------------
# 群内精简卡（2026-11-01 用户拍板：只给管理员、在群里能查、出精简卡）。
# 与上面审计全卡是两条链路，硬规矩更严：
# - 定位只查 web_users 本表（TG 号 / 站内用户名），绝不碰 login_logs /
#   播放会话——IP、设备、UA 信号根本不进精简卡链路；
# - 渲染只出字段白名单：站内用户名、TG 显示名、等级、公益资格、订阅
#   到期、能量余额、网盘绑定（只 provider＋状态，无 label / 凭据 id）、
#   账号状态；多一个字段都不许加（断言型测试挡敏感字段）；
# - 全只读，与全卡同一口径：不建行、不改行、不提交事务。
# ---------------------------------------------------------------------------

def find_lite_user(db, keyword: str):
    """精简卡带参定位：纯数字→telegram_id 精确；文本→username 精确
    优先，其次 username / emby_username 模糊（取最早一条）。"""
    keyword = (keyword or "").strip()
    if not keyword:
        return None
    if keyword.isdigit():
        return (
            db.query(models.WebUser)
            .filter(models.WebUser.telegram_id == int(keyword))
            .first()
        )
    exact = (
        db.query(models.WebUser)
        .filter(models.WebUser.username == keyword)
        .first()
    )
    if exact is not None:
        return exact
    like = f"%{keyword}%"
    return (
        db.query(models.WebUser)
        .filter(models.WebUser.username.ilike(like)
                | models.WebUser.emby_username.ilike(like))
        .order_by(models.WebUser.id)
        .first()
    )


def _binding_lite_line(db, user_id: int) -> str:
    """绑定盘（精简）：只出 provider＋状态，绝不带 label / 凭据 id。"""
    rows = (
        db.query(em.UserStorageBinding)
        .filter(em.UserStorageBinding.user_id == user_id)
        .order_by(em.UserStorageBinding.id)
        .all()
    )
    if not rows:
        return "未绑定网盘"
    parts = []
    for row in rows:
        provider = PROVIDER_TEXT.get(row.provider,
                                     html.escape(str(row.provider)))
        status = BINDING_STATUS_TEXT.get(row.status,
                                         html.escape(str(row.status)))
        parts.append(f"{provider}｜{status}")
    return "；".join(parts)


def _subscription_lite_line(db, user_id: int) -> str:
    """订阅到期状态：取到期最晚的一条出状态＋到期日（不带套餐名）。

    表缺失的夹具环境回落「暂无订阅」，与 features._level_info 的
    夹具回落同口径：只读展示路径不许因缺表炸掉整张卡。
    """
    try:
        row = (
            db.query(models.UserSubscription)
            .filter(models.UserSubscription.user_id == user_id)
            .order_by(models.UserSubscription.end_date.desc())
            .first()
        )
    except Exception:
        db.rollback()
        return "暂无订阅"
    if row is None:
        return "暂无订阅"
    end = row.end_date.strftime("%Y-%m-%d") if row.end_date else "未知"
    active = (row.status == "active" and row.end_date
              and row.end_date > datetime.now())
    return f"{'生效中' if active else '已结束'}（至 {end}）"


def render_whois_lite(db, user, tg_display_name: str | None = None) -> str:
    """群内精简卡：字段白名单逐项对用户定版，不许私自加行。

    等级 / 公益资格复用 features 的现行口径（横切只许一套）：
    _level_info 只读快照、_welfare_line 三态文案。
    """
    from backend.tg_bot import features

    info = features._level_info(db, user)
    welfare = features._welfare_line(user).split("：", 1)[-1]
    status = "正常" if bool(getattr(user, "is_active", True)) else "已停用"
    rows = [
        layout.row("👤", "账号",
                   f"<b>{html.escape(str(user.username or ''))}</b>"
                   f"（#{user.id}）"),
        layout.row("✈️", "TG 显示名",
                   html.escape(str(tg_display_name)) if tg_display_name
                   else "—"),
        layout.row("⭐", "等级",
                   f"Lv.{info['level']} "
                   f"{html.escape(str(info['name']))}"),
        layout.row("📊", "公益资格", welfare),
        layout.row("📦", "订阅", _subscription_lite_line(db, user.id)),
        layout.row("🍒", "能量余额", int(getattr(user, "points", 0) or 0)),
        layout.row("🔗", "网盘绑定", _binding_lite_line(db, user.id)),
        layout.row("🚦", "账号状态", status),
    ]
    return layout.card(layout.title("账号速览"), rows)
