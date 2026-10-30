"""
管理后台 API · 管理员认证

后台登录是整个站点最值钱的入口，所以它单独成篇、方便单独审：

- ``POST /api/admin/auth/login``：限流（8 次 / 分钟 / IP）+ 登录日志 + 人机验证；
- ``GET  /api/admin/auth/me``：当前管理员身份；
- ``POST /api/admin/auth/change-password``：管理员改自己的密码（落审计）。

路由对象与其它后台模块共用 ``admin_core.admin_router``，导入即注册。
"""
from datetime import datetime
from typing import Optional

from fastapi import Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from backend import admin_roles, models
from backend.api.admin_core import (
    _audit,
    _log_out,
    admin_router,
    get_current_admin,
)
from backend.authlog import client_ip as log_ip, record_event, user_agent
from backend.database import get_db
from backend.integrations import captcha as integrations_captcha
from backend.ratelimit import check_rate_limit, client_ip
from backend.security import (
    create_access_token,
    create_refresh_token,
    DUMMY_PASSWORD_HASH,
    hash_password,
    verify_password,
)


# ==================== 管理员认证 API ====================

class AdminLoginRequest(BaseModel):
    username: str
    password: str
    # 人机验证令牌（v2.20.0）：管理员在「系统设置 → 人机验证」里开了「保护管理后台登录」
    # 之后必填。后台登录是最值钱的入口，此前也是唯一没有这道闸的登录路径。
    captcha_token: str | None = None


@admin_router.post("/auth/login")
def admin_login(
    http_request: Request,
    request: AdminLoginRequest,
    db: Session = Depends(get_db),
):
    """管理员登录：仅 is_staff 的 WebUser 可登录管理后台

    安全：与用户端登录同样的限流 + 登录日志。后台登录原先既不限流也不留痕，
    被人跑字典猜管理员密码时站点完全无感，也无法事后审计。
    """
    allowed, retry_after = check_rate_limit(f"admin_login:{client_ip(http_request)}", 8, 60)
    if not allowed:
        record_event(
            db, username=request.username.strip(), ip=log_ip(http_request),
            agent=user_agent(http_request), success=False, reason="admin_login_failed",
            detail=f"尝试过于频繁，已限流（{retry_after}s）",
        )
        raise HTTPException(status_code=429, detail="尝试过于频繁，请稍后再试")

    integrations_captcha.guard(db, http_request, "admin_login", request.captcha_token,
                               username=request.username.strip())

    user = db.query(models.WebUser).filter(
        models.WebUser.username == request.username.strip()
    ).first()

    if user is None:
        # A-003（360° 审计 R1）：用户不存在也走一次等价 bcrypt 校验，
        # 抹平与存在用户之间的时序差，防止管理员用户名被枚举。
        verify_password(request.password, DUMMY_PASSWORD_HASH)
        password_ok = False
    else:
        password_ok = verify_password(request.password, user.password_hash)
    if not password_ok:
        record_event(
            db, username=request.username.strip(), user_id=user.id if user else None,
            ip=log_ip(http_request), agent=user_agent(http_request), success=False,
            reason="admin_login_failed", detail="用户名或密码错误",
        )
        raise HTTPException(status_code=401, detail="用户名或密码错误")
    if not user.is_staff:
        record_event(
            db, username=user.username, user_id=user.id, ip=log_ip(http_request),
            agent=user_agent(http_request), success=False, reason="admin_login_failed",
            detail="账号没有管理员权限（尝试登录后台）",
        )
        raise HTTPException(status_code=403, detail="该账号没有管理员权限")
    if not user.is_active:
        record_event(
            db, username=user.username, user_id=user.id, ip=log_ip(http_request),
            agent=user_agent(http_request), success=False, reason="admin_login_failed",
            detail="账号已被禁用",
        )
        raise HTTPException(status_code=403, detail="账号已被禁用")

    user.last_login_at = datetime.now()
    db.commit()
    record_event(
        db, username=user.username, user_id=user.id, ip=log_ip(http_request),
        agent=user_agent(http_request), success=True, reason="admin_login",
    )

    access = create_access_token(user.id, {"username": user.username, "staff": True},
                                  token_version=user.token_version or 0)
    refresh = create_refresh_token(user.id, token_version=user.token_version or 0)
    # 登录响应带上角色（与 /auth/me 同一个 payload）：进后台立刻按真实角色渲染导航与按钮，
    # 不用先显示成「超级管理员」再等一次 /auth/me 纠正（v2.26.0）
    perms = admin_roles.permission_payload(user)
    return {
        "access_token": access,
        "refresh_token": refresh,
        "token_type": "bearer",
        "user": {
            "id": user.id,
            "username": user.username,
            "is_staff": True,
            "admin_role": perms["role"],
            "role_label": perms["role_label"],
            "can_write": perms["can_write"],
            "is_super": perms["is_super"],
        },
    }


class AdminMeResponse(BaseModel):
    id: int
    username: str
    is_staff: bool
    created_at: Optional[str] = None
    # v2.26.0 角色与权限：前端据此置灰入口（服务端仍然自己判一次，界面只是提前告知）
    admin_role: Optional[str] = None
    role_label: Optional[str] = None
    can_write: bool = True
    is_super: bool = False


@admin_router.get("/auth/me", response_model=AdminMeResponse)
def admin_me(
    current_admin: models.WebUser = Depends(get_current_admin),
):
    perms = admin_roles.permission_payload(current_admin)
    return AdminMeResponse(
        id=current_admin.id,
        username=current_admin.username,
        is_staff=True,
        created_at=_log_out(current_admin),
        admin_role=perms["role"],
        role_label=perms["role_label"],
        can_write=perms["can_write"],
        is_super=perms["is_super"],
    )


class AdminChangePasswordRequest(BaseModel):
    old_password: str
    new_password: str = Field(..., min_length=8, max_length=64)


@admin_router.post("/auth/change-password")
def admin_change_password(
    http_request: Request,
    request: AdminChangePasswordRequest,
    current_admin: models.WebUser = Depends(get_current_admin),
    db: Session = Depends(get_db),
):
    """管理员修改自己的登录密码

    P0-1 修复：改密后旧 token 必须全部作废（token_version+1），与用户端改密同口径。
    否则被盗的管理员账号即使改密，攻击者手里的 access/refresh token 仍继续有效，
    "改密"这个安全动作在管理员面就是空转。
    """
    # A-004（360° 审计 R1）：旧密码校验按登录同口径限流（8 次/分钟，
    # 桶按 IP+管理员），失败落安全日志（此前无限流、仅成功写审计）。
    allowed, retry_after = check_rate_limit(
        f"admin_change_password:{client_ip(http_request)}:{current_admin.id}", 8, 60)
    if not allowed:
        raise HTTPException(status_code=429, detail="尝试过于频繁，请稍后再试")
    if not verify_password(request.old_password, current_admin.password_hash):
        record_event(
            db, username=current_admin.username, user_id=current_admin.id,
            ip=log_ip(http_request), agent=user_agent(http_request), success=False,
            reason="admin_password_change_failed", detail="原密码错误",
        )
        raise HTTPException(status_code=400, detail="原密码错误")

    current_admin.password_hash = hash_password(request.new_password)
    # 改密后旧 token 全部作废：版本号+1，get_current_admin 的新鲜度检查会拒绝旧 tv 的 token
    current_admin.token_version = int(current_admin.token_version or 0) + 1
    # 审计与密码修改在同一个事务里提交，避免"密码已改但审计丢失"（双 commit 问题）
    _audit(db, current_admin.id, "admin_change_password", "self", current_admin.id)
    db.commit()
    return {"success": True, "message": "密码修改成功"}


class AdminLogoutRequest(BaseModel):
    # 可选：一并吊销的 refresh token（jti 进吊销表）。不传则只吊销当前 access token。
    refresh_token: str | None = None


@admin_router.post("/auth/logout")
def admin_logout(
    http_request: Request,
    body: AdminLogoutRequest,
    current_admin: models.WebUser = Depends(get_current_admin),
    db: Session = Depends(get_db),
):
    """管理员登出：吊销当前 access token 的 jti（服务端生效）。

    与用户端 /api/user/auth/logout 同口径：登出是安全动作，必须服务端生效。
    R49 补齐：同步 bump token_version，令该管理员全部已签发 refresh 立即失效——
    否则本次登录签发的 refresh（前端不存、但服务端 30 天有效）仍可换发管理
    token，登出对持有 refresh 的攻击者形同虚设（用户端 A-P1-1 同一教训）。
    代价：该管理员的门户会话一并失效（同一套版本号），需重登——安全动作的
    合理语义。

    如请求体附带 refresh_token，一并吊销（防"已登出"的会话被 refresh 续签）。
    只吊销属于当前管理员自己的 refresh token，防止跨用户吊销。
    """
    from backend.security import decode_token, revoke_jti

    # 登出是安全动作，记审计（与 change-password 同口径；revoke_jti 内部会 commit，
    # 无 token 可吊销时靠最后的显式 commit 落盘，避免"已登出但无审计"）
    _audit(db, current_admin.id, "admin_logout", "self", current_admin.id)

    auth = http_request.headers.get("Authorization", "")
    token = auth[7:] if auth.lower().startswith("bearer ") else ""
    if token:
        payload = decode_token(token, expected_type="access")
        if payload:
            revoke_jti(db, payload.get("jti"), current_admin.id, reason="admin_logout")

    if body.refresh_token:
        payload = decode_token(body.refresh_token, expected_type="refresh")
        if payload:
            try:
                refresh_user_id = int(payload.get("sub"))
            except (TypeError, ValueError):
                refresh_user_id = None
            # 只允许吊销自己的 refresh token
            if refresh_user_id == current_admin.id:
                revoke_jti(db, payload.get("jti"), current_admin.id, reason="admin_logout")

    # R49：bump token_version —— 该管理员全部已签发 refresh 立即失效
    # （revoke_jti 内部已 commit，这里是同一事务的继续）
    current_admin.token_version = int(current_admin.token_version or 0) + 1
    db.commit()  # 无 token 可吊销时，审计记录靠这里落盘（有 revoke 时已在内部提交，这里是 no-op）
    return {"success": True, "message": "已登出"}
