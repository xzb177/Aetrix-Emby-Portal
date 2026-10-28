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


def _achievement_summary(db, user_id: int):
    """账号成就：返回（已解锁定义行（按 sort 排序，仅 enabled 定义），enabled 定义总数）。

    口径与 GET /api/panel/collector 一致：只展示 enabled 定义。
    """
    defs = (
        db.query(models.PanelAchievementDef)
        .filter_by(enabled=True)
        .order_by(models.PanelAchievementDef.sort)
        .all()
    )
    unlocked_keys = {
        r[0]
        for r in db.query(models.PanelAchievementUnlock.ach_key)
        .filter_by(user_id=user_id)
        .all()
    }
    shown = [d for d in defs if d.key in unlocked_keys]
    return shown, len(defs)


def _pet_summary(db, user_id: int):
    """馆灵：返回（累计观影小时，当前阶段 dict，下一阶段 dict 或 None）。

    观影秒数与阶段解析口径与 GET /api/panel/collector 逐字一致：
    emby_playback_sessions join MediaItem 后 playback_seconds 累加，
    阶段一律走 backend.api.panel._read_pet_stages（横切只许一套）。
    """
    from backend.api.panel import _read_pet_stages
    from backend.emby_server import models as em
    from backend.panel_achievements import playback_seconds

    rows = (
        db.query(
            em.PlaybackSession.position_ticks,
            em.MediaItem.duration_ticks,
        )
        .join(em.MediaItem, em.MediaItem.id == em.PlaybackSession.item_id)
        .filter(em.PlaybackSession.user_id == user_id)
        .all()
    )
    total_secs = sum(playback_seconds(r) for r in rows)
    total_hours = total_secs / 3600
    stages = _read_pet_stages(db)
    if not stages:
        # _read_pet_stages 自带非法回退，正常走不到这里；防御性兜底
        from backend.api.panel import PET_STAGES_DEFAULT

        stages = [dict(s) for s in PET_STAGES_DEFAULT]
    cur_idx = 0
    for i, st in enumerate(stages):
        if total_hours >= st["hours"]:
            cur_idx = i
    cur = stages[cur_idx]
    nxt = stages[cur_idx + 1] if cur_idx + 1 < len(stages) else None
    return total_hours, cur, nxt


def _welfare_line(user) -> str:
    """公益资格三态：未开通／长期有效／有效期至 YYYY-MM-DD。

    口径与 /account 既有写法一致（is_welfare＋welfare_expires_at），只补未开通分支。
    """
    if not bool(getattr(user, "is_welfare", False)):
        return "公益资格：未开通"
    end = getattr(user, "welfare_expires_at", None)
    if end is None:
        return "公益资格：长期有效"
    return f"公益资格：有效期至 {end.strftime('%Y-%m-%d')}"


def _view_unlock_line(db, user_id: int) -> str:
    """查看凭证：取用户名下有效行（expires_at 为空或未过期）。

    有效性口径与 portal.py 购票校验一致（expires_at IS NULL OR expires_at > now）。
    多行时永久有效优先，否则取最晚到期日；无有效行写未开通。
    """
    now = datetime.now()
    rows = (
        db.query(models.EmbyViewUnlock)
        .filter(models.EmbyViewUnlock.user_id == user_id)
        .all()
    )
    valid = [r for r in rows if r.expires_at is None or r.expires_at > now]
    if not valid:
        return "查看凭证：未开通"
    if any(r.expires_at is None for r in valid):
        return "查看凭证：长期有效"
    latest = max(r.expires_at for r in valid)
    return f"查看凭证：有效期至 {latest.strftime('%Y-%m-%d')}"


def _bindings_line(db, user_id: int) -> str:
    """网盘绑定：仅 active 行，按 provider 区分展示。"""
    from backend.emby_server import models as em

    rows = (
        db.query(em.UserStorageBinding)
        .filter(em.UserStorageBinding.user_id == user_id,
                em.UserStorageBinding.status == "active")
        .all()
    )
    providers = {r.provider for r in rows}
    names = []
    if "drive" in providers:
        names.append("Drive 已绑")
    if "pan115" in providers:
        names.append("115 已绑")
    for p in sorted(providers - {"drive", "pan115"}):
        names.append(f"{_esc(p)} 已绑")
    if not names:
        return "网盘绑定：未绑定，绑定后才能点播"
    return "网盘绑定：" + "、".join(names)


def _vitality_config(db) -> tuple[int, int]:
    """活力上限与观影阈值：走 get_vitality_config，读失败回落默认 14／3。"""
    try:
        from backend.vitality import get_vitality_config

        cfg = get_vitality_config(db)
        return int(cfg.get("max", 14)), int(cfg.get("limit_threshold", 3))
    except Exception:
        logger.debug("读取活力配置失败，回落默认 14／3", exc_info=True)
        return 14, 3


def _streak_renew_min(db) -> int:
    """连签保资格阈值：SystemConfig welfare_streak_renew_min，默认 30。"""
    try:
        row = (
            db.query(models.SystemConfig)
            .filter_by(key="welfare_streak_renew_min")
            .first()
        )
        return int(row.value) if row and row.value is not None else 30
    except Exception:
        return 30


def _streak_line(db, user_id: int) -> str:
    """连签：取最近一条签到记录的 streak 列，无记录为 0 天。"""
    row = (
        db.query(models.CheckinRecord)
        .filter(models.CheckinRecord.user_id == user_id)
        .order_by(models.CheckinRecord.checkin_date.desc())
        .first()
    )
    streak = int(row.streak) if row is not None and row.streak is not None else 0
    return f"📋 连签：{streak} 天（连签 {_streak_renew_min(db)} 天自动续公益资格）"


def compact_dashboard(db, user) -> str:
    """/start 已绑定用户 dashboard：账号、公益资格、查看凭证、能量·活力、网盘绑定、馆灵、成就、连签。"""
    shown_ach, total_ach = _achievement_summary(db, user.id)
    total_hours, pet_cur, pet_nxt = _pet_summary(db, user.id)
    cap, threshold = _vitality_config(db)
    vitality = getattr(user, "vitality", None)
    if vitality is None:
        vitality_text = "—"
    else:
        vitality_text = f"<b>{int(vitality)}</b>／{cap}"
        if int(vitality) < threshold:
            vitality_text += "（暂时播不了）"
    if pet_nxt is not None:
        remain = pet_nxt["hours"] - total_hours
        pet_text = (
            f"{_esc(pet_cur.get('icon', ''))} 馆灵：{_esc(pet_cur.get('name', ''))}"
            f"（累计观影 {total_hours:.1f} 小时，"
            f"距{_esc(pet_nxt.get('name', ''))}还差 {remain:.1f} 小时）"
        )
    else:
        pet_text = (
            f"{_esc(pet_cur.get('icon', ''))} 馆灵：{_esc(pet_cur.get('name', ''))}"
            f"（累计观影 {total_hours:.1f} 小时，已满级）"
        )
    lines = [
        "👤 <b>账号中心</b>",
        "",
        f"账号：<b>{_esc(user.username)}</b>",
        _welfare_line(user),
        _view_unlock_line(db, user.id),
        f"能量：<b>{int(user.points or 0)}</b> · 活力值：{vitality_text}",
        _bindings_line(db, user.id),
        pet_text,
        f"🏅 成就：{len(shown_ach)}／{total_ach}",
        _streak_line(db, user.id),
    ]
    return "\n".join(lines)


def handle_account(db, tg_user: dict, chat_id: int, args: str) -> str:
    """账号中心：能量、活力、馆灵、成就、订阅和最近设备。"""
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
        f"能量：<b>{int(user.points or 0)}</b>",
    ]
    if bool(getattr(user, "is_welfare", False)):
        welfare_end = getattr(user, "welfare_expires_at", None)
        suffix = welfare_end.strftime("%Y-%m-%d") if welfare_end else "长期有效"
        lines.append(f"公益资格：有效至 {suffix}")
    vitality = getattr(user, "vitality", None)
    if vitality is not None:
        lines.append(f"活力值：<b>{int(vitality)}</b>")

    total_hours, pet_cur, pet_nxt = _pet_summary(db, user.id)
    lines += ["", "🐉 <b>馆灵</b>"]
    lines.append(
        f"{_esc(pet_cur.get('icon', ''))} {_esc(pet_cur.get('name', ''))}"
        f"（累计观影 {total_hours:.1f} 小时）"
    )
    if pet_nxt is not None:
        remain = pet_nxt["hours"] - total_hours
        lines.append(
            f"下一阶段：{_esc(pet_nxt.get('name', ''))}，还差 {remain:.1f} 小时"
        )
    else:
        lines.append("已满级")

    shown_ach, total_ach = _achievement_summary(db, user.id)
    lines += ["", f"🏅 <b>成就</b>（已解锁 {len(shown_ach)}／共 {total_ach} 个）"]
    if not shown_ach:
        lines.append("暂无成就")
    else:
        for d in shown_ach:
            lines.append(f"{_esc(d.icon)} {_esc(d.name)}")

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
    # Step 1：榜单按加权附议和排（行文案仍显原始人数，不泄露投票人身份）
    from backend import media_seek
    rows = (
        db.query(models.MovieRequest)
        .filter(models.MovieRequest.status == "pending")
        .order_by(media_seek.weighted_vote_ordering().desc(),
                  models.MovieRequest.created_at.desc())
        .limit(10).all()
    )
    if not rows:
        return "🔥 <b>求片榜</b>\n\n当前没有排队中的求片。"
    lines = ["🔥 <b>求片榜</b>", "", "按加权热度排序："]
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
    """积分排行榜，复用 WebUser.points 单一积分余额。

    排序口径不变（积分降序、同分按 id 升序、前 10 名）；等级公益服活动
    治理 Step 4：仅公益服下追加等级列展示（Lv.数字＋等级名），付费服
    输出与此前逐字一致。
    """
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
    from backend.subscriptions import is_free_realm as _is_free_realm
    free_realm = _is_free_realm(db)
    level_names = {}
    if free_realm:
        level_names = {
            lv.level: lv.name
            for lv in db.query(models.MemberLevel)
            .filter(models.MemberLevel.is_active == True)  # noqa: E712
            .all()
        }
    my_id = int(tg_user.get("id") or 0)
    lines = ["🏆 <b>全站积分排行榜</b>", "", "仅显示已绑定且允许上榜的用户。"]
    medals = ["🥇", "🥈", "🥉"]
    for index, row in enumerate(rows, 1):
        mark = medals[index - 1] if index <= 3 else f"{index}."
        mine = " ◀️" if row.telegram_id == my_id else ""
        if free_realm:
            level = row.member_level or 1
            name = level_names.get(level)
            level_text = f" · Lv.{level} {_esc(name)}" if name else f" · Lv.{level}"
            lines.append(f"{mark} {_esc(row.username)}：<b>{int(row.points or 0)}</b>{level_text}{mine}")
        else:
            lines.append(f"{mark} {_esc(row.username)}：<b>{int(row.points or 0)}</b>{mine}")
    return "\n".join(lines)


_GOVLOG_ACTION_TEXT = {
    "freeze": "停权",
    "release": "解封",
    "downgrade": "降级",
}


def handle_govlog(db, tg_user: dict, chat_id: int, args: str) -> str:
    """治理审计日志只读查看（等级公益服活动治理 Step 4）。

    仅私聊（群聊由 router 的 private_only 拦）、仅公益服、仅 Lv.6 传奇；
    未绑定／非公益服／低于 Lv.6 一律只回提示、不泄露日志内容。
    """
    from backend import governance as _governance
    from backend.subscriptions import is_free_realm as _is_free_realm

    user = _bound_user(db, tg_user)
    if user is None:
        return "请先绑定账号后再查看治理审计日志。"
    if not _is_free_realm(db):
        return "治理审计日志仅在公益服开放。"
    if (user.member_level or 1) < 6:
        return "治理审计日志仅 Lv.6 传奇可见。"
    entries = _governance.list_audit_logs(db, limit=10)
    if not entries:
        return "📋 <b>治理审计日志</b>\n\n暂无记录。"
    user_ids = {e.actor_user_id for e in entries if e.actor_user_id}
    user_ids |= {e.target_user_id for e in entries if e.target_user_id}
    names = {}
    if user_ids:
        names = {
            u.id: u.username
            for u in db.query(models.WebUser).filter(models.WebUser.id.in_(user_ids)).all()
        }
    lines = ["📋 <b>治理审计日志（最近 10 条）</b>", ""]
    for e in entries:
        action_text = _GOVLOG_ACTION_TEXT.get(e.action, e.action)
        actor = names.get(e.actor_user_id, "系统") if e.actor_user_id else "系统"
        if e.target_user_id:
            target = names.get(e.target_user_id, f"用户{e.target_user_id}")
        elif e.target_ref:
            target = e.target_ref
        else:
            target = "—"
        created = e.created_at.strftime("%m-%d %H:%M") if e.created_at else ""
        lines.append(f"{created} {_esc(actor)} {action_text} {_esc(target)}")
    return "\n".join(lines)
