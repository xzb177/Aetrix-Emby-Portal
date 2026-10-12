"""管理后台 · API Key / Service Token 管理

第三方系统用长期有效的 API Key 调用受保护接口（服务端对服务端认证）。

- **创建**：明文只在创建响应里返回一次，前端弹窗展示并提示立即复制；
  库里只存 SHA256(明文 + pepper)，见 ``backend/api_key.py``
- **列表**：只展示 key_id 掩码，绝不返回明文
- **吊销**：软删除（is_active=False），吊销后立即失效
- 鉴权口径：复用 ``get_current_admin`` + JWT（管理员才能管 Key）
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from backend import api_key as api_key_core
from backend import models
from backend.api.admin_core import _audit, get_current_admin
from backend.database import get_db

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/admin", tags=["管理后台·API Key"])


def _mask_key_id(key_id: str) -> str:
    """key_id 掩码展示：ak_xxxxxx••••（前 8 位可见）"""
    if not key_id:
        return ""
    return key_id[:8] + "••••" if len(key_id) > 8 else key_id


def _key_to_dict(rec: models.ApiKey) -> dict:
    return {
        "id": rec.id,
        "key_id": rec.key_id,
        "key_id_masked": _mask_key_id(rec.key_id),
        "name": rec.name,
        "scopes": list(rec.scopes or []),
        "expires_at": rec.expires_at.isoformat() if rec.expires_at else None,
        "is_active": bool(rec.is_active),
        "last_used_at": rec.last_used_at.isoformat() if rec.last_used_at else None,
        "created_at": rec.created_at.isoformat() if rec.created_at else None,
    }


class ApiKeyCreateRequest(BaseModel):
    name: str = Field("", max_length=100)
    scopes: list[str] = Field(default_factory=lambda: ["read"])
    # 有效期天数：上限走环境变量 API_KEY_MAX_EXPIRE_DAYS（默认 3650），在 handler 里校验
    expires_days: Optional[int] = Field(default=None, ge=1)


class ApiKeyUpdateRequest(BaseModel):
    name: Optional[str] = Field(default=None, max_length=100)
    scopes: Optional[list[str]] = None
    expires_days: Optional[int] = Field(default=None, ge=1)
    is_active: Optional[bool] = None


def _check_scopes(scopes: list[str]) -> None:
    allowed = api_key_core.allowed_scopes()
    bad = [s for s in (scopes or []) if s not in allowed]
    if bad:
        raise HTTPException(
            status_code=400,
            detail=f"未知 scope: {', '.join(bad)}（允许: {', '.join(allowed)}）",
        )


def _check_expires_days(expires_days: Optional[int]) -> None:
    if expires_days is None:
        return
    limit = api_key_core.max_expire_days()
    if expires_days > limit:
        raise HTTPException(
            status_code=400,
            detail=f"有效期不能超过 {limit} 天（API_KEY_MAX_EXPIRE_DAYS）",
        )


@router.post("/api-keys")
def create_api_key(
    req: ApiKeyCreateRequest,
    db: Session = Depends(get_db),
    current_admin: models.WebUser = Depends(get_current_admin),
):
    """创建 API Key。**明文 key 只在本次响应返回**，请立即复制保存。"""
    if not req.name or not req.name.strip():
        raise HTTPException(status_code=400, detail="名称不能为空")
    _check_scopes(req.scopes)
    _check_expires_days(req.expires_days)
    try:
        rec, plaintext = api_key_core.create_api_key(
            db,
            name=req.name.strip(),
            scopes=req.scopes,
            expires_days=req.expires_days,
            created_by=getattr(current_admin, "id", None),
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    _audit(db, current_admin, "create_api_key", "api_key", rec.id,
           {"key_id": rec.key_id, "name": rec.name, "scopes": rec.scopes})
    db.commit()  # _audit 只 add 不提交，这里必须落盘，否则审计丢了
    out = _key_to_dict(rec)
    out["key"] = plaintext  # 仅此一次返回明文
    return out


@router.get("/api-keys")
def list_api_keys(
    db: Session = Depends(get_db),
    current_admin: models.WebUser = Depends(get_current_admin),
):
    """API Key 列表（不返回明文）。"""
    recs = db.query(models.ApiKey).order_by(models.ApiKey.id.desc()).all()
    return {"items": [_key_to_dict(r) for r in recs], "total": len(recs)}


@router.put("/api-keys/{key_id}")
def update_api_key(
    key_id: str,
    req: ApiKeyUpdateRequest,
    db: Session = Depends(get_db),
    current_admin: models.WebUser = Depends(get_current_admin),
):
    """改名 / 改 scope / 改过期 / 启用停用。"""
    rec = db.query(models.ApiKey).filter(models.ApiKey.key_id == key_id).first()
    if not rec:
        raise HTTPException(status_code=404, detail="API Key 不存在")
    changed: dict = {}
    if req.name is not None:
        if not req.name.strip():
            raise HTTPException(status_code=400, detail="名称不能为空")
        rec.name = req.name.strip()[:100]
        changed["name"] = rec.name
    if req.scopes is not None:
        _check_scopes(req.scopes)
        if not req.scopes:
            raise HTTPException(status_code=400, detail="scopes 不能为空")
        rec.scopes = list(req.scopes)
        changed["scopes"] = rec.scopes
    if req.expires_days is not None:
        _check_expires_days(req.expires_days)
        from datetime import timedelta
        rec.expires_at = datetime.now() + timedelta(days=req.expires_days)
        changed["expires_days"] = req.expires_days
    if req.is_active is not None:
        rec.is_active = bool(req.is_active)
        changed["is_active"] = rec.is_active
    db.commit()
    _audit(db, current_admin, "update_api_key", "api_key", rec.id,
           {"key_id": key_id, **changed})
    db.commit()  # _audit 只 add 不提交
    return _key_to_dict(rec)


@router.delete("/api-keys/{key_id}")
def revoke_api_key(
    key_id: str,
    db: Session = Depends(get_db),
    current_admin: models.WebUser = Depends(get_current_admin),
):
    """吊销 API Key（立即失效，不可恢复）。"""
    ok = api_key_core.revoke_api_key(db, key_id)
    if not ok:
        raise HTTPException(status_code=404, detail="API Key 不存在")
    _audit(db, current_admin, "revoke_api_key", "api_key", None, {"key_id": key_id})
    db.commit()  # _audit 只 add 不提交
    return {"ok": True, "key_id": key_id}


@router.get("/api-keys/verify")
def verify_api_key_endpoint(
    rec: models.ApiKey = Depends(api_key_core.require_api_key("read")),
):
    """连通性测试：第三方用 X-API-Key 头调用，验证 key 有效且 scope 足够。

    注意：此端点用 API Key 鉴权（不是管理员 JWT），供第三方接入自测。
    """
    return {
        "ok": True,
        "key_id": rec.key_id,
        "name": rec.name,
        "scopes": list(rec.scopes or []),
    }
