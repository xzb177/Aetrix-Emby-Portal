# backend/tg_bind.py
"""
公益服 Telegram 绑定核心模块

职责：
- 维护「公益服功能是否强制绑定 Telegram」的开关与宽限期逻辑；
- 提供 FastAPI 依赖 require_tg_bound，未绑定且不在宽限期则 403；
- 生成/校验 6 位数字绑定码（用户网页端生成码 -> Telegram 私信 Bot 发送码 -> 网页端校验）；
- 查询绑定状态、解绑。

约定：
- 配置读写走 SystemConfig 表；
- 所有写操作不 commit，由调用方提交事务；
- 时间一律使用 naive datetime.now()，与项目一致。
"""

import logging
import secrets
from datetime import datetime, timedelta
from typing import Any

import httpx
from fastapi import Depends, HTTPException
from sqlalchemy.orm import Session

from backend import models
from backend.integrations.store import switch_env_value as _switch_env_value
from backend.database import get_db
from backend.integrations import telegram as tg_integration
from backend.api.user import get_current_user

logger = logging.getLogger(__name__)

# 宽限期天数
GRACE_DAYS = 7
# 绑定码有效期（秒）
CODE_TTL_SECONDS = 600


def _cfg(db: Session, key: str, default: Any) -> Any:
    """读取 SystemConfig，取不到返回默认值。"""
    # A 类摘面板开关（2026-10-21）：env 显式设置即接管，口径见 integrations.store
    _env_v = _switch_env_value(key)
    if _env_v is not None:
        return _env_v
    row = db.query(models.SystemConfig).filter(models.SystemConfig.key == key).first()
    if row is None:
        return default
    return row.value


def _set_cfg(db: Session, key: str, value: Any) -> None:
    """写入 SystemConfig：存在则更新，不存在则插入（不 commit）。"""
    row = db.query(models.SystemConfig).filter(models.SystemConfig.key == key).first()
    if row is None:
        db.add(models.SystemConfig(key=key, value=value))
    else:
        row.value = value


def is_require_enabled(db: Session) -> bool:
    """是否强制绑定 Telegram（welfare_require_tg_bind，默认开启）。"""
    value = str(_cfg(db, "welfare_require_tg_bind", "1")).strip().lower()
    return value in ("1", "true")


def is_guide_enabled(db: Session) -> bool:
    """是否显示 TG 绑定引导页（tg_bind_guide_enabled，默认开启）。"""
    value = str(_cfg(db, "tg_bind_guide_enabled", "1")).strip().lower()
    return value not in ("0", "false", "no", "off")


def get_bot_username(db: Session) -> str:
    """获取 Bot 用户名：优先读缓存，没有则调 getMe 并缓存；失败返回空字符串。"""
    cached = _cfg(db, "telegram_bot_username", "")
    if cached:
        return str(cached)
    token = tg_integration.token(db)
    if not token:
        return ""
    try:
        with httpx.Client(timeout=10.0) as client:
            resp = client.get(f"https://api.telegram.org/bot{token}/getMe")
            data = resp.json()
        if data.get("ok"):
            username = str(data.get("result", {}).get("username", ""))
            if username:
                _set_cfg(db, "telegram_bot_username", username)
            return username
    except Exception as exc:  # noqa: BLE001
        logger.warning("getMe 调用失败：%s", exc)
    return ""


def get_launch_at(db: Session) -> datetime:
    """读取/初始化绑定功能上线时间（tg_bind_launch_at）。"""
    raw = _cfg(db, "tg_bind_launch_at", "")
    if raw:
        try:
            return datetime.fromisoformat(str(raw))
        except (TypeError, ValueError):
            logger.warning("tg_bind_launch_at 格式非法：%r", raw)
    now = datetime.now()
    _set_cfg(db, "tg_bind_launch_at", now.isoformat())
    return now


def in_grace_period(db: Session, user: models.WebUser) -> bool:
    """判断用户是否处于宽限期（上线前注册的老用户，上线起 7 天内放行）。"""
    launch_at = get_launch_at(db)
    created_at = user.created_at
    # created_at 为空按老用户处理
    is_old_user = created_at is None or created_at < launch_at
    if not is_old_user:
        return False
    return datetime.now() < launch_at + timedelta(days=GRACE_DAYS)


def require_tg_bound(
    current_user: models.WebUser = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> models.WebUser:
    """公益服写接口门禁：未绑定 Telegram 且不在宽限期则 403。"""
    if not is_require_enabled(db):
        return current_user
    if current_user.telegram_id:
        return current_user
    if in_grace_period(db, current_user):
        return current_user
    raise HTTPException(
        status_code=403,
        detail={"code": "TG_NOT_BOUND", "message": "公益服功能需要绑定 Telegram，防小号，1 分钟搞定"},
    )


def generate_bind_code(db: Session, user: models.WebUser) -> dict:
    """生成 6 位绑定码（10 分钟有效），作废该用户旧的未使用码。不 commit。"""
    now = datetime.now()
    db.query(models.TgBindCode).filter(
        models.TgBindCode.user_id == user.id,
        models.TgBindCode.used_at.is_(None),
    ).update({models.TgBindCode.used_at: now}, synchronize_session=False)
    code = str(100000 + secrets.randbelow(900000))  # CSPRNG，不可预测
    db.add(models.TgBindCode(
        user_id=user.id,
        code=code,
        expires_at=now + timedelta(seconds=CODE_TTL_SECONDS),
    ))
    return {"code": code, "expires_in": CODE_TTL_SECONDS, "bot_username": get_bot_username(db)}


def _latest_code_expired_by_attempts(db: Session, user: models.WebUser) -> bool:
    """该用户最新的绑定码是否因全站猜错次数超限被作废（防分布式枚举）。"""
    from backend.tg_bot.handlers import BIND_GLOBAL_FAIL_MAX

    latest = (
        db.query(models.TgBindCode)
        .filter(models.TgBindCode.user_id == user.id)
        .order_by(models.TgBindCode.id.desc())
        .first()
    )
    return bool(
        latest is not None
        and latest.used_at is None
        and (latest.fail_count or 0) >= BIND_GLOBAL_FAIL_MAX
    )


_CODE_EXPIRED_RESULT = {
    "success": False,
    "code_expired": True,
    "message": "检测到大量异常绑定尝试，你的绑定码已失效，请重新生成绑定码后再发送给 Bot",
}


def verify_bind(db: Session, user: models.WebUser) -> dict:
    """用户给 Bot 发送绑定码后，调 getUpdates 扫码验证。成功则写 telegram_id。不 commit。"""
    if not user.telegram_id and _latest_code_expired_by_attempts(db, user):
        return dict(_CODE_EXPIRED_RESULT)
    from backend.tg_bot import poller
    if poller.is_active(db):
        # Bot 长轮询在跑：绑定码由 poller 的绑定处理器消费并直接写库，
        # 这里绝不能再调 getUpdates（Telegram 409 + 抢走 poller 的 update），只读库状态。
        if user.telegram_id:
            return {"success": True, "telegram_id": user.telegram_id}
        return {"success": False, "message": "还没收到你发送的绑定码，请先私聊 Bot 发送 6 位数字码，几秒后再点验证"}
    token = tg_integration.token(db)
    if not token:
        raise ValueError("站点尚未配置 Telegram Bot，请联系管理员")
    try:
        offset = int(_cfg(db, "tg_bind_updates_offset", "0") or "0")
    except (TypeError, ValueError):
        offset = 0
    try:
        with httpx.Client(timeout=10.0) as client:
            resp = client.get(
                f"https://api.telegram.org/bot{token}/getUpdates",
                params={"offset": offset, "timeout": 0, "limit": 100},
            )
            body = resp.json()
    except Exception as exc:  # noqa: BLE001
        raise ValueError(f"Telegram 不可达：{type(exc).__name__}: {exc}")
    if not body.get("ok"):
        raise ValueError(f"Telegram API 错误：{body.get('description') or 'unknown'}")
    now = datetime.now()
    max_update_id = offset - 1
    matched = None
    for upd in body.get("result") or []:
        uid = upd.get("update_id")
        if isinstance(uid, int) and uid > max_update_id:
            max_update_id = uid
        msg = upd.get("message") or {}
        text = str(msg.get("text") or "").strip()
        from_id = (msg.get("from") or {}).get("id")
        if not text or not isinstance(from_id, int):
            continue
        rec = db.query(models.TgBindCode).filter(
            models.TgBindCode.user_id == user.id,
            models.TgBindCode.code == text,
            models.TgBindCode.used_at.is_(None),
            models.TgBindCode.expires_at > now,
        ).first()
        if rec:
            matched = (rec, from_id)
            break
    _set_cfg(db, "tg_bind_updates_offset", str(max_update_id + 1))
    if not matched:
        return {"success": False, "message": "未找到你发送的绑定码，请先给 Bot 发送 6 位数字码，再点验证"}
    rec, from_id = matched
    occupied = db.query(models.WebUser).filter(
        models.WebUser.telegram_id == from_id,
        models.WebUser.id != user.id,
    ).first()
    if occupied:
        raise ValueError("该 Telegram 账号已被其他用户绑定")
    user.telegram_id = from_id
    rec.used_at = now
    return {"success": True, "telegram_id": from_id}


def get_bind_status(db: Session, user: models.WebUser) -> dict:
    """绑定状态查询（读接口，不加门禁）。"""
    bound = bool(user.telegram_id)
    return {
        "bound": bound,
        "telegram_id": user.telegram_id,
        "required": is_require_enabled(db),
        "guide_enabled": is_guide_enabled(db),
        "in_grace": (not bound) and in_grace_period(db, user),
        "bot_username": get_bot_username(db),
    }


def unbind(db: Session, user: models.WebUser) -> None:
    """解绑 Telegram。不 commit。"""
    user.telegram_id = None
