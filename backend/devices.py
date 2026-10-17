"""用户设备登记与设备数上限（第三方播放器风控）

借鉴 twilight-kotomi 的设备限制：按 ``user_id + device_id`` 唯一登记设备，
记录客户端名称/版本/IP 与首末次出现时间；超出上限时可拒绝新设备登录，
也可自动踢掉最久未使用的设备（由 ``device_limit_auto_evict`` 控制）。

管理员始终放行，避免自己被锁在外面。
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Optional

from sqlalchemy.orm import Session

from backend import models

# 活跃判定：超过该天数未出现的设备不计入设备数
ACTIVE_DEVICE_DAYS = 30
# 令牌使用时的设备活跃时间刷新间隔（避免每个请求都写库）
TOUCH_INTERVAL_SECONDS = 300


class DeviceLimitExceeded(Exception):
    """设备数超限（由 AuthenticateByName 转成可读的 403 响应）"""

    def __init__(self, message: str, devices: list):
        super().__init__(message)
        self.message = message
        self.devices = devices


def _config_int(db: Session, key: str, default: int) -> int:
    row = db.query(models.SystemConfig).filter(models.SystemConfig.key == key).first()
    try:
        return int(str(row.value).strip()) if row and str(row.value).strip() else default
    except (TypeError, ValueError):
        return default


def _config_bool(db: Session, key: str, default: bool = False) -> bool:
    row = db.query(models.SystemConfig).filter(models.SystemConfig.key == key).first()
    if not row or not str(row.value).strip():
        return default
    return str(row.value).strip().lower() in ("1", "true", "yes", "on")


def device_limit(db: Session) -> int:
    """每用户设备上限，0 表示不限"""
    return max(_config_int(db, "device_limit_per_user", 0), 0)


def device_auto_evict(db: Session) -> bool:
    """设备超限时是否自动踢掉最久未使用的一台（否则直接拒绝新设备）"""
    return _config_bool(db, "device_limit_auto_evict")



def list_devices(db: Session, user_id: int) -> list[models.UserDevice]:
    return (
        db.query(models.UserDevice)
        .filter(models.UserDevice.user_id == user_id)
        .order_by(models.UserDevice.last_seen_at.desc())
        .all()
    )


def device_dto(device: models.UserDevice) -> dict:
    return {
        "device_id": device.device_id,
        "name": device.name,
        "client": device.client,
        "app_version": device.app_version,
        "ip": device.ip,
        "is_blocked": bool(device.is_blocked),
        "first_seen_at": device.first_seen_at.isoformat() if device.first_seen_at else None,
        "last_seen_at": device.last_seen_at.isoformat() if device.last_seen_at else None,
    }


def active_devices(db: Session, user_id: int, days: int = ACTIVE_DEVICE_DAYS) -> list[models.UserDevice]:
    cutoff = datetime.now() - timedelta(days=days)
    return [
        d
        for d in list_devices(db, user_id)
        if not d.is_blocked and (d.last_seen_at is None or d.last_seen_at >= cutoff)
    ]


def _get_device(db: Session, user_id: int, device_id: str) -> Optional[models.UserDevice]:
    return (
        db.query(models.UserDevice)
        .filter(models.UserDevice.user_id == user_id, models.UserDevice.device_id == device_id)
        .first()
    )


def register_device(
    db: Session,
    user: models.WebUser,
    *,
    device_id: str,
    name: Optional[str] = None,
    client: Optional[str] = None,
    app_version: Optional[str] = None,
    ip: Optional[str] = None,
) -> models.UserDevice:
    """登记 / 更新设备，并在设备数超限时抛 DeviceLimitExceeded"""
    device_id = (device_id or "unknown-device")[:128]
    device = _get_device(db, user.id, device_id)

    # 设备封禁：管理端封禁过或用户自行移除后又被拉黑的设备，直接拒绝登录
    if device is not None and device.is_blocked:
        raise DeviceLimitExceeded(
            "该设备已被禁用，请联系管理员解除",
            [device_dto(device)],
        )

    limit = device_limit(db)
    if device is None and limit and not user.is_staff:
        # R32-F2：上限检查「先 count 后 insert」并发可绕过——两个不同
        # device_id 并发登录都能通过检查，落成 limit+1 台；(user_id, device_id)
        # 唯一约束拦不住不同 device_id。先锁用户行再统计（求片/红包额度同口径）。
        from backend.api.economy import lock_user_row
        lock_user_row(db, user.id)
        current = active_devices(db, user.id)
        if len(current) >= limit:
            if _config_bool(db, "device_limit_auto_evict"):
                # 自动踢掉最久未使用的设备（含吊销其 token）
                oldest = min(current, key=lambda d: d.last_seen_at or datetime.min)
                remove_device(db, user.id, oldest.device_id)
            else:
                raise DeviceLimitExceeded(
                    f"设备数已达上限（{limit} 台），请先在个人中心移除不再使用的设备",
                    [device_dto(d) for d in current],
                )

    now = datetime.now()
    if device is None:
        device = models.UserDevice(
            user_id=user.id, device_id=device_id, name=name, client=client,
            app_version=app_version, ip=ip, first_seen_at=now, last_seen_at=now,
        )
        db.add(device)
    else:
        device.name = name or device.name
        device.client = client or device.client
        device.app_version = app_version or device.app_version
        device.ip = ip or device.ip
        device.last_seen_at = now
    db.commit()
    db.refresh(device)
    return device


def touch_device(
    db: Session, user: models.WebUser, device_id: Optional[str], ip: Optional[str] = None
) -> None:
    """令牌被使用时刷新设备活跃时间（带间隔节流，避免每请求写库）"""
    if not device_id:
        return
    device = _get_device(db, user.id, device_id)
    if device is None:
        return
    now = datetime.now()
    last = device.last_seen_at
    if last is not None and (now - last).total_seconds() < TOUCH_INTERVAL_SECONDS:
        return
    device.last_seen_at = now
    if ip:
        device.ip = ip
    db.commit()


def revoke_device_tokens(db: Session, user_id: int, device_id: str) -> int:
    """仅吊销某设备已签发的令牌（保留设备记录，用于封禁设备踢下线）"""
    from backend.emby_server import models as em

    count = (
        db.query(em.EmbyApiToken)
        .filter(em.EmbyApiToken.user_id == user_id, em.EmbyApiToken.device_id == device_id)
        .update({"is_revoked": True}, synchronize_session=False)
    )
    db.commit()
    return int(count or 0)


def remove_device(db: Session, user_id: int, device_id: str, revoke_tokens: bool = True) -> bool:
    """移除设备；默认同时吊销该设备已签发的 Emby 令牌（即踢下线）"""
    from backend.emby_server import models as em

    device = _get_device(db, user_id, device_id)
    if device is None:
        return False
    if revoke_tokens:
        (
            db.query(em.EmbyApiToken)
            .filter(em.EmbyApiToken.user_id == user_id, em.EmbyApiToken.device_id == device_id)
            .update({"is_revoked": True}, synchronize_session=False)
        )
    db.delete(device)
    db.commit()
    return True
