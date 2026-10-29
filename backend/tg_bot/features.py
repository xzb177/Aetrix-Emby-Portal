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
    """公益资格三态：未开通／长期有效／至 YYYY-MM-DD。

    口径与 /account 既有写法一致（is_welfare＋welfare_expires_at），只补未开通分支。
    """
    if not bool(getattr(user, "is_welfare", False)):
        return "公益资格：未开通"
    end = getattr(user, "welfare_expires_at", None)
    if end is None:
        return "公益资格：长期有效"
    return f"公益资格：至 {end.strftime('%Y-%m-%d')}"


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
    return f"查看凭证：至 {latest.strftime('%Y-%m-%d')}"


def _bindings_line(db, user_id: int) -> str:
    """网盘绑定：仅 active 行，按 provider 区分展示；已绑带 ✓。"""
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
        names.append("Drive 已绑 ✓")
    if "pan115" in providers:
        names.append("115 已绑 ✓")
    for p in sorted(providers - {"drive", "pan115"}):
        names.append(f"{_esc(p)} 已绑 ✓")
    if not names:
        return "网盘：未绑定，绑定后才能点播"
    return "网盘：" + "、".join(names)


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
    return f"📋 连签：{streak} 天（满 {_streak_renew_min(db)} 天自动续公益资格）"


def _bar(frac: float, width: int) -> str:
    """进度条：█ 已完成／░ 未完成；frac 钳制 0–1，格数四舍五入。"""
    try:
        f = float(frac)
    except (TypeError, ValueError):
        f = 0.0
    f = min(1.0, max(0.0, f))
    filled = min(width, max(0, int(f * width + 0.5)))
    return "█" * filled + "░" * (width - filled)


def _level_info(db, user) -> dict:
    """会员等级只读快照：等级名／经验／下一级阈值与差值；不写库。

    等级数按 member_xp 纯算（复用 lottery_governance.user_level，不触发
    get_member_info 的种子／迁移写副作用）；等级名与阈值取 member_levels
    active 行，表空时回落 DEFAULT_LEVELS。
    """
    from backend import member_level as _ml
    from backend.lottery_governance import user_level as _user_level

    try:
        level = _user_level(db, user.id)
        rows = (
            db.query(models.MemberLevel)
            .filter(models.MemberLevel.is_active == True)  # noqa: E712
            .order_by(models.MemberLevel.level.asc())
            .all()
        )
    except Exception:
        # 部分建表夹具（如 tg_bot 旧测试）没有 member_levels 表：user_level 本身
        # 也会查该表，一并回落默认阈值纯算，dashboard 仍可渲染。本函数只用于
        # 只读展示路径，rollback 安全（后续属性访问按需从库刷新）。
        db.rollback()
        level = None
        rows = []
    levels = (
        [(r.level, r.name, r.xp_threshold or 0) for r in rows]
        if rows
        else [(d["level"], d["name"], d["xp_threshold"]) for d in _ml.DEFAULT_LEVELS]
    )
    xp = int(getattr(user, "member_xp", 0) or 0)
    if level is None:
        level = 1
        for lv, _name, threshold in levels:
            if xp >= threshold:
                level = lv
    cur = None
    for lv, name, threshold in levels:
        if lv == level:
            cur = (lv, name, threshold)
            break
    if cur is None:
        below = [t for t in levels if t[2] <= xp]
        cur = below[-1] if below else levels[0]
        level = cur[0]
    higher = [t for t in levels if t[0] > cur[0]]
    nxt = higher[0] if higher else None
    return {
        "level": cur[0],
        "name": cur[1],
        "xp": xp,
        "cur_threshold": cur[2],
        "next_level": nxt[0] if nxt else None,
        "next_name": nxt[1] if nxt else None,
        "next_threshold": nxt[2] if nxt else None,
        "xp_to_next": (nxt[2] - xp) if nxt else None,
    }


def _level_line(db, user) -> str:
    """会员等级行（/account 完整版）：等级名＋经验＋下一级差值＋进度条。"""
    info = _level_info(db, user)
    if info["next_level"] is None:
        return (
            f"⭐ 会员等级：Lv.{info['level']} {_esc(info['name'])}"
            f" · 经验 {info['xp']}（已满级）"
        )
    span = info["next_threshold"] - info["cur_threshold"]
    frac = (info["xp"] - info["cur_threshold"]) / span if span > 0 else 1.0
    return (
        f"⭐ 会员等级：Lv.{info['level']} {_esc(info['name'])}"
        f" · 经验 {info['xp']}／{info['next_threshold']} {_bar(frac, 10)}"
        f"（距 Lv.{info['next_level']} {_esc(info['next_name'])}还差 {info['xp_to_next']} 经验）"
    )


def compact_dashboard(db, user, display_name: str | None = None) -> str:
    """/start 已绑定用户 dashboard（用户 2026-10-29 晚间亲批瘦身版式，逐字照搬）：

    问候＋账号·等级行之后把资格／状态／成长压成单行，全文 7 行：删三段标题、
    删活力条与馆灵条，资格两行并一行、能量活力并一行、成就连签并一行。
    字段与取值口径沿用上一版（各 helper 三态文案不变），仅改呈现：helper 文案
    里标签后的全角冒号在合并行里换成空格。经验与下一级差值仍不进精简版
    （只放 /account 与 /level）。display_name 缺省取账号名；低于活力阈值时
    保留“暂时播不了”提示，挂 ⚡ 行行尾；馆灵已满级时去掉距下一阶段的括号。
    """
    shown_ach, total_ach = _achievement_summary(db, user.id)
    total_hours, pet_cur, pet_nxt = _pet_summary(db, user.id)
    cap, threshold = _vitality_config(db)
    info = _level_info(db, user)
    greet = display_name or getattr(user, "username", "") or "朋友"

    welfare_text = _welfare_line(user).replace("：", " ", 1)
    view_text = _view_unlock_line(db, user.id).replace("：", " ", 1)

    vitality = getattr(user, "vitality", None)
    if vitality is None:
        vitality_text = "活力 —"
    elif cap <= 0:
        vitality_text = f"活力 {int(vitality)}"
    else:
        v = int(vitality)
        vitality_text = f"活力 {v}/{cap}"
        if v < threshold:
            vitality_text += "（暂时播不了）"

    bindings_text = _bindings_line(db, user.id)
    if bindings_text.startswith("网盘："):
        bindings_text = bindings_text[len("网盘："):]

    pet_text = (
        f"🌱 馆灵 {_esc(pet_cur.get('name', ''))} · 已观影 {total_hours:.1f} 小时"
    )
    if pet_nxt is not None:
        pet_text += (
            f"（距{_esc(pet_nxt.get('name', ''))}还差 "
            f"{pet_nxt['hours'] - total_hours:.1f} 小时）"
        )

    streak_text = _streak_line(db, user.id)
    if streak_text.startswith("📋 "):
        streak_text = streak_text[len("📋 "):]
    streak_text = streak_text.replace("：", " ", 1)

    lines = [
        f"✨ 欢迎回来，{_esc(greet)}",
        f"👤 {_esc(user.username)} · ⭐ Lv.{info['level']} {_esc(info['name'])}",
        f"🎫 {welfare_text} · {view_text}",
        f"⚡ 能量 {int(user.points or 0)} · {vitality_text}",
        f"💾 网盘 {bindings_text}",
        pet_text,
        f"🏅 成就 {len(shown_ach)}/{total_ach} · {streak_text}",
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
        _level_line(db, user),
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


# ---------- 等级治理 bot 补完（Step 0–4＋停权接线，bot 侧入口） ----------

def handle_level(db, tg_user: dict, chat_id: int, args: str) -> str:
    """会员等级：等级＋经验进度＋治理权表（公益服）；付费服只显等级。

    群里可放行（同 /account：只展示个人等级与通用权表，不含凭据）。
    """
    from datetime import datetime as _dt

    from backend import governance as _governance
    from backend import media_seek as _media_seek
    from backend.governance_report import review_weight as _review_weight
    from backend.lottery_governance import (
        direct_cap_for_level as _direct_cap,
        direct_count_this_month as _direct_count,
        proposal_cap_for_level as _proposal_cap,
        proposal_count_this_month as _proposal_count,
    )
    from backend.subscriptions import is_free_realm as _is_free_realm

    user = _bound_user(db, tg_user)
    if user is None:
        return "🔒 会员等级需要先绑定网站账号。\n\n发送 /bind 查看绑定方法。"
    info = _level_info(db, user)
    level = info["level"]
    if info["next_level"] is None:
        progress = f"经验 {info['xp']}（已满级）"
    else:
        span = info["next_threshold"] - info["cur_threshold"]
        frac = (info["xp"] - info["cur_threshold"]) / span if span > 0 else 1.0
        progress = (
            f"经验 {info['xp']}／{info['next_threshold']} {_bar(frac, 10)}"
            f"（距 Lv.{info['next_level']} {_esc(info['next_name'])}还差 {info['xp_to_next']} 经验）"
        )
    lines = [
        "⭐ <b>会员等级</b>",
        "",
        f"👤 {_esc(user.username)} · ⭐ Lv.{level} {_esc(info['name'])}",
        progress,
    ]
    frozen = _governance.is_governance_frozen(db, user.id)
    if frozen:
        lines += ["", "⚠️ 治理特权已被冻结（附议仍按 1 票计，提案／举报／复核暂停）"]
    if not _is_free_realm(db):
        lines += ["", "治理权表仅公益服开放，付费服等级仅展示。"]
        return "\n".join(lines)
    weight = 1 if frozen else _media_seek.vote_weight_for_level(level)
    if level >= 2:
        p_used = _proposal_count(db, user.id, _dt.now())
        p_cap = _proposal_cap(db, level)
        propose_line = f"本月提案：{p_used}／{p_cap} 次"
    else:
        propose_line = "本月提案：Lv.2 起开放"
    if level >= 5:
        d_used = _direct_count(db, user.id, _dt.now())
        d_cap = _direct_cap(db, level)
        direct_line = f"本月直接发起：{d_used}／{d_cap} 次"
    else:
        direct_line = "本月直接发起：Lv.5 起开放"
    rw = _review_weight(level)
    rw_text = str(rw) if rw else "无（Lv.3 起获得）"
    lines += [
        "",
        propose_line + " · " + direct_line,
        "",
        "── 治理权表 ──",
        f"附议权重：Lv.1–Lv.6 ＝ 1–6 票（我的权重：{weight} 票）",
        "活动提案：Lv.2+ 可提案，Lv.2/3 每月 1 次、Lv.4+ 每月 2 次",
        "直接发起：Lv.5 每月 1 次、Lv.6 每月 2 次（事后审计）",
        f"举报复核：Lv.3 权重 1、Lv.4 权重 2、Lv.5 权重 2、Lv.6 权重 3（我的权重：{rw_text}）",
    ]
    if level >= 6:
        lines.append("治理审计日志：Lv.6 可用 /govlog 只读查看")
    else:
        lines.append("治理审计日志：仅 Lv.6 可见（/govlog）")
    return "\n".join(lines)


_REPORT_REASON_ALIASES = {
    "spam": "spam", "刷屏": "spam", "广告": "spam",
    "duplicate": "duplicate", "重复": "duplicate",
    "violation": "violation", "违规": "violation",
}
_REPORT_REASON_NAMES = {"spam": "刷屏／广告", "duplicate": "重复求片", "violation": "违规内容"}


def _report_help() -> str:
    return (
        "🚩 <b>举报求片</b>\n\n"
        "私聊发送：\n"
        "<code>/report 求片编号 原因 [补充说明]</code>\n\n"
        "求片编号来自 /myrequests（如 #123）；\n"
        "原因：刷屏、重复、违规。\n\n"
        "例：<code>/report 123 刷屏 这条求了好几次了</code>"
    )


def handle_report(db, tg_user: dict, chat_id: int, args: str) -> str:
    """举报一条求片：复用 Step 2 create_report，门禁文案由服务端原样返回。"""
    from backend.governance_report import GovernanceError, create_report

    user = _bound_user(db, tg_user)
    if user is None:
        return "🔒 举报需要先绑定网站账号。\n\n发送 /bind 查看绑定方法。"
    parts = (args or "").strip().split(None, 2)
    if len(parts) < 2:
        return _report_help()
    try:
        target_id = int(parts[0].lstrip("#"))
    except ValueError:
        return "求片编号须为数字（如 123）。\n\n" + _report_help()
    reason = _REPORT_REASON_ALIASES.get(parts[1])
    if reason is None:
        return "举报原因只能是：刷屏、重复、违规。\n\n" + _report_help()
    detail = parts[2].strip() if len(parts) > 2 else None
    try:
        row = create_report(db, user, target_id, reason, detail)
    except GovernanceError as exc:
        db.rollback()
        return f"⛔ {_esc(exc.detail)}"
    except Exception:
        db.rollback()
        logger.exception("TG 举报提交失败 user=%s", user.id)
        return "举报提交失败，请稍后再试。"
    target = (
        db.query(models.MovieRequest)
        .filter(models.MovieRequest.id == target_id)
        .first()
    )
    title = f"《{_esc(target.movie_name)}》" if target is not None else f"求片 #{target_id}"
    return (
        "✅ <b>举报已提交</b>\n\n"
        f"{title}\n"
        f"举报编号：<code>#{row['id']}</code> · 原因：{_REPORT_REASON_NAMES[reason]}\n\n"
        "进入复核流程，结果会在复核后更新。"
    )


_PRIZE_TYPE_ALIASES = {
    "points": "points", "积分": "points",
    "days": "days", "天数": "days",
    "whitelist": "whitelist", "白名单": "whitelist",
}


def _parse_prize_spec(spec: str) -> list[dict]:
    """奖品段：逗号分隔，每项 类型:价值[x数量]，如 积分:100x2,天数:30x1。"""
    prizes = []
    for chunk in spec.split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        if ":" not in chunk:
            raise ValueError("奖品格式应为 类型:价值[x数量]，如 积分:100x2")
        ptype_raw, rest = chunk.split(":", 1)
        ptype = _PRIZE_TYPE_ALIASES.get(ptype_raw.strip())
        if ptype is None:
            raise ValueError("奖品类型只能是 积分／天数／白名单")
        if "x" in rest:
            value_raw, qty_raw = rest.split("x", 1)
        else:
            value_raw, qty_raw = rest, "1"
        try:
            value = int(value_raw.strip())
            qty = int(qty_raw.strip())
        except ValueError:
            raise ValueError("奖品价值与数量须为整数")
        prizes.append({"type": ptype, "value": value, "quantity": qty, "name": ""})
    if not prizes:
        raise ValueError("至少需要配置 1 个奖品")
    return prizes


def _parse_lottery_args(args: str) -> dict:
    """提案／直接发起参数：标题 | 奖品 | 群组ID [| 开奖时间] [| 人数上限] [| 口令]。

    群组ID 可空（仅当允许群恰好一个时服务端自动取）；开奖时间如 2026-11-01 20:00，
    为空则开奖时间待定；人数上限为空则不限；口令给出时为口令抽奖，否则为按钮抽奖。
    """
    segments = [s.strip() for s in (args or "").split("|")]
    if len(segments) < 2 or not segments[0] or not segments[1]:
        raise ValueError("参数不足")
    title = segments[0]
    prizes = _parse_prize_spec(segments[1])
    chat_id = None
    if len(segments) > 2 and segments[2]:
        try:
            chat_id = int(segments[2])
        except ValueError:
            raise ValueError("群组ID 须为整数")
    draw_at = segments[3] if len(segments) > 3 and segments[3] else None
    max_participants = None
    if len(segments) > 4 and segments[4]:
        try:
            max_participants = int(segments[4])
        except ValueError:
            raise ValueError("人数上限须为整数")
    keyword = segments[5] if len(segments) > 5 and segments[5] else None
    return {
        "title": title,
        "prizes": prizes,
        "chat_id": chat_id,
        "draw_at": draw_at,
        "max_participants": max_participants,
        "lottery_type": "password" if keyword else "button",
        "password_keyword": keyword,
    }


def _propose_help(direct: bool) -> str:
    verb = "直接发起" if direct else "活动提案"
    cmd = "/launch" if direct else "/propose"
    level = "Lv.5+" if direct else "Lv.2+"
    return (
        f"🎲 <b>{verb}</b>（仅公益服、{level}）\n\n"
        "私聊发送：\n"
        f"<code>{cmd} 标题 | 奖品 | 群组ID | 开奖时间 | 人数上限 | 口令</code>\n\n"
        "奖品：逗号分隔，类型:价值[x数量]，类型为 积分／天数／白名单，\n"
        "如 <code>积分:100x2,天数:30x1</code>。\n"
        "群组ID 可空（仅当允许群恰好一个时自动取）；开奖时间如 2026-11-01 20:00，\n"
        "为空则待定；人数上限为空则不限；口令给出时为口令抽奖，否则为按钮抽奖。\n\n"
        "例：<code>/propose 周末观影趴 | 积分:100x2 | -1001234567890</code>"
    )


def handle_propose(db, tg_user: dict, chat_id: int, args: str) -> str:
    """活动提案：复用 Step 3 submit_proposal，门禁文案由服务端原样返回。"""
    from backend.lottery_governance import LotteryGovernanceError, submit_proposal

    user = _bound_user(db, tg_user)
    if user is None:
        return "🔒 活动提案需要先绑定网站账号。\n\n发送 /bind 查看绑定方法。"
    if not (args or "").strip():
        return _propose_help(direct=False)
    try:
        payload = _parse_lottery_args(args)
    except ValueError as exc:
        return f"{_esc(exc)}\n\n" + _propose_help(direct=False)
    try:
        proposal = submit_proposal(db, user.id, payload)
    except LotteryGovernanceError as exc:
        db.rollback()
        return f"⛔ {_esc(exc.detail)}"
    except Exception:
        db.rollback()
        logger.exception("TG 活动提案提交失败 user=%s", user.id)
        return "活动提案提交失败，请稍后再试。"
    return (
        "✅ <b>活动提案已提交</b>\n\n"
        f"📌 {_esc(proposal.title)}\n"
        f"提案编号：<code>#{proposal.id}</code>\n\n"
        "等待管理员批准，批准后开奖。"
    )


def handle_direct_launch(db, tg_user: dict, chat_id: int, args: str) -> str:
    """直接发起活动：复用 Step 3 direct_launch，门禁文案由服务端原样返回。"""
    from backend.lottery_governance import LotteryGovernanceError, direct_launch

    user = _bound_user(db, tg_user)
    if user is None:
        return "🔒 直接发起活动需要先绑定网站账号。\n\n发送 /bind 查看绑定方法。"
    if not (args or "").strip():
        return _propose_help(direct=True)
    try:
        payload = _parse_lottery_args(args)
    except ValueError as exc:
        return f"{_esc(exc)}\n\n" + _propose_help(direct=True)
    try:
        round_obj, _record = direct_launch(db, user.id, payload)
    except LotteryGovernanceError as exc:
        db.rollback()
        return f"⛔ {_esc(exc.detail)}"
    except Exception:
        db.rollback()
        logger.exception("TG 直接发起活动失败 user=%s", user.id)
        return "直接发起活动失败，请稍后再试。"
    return (
        "✅ <b>活动已直接发起</b>\n\n"
        f"📌 {_esc(round_obj.title)}\n"
        f"轮次编号：<code>#{round_obj.id}</code>\n\n"
        "直接发起已记入事后审计，管理员可复核。"
    )
