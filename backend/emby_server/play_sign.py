"""播放短期签名 / token 有效期 / 防盗链。

三件事收拢在一个模块，避免横切能力散落各处：

1. **播放 URL 短期签名**（P0 安全加固）：
   ``HMAC-SHA256(SECRET_KEY, "user_id:item_id:exp")``，默认 15 分钟有效。
   ``PlaybackInfo`` 同时发放 ``api_key``（老客户端）与 ``uid/exp/sign``（新）——
   双轨并行，逐步迁移。播放端点优先验签：签名在且无效/过期 → 403；
   无签时回退原有 token/JWT 鉴权，老客户端不受影响。

2. **token 有效期**：
   新签发的 ``emby_api_tokens`` 默认 30 天有效（``expires_at``）；
   校验时过期视为无效，要求重新登录；历史 token 的 ``expires_at`` 为空
   视为永不过期（不强制全员重新登录）。janitor 定期清理过期行。

3. **防盗链**：
   ``SystemConfig[play_allowed_referers]`` 存逗号分隔的域名白名单，
   默认空 = 不检查。仅当请求带了 ``Referer`` 且主机不在白名单时 403；
   原生客户端（Infuse/VLC 等）一般不带 Referer，放行并在文档注明。
"""
from __future__ import annotations

import hashlib
import hmac
import logging
import time
from datetime import datetime, timedelta
from typing import Optional
from urllib.parse import urlparse

from fastapi import HTTPException, Request
from sqlalchemy.orm import Session

from backend.integrations import store
from backend.models import SystemConfig

logger = logging.getLogger(__name__)

# 播放签名默认有效期：15 分钟
SIGN_TTL_SECONDS = 900
# 新签发 token 默认有效期：30 天
TOKEN_TTL_DAYS = 30
# 防盗链白名单配置键（SystemConfig）
CONFIG_ALLOWED_REFERERS = "play_allowed_referers"

# --- 防盗链配置（SystemConfig + store 热读，风格同 backend/emby_server/cdn.py） ---
CONFIG_HOTLINK_ENABLED = "hotlink_protection_enabled"
CONFIG_PLAY_SIGN_TTL = "play_sign_ttl_seconds"

DESCRIPTIONS = {
    CONFIG_HOTLINK_ENABLED: "防盗链总开关：关闭后播放端点跳过播放签名校验（兼容老客户端）",
    CONFIG_PLAY_SIGN_TTL: "播放签名有效期（秒），范围 60~86400",
}

HOTLINK_DEFAULT = True
PLAY_TTL_DEFAULT = 900
PLAY_TTL_MIN, PLAY_TTL_MAX = 60, 86400

_TRUTHY = {"true", "1", "yes", "on"}
_FALSY = {"false", "0", "no", "off"}


def _clamp_ttl(raw, lo: int, hi: int, default: int) -> int:
    if raw is None:
        return default
    try:
        value = int(str(raw).strip())
    except (TypeError, ValueError):
        return default
    return max(lo, min(hi, value))


def hotlink_enabled(db: Session | None = None) -> bool:
    """防盗链总开关，默认开启（db 为 None / 读失败 / 非法值均回落默认启用=现状）。

    播放链路调用：配置表读不到时 fail-open（放行），播放不能因为配置故障全挂。
    """
    if db is None:
        return HOTLINK_DEFAULT
    try:
        raw = store.get_value(db, CONFIG_HOTLINK_ENABLED)
    except Exception as exc:  # noqa: BLE001
        logger.warning("防盗链开关读取失败，fail-open 默认启用: %s", exc)
        return HOTLINK_DEFAULT
    if raw is None:
        return HOTLINK_DEFAULT
    value = str(raw).strip().lower()
    if value in _TRUTHY:
        return True
    if value in _FALSY:
        return False
    return HOTLINK_DEFAULT


def play_sign_ttl_seconds(db: Session | None = None) -> int:
    """播放签名有效期（秒），热读 SystemConfig，非法值钳制到 60~86400。"""
    if db is None:
        return PLAY_TTL_DEFAULT
    try:
        raw = store.get_value(db, CONFIG_PLAY_SIGN_TTL)
    except Exception as exc:  # noqa: BLE001
        logger.warning("播放签名 TTL 读取失败，用默认值: %s", exc)
        return PLAY_TTL_DEFAULT
    return _clamp_ttl(raw, PLAY_TTL_MIN, PLAY_TTL_MAX, PLAY_TTL_DEFAULT)


def config_payload(db: Session | None = None) -> dict:
    """管理后台读取：当前值 + 默认值（前端卡片用）。"""
    return {
        "enabled": hotlink_enabled(db),
        "play_sign_ttl": play_sign_ttl_seconds(db),
        # 配置瘦身第 3c 批：防盗链 Referer 白名单回显（归一化后域名列表，空=不检查）
        "allowed_referers": get_allowed_referers(db),
        "defaults": {
            "enabled": HOTLINK_DEFAULT,
            "play_sign_ttl": PLAY_TTL_DEFAULT,
        },
    }


def write_config(db: Session, *, enabled: bool, play_sign_ttl: int) -> dict:
    """管理后台写入：校验范围 → 逐键 upsert SystemConfig（含 description）→ 失效热缓存。"""
    try:
        play_sign_ttl = int(play_sign_ttl)
    except (TypeError, ValueError):
        raise ValueError("播放签名有效期必须为整数秒")
    if not (PLAY_TTL_MIN <= play_sign_ttl <= PLAY_TTL_MAX):
        raise ValueError(f"播放签名有效期需在 {PLAY_TTL_MIN}~{PLAY_TTL_MAX} 秒之间")

    enabled = bool(enabled)
    values = (
        (CONFIG_HOTLINK_ENABLED, "true" if enabled else "false"),
        (CONFIG_PLAY_SIGN_TTL, str(play_sign_ttl)),
    )
    for key, value in values:
        row = db.query(SystemConfig).filter(SystemConfig.key == key).first()
        if row is None:
            db.add(SystemConfig(key=key, value=value, description=DESCRIPTIONS[key]))
        else:
            row.value = value
            row.description = DESCRIPTIONS[key]
    db.commit()
    for key, _ in values:
        store.invalidate(key)
    return config_payload(db)


def _signing_key() -> bytes:
    """签名密钥：复用面板 SECRET_KEY（EA 与 API 容器间已同步同一密钥）。"""
    from backend.security import SECRET_KEY

    return SECRET_KEY.encode("utf-8")


def _sign_payload(user_id: int, item_id: str, exp: int) -> str:
    msg = f"{user_id}:{item_id}:{exp}".encode("utf-8")
    return hmac.new(_signing_key(), msg, hashlib.sha256).hexdigest()[:32]


def make_play_sign(user_id: int, item_id: str, exp: int) -> str:
    """生成播放签名（32 位 hex）。"""
    return _sign_payload(user_id, item_id, int(exp))


def issue_play_sign(user_id: int, item_id: str,
                    ttl_seconds: int = SIGN_TTL_SECONDS) -> tuple[int, str]:
    """签发一次播放签名，返回 ``(exp, sign)``。纯计算，不碰 DB。"""
    exp = int(time.time()) + int(ttl_seconds)
    return exp, make_play_sign(user_id, item_id, exp)


def verify_play_sign(user_id: int, item_id: str, exp: int, sign: str) -> bool:
    """校验播放签名：过期或对不上都返回 False（由调用方转 403）。"""
    try:
        exp = int(exp)
        user_id = int(user_id)
    except (TypeError, ValueError):
        return False
    if exp <= int(time.time()):
        return False
    if not isinstance(sign, str) or len(sign) != 32 or not sign.isascii():
        return False
    expected = _sign_payload(user_id, str(item_id), exp)
    return hmac.compare_digest(expected, sign)


def token_is_usable(row) -> bool:
    """token 行是否可用：未吊销 + 未过期。

    ``expires_at`` 为空 = 历史 token，视为永不过期（不强制老客户端重登）。
    """
    if row is None:
        return False
    if getattr(row, "is_revoked", False):
        return False
    exp = getattr(row, "expires_at", None)
    if exp is not None and exp <= datetime.now():
        return False
    return True


def token_expiry_default() -> datetime:
    """新签发 token 的默认过期时间。"""
    return datetime.now() + timedelta(days=TOKEN_TTL_DAYS)


def purge_expired_tokens(db: Session) -> int:
    """删除已过期的 token 行，返回清理条数（janitor 定期调用）。"""
    from backend.emby_server import models as em

    now = datetime.now()
    rows = (
        db.query(em.EmbyApiToken)
        .filter(em.EmbyApiToken.expires_at.isnot(None),
                em.EmbyApiToken.expires_at < now)
        .all()
    )
    count = len(rows)
    for row in rows:
        db.delete(row)
    if count:
        db.commit()
        logger.info("清理过期 Emby token %d 条", count)
    return count


def _normalize_host(raw: str) -> Optional[str]:
    raw = (raw or "").strip().lower()
    if not raw:
        return None
    # 允许填完整 URL（https://cdn.example.com/x）或裸域名
    if "://" in raw:
        host = urlparse(raw).hostname
    else:
        host = raw.split("/")[0].split(":")[0]
    host = (host or "").strip().rstrip(".")
    if not host or any(ch.isspace() for ch in host):
        return None
    return host


def get_allowed_referers(db: Session) -> list:
    """读防盗链白名单（逗号分隔域名），默认空列表 = 不检查。"""
    from backend.models import SystemConfig

    row = db.query(SystemConfig).filter(
        SystemConfig.key == CONFIG_ALLOWED_REFERERS).first()
    if not row or not (row.value or "").strip():
        return []
    hosts = []
    for part in row.value.split(","):
        host = _normalize_host(part)
        if host and host not in hosts:
            hosts.append(host)
    return hosts


def write_allowed_referers(db: Session, raw: str) -> list:
    """写防盗链白名单（管理后台用）：做一遍域名归一化后落库，返回生效列表。"""
    from backend.models import SystemConfig

    hosts = []
    for part in (raw or "").split(","):
        host = _normalize_host(part)
        if host and host not in hosts:
            hosts.append(host)
    value = ",".join(hosts)
    row = db.query(SystemConfig).filter(
        SystemConfig.key == CONFIG_ALLOWED_REFERERS).first()
    if row:
        row.value = value
    else:
        db.add(SystemConfig(key=CONFIG_ALLOWED_REFERERS, value=value,
                            description="播放防盗链 Referer 白名单（逗号分隔域名），空=不检查"))
    db.commit()
    return hosts


def check_referer(request: Request, db: Session) -> None:
    """防盗链检查：白名单为空直接放行；Referer 缺失放行（原生客户端兼容）；

    仅当 Referer 存在且主机不在白名单时抛 403。
    配置读失败时 fail-open（放行）：播放链路不能因为配置表读不到就全挂。
    （防盗链只是附加防护；条目可见性校验 _library_scope 读失败则是 fail-closed。）
    """
    try:
        whitelist = get_allowed_referers(db)
    except Exception:  # noqa: BLE001
        logger.warning("防盗链白名单读取失败，fail-open 放行", exc_info=True)
        return
    if not whitelist:
        return
    referer = request.headers.get("referer")
    if not referer:
        # Infuse / VLC / nPlayer 等原生客户端一般不带 Referer，不能一刀切拦掉。
        return
    host = (urlparse(referer).hostname or "").lower().rstrip(".")
    if host in whitelist:
        return
    logger.warning("防盗链拦截: referer=%s 不在白名单 path=%s",
                   referer[:120], request.url.path)
    raise HTTPException(status_code=403, detail="Referer 不在白名单")
