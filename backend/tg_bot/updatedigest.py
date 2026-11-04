"""TG「更新入库」播报（2026-11-03 站主按竞品「云海 emby 资源更新 bot」
截图格式亲定）：新入库的剧集/电影攒批播报到机器人主群。

消息形态（照截图）：照片＋图注（无可用横幅图时纯文字），一条消息内
先电视剧栏后电影栏，哪栏有货出哪栏：

    电视剧更新
    📺绝世战魂 (2024) S02E38
    📺未解之谜：特别呈现 (2023) S02E15-E18

    电影更新
    🎬梦寻苏毗女儿国 (2026)

播报口径（站主/parent 定版）：
- 目标群：bot_group_ids 第一个群（与观影榜日推 rankpush 同一份群配置、
  同一解析口），不新造配置；发送走 tg_bot.sender 既有通道。
- 攒批：每 bot_update_digest_interval_minutes 分钟一批（默认 30），
  无更新不发；不逐条刷屏。
- 只报「用户真能看的」：顶层行已过身份货架判据（identity_gate
  identity_clause 同源口径）才进播报；新集按所属剧的身份判据（clause
  的子行分支本身就是跟随 series）。未认出身份的不报，等认出后再报。
- 防重复：tg_update_digest_sent 小表记已播行 id（电影=顶层行、
  新集=集行），同一条不播两次；发送成功才记，失败下批重试。
- 每栏最多 10 行，超出在栏尾写「……本批共 N 部/剧」，未播的留在
  表外顺延下一批，不丢。
- 存量窗口：只报最近 14 天内入库（date_added）的行。
- 上线基线（首跑种子）：功能上线时已在货架上（已绑定且 14 天内
  入库）的条目先整批记为 baseline 不播报——播报只播上线后新到的，
  不把存量当新闻翻出来刷屏；上线时未绑定的不在种子内，日后认出
  身份仍按上一条口径正常播报。
- 与「管理员新片通知」（notifications 渠道私发管理员）是两回事，
  本模块不碰它、也不由入库事件触发，只按调度攒批。

开关（SystemConfig，管理后台「公益服经济」页可配）：
- bot_update_digest_enabled 总开关，默认开。
- bot_update_digest_interval_minutes 攒批间隔分钟，默认 30（5~1440）。
调度标记 bot_update_digest_last_run / _seeded 存 SystemConfig，
只在推送成功/空批/种子完成时推进 last_run；失败下个 tick 重试
（与 rankpush 同纪律）。
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta

from sqlalchemy import func, insert, literal, select

from backend import models
from backend.emby_server import identity_gate, image_store
from backend.emby_server import models as em
from backend.integrations import store
from backend.tg_bot import layout, rankpush, sender

logger = logging.getLogger(__name__)

ENABLED_KEY = "bot_update_digest_enabled"
INTERVAL_KEY = "bot_update_digest_interval_minutes"
LAST_RUN_KEY = "bot_update_digest_last_run"
SEEDED_KEY = "bot_update_digest_seeded"

DEFAULT_INTERVAL_MINUTES = 30
WINDOW_DAYS = 14
MAX_LINES_PER_COLUMN = 10
_EPISODE_FETCH_CAP = 20000


def _cfg(db, key: str, default: str = "") -> str:
    try:
        return str(store.read_value(db, key, default) or "").strip()
    except Exception:
        logger.exception("updatedigest 配置读取失败 key=%s", key)
        return default


def digest_enabled(db) -> bool:
    return _cfg(db, ENABLED_KEY, "true").lower() not in ("0", "false", "off", "no")


def interval_minutes(db) -> int:
    try:
        value = int(_cfg(db, INTERVAL_KEY, str(DEFAULT_INTERVAL_MINUTES)))
    except ValueError:
        value = DEFAULT_INTERVAL_MINUTES
    return max(5, min(1440, value))


def _base_conditions(window_start: datetime) -> list:
    """候选行公共条件：近窗口内入库、未软删、未隐藏、非合并副本、未播报过。"""
    return [
        em.MediaItem.deleted_at.is_(None),
        em.MediaItem.is_hidden == False,  # noqa: E712  # PA-011
        em.MediaItem.merged_into_id.is_(None),
        em.MediaItem.date_added.isnot(None),
        em.MediaItem.date_added >= window_start,
        ~em.MediaItem.id.in_(select(models.TgUpdateDigestSent.item_id)),
        identity_gate.identity_clause(),
    ]


def _seed_baseline(db, now: datetime) -> int:
    """首跑基线：把当前已达标（已绑定＋窗口内）的电影/集行整批记为
    baseline 不播报。INSERT..SELECT 在库内完成，不逐行过 Python；
    未绑定的行不在种子内，日后绑定仍会被正常选中播报。"""
    window_start = now - timedelta(days=WINDOW_DAYS)
    total = 0
    for item_type in ("movie", "episode"):
        stmt = insert(models.TgUpdateDigestSent).from_select(
            ["item_id", "kind", "sent_at"],
            select(
                em.MediaItem.id, literal("baseline"), literal(now)
            ).where(
                em.MediaItem.item_type == item_type,
                *_base_conditions(window_start),
            ),
        )
        result = db.execute(stmt)
        total += int(result.rowcount or 0)
    store.write_values(db, {SEEDED_KEY: "1"})
    db.commit()
    logger.info("updatedigest 基线种子完成：%s 行", total)
    return total


def _collect_movies(db, window_start: datetime):
    """本批电影候选：返回 (总数, 前 MAX 行)。按入库先后 FIFO，
    超额的留下批（未记已播，自然顺延）。"""
    conds = [em.MediaItem.item_type == "movie", *_base_conditions(window_start)]
    total = int(db.query(func.count(em.MediaItem.id)).filter(*conds).scalar() or 0)
    rows = (
        db.query(em.MediaItem)
        .filter(*conds)
        .order_by(em.MediaItem.date_added.asc(), em.MediaItem.id.asc())
        .limit(MAX_LINES_PER_COLUMN)
        .all()
    )
    return total, rows


def _resolve_series_map(db, episodes) -> dict[int, object]:
    """批量解析集行所属 series 行（series_id 直指，或经季行 parent 转指）。"""
    wanted: set[int] = set()
    parent_ids: set[int] = set()
    for ep in episodes:
        if ep.series_id:
            wanted.add(int(ep.series_id))
        elif ep.parent_id:
            parent_ids.add(int(ep.parent_id))
    parents: dict[int, object] = {}
    if parent_ids:
        parents = {
            int(p.id): p
            for p in db.query(em.MediaItem).filter(em.MediaItem.id.in_(parent_ids)).all()
        }
    for ep in episodes:
        if not ep.series_id and ep.parent_id:
            parent = parents.get(int(ep.parent_id))
            if parent is not None and parent.series_id:
                wanted.add(int(parent.series_id))
    if not wanted:
        return {}
    return {
        int(s.id): s
        for s in db.query(em.MediaItem).filter(em.MediaItem.id.in_(wanted)).all()
    }


def _episode_season(db, ep, parents_cache: dict) -> int | None:
    """集行季号：自身列优先，缺失时取父季行的季号。"""
    if ep.season_number is not None:
        return int(ep.season_number)
    if ep.parent_id:
        parent = parents_cache.get(int(ep.parent_id))
        if parent is None:
            parent = (
                db.query(em.MediaItem).filter(em.MediaItem.id == int(ep.parent_id)).first()
            )
            if parent is not None:
                parents_cache[int(ep.parent_id)] = parent
        if parent is not None and parent.season_number is not None:
            return int(parent.season_number)
    return None


def _collect_episode_groups(db, window_start: datetime):
    """本批新集候选，按（剧，季）分组。返回 (总组数, 前 MAX 组)。

    每组：{"series": series 行, "season": int, "episodes": [集行...],
    "first_added": 组内最早入库时间}。集号缺失/剧或季解析不出的集行
    本批跳过（不记已播，补齐后仍可入选）。identity_clause 的子行分支
    已保证「集跟随所属剧的身份判据」。
    """
    conds = [em.MediaItem.item_type == "episode", *_base_conditions(window_start)]
    rows = (
        db.query(em.MediaItem)
        .filter(*conds)
        .order_by(em.MediaItem.date_added.asc(), em.MediaItem.id.asc())
        .limit(_EPISODE_FETCH_CAP)
        .all()
    )
    if not rows:
        return 0, []
    series_map = _resolve_series_map(db, rows)
    parents_cache: dict[int, object] = {}
    groups: dict[tuple[int, int], dict] = {}
    for ep in rows:
        if ep.episode_number is None:
            continue
        series_id = ep.series_id
        if not series_id and ep.parent_id:
            parent = parents_cache.get(int(ep.parent_id)) or (
                db.query(em.MediaItem).filter(em.MediaItem.id == int(ep.parent_id)).first()
            )
            if parent is not None:
                parents_cache[int(ep.parent_id)] = parent
                series_id = parent.series_id
        series = series_map.get(int(series_id)) if series_id else None
        if series is None:
            continue
        season = _episode_season(db, ep, parents_cache)
        if season is None:
            continue
        key = (int(series.id), season)
        group = groups.get(key)
        if group is None:
            group = groups[key] = {
                "series": series,
                "season": season,
                "episodes": [],
                "first_added": ep.date_added,
            }
        group["episodes"].append(ep)
        if ep.date_added and (group["first_added"] is None or ep.date_added < group["first_added"]):
            group["first_added"] = ep.date_added
    ordered = sorted(
        groups.values(),
        key=lambda g: (g["first_added"] or datetime.min, int(g["series"].id)),
    )
    return len(ordered), ordered[:MAX_LINES_PER_COLUMN]


def _title_with_year(item) -> str:
    name = (getattr(item, "name", None) or "").strip()
    year = getattr(item, "production_year", None)
    return f"{name} ({year})" if year else name


def format_movie_line(item) -> str:
    return f"🎬{_title_with_year(item)}"


def _episode_segments(numbers: list[int]) -> str:
    """集号列表 → 连续段拼写：[15,16,17,18] → E15-E18；[7] → E07；
    [15,16,18] → E15-E16、E18（首段后以「、」连接）。"""
    nums = sorted(set(int(n) for n in numbers))
    segments: list[str] = []
    start = prev = nums[0]
    for n in nums[1:]:
        if n == prev + 1:
            prev = n
            continue
        segments.append((start, prev))
        start = prev = n
    segments.append((start, prev))
    parts = [
        f"E{a:02d}" if a == b else f"E{a:02d}-E{b:02d}" for a, b in segments
    ]
    return "、".join(parts)


def format_episode_line(group) -> str:
    series = group["series"]
    numbers = [int(ep.episode_number) for ep in group["episodes"]]
    return (
        f"📺{_title_with_year(series)} "
        f"S{int(group['season']):02d}{_episode_segments(numbers)}"
    )


def build_message(tv_groups, movie_rows, tv_total: int, movie_total: int) -> str:
    """组装播报正文：先电视剧栏、后电影栏，空栏不出。"""
    sections: list[str] = []
    if tv_groups:
        lines = ["电视剧更新"]
        lines.extend(format_episode_line(g) for g in tv_groups)
        if tv_total > len(tv_groups):
            lines.append(f"……本批共 {tv_total} 剧")
        sections.append("\n".join(lines))
    if movie_rows:
        lines = ["电影更新"]
        lines.extend(format_movie_line(m) for m in movie_rows)
        if movie_total > len(movie_rows):
            lines.append(f"……本批共 {movie_total} 部")
        sections.append("\n".join(lines))
    return "\n\n".join(sections)


def _backdrop_file(item) -> str | None:
    """条目横幅图 → 本地文件（站内图片口径）：
    backdrop_path 本地文件优先；只有远程 URL 时走 image_store.localize
    落站内内容寻址缓存后再用，绝不把外链直接丢给 Telegram 拉取。"""
    local = getattr(item, "backdrop_path", None)
    if local and not str(local).startswith(("http://", "https://")):
        if os.path.isfile(str(local)):
            return str(local)
    url = getattr(item, "backdrop_image_url", None) or (
        local if local and str(local).startswith(("http://", "https://")) else None
    )
    if url:
        try:
            path = image_store.localize(str(url))
        except Exception:
            logger.exception("updatedigest 横幅图本地化失败 item=%s", getattr(item, "id", "?"))
            path = ""
        if path and os.path.isfile(path):
            return path
    return None


def pick_photo_path(movie_rows, tv_groups) -> str | None:
    """配图：本批一部作品的横幅图，优先电影、其次剧集（取所属剧行）。"""
    for item in list(movie_rows) + [g["series"] for g in tv_groups]:
        path = _backdrop_file(item)
        if path:
            return path
    return None


def _mark_sent(db, movie_rows, tv_groups) -> None:
    for item in movie_rows:
        db.add(models.TgUpdateDigestSent(item_id=int(item.id), kind="movie"))
    for group in tv_groups:
        for ep in group["episodes"]:
            db.add(models.TgUpdateDigestSent(item_id=int(ep.id), kind="episode"))
    db.commit()


def push_digest(db, now: datetime | None = None) -> dict:
    """攒一批并播报。返回 {pushed, reason, movies, tv_groups, text}。

    reason: ok / disabled / no_group / empty / seeded / send_failed。
    发送成功才记已播；失败不记，下批原样重试。
    """
    now = now or datetime.now()
    empty = {"pushed": False, "movies": 0, "tv_groups": 0, "text": ""}
    if not digest_enabled(db):
        return {**empty, "reason": "disabled"}
    if _cfg(db, SEEDED_KEY, "") != "1":
        seeded = _seed_baseline(db, now)
        return {**empty, "reason": "seeded", "seeded": seeded}
    group_id = rankpush.target_group_id(db)
    if group_id is None:
        logger.warning("updatedigest 未配置交流群（bot_group_ids），跳过播报")
        return {**empty, "reason": "no_group"}
    window_start = now - timedelta(days=WINDOW_DAYS)
    movie_total, movie_rows = _collect_movies(db, window_start)
    tv_total, tv_groups = _collect_episode_groups(db, window_start)
    if not movie_rows and not tv_groups:
        return {**empty, "reason": "empty"}
    text = build_message(tv_groups, movie_rows, tv_total, movie_total)
    photo_path = pick_photo_path(movie_rows, tv_groups)
    ok, err = False, None
    if photo_path and layout.caption_fits(text):
        ok, err = sender.send_photo(
            db, group_id, text, photo_path=photo_path, parse_mode=None
        )
        if not ok:
            logger.warning("updatedigest 发图失败，降级纯文本：%s", err)
    if not ok:
        ok, err = sender.send_message(db, group_id, text, parse_mode=None)
    if not ok:
        logger.warning("updatedigest 播报发送失败：%s", err)
        return {
            **empty, "reason": "send_failed",
            "movies": len(movie_rows), "tv_groups": len(tv_groups), "text": text,
        }
    _mark_sent(db, movie_rows, tv_groups)
    logger.info(
        "updatedigest 已播报 group=%s movies=%s tv_groups=%s photo=%s",
        group_id, len(movie_rows), len(tv_groups), bool(photo_path),
    )
    return {
        "pushed": True, "reason": "ok",
        "movies": len(movie_rows), "tv_groups": len(tv_groups), "text": text,
    }


def run_scheduled_tick(db, now: datetime | None = None) -> list[str]:
    """调度一拍：距上批 ≥ 间隔才攒批。last_run 只在推送成功/空批/
    种子完成时推进；失败下个 tick 重试（与 rankpush 同纪律）。"""
    now = now or datetime.now()
    if not digest_enabled(db):
        return []
    last_raw = _cfg(db, LAST_RUN_KEY, "")
    if last_raw:
        try:
            last = datetime.fromisoformat(last_raw)
            if (now - last) < timedelta(minutes=interval_minutes(db)):
                return []
        except ValueError:
            pass
    res = push_digest(db, now)
    if res["reason"] in ("ok", "empty", "seeded"):
        store.write_values(db, {LAST_RUN_KEY: now.isoformat()})
        db.commit()
        return [f"digest:{res['reason']}"]
    return []
