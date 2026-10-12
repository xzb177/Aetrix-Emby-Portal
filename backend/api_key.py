"""API Key / Service Token 机制（服务端对服务端认证）

第三方系统用长期有效的 API Key 调用受保护接口，不走用户登录 JWT。

安全设计：
- 明文只在创建时返回一次，库里只存 ``SHA256(明文 + pepper)``
- pepper：环境变量 ``API_KEY_PEPPER``，未设置时由 ``SECRET_KEY``
  单向派生（``HMAC-SHA256(SECRET_KEY, "aetrix-api-key")``），泄露派生值
  不能反推 ``SECRET_KEY``（参考 backend/node_auth.py 模式）
- 明文格式：``ak_`` + 48 位 URL 安全随机串
- scope 层级：``admin`` > ``write`` > ``read``（高包含低）
"""
from __future__ import annotations

import hashlib
import hmac
import logging
import os
import secrets
from datetime import datetime, timedelta
from typing import Optional

from fastapi import Depends, HTTPException, Request
from sqlalchemy.orm import Session

from backend import models
from backend.database import get_db

logger = logging.getLogger(__name__)

# 请求头名
API_KEY_HEADER = "X-API-Key"

# 阈值/行为全部走环境变量，不写死业务：
# - API_KEY_SCOPES：允许的 scope 名单，逗号分隔，顺序即层级（靠后包含靠前）。
#   默认 "read,write,admin"：admin > write > read。
# - API_KEY_MAX_EXPIRE_DAYS：创建/修改时允许的最大有效期（天），默认 3650（约 10 年）。
_SCOPES_ENV = "API_KEY_SCOPES"
_DEFAULT_SCOPES = ("read", "write", "admin")
_MAX_EXPIRE_ENV = "API_KEY_MAX_EXPIRE_DAYS"
_DEFAULT_MAX_EXPIRE_DAYS = 3650

_PEPPER_ENV = "API_KEY_PEPPER"
_DERIVE_LABEL = b"aetrix-api-key"


def allowed_scopes() -> tuple[str, ...]:
    """允许的 scope 名单（顺序即层级：靠后包含靠前）。每次调用都读环境变量，
    运维改配置无需重启进程即可对新创建的 Key 生效。"""
    raw = (os.getenv(_SCOPES_ENV) or "").strip()
    if not raw:
        return _DEFAULT_SCOPES
    scopes = tuple(s.strip().lower() for s in raw.split(",") if s.strip())
    return scopes or _DEFAULT_SCOPES


def max_expire_days() -> int:
    """允许的最大有效期（天）。"""
    try:
        return max(1, int(os.getenv(_MAX_EXPIRE_ENV) or _DEFAULT_MAX_EXPIRE_DAYS))
    except (TypeError, ValueError):
        return _DEFAULT_MAX_EXPIRE_DAYS


def _scope_rank() -> dict[str, int]:
    return {name: rank for rank, name in enumerate(allowed_scopes(), start=1)}


# 兼容旧引用：模块级常量保留为默认值快照，新代码请用 allowed_scopes()
ALLOWED_SCOPES = _DEFAULT_SCOPES


def _pepper() -> str:
    """API Key 哈希 pepper：显式配置优先，否则由 SECRET_KEY 单向派生"""
    explicit = (os.getenv(_PEPPER_ENV) or "").strip()
    if explicit:
        return explicit
    root = (os.getenv("SECRET_KEY") or "").strip()
    if not root:
        return ""
    return hmac.new(root.encode("utf-8"), _DERIVE_LABEL, hashlib.sha256).hexdigest()


def _hash_key(plaintext: str) -> str:
    pepper = _pepper()
    return hashlib.sha256(f"{plaintext}{pepper}".encode("utf-8")).hexdigest()


def generate_plaintext() -> tuple[str, str]:
    """生成 (key_id, 明文)。key_id 是公开标识，可安全展示/索引。"""
    rand = secrets.token_urlsafe(36)  # ~48 字符
    plaintext = f"ak_{rand}"
    key_id = plaintext[:12]  # "ak_" + 9 字符，公开前缀
    return key_id, plaintext


def create_api_key(
    db: Session,
    name: str,
    scopes: list[str],
    expires_days: Optional[int] = None,
    created_by: Optional[int] = None,
) -> tuple["models.ApiKey", str]:
    """创建 API Key。返回 (ApiKey, 明文)——明文只返回这一次，调用方必须立即展示给用户。"""
    allowed = allowed_scopes()
    clean_scopes = [s for s in (scopes or []) if s in allowed]
    if not clean_scopes:
        raise ValueError(f"scopes 不能为空，且必须在允许名单内：{', '.join(allowed)}")
    key_id, plaintext = generate_plaintext()
    # key_id 极小概率碰撞，循环重试
    for _ in range(5):
        if not db.query(models.ApiKey).filter(models.ApiKey.key_id == key_id).first():
            break
        key_id, plaintext = generate_plaintext()
    expires_at = None
    if expires_days and expires_days > 0:
        expires_at = datetime.now() + timedelta(days=expires_days)
    rec = models.ApiKey(
        key_id=key_id,
        key_hash=_hash_key(plaintext),
        name=(name or "").strip()[:100] or "未命名",
        scopes=clean_scopes,
        expires_at=expires_at,
        is_active=True,
        created_by=created_by,
    )
    db.add(rec)
    db.commit()
    db.refresh(rec)
    return rec, plaintext


def verify_api_key(db: Session, plaintext: str) -> Optional["models.ApiKey"]:
    """验证明文 key。返回 ApiKey 或 None（失败原因不区分，防枚举）。"""
    if not plaintext or not plaintext.startswith("ak_"):
        return None
    key_id = plaintext[:12]
    candidates = db.query(models.ApiKey).filter(
        models.ApiKey.key_id == key_id,
        models.ApiKey.is_active == True,  # noqa: E712
    ).all()
    want = _hash_key(plaintext)
    for rec in candidates:
        if hmac.compare_digest(rec.key_hash or "", want):
            if rec.expires_at and rec.expires_at < datetime.now():
                return None
            return rec
    return None


def touch_last_used(db: Session, rec: "models.ApiKey") -> None:
    """更新最后使用时间。失败不影响主流程（吞异常记日志）。"""
    try:
        rec.last_used_at = datetime.now()
        db.commit()
    except Exception:  # noqa: BLE001
        db.rollback()
        logger.warning("api_key touch_last_used 失败: %s", rec.key_id, exc_info=True)


def has_scope(rec: "models.ApiKey", required: str) -> bool:
    """scope 层级检查：名单靠后的包含靠前的（默认 admin > write > read）。

    名单外的 scope 一律按不满足处理（fail closed）——key 的 scope 只能来自
    允许名单，不存在名单外的 scope 能命中的情况。"""
    rank = _scope_rank()
    need = rank.get(required)
    if need is None:
        return False
    have = max((rank.get(s, 0) for s in (rec.scopes or [])), default=0)
    return have >= need


def revoke_api_key(db: Session, key_id: str) -> bool:
    """吊销（软删除）。返回是否找到。"""
    rec = db.query(models.ApiKey).filter(models.ApiKey.key_id == key_id).first()
    if not rec:
        return False
    rec.is_active = False
    db.commit()
    return True


def get_api_key_from_request(request: Request, db: Session) -> Optional["models.ApiKey"]:
    """从 X-API-Key 头解析并验证。供 Depends 依赖使用。"""
    plaintext = (request.headers.get(API_KEY_HEADER) or "").strip()
    if not plaintext:
        return None
    rec = verify_api_key(db, plaintext)
    if rec is not None:
        touch_last_used(db, rec)
    return rec


def require_api_key(*required_scopes: str):
    """FastAPI 依赖工厂：要求有效的 API Key 且 scope 足够。

    用法：``@router.get("/x", dependencies=[Depends(require_api_key("read"))])``
    或 ``rec: models.ApiKey = Depends(require_api_key("read"))``。
    """
    allowed = allowed_scopes()
    for s in required_scopes:
        if s not in allowed:
            raise ValueError(f"未知 scope: {s}（允许: {', '.join(allowed)}）")

    def _dep(request: Request, db: Session = Depends(get_db)) -> "models.ApiKey":
        rec = get_api_key_from_request(request, db)
        if rec is None:
            raise HTTPException(status_code=401, detail="API Key 无效、已过期或已吊销")
        for s in required_scopes:
            if not has_scope(rec, s):
                raise HTTPException(status_code=403, detail=f"API Key 缺少 scope: {s}")
        return rec

    return _dep
