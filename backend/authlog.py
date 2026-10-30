"""登录与账号安全事件日志

借鉴 twilight-kotomi 的登录日志 / 风控审查：记录登录成功与失败、设备超限被拒、
诱饵码触发封禁等事件，供管理后台筛选审查；并按保留天数清理，避免表无限增长。
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Optional

from sqlalchemy.orm import Session

from backend import models

# reason 取值范围（前端筛选下拉与这里保持一致）
REASONS = {
    "portal_login": "门户登录",
    "portal_login_failed": "门户登录失败",
    "admin_login": "管理员登录",
    "admin_login_failed": "管理员登录失败",
    "emby_login": "客户端登录",
    "emby_login_failed": "客户端登录失败",
    "device_limit": "设备数超限",
    "player_blacklisted": "播放器黑名单拦截",
    "decoy_code": "诱饵码触发",
    "captcha_failed": "人机验证失败",
    "password_change": "修改密码",
    "password_change_failed": "修改密码失败",
    "admin_password_change_failed": "管理员修改密码失败",
    # 防共享命中后停用账号（backend/share_guard.py）：这是「为什么这个号突然登不上」的答案
    "share_guard": "防共享停用",
}
DEFAULT_RETENTION_DAYS = 90  # 可由 login_log_retention_days 配置覆盖


def client_ip(request) -> Optional[str]:
    """取真实客户端 IP（信任反代头，与限流模块同一口径）"""
    if request is None:
        return None
    try:
        from backend.ratelimit import client_ip as _client_ip

        return _client_ip(request)
    except Exception:  # noqa: BLE001 — 日志不应因取 IP 失败而中断
        client = getattr(request, "client", None)
        return getattr(client, "host", None)


def user_agent(request) -> Optional[str]:
    if request is None:
        return None
    try:
        return request.headers.get("user-agent")
    except Exception:  # noqa: BLE001
        return None


def record_event(
    db: Session,
    *,
    username: Optional[str] = None,
    user_id: Optional[int] = None,
    ip: Optional[str] = None,
    agent: Optional[str] = None,
    success: bool = True,
    reason: str = "",
    detail: Optional[str] = None,
    region: Optional[str] = None,
    commit: bool = True,
) -> models.LoginLog:
    row = models.LoginLog(
        user_id=user_id,
        username=(username or "")[:64] or None,
        ip=(ip or "")[:64] or None,
        user_agent=(agent or "")[:300] or None,
        success=bool(success),
        reason=(reason or "")[:100] or None,
        detail=(detail or "")[:255] or None,
        region=(region or resolve_region(db, ip))[:100] or None,
    )
    db.add(row)
    if commit:
        db.commit()
    return row


def resolve_region(db: Session, ip: Optional[str]) -> str:
    """IP 归属地（能力：IP 与地理位置）

    未配置提供方时返回空串，不做任何外部请求；已配置时按能力模块自身的缓存查，
    所以日志写入不会因为多打一个外部接口而变慢。任何异常都吞掉——日志不能写不进去。
    """
    if not ip:
        return ""
    try:
        from backend.integrations import geoip

        return geoip.region_of(db, ip)
    except Exception:  # noqa: BLE001 — 归属地是可选的锦上添花
        return ""


def purge_old(db: Session, days: Optional[int] = None) -> int:
    """清理超过保留期的日志，返回删除条数"""
    if days is None:
        config = db.query(models.SystemConfig).filter(
            models.SystemConfig.key == "login_log_retention_days"
        ).first()
        try:
            days = int(str(config.value).strip()) if config and config.value else DEFAULT_RETENTION_DAYS
        except (TypeError, ValueError):
            days = DEFAULT_RETENTION_DAYS
    if days <= 0:
        return 0
    cutoff = datetime.now() - timedelta(days=days)
    deleted = (
        db.query(models.LoginLog)
        .filter(models.LoginLog.created_at < cutoff)
        .delete(synchronize_session=False)
    )
    db.commit()
    return int(deleted or 0)
