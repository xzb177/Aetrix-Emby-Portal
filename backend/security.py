"""认证安全工具：密码哈希 + JWT 签发/校验"""
from __future__ import annotations

import hashlib
import logging
import os
import secrets
import threading
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

import bcrypt
from jose import JWTError, jwt

logger = logging.getLogger(__name__)

# JWT 配置。
#
# 重要：模块导入阶段仍保留一个临时值，让 CLI / 测试可以先导入模型；真正启动
# EM/EA 时由 validate_secret_key() fail-closed。这样不会把“随机密钥能启动”误当成
# 生产安全配置，也不会让导入期异常吞掉清晰的启动错误。
_RAW_SECRET_KEY = os.getenv("SECRET_KEY", "").strip()
_ALLOW_EPHEMERAL_SECRET = os.getenv("ALLOW_EPHEMERAL_SECRET", "").strip().lower() in {
    "1", "true", "yes", "on",
}
SECRET_KEY = _RAW_SECRET_KEY or secrets.token_urlsafe(48)
ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = int(os.getenv("ACCESS_TOKEN_EXPIRE_MINUTES", "120"))
REFRESH_TOKEN_EXPIRE_DAYS = int(os.getenv("REFRESH_TOKEN_EXPIRE_DAYS", "30"))


def validate_secret_key() -> str:
    """验证服务启动所需的固定 JWT 密钥，并返回实际密钥。

    生产默认拒绝未配置或过短的密钥。临时随机密钥只能通过显式的
    ``ALLOW_EPHEMERAL_SECRET=true`` 开启，适合一次性本地测试，不应写进生产环境。
    """
    # JWT 签名密钥在导入时固定；运行中改环境变量不能悄悄造成“校验通过但
    # 签名仍用旧值”。同时允许配对检查在测试中临时移除环境变量验证拒绝行为。
    if os.getenv("SECRET_KEY", "").strip() != _RAW_SECRET_KEY:
        raise RuntimeError("SECRET_KEY 在模块导入后发生变化；请使用同一固定密钥重启服务。")
    if not _RAW_SECRET_KEY:
        if _ALLOW_EPHEMERAL_SECRET:
            logger.warning(
                "ALLOW_EPHEMERAL_SECRET 已开启：当前使用临时 JWT 密钥，重启后所有 token 将失效；"
                "仅适用于一次性开发/测试。"
            )
            return SECRET_KEY
        raise RuntimeError(
            "SECRET_KEY 未设置。为避免 JWT 在重启后更换、或使用不可控的临时密钥，"
            "服务拒绝启动。请设置至少 32 字符的随机 SECRET_KEY；"
            "一次性本地测试可显式设置 ALLOW_EPHEMERAL_SECRET=true。"
        )
    if len(_RAW_SECRET_KEY) < 32:
        raise RuntimeError(
            "SECRET_KEY 长度不足 32 字符，服务拒绝启动。"
            "请使用 python3 -c \"import secrets; print(secrets.token_urlsafe(48))\" 生成随机密钥。"
        )
    return _RAW_SECRET_KEY


if _RAW_SECRET_KEY and len(_RAW_SECRET_KEY) < 32:
    logger.warning("SECRET_KEY 长度不足 32 字符；服务启动时将拒绝该配置。")
elif not _RAW_SECRET_KEY and not _ALLOW_EPHEMERAL_SECRET:
    logger.warning("SECRET_KEY 未设置；EM/EA 在启动生命周期阶段将拒绝启动。")


_BCRYPT_SHA256_PREFIX = "$bcrypt-sha256$"


def _bcrypt_input(password: str) -> bytes:
    """把任意长度 UTF-8 密码压到固定长度摘要，再交给 bcrypt。

    bcrypt 原生只看前 72 **字节**；直接截断会让两个不同的长密码变成同一个凭据。
    新哈希使用显式前缀，旧 `$2b$...` 哈希仍按旧规则校验并可在成功登录时升级。
    """
    return hashlib.sha256(password.encode("utf-8")).hexdigest().encode("ascii")


def hash_password(password: str) -> str:
    """生成不会因 bcrypt 72 字节限制而碰撞的 bcrypt 哈希"""
    digest = bcrypt.hashpw(_bcrypt_input(password), bcrypt.gensalt()).decode("ascii")
    return f"{_BCRYPT_SHA256_PREFIX}{digest}"


def verify_password(plain_password: str, hashed_password: str) -> bool:
    """校验新摘要 bcrypt、旧 bcrypt 与历史错误格式；异常一律失败"""
    if not hashed_password:
        return False
    try:
        encoded = hashed_password.encode("ascii")
        if hashed_password.startswith(_BCRYPT_SHA256_PREFIX):
            encoded = hashed_password[len(_BCRYPT_SHA256_PREFIX):].encode("ascii")
            return bcrypt.checkpw(_bcrypt_input(plain_password), encoded)
        # 历史版本将密码按 72 字节截断，必须保留兼容登录路径；成功后由调用方可升级。
        return bcrypt.checkpw(plain_password.encode("utf-8")[:72], encoded)
    except (ValueError, TypeError, UnicodeEncodeError):
        return False


def needs_password_rehash(hashed_password: str) -> bool:
    """是否仍是旧 bcrypt/明文格式，需要在成功认证后升级"""
    return bool(hashed_password) and not hashed_password.startswith(_BCRYPT_SHA256_PREFIX)


def _create_token(data: dict, expires_delta: timedelta, token_type: str) -> str:
    to_encode = data.copy()
    expire = datetime.now(timezone.utc) + expires_delta
    # 每个 JWT 都带随机 jti，便于日志关联与排查。
    to_encode.update({"exp": expire, "type": token_type, "jti": secrets.token_hex(16)})
    return jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)


def create_access_token(user_id: int, extra: Optional[dict[str, Any]] = None,
                        token_version: int = 0) -> str:
    payload = {"sub": str(user_id), "tv": int(token_version or 0)}
    if extra:
        # P3 修复（审查）：extra 在 sub 之后 update 可覆盖 sub，是 footgun。
        # 先剔除保留键，再强制写 sub。
        for reserved in ("sub", "exp", "type", "jti", "tv"):
            extra.pop(reserved, None)
        payload.update(extra)
    return _create_token(payload, timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES), "access")


def create_refresh_token(user_id: int, token_version: int = 0) -> str:
    return _create_token({"sub": str(user_id), "tv": int(token_version or 0)},
                         timedelta(days=REFRESH_TOKEN_EXPIRE_DAYS), "refresh")


def decode_token(token: str, expected_type: Optional[str] = None) -> Optional[dict]:
    """解码并校验 JWT；无效/过期/类型不符返回 None"""
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
    except JWTError:
        return None
    if expected_type and payload.get("type") != expected_type:
        return None
    return payload


# JWT 吊销：进程内布隆式缓存（jti → 是否吊销）。命中缓存直接判，
# 未命中查库。缓存只记"已吊销"（肯定），不记"未吊销"（可能随后被吊销）。
_revoked_cache: set[str] = set()
_revoked_cache_lock = threading.Lock()
_REVOKED_CACHE_MAX = 10000


def is_jti_revoked(db, jti: str | None) -> bool:
    """jti 是否在吊销表里。P2 修复：登出/改密后旧 token 立即失效。"""
    if not jti:
        return False
    with _revoked_cache_lock:
        if jti in _revoked_cache:
            return True
    try:
        from backend.models import RevokedJwt
        row = db.query(RevokedJwt).filter(RevokedJwt.jti == jti).first()
    except Exception:
        return False
    if row is not None:
        with _revoked_cache_lock:
            if len(_revoked_cache) >= _REVOKED_CACHE_MAX:
                _revoked_cache.clear()
            _revoked_cache.add(jti)
        return True
    return False


def revoke_jti(db, jti: str | None, user_id: int, reason: str = "logout") -> None:
    """吊销一个 jti（幂等）。expires_at 按 token 的 exp 来，过期后可清理。"""
    if not jti:
        return
    try:
        from backend.models import RevokedJwt
        from datetime import datetime, timedelta
        # token 最长 30 天（refresh），吊销记录保留 31 天后可清
        expires = datetime.now() + timedelta(days=31)
        db.merge(RevokedJwt(jti=jti, user_id=user_id, reason=reason,
                            expires_at=expires))
        db.commit()
    except Exception:
        db.rollback()
        return
    with _revoked_cache_lock:
        _revoked_cache.add(jti)


def enforce_token_freshness(db, payload: dict | None, user) -> None:
    """Token 新鲜度双检查：jti 吊销表 + token_version 版本号。

    横切能力只许一套：所有 JWT 鉴权依赖（门户 get_current_user_jwt、
    用户端 get_current_user、refresh 换 token）都必须调这里，
    不许各自手写检查，否则会出现"改密后部分接口仍认旧 token"的不一致。
    不通过时抛 HTTPException(401)。
    """
    from fastapi import HTTPException, status
    if not payload:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED,
                            detail="无效或已过期的凭证")
    # P2：登出/改密后被吊销的 jti 立即失效
    if is_jti_revoked(db, payload.get("jti")):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED,
                            detail="凭证已吊销，请重新登录")
    # P3：token 版本号对不上 → 改过密码，旧 token 全部作废（防盗号后持续登录）
    token_tv = int(payload.get("tv", 0) or 0)
    user_tv = int(getattr(user, "token_version", 0) or 0)
    if token_tv != user_tv:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED,
                            detail="密码已变更，请重新登录")


def resolve_jwt_user_id(token: str) -> Optional[int]:
    """从 access token 解出 user_id"""
    payload = decode_token(token, expected_type="access")
    if not payload:
        return None
    try:
        return int(payload.get("sub"))
    except (TypeError, ValueError):
        return None


def revoke_all_user_tokens(db, user_id: int, reason: str = "security") -> None:
    """吊销用户的所有 token（P1 修复 A-P1-1：refresh 重用检测）。

    通过 bump token_version 使该用户所有已签发的 token（含 access 和 refresh）
    立即失效。用于检测到 refresh token 重用（可能的 token 泄露）时的应急响应。
    """
    try:
        from backend.models import WebUser
        user = db.query(WebUser).filter(WebUser.id == user_id).first()
        if user:
            user.token_version = int(user.token_version or 0) + 1
            db.commit()
    except Exception:
        db.rollback()
