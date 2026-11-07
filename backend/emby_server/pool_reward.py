# -*- coding: utf-8 -*-
"""公共贡献池激励结算（goal-33 PR-6，design §11）。

三层，口径单点：
- 计量纯函数 ``meter_member``：输入起播事件（H1 pool_play_events）
  与成员状态区间（H2 pool_member_status_events 展开），输出
  counted 起播与有效在线日。防刷四规则全在此层（§11.2）：本人
  及同人多盘互看不计（按 user_id 判）、同观众同副本去重窗口内
  只计首次、单日 counted 达冻结线后当日余量不计且整期标
  frozen、违规清退由执行器在函数外按 pool_gate
  .member_violation_ejected 判定后标 skipped。纯函数不碰 DB，
  结算与管理端预览共用同一份，事后可重算。
- 结算执行器 ``run_settlement``：管理员手动触发（自动定时默认
  不开）。先落明细行（(settlement_id, member_id) 唯一键第一道
  幂等锚）→ 真发开关开时同一事务发积分＋经验（points_logs
  .ref_id = ``pool_reward:{detail_id}`` 第二道锚），连跑不双发。
  真发开关（pool_reward_payout_enabled）默认关：落行 status
  =shadow 只算账不发钱（影子跑对账），开关开后重跑同批补发。
- 称号 ``pool_identity_payload``：状态驱动展示（§11.4），成员
  有 active 行即出「片库贡献者」，无发放动作、无存储。

周期口径：边界按 Asia/Shanghai 自然日/月（与成就日界一致），
入库时间列是 UTC naive 存储帧（生产容器 UTC），故周期界与
日界一律换算成 UTC naive 再与 created_at 比较（沿 economy
._today_start_utc_naive 的帧口径）。

一期边界（定版）：公益天数不发（pool_reward_welfare_days
默认 0、执行器不走 grant_welfare 通道）；订阅延期不进菜单；
不达门槛不结且不累计到下期（每期独立按本期事件计量，design
§11 未写顺延口径，按不累计实现）。
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Optional

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from backend import models as base_models
from backend.emby_server import models as em
from backend.emby_server import pool_gate

logger = logging.getLogger(__name__)

POOL_CONTRIBUTOR_TITLE = "片库贡献者"

_ACTIVE_DAY_MIN_SECONDS = 12 * 3600

#: 结算成员圈选：只有真进过池（被批准过）的状态才可能有贡献；
#: candidate/pending/rejected 从未下发过副本，不建明细行。
_SETTLABLE_STATUSES = ("active", "suspended", "exited", "ejected")

#: 进批次参数快照的计量键（费率/门槛/防刷参数）；总开关与真发
#: 开关是运行时闸门、读实时值，不进快照（影子批开闸后重跑要能
#: 按原计量口径补发，靠的就是快照里这组键不被事后改动污染）。
_SNAPSHOT_KEYS = (
    "pool_reward_period",
    "pool_reward_points_per_play",
    "pool_reward_points_per_active_day",
    "pool_reward_points_cap",
    "pool_reward_min_active_days",
    "pool_reward_min_counted_plays",
    "pool_reward_play_dedupe_window_sec",
    "pool_reward_daily_play_freeze",
    "pool_reward_welfare_days",
    "pool_reward_welfare_min_plays",
    "pool_reward_xp_per_play",
    "pool_reward_xp_cap",
)


class PoolRewardError(Exception):
    """结算业务拒绝：kind 供端点映射状态码，message 给人看。"""

    def __init__(self, kind: str, message: str):
        super().__init__(message)
        self.kind = kind
        self.message = message


# ------------------------------------------------------------ 时间帧


try:
    from zoneinfo import ZoneInfo

    _SH_TZ = ZoneInfo("Asia/Shanghai")
except Exception:  # noqa: BLE001 — 缺 tzdata 时上海固定 +8 兜底（无夏令时）
    _SH_TZ = timezone(timedelta(hours=8))


def _sh_date(ts_utc_naive: datetime) -> date:
    """UTC naive 时间戳落到哪个上海自然日。"""
    return ts_utc_naive.replace(tzinfo=timezone.utc) \
        .astimezone(_SH_TZ).date()


def _sh_midnight_utc(d: date) -> datetime:
    """上海自然日 d 的 00:00，换算成 UTC naive（存储帧）。"""
    return datetime(d.year, d.month, d.day, tzinfo=_SH_TZ) \
        .astimezone(timezone.utc).replace(tzinfo=None)


# ------------------------------------------------------------ 周期


def period_key_for(kind: str, sh_day: date) -> str:
    if kind == "weekly":
        iso = sh_day.isocalendar()
        return f"{iso.year:04d}-W{iso.week:02d}"
    return f"{sh_day.year:04d}-{sh_day.month:02d}"


def _kind_of_key(period_key: str) -> str:
    return "weekly" if "-W" in (period_key or "") else "monthly"


def period_bounds_utc(kind: str, period_key: str) \
        -> tuple[datetime, datetime]:
    """周期键 → [起, 止) UTC naive 界（上海自然月/周一为界的周）。"""
    if kind == "weekly":
        m = re.fullmatch(r"(\d{4})-W(\d{2})", period_key or "")
        if not m:
            raise PoolRewardError(
                "bad_period", f"周期键格式不对：{period_key!r}")
        try:
            start_d = date.fromisocalendar(
                int(m.group(1)), int(m.group(2)), 1)
        except ValueError:
            raise PoolRewardError(
                "bad_period", f"周期键不存在：{period_key!r}") from None
        end_d = start_d + timedelta(days=7)
    else:
        m = re.fullmatch(r"(\d{4})-(\d{2})", period_key or "")
        if not m:
            raise PoolRewardError(
                "bad_period", f"周期键格式不对：{period_key!r}")
        year, month = int(m.group(1)), int(m.group(2))
        if not 1 <= month <= 12:
            raise PoolRewardError(
                "bad_period", f"周期键不存在：{period_key!r}")
        start_d = date(year, month, 1)
        end_d = (date(year + 1, 1, 1) if month == 12
                 else date(year, month + 1, 1))
    return _sh_midnight_utc(start_d), _sh_midnight_utc(end_d)


def previous_period_key(kind: str,
                        now_utc_naive: Optional[datetime] = None) -> str:
    """上一个已完整结束的周期键（结算默认目标期）。"""
    now = now_utc_naive or datetime.now()
    cur_key = period_key_for(kind, _sh_date(now))
    cur_start, _ = period_bounds_utc(kind, cur_key)
    return period_key_for(kind, _sh_date(cur_start - timedelta(days=1)))


# ------------------------------------------------------------ 计量纯函数


@dataclass
class MeterResult:
    counted_plays: int = 0
    active_days: int = 0
    raw_plays: int = 0
    frozen: bool = False
    verdicts: dict = field(default_factory=dict)
    excluded_self: int = 0
    excluded_dedupe: int = 0
    excluded_over_cap: int = 0


def build_status_intervals(status_events, *, member_status: str,
                           period_start: datetime,
                           period_end: datetime) -> list:
    """H2 状态流水 → 周期内状态区间 [(start, end, status), ...]。

    周期起点状态取起点前最后一次变迁的 to_status；无前置流水时
    取首条流水的 from_status，再缺（成员从没变过状态）回落成员
    当前状态。区间只覆盖 [period_start, period_end)。
    """
    ordered = sorted(
        status_events,
        key=lambda e: (e.created_at, getattr(e, "id", 0) or 0))
    current = None
    for ev in ordered:
        if ev.created_at <= period_start:
            current = ev.to_status
        else:
            break
    if current is None:
        first = ordered[0] if ordered else None
        current = (first.from_status if first is not None
                   and first.from_status else None) or member_status
    intervals = []
    cursor = period_start
    for ev in ordered:
        ts = ev.created_at
        if ts <= period_start or ts >= period_end:
            continue
        if ts > cursor:
            intervals.append((cursor, ts, current))
        current = ev.to_status
        cursor = ts
    if period_end > cursor:
        intervals.append((cursor, period_end, current))
    return intervals


def _active_days(intervals, period_start: datetime,
                 period_end: datetime) -> int:
    """active 区间与上海自然日求交：单日累计 ≥12 小时计 1 天。"""
    first_day = _sh_date(period_start)
    last_day = _sh_date(period_end - timedelta(microseconds=1))
    days = 0
    day = first_day
    while day <= last_day:
        d_start = _sh_midnight_utc(day)
        d_end = d_start + timedelta(days=1)
        seconds = 0.0
        for start, end, status in intervals:
            if status != "active":
                continue
            lo, hi = max(start, d_start), min(end, d_end)
            if hi > lo:
                seconds += (hi - lo).total_seconds()
        if seconds >= _ACTIVE_DAY_MIN_SECONDS:
            days += 1
        day += timedelta(days=1)
    return days


def meter_member(events, *, member_user_id: int, status_intervals,
                 period_start: datetime, period_end: datetime,
                 cfg: dict) -> MeterResult:
    """单成员当期计量（纯函数，防刷口径单点，design §11.2）。

    events：当期 PoolPlayEvent 行（属性访问 id/viewer_user_id/
    variant_id/created_at），函数内按时间排序，调用方不必预排。
    GA-033 已在写入点做过同口径去重，本函数仍全量复核一遍——
    写时去重有无不改变结算口径。
    """
    window = int(cfg.get("pool_reward_play_dedupe_window_sec", 1800))
    freeze = int(cfg.get("pool_reward_daily_play_freeze", 1500))
    result = MeterResult()
    last_counted: dict = {}
    day_counts: dict = {}
    ordered = sorted(
        events, key=lambda e: (e.created_at, getattr(e, "id", 0) or 0))
    for ev in ordered:
        ts = ev.created_at
        if ts < period_start or ts >= period_end:
            continue
        result.raw_plays += 1
        if ev.viewer_user_id == member_user_id:
            result.verdicts[ev.id] = "excluded_self"
            result.excluded_self += 1
            continue
        key = (ev.viewer_user_id, ev.variant_id)
        last = last_counted.get(key)
        if (window > 0 and last is not None
                and (ts - last).total_seconds() < window):
            result.verdicts[ev.id] = "excluded_dedupe"
            result.excluded_dedupe += 1
            continue
        day = _sh_date(ts)
        if freeze > 0 and day_counts.get(day, 0) >= freeze:
            result.verdicts[ev.id] = "excluded_over_cap"
            result.excluded_over_cap += 1
            result.frozen = True
            continue
        result.counted_plays += 1
        day_counts[day] = day_counts.get(day, 0) + 1
        last_counted[key] = ts
        result.verdicts[ev.id] = "counted"
        if freeze > 0 and day_counts[day] >= freeze:
            result.frozen = True
    result.active_days = _active_days(
        status_intervals, period_start, period_end)
    return result


def _amounts(meter: MeterResult, cfg: dict) -> tuple[int, int]:
    """计量 → 应发（积分封顶、经验封顶；上限 0 = 不限）。"""
    points = (meter.counted_plays
              * int(cfg["pool_reward_points_per_play"])
              + meter.active_days
              * int(cfg["pool_reward_points_per_active_day"]))
    cap = int(cfg["pool_reward_points_cap"])
    if cap > 0:
        points = min(points, cap)
    xp = meter.counted_plays * int(cfg["pool_reward_xp_per_play"])
    xp_cap = int(cfg["pool_reward_xp_cap"])
    if xp_cap > 0:
        xp = min(xp, xp_cap)
    return points, xp


def _decide(meter: MeterResult, cfg: dict, violation: bool) \
        -> tuple[str, Optional[str]]:
    """计量结论 → 明细走向（payable/frozen/skipped + 原因）。

    顺序写死：违规清退最先（资格问题压过计量结论）；冻结次之
    （待人工审、不是没收，放行按 capped 数发，不再过门槛）；
    门槛最后（双条件同时满足才结）。
    """
    if violation:
        return "skipped", "ejected_violation"
    if meter.frozen:
        return "frozen", "daily_play_freeze"
    if (meter.active_days < int(cfg["pool_reward_min_active_days"])
            or meter.counted_plays
            < int(cfg["pool_reward_min_counted_plays"])):
        return "skipped", "below_threshold"
    return "payable", None


# ------------------------------------------------------------ 称号


def pool_identity_payload(db: Session, user) -> Optional[dict]:
    """贡献者称号展示（design §11.4）：用户名下有 active 成员行
    即出「片库贡献者」；suspended/exited/ejected/pending 都不出。
    状态驱动、零存储零发放；异常口径与白名单身份一致，查不到
    即 None（调用方负责兜底不拖垮出参）。"""
    if user is None:
        return None
    cfg = pool_gate.reward_config_payload(db)
    if not cfg.get("pool_reward_title_enabled", True):
        return None
    row = (db.query(em.PoolMember)
           .filter(em.PoolMember.user_id == user.id,
                   em.PoolMember.status == "active")
           .first())
    if row is None:
        return None
    return {"title": POOL_CONTRIBUTOR_TITLE}


# ------------------------------------------------------------ 执行器


def _member_meter(db: Session, member, start: datetime,
                  end: datetime, cfg: dict) \
        -> tuple[MeterResult, int]:
    events = (db.query(em.PoolPlayEvent)
              .filter(em.PoolPlayEvent.member_id == member.id,
                      em.PoolPlayEvent.created_at >= start,
                      em.PoolPlayEvent.created_at < end)
              .order_by(em.PoolPlayEvent.created_at,
                        em.PoolPlayEvent.id)
              .all())
    status_events = (db.query(em.PoolMemberStatusEvent)
                     .filter(
                         em.PoolMemberStatusEvent.member_id == member.id)
                     .order_by(em.PoolMemberStatusEvent.created_at,
                               em.PoolMemberStatusEvent.id)
                     .all())
    intervals = build_status_intervals(
        status_events, member_status=member.status,
        period_start=start, period_end=end)
    meter = meter_member(
        events, member_user_id=member.user_id,
        status_intervals=intervals,
        period_start=start, period_end=end, cfg=cfg)
    # 转存字节只作明细展示/对账列（一期不计奖）：用量行按 UTC
    # 日键，近似圈到周期 UTC 日期范围即可，不追求分钟级精确。
    transfer_bytes = (
        db.query(em.PoolUsageDaily)
        .filter(em.PoolUsageDaily.member_id == member.id,
                em.PoolUsageDaily.day >= start.date(),
                em.PoolUsageDaily.day <= end.date())
        .with_entities(em.PoolUsageDaily.transfer_bytes).all())
    return meter, int(sum(int(r[0] or 0) for r in transfer_bytes))


def _grant_detail(db: Session, batch, detail) -> str:
    """按明细应发量真发积分＋经验（同一事务、调用方提交）。

    幂等第二道锚：points_logs.ref_id = pool_reward:{detail_id}
    已存在即已发过，只补标 paid 不重复发；经验同理查
    member_xp_log 的 (source, ref_id)。返回最终状态。
    """
    user = (db.query(base_models.WebUser)
            .filter(base_models.WebUser.id == detail.user_id).first())
    if user is None:
        detail.status = "skipped"
        detail.reason = "user_missing"
        return "skipped"
    points_ref = f"pool_reward:{detail.id}"
    if int(detail.points or 0) > 0:
        log = (db.query(base_models.PointsLog)
               .filter(base_models.PointsLog.ref_id == points_ref)
               .first())
        if log is None:
            from backend.api.economy import _add_points

            _add_points(
                db, user, int(detail.points), "pool_reward",
                f"公共池贡献激励（{batch.period_key}）",
                ref_id=points_ref)
            log = (db.query(base_models.PointsLog)
                   .filter(base_models.PointsLog.ref_id == points_ref)
                   .first())
        if log is not None:
            detail.points_log_id = log.id
    if int(detail.xp or 0) > 0:
        xp_log = (db.query(base_models.MemberXpLog)
                  .filter(
                      base_models.MemberXpLog.user_id == user.id,
                      base_models.MemberXpLog.source == "pool_reward",
                      base_models.MemberXpLog.ref_id == str(detail.id))
                  .first())
        if xp_log is None:
            from backend.member_level import add_xp

            add_xp(db, user, int(detail.xp), source="pool_reward",
                   ref_id=str(detail.id))
    detail.status = "paid"
    detail.granted_at = datetime.now()
    return "paid"


def _settle_member(db: Session, batch, member, cfg: dict,
                   payout: bool) -> str:
    """单成员结算（自带提交）：返回本轮该行最终状态。

    重跑语义：paid/frozen 不动；shadow/pending 在真发开时补发；
    skipped 只重评 ejected_violation 一类（违规标志可能已被
    管理员清除），其余 skipped 是计量结论、重跑不变。
    """
    existing = (db.query(em.PoolRewardDetail)
                .filter(em.PoolRewardDetail.settlement_id == batch.id,
                        em.PoolRewardDetail.member_id == member.id)
                .first())
    if existing is not None:
        if existing.status in ("paid", "frozen"):
            return existing.status
        if existing.status in ("shadow", "pending"):
            if not payout:
                return existing.status
            outcome = _grant_detail(db, batch, existing)
            db.commit()
            return outcome
        if existing.reason != "ejected_violation":
            return "skipped"
        # skipped + 违规清退：往下走重新计量判定（标志可能已清）。
    meter, transfer_bytes = _member_meter(
        db, member, batch.period_start, batch.period_end, cfg)
    violation = pool_gate.member_violation_ejected(db, member.id)
    decision, reason = _decide(meter, cfg, violation)
    points, xp = _amounts(meter, cfg)
    if existing is None:
        detail = em.PoolRewardDetail(
            settlement_id=batch.id, member_id=member.id,
            user_id=member.user_id, status="pending")
        db.add(detail)
    else:
        detail = existing
    detail.counted_plays = meter.counted_plays
    detail.active_days = meter.active_days
    detail.raw_plays = meter.raw_plays
    detail.transfer_bytes = transfer_bytes
    detail.points = points
    detail.xp = xp
    detail.welfare_days = 0
    if decision == "payable":
        if payout:
            db.flush()  # 先拿到 detail.id（ref_id 锚要用）
            outcome = _grant_detail(db, batch, detail)
            db.commit()
            return outcome
        detail.status = "shadow"
        detail.reason = None
        db.commit()
        return "shadow"
    detail.status = decision
    detail.reason = reason
    db.commit()
    return decision


def _resolve_period(db: Session, period_key: Optional[str]) \
        -> tuple[str, str, datetime, datetime, dict]:
    cfg = pool_gate.reward_config_payload(db)
    kind = cfg["pool_reward_period"]
    key = period_key or previous_period_key(kind)
    key_kind = _kind_of_key(key)
    start, end = period_bounds_utc(key_kind, key)
    return key, key_kind, start, end, cfg


def run_settlement(db: Session, period_key: Optional[str] = None,
                   *, created_by: Optional[int] = None) -> dict:
    """执行一个周期的结算（管理员手动触发口的服务函数）。

    激励总开关关 → PoolRewardError 不建批。批次按 period_key
    get-or-create（唯一键），计量口径用建批快照；真发开关读
    实时值（影子批开闸重跑补发）。逐成员独立提交，单成员失败
    不拖垮整批（记 last_error、批次标 failed 可重跑）。
    """
    key, kind, start, end, live_cfg = _resolve_period(db, period_key)
    if not live_cfg["pool_reward_enabled"]:
        raise PoolRewardError(
            "disabled", "激励总开关（pool_reward_enabled）未开启，"
            "不建批不发放；计量数据照常积累，可先开预览核对。")
    payout = bool(live_cfg["pool_reward_payout_enabled"])
    batch = (db.query(em.PoolRewardSettlement)
             .filter(em.PoolRewardSettlement.period_key == key)
             .first())
    if batch is None:
        snapshot = {k: live_cfg[k] for k in _SNAPSHOT_KEYS}
        batch = em.PoolRewardSettlement(
            period_key=key, period_kind=kind,
            period_start=start, period_end=end,
            status="running", shadow=not payout,
            params_snapshot=json.dumps(snapshot, ensure_ascii=False),
            created_by=created_by)
        db.add(batch)
        try:
            db.commit()
        except IntegrityError:
            db.rollback()
            batch = (db.query(em.PoolRewardSettlement)
                     .filter(em.PoolRewardSettlement.period_key == key)
                     .first())
    db.refresh(batch)
    cfg = dict(live_cfg)
    if batch.params_snapshot:
        try:
            cfg.update(json.loads(batch.params_snapshot))
        except (TypeError, ValueError):
            logger.warning("结算批次 %s 参数快照解析失败，用实时配置",
                           batch.id, exc_info=True)
    batch.status = "running"
    db.commit()

    members = (db.query(em.PoolMember)
               .filter(em.PoolMember.status.in_(_SETTLABLE_STATUSES))
               .order_by(em.PoolMember.id.asc()).all())
    summary = {"period_key": key, "settlement_id": batch.id,
               "payout": payout, "paid": 0, "shadow": 0, "frozen": 0,
               "skipped": 0, "errors": 0}
    for member in members:
        try:
            outcome = _settle_member(db, batch, member, cfg, payout)
        except Exception as exc:  # noqa: BLE001 — 单成员失败不拖垮整批
            db.rollback()
            logger.warning("池激励结算成员失败 member=%s period=%s",
                           member.id, key, exc_info=True)
            batch.last_error = f"member {member.id}: {exc}"[:255]
            summary["errors"] += 1
            continue
        if outcome in summary:
            summary[outcome] += 1
    batch.status = "failed" if summary["errors"] else "done"
    if summary["errors"] == 0:
        batch.finished_at = datetime.now()
    db.commit()
    return summary


def preview_settlement(db: Session,
                       period_key: Optional[str] = None) -> dict:
    """结算预览（只算不落行）：与 run_settlement 同一份计量与
    判定，would_status 按当前真发开关折算（开→paid、关→shadow）。
    总开关关时也可预览（先核对计量再决定开不开）。"""
    key, kind, start, end, cfg = _resolve_period(db, period_key)
    payout = bool(cfg["pool_reward_payout_enabled"])
    members = (db.query(em.PoolMember)
               .filter(em.PoolMember.status.in_(_SETTLABLE_STATUSES))
               .order_by(em.PoolMember.id.asc()).all())
    rows = []
    for member in members:
        meter, transfer_bytes = _member_meter(db, member, start, end,
                                              cfg)
        violation = pool_gate.member_violation_ejected(db, member.id)
        decision, reason = _decide(meter, cfg, violation)
        points, xp = _amounts(meter, cfg)
        user = (db.query(base_models.WebUser)
                .filter(base_models.WebUser.id == member.user_id)
                .first())
        rows.append({
            "member_id": member.id,
            "user_id": member.user_id,
            "username": user.username if user is not None else None,
            "member_status": member.status,
            "counted_plays": meter.counted_plays,
            "active_days": meter.active_days,
            "raw_plays": meter.raw_plays,
            "transfer_bytes": transfer_bytes,
            "points": points,
            "xp": xp,
            "would_status": ("paid" if payout else "shadow")
            if decision == "payable" else decision,
            "reason": reason,
        })
    return {
        "period_key": key,
        "period_kind": kind,
        "period_start": start.isoformat(),
        "period_end": end.isoformat(),
        "reward_enabled": bool(cfg["pool_reward_enabled"]),
        "payout_enabled": payout,
        "rows": rows,
    }


def release_frozen_detail(db: Session, detail_id: int, *,
                          actor_id: Optional[int] = None):
    """frozen 明细人工审放行：按已 capped 的计量数发放（真发关
    时落 shadow 待开闸补发）。放行留痕 released_by/at；不是
    frozen 的行拒绝（状态机不许跳步）。"""
    detail = (db.query(em.PoolRewardDetail)
              .filter(em.PoolRewardDetail.id == detail_id).first())
    if detail is None:
        raise PoolRewardError("not_found", "结算明细不存在。")
    if detail.status != "frozen":
        raise PoolRewardError(
            "invalid_state", "只有冻结待审的明细可以放行。")
    batch = (db.query(em.PoolRewardSettlement)
             .filter(em.PoolRewardSettlement.id == detail.settlement_id)
             .first())
    cfg = pool_gate.reward_config_payload(db)
    detail.released_by = actor_id
    detail.released_at = datetime.now()
    if cfg["pool_reward_payout_enabled"]:
        _grant_detail(db, batch, detail)
    else:
        detail.status = "shadow"
    db.commit()
    db.refresh(detail)
    return detail
