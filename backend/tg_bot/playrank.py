"""每日播放排行榜：站主拿竞品机器人 21:30 的榜单截图亲点补做。
与「观影榜海报日推」（rankpush，用户时长榜海报＋发分）是两回事：
本模块不发分；播报默认图文合一（海报图＋文字榜 caption 一条发），
发出后置顶。

消息形态（照竞品「月饼 Emby」实际发布）：海报图一张（电影/剧集
各前 5，深色底红点缀，playrank_poster 渲染）＋文字榜全文作
caption；文字超 Telegram caption 上限（1024 字符）时退回图、文
两条（图无 caption、文字紧随）；海报关掉或生成/发送失败时降级
为纯文字一条。文字榜两行式，正文顺序电影 → 剧集 → 用户榜
（与海报上下呼应），哪榜有货出哪榜：

    📺 今日热播榜
    ──────────

    🎬 电影
    1. 生化危机：爆发夜
      播放 4 次 · 时长 2 小时 57 分钟

    📺 剧集
    1. 后会友期
      播放 29 次 · 时长 8 小时 46 分钟

    👑 观看时长榜
    1. gztony
      观看 8 小时 18 分钟

排序：三榜都按总观看时长降序，同长按播放次数降序（用户榜无次数
概念，同秒沿用 watch_duration_rank 的 user_id 升序）。片名用库内
显示名原样，不拼年份。

数据口径（不新造采集，与站内既有榜单同源同口径）：
- 表：emby_playback_sessions（播放会话行）＋ emby_items。
- 窗口：当日 Asia/Shanghai 00:00 至发送时刻；会话按 start_time
  （UTC 入库帧）落窗，换算走 portal.leaderboard_window，与时长榜
  同一口径——昨晚开播跨进今天的会话算昨天、不进今日榜。
- 时长：panel_achievements.playback_seconds（末次上报进度近似：
  min(position_ticks, 条目 duration_ticks)），与账号面板「已观影
  X 小时」、观影榜日推同源；暂停时 position 不前进、不计入。
- 播放次数：窗口内会话行数，与站内热播榜 hot_leaderboard_items
  同口径（会话按 PlaySessionId upsert、复播复活重置 start_time，
  一行＝一轮播放）。
- 条目聚合：集按 series_id 汇总到剧（同 hot_leaderboard_items 的
  聚合键）；被播条目已软删/已合并的不计；聚合实体若是折叠行，
  跟随 merged_into_id 到最终存活正主（与集跟随收敛后的库形态
  对齐，避免整剧因译名折叠行被吞）；总时长 <= 0 的条目不上榜
  （与用户榜「0 秒不上榜」一致）。
- 用户榜：复用 tg_bot.queries.watch_duration_rank（show_on_
  leaderboard 为 False 的用户不上榜，NULL 视为允许）。

开关（SystemConfig，管理后台「公益服经济」页可配）：
- bot_playrank_enabled 总开关，**默认关**；关了手动/定时都不发。
- bot_playrank_time 发送时间（上海时间 HH:MM），默认 21:30；
  非法值回落 21:30（原文存储、读侧校验，照 bot_rain_schedule 先例）。
- bot_playrank_top_n 每榜条数，默认 5（1~20）。
- bot_playrank_users_enabled / _series_enabled / _movies_enabled
  三榜各自开关，默认开。
- bot_playrank_poster_enabled 海报图开关，默认开；关了只发文字。
  海报固定取电影/剧集前 5，与 bot_playrank_top_n 无关。
调度标记 bot_playrank_last_run 存 SystemConfig，只在推送成功或
空榜时写入；发送失败/未配群不写，下个 tick 重试（与 rankpush
同纪律）。定时搭 tg-rankpush 调度线程的分钟巡检（与 gamerank
同路），不新起进程。手动触发：管理员私聊机器人发 /playrank。
"""
from __future__ import annotations

import logging
from datetime import datetime

from backend.emby_server import models as em
from backend.integrations import store
from backend.tg_bot import queries, rankpush, sender
from backend.tg_bot.layout import caption_fits

logger = logging.getLogger(__name__)

ENABLED_KEY = "bot_playrank_enabled"
TIME_KEY = "bot_playrank_time"
TOP_N_KEY = "bot_playrank_top_n"
USERS_KEY = "bot_playrank_users_enabled"
SERIES_KEY = "bot_playrank_series_enabled"
MOVIES_KEY = "bot_playrank_movies_enabled"
POSTER_KEY = "bot_playrank_poster_enabled"
LAST_RUN_KEY = "bot_playrank_last_run"

DEFAULT_TIME = (21, 30)
DEFAULT_TOP_N = 5
MAX_TOP_N = 20


def _cfg(db, key: str, default: str = "") -> str:
    try:
        return str(store.read_value(db, key, default) or "").strip()
    except Exception:
        logger.exception("playrank 配置读取失败 key=%s", key)
        return default


def _switch(db, key: str, default: bool) -> bool:
    raw = _cfg(db, key, "true" if default else "false").lower()
    if default:
        return raw not in ("0", "false", "off", "no")
    return raw in ("1", "true", "on", "yes")


def playrank_enabled(db) -> bool:
    return _switch(db, ENABLED_KEY, False)


def users_enabled(db) -> bool:
    return _switch(db, USERS_KEY, True)


def series_enabled(db) -> bool:
    return _switch(db, SERIES_KEY, True)


def movies_enabled(db) -> bool:
    return _switch(db, MOVIES_KEY, True)


def poster_enabled(db) -> bool:
    return _switch(db, POSTER_KEY, True)


def send_clock(db) -> tuple[int, int]:
    """发送钟点（上海时间）：bot_playrank_time 原文 HH:MM，非法回落 21:30。"""
    raw = _cfg(db, TIME_KEY, "21:30")
    try:
        hour_s, minute_s = raw.split(":", 1)
        hour, minute = int(hour_s), int(minute_s)
        if 0 <= hour <= 23 and 0 <= minute <= 59:
            return hour, minute
    except (ValueError, AttributeError):
        pass
    return DEFAULT_TIME


def top_n(db) -> int:
    try:
        value = int(_cfg(db, TOP_N_KEY, str(DEFAULT_TOP_N)))
    except ValueError:
        value = DEFAULT_TOP_N
    return max(1, min(MAX_TOP_N, value))


def fmt_duration(seconds) -> str:
    """时长展示：X 小时 Y 分钟；不足 1 小时只写 Y 分钟（全帖统一口径）。"""
    minutes = max(0, int(seconds or 0)) // 60
    hours, mins = divmod(minutes, 60)
    if hours:
        return f"{hours} 小时 {mins} 分钟"
    return f"{mins} 分钟"


# ---------------- 条目聚合 ----------------

def _resolve_entities(db, key_ids: set[int]) -> dict[int, em.MediaItem]:
    """聚合键 → 最终存活正主行。折叠行跟随 merged_into_id（带环保护），
    链断/成环/最终行已软删的键不返回（该键播放不进榜）。"""
    resolved: dict[int, em.MediaItem] = {}
    # 逐跳批量取行：每跳把当前待解析 id 换成其 merged_into_id，直到
    # 全部落到未合并行或无路可走。跳数上限与链长现实（<=2）留足余量。
    # current 只保留未解析的键（键 → 当前指向 id），解析完/作废即弹出。
    current: dict[int, int] = {int(k): int(k) for k in key_ids}
    for _hop in range(8):
        want = set(current.values())
        if not want:
            break
        rows = (
            db.query(em.MediaItem)
            .filter(em.MediaItem.id.in_(list(want)))
            .all()
        )
        by_id = {int(r.id): r for r in rows}
        progressed = False
        for key, cur in list(current.items()):
            if key in resolved:
                continue
            row = by_id.get(cur)
            if row is None or row.deleted_at is not None:
                current.pop(key, None)  # 链断/已下架：该键作废
                progressed = True
                continue
            if row.merged_into_id is None:
                resolved[key] = row
                current.pop(key, None)
                progressed = True
            else:
                nxt = int(row.merged_into_id)
                if nxt == cur:  # 自指环
                    current.pop(key, None)
                else:
                    current[key] = nxt
                progressed = True
        if not progressed:
            break
    return resolved


def item_play_rank(db, start_utc, limit: int) -> dict:
    """当日条目榜：{"series": [...], "movies": [...]}。

    每行 {item_id, name, plays, seconds}，按 seconds 降序、plays
    降序、item_id 升序稳定排序后截断到 limit。
    """
    from backend.panel_achievements import playback_seconds

    rows = (
        db.query(
            em.PlaybackSession.item_id,
            em.PlaybackSession.position_ticks,
            em.MediaItem.duration_ticks,
            em.MediaItem.series_id,
        )
        .join(em.MediaItem, em.MediaItem.id == em.PlaybackSession.item_id)
        .filter(
            em.PlaybackSession.start_time >= start_utc,
            em.PlaybackSession.item_id.isnot(None),
            # 联结查询软删钩子看不见，自己带条件（PA-037 同款）。
            em.MediaItem.deleted_at.is_(None),
            em.MediaItem.merged_into_id.is_(None),
        )
        .all()
    )
    totals: dict[int, list[int]] = {}  # 聚合键 → [plays, seconds]
    for row in rows:
        key = int(row.series_id) if row.series_id else int(row.item_id)
        bucket = totals.setdefault(key, [0, 0])
        bucket[0] += 1
        bucket[1] += int(playback_seconds(row))
    totals = {k: v for k, v in totals.items() if v[1] > 0}
    if not totals:
        return {"series": [], "movies": []}
    entities = _resolve_entities(db, set(totals))
    series: list[dict] = []
    movies: list[dict] = []
    for key, (plays, seconds) in totals.items():
        entity = entities.get(key)
        if entity is None:
            continue
        kind = str(entity.item_type or "").lower()
        entry = {
            "item_id": int(entity.id),
            "name": entity.name or "",
            "plays": int(plays),
            "seconds": int(seconds),
        }
        if kind == "series":
            series.append(entry)
        elif kind == "movie":
            movies.append(entry)
    order = lambda r: (-r["seconds"], -r["plays"], r["item_id"])  # noqa: E731
    limit = max(1, int(limit or DEFAULT_TOP_N))
    return {
        "series": sorted(series, key=order)[:limit],
        "movies": sorted(movies, key=order)[:limit],
    }


# ---------------- 组榜与渲染 ----------------

def collect_boards(db) -> dict:
    """按分榜开关组齐三榜。返回 {date_sh, users, series, movies}。"""
    from backend.emby_server.portal import leaderboard_window

    start_sh, start_utc = leaderboard_window("day")
    n = top_n(db)
    users = queries.watch_duration_rank(db, "day", n) if users_enabled(db) else []
    items = item_play_rank(db, start_utc, n)
    return {
        "date_sh": start_sh,
        "users": users,
        "series": items["series"] if series_enabled(db) else [],
        "movies": items["movies"] if movies_enabled(db) else [],
    }


def render_message(date_sh, users, series, movies) -> str:
    """渲染文字榜（竞品两行式）。date_sh 为上海自然日 datetime
    （日期只上海报图，文字标题行不带日期）。正文顺序电影 → 剧集
    → 用户榜，与海报上下呼应；每条两行，第二行缩进写数据。"""
    lines = ["📺 今日热播榜", "──────────"]
    for title, board in (("🎬 电影", movies), ("📺 剧集", series)):
        if not board:
            continue
        lines += ["", title]
        for idx, row in enumerate(board, 1):
            lines.append(f"{idx}. {row.get('name') or ''}")
            lines.append(
                f"  播放 {int(row.get('plays') or 0)} 次 "
                f"· 时长 {fmt_duration(row.get('seconds'))}"
            )
    if users:
        lines += ["", "👑 观看时长榜"]
        for idx, row in enumerate(users, 1):
            lines.append(f"{idx}. {row.get('username') or ''}")
            lines.append(f"  观看 {fmt_duration(row.get('seconds'))}")
    return "\n".join(lines)


# ---------------- 推送与调度 ----------------

def push_daily_rank(db) -> dict:
    """生成并推送今日播放排行榜。返回 {pushed, reason, users, series, movies}。

    reason: ok / disabled / empty / no_group / send_failed。
    空榜（三榜皆空，含三榜全关）不发；发出后置顶，不发分。
    """
    empty = {"users": [], "series": [], "movies": []}
    if not playrank_enabled(db):
        return {"pushed": False, "reason": "disabled", **empty}
    boards = collect_boards(db)
    payload = {k: boards[k] for k in ("users", "series", "movies")}
    if not any(payload.values()):
        logger.info("playrank 今日榜为空，跳过推送")
        return {"pushed": False, "reason": "empty", **payload}
    group_id = rankpush.target_group_id(db)
    if group_id is None:
        logger.warning("playrank 未配置交流群（bot_group_ids），跳过推送")
        return {"pushed": False, "reason": "no_group", **payload}
    text = render_message(boards["date_sh"], **payload)
    sent = False
    message_id = None
    if poster_enabled(db) and (payload["movies"] or payload["series"]):
        sent, message_id = _try_send_with_poster(
            db, group_id, boards["date_sh"], payload, text
        )
    if not sent:
        try:
            ok, err, message_id = sender.send_message_ex(
                db, group_id, text, parse_mode=None
            )
        except Exception:
            logger.exception("playrank 发送异常")
            ok, err, message_id = False, "exception", None
        if not ok:
            logger.warning("playrank 推送失败 err=%s", err)
            return {"pushed": False, "reason": "send_failed", **payload}
    _try_pin(db, group_id, message_id)
    logger.info(
        "playrank 已推送 group=%s users=%s series=%s movies=%s",
        group_id, len(payload["users"]), len(payload["series"]), len(payload["movies"]),
    )
    return {"pushed": True, "reason": "ok", **payload}


def _try_send_with_poster(db, group_id, date_sh, payload, text):
    """图文播报：海报先行，文字榜全文塞得进 caption 就合一一条；
    超限退回图（无 caption）＋文字两条。返回 (是否已发出, 承载
    文字那条消息的 id)。海报任何失败回 (False, None)，调用方
    降级纯文字——海报问题绝不堵死播报。"""
    try:
        from backend.tg_bot import playrank_poster

        movies = list(payload["movies"])
        series = list(payload["series"])
        limit = playrank_poster.POSTER_LIMIT
        if (movies and len(movies) < limit) or (
            series and len(series) < limit
        ):
            # 海报固定前 5：文字榜 top_n 不足 5 时用同一聚合补齐。
            from backend.emby_server.portal import leaderboard_window

            full = item_play_rank(db, leaderboard_window("day")[1], limit)
            if movies:
                movies = full["movies"]
            if series:
                series = full["series"]
        if caption_fits(text):
            ok, mid = playrank_poster.send_rank_photo(
                db, group_id, date_sh, movies, series, caption=text
            )
            if ok:
                return True, mid
            logger.warning("playrank 图文合一发送失败，降级纯文字")
            return False, None
        ok, _photo_mid = playrank_poster.send_rank_photo(
            db, group_id, date_sh, movies, series, caption=None
        )
        if not ok:
            return False, None
        ok, err, mid = sender.send_message_ex(db, group_id, text, parse_mode=None)
        if not ok:
            logger.warning(
                "playrank 海报已发但文字发送失败 err=%s，走纯文字兜底", err
            )
            return False, None
        return True, mid
    except Exception:
        logger.exception("playrank 海报推送异常（文字榜照常发送）")
        return False, None


def _try_pin(db, group_id, message_id) -> None:
    """播报置顶（与竞品一致）：失败只记日志，不影响播报本身。"""
    if not message_id:
        return
    try:
        ok, err = sender.pin_message(db, group_id, int(message_id))
        if not ok:
            logger.warning("playrank 置顶失败 err=%s", err)
    except Exception:
        logger.exception("playrank 置顶异常")


def run_scheduled_tick(db, now_sh: datetime | None = None) -> list[str]:
    """调度一拍：到配置钟点且今日未推过才推。返回动作列表（如 ["day:pushed"]）。

    标记只在推送成功或空榜时写入；发送失败/未配群不写，下个 tick
    重试；总开关关时直接返回、不写标记（重开后当晚仍可补推）。
    """
    now = now_sh or rankpush._now_shanghai()
    if not playrank_enabled(db):
        return []
    hour, minute = send_clock(db)
    if (now.hour, now.minute) < (hour, minute):
        return []
    today_key = now.date().isoformat()
    if _cfg(db, LAST_RUN_KEY, "") == today_key:
        return []
    res = push_daily_rank(db)
    if res["reason"] in ("ok", "empty"):
        store.write_values(db, {LAST_RUN_KEY: today_key})
        db.commit()
        return ["day:pushed" if res["pushed"] else "day:empty"]
    return [f"day:{res['reason']}"]
