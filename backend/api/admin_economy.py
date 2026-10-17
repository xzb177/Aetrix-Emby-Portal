"""
管理后台 API · 经济、邀请与统计

从 `admin.py` 拆出的「后段」：邀请与返利、积分台账、经济系统设置、经济统计，
以及 v2.4.0 补的用户详情 / 趋势 / 订阅总览。

**这里的端点一律写成同步 ``def``**：项目用的是同步 SQLAlchemy，``async def``
端点会在事件循环上直接跑查询，一个管理员点开「经济统计」就会把全站请求按住。
``def`` 端点由 FastAPI 自动丢到线程池，语义不变（HTTP 契约、依赖注入、返回类型
都一样），但不再占住事件循环。唯一保留 ``async def`` 的是
``economy_adjust_points``——它真的要 ``await notify_admin_event``。

路由对象与其它后台模块共用 ``admin_core.admin_router``，导入即注册。
"""
from datetime import datetime, timedelta
import json
from typing import List, Optional

from fastapi import Depends, HTTPException
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel, Field
from sqlalchemy import func, or_
from sqlalchemy.orm import Session

from backend import models
from backend.member_level import ensure_member_levels_seeded
from backend.api.admin_core import (
    _audit,
    admin_router,
    get_current_admin,
)
from backend.database import get_db
from backend.notifications import notify_admin_event


# ---------- 邀请与返利管理 ----------

@admin_router.get("/economy/invitations")
def economy_list_invitations(
    limit: int = 100,
    current_admin: models.WebUser = Depends(get_current_admin),
    db: Session = Depends(get_db),
):
    """邀请记录列表（谁邀请了谁、奖励多少）"""
    records = db.query(models.InvitationRecord).order_by(
        models.InvitationRecord.created_at.desc()
    ).limit(min(limit, 200)).all()

    user_ids = {r.inviter_id for r in records} | {r.invitee_id for r in records}
    users = {u.id: u.username for u in db.query(models.WebUser).filter(
        models.WebUser.id.in_(user_ids)).all()} if user_ids else {}

    return {"records": [
        {
            "id": r.id,
            "inviter": users.get(r.inviter_id, "未知"),
            "invitee": users.get(r.invitee_id, "未知"),
            "reward_points": r.reward_points,
            "created_at": r.created_at.isoformat() if r.created_at else None,
        }
        for r in records
    ]}


@admin_router.get("/economy/points-logs")
def economy_points_logs(
    user_id: Optional[int] = None,
    type_filter: Optional[str] = None,
    limit: int = 100,
    offset: int = 0,
    current_admin: models.WebUser = Depends(get_current_admin),
    db: Session = Depends(get_db),
):
    """全站积分流水台账"""
    q = db.query(models.PointsLog)
    if user_id:
        q = q.filter(models.PointsLog.user_id == user_id)
    if type_filter:
        q = q.filter(models.PointsLog.type == type_filter)

    total = q.count()
    logs = q.order_by(models.PointsLog.created_at.desc()).offset(offset).limit(min(limit, 200)).all()

    user_ids = {l.user_id for l in logs}
    users = {u.id: u.username for u in db.query(models.WebUser).filter(
        models.WebUser.id.in_(user_ids)).all()} if user_ids else {}

    return {
        "total": total,
        "logs": [
            {
                "id": l.id, "user_id": l.user_id,
                "username": users.get(l.user_id, "未知"),
                "amount": l.amount, "balance_after": l.balance_after,
                "type": l.type, "description": l.description,
                "ref_id": l.ref_id,
                "audited": l.record_hash is not None,  # C3：是否有 hash（审计开启后写入的）
                "created_at": l.created_at.isoformat() if l.created_at else None,
            }
            for l in logs
        ],
    }


class PointsAuditVerifyRequest(BaseModel):
    user_id: int
    limit: int = 20000


@admin_router.post("/economy/points-audit/verify")
def economy_points_audit_verify(
    request: PointsAuditVerifyRequest,
    current_admin: models.WebUser = Depends(get_current_admin),
    db: Session = Depends(get_db),
):
    """C3 流水审计：核验某用户的积分流水 hash 链是否完整（防篡改）"""
    from backend.api.economy import verify_points_chain
    user = db.query(models.WebUser).filter(models.WebUser.id == request.user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="用户不存在")
    result = verify_points_chain(db, request.user_id, limit=max(1, min(request.limit, 100000)))
    result["user_id"] = request.user_id
    result["username"] = user.username
    _audit(db, current_admin, "points_audit_verify", "user", request.user_id,
           {"ok": result["ok"], "verified": result["verified"]})
    db.commit()
    return result


class PointsAdjustRequest(BaseModel):
    amount: int  # 正数发放 / 负数扣除
    reason: str = ""


@admin_router.post("/economy/users/{user_id}/points")
async def economy_adjust_points(
    user_id: int,
    request: PointsAdjustRequest,
    current_admin: models.WebUser = Depends(get_current_admin),
    db: Session = Depends(get_db),
):
    """管理员手动调整用户积分（记账，留审计）—— 写库整段下放线程池

    同步 SQLAlchemy 跑在事件循环上时，一次提交 = 全站排队；这里只需要等通知（WebSocket）。
    """
    from backend.api.economy import _add_points

    admin_id = current_admin.id
    reason = request.reason

    def _adjust() -> int:
        user = db.query(models.WebUser).filter(models.WebUser.id == user_id).first()
        if not user:
            raise HTTPException(status_code=404, detail="用户不存在")
        if request.amount == 0:
            raise HTTPException(status_code=400, detail="调整数量不能为 0")

        balance = _add_points(
            db, user, request.amount,
            "admin_grant" if request.amount > 0 else "admin_deduct",
            reason or f"管理员调整（{request.amount:+d}）",
            f"admin:{admin_id}",
        )
        db.commit()

        _audit(db, admin_id, "economy_adjust_points", "user", user_id,
               {"amount": request.amount, "reason": reason})
        db.commit()
        return balance

    balance = await run_in_threadpool(_adjust)

    await notify_admin_event(
        event_type="economy.admin_adjust",
        user_id=user_id,
        title=f"💰 积分变动 {request.amount:+d}",
        content=f"{reason or '管理员调整'}\n当前余额：{balance} 积分",
        from_admin_id=admin_id,
    )
    return {"success": True, "balance": balance}


# ---------- 经济系统设置 ----------

ECONOMY_CONFIG_KEYS = {
    "checkin_enabled": "bool", "checkin_base_points": "int", "checkin_streak_bonus": "int",
    "checkin_streak_max_bonus": "int",
    # 签到随机基础分 / 惩罚（economy._checkin_rules 读取；留空=回退到 checkin_base_points / 默认值）
    "checkin_base_min": "int", "checkin_base_max": "int",
    "checkin_penalty_pct": "int", "checkin_penalty_min": "int", "checkin_penalty_max": "int",
    "media_seek_daily_limit": "int",  # 用户每日求片上限（用户端硬性校验）
    "exchange_enabled": "bool",
    "recharge_enabled": "bool", "subscription_purchase_enabled": "bool",
    "payment_gateway_url": "str", "payment_partner_id": "str",
    "payment_partner_key": "secret", "payment_qqpay_enabled": "bool",
    "site_url": "str",
    # 付费墙：是否要求有效订阅才能播放 + 自定义拦截文案
    "subscription_required": "bool", "subscription_gate_message": "str",
    # 下载开关与设备/日志风控（借鉴 twilight-kotomi 的运营策略）
    "allow_download": "bool",
    "device_limit_per_user": "int", "device_limit_auto_evict": "bool",
    "login_log_retention_days": "int",
    "max_pending_orders_per_user": "int",
    "invitation_enabled": "bool", "invitation_reward_points": "int",
    "invitation_invitee_reward_points": "int", "invitation_rebate_percent": "int",
    # 推广奖励（v2.44.0）：默认全关/全 0，管理员不手动开就不发
    "promotion_reward_enabled": "bool",
    "promotion_reward_type": "str",
    "promotion_reward_amount": "int",
    "promotion_reward_days": "int",
    # 流媒体加速（v2.49.0）：一个开关 + 一个域名，点保存即生效（域名守卫侧最长 60 秒）
    "stream_accel_enabled": "bool",
    "stream_accel_domain": "str",
    # 活力值（C1 竞品借鉴）：上限/每日扣减/观影阈值/积分兑换率/观影奖励/每日获取上限/归档钳制，全部可配
    "vitality_enabled": "bool", "vitality_max": "int", "vitality_daily_cost": "int",
    "vitality_limit_threshold": "int", "vitality_point_cost": "int",
    "vitality_watch_reward": "int", "vitality_daily_gain_limit": "int",
    "vitality_archive_clamp": "int",
    # C3 流水审计：积分流水 hash 链总开关（默认开；关闭后新流水不写 hash）
    "points_audit_enabled": "bool",
    # Q3 同剧合并：宽松归一化自动合并拆条 series（默认全开；间隔下限 3600 秒，
    # 上限 604800 秒；每轮组数上限 10~100000）。DB 行 > 环境变量 > 代码缺省，
    # 读取侧见 merge_versions_worker.series_merge_config。
    "series_merge_enabled": "bool", "series_merge_loose_enabled": "bool",
    "series_merge_require_year": "bool",
    "series_merge_interval_sec": "int", "series_merge_batch_limit": "int",
}


# 签到数值配置的取值范围（含端点）；空字符串表示「未设置」，读取侧回退默认值
_CHECKIN_INT_RANGES = {
    "checkin_base_min": (0, 100000),
    "checkin_base_max": (0, 100000),
    "checkin_penalty_pct": (0, 100),
    "checkin_penalty_min": (0, 100000),
    "checkin_penalty_max": (0, 100000),
}


def _validate_vitality_settings(db: Session, settings: dict) -> None:
    """活力值配置校验：整数、非负、阈值不超上限（非法直接 400，此时尚未写库）"""
    from backend import vitality as _vitality

    vitality_keys = [k for k in settings if k.startswith("vitality_")]
    if not vitality_keys:
        return

    def _final(key: str) -> str:
        # 本次提交了非空值 → 用它；提交空值视为删除该行 → 用默认值；没提交 → 用库里现有的，没有则用默认值
        if key in settings:
            v = str(settings[key] if settings[key] is not None else "").strip()
            if v:
                return v
            return _vitality.VITALITY_DEFAULTS.get(key, "")
        row = db.query(models.SystemConfig).filter(models.SystemConfig.key == key).first()
        if row and (row.value or "").strip():
            return (row.value or "").strip()
        return _vitality.VITALITY_DEFAULTS.get(key, "")

    merged = {k: _final(k) for k in _vitality.VITALITY_DEFAULTS}
    errors = _vitality.validate_vitality_config(merged)
    # 注意：不能只保留本次提交的 key 再报错。跨字段错误（如 threshold > max）
    # 会被归因到本次未提交的 key 上，过滤后 errors 为空 → 非法组合直接存库。
    # 后果：max < threshold 存入后，所有公益服用户活力值被钳制在 max 以下，
    # 永远达不到观影阈值 → 全员 403 无法观影，且管理员收不到任何提示。
    # 因此合并后的全部错误都要拦截；若库里本就有非法存量值，这次也会一并暴露出来。
    if errors:
        first_key = next(iter(errors))
        raise HTTPException(status_code=400, detail=errors[first_key])


def _validate_checkin_settings(db: Session, settings: dict) -> None:
    """签到随机/惩罚配置校验：整数、范围内、下限不大于上限（非法直接 400，此时尚未写库）"""
    if not any(k in settings for k in _CHECKIN_INT_RANGES):
        return

    def _final(key: str) -> str:
        if key in settings:
            return str(settings[key] if settings[key] is not None else "").strip()
        row = db.query(models.SystemConfig).filter(models.SystemConfig.key == key).first()
        return (row.value or "").strip() if row else ""

    parsed: dict = {}
    for key, (lo, hi) in _CHECKIN_INT_RANGES.items():
        raw = _final(key)
        if raw == "":
            parsed[key] = None
            continue
        try:
            val = int(raw)
        except (TypeError, ValueError):
            raise HTTPException(status_code=400, detail=f"{key} 必须是整数")
        if not lo <= val <= hi:
            raise HTTPException(status_code=400, detail=f"{key} 需在 {lo}~{hi} 之间")
        parsed[key] = val
    for lo_key, hi_key, label in (("checkin_base_min", "checkin_base_max", "签到基础积分"),
                                  ("checkin_penalty_min", "checkin_penalty_max", "签到惩罚")):
        if parsed[lo_key] is not None and parsed[hi_key] is not None and parsed[lo_key] > parsed[hi_key]:
            raise HTTPException(status_code=400, detail=f"{label}下限不能大于上限")


class EconomySettingsRequest(BaseModel):
    settings: dict


@admin_router.get("/economy/settings")
def economy_get_settings(
    current_admin: models.WebUser = Depends(get_current_admin),
    db: Session = Depends(get_db),
):
    result = {}
    for key, value_type in ECONOMY_CONFIG_KEYS.items():
        config = db.query(models.SystemConfig).filter(models.SystemConfig.key == key).first()
        if value_type == "secret":
            result[key] = "******" if (config and config.value) else ""
        else:
            result[key] = config.value if config else ""
    return {"settings": result}


@admin_router.put("/economy/settings")
def economy_update_settings(
    request: EconomySettingsRequest,
    current_admin: models.WebUser = Depends(get_current_admin),
    db: Session = Depends(get_db),
):
    """更新经济系统配置（secret 值为 ****** 时保持不变）"""
    # 流媒体加速：开开关必须配合法域名，否则域名守卫会把所有请求 403 掉。
    # 先按"请求值优先、DB 现有值兜底"算出最终状态，非法直接 400（此时尚未写库）。
    if "stream_accel_enabled" in request.settings or "stream_accel_domain" in request.settings:
        from backend.emby_server import stream_accel as _sa

        def _final(key: str) -> str:
            if key in request.settings:
                return str(request.settings[key] or "")
            row = db.query(models.SystemConfig).filter(models.SystemConfig.key == key).first()
            return row.value if row and row.value else ""

        _enabled = _final("stream_accel_enabled").strip().lower() in ("1", "true", "yes", "on")
        if _enabled:
            try:
                _sa.validate_domain(_final("stream_accel_domain"))
            except ValueError as e:
                raise HTTPException(status_code=400, detail=str(e))
    _validate_checkin_settings(db, request.settings)
    _validate_vitality_settings(db, request.settings)
    # 配置瘦身第 4 批 4-6：落库走统一写入通道 config_service.save_config
    # （校验→批量落库→写时失效）。以下口径与旧代码逐字一致：
    # - 未知键静默忽略；secret 传空/"******" = 保留原行、不进落库；
    # - int/bool 键的空串 = 删行回退默认；str/secret 键原样存（空串也存行）；
    # - 复合校验（stream_accel 联动、签到、活力值）仍在端点层、落库前 400。
    # 失效缺口补齐：旧代码只手动失效 stream_accel_*/vitality_*，其余键保存后
    # 60 秒内可能读到旧值；统一通道对全部落库键 + 删行键写时失效。
    from backend import config_service

    values: dict = {}
    is_secret: dict = {}
    for key, value in request.settings.items():
        if key not in ECONOMY_CONFIG_KEYS:
            continue  # 未知键：今天就是静默忽略
        if ECONOMY_CONFIG_KEYS[key] == "secret" and (not value or value == "******"):
            continue  # 掩码/空：保留原行
        values[key] = value
        is_secret[key] = ECONOMY_CONFIG_KEYS[key] == "secret"

    normalized: dict = {}

    def _capture(_schema: str, norm: dict) -> None:
        normalized.update(norm)

    saved: list = []
    if values:
        # 校验失败抛 ConfigValueError，由 main.py 的统一 handler 转 400
        saved = config_service.save_config(
            db, values, schema="economy", audit=_capture)

    db.commit()
    # （写时失效已由 store.write_values 承接，失效时机从提交后移到写时，
    # 这是正统通道的既定语义，4-1/4-3 起各家族迁移统一如此。）
    changed = {}
    for key in saved:
        if is_secret[key]:
            changed[key] = "(已更新)"  # secret 不进审计日志，只记"改了"
        else:
            changed[key] = normalized.get(key) or ""  # 删行键记 ""，与旧代码一致
    _audit(db, current_admin, "economy_update_settings", "system", None, changed)
    db.commit()
    return {"success": True, "changed": list(changed.keys())}


# ---------- 防盗链（播放签名 + 来路检查，见 backend/emby_server/play_sign.py） ----------

class HotlinkConfigRequest(BaseModel):
    enabled: bool = True
    play_sign_ttl: int = 900
    # 配置瘦身第 3c 批：Referer 白名单（逗号分隔域名）；None=老前端没传=不改
    allowed_referers: Optional[str] = None


@admin_router.get("/hotlink-config")
def hotlink_get_config(
    current_admin: models.WebUser = Depends(get_current_admin),
    db: Session = Depends(get_db),
):
    """防盗链配置读取：总开关 + 播放签名 TTL（含默认值）。"""
    from backend.emby_server import play_sign

    return play_sign.config_payload(db)


@admin_router.put("/hotlink-config")
def hotlink_update_config(
    request: HotlinkConfigRequest,
    current_admin: models.WebUser = Depends(get_current_admin),
    db: Session = Depends(get_db),
):
    """防盗链配置写入：范围非法时 400（中文信息），写完即失效热缓存。"""
    from backend.emby_server import play_sign

    try:
        # 配置瘦身第 3c 批：白名单走同一张卡的既有保存通道（归一化+落库在
        # write_allowed_referers 里），None=前端没传=不碰原值；先写它，
        # 随后的 write_config 回包 config_payload 才能带上最新白名单
        if request.allowed_referers is not None:
            play_sign.write_allowed_referers(db, request.allowed_referers)
        result = play_sign.write_config(
            db,
            enabled=request.enabled,
            play_sign_ttl=request.play_sign_ttl,
        )
    except (ValueError, TypeError) as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    _audit(db, current_admin, "hotlink_update_config", "system", None,
           {"enabled": request.enabled, "play_sign_ttl": request.play_sign_ttl,
            "allowed_referers": request.allowed_referers})
    db.commit()
    return result


# ---------- 经济统计 ----------

def _checkin_day_key(day_val) -> str:
    """签到趋势图：把 checkin_date 标记值映射为上海自然日字符串。

    r34：checkin_date 是「上海自然日 00:00」的 UTC-naive 标记（见
    economy._today_start），+8h 即其代表的上海日期。防御性处理脏数据。
    """
    try:
        return (day_val + timedelta(hours=8)).strftime("%Y-%m-%d")
    except Exception:  # noqa: BLE001
        return str(day_val)[:10]


@admin_router.get("/economy/stats")
def economy_stats(
    current_admin: models.WebUser = Depends(get_current_admin),
    db: Session = Depends(get_db),
):
    """经济系统概览：用户/积分/订单/兑换/邀请"""
    from sqlalchemy import func as _func
    # r34：与签到写入帧同口径（上海自然日，UTC naive）。此前服务器本地口径，
    # 签到帧改为上海日界后这里若不跟进会恒为 0。
    from backend.api.economy import _today_start_utc_naive as _day_start_sh

    total_points = db.query(_func.coalesce(_func.sum(models.WebUser.points), 0)).scalar() or 0
    pending_recharge = db.query(models.RechargeOrder).filter(
        models.RechargeOrder.status == "pending").count()
    paid_recharge = db.query(_func.coalesce(_func.sum(models.RechargeOrder.price), 0)).filter(
        models.RechargeOrder.status == "paid").scalar() or 0
    paid_sub = db.query(_func.coalesce(_func.sum(models.SubscriptionOrder.amount), 0)).filter(
        models.SubscriptionOrder.status == "paid").scalar() or 0

    return {
        "total_points": int(total_points),
        "checkins_today": db.query(models.CheckinRecord).filter(
            models.CheckinRecord.checkin_date >= _day_start_sh()).count(),
        "orders": {"pending": pending_recharge,
                   "revenue": round(float(paid_recharge) + float(paid_sub), 2)},
        "exchange_codes": {
            "total": db.query(models.ExchangeCode).count(),
            "used": db.query(models.ExchangeCode).filter(
                models.ExchangeCode.use_count > 0).count(),
        },
        "invitations": db.query(models.InvitationRecord).count(),
    }


# ==================== 用户详情 / 趋势 / 订阅总览（v2.4.0 补齐） ====================


def _days_left(end_date: Optional[datetime], now: datetime) -> int:
    if not end_date:
        return 0
    return max(0, (end_date - now).days)


@admin_router.get("/users/{user_id}")
def get_user_detail(
    user_id: int,
    current_admin: models.WebUser = Depends(get_current_admin),
    db: Session = Depends(get_db),
):
    """用户 360° 详情：资料 / 订阅 / 积分 / 订单 / 邀请 / 签到 / 观看"""
    from backend.emby_server import models as em

    user = db.query(models.WebUser).filter(models.WebUser.id == user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="用户不存在")

    now = datetime.now()

    # ---------- 订阅 ----------
    subs = (
        db.query(models.UserSubscription)
        .filter(models.UserSubscription.user_id == user_id)
        .order_by(models.UserSubscription.end_date.desc())
        .limit(10)
        .all()
    )
    active_sub = next(
        (s for s in subs if s.status == "active" and s.end_date and s.end_date > now), None
    )

    def _sub_row(s: models.UserSubscription) -> dict:
        return {
            "id": s.id,
            "plan_name": s.plan.name if s.plan else f"套餐 #{s.plan_id}",
            "start_date": s.start_date.isoformat() if s.start_date else None,
            "end_date": s.end_date.isoformat() if s.end_date else None,
            "days_left": _days_left(s.end_date, now),
            "status": s.status,
        }

    # ---------- 积分 ----------
    income = db.query(func.coalesce(func.sum(models.PointsLog.amount), 0)).filter(
        models.PointsLog.user_id == user_id, models.PointsLog.amount > 0
    ).scalar() or 0
    expense = db.query(func.coalesce(func.sum(models.PointsLog.amount), 0)).filter(
        models.PointsLog.user_id == user_id, models.PointsLog.amount < 0
    ).scalar() or 0
    recent_logs = (
        db.query(models.PointsLog)
        .filter(models.PointsLog.user_id == user_id)
        .order_by(models.PointsLog.created_at.desc())
        .limit(10)
        .all()
    )

    # ---------- 订单 ----------
    recharge_orders = (
        db.query(models.RechargeOrder)
        .filter(models.RechargeOrder.user_id == user_id)
        .order_by(models.RechargeOrder.created_at.desc())
        .limit(10)
        .all()
    )
    sub_orders = (
        db.query(models.SubscriptionOrder)
        .filter(models.SubscriptionOrder.user_id == user_id)
        .order_by(models.SubscriptionOrder.created_at.desc())
        .limit(10)
        .all()
    )
    paid_total = float(
        db.query(func.coalesce(func.sum(models.RechargeOrder.price), 0)).filter(
            models.RechargeOrder.user_id == user_id,
            models.RechargeOrder.status == "paid",
        ).scalar() or 0
    ) + float(
        db.query(func.coalesce(func.sum(models.SubscriptionOrder.amount), 0)).filter(
            models.SubscriptionOrder.user_id == user_id,
            models.SubscriptionOrder.status == "paid",
        ).scalar() or 0
    )

    # ---------- 邀请 ----------
    invite_records = (
        db.query(models.InvitationRecord)
        .filter(models.InvitationRecord.inviter_id == user_id)
        .order_by(models.InvitationRecord.created_at.desc())
        .limit(10)
        .all()
    )
    invitee_ids = [r.invitee_id for r in invite_records]
    invitee_names = {
        u.id: u.username
        for u in db.query(models.WebUser).filter(models.WebUser.id.in_(invitee_ids)).all()
    } if invitee_ids else {}
    rebate_total = db.query(func.coalesce(func.sum(models.PointsLog.amount), 0)).filter(
        models.PointsLog.user_id == user_id, models.PointsLog.type == "rebate"
    ).scalar() or 0

    # ---------- 签到 ----------
    checkin_count = db.query(models.CheckinRecord).filter(
        models.CheckinRecord.user_id == user_id
    ).count()
    last_checkin = (
        db.query(models.CheckinRecord)
        .filter(models.CheckinRecord.user_id == user_id)
        .order_by(models.CheckinRecord.checkin_date.desc())
        .first()
    )

    # ---------- 观看 ----------
    plays = db.query(em.PlaybackSession).filter(em.PlaybackSession.user_id == user_id).count()
    watched_items = db.query(em.UserMediaData).filter(
        em.UserMediaData.user_id == user_id,
        em.UserMediaData.played == True,  # noqa: E712
    ).count()

    return {
        "profile": {
            "id": user.id,
            "username": user.username,
            "email": user.email,
            "is_active": user.is_active,
            "is_staff": user.is_staff,
            "emby_username": user.emby_username,
            "points": user.points or 0,
            "last_login_at": user.last_login_at.isoformat() if user.last_login_at else None,
            "created_at": user.created_at.isoformat() if user.created_at else None,
        },
        "subscription": {
            "active": _sub_row(active_sub) if active_sub else None,
            "history": [_sub_row(s) for s in subs],
        },
        "points": {
            "balance": user.points or 0,
            "income": int(income),
            "expense": abs(int(expense)),
            "recent": [
                {
                    "id": l.id,
                    "amount": l.amount,
                    "balance_after": l.balance_after,
                    "type": l.type,
                    "description": l.description,
                    "created_at": l.created_at.isoformat() if l.created_at else None,
                }
                for l in recent_logs
            ],
        },
        "orders": {
            "paid_total": round(paid_total, 2),
            "recharge": [
                {
                    "order_id": o.order_id,
                    "item_name": o.package.name if o.package else f"套餐 #{o.package_id}",
                    "amount": float(o.price),
                    "points": o.amount,
                    "status": o.status,
                    "created_at": o.created_at.isoformat() if o.created_at else None,
                }
                for o in recharge_orders
            ],
            "subscription": [
                {
                    "order_id": o.order_id,
                    "item_name": o.item_name or (o.plan.name if o.plan else f"套餐 #{o.plan_id}"),
                    "amount": float(o.amount),
                    "status": o.status,
                    "created_at": o.created_at.isoformat() if o.created_at else None,
                }
                for o in sub_orders
            ],
        },
        "invitation": {
            "count": db.query(models.InvitationRecord).filter(
                models.InvitationRecord.inviter_id == user_id
            ).count(),
            "rebate_total": int(rebate_total),
            "invitees": [
                {
                    "username": invitee_names.get(r.invitee_id, "未知"),
                    "reward_points": r.reward_points,
                    "created_at": r.created_at.isoformat() if r.created_at else None,
                }
                for r in invite_records
            ],
        },
        "checkin": {
            "total": checkin_count,
            "last_date": last_checkin.checkin_date.isoformat() if last_checkin else None,
            "streak": last_checkin.streak if last_checkin else 0,
        },
        "watch": {
            "plays": plays,
            "watched_items": watched_items,
        },
    }


@admin_router.get("/stats/trend")
def get_stats_trend(
    days: int = 14,
    current_admin: models.WebUser = Depends(get_current_admin),
    db: Session = Depends(get_db),
):
    """趋势统计（默认近 14 天）：新增用户 / 播放次数 / 营收 / 签到

    单次聚合查询后按日补零，保证折线图连续。
    """
    from backend.emby_server import models as em

    days = max(1, min(days, 90))
    # r43：label 轴按上海自然日。入库时间戳是 UTC-naive，此前用服务器本地
    # （生产 UTC）的 today 生成 UTC 日期序列，签到系列经 _checkin_day_key
    # +8h 还原为上海日期 → 每天 00:00–08:00 CST 的签到在图上消失。
    # tzdata 由镜像显式安装（Dockerfile.backend），这里仍 try/except 回退。
    from zoneinfo import ZoneInfo as _ZoneInfo
    try:
        _now = datetime.now(_ZoneInfo("Asia/Shanghai")).replace(tzinfo=None)
    except Exception:  # noqa: BLE001
        _now = datetime.now()
    today = _now.replace(hour=0, minute=0, second=0, microsecond=0)
    start_day = today - timedelta(days=days - 1)
    # 过滤起点再往前推 8h，覆盖上海窗口左边界（D 日 00:00 CST = D-1 日 16:00 UTC）。
    start = start_day - timedelta(hours=8)

    def _series(rows) -> dict:
        # r43：SQL 只按原始 UTC-naive 时间戳分组取值，Python 侧逐行 +8h 再按
        # 上海自然日聚合（此前用 func.date 按 UTC 切日、bucket 午夜 +8h 仍是同一天，
        # 属 no-op：每天 00:00–08:00 CST 的事件被算到前一天 label。方言无关，
        # SQLite/PG 的 interval 写法不同，不在 SQL 里做时区换算）。
        out: dict = {}
        for ts, value in rows:
            try:
                key = (ts + timedelta(hours=8)).strftime("%Y-%m-%d")
            except Exception:  # noqa: BLE001
                key = str(ts)[:10]
            out[key] = out.get(key, 0) + (value or 0)
        return out

    user_rows = db.query(
        models.WebUser.created_at, func.count(models.WebUser.id)
    ).filter(models.WebUser.created_at >= start).group_by(
        models.WebUser.created_at
    ).all()

    play_rows = db.query(
        em.PlaybackSession.start_time, func.count(em.PlaybackSession.id)
    ).filter(em.PlaybackSession.start_time >= start).group_by(
        em.PlaybackSession.start_time
    ).all()

    recharge_rows = db.query(
        models.RechargeOrder.paid_at, func.coalesce(func.sum(models.RechargeOrder.price), 0)
    ).filter(
        models.RechargeOrder.status == "paid", models.RechargeOrder.paid_at >= start
    ).group_by(models.RechargeOrder.paid_at).all()

    sub_rows = db.query(
        models.SubscriptionOrder.paid_at, func.coalesce(func.sum(models.SubscriptionOrder.amount), 0)
    ).filter(
        models.SubscriptionOrder.status == "paid", models.SubscriptionOrder.paid_at >= start
    ).group_by(models.SubscriptionOrder.paid_at).all()

    # r34：checkin_date 是「上海自然日 00:00」的 UTC-naive 标记（见 economy._today_start），
    # 不是事件时间——按 func.date(checkin_date) 分组会把每天的签到算到前一天。
    # 这里按原始标记值分组，在 Python 侧 +8h 还原上海日期（方言无关，
    # SQLite/PG 的 interval 写法不同，不在 SQL 里做）。
    checkin_rows = db.query(
        models.CheckinRecord.checkin_date, func.count(models.CheckinRecord.id)
    ).filter(models.CheckinRecord.checkin_date >= start).group_by(
        models.CheckinRecord.checkin_date
    ).all()
    checkin_map = {}
    for _day_val, _cnt in checkin_rows:
        _key = _checkin_day_key(_day_val)
        checkin_map[_key] = checkin_map.get(_key, 0) + (_cnt or 0)

    users_map = _series(user_rows)
    plays_map = _series(play_rows)
    recharge_map = _series(recharge_rows)
    sub_map = _series(sub_rows)

    series = []
    for i in range(days):
        day = start_day + timedelta(days=i)
        key = day.strftime("%Y-%m-%d")
        series.append({
            "date": key,
            "new_users": int(users_map.get(key, 0)),
            "plays": int(plays_map.get(key, 0)),
            "revenue": round(float(recharge_map.get(key, 0)) + float(sub_map.get(key, 0)), 2),
            "checkins": int(checkin_map.get(key, 0)),
        })

    return {
        "days": days,
        "series": series,
        "totals": {
            "new_users": sum(p["new_users"] for p in series),
            "plays": sum(p["plays"] for p in series),
            "revenue": round(sum(p["revenue"] for p in series), 2),
            "checkins": sum(p["checkins"] for p in series),
        },
    }


@admin_router.get("/economy/subscriptions")
def economy_list_subscriptions(
    status_filter: str = "",
    limit: int = 50,
    offset: int = 0,
    current_admin: models.WebUser = Depends(get_current_admin),
    db: Session = Depends(get_db),
):
    """订阅总览：全部订阅记录 + 状态筛选 + 到期概览

    status_filter: active（生效中）/ expiring（7 天内到期）/ expired（已过期）
    """
    now = datetime.now()
    week_later = now + timedelta(days=7)

    q = db.query(models.UserSubscription)
    if status_filter == "active":
        q = q.filter(models.UserSubscription.status == "active",
                     models.UserSubscription.end_date > now)
    elif status_filter == "expiring":
        q = q.filter(models.UserSubscription.status == "active",
                     models.UserSubscription.end_date > now,
                     models.UserSubscription.end_date <= week_later)
    elif status_filter == "expired":
        q = q.filter(or_(models.UserSubscription.status == "expired",
                         models.UserSubscription.end_date <= now))

    total = q.count()
    rows = q.order_by(models.UserSubscription.end_date.asc()).offset(offset).limit(min(limit, 200)).all()

    user_ids = {r.user_id for r in rows}
    users = {
        u.id: u.username
        for u in db.query(models.WebUser).filter(models.WebUser.id.in_(user_ids)).all()
    } if user_ids else {}

    active_count = db.query(models.UserSubscription).filter(
        models.UserSubscription.status == "active", models.UserSubscription.end_date > now
    ).count()
    expiring_count = db.query(models.UserSubscription).filter(
        models.UserSubscription.status == "active",
        models.UserSubscription.end_date > now,
        models.UserSubscription.end_date <= week_later,
    ).count()
    expired_count = db.query(models.UserSubscription).filter(
        or_(models.UserSubscription.status == "expired",
            models.UserSubscription.end_date <= now)
    ).count()

    return {
        "total": total,
        "summary": {
            "active": active_count,
            "expiring_7d": expiring_count,
            "expired": expired_count,
        },
        "subscriptions": [
            {
                "id": r.id,
                "user_id": r.user_id,
                "username": users.get(r.user_id, "未知"),
                "plan_name": r.plan.name if r.plan else f"套餐 #{r.plan_id}",
                "start_date": r.start_date.isoformat() if r.start_date else None,
                "end_date": r.end_date.isoformat() if r.end_date else None,
                "days_left": _days_left(r.end_date, now),
                "status": "active" if (r.status == "active" and r.end_date and r.end_date > now) else "expired",
            }
            for r in rows
        ],
    }


# ========== 会员等级管理（P1 统一货币体系） ==========

def _level_to_dict(lv: models.MemberLevel) -> dict:
    """MemberLevel 转 dict（benefits_json 容错解析为数组）"""
    benefits = []
    if lv.benefits_json:
        try:
            parsed = json.loads(lv.benefits_json)
            if isinstance(parsed, list):
                benefits = parsed
        except (ValueError, TypeError):
            benefits = []
    return {
        "id": lv.id,
        "level": lv.level,
        "name": lv.name,
        "xp_threshold": lv.xp_threshold,
        "discount_pct": lv.discount_pct or 0,
        "benefits": benefits,
        "badge_icon": lv.badge_icon,
        "badge_color": lv.badge_color,
        "is_active": lv.is_active,
    }


class MemberLevelUpdate(BaseModel):
    name: Optional[str] = None
    xp_threshold: Optional[int] = None
    discount_pct: Optional[int] = Field(default=None, ge=0, le=100)
    benefits: Optional[List[str]] = None
    badge_icon: Optional[str] = None
    badge_color: Optional[str] = None


@admin_router.get("/member-levels")
def list_member_levels(current_admin: models.WebUser = Depends(get_current_admin), db: Session = Depends(get_db)):
    # 确保种子等级存在
    ensure_member_levels_seeded(db)
    levels = db.query(models.MemberLevel).order_by(models.MemberLevel.level.asc()).all()
    return {"levels": [_level_to_dict(lv) for lv in levels]}


@admin_router.put("/member-levels/{level_id}")
def update_member_level(level_id: int, payload: MemberLevelUpdate, current_admin: models.WebUser = Depends(get_current_admin), db: Session = Depends(get_db)):
    lv = db.query(models.MemberLevel).filter(models.MemberLevel.id == level_id).first()
    if not lv:
        raise HTTPException(status_code=404, detail="等级不存在")
    update_data = {}
    if payload.name is not None:
        lv.name = payload.name
        update_data["name"] = payload.name
    if payload.xp_threshold is not None:
        if payload.xp_threshold < 0:
            raise HTTPException(status_code=400, detail="经验阈值不能为负数")
        lv.xp_threshold = payload.xp_threshold
        update_data["xp_threshold"] = payload.xp_threshold
    if payload.discount_pct is not None:
        lv.discount_pct = payload.discount_pct
        update_data["discount_pct"] = payload.discount_pct
    if payload.benefits is not None:
        # 数组转 JSON 存储
        lv.benefits_json = json.dumps(payload.benefits, ensure_ascii=False)
        update_data["benefits"] = payload.benefits
    if payload.badge_icon is not None:
        lv.badge_icon = payload.badge_icon
        update_data["badge_icon"] = payload.badge_icon
    if payload.badge_color is not None:
        lv.badge_color = payload.badge_color
        update_data["badge_color"] = payload.badge_color
    db.commit()
    db.refresh(lv)
    _audit(db, current_admin.id, "member_level_update", "member_level", lv.id, update_data)
    return {"level": _level_to_dict(lv)}


@admin_router.delete("/member-levels/{level_id}")
def delete_member_level(level_id: int, current_admin: models.WebUser = Depends(get_current_admin), db: Session = Depends(get_db)):
    lv = db.query(models.MemberLevel).filter(models.MemberLevel.id == level_id).first()
    if not lv:
        raise HTTPException(status_code=404, detail="等级不存在")
    # 删除前校验：删除启用等级时至少保留 1 个启用等级
    if lv.is_active:
        active_count = db.query(models.MemberLevel).filter(
            models.MemberLevel.is_active.is_(True),
            models.MemberLevel.id != level_id,
        ).count()
        if active_count < 1:
            raise HTTPException(status_code=400, detail="至少保留 1 个启用等级")
    level_num = lv.level
    db.delete(lv)
    db.commit()
    _audit(db, current_admin.id, "member_level_delete", "member_level", level_id, {"level": level_num})
    return {"deleted": True}


@admin_router.put("/member-levels/{level_id}/toggle")
def toggle_member_level(level_id: int, current_admin: models.WebUser = Depends(get_current_admin), db: Session = Depends(get_db)):
    lv = db.query(models.MemberLevel).filter(models.MemberLevel.id == level_id).first()
    if not lv:
        raise HTTPException(status_code=404, detail="等级不存在")
    # 关闭时需保证至少保留 1 个启用等级
    if lv.is_active:
        active_count = db.query(models.MemberLevel).filter(
            models.MemberLevel.is_active.is_(True),
            models.MemberLevel.id != level_id,
        ).count()
        if active_count < 1:
            raise HTTPException(status_code=400, detail="至少保留 1 个启用等级")
    lv.is_active = not lv.is_active
    db.commit()
    db.refresh(lv)
    _audit(db, current_admin.id, "member_level_toggle", "member_level", lv.id, {"is_active": lv.is_active})
    return {"level": _level_to_dict(lv)}
