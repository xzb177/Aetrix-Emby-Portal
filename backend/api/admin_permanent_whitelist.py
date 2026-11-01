"""
管理后台 · 永久账号白名单

名单本体在数据表 ``permanent_whitelist_entries``，判定单口在
``backend/permanent_whitelist.py``；本文件只做运营面：名单列表/搜索/
添加/移除 + 总开关读写（开关保存走 config_service 统一校验通道，
schema 家族 ``permanent_whitelist``）。

为什么单独成文件：同 admin_player_blacklist.py 的理由——backend/api/admin.py
已经很大，新功能独立成文件、鉴权口径一致（get_current_admin + _audit）。
"""
from __future__ import annotations

import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy import or_
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from backend import config_service, models, permanent_whitelist as pw
from backend.api.admin_core import _audit, get_current_admin
from backend.config_schema import ConfigValueError
from backend.database import get_db

logger = logging.getLogger(__name__)

admin_permanent_whitelist_router = APIRouter(
    prefix="/api/admin/permanent-whitelist", tags=["管理后台·永久账号白名单"])


def _entry_payload(db: Session, entry: models.PermanentWhitelistEntry) -> dict:
    user = entry.user
    adder = None
    if entry.added_by:
        adder = db.query(models.WebUser).filter(
            models.WebUser.id == entry.added_by).first()
    return {
        "id": entry.id,
        "user_id": entry.user_id,
        "username": user.username if user else None,
        "email": user.email if user else None,
        "is_welfare": bool(user.is_welfare) if user else False,
        "note": entry.note,
        "added_by": entry.added_by,
        "added_by_name": adder.username if adder else None,
        "created_at": entry.created_at.isoformat() if entry.created_at else None,
    }


@admin_permanent_whitelist_router.get("/entries")
def list_entries(
    current_admin: models.WebUser = Depends(get_current_admin),
    db: Session = Depends(get_db),
):
    """名单全表（按添加时间倒序）+ 总开关当前值。"""
    entries = (
        db.query(models.PermanentWhitelistEntry)
        .order_by(models.PermanentWhitelistEntry.created_at.desc(),
                  models.PermanentWhitelistEntry.id.desc())
        .all()
    )
    return {
        "enabled": pw.whitelist_enabled(db),
        "entries": [_entry_payload(db, e) for e in entries],
    }


@admin_permanent_whitelist_router.get("/search")
def search_users(
    q: str = "",
    current_admin: models.WebUser = Depends(get_current_admin),
    db: Session = Depends(get_db),
):
    """按用户名/邮箱模糊搜，或按纯数字 ID 精确找（给添加名单时的选人框用）。"""
    text = (q or "").strip()
    if not text:
        return {"users": []}
    query = db.query(models.WebUser)
    if text.isdigit():
        query = query.filter(models.WebUser.id == int(text))
    else:
        like = f"%{text}%"
        query = query.filter(or_(models.WebUser.username.ilike(like),
                                 models.WebUser.email.ilike(like)))
    users = query.order_by(models.WebUser.id.asc()).limit(20).all()
    listed = {
        r[0] for r in db.query(models.PermanentWhitelistEntry.user_id)
        .filter(models.PermanentWhitelistEntry.user_id.in_([u.id for u in users]))
        .all()
    } if users else set()
    return {
        "users": [
            {
                "id": u.id,
                "username": u.username,
                "email": u.email,
                "is_welfare": bool(u.is_welfare),
                "already_listed": u.id in listed,
            }
            for u in users
        ]
    }


class AddEntryRequest(BaseModel):
    user_id: int
    note: Optional[str] = None


@admin_permanent_whitelist_router.post("/entries")
def add_entry(
    request: AddEntryRequest,
    raw_request: Request,
    current_admin: models.WebUser = Depends(get_current_admin),
    db: Session = Depends(get_db),
):
    """把一个用户加入永久名单：用户不存在 400，已在名单 409。"""
    user = db.query(models.WebUser).filter(
        models.WebUser.id == request.user_id).first()
    if user is None:
        raise HTTPException(status_code=400, detail="用户不存在")
    existing = (
        db.query(models.PermanentWhitelistEntry)
        .filter(models.PermanentWhitelistEntry.user_id == user.id)
        .first()
    )
    if existing is not None:
        raise HTTPException(status_code=409, detail="该用户已在永久名单里")
    note = (request.note or "").strip() or None
    if note is not None and len(note) > 255:
        raise HTTPException(status_code=400, detail="备注不能超过 255 字")
    entry = models.PermanentWhitelistEntry(
        user_id=user.id, note=note, added_by=current_admin.id)
    db.add(entry)
    try:
        db.flush()
    except IntegrityError:
        db.rollback()
        raise HTTPException(status_code=409, detail="该用户已在永久名单里")
    _audit(db, current_admin, "permanent_whitelist.add",
           target_type="web_user", target_id=user.id,
           details={"note": note},
           ip=raw_request.client.host if raw_request.client else None)
    db.commit()
    db.refresh(entry)
    return {"success": True, "entry": _entry_payload(db, entry)}


@admin_permanent_whitelist_router.delete("/entries/{user_id}")
def remove_entry(
    user_id: int,
    raw_request: Request,
    current_admin: models.WebUser = Depends(get_current_admin),
    db: Session = Depends(get_db),
):
    """把一个用户移出永久名单：不在名单 404。移除只删名单行，
    用户的资格/凭证/订阅数据一律不动——移除后即刻回原过期口径。"""
    entry = (
        db.query(models.PermanentWhitelistEntry)
        .filter(models.PermanentWhitelistEntry.user_id == user_id)
        .first()
    )
    if entry is None:
        raise HTTPException(status_code=404, detail="该用户不在永久名单里")
    _audit(db, current_admin, "permanent_whitelist.remove",
           target_type="web_user", target_id=user_id,
           details={"note": entry.note},
           ip=raw_request.client.host if raw_request.client else None)
    db.delete(entry)
    db.commit()
    return {"success": True}


@admin_permanent_whitelist_router.get("/config")
def get_config(
    current_admin: models.WebUser = Depends(get_current_admin),
    db: Session = Depends(get_db),
):
    """总开关当前值（默认开：无配置行即 True）。"""
    return {"enabled": pw.whitelist_enabled(db)}


class WhitelistConfigRequest(BaseModel):
    enabled: bool


@admin_permanent_whitelist_router.put("/config")
def update_config(
    request: WhitelistConfigRequest,
    raw_request: Request,
    current_admin: models.WebUser = Depends(get_current_admin),
    db: Session = Depends(get_db),
):
    """保存总开关：关闭后名单保留、全站回原过期口径；重开即刻恢复。"""
    try:
        config_service.save_config(
            db, {pw.CONFIG_KEY: request.enabled},
            schema="permanent_whitelist",
            descriptions={pw.CONFIG_KEY: "永久账号白名单总开关（默认开）"})
    except ConfigValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    _audit(db, current_admin, "permanent_whitelist.config",
           target_type="system_config", target_id=None,
           details={"enabled": request.enabled},
           ip=raw_request.client.host if raw_request.client else None)
    db.commit()
    return {"enabled": pw.whitelist_enabled(db)}


__all__ = ["admin_permanent_whitelist_router"]
