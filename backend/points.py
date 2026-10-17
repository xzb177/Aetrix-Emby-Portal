# -*- coding: utf-8 -*-
"""公益服积分模块（模块2）

定位：公益服的"货币"层。复用 economy.py 的基础设施
（WebUser.points 余额 + PointsLog 流水 + _add_points 原子更新），
只提供公益服专用的业务逻辑：

- award_chat_points：发言奖励（每日上限）
- get_points_summary：积分概览
- redeem_welfare：积分兑换公益天数
- welfare_signin：【已废弃 2026-10-09】转发到 backend.api.economy._do_checkin_core，
  签到已统一到 POST /api/user/economy/checkin（配置在系统设置→每日签到）

SystemConfig 键（实际生效的签到键在 backend/api/economy._checkin_rules 读取，
管理后台「系统设置 → 每日签到」可配）：
- checkin_base_min / checkin_base_max（基础积分随机范围）
- checkin_streak_bonus（连签加成）
- points_chat_daily_cap=20, points_chat_per_msg=1
- points_redeem_7d=100, points_redeem_30d=300
"""

from __future__ import annotations

import logging
import random

from sqlalchemy import func
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from backend import models
from backend.api import economy

logger = logging.getLogger(__name__)


def _get_int(db: Session, key: str, default: int) -> int:
    """读整数配置"""
    return economy._get_int_config(db, key, default)


def award_chat_points(db: Session, user_id: int) -> int:
    """发言奖励（供聊天/评论模块调用）

    每条发言奖励 points_chat_per_msg 分，每日上限 points_chat_daily_cap 分。
    达到上限后返回 0，不再奖励。
    """
    user = db.query(models.WebUser).filter(models.WebUser.id == user_id).first()
    if user is None:
        return 0

    cap = _get_int(db, "points_chat_daily_cap", 20)
    if cap < 0:
        # R18-P3：与 per_msg 同口径——负数记日志回落默认 20。旧实现 earned_today>=cap
        # 恒成立（earned_today>=0 > 负 cap），奖励被静默永久关闭且无日志，配错了都不知道。
        logger.warning("points_chat_daily_cap 配置为负数 %s，已回落默认值 20", cap)
        cap = 20
    per_msg = _get_int(db, "points_chat_per_msg", 1)
    # P2 修复（完美审查 B-P2-5）：负配置会反向扣分——非法值记日志回落默认；
    # 0 视为关闭奖励，直接返回。
    if per_msg < 0:
        logger.warning("points_chat_per_msg 配置为负数 %s，已回落默认值 1", per_msg)
        per_msg = 1
    if per_msg == 0:
        # R18-P3：奖励关闭的早退移到行锁 + SUM 查询之前——读配置不需要锁，
        # 每条聊天消息白拿一次 FOR UPDATE 行锁是纯浪费。
        return 0

    # P2 修复（审查）：每日上限是"先 SUM 后判"，并发两请求都读到同一旧用量、
    # 都判定未超限 → 超发。先锁用户行再统计（与红包频率检查同口径）。
    economy.lock_user_row(db, user_id)

    today = economy._today_start_utc_naive()  # r34：上海自然日（UTC 入库帧），此前服务器本地口径在北京时间 08:00 才重置
    earned_today = db.query(func.coalesce(func.sum(models.PointsLog.amount), 0)).filter(
        models.PointsLog.user_id == user_id,
        models.PointsLog.type == "chat",
        models.PointsLog.amount > 0,
        models.PointsLog.created_at >= today,
    ).scalar() or 0

    if earned_today >= cap:
        # P2 修复（完美审查 B-P2-4）：上限分支也要先 rollback 再返回——
        # 否则行锁持有到请求结束，阻塞同用户并发写入。
        db.rollback()
        return 0

    # 避免单次奖励超过上限
    award = min(per_msg, cap - earned_today)
    economy._add_points(db, user, award, "chat", "发言奖励")
    db.commit()
    return award


def get_points_summary(db: Session, user: models.WebUser) -> dict:
    """积分概览：余额 / 今日获得 / 累计获得"""
    balance = int(user.points or 0)
    today = economy._today_start_utc_naive()  # r34：上海自然日（UTC 入库帧），此前服务器本地口径在北京时间 08:00 才重置

    today_earned = db.query(func.coalesce(func.sum(models.PointsLog.amount), 0)).filter(
        models.PointsLog.user_id == user.id,
        models.PointsLog.amount > 0,
        models.PointsLog.created_at >= today,
    ).scalar() or 0

    total_earned = db.query(func.coalesce(func.sum(models.PointsLog.amount), 0)).filter(
        models.PointsLog.user_id == user.id,
        models.PointsLog.amount > 0,
    ).scalar() or 0

    return {
        "balance": balance,
        "today_earned": int(today_earned),
        "total_earned": int(total_earned),
    }


def redeem_welfare(db: Session, user: models.WebUser, days_option: int) -> dict:
    """积分兑换公益天数

    days_option: 只能是 7 或 30
    流程：扣分 → 调 portal.grant_welfare 开通/续期
    注意：grant_welfare 内部会 db.commit()，所以扣分后不再单独 commit
    """
    if days_option not in (7, 30):
        raise ValueError("仅支持兑换 7 天或 30 天")

    cost_key = "points_redeem_7d" if days_option == 7 else "points_redeem_30d"
    default_cost = 100 if days_option == 7 else 300
    cost = _get_int(db, cost_key, default_cost)

    # P1 修复（审查）：「先读余额判断、再 _add_points 扣」是读-改-写双花。
    # _spend_points 把「够不够」放进 UPDATE 的 WHERE 由数据库原子判定。
    from backend.api.economy import _spend_points, InsufficientPoints
    try:
        new_balance = _spend_points(
            db, user, cost, "redeem", f"积分兑换公益{days_option}天"
        )
    except InsufficientPoints as exc:
        raise ValueError(str(exc)) from exc

    # 再开通（内部 commit）
    from backend.emby_server import portal
    expires_at = portal.grant_welfare(
        db, user, channel="points_redeem", days=days_option
    )

    return {
        "days": days_option,
        "cost": cost,
        "balance": new_balance,
        "expires_at": expires_at.isoformat() if expires_at else None,
    }


def welfare_signin(db: Session, user: models.WebUser) -> dict:
    """【已废弃 2026-10-09】公益服签到已统一到 backend.api.economy._do_checkin_core。

    此函数仅为兼容保留（POST /api/points/signin 无人调用），转发到统一核心。
    新代码请直接调用 economy._do_checkin_core。

    返回：{'points': 实际得分, 'streak': 连签天数,
           'balance': 余额, 'penalty': 是否触发惩罚}
    """
    logger.warning(
        "deprecated backend.points.welfare_signin called for user %s; "
        "use backend.api.economy._do_checkin_core instead", user.id,
    )
    from backend.api.economy import _do_checkin_core
    result = _do_checkin_core(db, user)
    return {
        "points": result["points_awarded"],
        "streak": result["streak"],
        "balance": result["balance"],
        "penalty": result.get("penalty", False),
    }
