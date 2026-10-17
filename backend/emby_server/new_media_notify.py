"""新片入库通知（富媒体版）：扫描完成后，给管理员推 TG 富媒体消息

要解决的问题：扫描器发现新片后从不通知，管理员不知道库里进了什么。
通知管道（TG/邮件/站内）早已存在，这里只做「扫描完成 → 汇总新增 → 发 TG」的接线。

设计（"功能只提供能力"，库名/标题/URL 全从数据/配置里取）：
- 触发：扫描成功 / 部分成功且本轮有新增条目时（``maybe_notify_new_media``，
  由 ``scan_queue._run_task`` 在扫描提交完成后调用）。
- 同一轮扫描只发一条汇总消息，不逐部刷屏。
- 查询新增用**独立 Session**（``SessionLocal()`` 新开），绝不碰扫描线程的
  Session——PR #202 的并发教训。
- 真正的活在 daemon 线程里做：查库 → 组消息 → 发 TG。任何异常只记日志，
  绝不影响扫描本身。
- 富媒体：有 TMDB 海报的新片用 sendPhoto；多部用 sendMediaGroup（相册，
  caption 挂第一张）；发图失败自动降级为纯文本，通知不丢。
- 排版用 HTML parse_mode（标题加粗、年份/集数/评分享受结构化展示），
  所有来自数据的文本都做 HTML 转义。
- 配置（SystemConfig）：``new_media_notify_enabled``（"1"/"0"，默认 "1"，开箱即有）、
  ``new_media_notify_channels``（逗号分隔，默认 "telegram"，以后可扩展）。
- 收件人：全部启用中的管理员（``is_staff``），走他们绑定的 TG 账号。
  没绑 TG 的管理员记一条 warning 跳过，不报错。
"""

from __future__ import annotations

import asyncio
import html
import logging
import threading
from datetime import datetime
from typing import Optional

from sqlalchemy.orm import Session

from backend import models
from backend.database import SessionLocal
from backend.emby_server import models as emby_models
from backend.integrations import store

logger = logging.getLogger(__name__)

CONFIG_ENABLED = "new_media_notify_enabled"
CONFIG_CHANNELS = "new_media_notify_channels"

DEFAULT_ENABLED = True
DEFAULT_CHANNELS = "telegram"

# 消息里最多展示的条目个数，超出截断（防刷屏）
MAX_ITEMS = 10
# 纯文本消息上限（Telegram 4096）；caption 上限由 TelegramChannel 处理
MAX_TEXT_LEN = 4000


def _get_config(db: Session) -> tuple[bool, list[str]]:
    """读通知配置；返回 (是否启用, 渠道列表)。"""
    vals = store.read_values(
        db,
        [CONFIG_ENABLED, CONFIG_CHANNELS],
        {
            CONFIG_ENABLED: "1" if DEFAULT_ENABLED else "0",
            CONFIG_CHANNELS: DEFAULT_CHANNELS,
        },
    )
    enabled = vals[CONFIG_ENABLED].strip() == "1"
    channels = [c.strip().lower() for c in vals[CONFIG_CHANNELS].split(",") if c.strip()]
    if not channels:
        channels = [DEFAULT_CHANNELS]
    return enabled, channels


def maybe_notify_new_media(
    library_id: int,
    library_name: str,
    started_at: Optional[datetime],
    added_count: int,
) -> None:
    """扫描完成后的通知入口（在扫描工作线程里调用）。

    只是起一个 daemon 线程，立即返回——TG 投递再慢也不会拖住扫描收尾。
    本轮没有新增条目时直接返回，不打扰任何人。
    """
    if not added_count or added_count <= 0:
        return
    thread = threading.Thread(
        target=_notify_worker,
        args=(library_id, library_name, started_at),
        name=f"new-media-notify-{library_id}",
        daemon=True,
    )
    thread.start()


def _notify_worker(
    library_id: int,
    library_name: str,
    started_at: Optional[datetime],
) -> None:
    """后台线程：查本轮新增 → 组消息 → 发给管理员。异常只记日志。"""
    try:
        _do_notify(library_id, library_name, started_at)
    except Exception:  # noqa: BLE001 — 通知失败绝不能影响扫描
        logger.warning(
            "新片入库通知失败（库 id=%s）", library_id, exc_info=True
        )


def _do_notify(
    library_id: int,
    library_name: str,
    started_at: Optional[datetime],
) -> None:
    db = SessionLocal()
    try:
        enabled, channels = _get_config(db)
        if not enabled:
            return
        if started_at is None:
            return

        collection_type = _library_collection_type(db, library_id)
        summary = _summarize_new_items(db, library_id, started_at)
        if summary is None:
            return

        emoji = _library_emoji(library_name, collection_type)
        text = _build_message(emoji, library_name, summary)
        photos = _collect_photos(summary)

        staff_ids = _staff_ids(db)
        if not staff_ids:
            logger.warning("新片入库通知：没有启用中的管理员，跳过发送")
            return
    finally:
        db.close()

    # 投递是异步的；工作线程里没有事件循环，用 asyncio.run 新起一个
    asyncio.run(_send_to_staff(staff_ids, text, photos, channels))


def _library_collection_type(db: Session, library_id: int) -> str:
    """查库的 collection_type（movies/tvshows/mixed），查不到返回空。"""
    try:
        row = (
            db.query(emby_models.Library.collection_type)
            .filter(emby_models.Library.id == library_id)
            .first()
        )
        return (row[0] or "") if row else ""
    except Exception:  # noqa: BLE001 — 查不到就按默认展示
        logger.warning("查库类型失败（库 id=%s）", library_id, exc_info=True)
        return ""


def _library_emoji(library_name: str, collection_type: str) -> str:
    """按库给 emoji：动漫/剧集/电影不同图标。名字和类型都从数据里取。"""
    name = library_name or ""
    if any(k in name for k in ("动漫", "动画", "番剧")):
        return "📚"
    if collection_type == "movies" or any(k in name for k in ("电影", "movie")):
        return "🎬"
    if collection_type == "tvshows" or any(k in name for k in ("剧", "综艺", "演唱会")):
        return "📺"
    return "📚"


def _summarize_new_items(
    db: Session, library_id: int, started_at: datetime
) -> Optional[dict]:
    """汇总本轮新增：只用聚合/小批量查询，不把几万条 ORM 对象全载进内存。

    返回：
    - items: 新剧/新电影列表（最多 MAX_ITEMS 条），每条含 id/name/类型/年份/
      评分/海报 URL/本轮新增集数
    - total: 新剧/新电影总数
    - episode_groups: 老剧出新集 [(剧名, 季号, 集数, 剧 id, 海报 URL)]
    返回 None 表示本轮无新增。
    """
    from sqlalchemy import func

    MI = emby_models.MediaItem
    base = [MI.library_id == library_id, MI.date_added >= started_at]

    # --- 新剧 / 新电影：总数 + 前 N 条详情 ---
    sm_q = db.query(MI).filter(*base, MI.item_type.in_(["series", "movie"]))
    sm_total = sm_q.count()
    sm_rows = (
        sm_q.order_by(MI.date_added).limit(MAX_ITEMS).all()
    )

    # 本轮各剧新增的集数（给新剧标"12 集"用）
    ep_count_by_series: dict[int, int] = {}
    if sm_rows:
        series_ids = [r.id for r in sm_rows if r.item_type == "series"]
        if series_ids:
            for sid, cnt in (
                db.query(MI.series_id, func.count(MI.id))
                .filter(
                    *base,
                    MI.item_type == "episode",
                    MI.series_id.in_(series_ids),
                )
                .group_by(MI.series_id)
                .all()
            ):
                ep_count_by_series[sid] = cnt

    items = []
    for r in sm_rows:
        items.append({
            "id": r.id,
            "name": (r.name or "").strip() or f"条目#{r.id}",
            "item_type": r.item_type,
            "year": r.production_year,
            "rating": r.community_rating,
            "poster": _safe_url(r.primary_image_url),
            "episode_count": ep_count_by_series.get(r.id, 0),
        })

    # --- 老剧出新集：按 (series_id, season_number) 分组 ---
    ep_rows = (
        db.query(
            MI.series_id, MI.season_number, func.count(MI.id),
        )
        .filter(*base, MI.item_type == "episode")
        .group_by(MI.series_id, MI.season_number)
        .all()
    )

    # 新剧自己的集不算"老剧出新集"（上面已统计过）
    new_series_ids = {r.id for r in sm_rows if r.item_type == "series"}
    ep_rows = [row for row in ep_rows if row[0] not in new_series_ids]

    episode_groups = []
    if ep_rows:
        series_ids = sorted({sid for sid, _, _ in ep_rows if sid})
        meta: dict[int, dict] = {}
        if series_ids:
            for r in (
                db.query(MI)
                .filter(MI.id.in_(series_ids))
                .all()
            ):
                meta[r.id] = {
                    "name": (r.name or "").strip() or f"剧集#{r.id}",
                    "poster": _safe_url(r.primary_image_url),
                }
        for sid, season_no, cnt in sorted(
            ep_rows, key=lambda x: (meta.get(x[0], {}).get("name", ""), x[1] or 0)
        ):
            m = meta.get(sid, {})
            episode_groups.append({
                "series_id": sid,
                "name": m.get("name") or "未知剧集",
                "season": season_no,
                "count": cnt,
                "poster": m.get("poster"),
            })

    if sm_total == 0 and not episode_groups:
        return None

    return {
        "items": items,
        "total": sm_total,
        "episode_groups": episode_groups,
    }


def _safe_url(url: Optional[str]) -> Optional[str]:
    """只接受 http(s) 海报 URL，其他一律丢掉（防 SSRF/坏数据）。"""
    u = (url or "").strip()
    if u.startswith("http://") or u.startswith("https://"):
        return u
    return None


def _fmt_rating(rating) -> Optional[str]:
    try:
        v = float(rating)
    except (TypeError, ValueError):
        return None
    if v <= 0:
        return None
    return f"{v:.1f}"


def _build_message(emoji: str, library_name: str, summary: dict) -> str:
    """组 HTML 消息：标题加粗、年份/集数/评分享受结构化展示。"""
    lines: list[str] = [f"{emoji} <b>新片入库 · {html.escape(library_name)}</b>", ""]

    for it in summary["items"]:
        name = html.escape(it["name"])
        if it["item_type"] == "movie":
            kind = "🎬 电影"
        else:
            n = it["episode_count"]
            kind = f"📺 {n} 集" if n > 0 else "📺 剧集"
        meta_parts = []
        if it["year"]:
            meta_parts.append(f"📅 {it['year']}")
        meta_parts.append(kind)
        rating = _fmt_rating(it["rating"])
        if rating:
            meta_parts.append(f"⭐ {rating}")
        lines.append(f"<b>《{name}》</b>")
        lines.append(" · ".join(meta_parts))
        lines.append("")

    groups = summary["episode_groups"]
    if groups:
        shown = groups[:MAX_ITEMS]
        for g in shown:
            name = html.escape(g["name"])
            season_txt = f"第 {g['season']} 季" if g["season"] else ""
            lines.append(f"🎬 <b>《{name}》</b>{season_txt}新增 {g['count']} 集")
        if len(groups) > MAX_ITEMS:
            lines.append(f"<i>…等 {len(groups)} 部剧有更新</i>")

    total = summary["total"]
    if total > len(summary["items"]):
        lines.append("")
        lines.append(f"<i>…等共 {total} 部新片</i>")

    text = "\n".join(lines).rstrip()
    # 超长截断：优先保标题，逐条丢尾部
    while len(text) > MAX_TEXT_LEN and len(lines) > 3:
        lines.pop()
        text = "\n".join(lines).rstrip()
    return text


def _collect_photos(summary: dict) -> list[str]:
    """收集海报 URL（最多 10 张，去重）。"""
    photos: list[str] = []
    seen: set[str] = set()
    for it in summary["items"]:
        p = it.get("poster")
        if p and p not in seen:
            seen.add(p)
            photos.append(p)
    for g in summary["episode_groups"]:
        p = g.get("poster")
        if p and p not in seen:
            seen.add(p)
            photos.append(p)
    return photos[:10]


def _staff_ids(db: Session) -> list[int]:
    rows = (
        db.query(models.WebUser.id)
        .filter(
            models.WebUser.is_staff == True,  # noqa: E712
            models.WebUser.is_active == True,  # noqa: E712
        )
        .all()
    )
    return [r[0] for r in rows]


def _staff_chat_ids(db: Session, staff_ids: list[int]) -> dict[int, int]:
    """管理员 id → TG chat_id（r30 N1：旧 telegram_users 表已废弃，改查 WebUser.telegram_id）。"""
    rows = (
        db.query(models.WebUser.id, models.WebUser.telegram_id)
        .filter(models.WebUser.id.in_(staff_ids))
        .filter(models.WebUser.telegram_id.isnot(None))
        .all()
    )
    return {web_id: chat_id for web_id, chat_id in rows}


def _to_plain_text(html_text: str) -> str:
    """HTML 消息转纯文本（给邮件/站内信等非 TG 渠道）。"""
    import re

    text = re.sub(r"</?(b|i|u|code)[^>]*>", "", html_text)
    return html.unescape(text)


async def _send_to_staff(
    staff_ids: list[int],
    text: str,
    photos: list[str],
    channels: list[str],
) -> None:
    """给每位管理员发富媒体通知。单个失败只记日志，不影响其他人。

    telegram 渠道走 TelegramChannel.send_rich（sendPhoto/media group）；
    其他渠道沿用旧的文本投递。
    """
    from backend.notifications import get_notification_service

    service = get_notification_service()
    valid = [c for c in channels if c in service.channels]
    if not valid:
        logger.warning("新片入库通知：配置的渠道 %s 都不可用，跳过", channels)
        return

    tg_channel = service.channels.get("telegram") if "telegram" in valid else None
    other_channels = [c for c in valid if c != "telegram"]

    # TG 需要 chat_id：独立查一次（不碰扫描 Session）
    chat_map: dict[int, int] = {}
    if tg_channel is not None:
        db = SessionLocal()
        try:
            chat_map = _staff_chat_ids(db, staff_ids)
        finally:
            db.close()

    for staff_id in staff_ids:
        try:
            if tg_channel is not None:
                chat_id = chat_map.get(staff_id)
                if chat_id is None:
                    logger.warning(
                        "新片入库通知：管理员 id=%s 未绑定 TG，跳过", staff_id
                    )
                else:
                    ok, error = await tg_channel.send_rich(
                        chat_id, text=text, photos=photos,
                        title="新片入库通知",
                    )
                    if not ok:
                        logger.warning(
                            "新片入库通知 TG 投递失败（管理员 id=%s）：%s",
                            staff_id, error,
                        )
            for ch in other_channels:
                results = await service.send(
                    user_id=staff_id,
                    title="新片入库通知",
                    content=_to_plain_text(text),
                    channels=[ch],
                    message_type="system",
                )
                if not any(results.values()):
                    logger.warning(
                        "新片入库通知投递失败（管理员 id=%s，渠道 %s）：%s",
                        staff_id, ch, results,
                    )
        except Exception:  # noqa: BLE001 — 单个管理员失败不影响其他人
            logger.warning(
                "新片入库通知投递异常（管理员 id=%s）", staff_id, exc_info=True
            )
