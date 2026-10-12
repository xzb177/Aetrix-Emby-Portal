"""管理后台 · 管理员与权限（v2.26.0）

此前后台只有一种身份：``is_staff``。给客服 / 运营 / 审计的人开号，就必须把
「改系统设置、改上游 Key、管管理员」一起交出去。

这一组端点把「谁是管理员、他是什么角色」变成可管理的一件事（角色定义与判定见
``backend/admin_roles.py``；判定发生在鉴权依赖里，不在这里）：

- ``GET    /api/admin/admins``            管理员清单 + 角色元数据
- ``POST   /api/admin/admins``            把某个用户提为管理员（指定角色）
- ``PATCH  /api/admin/admins/{user_id}``  改角色 / 启用停用
- ``DELETE /api/admin/admins/{user_id}``  撤销管理员

三条护栏（都是「真实会踩到」的坑，不是形式主义）：

1. **不能动自己**：把自己的角色降下去或撤销自己，等于把自己关在门外，只能改库；
2. **不能没有超级管理员**：最后一个 super 既不能降级也不能撤销；
3. **被停用的账号不能当管理员**：``is_active`` 为假的账号先启用再说（否则等于开了个
   永远登不进来的号）。
"""

from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy.orm import Session

from backend import admin_roles, models
from backend.api.admin_core import _audit, get_current_admin
from backend.database import get_db
from backend.ratelimit import get_client_ip

router = APIRouter(prefix="/api/admin/admins", tags=["管理后台·管理员与权限"])

MAX_ADMINS = 50  # 一个后台不会需要 50 个管理员；到量了先撤掉不用的


def _serialize(user: models.WebUser) -> dict:
    role = admin_roles.role_of(user)
    return {
        "id": user.id,
        "username": user.username,
        "email": user.email or "",
        "is_active": bool(user.is_active),
        "admin_role": role,
        "role_label": admin_roles.role_label(role),
        "last_login_at": user.last_login_at.isoformat() if user.last_login_at else None,
        "created_at": user.created_at.isoformat() if user.created_at else None,
    }


def _admins_query(db: Session):
    return db.query(models.WebUser).filter(models.WebUser.is_staff.is_(True))


def _super_count(db: Session) -> int:
    """还剩几个超级管理员（空角色按 super 算，与 role_of 的口径一致）"""
    count = 0
    for user in _admins_query(db).all():
        if admin_roles.role_of(user) == admin_roles.ROLE_SUPER:
            count += 1
    return count


def _locked_super_count(db: Session) -> int:
    """行锁所有管理员后计数（F-P1-6 修复）。

    「至少保留一个 super」的检查与修改必须同事务串行化：不加锁时两名 super
    并发互相降级可同时读到数量 2 并通过检查。调用方须在判断后、提交前调用，
    锁住的行直到事务提交才释放。

    注意（P1-1，2026-10-12）：角色变更 / 停用 / 撤销路径现已统一用
    ``_locked_loginable_super_count``（角色口径会留下"无人能登录"的死局）；
    本函数保留，仅作兼容。
    """
    users = _admins_query(db).with_for_update().all()
    return sum(1 for u in users if admin_roles.role_of(u) == admin_roles.ROLE_SUPER)


def _locked_loginable_super_count(db: Session) -> int:
    """行锁后计数「可登录的超级管理员」（角色 super 且 is_active，F-P1-14 修复）。

    停用 super 走这个口径：只看角色的计数会留下"计数仍为 2 但无人能登录"的死局。
    """
    users = _admins_query(db).with_for_update().all()
    return sum(
        1
        for u in users
        if admin_roles.role_of(u) == admin_roles.ROLE_SUPER and bool(u.is_active)
    )


def _require_user(db: Session, user_id: int) -> models.WebUser:
    user = db.query(models.WebUser).filter(models.WebUser.id == user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="用户不存在")
    return user


def _parse_role(raw: Optional[str]) -> str:
    value = (raw or "").strip().lower()
    if value not in admin_roles.ROLES:
        raise HTTPException(
            status_code=400,
            detail="角色只能是 " + " / ".join(admin_roles.ROLES),
        )
    return value


@router.get("")
def list_admins(
    current_admin: models.WebUser = Depends(get_current_admin),
    db: Session = Depends(get_db),
):
    """管理员清单（含角色元数据与「我」的身份，前端不再自己维护一份枚举）"""
    users = _admins_query(db).order_by(models.WebUser.id).all()
    return {
        "admins": [_serialize(u) for u in users],
        "roles": admin_roles.roles_payload(),
        "me": {
            "id": current_admin.id,
            "admin_role": admin_roles.role_of(current_admin),
        },
        "super_count": _super_count(db),
        "limit": MAX_ADMINS,
    }


class GrantAdminRequest(BaseModel):
    username: Optional[str] = None
    email: Optional[str] = None
    role: str = admin_roles.ROLE_OPERATOR


@router.post("")
def grant_admin(
    payload: GrantAdminRequest,
    request: Request,
    current_admin: models.WebUser = Depends(get_current_admin),
    db: Session = Depends(get_db),
):
    """把某个已注册用户提为管理员（按用户名或邮箱找账号）

    管理员本身**不新建账号**：后台账号就是前台账号（同一套 token、同一个密码），
    所以这里只做「找到那个人 + 标记 + 定角色」，不涉及密码与注册。
    """
    role = _parse_role(payload.role)
    username = (payload.username or "").strip()
    email = (payload.email or "").strip()
    if not username and not email:
        raise HTTPException(status_code=400, detail="请填写用户名或邮箱")

    # F-P1-13 修复：两字段同时填写时分别精确查询，要求命中同一账号。
    # 原 or_ 查询在"用户名命中甲、邮箱命中乙"时会静默给首条记录授管理员权限。
    # fail-closed：对不上就 400，不猜。
    if username and email:
        by_username = db.query(models.WebUser).filter(models.WebUser.username == username).first()
        by_email = db.query(models.WebUser).filter(models.WebUser.email == email).first()
        if by_username is None or by_email is None or by_username.id != by_email.id:
            raise HTTPException(status_code=400, detail="用户名与邮箱没有命中同一账号，请检查后重试")
        user = by_username
    elif username:
        user = db.query(models.WebUser).filter(models.WebUser.username == username).first()
    else:
        user = db.query(models.WebUser).filter(models.WebUser.email == email).first()
    if not user:
        raise HTTPException(status_code=404, detail="找不到这个账号（需要先在站点注册）")
    if user.is_staff:
        raise HTTPException(status_code=400, detail=f"「{user.username}」已经是管理员了")
    if _admins_query(db).count() >= MAX_ADMINS:
        raise HTTPException(status_code=400, detail=f"管理员已达上限（{MAX_ADMINS} 个）")
    if not user.is_active:
        # 停用账号当管理员等于开一个永远登不进来的号，这里直接说清怎么修
        raise HTTPException(status_code=400, detail=f"「{user.username}」已被停用：先在「用户」页启用，再授予管理员")

    user.is_staff = True
    user.admin_role = role
    _audit(db, current_admin, "admin_grant", "user", user.id,
           {"username": user.username, "role": role}, ip=_client_ip(request))
    db.commit()
    return {"success": True, "admin": _serialize(user)}


class UpdateAdminRequest(BaseModel):
    role: Optional[str] = None
    is_active: Optional[bool] = None


@router.patch("/{user_id}")
def update_admin(
    user_id: int,
    payload: UpdateAdminRequest,
    request: Request,
    current_admin: models.WebUser = Depends(get_current_admin),
    db: Session = Depends(get_db),
):
    """改角色 / 启用停用某个管理员

    停用管理员 = 停用这个账号（``is_active``）：与用户页同一套语义，
    停用后连同前台登录一起失效（管理员身份不会比账号本身更宽）。
    """
    user = _require_user(db, user_id)
    if not user.is_staff:
        raise HTTPException(status_code=400, detail="这个账号不是管理员")
    if user.id == current_admin.id:
        raise HTTPException(status_code=400, detail="不能修改自己的角色或状态（避免把自己关在门外）")

    changed: dict = {}
    if payload.role is not None:
        role = _parse_role(payload.role)
        if role != admin_roles.role_of(user):
            # F-P1-6：行锁后计数，防并发同时通过"至少保留一个"检查
            # P1-1（2026-10-12）：角色变更路径同样按"可登录 super"口径计数。
            # 只看角色的计数会留下死局：super A（在用）+ super B（已停用）时，
            # 降级 A 能通过检查（角色计数=2），此后可登录 super 归零。
            # 目标账号本身已停用时，其角色变更不影响可登录数，无需拦截
            # （与停用路径的语义对称）。
            if (admin_roles.role_of(user) == admin_roles.ROLE_SUPER
                    and bool(user.is_active)
                    and _locked_loginable_super_count(db) <= 1):
                raise HTTPException(status_code=400, detail="至少要保留一个可登录的超级管理员：请先给别的账号授予超级管理员")
            user.admin_role = role
            changed["role"] = role
    if payload.is_active is not None and bool(payload.is_active) != bool(user.is_active):
        # F-P1-14：停用 super 同样要保护——只看角色的计数会留下
        # "计数仍为 2 但无人能登录"的死局，这里按"可登录 super"口径检查
        if not payload.is_active and admin_roles.role_of(user) == admin_roles.ROLE_SUPER:
            if _locked_loginable_super_count(db) <= 1:
                raise HTTPException(status_code=400, detail="至少要保留一个可登录的超级管理员：请先给别的账号授予超级管理员")
        user.is_active = bool(payload.is_active)
        changed["is_active"] = bool(payload.is_active)

    if not changed:
        return {"success": True, "changed": {}, "admin": _serialize(user)}

    _audit(db, current_admin, "admin_update", "user", user.id,
           {"username": user.username, **changed}, ip=_client_ip(request))
    db.commit()
    return {"success": True, "changed": changed, "admin": _serialize(user)}


@router.delete("/{user_id}")
def revoke_admin(
    user_id: int,
    request: Request,
    current_admin: models.WebUser = Depends(get_current_admin),
    db: Session = Depends(get_db),
):
    """撤销管理员（账号本身与会员、订阅都不受影响）"""
    user = _require_user(db, user_id)
    if not user.is_staff:
        raise HTTPException(status_code=400, detail="这个账号不是管理员")
    if user.id == current_admin.id:
        raise HTTPException(status_code=400, detail="不能撤销自己（避免把自己关在门外）")
    # F-P1-6：行锁后计数，防两名 super 并发互相撤销时同时通过检查。
    # 撤销会让账号同时失去 super 角色与 is_staff，必须按"可登录 super"口径计数，
    # 否则可能出现角色计数还剩≥2 但可登录 super 归零的死局（F-P1-14 修法扩展到撤销路径）。
    if admin_roles.role_of(user) == admin_roles.ROLE_SUPER and _locked_loginable_super_count(db) <= 1:
        raise HTTPException(status_code=400, detail="至少要保留一个可登录的超级管理员：请先给别的账号授予超级管理员")

    user.is_staff = False
    user.admin_role = None
    _audit(db, current_admin, "admin_revoke", "user", user.id,
           {"username": user.username}, ip=_client_ip(request))
    db.commit()
    return {"success": True}


def _client_ip(request: Request) -> str:
    """审计日志里的来源 IP（反向代理后面取真实地址，防伪造）"""
    return get_client_ip(request)
