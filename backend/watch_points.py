"""观影能量（货币再设计第一期，2026-11-01）：按当日累计观影分钟发能量。

与活力观影奖（backend/vitality.py::award_watch_minutes_reward）同结算点
（compat_routes._record_stopped）、同输入（单条目本轮最远点分钟数），但
两套口径不同、并行发放：
- 活力奖按「单次停播」的分钟数取档，日上限 2 兜底；
- 观影能量按「当日累计」分钟数分档：累计满首看分钟数 +首看奖、满一档
  +一档奖、满二档再 +二档奖，三档合计受每日封顶钳制。

累计口径（设计稿第九节未知项的落地结论）：UserMediaData.max_position
是单条目本轮最远点（看完清零），推不出当日累计，故累计量落库在
watch_points_daily：每人每天一行，item_baselines 记每个条目当日已
计入的基线分钟——同条目重复停播只计超出基线的增量（幂等），看完
（finished）后该条目基线清零、重看分钟重新计入。快进虚增、回拖不减
与活力奖同款已知偏差，靠档位阈值与日封顶兜底。

幂等：发了哪几档以 points_logs 为唯一凭证（type='watch_points'、
ref_id='watch_points:<上海日期>:<档位>'），重复结算/补结算先查流水
再发，不双发。日界按 Asia/Shanghai 自然日（与成就口径一致；流水
时间窗用 economy._today_start_utc_naive 的 UTC naive 帧）。

身份：仅公益用户（is_welfare，与活力奖同一口径）。本函数自锁用户行
＋自提交（供 _record_stopped 尽力而为调用），失败回滚返回 0，绝不
影响停播主流程。
"""
from __future__ import annotations

import json
import logging
from datetime import date, datetime

from sqlalchemy import func
from sqlalchemy.orm import Session

from backend import models
from backend.integrations import store

logger = logging.getLogger(__name__)

CONFIG_FIRST = "watch_points_first"
CONFIG_FIRST_MINUTES = "watch_points_first_minutes"
CONFIG_TIER1 = "watch_points_tier1"
CONFIG_TIER1_MINUTES = "watch_points_tier1_minutes"
CONFIG_TIER2 = "watch_points_tier2"
CONFIG_TIER2_MINUTES = "watch_points_tier2_minutes"
CONFIG_DAILY_CAP = "watch_points_daily_cap"

WATCH_POINTS_DEFAULTS = {
    CONFIG_FIRST: "4",
    CONFIG_FIRST_MINUTES: "20",
    CONFIG_TIER1: "8",
    CONFIG_TIER1_MINUTES: "45",
    CONFIG_TIER2: "7",
    CONFIG_TIER2_MINUTES: "120",
    CONFIG_DAILY_CAP: "16",
}

POINTS_TYPE = "watch_points"

# (配置奖励键, 配置分钟键, 档位名, 流水说明)——顺序即发放顺序（先首看后档位）
_TIERS = (
    (CONFIG_FIRST, CONFIG_FIRST_MINUTES, "first", "观影能量·首看奖"),
    (CONFIG_TIER1, CONFIG_TIER1_MINUTES, "tier1", "观影能量·时长一档"),
    (CONFIG_TIER2, CONFIG_TIER2_MINUTES, "tier2", "观影能量·时长二档"),
)


def _cfg_int(db: Session, key: str, default: int) -> int:
    raw = store.get_value(db, key, "")
    if raw is None or str(raw).strip() == "":
        return default
    try:
        return int(float(str(raw).strip()))
    except (TypeError, ValueError):
        return default


def get_watch_points_config(db: Session) -> dict:
    return {
        "first": _cfg_int(db, CONFIG_FIRST, 4),
        "first_minutes": _cfg_int(db, CONFIG_FIRST_MINUTES, 20),
        "tier1": _cfg_int(db, CONFIG_TIER1, 8),
        "tier1_minutes": _cfg_int(db, CONFIG_TIER1_MINUTES, 45),
        "tier2": _cfg_int(db, CONFIG_TIER2, 7),
        "tier2_minutes": _cfg_int(db, CONFIG_TIER2_MINUTES, 120),
        "daily_cap": _cfg_int(db, CONFIG_DAILY_CAP, 16),
    }


def validate_watch_points_config(values: dict) -> dict:
    """校验观影能量配置：返回 {key: error_msg}，空 dict 表示全部合法。

    规则：全部为非负整数；分钟数须 first <= tier1 <= tier2（倒挂时
    高档永远先于低档触发，属配置错误，直接拦）。
    """
    errors = {}

    def _as_int(key):
        raw = values.get(key, WATCH_POINTS_DEFAULTS[key])
        try:
            return int(float(str(raw).strip()))
        except (TypeError, ValueError, AttributeError):
            errors[key] = "必须是整数"
            return None

    parsed = {}
    for key in WATCH_POINTS_DEFAULTS:
        v = _as_int(key)
        if v is not None:
            if v < 0:
                errors[key] = "不能为负数"
            parsed[key] = v
    if (CONFIG_FIRST_MINUTES in parsed and CONFIG_TIER1_MINUTES in parsed
            and parsed[CONFIG_TIER1_MINUTES] < parsed[CONFIG_FIRST_MINUTES]):
        errors[CONFIG_TIER1_MINUTES] = "一档分钟数不能小于首看分钟数"
    if (CONFIG_TIER1_MINUTES in parsed and CONFIG_TIER2_MINUTES in parsed
            and parsed[CONFIG_TIER2_MINUTES] < parsed[CONFIG_TIER1_MINUTES]):
        errors[CONFIG_TIER2_MINUTES] = "二档分钟数不能小于一档分钟数"
    return errors


def _today_str() -> str:
    """「今天」的日期字符串（Asia/Shanghai 自然日），与活力每日扣减认领同款。"""
    try:
        from zoneinfo import ZoneInfo
        return datetime.now(ZoneInfo("Asia/Shanghai")).date().isoformat()
    except Exception:  # noqa: BLE001 — 缺 tzdata 等极端情况回退
        return date.today().isoformat()


def _tier_ref(day: str, tier_name: str) -> str:
    return f"{POINTS_TYPE}:{day}:{tier_name}"


def award_watch_points(db: Session, user_id: int, watched_minutes,
                       item_id, finished: bool = False) -> int:
    """观影能量结算：把本次停播的分钟数计入当日累计，跨档则发能量。

    返回本次实际发放的能量（0 = 未发放：非公益用户 / 本次无新增分钟 /
    未跨新档 / 已达日封顶）。自带事务：成功提交、失败回滚返回 0。
    """
    cfg = get_watch_points_config(db)
    tiers = [
        (name, cfg[f"{name}_minutes"], cfg[name], label)
        for _rk, _mk, name, label in _TIERS
    ]
    if all(reward <= 0 for _n, _m, reward, _l in tiers):
        return 0
    try:
        minutes = float(watched_minutes or 0)
    except (TypeError, ValueError):
        return 0
    if minutes <= 0:
        return 0

    # 函数内导入 economy 避开循环导入（与 member_level 同款写法）
    from backend.api.economy import _add_points, _today_start_utc_naive, lock_user_row

    day = _today_str()
    try:
        # 先锁行：「查累计-判跨档-发放」在同一事务内串行化（与活力/XP 同款）
        lock_user_row(db, user_id)
        user = db.query(models.WebUser).filter(models.WebUser.id == user_id).first()
        if user is None or not getattr(user, "is_welfare", False):
            db.rollback()
            return 0

        row = (db.query(models.WatchPointsDaily)
               .filter(models.WatchPointsDaily.user_id == user_id,
                       models.WatchPointsDaily.day == day)
               .first())
        if row is None:
            row = models.WatchPointsDaily(
                user_id=user_id, day=day, total_minutes=0, item_baselines="{}")
            db.add(row)
            db.flush()
        try:
            baselines = json.loads(row.item_baselines or "{}")
            if not isinstance(baselines, dict):
                baselines = {}
        except (TypeError, ValueError):
            baselines = {}

        key = str(item_id)
        try:
            baseline = float(baselines.get(key, 0) or 0)
        except (TypeError, ValueError):
            baseline = 0.0
        contribution = max(0.0, minutes - baseline)
        total = float(row.total_minutes or 0) + contribution
        # 看完本轮结束：基线清零，重看从 0 重新计入；未看完只抬不降
        baselines[key] = 0.0 if finished else max(baseline, minutes)
        row.total_minutes = total
        row.item_baselines = json.dumps(baselines)

        # 今日已发（日封顶核算）：只认本类型流水，时间窗用上海日界 UTC 帧
        today_start = _today_start_utc_naive()
        granted_today = int(
            db.query(func.coalesce(func.sum(models.PointsLog.amount), 0))
            .filter(models.PointsLog.user_id == user_id,
                    models.PointsLog.type == POINTS_TYPE,
                    models.PointsLog.created_at >= today_start)
            .scalar() or 0)
        cap = cfg["daily_cap"]

        granted = 0
        for name, threshold, reward, label in tiers:
            if reward <= 0 or total < threshold:
                continue
            ref = _tier_ref(day, name)
            already = (db.query(models.PointsLog.id)
                       .filter(models.PointsLog.user_id == user_id,
                               models.PointsLog.type == POINTS_TYPE,
                               models.PointsLog.ref_id == ref)
                       .first())
            if already is not None:
                continue
            amount = reward
            if cap > 0:
                amount = min(amount, cap - granted_today - granted)
            if amount <= 0:
                continue
            _add_points(db, user, amount, POINTS_TYPE, label, ref)
            granted += amount
        db.commit()
    except Exception:
        db.rollback()
        logger.exception("观影能量发放失败 user=%s item=%s", user_id, item_id)
        return 0
    return granted


__all__ = [
    "WATCH_POINTS_DEFAULTS", "get_watch_points_config",
    "validate_watch_points_config", "award_watch_points", "POINTS_TYPE",
]
