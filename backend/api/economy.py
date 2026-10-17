"""
用户经济系统 API：每日签到 / 兑换码 / 支付下单 / 订单查询

端点（前缀 /api/user/economy）：
- GET  /checkin/status        查询今日签到状态、连签、奖励规则
- POST /checkin               每日签到（发积分，连续签到加成）
- GET  /points/log            积分流水（台账）
- GET  /exchange/config       公开的积分/订阅兑换奖励规则
- POST /exchange/redeem       兑换码核销（积分 / 订阅）
- GET  /payment/packages      充值套餐列表（公开）
- GET  /payment/plans         订阅套餐列表（公开）
- GET  /payment/methods       可用支付方式（按网关能力推导）
- GET  /payment/coupon/config 优惠券开关（关闭时用户端不显示优惠码输入框）
- POST /payment/coupon/quote  优惠码试算（能不能用、省多少、实付多少）
- POST /payment/order         创建支付订单（充值积分 / 购买订阅），返回支付跳转 URL
- GET  /payment/orders        我的订单列表
"""
from __future__ import annotations

import hashlib
import hmac
import logging
import math
import random
import time
import uuid
from datetime import datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Optional
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse, PlainTextResponse
from pydantic import BaseModel, Field
from sqlalchemy import func, or_
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.orm import Session, joinedload

from backend import coupons, models, realms, vitality as _vitality
from backend.database import get_db
from backend.api.user import get_current_user
from backend.ratelimit import check_rate_limit, client_ip
from backend.security import resolve_jwt_user_id
from backend.notifications import notify_admin_event
from backend.tg_bind import require_tg_bound

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/user/economy", tags=["用户端-经济系统"])

bearer_scheme_deps = None  # reserved


# ==================== 配置中心工具 ====================

def _get_config(db: Session, key: str, default: str) -> str:
    config = db.query(models.SystemConfig).filter(models.SystemConfig.key == key).first()
    return config.value if config and config.value is not None else default


def _get_int_config(db: Session, key: str, default: int) -> int:
    try:
        return int(_get_config(db, key, str(default)))
    except (TypeError, ValueError):
        return default


def _get_float_config(db: Session, key: str, default: float) -> float:
    """读浮点配置（P2 货币体系：recharge_ratio 用）"""
    try:
        return float(_get_config(db, key, str(default)))
    except (TypeError, ValueError):
        return default


def _get_quick_amounts(db: Session) -> list[int]:
    """快捷充值金额（C4）：读 recharge_quick_amounts，逗号分隔；非法/空回退默认；去重、升序、最多 8 个、只保留 1~100000 的正整数。"""
    default_amounts = [10, 30, 50, 100, 200]
    raw = _get_config(db, "recharge_quick_amounts", "10,30,50,100,200")
    try:
        values = [int(part.strip()) for part in raw.split(",")]
    except (TypeError, ValueError):
        return default_amounts
    amounts = sorted({v for v in values if 1 <= v <= 100000})[:8]
    return amounts or default_amounts


def _get_bool_config(db: Session, key: str, default: bool) -> bool:
    return _get_config(db, key, "true" if default else "false").strip().lower() == "true"


# ==================== 签到 ====================

def _today_start() -> datetime:
    """「今日」起点：Asia/Shanghai 自然日 00:00（UTC naive 存储帧）。

    r34 口径收口：此前是服务器本地 00:00，生产容器为 UTC，签到/连签的
    「今日」实际在北京时间 08:00 才翻天（r18 F1、r30 L1、r33 F3 同类）。
    直接委托给 _today_start_utc_naive()，与 CheckinRecord.checkin_date 的
    写入帧保持一致（写入点唯一：_do_checkin_core）。
    """
    return _today_start_utc_naive()


def _today_start_utc_naive() -> datetime:
    """上海自然日 00:00，换算成 UTC naive——与入库时间列的存储帧对齐。

    生产容器系统时区为 UTC，``PointsLog.created_at`` 等列以 UTC naive 入库；
    直接用服务器本地 00:00 当「今日」起点，会让日限额在北京时间 08:00 才重置
    （r18 F1、r30 L1 同类问题）。返回的起点已是 UTC 帧，可直接与入库列比较。
    """
    from zoneinfo import ZoneInfo
    try:
        now_sh = datetime.now(ZoneInfo("Asia/Shanghai"))
    except Exception:  # noqa: BLE001 — 缺 tzdata 等极端情况回退
        now_sh = datetime.now().astimezone()
    start_sh = now_sh.replace(hour=0, minute=0, second=0, microsecond=0)
    return start_sh.astimezone(timezone.utc).replace(tzinfo=None)


def _month_start_utc_naive() -> datetime:
    """上海自然月 1 日 00:00，换算成 UTC naive——与入库时间列的存储帧对齐。

    求片月度额度（media_seek.used_this_month、welfare_requests._month_start）
    此前用服务器本地月界，生产 UTC 下每月 1 日 08:00（北京时间）才重置。
    与 _today_start_utc_naive() 同一写法。
    """
    from zoneinfo import ZoneInfo
    try:
        now_sh = datetime.now(ZoneInfo("Asia/Shanghai"))
    except Exception:  # noqa: BLE001 — 缺 tzdata 等极端情况回退
        now_sh = datetime.now().astimezone()
    start_sh = now_sh.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    return start_sh.astimezone(timezone.utc).replace(tzinfo=None)


def _points_record_hash(
    prev_hash: str, user_id: int, amount: int, balance_after: int,
    type_: str, description: str | None, ref_id: str | None, created_at,
) -> str:
    """C3 流水审计：计算单条流水的 hash。

    hash = sha256("prev_hash|user_id|amount|balance_after|type|description|ref_id|created_at_iso")。
    每条记录链接上一条的 record_hash，形成防篡改链；任何字段被改都会导致本条及后续 hash 对不上。
    """
    ts = created_at.isoformat() if hasattr(created_at, "isoformat") else str(created_at)
    payload = "|".join([
        prev_hash or "",
        str(user_id), str(amount), str(balance_after),
        type_ or "", description or "", ref_id or "", ts,
    ])
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _append_points_log(
    db: Session, user_id: int, amount: int, balance_after: int,
    type_: str, description: str, ref_id: str | None = None,
) -> models.PointsLog:
    """写一条积分流水并按 `points_audit_enabled` 开关接入 hash 链（C3）。

    调用方负责余额本身的增减与 balance_after 的正确性；本函数只负责台账行。
    开关开启时用行锁取该用户最新一条流水作为链尾，写入 prev_hash / record_hash；
    关闭时两列留 NULL。返回 PointsLog 对象（已 add，未 commit）。

    注意：刻意不在这里 flush。调用方（如 apply_invitation）依赖"所有写一次 flush、
    冲突整体回滚"的语义，提前 flush 会把唯一约束冲突提前抛到它们的 try/except 之外。
    created_at 在构造时显式赋值（与列默认 datetime.now 等价），hash 直接用该值计算。
    """
    audit_enabled = _get_bool_config(db, "points_audit_enabled", True)
    prev_hash = ""
    if audit_enabled:
        # 行锁取链尾：并发写同一用户时串行化，保证 prev_hash 链接不断
        tail = (
            db.query(models.PointsLog)
            .filter(models.PointsLog.user_id == user_id)
            .order_by(models.PointsLog.id.desc())
            .with_for_update()
            .first()
        )
        prev_hash = (tail.record_hash if tail and tail.record_hash else "") or ""
    now = datetime.now()
    log = models.PointsLog(
        user_id=user_id,
        amount=amount,
        balance_after=balance_after,
        type=type_,
        description=description,
        ref_id=ref_id,
        created_at=now,
    )
    if audit_enabled:
        log.prev_hash = prev_hash
        log.record_hash = _points_record_hash(
            prev_hash, user_id, amount, balance_after,
            type_, description, ref_id, now,
        )
    db.add(log)
    return log


def _add_points(
    db: Session, user: models.WebUser, amount: int,
    type_: str, description: str, ref_id: str | None = None,
) -> int:
    """加积分并写台账（amount 可为负）

    用 SQL 级自增（`points = coalesce(points,0) + amount`）而不是 Python 读改写：
    后者在并发下会丢更新（两个请求各自读到同一旧余额，后提交的覆盖前者）。
    返回写入后的真实余额。
    """
    db.query(models.WebUser).filter(models.WebUser.id == user.id).update(
        {models.WebUser.points: func.coalesce(models.WebUser.points, 0) + amount},
        synchronize_session="fetch",
    )
    balance = int(
        db.query(models.WebUser.points).filter(models.WebUser.id == user.id).scalar() or 0
    )
    _append_points_log(db, user.id, amount, balance, type_, description, ref_id)
    return balance


class InsufficientPoints(ValueError):
    """余额不足（_spend_points 的条件扣减没扣到）"""


def lock_user_row(db: Session, user_id: int) -> None:
    """对用户行加写锁，持有到本事务提交/回滚（每日/滚动额度类「读用量-判上限-写」的串行化原语）。

    额度检查（今日已转出、今日群发言已得、7 天红包收发次数）都是先 SUM/COUNT 再判断：
    并发两个请求都读到同一旧用量、都判定未超限 → 上限被绕过。统一做法：检查前先锁住
    该用户行，再在锁内统计用量；第二个请求在锁上等前者提交后，重新统计就能看到前者的记录。
    - PostgreSQL：SELECT ... FOR UPDATE（READ COMMITTED 下锁后的新语句看得到已提交数据）；
    - SQLite：无行锁，用无副作用 UPDATE 取得库级写锁（同样持有到提交）。
    不需要新表/迁移。必须在统计用量之前调用。
    """
    from sqlalchemy import text as _text
    if db.get_bind().dialect.name == "postgresql":
        db.execute(_text("SELECT id FROM web_users WHERE id = :id FOR UPDATE"), {"id": user_id})
    else:
        db.execute(_text("UPDATE web_users SET id = id WHERE id = :id"), {"id": user_id})


def _spend_points(
    db: Session, user: models.WebUser, cost: int,
    type_: str, description: str, ref_id: str | None = None,
) -> int:
    """原子扣积分：``UPDATE ... SET points = points - cost WHERE points >= cost``。

    「先读余额判断够不够、再 _add_points 扣」是读-改-写：两个并发请求都读到同一旧余额、
    都判定够用，然后各扣一次 → 余额被扣成负数（双花）。这里把「够不够」放进 UPDATE 的
    WHERE 里由数据库判定：并发第二个请求在行锁上等前者提交后拿到 0 行 → InsufficientPoints。
    成功时写台账并返回扣后余额；失败时什么都没写（调用方按需回滚自己的其它改动）。
    """
    cost = int(cost)
    if cost <= 0:
        raise ValueError("扣减积分必须为正数")
    updated = (
        db.query(models.WebUser)
        .filter(
            models.WebUser.id == user.id,
            func.coalesce(models.WebUser.points, 0) >= cost,
        )
        .update(
            {models.WebUser.points: func.coalesce(models.WebUser.points, 0) - cost},
            synchronize_session="fetch",
        )
    )
    if not updated:
        balance = int(
            db.query(models.WebUser.points).filter(models.WebUser.id == user.id).scalar() or 0
        )
        raise InsufficientPoints(f"积分不足，需要 {cost} 分，当前 {balance} 分")
    balance = int(
        db.query(models.WebUser.points).filter(models.WebUser.id == user.id).scalar() or 0
    )
    _append_points_log(db, user.id, -cost, balance, type_, description, ref_id)
    return balance


def verify_points_chain(db: Session, user_id: int, limit: int = 20000) -> dict:
    """C3 流水审计：核验某用户的积分流水 hash 链是否完整。

    按 id 升序逐条重算 hash 并校验链接：
    - record_hash 为 NULL 的是审计开启前的历史记录，跳过校验（计入 legacy_skipped），
      且链条从其之后的第一条审计记录重新起头（prev_hash=""）；
    - 每条审计记录校验两点：prev_hash == 上一条的 record_hash（链头为 ""），
      且按同样算法重算的 hash == record_hash。

    返回 {"ok", "total", "verified", "legacy_skipped", "broken_at", "broken_reason"}。

    只核验最近 limit 条（倒序取再正序验）：用户流水可能很多，抽查最近的才有意义。
    多取一条做锚点：窗口内第一条的 prev_hash 应等于锚点的 record_hash（无锚点=全量，
    则第一条必须是链头 ""）。
    """
    rows = (
        db.query(models.PointsLog)
        .filter(models.PointsLog.user_id == user_id)
        .order_by(models.PointsLog.id.desc())
        .limit(limit + 1)
        .all()
    )
    rows.reverse()  # 最老在前
    if len(rows) == limit + 1:
        anchor, logs = rows[0], rows[1:]
        expected_prev = anchor.record_hash or ""  # 锚点是历史记录时链条从 "" 起头
    else:
        logs = rows
        expected_prev = ""
    verified = 0
    legacy_skipped = 0
    broken_at = None
    broken_reason = ""
    for log in logs:
        if not log.record_hash:
            legacy_skipped += 1
            expected_prev = ""  # 历史缺口之后链条重新起头
            continue
        if (log.prev_hash or "") != expected_prev:
            broken_at = log.id
            broken_reason = "prev_hash 链接断裂（上一条记录可能被删除或篡改）"
            break
        recalc = _points_record_hash(
            log.prev_hash or "", log.user_id, log.amount, log.balance_after,
            log.type, log.description, log.ref_id, log.created_at,
        )
        if recalc != log.record_hash:
            broken_at = log.id
            broken_reason = "本条记录字段被篡改（重算 hash 与记录不一致）"
            break
        verified += 1
        expected_prev = log.record_hash
    return {
        "ok": broken_at is None,
        "total": len(logs),
        "verified": verified,
        "legacy_skipped": legacy_skipped,
        "broken_at": broken_at,
        "broken_reason": broken_reason,
    }


def _checkin_rules(db: Session) -> dict:
    """签到奖励规则（管理端 SystemConfig 可调）

    2026-10-09 合并公益服签到后的统一规则：
    - 基础积分：checkin_base_min ~ checkin_base_max 随机；
      未配置 min/max 时回退到 checkin_base_points（默认 5~5 = 固定 5 分，旧行为不变）
    - 连签加成：streak_bonus * (streak-1)，封顶 streak_max_bonus
    - 惩罚：penalty_pct% 概率扣 penalty_min ~ penalty_max（默认 0 = 关闭，旧行为不变）
    - 积分仅发放给公益服用户（is_welfare=True）；其他用户签到只记录连签
    """
    base_points = _get_int_config(db, "checkin_base_points", 5)
    base_min = _get_int_config(db, "checkin_base_min", base_points)
    base_max = _get_int_config(db, "checkin_base_max", base_points)
    if base_min > base_max:
        base_min, base_max = base_max, base_min
    penalty_min = _get_int_config(db, "checkin_penalty_min", 1)
    penalty_max = _get_int_config(db, "checkin_penalty_max", 3)
    if penalty_min > penalty_max:
        penalty_min, penalty_max = penalty_max, penalty_min
    return {
        "enabled": _get_bool_config(db, "checkin_enabled", True),
        "base_points": base_points,
        "base_min": base_min,
        "base_max": base_max,
        "streak_bonus": _get_int_config(db, "checkin_streak_bonus", 2),
        "streak_max_bonus": _get_int_config(db, "checkin_streak_max_bonus", 10),
        "penalty_pct": _get_int_config(db, "checkin_penalty_pct", 0),
        "penalty_min": penalty_min,
        "penalty_max": penalty_max,
    }


class CheckinStatusResponse(BaseModel):
    enabled: bool
    checked_today: bool
    streak: int
    base_points: int
    streak_bonus: int
    streak_max_bonus: int
    points: int
    # 2026-10-09 合并公益服签到后新增（带默认值，保持旧客户端兼容）
    base_min: int = 5
    base_max: int = 5
    penalty_pct: int = 0
    points_enabled: bool = True  # 当前用户是否为公益服（是否会获得积分）


@router.get("/checkin/status", response_model=CheckinStatusResponse)
def checkin_status(
    current_user: models.WebUser = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    today = _today_start()
    checked = db.query(models.CheckinRecord).filter(
        models.CheckinRecord.user_id == current_user.id,
        models.CheckinRecord.checkin_date >= today,
    ).first() is not None

    # 连签数：今日已签按当日记录，未签则按昨天回溯
    anchor = today
    if not checked:
        anchor = today - timedelta(days=1)
    last = db.query(models.CheckinRecord).filter(
        models.CheckinRecord.user_id == current_user.id,
        models.CheckinRecord.checkin_date <= anchor,
    ).order_by(models.CheckinRecord.checkin_date.desc()).first()
    streak = last.streak if last else 0

    rules = _checkin_rules(db)
    return CheckinStatusResponse(
        enabled=rules["enabled"],
        checked_today=checked,
        streak=streak,
        base_points=rules["base_points"],
        streak_bonus=rules["streak_bonus"],
        streak_max_bonus=rules["streak_max_bonus"],
        points=current_user.points or 0,
        base_min=rules["base_min"],
        base_max=rules["base_max"],
        penalty_pct=rules["penalty_pct"],
        points_enabled=bool(getattr(current_user, "is_welfare", False)),
    )


def _do_checkin_core(db: Session, user: models.WebUser) -> dict:
    """签到核心逻辑（同步，在线程池中执行）

    2026-10-09 合并公益服签到后的统一逻辑：
    - 积分仅发放给公益服用户（is_welfare=True）；其他用户签到只记录连签，积分为 0
    - 公益服用户：基础积分 base_min ~ base_max 随机 + 连签加成 - 惩罚（按概率）

    唯一索引 (user_id, checkin_date) 是并发防重的最后一道门：先 flush 让冲突在提交前
    暴露，避免「先发积分、后落库失败」或直接 500。

    返回 {"points_awarded", "streak", "balance", "penalty"}，失败时抛 HTTPException。
    供 POST /api/user/economy/checkin 路由和已废弃的 POST /api/points/signin 复用。
    """
    user_id = user.id
    rules = _checkin_rules(db)
    if not rules["enabled"]:
        raise HTTPException(status_code=403, detail="签到功能未开启")

    today = _today_start()
    exists = db.query(models.CheckinRecord).filter(
        models.CheckinRecord.user_id == user_id,
        models.CheckinRecord.checkin_date >= today,
    ).first()
    if exists:
        raise HTTPException(status_code=400, detail="今天已经签到过啦")

    # 连签：昨天有记录则 +1，否则重置为 1
    yesterday_record = db.query(models.CheckinRecord).filter(
        models.CheckinRecord.user_id == user_id,
        models.CheckinRecord.checkin_date >= today - timedelta(days=1),
        models.CheckinRecord.checkin_date < today,
    ).order_by(models.CheckinRecord.checkin_date.desc()).first()
    streak = (yesterday_record.streak + 1) if yesterday_record else 1

    # 奖励计算：仅公益服用户获得积分
    is_welfare = bool(getattr(user, "is_welfare", False))
    penalty = False
    if not is_welfare:
        reward = 0
    else:
        base = random.randint(rules["base_min"], rules["base_max"])
        bonus = min(rules["streak_bonus"] * (streak - 1), rules["streak_max_bonus"])
        penalty_amount = 0
        if rules["penalty_pct"] > 0 and random.random() * 100 < rules["penalty_pct"]:
            penalty_amount = random.randint(rules["penalty_min"], rules["penalty_max"])
            penalty = True
        # 惩罚只削减当日奖励，不倒扣余额：base+bonus-penalty 可能为负（如 base=1、penalty=3），
        # 负数会经 _add_points 直接扣用户积分（可扣成负余额），且前端/通知文案都不支持负奖励
        reward = max(0, base + bonus - penalty_amount)

    record = models.CheckinRecord(
        user_id=user_id,
        checkin_date=today,
        points_awarded=reward,
        streak=streak,
    )
    db.add(record)
    try:
        db.flush()
    except IntegrityError:
        db.rollback()
        raise HTTPException(status_code=400, detail="今天已经签到过啦")
    except OperationalError:
        # 数据库写锁竞争：没发奖也没落库，让客户端重试
        db.rollback()
        raise HTTPException(status_code=409, detail="签到请求冲突，请稍后重试")
    if reward:
        description = f"每日签到（连续 {streak} 天）"
    else:
        description = f"每日签到（连续 {streak} 天，无积分奖励）"
    balance = _add_points(
        db, user, reward, "checkin",
        description, f"checkin:{today.strftime('%Y%m%d')}",
    )
    # C1 活力值：公益服用户签到恢复活力（受每日免费获取上限钳制）
    vitality_gained = 0
    if is_welfare:
        try:
            cfg = _vitality.get_vitality_config(db)
            if cfg["enabled"]:
                limit = int(cfg.get("daily_gain_limit", 0) or 0)
                gained = _vitality.get_today_free_gain(db, user_id) if limit > 0 else 0
                if limit <= 0 or gained < limit:
                    vitality_gained = _vitality._add_vitality(db, user_id, 1, "checkin")
        except Exception:
            logger.exception("checkin vitality restore failed for user %s", user_id)
    db.commit()
    return {"points_awarded": reward, "streak": streak, "balance": balance,
            "penalty": penalty, "vitality_gained": vitality_gained}


@router.post("/checkin")
async def do_checkin(
    request: Request,
    current_user: models.WebUser = Depends(get_current_user),
    db: Session = Depends(get_db),
    _tg: models.WebUser = Depends(require_tg_bound)
):
    """每日签到：基础积分 + 连签加成（封顶）；积分仅限公益服用户"""
    user_id = current_user.id

    allowed, _ = check_rate_limit(f"checkin:{user_id}", 5, 60)
    if not allowed:
        raise HTTPException(status_code=429, detail="操作过于频繁，请稍后再试")

    award = await run_in_threadpool(_do_checkin_core, db, current_user)

    if award["points_awarded"] > 0:
        title = f"📅 签到成功 +{award['points_awarded']} 积分"
        content = (f"已连续签到 {award['streak']} 天，当前余额 {award['balance']} 积分。"
                   f"\n明日再来看看，连签奖励更高！")
        message = f"签到成功，+{award['points_awarded']} 积分（连续 {award['streak']} 天）"
    else:
        title = "📅 签到成功"
        content = (f"已连续签到 {award['streak']} 天。积分奖励仅限公益服用户。")
        message = f"签到成功（连续 {award['streak']} 天，积分仅限公益服用户）"
    await notify_admin_event(
        event_type="economy.checkin",
        user_id=user_id,
        title=title,
        content=content,
    )
    return {
        "success": True,
        "points_awarded": award["points_awarded"],
        "streak": award["streak"],
        "balance": award["balance"],
        "vitality_gained": award.get("vitality_gained", 0),
        "message": message,
    }


# ==================== 活力值 ====================


@router.get("/vitality")
def get_vitality(
    current_user: models.WebUser = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """查询我的活力值状态（仅公益服用户）"""
    if not getattr(current_user, "is_welfare", False):
        raise HTTPException(status_code=403, detail="活力值仅限公益服用户")
    return {"success": True, **_vitality.vitality_status(db, current_user)}


class VitalityRechargeRequest(BaseModel):
    points: int = Field(..., gt=0, description="要消耗的积分数")


@router.post("/vitality/recharge")
async def recharge_vitality(
    request: Request,
    payload: VitalityRechargeRequest,
    current_user: models.WebUser = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """用积分续活力值：1 点活力 = vitality_point_cost 积分（可配置）"""
    if not getattr(current_user, "is_welfare", False):
        raise HTTPException(status_code=403, detail="活力值仅限公益服用户")
    allowed, _ = check_rate_limit(f"vitality_recharge:{current_user.id}", 5, 60)
    if not allowed:
        raise HTTPException(status_code=429, detail="操作过于频繁，请稍后再试")
    cfg = _vitality.get_vitality_config(db)
    if not cfg["enabled"]:
        raise HTTPException(status_code=403, detail="活力值功能未开启")
    cost = cfg["point_cost"]
    if payload.points % cost != 0:
        raise HTTPException(status_code=400, detail=f"积分数必须是 {cost} 的整数倍")
    want = payload.points // cost
    st = _vitality.vitality_status(db, current_user)
    room = st["max"] - st["vitality"]
    if room <= 0:
        raise HTTPException(status_code=400, detail="活力值已满，无需续")
    requested = want  # 外层空间检查只用于快速失败；真正的空间在事务内锁行后重算

    def _do() -> dict:
        # 锁用户行后重读活力值算剩余空间：外层读的是旧值，并发两笔续费都按同一空间扣分，
        # 活力被 _add_vitality 截断在上限 → 多扣的积分白花（超付）。
        lock_user_row(db, current_user.id)
        current_v = int(
            db.query(models.WebUser.vitality).filter(models.WebUser.id == current_user.id).scalar() or 0
        )
        room_now = st["max"] - current_v
        if room_now <= 0:
            db.rollback()
            raise HTTPException(status_code=400, detail="活力值已满，无需续")
        gain = min(requested, room_now)
        spend = gain * cost
        try:
            balance = _spend_points(db, current_user, spend, "vitality_recharge",
                                    f"积分续活力 +{gain}", f"vitality:{gain}")
        except InsufficientPoints:
            db.rollback()
            raise HTTPException(status_code=400, detail="积分不足")
        new_v = _vitality._add_vitality(db, current_user.id, gain, "recharge")
        db.commit()
        return {"vitality_gained": gain, "points_spent": spend,
                "vitality": new_v, "points_balance": balance}

    return {"success": True, **await run_in_threadpool(_do)}


# ==================== 积分流水 ====================

@router.get("/points/log")
def points_log(
    limit: int = 50,
    offset: int = 0,
    type_filter: Optional[str] = None,
    current_user: models.WebUser = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    query = db.query(models.PointsLog).filter(
        models.PointsLog.user_id == current_user.id
    )
    if type_filter:
        query = query.filter(models.PointsLog.type == type_filter)

    total = query.count()
    logs = query.order_by(models.PointsLog.created_at.desc()).offset(offset).limit(min(limit, 200)).all()
    return {
        "total": total,
        "balance": current_user.points or 0,
        "logs": [
            {
                "id": l.id,
                "amount": l.amount,
                "balance_after": l.balance_after,
                "type": l.type,
                "description": l.description,
                "ref_id": l.ref_id,
                "created_at": l.created_at.isoformat() if l.created_at else None,
            }
            for l in logs
        ],
    }


# ==================== 兑换码 ====================

def _grant_subscription(db: Session, user: models.WebUser, plan: models.SubscriptionPlan,
                        days: int, source: str, ref_id: str,
                        realm_id: int | None = None) -> models.UserSubscription:
    """发放订阅：**同一个服**已有有效订阅则顺延，否则新建

    订阅是一个服一个的：甲服的兑换码只能顺延甲服的会员，不能给乙服的会员加天数。

    `with_for_update()` 在 PostgreSQL 下是真正的行锁，避免同一用户并发发放
    （同时核销两张码 / 码与支付回调撞车）时两边都读到同一到期时间、互相覆盖；
    SQLite 会忽略它，但 SQLite 写入本身是库级串行的。
    """
    now = datetime.now()
    target_realm = realm_id if realm_id is not None else plan.realm_id
    query = db.query(models.UserSubscription).filter(
        models.UserSubscription.user_id == user.id,
        models.UserSubscription.status == "active",
        models.UserSubscription.end_date > now,
    )
    if target_realm is not None:
        query = query.filter(models.UserSubscription.realm_id == target_realm)
    active = query.order_by(models.UserSubscription.end_date.desc()).with_for_update().first()

    if active:
        active.end_date = active.end_date + timedelta(days=days)
        active.updated_at = now
        subscription = active
    else:
        subscription = models.UserSubscription(
            user_id=user.id,
            plan_id=plan.id,
            realm_id=target_realm if target_realm is not None else realms.active_realm_id(db),
            start_date=now,
            end_date=now + timedelta(days=days),
            status="active",
        )
        db.add(subscription)
        db.flush()
    return subscription


@router.get("/exchange/config")
async def exchange_config(db: Session = Depends(get_db)):
    return {
        "enabled": _get_bool_config(db, "exchange_enabled", True),
        "invitee_note": "被邀请注册的奖励自动发放",
    }


def _best_discount_credit(db: Session, user_id: int) -> Optional[models.ExchangeDiscountCredit]:
    """用户最优的一张未用折扣权益（pct 最小=折扣最大），没有返回 None"""
    now = datetime.now()
    return (
        db.query(models.ExchangeDiscountCredit)
        .filter(
            models.ExchangeDiscountCredit.user_id == user_id,
            models.ExchangeDiscountCredit.status == "unused",
            or_(
                models.ExchangeDiscountCredit.expires_at.is_(None),
                models.ExchangeDiscountCredit.expires_at > now,
            ),
        )
        .order_by(models.ExchangeDiscountCredit.discount_pct.asc())
        .first()
    )


def _grant_discount_credit(db: Session, user_id: int, code: models.ExchangeCode) -> models.ExchangeDiscountCredit:
    """核销 discount 型兑换码：发一张折扣权益。调用方已做占位和并发保护；pct 合法性由调用方校验。"""
    credit = models.ExchangeDiscountCredit(
        user_id=user_id,
        exchange_code_id=code.id,
        discount_pct=code.discount_pct,
        expires_at=code.expires_at,
    )
    db.add(credit)
    return credit


def _exchange_discount_amount(list_price: Decimal, pct: int) -> tuple[Decimal, Decimal]:
    """兑换码折扣金额口径：与 coupons.compute 的 percent 一致（pct=实付百分比，85=八五折）。
    返回 (优惠额, 实付额)，两位小数。"""
    from backend.coupons import _money
    pct = max(1, min(99, int(pct or 0)))
    discount = _money(list_price * (Decimal("100") - Decimal(str(pct))) / Decimal("100"))
    if discount > list_price:
        discount = list_price
    paid = _money(list_price - discount)
    return discount, paid


class RedeemRequest(BaseModel):
    code: str = Field(..., min_length=4, max_length=32)


def _redeem_exchange_core(db: Session, user: models.WebUser, code_str: str) -> dict:
    """兑换码核销核心（同步，成功结尾 db.commit()；失败抛 HTTPException，detail 为用户可读文案）。

    HTTP 路由 POST /api/user/economy/exchange/redeem 与 TG Bot /redeem 命令共用同一实现
    （"横切能力只许一套"：不要复制第二套核销逻辑）。
    """
    user_id = user.id
    if not _get_bool_config(db, "exchange_enabled", True):
        raise HTTPException(status_code=403, detail="兑换功能未开启")

    code_str = code_str.strip().upper()
    code = db.query(models.ExchangeCode).filter(
        models.ExchangeCode.code == code_str
    ).first()

    now = datetime.now()
    if code is None or not code.is_active or (code.expires_at and code.expires_at < now):
        raise HTTPException(status_code=400, detail="兑换码无效或已过期")

    # H5：同一个账号同一张码最多兑换一次（max_uses 是总次数，不是每人次数）。
    # 先查（含升级前 used_by 里的历史），再在本事务里写核销记录：唯一约束兜住并发重复提交。
    from backend import codes as code_lib

    if code_lib.user_already_redeemed(db, code_lib.REDEMPTION_KIND_EXCHANGE, code, user_id):
        raise HTTPException(status_code=400, detail="你已经兑换过这个兑换码（每个账号限一次）")

    # 先原子占位再去发奖：只有仍可用的兑换码才会被 +1，并发下第二个请求 rowcount=0，
    # 因此不会出现「同一张单次码被同时核销两次、发两份奖励」。
    try:
        if not code_lib.record_redemption(
            db, code_lib.REDEMPTION_KIND_EXCHANGE, code.id, user_id
        ):
            raise HTTPException(status_code=400, detail="你已经兑换过这个兑换码（每个账号限一次）")
        claimed = (
            db.query(models.ExchangeCode)
            .filter(
                models.ExchangeCode.id == code.id,
                models.ExchangeCode.is_active.is_(True),
                or_(
                    models.ExchangeCode.expires_at.is_(None),
                    models.ExchangeCode.expires_at > now,
                ),
                or_(
                    models.ExchangeCode.max_uses.is_(None),
                    models.ExchangeCode.use_count < models.ExchangeCode.max_uses,
                ),
            )
            .update(
                {models.ExchangeCode.use_count: func.coalesce(models.ExchangeCode.use_count, 0) + 1},
                synchronize_session=False,
            )
        )
    except OperationalError:
        # SQLite 下读写事务升级失败（并发写入）：没发奖也没占位，让客户端重试
        db.rollback()
        raise HTTPException(status_code=409, detail="兑换码正在核销中，请稍后重试")
    if not claimed:
        db.rollback()
        raise HTTPException(status_code=400, detail="兑换码已用尽或已过期")

    # 占位成功后才读回详情：use_count 是 SQL 级自增，身份映射里的旧实例要 refresh，
    # 否则下面判断「用满停用」会拿到过期的 0
    db.refresh(code)

    result: dict = {"success": True}
    if code.type == "points":
        balance = _add_points(
            db, user, code.points_value, "exchange",
            f"兑换码 {code_str}", f"exchange:{code_str}",
        )
        result.update(reward_type="points", points=code.points_value, balance=balance,
                      message=f"兑换成功，+{code.points_value} 积分")
    elif code.type == "subscription":
        plan = db.query(models.SubscriptionPlan).filter(
            models.SubscriptionPlan.id == code.plan_id
        ).first() if code.plan_id else None
        if not plan:
            # 已原子占位但无法履约：回滚，别白白吃掉一次使用次数
            db.rollback()
            raise HTTPException(status_code=400, detail="兑换码关联套餐不存在")
        subscription = _grant_subscription(
            db, user, plan, code.duration_days, "exchange", code_str,
            # 兑换码按它自己所属的服发会员（未标注时回退到套餐的服）
            realm_id=getattr(code, "realm_id", None) or plan.realm_id,
        )
        result.update(reward_type="subscription", plan_name=plan.name,
                      days=code.duration_days,
                      end_date=subscription.end_date.isoformat(),
                      message=f"兑换成功，「{plan.name}」× {code.duration_days} 天")
    elif code.type == "discount":
        if not (1 <= (code.discount_pct or 0) <= 99):
            db.rollback()
            raise HTTPException(status_code=400, detail="该折扣码配置无效")
        _grant_discount_credit(db, user_id, code)
        result.update(reward_type="discount", discount_pct=code.discount_pct,
                      message=f"兑换成功，获得订阅 {code_lib.discount_pct_text(code.discount_pct)} 优惠，下次购买订阅自动抵扣")
    else:
        db.rollback()
        raise HTTPException(status_code=400, detail="兑换码类型不支持")

    # 核销审计（use_count 已在上面原子 +1，这里只补使用者并处理用满停用）
    used = [i for i in str(code.used_by or "").split(",") if i.strip()]
    if str(user_id) not in used:
        used.append(str(user_id))
    code.used_by = ",".join(used)[:500]
    if code.max_uses and (code.use_count or 0) >= code.max_uses:
        code.is_active = False
    db.commit()
    return result



@router.post("/exchange/redeem")
async def redeem_exchange_code(
    request: Request,
    req: RedeemRequest,
    current_user: models.WebUser = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """兑换码核销：points 型发积分；subscription 型发订阅；discount 型发一张订阅折扣权益

    占位 / 发奖 / 审计整段是同步 SQLAlchemy（体内一个 await 都没有），下放线程池执行；
    通知要走 WebSocket，必须留在事件循环上 await。
    """
    user_id = current_user.id
    allowed, _ = check_rate_limit(f"redeem:{user_id}", 10, 60)
    if not allowed:
        raise HTTPException(status_code=429, detail="操作过于频繁，请稍后再试")

    result = await run_in_threadpool(_redeem_exchange_core, db, current_user, req.code)

    await notify_admin_event(
        event_type="economy.redeem",
        user_id=user_id,
        title="🎁 兑换成功",
        content=result["message"],
    )
    return result


@router.get("/exchange/discount-credit")
def my_discount_credit(
    current_user: models.WebUser = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """我未使用的兑换码折扣权益（最优一张），前端在订阅购买区展示"""
    credit = _best_discount_credit(db, current_user.id)
    if credit is None:
        return {"has_credit": False}
    return {
        "has_credit": True,
        "discount_pct": credit.discount_pct,
        "expires_at": credit.expires_at.isoformat() if credit.expires_at else None,
    }


# ==================== 支付下单（易支付兼容网关） ====================

class PaymentMethod(BaseModel):
    id: str
    name: str
    enabled: bool


@router.get("/payment/methods", response_model=list[PaymentMethod])
async def payment_methods(db: Session = Depends(get_db)):
    methods = [
        PaymentMethod(id="alipay", name="支付宝", enabled=True),
        PaymentMethod(id="wxpay", name="微信支付", enabled=True),
    ]
    if _get_config(db, "payment_qqpay_enabled", "false").lower() == "true":
        methods.append(PaymentMethod(id="qqpay", name="QQ 钱包", enabled=True))
    return methods


@router.get("/payment/packages")
def payment_packages(db: Session = Depends(get_db)):
    """充值套餐（积分）"""
    if not _get_bool_config(db, "recharge_enabled", True):
        return {"enabled": False, "packages": []}
    packages = db.query(models.RechargePackage).filter(
        models.RechargePackage.is_active == True  # noqa: E712
    ).order_by(models.RechargePackage.sort_order).all()
    return {
        "enabled": True,
        "packages": [
            {
                "id": p.id, "name": p.name, "amount": p.amount,
                "bonus": p.bonus, "price": float(p.price),
                "is_popular": p.is_popular, "icon": p.icon,
                "total_points": p.amount + p.bonus,
            }
            for p in packages
        ],
    }


@router.get("/payment/plans")
def payment_plans(realm_id: Optional[int] = None, db: Session = Depends(get_db)):
    """订阅套餐（购买订阅）

    套餐是一个服一个的：默认只展示**当前服**的可购套餐（``realm_id=0`` 看全部服）。
    用户买哪一份，会员就开在哪个服。

    同时下发该服的**接入方式**：公益服（``is_free``）不需要卖会员，用户端的钱包页
    据此把「开通会员」换成「公益服 · 免费开放」的说明，不再推付费引导。
    """
    if not _get_bool_config(db, "subscription_purchase_enabled", True):
        return {"enabled": False, "plans": []}
    scope_id = None if realm_id == 0 else (realm_id or realms.active_realm_id(db))
    query = realms.scope(db.query(models.SubscriptionPlan),
                         models.SubscriptionPlan.realm_id, scope_id)
    plans = query.filter(
        models.SubscriptionPlan.is_active == True  # noqa: E712
    ).order_by(models.SubscriptionPlan.sort_order).all()
    free = realms.is_free_realm(db, scope_id) if scope_id else False
    return {
        "enabled": True,
        "realm_id": scope_id,
        "access_mode": "free" if free else "paid",
        "is_free": free,
        "access_note": realms.access_note_of(db, scope_id) if scope_id else "",
        "plans": [
            {
                "id": p.id, "name": p.name, "description": p.description,
                "price": float(p.price), "duration_days": p.duration_days,
                "features": p.features, "is_popular": p.is_popular,
                "realm_id": p.realm_id,
                "realm_name": (p.realm.name if p.realm else ""),
                "points_price": (float(p.points_price) if p.points_price is not None else None),
            }
            for p in plans
        ],
    }


@router.get("/payment/coupon/config")
def coupon_config(db: Session = Depends(get_db)):
    """优惠券开关：关闭时用户端不展示优惠码输入框，避免填了才报错"""
    return {"enabled": coupons.enabled(db)}


@router.get("/currency")
def currency_info(db: Session = Depends(get_db)):
    """货币体系公开信息（P2/C4）：名称、充值比例、快捷金额——用户端自定义充值换算用"""
    return {
        "name": "积分",
        "recharge_ratio": _get_float_config(db, "recharge_ratio", 1.2),
        "quick_amounts": _get_quick_amounts(db),
    }


class CouponQuoteRequest(BaseModel):
    code: str = Field(..., description="用户填写的优惠码")
    kind: str = Field(..., description="recharge / subscription")
    item_id: int = Field(..., description="充值套餐ID 或 套餐ID")


@router.post("/payment/coupon/quote")
def coupon_quote(
    req: CouponQuoteRequest,
    current_user: models.WebUser = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """优惠码试算（下单前预览）

    用 POST 而不是 GET：优惠码不该进访问日志/浏览器历史（与卡码预检同一考虑）。
    下单时走的是同一套口径（先会员折扣→再优惠券），不会出现「预览 8 折、实付全价」。
    返回里带 member_discount_pct/amount，前端可算出最终实付。
    """
    allowed, _ = check_rate_limit(f"coupon-quote:{current_user.id}", 30, 60)
    if not allowed:
        raise HTTPException(status_code=429, detail="操作过于频繁，请稍后再试")
    result = coupons.quote(db, user=current_user, code=req.code,
                           kind=req.kind, item_id=req.item_id)
    # 会员等级折扣（仅订阅+仅付费服），与 create_payment_order 同一口径：
    # 先会员折扣得中间价，再对中间价算优惠券。预览与下单必须一致。
    member_discount_pct = 0
    member_discount_amount = Decimal("0.00")
    if req.kind == "subscription":
        from backend import member_level as _ml
        plan = db.query(models.SubscriptionPlan).options(
            joinedload(models.SubscriptionPlan.realm)
        ).filter(models.SubscriptionPlan.id == req.item_id).first()
        _realm = plan.realm if plan else None
        _is_paid = not (_realm is not None
                        and getattr(_realm, "access_mode", "paid") == "free")
        if _is_paid:
            member_discount_pct = _ml.get_user_discount_pct(db, current_user)
            if member_discount_pct > 0:
                _list = Decimal(str(result.get("list_price", 0)))
                member_discount_amount = (
                    _list * Decimal(member_discount_pct) / Decimal(100)
                ).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
                # 对中间价重算优惠券（与下单逻辑一致）
                coupon_obj = db.query(models.CouponCode).filter(
                    models.CouponCode.id == result["coupon_id"]).first()
                if coupon_obj is not None:
                    _base = _list - member_discount_amount
                    c_discount, c_paid = coupons.compute(_base, coupon_obj)
                    result["discount_amount"] = float(member_discount_amount + c_discount)
                    result["paid_amount"] = float(c_paid)
    result["member_discount_pct"] = member_discount_pct
    result["member_discount_amount"] = float(member_discount_amount)
    return result


class CreateOrderRequest(BaseModel):
    kind: str = Field(..., description="recharge=充值积分 / subscription=购买订阅")
    item_id: Optional[int] = Field(default=None, description="充值套餐ID 或 套餐ID；自定义金额充值时可为空")
    # P3 修复（审查）：payment_method 无白名单，任意字符串进网关。限定可选值。
    payment_method: str = Field(default="alipay", pattern="^(alipay|wxpay|qqpay)$")
    coupon_code: str = Field(default="", description="优惠码（可空）；下单时占额度，付款转已用，关单/退款自动还回")
    custom_amount: Optional[float] = Field(default=None, ge=1, le=100000, description="自定义充值金额（元）；仅 kind=recharge 时有效，与 item_id 二选一")
    pay_with_points: bool = Field(default=False, description="积分支付；仅 kind=subscription 时有效")


def _yipay_sign(params: dict, key: str) -> str:
    """易支付 MD5 签名：按 key ASCII 升序拼接 a=b&...&<key>"""
    filtered = {k: v for k, v in params.items()
                if v not in (None, "") and k != "sign" and k != "sign_type"}
    qs = urlencode(sorted(filtered.items()))
    return hashlib.md5(f"{qs}{key}".encode("utf-8")).hexdigest()


@router.post("/payment/order")
def create_payment_order(
    request: Request,
    req: CreateOrderRequest,
    current_user: models.WebUser = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """创建支付订单，返回易支付跳转 URL（epay 标准提交）"""
    allowed, _ = check_rate_limit(f"order:{current_user.id}", 10, 60)
    if not allowed:
        raise HTTPException(status_code=429, detail="下单过于频繁，请稍后再试")

    # P2 修复（审查）：用户可无限创建 pending 订单（10/min = 600/小时），
    # 每单都 coupons.reserve 占额度，可耗尽一张券的 max_uses。限制每用户
    # pending 订单数（默认 5，可配）。
    try:
        max_pending = int(_get_config(db, "max_pending_orders_per_user", "5") or 5)
    except ValueError:
        max_pending = 5
    if max_pending > 0:
        pending = (
            db.query(models.RechargeOrder).filter(
                models.RechargeOrder.user_id == current_user.id,
                models.RechargeOrder.status == "pending",
            ).count()
            + db.query(models.SubscriptionOrder).filter(
                models.SubscriptionOrder.user_id == current_user.id,
                models.SubscriptionOrder.status == "pending",
            ).count()
        )
        if pending >= max_pending:
            raise HTTPException(
                status_code=429,
                detail=f"待支付订单过多（{pending} 笔），请先完成或取消后再下单",
            )

    gateway = _get_config(db, "payment_gateway_url", "").strip().rstrip("/")
    pid = _get_config(db, "payment_partner_id", "").strip()
    key = _get_config(db, "payment_partner_key", "").strip()
    if not gateway or not pid or not key:
        raise HTTPException(status_code=503, detail="支付通道未配置，请联系管理员")

    site_url = _get_config(db, "site_url", "").strip().rstrip("/")
    notify_url = f"{site_url}/api/user/economy/payment/notify"
    return_url = f"{site_url}/wallet?paid=1"

    order_id = f"RB{int(time.time() * 1000)}{uuid.uuid4().hex[:6].upper()}"

    if req.kind == "recharge":
        if not _get_bool_config(db, "recharge_enabled", True):
            raise HTTPException(status_code=403, detail="充值功能未开启")
        if req.custom_amount is not None:
            if req.custom_amount < 1 or req.custom_amount > 100000:
                raise HTTPException(status_code=400, detail="自定义金额需在 1~100000 元之间")
            ratio = _get_float_config(db, "recharge_ratio", 1.2)
            if ratio <= 0:
                ratio = 1.2
            points = int(Decimal(str(req.custom_amount)) * Decimal(str(ratio)))
            if points < 1:
                raise HTTPException(status_code=400, detail="金额过小，无法兑换积分")
            item_name = f"积分充值 - 自定义 {req.custom_amount:.2f} 元"
            amount = Decimal(str(req.custom_amount))
            order = models.RechargeOrder(
                order_id=order_id, user_id=current_user.id,
                package_id=0, amount=points, price=amount, payment_method=req.payment_method, status="pending",
            )
        else:
            package = db.query(models.RechargePackage).filter(
                models.RechargePackage.id == req.item_id,
                models.RechargePackage.is_active == True,  # noqa: E712
            ).first()
            if not package:
                raise HTTPException(status_code=404, detail="充值套餐不存在")
            item_name = f"积分充值 - {package.name}"
            amount = package.price
            order = models.RechargeOrder(
                order_id=order_id, user_id=current_user.id,
                package_id=package.id, amount=package.amount + package.bonus,
                price=amount, payment_method=req.payment_method, status="pending",
            )
    elif req.kind == "subscription":
        if not _get_bool_config(db, "subscription_purchase_enabled", True):
            raise HTTPException(status_code=403, detail="订阅购买未开启")
        plan = db.query(models.SubscriptionPlan).options(
            joinedload(models.SubscriptionPlan.realm)
        ).filter(
            models.SubscriptionPlan.id == req.item_id,
            models.SubscriptionPlan.is_active == True,  # noqa: E712
        ).first()
        if not plan:
            raise HTTPException(status_code=404, detail="套餐不存在")
        if req.pay_with_points:
            if plan.points_price is None:
                raise HTTPException(status_code=400, detail="该套餐不支持积分购买")
            points_cost = int(plan.points_price)
            if points_cost <= 0:
                raise HTTPException(status_code=400, detail="套餐积分价配置错误")
            balance = db.query(models.WebUser.points).filter(models.WebUser.id == current_user.id).scalar() or 0
            if balance < points_cost:
                raise HTTPException(status_code=400, detail=f"积分不足（需要 {points_cost}，当前 {int(balance)}）")
            # 原子条件扣减：上面的余额预检只用于给出友好提示，真正的够不够由 UPDATE 判定，
            # 防并发双下单把余额扣成负数（双花）
            try:
                _spend_points(db, current_user, points_cost, "subscription_buy",
                              f"积分购买订阅 - {plan.name}", f"subscription_points:{order_id}")
            except InsufficientPoints:
                db.rollback()
                raise HTTPException(status_code=400, detail=f"积分不足（需要 {points_cost}）")
            sub = _grant_subscription(db, current_user, plan, plan.duration_days, "points_purchase", f"subscription_points:{order_id}")
            order = models.SubscriptionOrder(
                order_id=order_id, user_id=current_user.id,
                plan_id=plan.id, item_name=plan.name,
                amount=plan.price, payment_method="points", status="paid",
                paid_at=datetime.now(),
                subscription_id=sub.id, days_granted=plan.duration_days,
            )
            db.add(order)
            db.commit()
            return {
                "success": True,
                "order_id": order_id,
                "paid_with_points": True,
                "points_cost": points_cost,
                "message": "积分支付成功，订阅已开通",
            }
        item_name = f"订阅购买 - {plan.name}"
        amount = plan.price
        order = models.SubscriptionOrder(
            order_id=order_id, user_id=current_user.id,
            plan_id=plan.id, item_name=plan.name,
            amount=amount, payment_method=req.payment_method, status="pending",
        )
    else:
        raise HTTPException(status_code=400, detail="kind 必须是 recharge 或 subscription")

    # 会员等级折扣：仅订阅 + 仅付费服。公益服（access_mode='free'）不打折，充值不打折。
    # 叠加顺序：先等级折扣得中间价，再对中间价算优惠券。
    list_price = Decimal(str(amount))
    member_discount_pct = 0
    member_discount_amount = Decimal("0.00")
    if req.kind == "subscription":
        from backend import member_level as _ml
        # plan.realm 已用 joinedload 预加载；realm 为空（老数据）按付费服处理
        _realm = plan.realm
        _is_paid_plan = not (_realm is not None
                             and getattr(_realm, "access_mode", "paid") == "free")
        if _is_paid_plan:
            member_discount_pct = _ml.get_user_discount_pct(db, current_user)
            if member_discount_pct > 0:
                member_discount_amount = (
                    list_price * Decimal(member_discount_pct) / Decimal(100)
                ).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    coupon_base = list_price - member_discount_amount

    # 优惠券：先用 quote 校验券有效性（按原价校验门槛），再对中间价重算折扣
    discount_amount = member_discount_amount
    paid_amount = coupon_base
    preview = None
    coupon_code = (req.coupon_code or "").strip()
    credit = None
    # P2：自定义金额充值暂不支持优惠券（无套餐可供优惠券校验）；积分支付已直接返回，不会到这里
    is_custom_recharge = req.kind == "recharge" and req.custom_amount is not None
    if is_custom_recharge and coupon_code:
        # 自定义金额没有套餐可供优惠券校验（coupons.resolve_item 按套餐查），
        # 静默忽略会让用户「填了码却按全价付」还无从察觉；直接拒绝，让前端藏起输入框。
        raise HTTPException(status_code=400, detail="自定义金额充值暂不支持使用优惠券")
    if coupon_code and not is_custom_recharge:
        preview = coupons.quote(db, user=current_user, code=coupon_code,
                                kind=req.kind, item_id=req.item_id)
        coupon_obj = db.query(models.CouponCode).filter(
            models.CouponCode.id == preview["coupon_id"]).first()
        if coupon_obj is None:
            raise HTTPException(status_code=400, detail="优惠码不存在")
        c_discount, c_paid = coupons.compute(coupon_base, coupon_obj)
        discount_amount = member_discount_amount + c_discount
        paid_amount = c_paid
        coupon_code = preview["code"]
    elif req.kind == "subscription":
        # 兑换码折扣权益：没填优惠券时自动用最优的一张（pct 最小=折扣最大），不与优惠券叠加
        credit = _best_discount_credit(db, current_user.id)
        if credit is not None:
            discount_amount, paid_amount = _exchange_discount_amount(list_price, credit.discount_pct)

    # 订单上快照「原价 / 优惠 / 实付」，并让订单金额一律等于**实付**：
    # 对账、邀请返利比例、退款都以用户真付的钱为准，不能按原价算。
    order.list_price = list_price
    order.discount_amount = discount_amount
    if req.kind == "recharge":
        order.price = paid_amount
    else:
        order.amount = paid_amount

    db.add(order)
    if credit is not None:
        # 兑换码折扣权益：下单即核销（一张只用一次）。条件 UPDATE 防并发双下单抢同一张。
        claimed = (
            db.query(models.ExchangeDiscountCredit)
            .filter(
                models.ExchangeDiscountCredit.id == credit.id,
                models.ExchangeDiscountCredit.status == "unused",
            )
            .update(
                {"status": "used", "used_order_id": order_id},
                synchronize_session=False,
            )
        )
        if not claimed:
            db.rollback()
            raise HTTPException(status_code=409, detail="折扣权益已被使用，请刷新后重试")
    if preview is not None:
        coupon = db.query(models.CouponCode).filter(
            models.CouponCode.id == preview["coupon_id"]).first()
        if coupon is None:
            raise HTTPException(status_code=400, detail="优惠码不存在")
        # 占额度（条件 UPDATE，并发也超不了总限）；失败则整笔下单回滚
        # reserve 记录的是优惠券口径：list_price=中间价，discount=仅优惠券部分
        usage = coupons.reserve(db, coupon=coupon, user=current_user, order_id=order_id,
                                kind=req.kind, list_price=coupon_base,
                                discount=discount_amount - member_discount_amount,
                                paid=paid_amount)
        order.coupon_usage_id = usage.id
    db.commit()

    params = {
        "pid": pid,
        "type": req.payment_method,
        "out_trade_no": order_id,
        "notify_url": notify_url,
        "return_url": return_url,
        "name": item_name,
        "money": f"{float(paid_amount):.2f}",
    }
    params["sign"] = _yipay_sign(params, key)
    params["sign_type"] = "MD5"
    pay_url = f"{gateway}/submit.php?{urlencode(params)}"

    order.payment_url = pay_url
    db.commit()

    logger.info("创建支付订单: user=%s order=%s kind=%s amount=%s discount=%s",
                current_user.id, order_id, req.kind, paid_amount, discount_amount)
    return {
        "success": True,
        "order_id": order_id,
        "amount": float(paid_amount),
        "list_price": float(list_price),
        "discount_amount": float(discount_amount),
        "member_discount_pct": member_discount_pct,
        "member_discount_amount": float(member_discount_amount),
        "coupon_code": coupon_code,
        "exchange_discount_pct": credit.discount_pct if credit is not None else 0,
        "pay_url": pay_url,
        "message": "订单已创建，正在跳转支付",
    }


@router.get("/payment/orders")
def my_orders(
    kind: Optional[str] = None,
    limit: int = 30,
    current_user: models.WebUser = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """我的订单列表（充值 + 订阅合并）

    带优惠快照：原价 / 优惠金额 / 实付 / 用的哪个码。用户端要能自己对账
    （“我明明用了券，为什么订单显示全价”）。
    """
    orders: list[dict] = []

    if kind in (None, "recharge"):
        for o in db.query(models.RechargeOrder).filter(
            models.RechargeOrder.user_id == current_user.id
        ).order_by(models.RechargeOrder.created_at.desc()).limit(limit).all():
            orders.append({
                "order_id": o.order_id, "kind": "recharge",
                "item_name": f"积分充值（+{o.amount} 积分）",
                "amount": float(o.price), "status": o.status,
                "list_price": float(o.list_price or 0),
                "discount_amount": float(o.discount_amount or 0),
                "coupon_code": "",
                "coupon_usage_id": o.coupon_usage_id,
                "created_at": o.created_at.isoformat() if o.created_at else None,
                "paid_at": o.paid_at.isoformat() if o.paid_at else None,
                # 退款留痕：用户端要能看到“为什么退了/退了多少”，否则只会来问客服
                "refunded_at": o.refunded_at.isoformat() if o.refunded_at else None,
                "refund_reason": o.refund_reason or "",
            })

    if kind in (None, "subscription"):
        for o in db.query(models.SubscriptionOrder).filter(
            models.SubscriptionOrder.user_id == current_user.id
        ).order_by(models.SubscriptionOrder.created_at.desc()).limit(limit).all():
            orders.append({
                "order_id": o.order_id, "kind": "subscription",
                "item_name": f"订阅 - {o.item_name}",
                "amount": float(o.amount), "status": o.status,
                "list_price": float(o.list_price or 0),
                "discount_amount": float(o.discount_amount or 0),
                "coupon_code": "",
                "coupon_usage_id": o.coupon_usage_id,
                "created_at": o.created_at.isoformat() if o.created_at else None,
                "paid_at": o.paid_at.isoformat() if o.paid_at else None,
                "refunded_at": o.refunded_at.isoformat() if o.refunded_at else None,
                "refund_reason": o.refund_reason or "",
            })

    # 优惠码回填（一次查完，不给每条订单配一个查询）
    usage_ids = {o["coupon_usage_id"] for o in orders if o.get("coupon_usage_id")}
    if usage_ids:
        rows = db.query(models.CouponUsage.id, models.CouponCode.code).join(
            models.CouponCode, models.CouponCode.id == models.CouponUsage.coupon_id
        ).filter(models.CouponUsage.id.in_(usage_ids)).all()
        code_map = {uid: code for uid, code in rows}
        for o in orders:
            o["coupon_code"] = code_map.get(o["coupon_usage_id"], "")
    for o in orders:
        o.pop("coupon_usage_id", None)

    orders.sort(key=lambda x: x["created_at"] or "", reverse=True)
    return {"orders": orders[:limit]}


# ==================== 支付回调 ====================

def _verify_yipay_notify(params: dict, key: str) -> bool:
    """易支付回调验签（MD5）

    比较用 ``hmac.compare_digest``：签名是回调方给的字符串，逐字节短路比较
    会把「对了几位」变成可测量的时间差（v2.30.0）。
    """
    sign = str(params.get("sign") or "")
    expected = _yipay_sign(params, key)
    return hmac.compare_digest(sign.lower(), expected.lower())


def _order_paid_amount(recharge_order=None, subscription_order=None) -> Optional[Decimal]:
    """订单**应付金额**（实付口径）

    下单时把订单金额统一写成实付（原价 / 优惠 / 实付三个快照见 ``create_payment_order``），
    所以这里读的就是用户真正该付的钱。
    """
    order = recharge_order if recharge_order is not None else subscription_order
    if order is None:
        return None
    raw = order.price if recharge_order is not None else order.amount
    if raw is None:
        return None
    try:
        return Decimal(str(raw)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    except (InvalidOperation, ValueError):
        return None


def _notify_amount(value) -> Optional[Decimal]:
    """回调里报的金额（解析不了返回 None，不是 0）"""
    if value is None or str(value).strip() == "":
        return None
    try:
        return Decimal(str(value).strip()).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    except (InvalidOperation, ValueError):
        return None


def _amount_mismatch(recharge_order, subscription_order, raw: dict) -> Optional[str]:
    """回调金额与订单是否对不上（对不上时返回给人看的说明）

    验签只证明「这条通知来自网关」；它不证明**钱数对不对**。网关配错价、订单号被复用、
    上游按别的金额收款时，旧实现照样发货（积分 / 会员都按订单发货），而钱只收了一部分。

    所以回调里报了金额就逐笔核对：对不上 **不发货**（返回 fail，让网关重试并让运维看见）。
    回调没带金额时只能记一行日志按原逻辑发货——不能因为「读不到 money」把正常付款卡死。
    """
    expected = _order_paid_amount(recharge_order, subscription_order)
    reported = _notify_amount(raw.get("money"))
    if expected is None:
        return None
    if reported is None:
        logger.warning("支付回调未带金额，无法核对（按订单实付 %s 发货）: order=%s",
                       expected, raw.get("out_trade_no"))
        return None
    if reported != expected:
        return f"金额不符：订单应付 {expected}，回调报 {reported}"
    return None


def _claim_order(db: Session, model, order) -> bool:
    """把订单从「未支付」原子地推进到 ``paid``：并发回调 / 人工补单只有一个能拿到那一行

    旧实现的判定与发货之间是读-改-写（``if status not in (paid, refunded, closed)``）：
    网关重试回调和管理员补单撞在一起时，两个事务都读到 ``pending`` → 双倍积分 / 双份订阅。
    SQLite 的单写锁能部分兜住，跳机器部署的 PostgreSQL 下就是实打实的资损。

    现在先做条件 ``UPDATE ... WHERE status NOT IN (...)``：

    - 拿到 1 行 → 由本请求发货（并发第二个请求会在行锁上等到提交，然后拿到 0 行）；
    - 拿到 0 行 → 已被别人履约（或已退款/关单），跳过发货，幂等成立。

    更新与发货在同一事务里，失败会一起回滚；``paid_at`` 不再由调用方另行赋值。
    """
    if order is None or order.status in ("paid", "refunded", "closed"):
        return False
    claimed = (
        db.query(model)
        .filter(model.id == order.id, model.status.notin_(("paid", "refunded", "closed")))
        .update({"status": "paid", "paid_at": datetime.now()}, synchronize_session=False)
    )
    if not claimed:
        return False
    # 让当前事务里的 ORM 对象与刚写入的行保持一致（后续代码与提交都用它）
    order.status = "paid"
    order.paid_at = datetime.now()
    return True


class FulfillmentError(RuntimeError):
    """履约缺料：订单该发的东西已经不存在了（套餐被删 / 用户被删等）

    这类情况**必须**让调用方回滚并把回调判成失败（网关会重试，或者转人工补单），
    不能静默返回 success——「钱收了、订单标了 paid、用户什么都没拿到」是最坏的结果，
    而且优惠券还会被照常 ``consume`` 掉。
    """


def _fulfill_order(db: Session, recharge_order=None, subscription_order=None) -> list:
    """订单履约：充值发积分 / 订阅发放，幂等（重复回调不重复发货）

    **同步函数**（体内一个 await 都没有）：async 路由调用它时必须用
    ``await run_in_threadpool(...)`` 下放线程池，别把一次提交压在事件循环上。

    **返回待发送的通知列表，不要在这里就发出去。**

    履约事务（积分 / 订阅 / 订单状态）还没 ``commit`` 时，通知服务会另开一个 session 写站内信，
    在 SQLite 上这属于「读事务升级为写事务」，会被直接判为 ``database is locked``（不会等锁），
    结果是「充值成功」这类站内信被静默丢弃（只留一行日志），而钱已经收了。
    所以通知由调用方在 ``db.commit()`` 之后统一发：见 ``send_fulfill_notifications``。
    """
    # 已退款 / 已关闭的订单绝不再履约：支付回调可能晚到或重放（网关重试、管理员已经
    # 关单后又收到回调），只判 `!= "paid"` 会把退过的订单又发一遍货。
    pending: list = []
    if recharge_order and _claim_order(db, models.RechargeOrder, recharge_order):
        user = db.query(models.WebUser).filter(
            models.WebUser.id == recharge_order.user_id
        ).first()
        if user is None:
            # 用户被删了：订单不能就这么标成 paid 又什么都不发（见 FulfillmentError）
            raise FulfillmentError(
                f"充值履约缺料：订单 {recharge_order.order_id} 的用户 "
                f"#{recharge_order.user_id} 已不存在"
            )
        if user:
            _add_points(
                db, user, recharge_order.amount, "recharge",
                f"充值到账（订单 {recharge_order.order_id}）",
                f"recharge:{recharge_order.order_id}",
            )
            # P1 会员经验：真实充值 1 元 = 1 经验（向下取整），与积分/订单在同一事务。
            # 经验失败不阻塞充值履约（只记日志），避免「钱收了货没发全」。
            try:
                from backend import member_level as _ml
                _xp = max(0, int(recharge_order.price or 0))
                if _xp:
                    _ml.add_xp(db, user, _xp, "recharge",
                               f"recharge:{recharge_order.order_id}")
            except Exception:  # noqa: BLE001
                logger.exception("充值经验累加失败: %s", recharge_order.order_id)
            # 邀请返利：被邀请人充值 → 邀请人得返利积分
            try:
                from backend.api.invitation import apply_rebate
                apply_rebate(db, user, float(recharge_order.price or 0),
                             recharge_order.order_id)
            except Exception:  # noqa: BLE001 — 返利失败不阻塞充值履约
                logger.exception("充值返利计算失败: %s", recharge_order.order_id)
            pending.append(dict(
                event_type="economy.recharge_success",
                user_id=user.id,
                title=f"💰 充值成功 +{recharge_order.amount} 积分",
                content=f"订单 {recharge_order.order_id} 已到账，当前余额 {user.points} 积分。",
            ))
        # 优惠券预订 → 已消费（额度仍占用）；关单/退款时才释放
        # （订单状态与 paid_at 已由 _claim_order 原子写入，见该函数的说明）
        coupons.consume(db, recharge_order.coupon_usage_id)

    if subscription_order and _claim_order(db, models.SubscriptionOrder, subscription_order):
        plan = db.query(models.SubscriptionPlan).filter(
            models.SubscriptionPlan.id == subscription_order.plan_id
        ).first()
        user = db.query(models.WebUser).filter(
            models.WebUser.id == subscription_order.user_id
        ).first()
        if user is None or plan is None:
            # 下单之后、支付回调之前套餐被删（或用户被删）：订单会被标成 paid 但用户
            # 什么都拿不到，优惠券还会被 consume 掉——旧实现就是在这里静静地什么都不做，
            # 然后回调返回 success。现在抛出去：调用方回滚 + 返回 fail，转人工处理。
            raise FulfillmentError(
                f"订阅履约缺料：订单 {subscription_order.order_id}"
                f"（套餐 {'已删除' if plan is None else '正常'}、"
                f"用户 {'已删除' if user is None else '正常'}）"
            )
        if user and plan:
            subscription = _grant_subscription(
                db, user, plan,
                plan.duration_days, "purchase", subscription_order.order_id,
                realm_id=plan.realm_id,
            )
            # P1 会员经验：订阅实付 1 元 = 1 经验（向下取整），与订阅发放同一事务。
            try:
                from backend import member_level as _ml
                _xp = max(0, int(subscription_order.amount or 0))
                if _xp:
                    _ml.add_xp(db, user, _xp, "subscription",
                               f"subscription:{subscription_order.order_id}")
            except Exception:  # noqa: BLE001 — 经验失败不阻塞订阅履约
                logger.exception("订阅经验累加失败: %s", subscription_order.order_id)
            # 记下「这条订单开出的是哪份订阅、多少天」：退款按这笔精确回滚，
            # 不靠猜用户当前那笔生效中的订阅（可能来自卡码/兑换码/别的订单）。
            subscription_order.subscription_id = subscription.id
            subscription_order.days_granted = plan.duration_days
            pending.append(dict(
                event_type="economy.subscription_success",
                user_id=user.id,
                title="🎉 订阅购买成功",
                content=(f"「{plan.name}」已开通，"
                         f"到期时间 {subscription.end_date.strftime('%Y-%m-%d')}。"),
                related_id=subscription.id,
            ))
        coupons.consume(db, subscription_order.coupon_usage_id)

    return pending


async def send_fulfill_notifications(pending: list) -> None:
    """履约事务提交之后发送站内信（失败只记日志，不影响已到账的订单）"""
    for item in pending or []:
        try:
            await notify_admin_event(**item)
        except Exception as exc:  # noqa: BLE001 — 通知失败不能影响支付结果
            logger.warning("履约通知发送失败（用户 %s）: %s", item.get("user_id"), exc)


@router.api_route("/payment/notify", methods=["GET", "POST"], response_class=PlainTextResponse)
async def payment_notify(request: Request, db: Session = Depends(get_db)):
    """易支付异步回调：验签 → 履约 → 返回 success

    唯一必须 await 的是读表单与发通知（网关回调最忌讳全站压在事件循环上等一次提交）；
    验签 / 查单 / 履约整段是同步 SQLAlchemy，下放线程池执行。
    """
    raw = dict(request.query_params)
    if request.method == "POST":
        try:
            form = await request.form()
            raw.update({k: v for k, v in form.items() if isinstance(v, str)})
        except Exception:  # noqa: BLE001
            pass

    out_trade_no = raw.get("out_trade_no", "")
    trade_status = raw.get("trade_status", "")
    logger.info("支付回调: order=%s status=%s", out_trade_no, trade_status)

    def _notify() -> tuple:
        """验签 / 查单 / 金额核对 / 履约 → (应答文本, 待发通知)

        履约失败一律返回 "fail"（网关会重试，或者转人工补单），只有真的发出货
        才返回 "success"——不能出现「钱收了、订单标了 paid、用户什么都没拿到」。
        """
        key = _get_config(db, "payment_partner_key", "").strip()
        if not key:
            return "fail", []
        if not _verify_yipay_notify(raw, key):
            logger.warning("支付回调验签失败: order=%s", out_trade_no)
            return "fail", []
        if trade_status not in ("TRADE_SUCCESS", "TRADE_FINISHED"):
            return "success", []  # 非成功状态直接确认，避免重复通知

        recharge_order = db.query(models.RechargeOrder).filter(
            models.RechargeOrder.order_id == out_trade_no
        ).first()
        subscription_order = db.query(models.SubscriptionOrder).filter(
            models.SubscriptionOrder.order_id == out_trade_no
        ).first()

        if not recharge_order and not subscription_order:
            logger.warning("支付回调订单不存在: %s", out_trade_no)
            return "fail", []

        mismatch = _amount_mismatch(recharge_order, subscription_order, raw)
        if mismatch:
            logger.error("支付回调被拒（%s）: order=%s", mismatch, out_trade_no)
            return "fail", []

        try:
            pending = _fulfill_order(db, recharge_order=recharge_order,
                                     subscription_order=subscription_order)
            db.commit()
        except Exception:  # noqa: BLE001
            db.rollback()
            logger.exception("订单履约失败: %s", out_trade_no)
            return "fail", []
        return "success", pending

    reply, pending = await run_in_threadpool(_notify)

    # 先提交再发通知：见 _fulfill_order 的说明（履约中另开会话写站内信会撞 SQLite 写锁）
    if pending:
        await send_fulfill_notifications(pending)
    return reply


@router.api_route("/payment/return", methods=["GET"], response_class=JSONResponse)
async def payment_return(request: Request, db: Session = Depends(get_db)):
    """同步跳转：返回前端跳转地址（前端钱包页轮询订单状态）"""
    order_id = request.query_params.get("out_trade_no", "")
    return {"success": True, "order_id": order_id, "redirect": f"/wallet?order={order_id}"}


# ==================== 积分转账（C2） ====================

def _transfer_enabled(db: Session) -> bool:
    """积分转账总开关（points_transfer_enabled，"1"=开；兼容 true/on/yes）。"""
    value = str(_get_config(db, "points_transfer_enabled", "1")).strip().lower()
    return value not in ("0", "false", "no", "off")


def transfer_points_core(db: Session, sender: models.WebUser, recipient_username: str, amount: int) -> dict:
    """积分转账核心逻辑：校验开关、收款人、金额限制、每日转出上限、手续费与余额，并完成转出方、手续费、收款方三方记账。"""
    if not _transfer_enabled(db):
        raise ValueError("积分转账功能已关闭")

    recipient_username = (recipient_username or "").strip()
    if not recipient_username:
        raise ValueError("请输入对方用户名")

    if recipient_username.lower() == (sender.username or "").lower():
        raise ValueError("不能给自己转账")

    if not isinstance(amount, int) or isinstance(amount, bool) or amount <= 0:
        raise ValueError("转账金额必须为正整数")

    min_amount = _get_int_config(db, "points_transfer_min", 1)
    max_amount = _get_int_config(db, "points_transfer_max", 0)
    daily_cap = _get_int_config(db, "points_transfer_daily_cap", 0)
    fee_pct = _get_int_config(db, "points_transfer_fee_pct", 5)

    if amount < min_amount:
        raise ValueError(f"单笔最少转账 {min_amount} 积分")

    if max_amount > 0 and amount > max_amount:
        raise ValueError(f"单笔最多转账 {max_amount} 积分")

    recipient = (
        db.query(models.WebUser)
        .filter(func.lower(models.WebUser.username) == recipient_username.lower())
        .first()
    )
    if not recipient or not recipient.is_active:
        raise ValueError("对方用户不存在")

    if daily_cap > 0:
        # 先锁转出方用户行再统计今日已转出，避免并发多笔都读到旧用量绕过上限
        lock_user_row(db, sender.id)
        # 日界线按北京时间（上海自然日），与 created_at 的 UTC 入库帧对齐；
        # 服务器本地 00:00 在生产（UTC）是北京时间 08:00，会让限额晚 8 小时重置。
        today_start = _today_start_utc_naive()
        used_today = (
            db.query(func.coalesce(func.sum(-models.PointsLog.amount), 0))
            .filter(
                models.PointsLog.user_id == sender.id,
                models.PointsLog.type == "transfer_out",
                models.PointsLog.amount < 0,
                models.PointsLog.created_at >= today_start,
            )
            .scalar()
        ) or 0
        if used_today + amount > daily_cap:
            raise ValueError(f"今日转账额度已用完（上限 {daily_cap} 积分）")

    fee = math.ceil(amount * fee_pct / 100) if fee_pct > 0 else 0
    need = amount + fee
    balance = sender.points or 0
    if balance < need:
        if fee > 0:
            raise ValueError(f"积分不足，需要 {need} 分（含手续费 {fee} 分），当前 {balance} 分")
        raise ValueError(f"积分不足，需要 {need} 分，当前 {balance} 分")

    ref_id = f"transfer:{sender.id}:{recipient.id}:{int(datetime.now().timestamp())}"

    # 原子条件扣减（本金 + 手续费）：上面的余额检查读的是 ORM 里的旧值，只用于友好提示；
    # 并发两笔转账都会通过它，真正的「够不够」必须由 UPDATE ... WHERE points >= x 判定
    try:
        new_balance = _spend_points(
            db, sender, amount, "transfer_out",
            f"转账给 {recipient.username}（{amount} 积分）", ref_id=ref_id,
        )
        if fee > 0:
            new_balance = _spend_points(
                db, sender, fee, "transfer_fee",
                f"转账手续费 {fee} 积分（{fee_pct}%）", ref_id=ref_id,
            )
    except InsufficientPoints:
        db.rollback()
        raise ValueError(f"积分不足，需要 {need} 分")
    _add_points(
        db, recipient, amount, "transfer_in",
        f"收到 {sender.username} 的转账（{amount} 积分）", ref_id=ref_id,
    )
    db.commit()

    return {"amount": amount, "fee": fee, "recipient": recipient.username, "balance": new_balance}


class PointsTransferRequest(BaseModel):
    """积分转账请求体。"""
    recipient: str
    amount: int


@router.post("/points/transfer")
def transfer_points(
    payload: PointsTransferRequest,
    db: Session = Depends(get_db),
    current_user: models.WebUser = Depends(get_current_user),
):
    """积分转账接口：校验并执行一笔积分转账，返回转账金额、手续费、收款人与转出后余额。"""
    try:
        result = transfer_points_core(db, current_user, payload.recipient, payload.amount)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"success": True, **result}


@router.get("/points/transfer-config")
def get_points_transfer_config(
    db: Session = Depends(get_db),
    current_user: models.WebUser = Depends(get_current_user),
):
    """读取积分转账开关与规则（手续费比例、单笔最小/最大、每日转出上限），供前端隐藏入口与展示提示。"""
    return {
        "enabled": _transfer_enabled(db),
        "fee_pct": _get_int_config(db, "points_transfer_fee_pct", 5),
        "min": _get_int_config(db, "points_transfer_min", 1),
        "max": _get_int_config(db, "points_transfer_max", 0),
        "daily_cap": _get_int_config(db, "points_transfer_daily_cap", 0),
    }
