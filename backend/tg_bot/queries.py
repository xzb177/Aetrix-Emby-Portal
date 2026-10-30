"""TG Bot P1 自助查询（只读）：查绑定 / 查转存 / 为什么播不了 / 查想看。

硬规矩：
- 只读：本模块不建任何行、不改任何行、不提交事务；调用方只做查询。
- 只查本人：一切查询以 TG 绑定的站账号为准；未绑定先指引绑定。
- 绝不发播放链接、不绕 #1405 门禁：whyplay 只解释原因和下一步。
- 回复里不带 token、密钥、凭据 id、他人信息；片名与绑定名做 HTML 转义。
- 群聊里不查个人数据：调用方（handlers 包装层）先拦私聊外请求。
"""
from __future__ import annotations

import html
import logging
from datetime import datetime

from backend.emby_server import models as em
from backend.tg_bot import layout
from backend.emby_server.transfer_variant import _item_provider
from backend.emby_server.user_bindings import BINDING_ERROR_MESSAGES

logger = logging.getLogger(__name__)

PROVIDER_TEXT = {"drive": "谷歌网盘", "pan115": "115 网盘"}
BINDING_STATUS_TEXT = {"active": "正常", "invalid": "已失效", "expired": "已过期"}
JOB_STATUS_TEXT = {
    "pending": "排队中",
    "running": "转存中",
    "done": "已完成",
    "failed": "失败",
    "cancelled": "已取消",
}
# 转存失败原因码 → 一句话（未知码原样展示，不编造）
JOB_ERROR_TEXT = {
    "no_binding": "绑定已失效",
    "auth_error": "授权已失效",
    "dead": "原件已失效",
    "unsupported": "暂不支持该转存",
    "no_drive": "源文件缺失",
    "no_source_fid": "源文件缺失",
    "orphan": "副本权限异常",
    "item_gone": "原片已下架",
    "throttled": "触发限流，稍后再试",
    "retriable": "转存失败，可重试",
}
RECENT_LIMIT = 5
SEARCH_LIMIT = 6

PRIVATE_ONLY_TEXT = layout.card(
    layout.title("私聊查询"),
    [layout.row("🔒", "使用方式", "个人查询请私聊我发送，群里不查")],
)
BIND_GUIDE_SHORT = (
    "🔗 请先在网页端「我的网盘」里绑定你的网盘，绑定后再回来查询。"
)


def _fmt_time(dt) -> str:
    if isinstance(dt, datetime):
        return dt.strftime("%m-%d %H:%M")
    return "—"


def _job_line(job, item_name: str) -> str:
    status = JOB_STATUS_TEXT.get(job.status, html.escape(str(job.status)))
    suffix = ""
    if job.status == "failed" and getattr(job, "error_kind", None):
        kind = str(job.error_kind)
        suffix = f"（{JOB_ERROR_TEXT.get(kind, '原因码：' + html.escape(kind))}）"
    return (
        f"• 《{html.escape(item_name)}》｜{status}{suffix}"
        f"｜{_fmt_time(job.created_at)}"
    )


def _item_name(db, item_id: int) -> str:
    row = db.query(em.MediaItem).filter(em.MediaItem.id == item_id).first()
    return str(row.name) if row is not None else "未知条目"


def _user_jobs(db, user_id: int, trigger: str | None = None):
    q = (
        db.query(em.TransferJob)
        .filter(
            em.TransferJob.owner_scope == "user",
            em.TransferJob.owner_ref == str(user_id),
        )
    )
    if trigger is not None:
        q = q.filter(em.TransferJob.trigger == trigger)
    return q.order_by(em.TransferJob.id.desc()).limit(RECENT_LIMIT).all()


def render_bindings(db, user) -> str:
    """该用户名下的网盘绑定（label、provider、status，不含任何凭据信息）。"""
    rows = (
        db.query(em.UserStorageBinding)
        .filter(em.UserStorageBinding.user_id == user.id)
        .order_by(em.UserStorageBinding.id)
        .all()
    )
    if not rows:
        return layout.card(
            layout.title("我的网盘绑定"),
            [layout.row("🔗", "绑定状态", "你还没有绑定网盘")],
            [BIND_GUIDE_SHORT],
        )
    card_rows = []
    for row in rows:
        provider = PROVIDER_TEXT.get(row.provider, html.escape(str(row.provider)))
        status = BINDING_STATUS_TEXT.get(
            row.status, html.escape(str(row.status)))
        card_rows.append(layout.row(
            "🔗", html.escape(str(row.account_label)),
            f"{provider}｜状态：{status}"))
        if row.status != "active" and row.last_error_kind:
            msg = BINDING_ERROR_MESSAGES.get(str(row.last_error_kind))
            if msg:
                card_rows.append(layout.row("⚠️", "失效原因", html.escape(msg)))
    return layout.card(
        layout.title("我的网盘绑定"), card_rows,
        ["绑定失效时请在网页端「我的网盘」里重新绑定。"])


def render_transfers(db, user) -> str:
    """该用户最近的转存任务（只查自己的 owner_scope=user 行）。"""
    jobs = _user_jobs(db, user.id)
    if not jobs:
        return layout.card(
            layout.title("转存进度"),
            [layout.row("📦", "转存任务", "还没有转存任务")],
        )
    card_rows = [
        layout.row("📦", "转存任务", _job_line(job, _item_name(db, job.item_id)))
        for job in jobs
    ]
    return layout.card(layout.title("转存进度"), card_rows)


def render_wants(db, user) -> str:
    """该用户的想看条目（本人 trigger=store_want 的转存任务行）。"""
    jobs = _user_jobs(db, user.id, trigger="store_want")
    if not jobs:
        return layout.card(
            layout.title("我的想看"),
            [layout.row("⭐", "想看条目", "还没有想看的条目")],
        )
    card_rows = [
        layout.row("⭐", "想看条目", _job_line(job, _item_name(db, job.item_id)))
        for job in jobs
    ]
    return layout.card(layout.title("我的想看"), card_rows)


def _find_items(db, title: str):
    """按片名模糊找可见条目（软删除与合并副本排除）。"""
    pattern = f"%{title}%"
    return (
        db.query(em.MediaItem)
        .filter(
            em.MediaItem.name.ilike(pattern),
            em.MediaItem.deleted_at.is_(None),
            em.MediaItem.merged_into_id.is_(None),
        )
        .order_by(em.MediaItem.id)
        .limit(SEARCH_LIMIT)
        .all()
    )


def _latest_job(db, user_id: int, item_id: int):
    return (
        db.query(em.TransferJob)
        .filter(
            em.TransferJob.item_id == item_id,
            em.TransferJob.owner_scope == "user",
            em.TransferJob.owner_ref == str(user_id),
        )
        .order_by(em.TransferJob.id.desc())
        .first()
    )


def _ready_variant(db, user_id: int, item_id: int):
    return (
        db.query(em.TransferVariant)
        .filter(
            em.TransferVariant.item_id == item_id,
            em.TransferVariant.owner_scope == "user",
            em.TransferVariant.owner_ref == str(user_id),
            em.TransferVariant.status == "ready",
        )
        .order_by(em.TransferVariant.id.desc())
        .first()
    )


def _whyplay_card(name: str, rows: list[str], notes: list[str] | None = None) -> str:
    heading = "为什么播不了" + (f"｜《{name}》" if name else "")
    return layout.card(layout.title(heading), rows, notes)


def diagnose_whyplay(db, user, title: str) -> str:
    """为什么播不了：只解释、不放行、不建任务、不发播放链接。

    诊断链（与 #1405 门禁同一口径，只读镜像）：
    byos 在盘 → 来源不明 → 无绑定 → 错 provider（绑哪播哪）→
    绑定失效 → 副本就绪（含权限收回）→ 转存中 → 转存失败 → 未转存。
    """
    title = (title or "").strip()
    if not title:
        return _whyplay_card("", [
            layout.row("❓", "用法", "<code>/whyplay 片名</code>"),
            layout.row("💡", "例如", "<code>/whyplay 嫁金钗</code>"),
        ])
    items = _find_items(db, title)
    if not items:
        return _whyplay_card("", [
            layout.row("🔍", "查找结果", f"没找到叫「{html.escape(title)}」的片子，请检查片名再试"),
        ])
    if len(items) > 1:
        rows = [layout.row(
            "🔍", "查找结果", "片名太宽泛，找到了多个结果，请发送更完整的片名")]
        for row in items[:5]:
            rows.append(layout.row("🎬", "候选影片", f"《{html.escape(str(row.name))}》"))
        return _whyplay_card("", rows)

    item = items[0]
    name = html.escape(str(item.name))

    path = (getattr(item, "file_path", "") or "").strip()
    if path.startswith("byos://"):
        return _whyplay_card(name, [
            layout.row("💾", "所在位置", "这部片已在你自己的盘里，直接点播即可"),
        ])
    provider = _item_provider(db, item)
    if provider is None:
        return _whyplay_card(name, [
            layout.row("❓", "来源", "这部片的来源暂无法识别，请在网页端重试一次，仍播不了请联系客服"),
        ])
    provider_text = PROVIDER_TEXT.get(provider, html.escape(str(provider)))
    bindings = (
        db.query(em.UserStorageBinding)
        .filter(em.UserStorageBinding.user_id == user.id)
        .order_by(em.UserStorageBinding.id)
        .all()
    )
    matching = [b for b in bindings if b.provider == provider]
    active_matching = [b for b in matching if b.status == "active"]
    if not active_matching:
        if not bindings:
            return _whyplay_card(name, [
                layout.row("🔗", "网盘绑定", "你还没有绑定网盘，点播需要先绑定自己的账号"),
            ], [BIND_GUIDE_SHORT])
        if matching:
            row = matching[0]
            rows = [layout.row(
                "🔗", "网盘绑定", f"你绑的{provider_text}已失效，点播前请先重新绑定")]
            msg = BINDING_ERROR_MESSAGES.get(str(row.last_error_kind or ""))
            if msg:
                rows.append(layout.row("⚠️", "失效原因", html.escape(msg)))
            return _whyplay_card(name, rows, [BIND_GUIDE_SHORT])
        other = "pan115" if provider == "drive" else "drive"
        other_text = PROVIDER_TEXT.get(other, html.escape(str(other)))
        return _whyplay_card(name, [
            layout.row(
                "🔀", "绑定与来源",
                f"你只绑了{other_text}，这部片是{provider_text}的片，"
                "按“绑哪播哪”会转存到你绑的盘里再播"),
            layout.row("▶️", "下一步", "去详情页点一次播放，系统会先转存，转完才可点"),
        ])
    variant = _ready_variant(db, user.id, item.id)
    if variant is not None:
        if getattr(variant, "share_revoked_at", None) is not None:
            return _whyplay_card(name, [
                layout.row("📦", "副本状态", "副本已就绪，但分享权限曾被收回，重新点播时会自动恢复"),
                layout.row("▶️", "下一步", "如果仍播不了，把这部片从播放器的“继续观看”里移除，再从详情页重新点播放"),
            ])
        return _whyplay_card(name, [
            layout.row("📦", "副本状态", "副本已就绪，服务端这边没问题"),
            layout.row("▶️", "下一步", "如果播放器里仍播不了，多半是它用了旧链接：把这部片从“继续观看”里移除（或退出重进），再从详情页重新点播放，拿到新链接即可"),
        ])
    job = _latest_job(db, user.id, item.id)
    if job is not None:
        if job.status in ("pending", "running"):
            return _whyplay_card(name, [
                layout.row("📦", "转存状态", "正在转存到你的盘里，转完才可点播"),
                layout.row("🕒", "任务开始", f"{_fmt_time(job.created_at)}，请稍后再从详情页点播放"),
            ])
        if job.status == "failed":
            kind = str(job.error_kind or "")
            reason = JOB_ERROR_TEXT.get(kind, f"原因码：{html.escape(kind)}" if kind else "未知原因")
            return _whyplay_card(name, [
                layout.row("📦", "转存状态", f"上一次转存失败（{reason}）"),
                layout.row("▶️", "下一步", "重新点播一次可再试，仍失败请联系客服"),
            ])
        if job.status == "done":
            return _whyplay_card(name, [
                layout.row("📦", "转存状态", "转存任务已完成，但副本行暂未就绪"),
                layout.row("▶️", "下一步", "重新点播一次，仍播不了请联系客服"),
            ])
    return _whyplay_card(name, [
        layout.row("📦", "转存状态", "这部片还没有转存到你的盘里"),
        layout.row("▶️", "下一步", "去详情页点一次播放，系统会先转存再播，转完才可点"),
    ])


# ---------------- 观影时长榜聚合（海报日推数据源，只读） ----------------

def watch_duration_rank(db, period: str, limit: int = 10) -> list[dict]:
    """观影时长榜 Top N：[{user_id, username, seconds}]，秒数降序。

    数据源与口径（与账号面板「已观影 X 小时」同源同口径，不新造统计）：
    - 表：emby_playback_sessions（Emby 播放会话行），秒数走
      backend.panel_achievements.playback_seconds——position 口径
      （min(position_ticks, duration_ticks)），暂停时 position 不前进、
      不计入；片长取条目 duration_ticks（会话行须能 join 到条目，
      条目已删的会话不计）。
    - 窗口：复用 emby_server.portal.leaderboard_window——day＝
      Asia/Shanghai 自然日、week＝ISO 周（周一起）；会话按 start_time
      （UTC 入库帧）落窗，昨晚开播跨进今天的会话算昨天、不进今日榜。
    - 上榜过滤与站内时长榜一致：show_on_leaderboard 为 False 的用户
      不上榜（NULL 视为允许）；总秒数 <= 0 不上榜。
    - 排序：秒数降序，同秒按 user_id 升序，保证同分名次稳定。
    - 用户名展示口径与 bot /rank、站内时长榜一致（WebUser.username 原样）。
    """
    from backend.emby_server.portal import leaderboard_window
    from backend.models import WebUser
    from backend.panel_achievements import playback_seconds

    _start_sh, start_utc = leaderboard_window(period)
    rows = (
        db.query(
            em.PlaybackSession.user_id,
            em.PlaybackSession.position_ticks,
            em.MediaItem.duration_ticks,
        )
        .join(em.MediaItem, em.MediaItem.id == em.PlaybackSession.item_id)
        .filter(
            em.PlaybackSession.start_time >= start_utc,
            em.PlaybackSession.user_id.isnot(None),
        )
        .all()
    )
    totals: dict[int, int] = {}
    for row in rows:
        secs = playback_seconds(row)
        if secs > 0:
            totals[int(row.user_id)] = totals.get(int(row.user_id), 0) + int(secs)
    if not totals:
        return []
    users = (
        db.query(WebUser.id, WebUser.username, WebUser.show_on_leaderboard)
        .filter(WebUser.id.in_(list(totals)))
        .all()
    )
    names = {
        int(uid): (username or "")
        for uid, username, shown in users
        if shown is not False
    }
    ranked = sorted(
        ((uid, secs) for uid, secs in totals.items() if uid in names),
        key=lambda kv: (-kv[1], kv[0]),
    )
    return [
        {"user_id": uid, "username": names[uid], "seconds": int(secs)}
        for uid, secs in ranked[: max(1, int(limit or 10))]
    ]
