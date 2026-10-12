"""v4 探险家日志面板 · 成就引擎

纯函数评估器：evaluate(user_id, db) -> list[新解锁的 ach_key]
幂等：已解锁的不重复写。

12 条规则 v1（全部可配置开关，走 panel_achievement_defs.enabled）：
  first_night   初夜：首次深夜观影（23:00–05:00 开始播放）
  streak7       连击：连签 7 天
  night_owl     熬夜：单日观影超 6 小时
  archaeology   考古：看 10 年前（含）的老片
  binge         追完：一口气看完一季（同一 series_id 单日 >= 6 集）
  eclectic      杂食：集满 5 种口味章
  deep_sea      深海：看完一部纪录片
  mind_bender   烧脑：看完一部悬疑/科幻/惊悚片
  sharer        分享：成功邀请 1 位好友注册
  revisit       回头：重温（同一条目 30 天后再次播放）
  marathon      马拉松：周末单日观影超 10 小时
  early_bird    早鸟：首次晨间观影（05:00–08:00 开始播放）
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta

from sqlalchemy import func
from sqlalchemy.orm import Session

from backend import models

logger = logging.getLogger("aetrix.panel.achievements")

# 口味映射：genre 关键字 -> 口味 key（12 口味，与前端护照一致）
TASTE_MAP = [
    ("taste_anime", "国漫", ["动画", "动漫", "anime", "animation"]),
    ("taste_drama_cn", "国产剧", ["国产", "华语剧"]),
    ("taste_drama_us", "欧美剧", ["欧美", "美剧", "英剧"]),
    ("taste_drama_jpkr", "日韩剧", ["日剧", "韩剧", "日本", "韩国"]),
    ("taste_movie_cn", "华语电影", ["华语电影"]),
    ("taste_movie_foreign", "外语电影", ["外语电影"]),
    ("taste_movie_anime", "动画电影", ["动画电影"]),
    ("taste_doc", "纪录片", ["纪录片", "documentary"]),
    ("taste_variety", "综艺", ["综艺", "真人秀"]),
    ("taste_kids", "儿童", ["儿童", "少儿"]),
    ("taste_suspense", "悬疑", ["悬疑", "惊悚", "犯罪", "mystery", "thriller", "crime"]),
    ("taste_scifi", "科幻", ["科幻", "奇幻", "sci-fi", "fantasy"]),
]

# 成就定义 seed（key, name, desc, icon, sort）
ACHIEVEMENT_DEFS = [
    ("first_night", "初夜", "第一次在深夜看片（23:00–05:00）", "🌙", 10),
    ("streak7", "连击", "连续签到 7 天", "🔥", 20),
    ("night_owl", "熬夜", "单日观影超过 6 小时", "🦉", 30),
    ("archaeology", "考古", "看了一部 10 年前的老片", "🏺", 40),
    ("binge", "追完", "一口气刷完一季（单日 6 集+）", "📺", 50),
    ("eclectic", "杂食", "集满 5 种口味印章", "🍱", 60),
    ("deep_sea", "深海", "看完一部纪录片", "🌊", 70),
    ("mind_bender", "烧脑", "看完一部悬疑/科幻/惊悚片", "🧠", 80),
    ("sharer", "分享", "成功邀请 1 位好友", "💌", 90),
    ("revisit", "回头", "30 天后重温同一部片", "🔄", 100),
    ("marathon", "马拉松", "周末单日观影超 10 小时", "🏃", 110),
    ("early_bird", "早鸟", "第一次在清晨看片（05:00–08:00）", "🐦", 120),
]


def seed_achievement_defs(db: Session) -> int:
    """启动时 seed 成就定义（已存在的跳过，不覆盖用户改过的开关）"""
    added = 0
    for key, name, desc, icon, sort in ACHIEVEMENT_DEFS:
        exists = db.query(models.PanelAchievementDef).filter_by(key=key).first()
        if not exists:
            db.add(models.PanelAchievementDef(
                key=key, name=name, desc=desc, icon=icon, sort=sort, enabled=True,
            ))
            added += 1
    if added:
        db.commit()
    return added


def _enabled_keys(db: Session) -> set:
    rows = db.query(models.PanelAchievementDef.key).filter_by(enabled=True).all()
    return {r[0] for r in rows}


def _unlocked_keys(db: Session, user_id: int) -> set:
    rows = db.query(models.PanelAchievementUnlock.ach_key).filter_by(user_id=user_id).all()
    return {r[0] for r in rows}


def _parse_genres(genres_raw) -> list:
    if not genres_raw:
        return []
    if isinstance(genres_raw, list):
        return [str(g) for g in genres_raw]
    try:
        parsed = json.loads(genres_raw)
        if isinstance(parsed, list):
            return [str(g) for g in parsed]
    except Exception:
        pass
    return [str(genres_raw)]


def user_tastes(db: Session, user_id: int) -> dict:
    """12 口味印章状态：{taste_key: {'name':, 'stamped': bool, 'count': int}}

    阈值：某口味看满 5 部（去重条目）盖章。genre 缺失的条目不计入任何口味。
    """
    from backend.emby_server import models as em  # noqa: F401  (确保表已注册)
    # 用 emby_sessions 的 item_name 去重近似（session 不存 item_id）
    sessions = db.query(models.EmbySession).filter(
        models.EmbySession.user_id == user_id,
        models.EmbySession.play_duration > 60,  # 至少看 1 分钟才算
    ).order_by(models.EmbySession.start_time.desc()).all()
    # r20 确定性截断：按最近观看倒序、去重保序取前 500（原 list(set)[:500] 顺序不定，
    # 重度用户每次请求截到不同子集，口味/成就判定抖动）
    seen_names = list(dict.fromkeys(s.item_name for s in sessions if s.item_name))
    # 批量查 genre
    items = {}
    if seen_names:
        for row in db.query(em.MediaItem.name, em.MediaItem.genres).filter(
            em.MediaItem.name.in_(seen_names[:500])
        ).all():
            items[row[0]] = _parse_genres(row[1])

    taste_counts = {t[0]: 0 for t in TASTE_MAP}
    for name in seen_names:
        genres = items.get(name, [])
        genre_text = " ".join(genres).lower()
        for tkey, tname, keywords in TASTE_MAP:
            if any(kw.lower() in genre_text for kw in keywords):
                taste_counts[tkey] += 1

    result = {}
    for tkey, tname, _ in TASTE_MAP:
        cnt = taste_counts[tkey]
        result[tkey] = {"name": tname, "stamped": cnt >= 5, "count": cnt}
    return result


def evaluate(user_id: int, db: Session) -> list:
    """评估用户成就，返回新解锁的 key 列表（已写库，幂等）"""
    enabled = _enabled_keys(db)
    if not enabled:
        return []
    unlocked = _unlocked_keys(db, user_id)
    now = datetime.now()
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)

    new_unlocks = []

    def _grant(key: str):
        if key in enabled and key not in unlocked and key not in new_unlocks:
            new_unlocks.append(key)

    # 基础数据：近 90 天有效 session
    cutoff = now - timedelta(days=90)
    sessions = db.query(models.EmbySession).filter(
        models.EmbySession.user_id == user_id,
        models.EmbySession.start_time >= cutoff,
    ).all()
    valid = [s for s in sessions if (s.play_duration or 0) > 60]

    # --- 时间类规则 ---
    for s in valid:
        if not s.start_time:
            continue
        h = s.start_time.hour
        if h >= 23 or h < 5:
            _grant("first_night")
        if 5 <= h < 8:
            _grant("early_bird")

    # --- 单日时长类 ---
    day_seconds: dict = {}
    for s in valid:
        if not s.start_time:
            continue
        d = s.start_time.date().isoformat()
        day_seconds[d] = day_seconds.get(d, 0) + (s.play_duration or 0)
    for d, secs in day_seconds.items():
        if secs >= 6 * 3600:
            _grant("night_owl")
        if secs >= 10 * 3600:
            try:
                wd = datetime.fromisoformat(d).weekday()
                if wd >= 5:  # 周六/日
                    _grant("marathon")
            except Exception:
                pass

    # --- 连签 ---
    latest = db.query(models.CheckinRecord).filter_by(user_id=user_id).order_by(
        models.CheckinRecord.checkin_date.desc()).first()
    if latest and (latest.streak or 0) >= 7:
        _grant("streak7")

    # --- 内容类规则（需要 item genre/年份） ---
    from backend.emby_server import models as em
    # r20 确定性截断：valid 按 start_time 倒序、去重保序取前 500（同上）
    _ordered = sorted((s for s in valid if s.item_name),
                      key=lambda s: s.start_time or datetime.min, reverse=True)
    names = list(dict.fromkeys(s.item_name for s in _ordered))
    item_map = {}
    if names:
        for row in db.query(
            em.MediaItem.name, em.MediaItem.genres, em.MediaItem.production_year,
            em.MediaItem.item_type, em.MediaItem.series_id,
        ).filter(em.MediaItem.name.in_(names[:500])).all():
            item_map[row[0]] = row

    for s in valid:
        row = item_map.get(s.item_name)
        if not row:
            continue
        _, genres_raw, prod_year, item_type, series_id = row
        genres = _parse_genres(genres_raw)
        gtext = " ".join(genres).lower()
        # 考古：10 年前老片
        if prod_year and prod_year <= now.year - 10:
            _grant("archaeology")
        # 深海：纪录片
        if any(k in gtext for k in ["纪录片", "documentary"]):
            _grant("deep_sea")
        # 烧脑：悬疑/科幻/惊悚
        if any(k in gtext for k in ["悬疑", "科幻", "惊悚", "mystery", "thriller", "sci-fi"]):
            _grant("mind_bender")

    # 追完：同一 series_id 单日 >= 6 集（episode 类型）
    series_day: dict = {}
    for s in valid:
        row = item_map.get(s.item_name)
        if not row or not s.start_time:
            continue
        _, _, _, item_type, series_id = row
        if item_type == "episode" and series_id:
            k = (series_id, s.start_time.date().isoformat())
            series_day[k] = series_day.get(k, 0) + 1
    if any(c >= 6 for c in series_day.values()):
        _grant("binge")

    # 杂食：5 种口味章
    tastes = user_tastes(db, user_id)
    if sum(1 for t in tastes.values() if t["stamped"]) >= 5:
        _grant("eclectic")

    # 分享：邀请记录 >= 1
    inv = db.query(models.InvitationRecord).filter_by(inviter_id=user_id).count()
    if inv >= 1:
        _grant("sharer")

    # 回头：同一条目 30 天后再次播放
    name_times: dict = {}
    for s in valid:
        if s.item_name and s.start_time:
            name_times.setdefault(s.item_name, []).append(s.start_time)
    for times in name_times.values():
        times.sort()
        for i in range(1, len(times)):
            if (times[i] - times[i - 1]).days >= 30:
                _grant("revisit")
                break

    # 写库（幂等：UNIQUE 约束兜底）
    for key in new_unlocks:
        try:
            db.add(models.PanelAchievementUnlock(user_id=user_id, ach_key=key))
            db.commit()
        except Exception:
            db.rollback()  # 并发重复解锁，忽略
    return new_unlocks


__all__ = [
    "ACHIEVEMENT_DEFS", "TASTE_MAP",
    "seed_achievement_defs", "evaluate", "user_tastes",
]
