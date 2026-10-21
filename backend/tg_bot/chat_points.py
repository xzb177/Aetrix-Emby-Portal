

# -*- coding: utf-8 -*-
"""群发言积分模块（M1）。

被轮询分发器调用：非命令分支调 handle_group_message，/chatpoints 命令调 handle_chatpoints。
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import func
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from backend import models
from backend.integrations.store import switch_env_value as _switch_env_value
from backend.api import economy

logger = logging.getLogger(__name__)


def _get_str_config(db: Session, key: str, default: str = "") -> str:
    """读取字符串类型 SystemConfig。"""
    # A 类摘面板开关（2026-10-21）：env 显式设置即接管，口径见 integrations.store
    _env_v = _switch_env_value(key)
    if _env_v is not None:
        return _env_v
    cfg = db.query(models.SystemConfig).filter(models.SystemConfig.key == key).first()
    return cfg.value if cfg else default


def _batch_configs(db: Session, defaults: dict[str, str]) -> dict[str, str]:
    """批量读取多个 SystemConfig（1 次 IN 查询）。

    高频路径（每条群消息）用，避免逐个 key 查 DB。
    """
    keys = list(defaults.keys())
    rows = (
        db.query(models.SystemConfig.key, models.SystemConfig.value)
        .filter(models.SystemConfig.key.in_(keys))
        .all()
    )
    result = dict(defaults)
    for k, v in rows:
        result[k] = v
    # A 类摘面板开关（2026-10-21）：env 显式设置即接管，口径见 integrations.store
    for k in result:
        _env_v = _switch_env_value(k)
        if _env_v is not None:
            result[k] = _env_v
    return result


def is_enabled(db: Session) -> bool:
    """判断群发言积分总开关是否开启。"""
    val = _get_str_config(db, "chat_points_enabled", "false")
    return val.strip().lower() == "true"


def _to_int(val: str, default: int) -> int:
    """字符串转 int，非法时返回默认值。"""
    try:
        return int(str(val).strip())
    except (ValueError, TypeError):
        return default


def _parse_group_ids_str(val: str) -> set[int]:
    """解析逗号分隔的群 id 字符串，非法片段跳过。"""
    result: set[int] = set()
    for part in val.split(","):
        part = part.strip()
        if part:
            try:
                result.add(int(part))
            except ValueError:
                pass
    return result


def parse_group_ids(db: Session) -> set[int]:
    """解析目标群 id 集合，逗号分隔，非法片段跳过。"""
    return _parse_group_ids_str(_get_str_config(db, "chat_points_group_ids", ""))


def is_valid_message(msg: dict[str, Any], min_len: int) -> bool:
    """判断消息是否为有效发言。"""
    text = msg.get("text")
    if not text or not isinstance(text, str):
        return False
    stripped = text.strip()
    if stripped.startswith("/"):
        return False
    if msg.get("from", {}).get("is_bot") is True:
        return False
    if "forward_origin" in msg:
        return False
    if len(stripped) < min_len:
        return False
    return True


def handle_group_message(db: Session, update: dict[str, Any]) -> int:
    """处理群消息，返回实际发放的分数（>0 表示成功）。"""
    try:
        cfg = _batch_configs(db, {
            "chat_points_enabled": "false",
            "chat_points_group_ids": "",
            "chat_points_min_len": "2",
            "chat_points_minute_window": "60",
            "chat_points_daily_cap": "20",
            "chat_points_per_message": "1",
        })
        if cfg["chat_points_enabled"].strip().lower() != "true":
            return 0
        msg = update.get("message")
        if not msg:
            return 0
        chat_id = msg.get("chat", {}).get("id")
        group_ids = _parse_group_ids_str(cfg["chat_points_group_ids"])
        if not group_ids or chat_id not in group_ids:
            return 0
        from_user = msg.get("from", {})
        telegram_id = from_user.get("id")
        if not telegram_id:
            return 0
        user = db.query(models.WebUser).filter(
            models.WebUser.telegram_id == telegram_id
        ).first()
        if not user:
            return 0
        min_len = _to_int(cfg["chat_points_min_len"], 2)
        if not is_valid_message(msg, min_len):
            return 0
        minute_window = _to_int(cfg["chat_points_minute_window"], 60)
        cutoff = datetime.now() - timedelta(seconds=minute_window)
        recent = db.query(models.ChatPointsLog.id).filter(
            models.ChatPointsLog.telegram_id == telegram_id,
            models.ChatPointsLog.created_at >= cutoff,
        ).limit(1).first()
        if recent:
            return 0
        cap = _to_int(cfg["chat_points_daily_cap"], 20)
        # 锁用户行后再统计今日已得：并发多条消息都读到旧 earned 会绕过日上限
        economy.lock_user_row(db, user.id)
        # 锁后二次校验分钟窗口：上面的 recent 检查是无锁的 check-then-act，
        # 并发线程可同时通过；而本线程在等用户行锁期间，对方可能已提交发奖记录，
        # 因此必须持锁后用新的 cutoff 重新查一次，避免 60 秒窗口被绕过双发。
        cutoff = datetime.now() - timedelta(seconds=minute_window)
        recent = db.query(models.ChatPointsLog.id).filter(
            models.ChatPointsLog.telegram_id == telegram_id,
            models.ChatPointsLog.created_at >= cutoff,
        ).limit(1).first()
        if recent:
            db.rollback()
            return 0
        today = datetime.now().date()
        earned = db.query(
            func.coalesce(func.sum(models.ChatPointsLog.points), 0)
        ).filter(
            models.ChatPointsLog.web_user_id == user.id,
            models.ChatPointsLog.points_date == today,
        ).scalar()
        if earned >= cap:
            return 0
        per_msg = _to_int(cfg["chat_points_per_message"], 1)
        award = min(per_msg, cap - earned)
        if award <= 0:
            return 0
        economy._add_points(db, user, award, "chat", "群发言奖励")
        message_id = msg.get("message_id")
        if message_id is None:
            db.rollback()
            return 0
        log = models.ChatPointsLog(
            web_user_id=user.id,
            telegram_id=telegram_id,
            chat_id=chat_id,
            message_id=message_id,
            points=award,
            points_date=today,
            created_at=datetime.now(),
        )
        db.add(log)
        db.commit()
        return award
    except IntegrityError:
        db.rollback()
        return 0
    except Exception:
        logger.error("chat_points: unexpected error", exc_info=True)
        db.rollback()
        return 0


def get_today_summary(db: Session, user: models.WebUser) -> dict[str, int]:
    """获取用户积分摘要。"""
    today = datetime.now().date()
    today_sum = db.query(
        func.coalesce(func.sum(models.ChatPointsLog.points), 0)
    ).filter(
        models.ChatPointsLog.web_user_id == user.id,
        models.ChatPointsLog.points_date == today,
    ).scalar()
    cap = economy._get_int_config(db, "chat_points_daily_cap", 20)
    month_start = today.replace(day=1)
    month_sum = db.query(
        func.coalesce(func.sum(models.ChatPointsLog.points), 0)
    ).filter(
        models.ChatPointsLog.web_user_id == user.id,
        models.ChatPointsLog.points_date >= month_start,
    ).scalar()
    return {"today": today_sum, "cap": cap, "month": month_sum}


def handle_chatpoints(
    db: Session, tg_user: dict[str, Any], chat_id: int, args: str
) -> str:
    """处理 /chatpoints 命令，返回回复文本。

    调用方负责发送：即使在群里触发也用 tg_user["id"] 私聊回复，避免泄露积分信息。
    chat_id 参数保留以兼容 (db, tg_user, chat_id, args) 处理器签名。
    """
    if not is_enabled(db):
        return "💬 群发言积分功能未开启，请联系服主开启。"
    tg_id = tg_user.get("id")
    if not tg_id:
        return "请先绑定 Telegram 后再查询。"
    user = db.query(models.WebUser).filter(
        models.WebUser.telegram_id == tg_id
    ).first()
    if not user:
        return "💬 请先绑定 Telegram 账号：在网页个人中心 → 绑定 Telegram，或发送 /bind 获取绑定码。"
    summary = get_today_summary(db, user)
    today, cap, month = summary["today"], summary["cap"], summary["month"]
    filled = round(today / cap * 10) if cap > 0 else 0
    bar = "▓" * filled + "░" * (10 - filled)
    lines = [
        "💬 <b>群发言积分</b>",
        "",
        f"📊 今日已得：<b>{today}</b> / {cap} 分",
        f"<code>{bar}</code>",
        f"📅 本月累计：<b>{month}</b> 分",
    ]
    if today >= cap:
        lines.append("")
        lines.append("✅ 今日已达上限，明天再来！")
    return "\n".join(lines)
