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

PRIVATE_ONLY_TEXT = "🔒 个人查询请私聊我发送，群里不查"
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
        return (
            "🔗 <b>我的网盘绑定</b>\n"
            "\n"
            "你还没有绑定网盘。\n"
            "\n"
            + BIND_GUIDE_SHORT
        )
    lines = ["🔗 <b>我的网盘绑定</b>", ""]
    for row in rows:
        provider = PROVIDER_TEXT.get(row.provider, html.escape(str(row.provider)))
        status = BINDING_STATUS_TEXT.get(
            row.status, html.escape(str(row.status)))
        line = (
            f"• {html.escape(str(row.account_label))}"
            f"（{provider}）｜状态：{status}"
        )
        if row.status != "active" and row.last_error_kind:
            msg = BINDING_ERROR_MESSAGES.get(str(row.last_error_kind))
            if msg:
                line += f"\n　　{html.escape(msg)}"
        lines.append(line)
    lines.append("")
    lines.append("绑定失效时请在网页端「我的网盘」里重新绑定。")
    return "\n".join(lines)


def render_transfers(db, user) -> str:
    """该用户最近的转存任务（只查自己的 owner_scope=user 行）。"""
    jobs = _user_jobs(db, user.id)
    if not jobs:
        return "📦 <b>转存进度</b>\n\n还没有转存任务。"
    lines = ["📦 <b>转存进度</b>", ""]
    for job in jobs:
        lines.append(_job_line(job, _item_name(db, job.item_id)))
    return "\n".join(lines)


def render_wants(db, user) -> str:
    """该用户的想看条目（本人 trigger=store_want 的转存任务行）。"""
    jobs = _user_jobs(db, user.id, trigger="store_want")
    if not jobs:
        return "⭐ <b>我的想看</b>\n\n还没有想看的条目。"
    lines = ["⭐ <b>我的想看</b>", ""]
    for job in jobs:
        lines.append(_job_line(job, _item_name(db, job.item_id)))
    return "\n".join(lines)


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


def diagnose_whyplay(db, user, title: str) -> str:
    """为什么播不了：只解释、不放行、不建任务、不发播放链接。

    诊断链（与 #1405 门禁同一口径，只读镜像）：
    byos 在盘 → 来源不明 → 无绑定 → 错 provider（绑哪播哪）→
    绑定失效 → 副本就绪（含权限收回）→ 转存中 → 转存失败 → 未转存。
    """
    title = (title or "").strip()
    if not title:
        return (
            "❓ <b>为什么播不了</b>\n"
            "\n"
            "用法：<code>/whyplay 片名</code>\n"
            "例如：<code>/whyplay 嫁金钗</code>"
        )
    items = _find_items(db, title)
    if not items:
        return (
            "❓ <b>为什么播不了</b>\n"
            "\n"
            f"没找到叫「{html.escape(title)}」的片子，请检查片名再试。"
        )
    if len(items) > 1:
        lines = [
            "❓ <b>为什么播不了</b>",
            "",
            "片名太宽泛，找到了多个结果，请发送更完整的片名：",
            "",
        ]
        for row in items[:5]:
            lines.append(f"• 《{html.escape(str(row.name))}》")
        return "\n".join(lines)

    item = items[0]
    name = html.escape(str(item.name))
    head = f"❓ <b>为什么播不了</b>｜《{name}》\n\n"

    path = (getattr(item, "file_path", "") or "").strip()
    if path.startswith("byos://"):
        return head + "这部片已在你自己的盘里，直接点播即可。"
    provider = _item_provider(db, item)
    if provider is None:
        return (
            head
            + "这部片的来源暂无法识别，请在网页端重试一次，"
            "仍播不了请联系客服。"
        )
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
            return (
                head
                + "你还没有绑定网盘，点播需要先绑定自己的账号。\n\n"
                + BIND_GUIDE_SHORT
            )
        if matching:
            row = matching[0]
            msg = BINDING_ERROR_MESSAGES.get(str(row.last_error_kind or ""))
            detail = f"\n　　{html.escape(msg)}" if msg else ""
            return (
                head
                + f"你绑的{provider_text}已失效，点播前请先重新绑定。{detail}\n\n"
                + BIND_GUIDE_SHORT
            )
        other = "pan115" if provider == "drive" else "drive"
        other_text = PROVIDER_TEXT.get(other, html.escape(str(other)))
        return (
            head
            + f"你只绑了{other_text}，这部片是{provider_text}的片，"
            "按“绑哪播哪”会转存到你绑的盘里再播。\n"
            "去详情页点一次播放，系统会先转存，转完才可点。"
        )
    variant = _ready_variant(db, user.id, item.id)
    if variant is not None:
        if getattr(variant, "share_revoked_at", None) is not None:
            return (
                head
                + "副本已就绪，但分享权限曾被收回，重新点播时会自动恢复。\n"
                "如果仍播不了，把这部片从播放器的“继续观看”里移除，"
                "再从详情页重新点播放。"
            )
        return (
            head
            + "副本已就绪，服务端这边没问题。\n"
            "如果播放器里仍播不了，多半是它用了旧链接："
            "把这部片从“继续观看”里移除（或退出重进），"
            "再从详情页重新点播放，拿到新链接即可。"
        )
    job = _latest_job(db, user.id, item.id)
    if job is not None:
        if job.status in ("pending", "running"):
            return (
                head
                + "正在转存到你的盘里，转完才可点播。\n"
                f"任务开始于 {_fmt_time(job.created_at)}，请稍后再从详情页点播放。"
            )
        if job.status == "failed":
            kind = str(job.error_kind or "")
            reason = JOB_ERROR_TEXT.get(kind, f"原因码：{html.escape(kind)}" if kind else "未知原因")
            return (
                head
                + f"上一次转存失败（{reason}）。\n"
                "重新点播一次可再试，仍失败请联系客服。"
            )
        if job.status == "done":
            return (
                head
                + "转存任务已完成，但副本行暂未就绪。\n"
                "重新点播一次，仍播不了请联系客服。"
            )
    return (
        head
        + "这部片还没有转存到你的盘里。\n"
        "去详情页点一次播放，系统会先转存再播，转完才可点。"
    )
