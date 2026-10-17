"""
管理后台 API 路由

借鉴 twilight-kotomi 的运营面板设计：
- 统一 JWT 管理员鉴权（is_staff 的 WebUser），全端点强制鉴权
- 卡码体系：注册码生成/停用/审计 + 注册模式开关（开放/需要码/关闭）
- 用户管理：列表/搜索/禁用/启用/重置密码
- 实时播放统计：今日榜/热门条目/最近注册（低 IO，直接读本地会话表）
- 操作审计：全部管理操作落 AdminLog（admin_user_id 存 WebUser.id）

管理员操作会触发通知发送到用户前台，实现前后台联动。

v2.21.0 起按域拆分成同目录的兄弟模块（导入即注册同一个 ``admin_router``）：

- ``admin_core``：公共骨架（路由对象 / 管理员鉴权依赖 / 审计与卡码辅助）
- ``admin_auth``：管理员登录 / 当前身份 / 改密
- ``admin_economy``：邀请返利 / 积分台账 / 经济设置与统计 / 用户详情与趋势

本模块保留订阅、用户、卡码、公告、工单、求片、操作日志、统计与经济系统管理。
原本单文件两千多行，改一个审计点要在几千行里翻，也没法整体复核。
"""
from datetime import datetime, timedelta
import json
import re
from decimal import Decimal
from typing import Optional

from fastapi import Depends, HTTPException
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel, Field
from sqlalchemy import func, or_
from sqlalchemy.orm import Session

from backend import config_service, media_seek, models, realms, register_channel
from backend import qbittorrent
from backend import servers
from backend.database import get_db
from backend.error_lexicon import translate as _translate_error
from backend.notifications import (
    AdminEvent,
    notify_admin_event,
    notify_all_users,
)
from backend.security import hash_password
from backend.db_retry import commit_with_retry
from backend.api.pagination import pagination_params
from backend.api.admin_core import (
    _audit,
    _generate_code,
    _log_out,
    admin_router,
    get_current_admin,
    logger,
)

#: 渠道筛选用的「未记录」哨兵值：升级前的存量用户没记渠道，单独给它一个可选值
UNRECORDED_CHANNEL = "__unrecorded"

# ==================== 请求/响应模型 ====================

class AnnouncementCreateRequest(BaseModel):
    title: str
    content: str
    type: str = "system"
    is_pinned: bool = False


class AnnouncementUpdateRequest(BaseModel):
    title: Optional[str] = None
    content: Optional[str] = None
    type: Optional[str] = None
    is_pinned: Optional[bool] = None
    is_active: Optional[bool] = None


class TicketUpdateRequest(BaseModel):
    status: Optional[str] = None
    priority: Optional[str] = None


class TicketReplyRequest(BaseModel):
    message: str
    close_ticket: bool = False


class MediaSeekUpdateRequest(BaseModel):
    # 审核结果：approved / rejected；「入库」走单独的 mark-in-library（要校验片真的进了库）
    status: str
    admin_note: Optional[str] = None


class MediaSeekMarkRequest(BaseModel):
    """标记已入库：force = 库里没匹配到也要标（人工兜底）"""
    force: bool = False


class MediaSeekPushRequest(BaseModel):
    """把求片转交给外部服务

    - ``target``：``moviepilot``（提交订阅）/ ``qbittorrent``（加种）/ ``auto``（优先 MoviePilot）
    - ``link``：qB 需要磁力 / 种子链接（它自己不会去找片子；MoviePilot 不需要）
    """
    target: str = "auto"
    link: Optional[str] = Field(default=None, max_length=2000)


class UserMessageSendRequest(BaseModel):
    user_id: Optional[int] = None  # 以路径参数为准，保留字段兼容旧调用
    title: str
    content: str
    message_type: str = "system"


class BroadcastMessageRequest(BaseModel):
    title: str
    content: str


class SubscriptionGrantRequest(BaseModel):
    """授予订阅请求"""
    plan_id: int
    duration_days: int
    # 开哪个服的会员：留空时按套餐所属的服（再回退到面板当前服）
    realm_id: Optional[int] = None


class SubscriptionExtendRequest(BaseModel):
    """延长订阅请求"""
    days: int = Field(..., ge=1, le=3650)


# ==================== 订阅管理 API ====================

@admin_router.post("/users/{user_id}/subscriptions")
async def grant_subscription(
    user_id: int,
    request: SubscriptionGrantRequest,
    current_admin: models.WebUser = Depends(get_current_admin),
    db: Session = Depends(get_db),
):
    """授予用户订阅 - 联动：通知用户

    校验 / 落库 / 审计整段是同步 SQLAlchemy，压在事件循环上时一次提交就能让全站
    （包括别人的播放）排队，所以下放线程池；通知要走 WebSocket，必须留在循环上 await。
    """
    admin_id = current_admin.id      # 提交会让 ORM 属性过期，先在循环上取成纯值

    def _grant() -> dict:
        user = db.query(models.WebUser).filter(models.WebUser.id == user_id).first()
        if not user:
            raise HTTPException(status_code=404, detail="用户不存在")

        plan = db.query(models.SubscriptionPlan).filter(
            models.SubscriptionPlan.id == request.plan_id
        ).first()
        if not plan:
            raise HTTPException(status_code=404, detail="套餐不存在")
        plan_name = plan.name    # 提交后回读属性=一次隐式回查，先取成纯值

        end_date = datetime.now() + timedelta(days=request.duration_days)
        # 会员开在哪个服：显式指定 > 套餐所属的服 > 面板当前服。
        # 决定了这份会员能在哪台 EA 上播放（见 backend/subscriptions.py）。
        realm_id = request.realm_id or plan.realm_id or realms.active_realm_id(db)
        subscription = models.UserSubscription(
            user_id=user_id,
            plan_id=request.plan_id,
            realm_id=realm_id,
            start_date=datetime.now(),
            end_date=end_date,
            status="active",
        )
        db.add(subscription)
        db.commit()
        db.refresh(subscription)

        _audit(db, admin_id, "grant_subscription", "subscription",
               subscription.id, {"user_id": user_id, "plan_id": request.plan_id,
                                 "duration_days": request.duration_days,
                                 "realm_id": realm_id})
        db.commit()
        return {"subscription_id": subscription.id, "plan_name": plan_name,
                "end_date": end_date}

    info = await run_in_threadpool(_grant)

    await notify_admin_event(
        event_type=AdminEvent.SUBSCRIPTION_MANUAL,
        user_id=user_id,
        title="🎉 恭喜获得订阅",
        content=(f"管理员已为您开通「{info['plan_name']}」订阅，有效期 {request.duration_days} 天"
                 f"\n到期时间：{info['end_date'].strftime('%Y-%m-%d')}"),
        related_id=info["subscription_id"],
        from_admin_id=admin_id,
    )
    return {"success": True, "subscription_id": info["subscription_id"],
            "message": "订阅授予成功并已通知用户"}


@admin_router.post("/subscriptions/{subscription_id}/extend")
async def extend_subscription(
    subscription_id: int,
    request: SubscriptionExtendRequest,
    current_admin: models.WebUser = Depends(get_current_admin),
    db: Session = Depends(get_db),
):
    """延长订阅有效期 - 联动：通知用户（同步写库整段下放线程池，通知仍 await）"""
    admin_id = current_admin.id

    def _extend() -> dict:
        # F-P1-4 修复：行锁防并发延长丢失。读后写（max+timedelta）不加锁时，
        # 两个请求读到同一旧日期会互相覆盖。用 with_for_update 让后到的事务
        # 等前一个提交后重读，串行化后不再丢延长。
        subscription = (
            db.query(models.UserSubscription)
            .filter(models.UserSubscription.id == subscription_id)
            .with_for_update()
            .first()
        )
        if not subscription:
            raise HTTPException(status_code=404, detail="订阅不存在")

        subscription.end_date = max(
            subscription.end_date or datetime.now(), datetime.now()
        ) + timedelta(days=request.days)
        subscription.updated_at = datetime.now()
        db.commit()

        _audit(db, admin_id, "extend_subscription", "subscription",
               subscription.id, {"days": request.days})
        db.commit()
        return {"subscription_id": subscription.id, "user_id": subscription.user_id,
                "end_date": subscription.end_date}

    info = await run_in_threadpool(_extend)

    await notify_admin_event(
        event_type=AdminEvent.SUBSCRIPTION_EXTENDED,
        user_id=info["user_id"],
        title="订阅已延长",
        content=(f"您的订阅已延长 {request.days} 天，"
                 f"新到期时间：{info['end_date'].strftime('%Y-%m-%d')}"),
        related_id=info["subscription_id"],
        from_admin_id=admin_id,
    )
    return {"success": True, "message": "订阅延长成功"}


# ==================== 用户管理 API ====================

@admin_router.get("/users")
def list_users(
    search: str = "",
    active: Optional[bool] = None,
    channel: str = "",
    user_type: str = "",
    limit: int = 50,
    offset: int = 0,
    current_admin: models.WebUser = Depends(get_current_admin),
    db: Session = Depends(get_db),
):
    """用户列表：搜索（用户名/邮箱）+ 状态筛选 + 注册渠道筛选 + 用户类型筛选 + 分页

    ``channel`` 空 = 全部；``channel=__unrecorded`` = 存量用户（当时没记渠道）。
    ``user_type`` 空 = 全部；``welfare`` = 公益服（is_welfare 为真）；
    ``paid`` = 付费（有生效中订阅且非公益）；``normal`` = 普通（非公益且无生效中订阅）。
    """
    query = db.query(models.WebUser)
    if search:
        like = f"%{search}%"
        query = query.filter(or_(
            models.WebUser.username.ilike(like),
            models.WebUser.email.ilike(like),
        ))
    if active is not None:
        query = query.filter(models.WebUser.is_active == active)
    if channel:
        col = models.WebUser.register_channel
        if channel == UNRECORDED_CHANNEL:
            query = query.filter(or_(col.is_(None), col == ""))
        elif channel in register_channel.ALL:
            query = query.filter(col == channel)
        else:
            query = query.filter(or_(col.is_(None), col == ""))

    # 用户类型筛选（v2.55 公益服并入用户管理）：公益 > 付费 > 普通
    if user_type == "welfare":
        query = query.filter(models.WebUser.is_welfare == True)  # noqa: E712
    elif user_type in ("paid", "normal"):
        _active_sub_q = (
            db.query(models.UserSubscription.user_id)
            .filter(
                models.UserSubscription.status == "active",
                models.UserSubscription.end_date > datetime.now(),
            )
            .distinct()
        )
        _not_welfare = or_(
            models.WebUser.is_welfare == False,  # noqa: E712
            models.WebUser.is_welfare.is_(None),
        )
        if user_type == "paid":
            query = query.filter(_not_welfare, models.WebUser.id.in_(_active_sub_q))
        else:
            query = query.filter(_not_welfare, ~models.WebUser.id.in_(_active_sub_q))

    # F-P1-5：分页参数统一钳制（limit∈[1,200]，offset≥0；负数/非法输入不再绕过）
    limit, offset = pagination_params(limit, offset)
    total = query.count()
    users = query.order_by(models.WebUser.id.desc()).offset(offset).limit(limit).all()
    now = datetime.now()

    # F-P1-1：批量取各用户的有效订阅（原逐用户查询 N+1）。
    # order_by end_date desc + setdefault：同用户第一条即最晚到期的那条。
    user_ids = [u.id for u in users]
    sub_map: dict = {}
    if user_ids:
        for s in (
            db.query(models.UserSubscription)
            .filter(
                models.UserSubscription.user_id.in_(user_ids),
                models.UserSubscription.status == "active",
                models.UserSubscription.end_date > now,
            )
            .order_by(models.UserSubscription.end_date.desc())
            .all()
        ):
            sub_map.setdefault(s.user_id, s)

    items = []
    for u in users:
        active_sub = sub_map.get(u.id)
        items.append({
            "id": u.id,
            "username": u.username,
            "email": u.email,
            "is_active": u.is_active,
            "is_staff": u.is_staff,
            "emby_username": u.emby_username,
            # 用户类型（v2.55 公益服并入用户管理）：welfare > paid > normal
            "is_welfare": bool(u.is_welfare),
            "welfare_expires_at": u.welfare_expires_at.isoformat() if u.welfare_expires_at else None,
            "welfare_grant_channel": u.welfare_grant_channel,
            # TG 绑定状态（公益服门禁用）：只暴露是否绑定，不暴露具体 ID
            "tg_bound": bool(u.telegram_id),
            "user_type": (
                "welfare" if u.is_welfare
                else ("paid" if active_sub is not None else "normal")
            ),
            "has_subscription": active_sub is not None,
            "subscription_id": active_sub.id if active_sub else None,
            "subscription_end": active_sub.end_date.isoformat() if active_sub else None,
            "last_login_at": u.last_login_at.isoformat() if u.last_login_at else None,
            # 注册渠道（v2.44.0 归因）：空值 = 升级前存量，显示「未记录」
            "register_channel": register_channel.normalize(u.register_channel),
            "register_channel_label": register_channel.label_of(u.register_channel),
            "created_at": _log_out(u),
        })

    return {
        "total": total,
        "users": items,
        "channels": [
            {"value": c, "label": register_channel.LABELS[c]} for c in register_channel.ALL
        ] + [{"value": UNRECORDED_CHANNEL, "label": register_channel.LABELS[""]}],
    }


class UserUpdateRequest(BaseModel):
    is_active: Optional[bool] = None
    is_staff: Optional[bool] = None


def _require_can_manage_target(current_admin: models.WebUser, target: models.WebUser) -> None:
    """S2：管理员账号只能由超级管理员管理；运营 / 只读只能管普通用户"""
    from backend import admin_roles

    if target.is_staff and target.id != current_admin.id and not admin_roles.is_super(current_admin):
        raise HTTPException(
            status_code=403,
            detail="目标账号是管理员：只有超级管理员能修改其他管理员（当前：{}）".format(
                admin_roles.role_label(admin_roles.role_of(current_admin))),
        )


@admin_router.put("/users/{user_id}")
def update_user(
    user_id: int,
    request: UserUpdateRequest,
    current_admin: models.WebUser = Depends(get_current_admin),
    db: Session = Depends(get_db),
):
    """更新用户状态（禁用/启用/授权管理员）

    安全约束：
    - 不能禁用/降级自己，避免把自己锁在门外；
    - 授予 / 撤销管理员（``is_staff``）只有超级管理员能做，且授予时显式写**最低权限**
      ``viewer``（再到「管理员」页调角色）——不再出现「空角色 = super」（S1）；
    - 目标是管理员时只有超级管理员能改（S2）；不能停用 / 撤销最后一个超级管理员。
    """
    from backend import admin_roles
    from backend.api.admins_admin import (
        MAX_ADMINS,
        _admins_query,
        _locked_loginable_super_count,
    )

    user = db.query(models.WebUser).filter(models.WebUser.id == user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="用户不存在")

    if user.id == current_admin.id and (
        request.is_active is False or request.is_staff is False
    ):
        raise HTTPException(status_code=400, detail="不能禁用或降级自己的账号")

    _require_can_manage_target(current_admin, user)

    staff_change = request.is_staff is not None and bool(request.is_staff) != bool(user.is_staff)
    if staff_change and not admin_roles.is_super(current_admin):
        raise HTTPException(status_code=403, detail="授予 / 撤销管理员只有超级管理员能做")

    target_is_super = bool(user.is_staff) and admin_roles.role_of(user) == admin_roles.ROLE_SUPER
    # F-P1-6/F-P1-14 修复："至少保留一个 super"检查必须行锁后计数，防并发同时通过；
    # 降级/撤销/停用统一走可登录 super 口径。
    losing_super = target_is_super and (
        request.is_active is False or request.is_staff is False
    )
    if losing_super and request.is_staff is False and _locked_loginable_super_count(db) <= 1:
        raise HTTPException(status_code=400, detail="至少要保留一个可登录的超级管理员：请先给别的账号授予超级管理员")
    if losing_super and request.is_active is False and _locked_loginable_super_count(db) <= 1:
        raise HTTPException(status_code=400, detail="至少要保留一个可登录的超级管理员：请先给别的账号授予超级管理员")

    changed = {}
    if request.is_active is not None:
        user.is_active = request.is_active
        changed["is_active"] = request.is_active
    if staff_change:
        if request.is_staff:
            if _admins_query(db).count() >= MAX_ADMINS:
                raise HTTPException(status_code=400, detail=f"管理员已达上限（{MAX_ADMINS} 个）")
            user.is_staff = True
            user.admin_role = admin_roles.ROLE_VIEWER
            changed["admin_role"] = admin_roles.ROLE_VIEWER
        else:
            user.is_staff = False
            user.admin_role = None
        changed["is_staff"] = bool(request.is_staff)

    db.commit()
    _audit(db, current_admin, "update_user", "user", user.id, changed)
    db.commit()
    return {"success": True, "changed": changed}


class UserResetPasswordRequest(BaseModel):
    new_password: str = Field(..., min_length=6, max_length=64)


@admin_router.post("/users/{user_id}/reset-password")
def reset_user_password(
    user_id: int,
    request: UserResetPasswordRequest,
    current_admin: models.WebUser = Depends(get_current_admin),
    db: Session = Depends(get_db),
):
    """重置用户密码（门户密码同步为 Emby 播放密码）"""
    from backend.emby_server.auth import ensure_emby_credentials

    user = db.query(models.WebUser).filter(models.WebUser.id == user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="用户不存在")
    # S2：重置其他管理员的密码 = 接管其账号，只有超级管理员能做
    _require_can_manage_target(current_admin, user)

    user.password_hash = hash_password(request.new_password)
    db.commit()

    # 同步 Emby 播放密码，保持"门户账号即 Emby 账号"
    try:
        ensure_emby_credentials(db, user, password=request.new_password)
    except Exception:  # noqa: BLE001 — Emby 凭据同步失败不阻塞密码重置
        logger.warning("重置密码后同步 Emby 凭据失败: user_id=%s", user_id, exc_info=True)

    _audit(db, current_admin, "reset_user_password", "user", user.id)
    db.commit()
    return {"success": True, "message": "密码已重置"}


@admin_router.post("/users/{user_id}/messages")
async def send_user_message(
    user_id: int,
    request: UserMessageSendRequest,
    current_admin: models.WebUser = Depends(get_current_admin),
    db: Session = Depends(get_db),
):
    """给指定用户发送站内消息（校验 + 审计下放线程池）"""
    admin_id = current_admin.id

    def _prepare() -> None:
        user = db.query(models.WebUser).filter(models.WebUser.id == user_id).first()
        if not user:
            raise HTTPException(status_code=404, detail="用户不存在")

        _audit(db, admin_id, "send_user_message", "user", user_id,
               {"title": request.title})
        db.commit()

    await run_in_threadpool(_prepare)

    results = await notify_admin_event(
        event_type=f"station.{request.message_type}",
        user_id=user_id,
        title=request.title,
        content=request.content,
        from_admin_id=current_admin.id,
    )
    if not results.get("in_app", False):
        raise HTTPException(status_code=500, detail="消息发送失败")
    return {"success": True, "message": "消息发送成功"}


@admin_router.post("/messages/broadcast")
async def broadcast_message(
    request: BroadcastMessageRequest,
    current_admin: models.WebUser = Depends(get_current_admin),
    db: Session = Depends(get_db),
):
    """广播消息给所有用户（审计下放线程池；广播本身要给每个用户落站内信 + 实时推送）"""
    admin_id = current_admin.id

    def _audit_broadcast() -> None:
        _audit(db, admin_id, "broadcast_message", "all_users", None,
               {"title": request.title})
        db.commit()

    await run_in_threadpool(_audit_broadcast)

    count = await notify_all_users(
        event_type="system.broadcast",
        title=request.title,
        content=request.content,
    )
    # count = 真正落了站内消息的用户数（全部启用账号；在线的那部分同时做了实时推送），
    # 旧文案写成「在线用户」与现在的行为不符
    return {"success": True, "notified_users": count,
            "message": f"广播已发送，已为 {count} 位用户生成站内消息"}


# ---------- Google Drive SA 账号管理（Fix 6） ----------

@admin_router.get("/gdrive/sa-status")
def gdrive_sa_status(
    current_admin: models.WebUser = Depends(get_current_admin),
    db: Session = Depends(get_db),
):
    """Google Drive Service Account 状态：数量、当前轮换位置、磁盘压力"""
    from pathlib import Path
    import json
    import shutil
    from backend.emby_server import playback_tune

    sa_dir = Path("/opt/rclone-sa")
    sa_files = sorted(sa_dir.glob("*.json")) if sa_dir.exists() else []
    # 轮换状态
    state_file = sa_dir / ".sa_rotation.json"
    current_sa = None
    rotation_index = 0
    if state_file.exists():
        try:
            state = json.loads(state_file.read_text())
            rotation_index = state.get("index", 0)
            if sa_files:
                current_sa = sa_files[rotation_index % len(sa_files)].name
        except Exception:
            pass
    # 磁盘压力
    disk = playback_tune.check_disk_pressure("/") if hasattr(playback_tune, "check_disk_pressure") else {}
    # rclone 缓存目录大小
    cache_dir = Path("/var/cache/rclone-mp")
    cache_size = 0
    if cache_dir.exists():
        try:
            cache_size = sum(f.stat().st_size for f in cache_dir.rglob("*") if f.is_file())
        except Exception:
            pass

    return {
        "sa_count": len(sa_files),
        "sa_files": [f.name for f in sa_files[:20]],  # 只返回前 20 个文件名
        "sa_truncated": len(sa_files) > 20,
        "current_sa": current_sa,
        "rotation_index": rotation_index,
        "disk": disk,
        "cache_size_bytes": cache_size,
        "cache_size_human": f"{cache_size / (1024**3):.1f} GB" if cache_size else "0",
    }


@admin_router.post("/gdrive/sa-rotate")
def gdrive_sa_rotate(
    current_admin: models.WebUser = Depends(get_current_admin),
    db: Session = Depends(get_db),
):
    """手动触发 SA 轮换（切换账号；步长 1..n 随机，反风控）"""
    from pathlib import Path
    from backend.emby_server import playback_tune

    sa_dir = Path("/opt/rclone-sa")
    if not sa_dir.exists():
        raise HTTPException(status_code=404, detail="SA 目录不存在")
    # sa_dir 是必填参数：以前漏传，这个接口每次都 TypeError → 500，手动轮换从没生效过
    result = playback_tune.ensure_sa_rotation("/root/.config/rclone/rclone.conf", sa_dir)
    _audit(db, current_admin, "gdrive_sa_rotate", "gdrive_sa", 0, {"sa_file": result.get("sa_file")})
    db.commit()
    # 轮换只改了 rclone.conf：SA 在挂载/rcd 启动时加载，代码库里没有应用内重挂能力，
    # 必须把"需要重新挂载才生效"明确告诉管理员，否则以为切了其实还在用旧账号。
    needs_remount = bool(result.get("needs_remount", result.get("changed", False)))
    return {
        "success": True,
        "sa_file": result.get("sa_file"),
        "mode": result.get("mode"),
        "needs_remount": needs_remount,
        "remount_hint": (
            "已改写 rclone.conf，但正在运行的 rclone 仍在用旧账号：需在宿主机重新挂载"
            "（重跑 scripts/rclone_vfs_mount.sh）或重启 rclone 控制面"
            "（scripts/restart_rclone_rcd.sh）后才生效。"
        ) if needs_remount else "",
    }


class RegistrationModeRequest(BaseModel):
    # 注册码门禁（"code"）已下线：只接受 open / closed。DB 里残留的 "code"
    # 由注册接口兼容处理（按 open 走），这里直接拒绝新写入。
    mode: str  # open / closed
    message: str = ""
    # 注册限流（可选，None 表示不修改）
    ratelimit_enabled: Optional[bool] = None  # 是否启用注册限流
    ratelimit_max: Optional[int] = None  # 窗口内最大注册次数
    ratelimit_window: Optional[int] = None  # 窗口秒数


@admin_router.put("/settings/registration")
def set_registration_mode(
    request: RegistrationModeRequest,
    current_admin: models.WebUser = Depends(get_current_admin),
    db: Session = Depends(get_db),
):
    """设置注册模式：open 开放注册 / closed 关闭注册

    可选附带注册限流配置（None 表示不修改）：
    - ratelimit_enabled: 是否启用注册限流
    - ratelimit_max: 窗口内最大注册次数（必须 >= 1）
    - ratelimit_window: 窗口秒数（必须 >= 60）
    """
    if request.mode not in ("open", "closed"):
        raise HTTPException(status_code=400, detail="mode 必须是 open/closed")
    if request.ratelimit_max is not None and request.ratelimit_max < 1:
        raise HTTPException(status_code=400, detail="ratelimit_max 必须大于等于 1")
    if request.ratelimit_window is not None and request.ratelimit_window < 60:
        raise HTTPException(status_code=400, detail="ratelimit_window 必须大于等于 60")

    # 注册限流：仅当字段提供（非 None）时写入；与模式配置合并为一次批量写入
    values = {
        "registration_mode": request.mode,
        "registration_closed_message": request.message,
    }
    if request.ratelimit_enabled is not None:
        values["register_ratelimit_enabled"] = (
            "true" if request.ratelimit_enabled else "false")
    if request.ratelimit_max is not None:
        values["register_ratelimit_max"] = str(request.ratelimit_max)
    if request.ratelimit_window is not None:
        values["register_ratelimit_window"] = str(request.ratelimit_window)

    # 配置瘦身第 4 批 4-4：走统一写入通道（校验→落库→写时失效）；
    # store.write_values 内部已按 store 纪律失效，4-1 的失效覆盖由正统通道承接。
    # registration_closed_message="" 走统一「未设置」语义删行，读侧
    # (closed_msg_config.value if ... and ... else 默认文案) 与空行等价。
    config_service.save_config(db, values, schema="registration")
    db.commit()
    audit_details = {"mode": request.mode}
    if request.ratelimit_enabled is not None:
        audit_details["ratelimit_enabled"] = request.ratelimit_enabled
    if request.ratelimit_max is not None:
        audit_details["ratelimit_max"] = request.ratelimit_max
    if request.ratelimit_window is not None:
        audit_details["ratelimit_window"] = request.ratelimit_window
    _audit(db, current_admin, "set_registration_mode", "system", None,
           audit_details)
    db.commit()
    return {"success": True, "mode": request.mode}


@admin_router.get("/settings/registration")
def get_registration_mode(
    current_admin: models.WebUser = Depends(get_current_admin),
    db: Session = Depends(get_db),
):
    # 一次查询取回全部五个键
    rows = (
        db.query(models.SystemConfig)
        .filter(models.SystemConfig.key.in_([
            "registration_mode",
            "registration_closed_message",
            "register_ratelimit_enabled",
            "register_ratelimit_max",
            "register_ratelimit_window",
        ]))
        .all()
    )
    values = {r.key: r.value for r in rows}

    # 与注册接口同口径：历史 code / 未知值按 closed 展示（注册接口实际也拒绝注册）
    from backend.api.emby_portal import _normalize_registration_mode
    mode = _normalize_registration_mode(values.get("registration_mode"))
    message = values.get("registration_closed_message") or ""

    ratelimit_enabled = True
    enabled_raw = values.get("register_ratelimit_enabled")
    if enabled_raw is not None:
        normalized = enabled_raw.strip().lower()
        if normalized in ("true", "false"):
            ratelimit_enabled = normalized == "true"

    ratelimit_max = 5
    max_raw = values.get("register_ratelimit_max")
    if max_raw is not None:
        try:
            ratelimit_max = int(max_raw)
        except (TypeError, ValueError):
            ratelimit_max = 5

    ratelimit_window = 3600
    window_raw = values.get("register_ratelimit_window")
    if window_raw is not None:
        try:
            ratelimit_window = int(window_raw)
        except (TypeError, ValueError):
            ratelimit_window = 3600

    return {
        "mode": mode,
        "message": message,
        "ratelimit_enabled": ratelimit_enabled,
        "ratelimit_max": ratelimit_max,
        "ratelimit_window": ratelimit_window,
    }


# ==================== 公告管理 API ====================

@admin_router.get("/announcements")
def get_announcements(
    active_only: bool = False,
    limit: int = 100,
    current_admin: models.WebUser = Depends(get_current_admin),
    db: Session = Depends(get_db),
):
    """公告列表（管理端，含未启用项）"""
    query = db.query(models.Announcement)
    if active_only:
        query = query.filter(models.Announcement.is_active == True)  # noqa: E712
    announcements = (
        query.order_by(models.Announcement.is_pinned.desc(), models.Announcement.created_at.desc())
        .limit(max(1, min(limit, 500)))
        .all()
    )
    return [
        {
            "id": a.id,
            "title": a.title,
            "content": a.content,
            "type": a.type,
            "is_active": bool(a.is_active),
            "is_pinned": bool(a.is_pinned),
            "created_at": a.created_at.isoformat() if a.created_at else None,
            "updated_at": a.updated_at.isoformat() if a.updated_at else None,
        }
        for a in announcements
    ]


@admin_router.post("/announcements")
async def create_announcement(
    request: AnnouncementCreateRequest,
    current_admin: models.WebUser = Depends(get_current_admin),
    db: Session = Depends(get_db),
):
    """创建公告 - 联动：广播通知所有用户（落库 + 审计下放线程池）"""
    admin_id = current_admin.id

    def _create() -> int:
        announcement = models.Announcement(
            title=request.title,
            content=request.content,
            type=request.type,
            is_pinned=request.is_pinned,
            is_active=True,
        )
        db.add(announcement)
        db.commit()
        db.refresh(announcement)
        announcement_id = announcement.id

        _audit(db, admin_id, "create_announcement", "announcement",
               announcement_id, {"title": request.title})
        db.commit()
        return announcement_id

    announcement_id = await run_in_threadpool(_create)

    # notify_all_users 现在给**每个启用中的用户**落站内消息（不只当时在线的人），
    # 返回值就是实际收到的用户数，文案跟着这个数字走，不再无条件宣称"已推送给所有用户"
    notified = await notify_all_users(
        event_type=AdminEvent.ANNOUNCEMENT_PUBLISHED,
        title=f"📢 {request.title}",
        content=request.content,
        data={"announcement_id": announcement_id, "type": request.type},
    )
    return {"success": True, "announcement_id": announcement_id,
            "notified_users": notified,
            "message": f"公告创建成功，已为 {notified} 位用户生成站内消息"}


@admin_router.put("/announcements/{announcement_id}")
async def update_announcement(
    announcement_id: int,
    request: AnnouncementUpdateRequest,
    current_admin: models.WebUser = Depends(get_current_admin),
    db: Session = Depends(get_db),
):
    admin_id = current_admin.id

    def _update() -> str:
        announcement = db.query(models.Announcement).filter(
            models.Announcement.id == announcement_id
        ).first()
        if not announcement:
            raise HTTPException(status_code=404, detail="公告不存在")

        update_data = request.dict(exclude_unset=True)
        for key, value in update_data.items():
            setattr(announcement, key, value)
        announcement.updated_at = datetime.now()
        title = announcement.title or ""    # 提交后回读会隐式回查，先取成纯值
        db.commit()

        _audit(db, admin_id, "update_announcement", "announcement",
               announcement_id, update_data)
        db.commit()
        return title

    title = await run_in_threadpool(_update)

    await notify_all_users(
        event_type=AdminEvent.ANNOUNCEMENT_UPDATED,
        title="公告已更新",
        content=f"公告「{title}」已更新，请查看",
        data={"announcement_id": announcement_id},
    )
    return {"success": True, "message": "公告更新成功"}


@admin_router.delete("/announcements/{announcement_id}")
def delete_announcement(
    announcement_id: int,
    current_admin: models.WebUser = Depends(get_current_admin),
    db: Session = Depends(get_db),
):
    announcement = db.query(models.Announcement).filter(
        models.Announcement.id == announcement_id
    ).first()
    if not announcement:
        raise HTTPException(status_code=404, detail="公告不存在")

    title = announcement.title
    db.delete(announcement)
    db.commit()
    _audit(db, current_admin, "delete_announcement", "announcement",
           announcement_id, {"title": title})
    db.commit()
    return {"success": True, "message": "公告删除成功"}


# ==================== 探险世界 · 到此一游留言墙管理 ====================

@admin_router.get("/wall-posts")
def list_wall_posts(
    limit: int = 50,
    current_admin: models.WebUser = Depends(get_current_admin),
    db: Session = Depends(get_db),
):
    """留言墙管理：最新留言列表（含已隐藏）"""
    from backend.emby_server import models as em
    rows = (
        db.query(em.WorldWallPost)
        .order_by(em.WorldWallPost.created_at.desc())
        .limit(max(1, min(limit, 200)))
        .all()
    )
    return {
        "posts": [
            {
                "id": r.id,
                "user_id": r.user_id,
                "nickname": r.nickname,
                "content": r.content,
                "is_hidden": bool(r.is_hidden),
                "created_at": r.created_at.isoformat() if r.created_at else None,
            }
            for r in rows
        ]
    }


@admin_router.delete("/wall-posts/{post_id}")
def delete_wall_post(
    post_id: int,
    current_admin: models.WebUser = Depends(get_current_admin),
    db: Session = Depends(get_db),
):
    """留言墙管理：删除留言（软删除=隐藏，不物理删）"""
    from backend.emby_server import models as em
    post = db.query(em.WorldWallPost).filter(em.WorldWallPost.id == post_id).first()
    if not post:
        raise HTTPException(status_code=404, detail="留言不存在")
    post.is_hidden = True
    db.commit()
    _audit(db, current_admin, "delete_wall_post", "wall_post",
           post_id, {"nickname": post.nickname})
    db.commit()
    return {"success": True, "message": "留言已删除"}


# ==================== 探险预告 · 保密新大陆（揭晓位配置）====================

class TeaserConfigSaveRequest(BaseModel):
    values: dict = Field(default_factory=dict,
                         description="揭晓位配置待保存值（key 必须在白名单内，见 backend/emby_server/teaser.py）")


@admin_router.get("/teaser-config")
def get_teaser_config(
    current_admin: models.WebUser = Depends(get_current_admin),
    db: Session = Depends(get_db),
):
    """揭晓位配置：全部字段 + 派生状态（保密中 / 已揭晓 / 已关闭，倒计时天数）"""
    from backend.emby_server import teaser
    return teaser.read_admin(db)


@admin_router.put("/teaser-config")
def save_teaser_config(
    req: TeaserConfigSaveRequest,
    current_admin: models.WebUser = Depends(get_current_admin),
    db: Session = Depends(get_db),
):
    """保存揭晓位配置（写 SystemConfig，保存即生效；未知 key / 非法日期 / 非法链接 400）"""
    from backend.emby_server import teaser
    try:
        cfg = teaser.write_admin(db, req.values or {})
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    _audit(db, current_admin, "save_teaser_config", "system_config",
           0, {"keys": sorted((req.values or {}).keys())})
    db.commit()
    return cfg


# ==================== 工单管理 API ====================

@admin_router.get("/tickets")
def get_tickets(
    status_filter: Optional[str] = None,
    category_filter: Optional[str] = None,
    current_admin: models.WebUser = Depends(get_current_admin),
    db: Session = Depends(get_db),
):
    query = db.query(models.Ticket)
    if status_filter:
        query = query.filter(models.Ticket.status == status_filter)
    if category_filter:
        query = query.filter(models.Ticket.category == category_filter)

    tickets = query.order_by(models.Ticket.created_at.desc()).limit(200).all()

    # F-P1-1：批量取用户与各工单最新消息（原 2N+1 次查询）。
    # 最新消息：一次查出这些工单的全部消息（按时间倒序），Python 里按工单取第一条。
    ticket_ids = [t.id for t in tickets]
    user_ids = {t.user_id for t in tickets}
    users = {
        u.id: u.username
        for u in db.query(models.WebUser.id, models.WebUser.username)
        .filter(models.WebUser.id.in_(list(user_ids) or [-1])).all()
    } if user_ids else {}
    latest_map: dict = {}
    if ticket_ids:
        for m in (
            db.query(models.TicketMessage)
            .filter(models.TicketMessage.ticket_id.in_(ticket_ids))
            .order_by(models.TicketMessage.created_at.desc())
            .all()
        ):
            latest_map.setdefault(m.ticket_id, m)

    result = []
    for ticket in tickets:
        latest_message = latest_map.get(ticket.id)

        result.append({
            "id": ticket.id,
            "title": ticket.title,
            "category": ticket.category,
            "status": ticket.status,
            "priority": ticket.priority,
            "user_name": users.get(ticket.user_id, "未知"),
            "created_at": _log_out(ticket),
            "updated_at": ticket.updated_at.isoformat() if ticket.updated_at else "",
            "latest_message": latest_message.message[:100] if latest_message else "",
        })
    return result


@admin_router.get("/tickets/{ticket_id}/messages")
def get_ticket_messages(
    ticket_id: int,
    current_admin: models.WebUser = Depends(get_current_admin),
    db: Session = Depends(get_db),
):
    """工单会话内容（管理端）"""
    ticket = db.query(models.Ticket).filter(models.Ticket.id == ticket_id).first()
    if not ticket:
        raise HTTPException(status_code=404, detail="工单不存在")

    messages = (
        db.query(models.TicketMessage)
        .filter(models.TicketMessage.ticket_id == ticket_id)
        .order_by(models.TicketMessage.created_at.asc())
        .all()
    )

    author_ids = [m.user_id for m in messages if m.user_id]
    authors = {
        u.id: u.username
        for u in db.query(models.WebUser).filter(models.WebUser.id.in_(author_ids)).all()
    } if author_ids else {}

    return [
        {
            "id": m.id,
            "message": m.message,
            "is_admin": bool(m.is_admin),
            "created_at": m.created_at.isoformat() if m.created_at else None,
            "admin_name": authors.get(m.user_id, "管理员") if m.is_admin else None,
        }
        for m in messages
    ]


@admin_router.put("/tickets/{ticket_id}")
async def update_ticket(
    ticket_id: int,
    request: TicketUpdateRequest,
    current_admin: models.WebUser = Depends(get_current_admin),
    db: Session = Depends(get_db),
):
    admin_id = current_admin.id

    def _update() -> dict:
        ticket = db.query(models.Ticket).filter(models.Ticket.id == ticket_id).first()
        if not ticket:
            raise HTTPException(status_code=404, detail="工单不存在")

        update_data = request.dict(exclude_unset=True)
        for key, value in update_data.items():
            setattr(ticket, key, value)
        ticket.updated_at = datetime.now()
        db.commit()

        _audit(db, admin_id, "update_ticket", "ticket", ticket_id, update_data)
        db.commit()
        return {"user_id": ticket.user_id, "title": ticket.title,
                "status": ticket.status}

    info = await run_in_threadpool(_update)

    await notify_admin_event(
        event_type=AdminEvent.TICKET_REPLIED,
        user_id=info["user_id"],
        title="工单状态已更新",
        content=f"您的工单「{info['title']}」状态已变更为：{info['status']}",
        related_id=ticket_id,
        from_admin_id=admin_id,
    )
    return {"success": True, "message": "工单更新成功"}


@admin_router.post("/tickets/{ticket_id}/reply")
async def reply_ticket(
    ticket_id: int,
    request: TicketReplyRequest,
    current_admin: models.WebUser = Depends(get_current_admin),
    db: Session = Depends(get_db),
):
    """回复工单 - 联动：通知用户（落库 + 审计下放线程池，通知仍 await）"""
    admin_id = current_admin.id

    def _reply() -> dict:
        ticket = db.query(models.Ticket).filter(models.Ticket.id == ticket_id).first()
        if not ticket:
            raise HTTPException(status_code=404, detail="工单不存在")

        message = models.TicketMessage(
            ticket_id=ticket.id,
            user_id=admin_id,      # 统一账号体系：staff 回复记 WebUser.id
            message=request.message,
            is_admin=True,
        )
        db.add(message)

        ticket.status = "closed" if request.close_ticket else "open"
        ticket.updated_at = datetime.now()
        db.commit()

        _audit(db, admin_id, "reply_ticket", "ticket", ticket_id,
               {"closed": request.close_ticket})
        db.commit()
        return {"user_id": ticket.user_id}

    info = await run_in_threadpool(_reply)

    await notify_admin_event(
        event_type=AdminEvent.TICKET_REPLIED if not request.close_ticket else AdminEvent.TICKET_CLOSED,
        user_id=info["user_id"],
        title="工单有新回复" if not request.close_ticket else "工单已关闭",
        content=request.message,
        related_id=ticket_id,
        from_admin_id=admin_id,
    )
    return {"success": True, "message": "回复成功"}


@admin_router.post("/tickets/{ticket_id}/close")
async def close_ticket(
    ticket_id: int,
    current_admin: models.WebUser = Depends(get_current_admin),
    db: Session = Depends(get_db),
):
    admin_id = current_admin.id

    def _close() -> dict:
        ticket = db.query(models.Ticket).filter(models.Ticket.id == ticket_id).first()
        if not ticket:
            raise HTTPException(status_code=404, detail="工单不存在")

        was_closed = ticket.status == "closed"
        ticket.status = "closed"
        ticket.updated_at = datetime.now()
        # 关单与「回复并关单」是同一件事的两条入口，但这条一直没写审计：
        # 用户收到「工单已被关闭」的通知，运营在操作日志里却查不到是谁关的（v2.30.0 补上）。
        _audit(db, admin_id, "close_ticket", "ticket", ticket_id,
               {"already_closed": was_closed})
        db.commit()
        return {"user_id": ticket.user_id, "title": ticket.title}

    info = await run_in_threadpool(_close)

    await notify_admin_event(
        event_type=AdminEvent.TICKET_CLOSED,
        user_id=info["user_id"],
        title="工单已关闭",
        content=f"您的工单「{info['title']}」已被管理员关闭",
        related_id=ticket_id,
        from_admin_id=admin_id,
    )
    return {"success": True, "message": "工单已关闭"}


# ==================== 求片管理 API ====================

@admin_router.get("/media-seek")
def get_media_seeks(
    status_filter: Optional[str] = None,
    realm_id: Optional[int] = None,
    order: Optional[str] = None,
    current_admin: models.WebUser = Depends(get_current_admin),
    db: Session = Depends(get_db),
):
    """求片清单（可按归属服筛选；``realm_id=0`` = 全部服）

    求片本身是"这部片要进哪个服的库"，用户提交时选的服会被记下来；
    推送出口（MoviePilot / qB）是全局共享一套，所以推的时候不分服。

    v2：``order=hot`` 按附议数降序（热度排序），否则按提交时间降序。
    """
    scope_id = None if realm_id == 0 else (realm_id or realms.active_realm_id(db))
    query = realms.scope_inclusive(db.query(models.MovieRequest),
                                   models.MovieRequest.realm_id, scope_id)
    if status_filter:
        query = query.filter(models.MovieRequest.status == status_filter)
    else:
        # 用户主动撤回的条目不进待办清单（仍计入用户当天的提交额度，见 user.py）。
        # 想看它们就显式传 status_filter=withdrawn。
        query = query.filter(models.MovieRequest.status != "withdrawn")

    requests = query.order_by(models.MovieRequest.created_at.desc()).limit(200).all()
    if order == "hot":
        # v2 热度排序：附议数高的在前（附议数相同按提交时间）
        requests = sorted(
            requests,
            key=lambda r: ((r.vote_count or 0), r.created_at),
            reverse=True,
        )[:200]
    realm_names = {r.id: r.name for r in realms.list_realms(db)}

    # F-P1-1：批量取用户（原逐条查询 N+1）
    _req_user_ids = {r.user_id for r in requests}
    _req_users = {
        u.id: u.username
        for u in db.query(models.WebUser.id, models.WebUser.username)
        .filter(models.WebUser.id.in_(list(_req_user_ids) or [-1])).all()
    } if _req_user_ids else {}

    result = []
    for req in requests:
        result.append({
            "id": req.id,
            "user_id": req.user_id,
            "movie_name": req.movie_name,
            "year": req.year,
            "type": req.type,
            # 剧集按整季申请：season 是 "1,2" / "all"，label 是给界面看的文案
            "season": req.season,
            "season_label": media_seek.season_label(req.season),
            "tmdb_id": req.tmdb_id,
            "emby_item_id": req.emby_item_id,
            "note": req.note,
            "status": req.status,
            "admin_note": req.admin_note,
            "realm_id": req.realm_id,
            "realm_name": realm_names.get(req.realm_id, "") if req.realm_id else "",
            "user_name": _req_users.get(req.user_id, "未知"),
            "created_at": _log_out(req),
            # 转交外部服务的结果：让面板能看出「批了但还没真的去下载」
            "push_target": req.push_target,
            "push_status": req.push_status,
            "push_message": req.push_message,
            "pushed_at": req.pushed_at.isoformat() if req.pushed_at else None,
            # v2 附议数
            "vote_count": req.vote_count or 0,
        })
    return result


@admin_router.get("/media-seek/user-quota/{user_id}")
def get_media_seek_user_quota(
    user_id: int,
    current_admin: models.WebUser = Depends(get_current_admin),
    db: Session = Depends(get_db),
):
    """某用户的求片月度额度（管理后台审核时查看：公益/付费区分）"""
    user = db.query(models.WebUser).filter(models.WebUser.id == user_id).first()
    if user is None:
        raise HTTPException(status_code=404, detail="用户不存在")
    return media_seek.monthly_quota(db, user)


def _load_movie_request_sync(db: Session, request_id: int) -> models.MovieRequest:
    """同步取一条求片（不在时按 404 抛，语义与以前一致）——放线程池里跑，见 push_media_seek"""
    media_request = db.query(models.MovieRequest).filter(
        models.MovieRequest.id == request_id
    ).first()
    if not media_request:
        raise HTTPException(status_code=404, detail="求片请求不存在")
    return media_request


def _record_seek_push_sync(
    db: Session,
    media_request: models.MovieRequest,
    result: dict,
    target: str,
) -> dict:
    """同步记录推送结果并提交（连同端点挂上的那条审计），返回面板要展示的纯值

    提交之后 ORM 属性会过期，所以状态在这**里**读出来返回，
    免得回到事件循环上再触发一次隐式回查。
    """
    media_request.push_target = target
    media_request.push_status = "ok" if result.get("ok") else "failed"
    media_request.push_message = str(result.get("message") or "")[:300]
    media_request.pushed_at = datetime.now()
    # 交出去了就代表处理过了：批过的求片推成功时顺手标成已批准，省得管理员再点一次
    if result.get("ok") and media_request.status == "pending":
        media_request.status = "approved"
    db.commit()
    return {"target": target, "status": media_request.status}


@admin_router.post("/media-seek/{request_id}/push")
async def push_media_seek(
    request_id: int,
    payload: MediaSeekPushRequest,
    current_admin: models.WebUser = Depends(get_current_admin),
    db: Session = Depends(get_db),
):
    """把一条求片交给 MoviePilot（订阅）或 qBittorrent（加种）

    以前求片只能改状态，批了之后得管理员自己去别处搜片子——这个端点把那一步接上。
    失败原因（连不上 / 凭据不对 / 没填链接）原样回给面板，不假装成功。
    """
    # 取求片：同步查询下放线程池（这个端点还要 await 外部推送，必须是 async def）
    media_request = await run_in_threadpool(_load_movie_request_sync, db, request_id)

    # qB 要链接：直接给磁力最好；只贴了整段分享文本也能从中挑出一条
    link = qbittorrent.pick_link(payload.link or "") or (payload.link or "").strip()
    result = await servers.push_media_seek(db, media_request, payload.target, link=link)

    target = str(result.get("target") or payload.target)
    # 审计这一行必须在**端点函数体**里（scripts/check_admin_audit_coverage.py 是源码级判定）。
    # 刻意放在提交之前：提交会让 ORM 属性过期，之后再读 current_admin.id 就会在事件循环上
    # 触发一次隐式回查。_audit 本身只往会话挂对象、不发 SQL——两批写由下面那次提交一起落盘，
    # 而提交（真正产生 IO 的地方）下放线程池。
    _audit(db, current_admin, "push_media_seek", "media_seek", request_id,
           {"target": target, "ok": bool(result.get("ok")),
            "server": result.get("server")})

    snapshot = await run_in_threadpool(
        _record_seek_push_sync, db, media_request, result, target
    )

    return {
        "success": bool(result.get("ok")),
        "target": snapshot["target"],
        "server": result.get("server"),
        "message": str(result.get("message") or ""),
        "request_id": request_id,
        "status": snapshot["status"],
    }


def _review_media_seek_sync(db: Session, admin_id: int, request_id: int,
                            status_value: str, note: str) -> dict:
    """同步落库：参数校验 + 审核结果 + 审计（整段下放线程池，见 update_media_seek）

    校验放在这里而不是只放端点里，是为了让「拒绝必须写理由」这条规则
    在单元测试里有一处直接的调用点（不必起整个 HTTP 栈）。
    """
    if status_value not in ("approved", "rejected"):
        raise HTTPException(
            status_code=400,
            detail="状态只支持 approved / rejected；已入库请用「标记已入库」（会校验片真的进了库）",
        )
    if status_value == "rejected" and not note:
        raise HTTPException(status_code=400, detail="拒绝求片必须填写理由（用户会收到这条说明）")

    media_request = db.query(models.MovieRequest).filter(
        models.MovieRequest.id == request_id
    ).first()
    if not media_request:
        raise HTTPException(status_code=404, detail="求片请求不存在")

    media_request.status = status_value
    media_request.admin_note = note or None
    media_request.updated_at = datetime.now()
    db.commit()

    _audit(db, admin_id, "update_media_seek", "media_seek", request_id,
           {"status": status_value})
    db.commit()
    return {"user_id": media_request.user_id,
            "movie_name": media_request.movie_name,
            "season": media_request.season}


@admin_router.put("/media-seek/{request_id}")
async def update_media_seek(
    request_id: int,
    request: MediaSeekUpdateRequest,
    current_admin: models.WebUser = Depends(get_current_admin),
    db: Session = Depends(get_db),
):
    """审核求片：批准 / 拒绝（拒绝必须写理由，用户会收到这条说明）

    「已入库」不再走这个入口（以前 completed 只是改个状态，面板无从知道片到底有没有进库）：
    入库后标记请用 ``POST /media-seek/{id}/mark-in-library``，它会先确认片真的在媒体库里。
    """
    admin_id = current_admin.id
    # 校验在 _review_media_seek_sync 里（与落库同一段；单测直接打那个函数）
    status_value = (request.status or "").strip()
    note = (request.admin_note or "").strip()

    info = await run_in_threadpool(
        _review_media_seek_sync, db, admin_id, request_id, status_value, note,
    )

    event_map = {
        "approved": AdminEvent.MEDIA_SEEK_APPROVED,
        "rejected": AdminEvent.MEDIA_SEEK_REJECTED,
    }
    title_map = {
        "approved": "求片请求已批准",
        "rejected": "求片请求已拒绝",
    }
    # 用户看到的通知用中文状态与「（全季）」这样的范围说明，不把英文状态码和季混在一起
    status_text = {"approved": "已批准", "rejected": "已拒绝"}.get(status_value, status_value)
    season_text = media_seek.season_label(info["season"])
    await notify_admin_event(
        event_type=event_map.get(status_value, "media_seek.updated"),
        user_id=info["user_id"],
        title=title_map.get(status_value, "求片状态已更新"),
        content=(f"您的求片《{info['movie_name']}》"
                 + (f"（{season_text}）" if season_text else "")
                 + f"状态已更新为：{status_text}"
                 + (f"\n备注：{note}" if note else "")),
        related_id=request_id,
        from_admin_id=admin_id,
    )
    return {"success": True, "message": "求片状态更新成功"}


def _mark_media_seek_sync(db: Session, admin_id: int, request_id: int, force: bool) -> dict:
    """同步落库：库内匹配 + 标记已入库 + 审计（整段下放线程池，见 mark_media_seek_in_library）"""
    media_request = db.query(models.MovieRequest).filter(
        models.MovieRequest.id == request_id
    ).first()
    if not media_request:
        raise HTTPException(status_code=404, detail="求片请求不存在")
    if media_request.status not in media_seek.ACTIVE_STATUSES:
        raise HTTPException(
            status_code=400,
            detail=f"这条求片当前是「{media_request.status}」，不能重复标记入库",
        )

    match = media_seek.find_library_match(db, media_request)
    if match is None and not force:
        raise HTTPException(
            status_code=409,
            detail=(f"媒体库里没找到《{media_request.movie_name}》：等扫描入库后再标记；"
                    f"如果片已入库（但改了名），可以再点一次「强制标记」"),
        )

    media_request.status = "completed"
    media_request.emby_item_id = str(getattr(match, "guid", "") or "") or None
    media_request.updated_at = datetime.now()
    # v2：附议者名单（通知用；开关关闭则为空）
    voter_list = []
    if media_seek.notify_voters_enabled(db):
        voter_list = media_seek.voter_ids(db, request_id, exclude_user_id=media_request.user_id)
    _audit(db, admin_id, "mark_media_seek_in_library", "media_seek", request_id,
           {"matched": match is not None, "force": force,
            "item_type": getattr(match, "item_type", None)})
    db.commit()
    return {
        "user_id": media_request.user_id,
        "movie_name": media_request.movie_name,
        "season": media_request.season,
        "item_id": media_request.emby_item_id,
        "matched": match is not None,
        "voter_ids": voter_list,
    }


@admin_router.post("/media-seek/{request_id}/mark-in-library")
async def mark_media_seek_in_library(
    request_id: int,
    payload: MediaSeekMarkRequest,
    current_admin: models.WebUser = Depends(get_current_admin),
    db: Session = Depends(get_db),
):
    """入库后标记已入库：先确认片真的在媒体库里（按 tmdb_id 优先、退片名匹配）

    匹配上 → 求片置为 completed 并记下条目 guid（用户端可直接去观看）；
    没匹配到 → 409 并说清「等扫描入库后再标记」，除非显式 ``force``（人工兜底，
    例如片换了名字入库，面板匹配不到）。以前 completed 只是纯状态改写，
    这次把它变成一条可核实的结论。
    """
    admin_id = current_admin.id
    force = bool(payload.force)

    info = await run_in_threadpool(_mark_media_seek_sync, db, admin_id, request_id, force)

    season_text = media_seek.season_label(info["season"])
    await notify_admin_event(
        event_type=AdminEvent.MEDIA_SEEK_COMPLETED,
        user_id=info["user_id"],
        title="求片已入库",
        content=(f"您求的《{info['movie_name']}》"
                 + (f"（{season_text}）" if season_text else "")
                 + ("已入库，可以直接观看了。" if info["matched"] else "已入库（管理员已确认）。")),
        related_id=request_id,
        from_admin_id=admin_id,
    )
    # v2：附议者也收到入库通知（开关可关；失败不影响主流程）
    for voter_id in info.get("voter_ids") or []:
        try:
            await notify_admin_event(
                event_type=AdminEvent.MEDIA_SEEK_COMPLETED,
                user_id=voter_id,
                title="求片已入库",
                content=(f"您附议的《{info['movie_name']}》"
                         + (f"（{season_text}）" if season_text else "")
                         + "已入库，可以直接观看了。"),
                related_id=request_id,
                from_admin_id=admin_id,
            )
        except Exception:  # noqa: BLE001 — 通知失败不应影响标记入库
            logger.warning("求片入库通知附议者失败（user_id=%s）", voter_id)
    return {
        "success": True,
        "matched": info["matched"],
        "emby_item_id": info["item_id"],
        "message": "已标记入库" if info["matched"] else "已强制标记入库（媒体库里没匹配到条目）",
    }


# ==================== 操作日志 API ====================

@admin_router.get("/logs")
def get_admin_logs(
    action_filter: Optional[str] = None,
    target_type_filter: Optional[str] = None,
    limit: int = 100,
    current_admin: models.WebUser = Depends(get_current_admin),
    db: Session = Depends(get_db),
):
    query = db.query(models.AdminLog)
    if action_filter:
        query = query.filter(models.AdminLog.action == action_filter)
    if target_type_filter:
        query = query.filter(models.AdminLog.target_type == target_type_filter)

    logs = query.order_by(models.AdminLog.created_at.desc()).limit(min(limit, 500)).all()

    # F-P1-1: 批量预加载操作人，修复 N+1（原实现循环内每条日志查一次 WebUser，最多 501 次 DB 往返）
    # 去重并排除 None，避免无效 id 进入 IN 条件
    admin_ids = {log.admin_user_id for log in logs if log.admin_user_id is not None}
    operators = {}
    if admin_ids:
        # 仅在非空时查询，避免生成 IN () 语法；整批只查一次
        operators = {
            u.id: u
            for u in db.query(models.WebUser).filter(models.WebUser.id.in_(admin_ids)).all()
        }

    result = []
    for log in logs:
        # 统一账号体系后 admin_user_id 存 WebUser.id
        # F-P1-1: 改为内存映射取值，无额外 DB 查询
        operator = operators.get(log.admin_user_id)
        # 错误码大白话：details 里若含错误原文，旁边挂中文解释 + 检查步骤。
        # 预过滤：只有看起来像错误的文本才走词典，避免成功操作的审计被误翻。
        try:
            _detail_text = (json.dumps(log.details, ensure_ascii=False, default=str)
                            if log.details else "")
            _combined = f"{log.action} {_detail_text}"
            error_hint = None
            if re.search(r"fail|error|异常|失败|超时|拒绝|错误|exception|traceback",
                         _combined, re.IGNORECASE):
                error_hint = _translate_error(_combined)
        except Exception:  # noqa: BLE001 — 翻译失败不影响日志查询
            error_hint = None

        result.append({
            "id": log.id,
            "action": log.action,
            "target_type": log.target_type,
            "target_id": log.target_id,
            "details": log.details,
            "ip_address": log.ip_address,
            "created_at": _log_out(log),
            "admin_name": operator.username if operator else "未知",
            "error_hint": error_hint,
        })
    return result


# ==================== 统计数据 API ====================

@admin_router.get("/stats/overview")
def get_stats_overview(
    current_admin: models.WebUser = Depends(get_current_admin),
    db: Session = Depends(get_db),
):
    """系统概览统计"""
    from backend.emby_server import models as em
    from backend.emby_server import soft_delete

    total_users = db.query(models.WebUser).count()
    active_users = db.query(models.WebUser).filter(
        models.WebUser.is_active == True  # noqa: E712
    ).count()
    open_tickets = db.query(models.Ticket).filter(
        models.Ticket.status == "open"
    ).count()
    pending_requests = db.query(models.MovieRequest).filter(
        models.MovieRequest.status == "pending"
    ).count()

    # 自建 Emby 实时数据（低 IO：直接读本地表，不扫媒体库）
    total_items = soft_delete.count_visible(db)
    active_sessions = db.query(em.PlaybackSession).filter(
        em.PlaybackSession.ended_at.is_(None)
    ).count()

    return {
        "users": {"total": total_users, "active": active_users},
        "tickets": {"open": open_tickets},
        "media_seeks": {"pending": pending_requests},
        "emby": {"total_items": total_items, "active_sessions": active_sessions},
    }


@admin_router.get("/stats/playback")
def get_playback_stats(
    current_admin: models.WebUser = Depends(get_current_admin),
    db: Session = Depends(get_db),
):
    """实时播放统计（借鉴 twilight-kotomi 的低 IO 统计）：

    - 今日播放次数 / 今日观看用户数（PlaybackSession 本地表）
    - 用户播放排行（Top 10）
    - 热门条目（Top 10）
    """
    from backend.emby_server import models as em

    # r35："今日"按上海自然日语义；start_time 是 UTC-naive 入库，起点换算成 UTC 帧
    # 再比较（此前用服务器本地=UTC 日，"今日"实为 UTC 日）。函数内导入避循环。
    from backend.api.panel import _today_start_utc_naive
    day_start = _today_start_utc_naive()

    today_count = db.query(em.PlaybackSession).filter(
        em.PlaybackSession.start_time >= day_start
    ).count()
    today_users = db.query(func.count(func.distinct(em.PlaybackSession.user_id))).filter(
        em.PlaybackSession.start_time >= day_start
    ).scalar() or 0

    # 用户播放排行（近 7 天）
    week_start = day_start - timedelta(days=6)
    user_ranking = (
        db.query(
            models.WebUser.username,
            func.count(em.PlaybackSession.id).label("plays"),
        )
        .join(em.PlaybackSession, em.PlaybackSession.user_id == models.WebUser.id)
        .filter(em.PlaybackSession.start_time >= week_start)
        .group_by(models.WebUser.username)
        .order_by(func.count(em.PlaybackSession.id).desc())
        .limit(10)
        .all()
    )

    # 热门条目（近 7 天）
    item_ranking = (
        db.query(
            em.MediaItem.name,
            em.MediaItem.item_type,
            func.count(em.PlaybackSession.id).label("plays"),
        )
        .join(em.PlaybackSession, em.PlaybackSession.item_id == em.MediaItem.id)
        .filter(em.PlaybackSession.start_time >= week_start)
        .group_by(em.MediaItem.name, em.MediaItem.item_type)
        .order_by(func.count(em.PlaybackSession.id).desc())
        .limit(10)
        .all()
    )

    return {
        "today": {"plays": today_count, "users": today_users},
        "user_ranking": [
            {"username": name, "plays": plays} for name, plays in user_ranking
        ],
        "item_ranking": [
            {"name": name, "type": item_type, "plays": plays}
            for name, item_type, plays in item_ranking
        ],
    }


# ==================== 经济系统管理 API（v2.3.0） ====================

# ---------- 订阅套餐 CRUD ----------

class PlanUpsertRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=100)
    description: Optional[str] = None
    price: float = Field(..., ge=0)
    duration_days: int = Field(..., ge=1, le=3650)
    features: Optional[list] = None
    is_active: bool = True
    is_popular: bool = False
    sort_order: int = 0
    # 套餐一个服一个：留空时归到面板当前服
    realm_id: Optional[int] = None
    points_price: Optional[float] = Field(default=None, ge=0, description="积分价；为空表示不支持积分购买")


@admin_router.get("/economy/plans")
def economy_list_plans(
    realm_id: Optional[int] = None,
    only_active: bool = False,
    current_admin: models.WebUser = Depends(get_current_admin),
    db: Session = Depends(get_db),
):
    """订阅套餐 —— 默认含停用；``only_active=true`` 时只返回启用的套餐；
    默认只列当前服，``realm_id=0`` 列全部服"""
    scope_id = None if realm_id == 0 else (realm_id or realms.active_realm_id(db))
    query = realms.scope(db.query(models.SubscriptionPlan), models.SubscriptionPlan.realm_id, scope_id)
    if only_active:
        query = query.filter(models.SubscriptionPlan.is_active == True)  # noqa: E712
    plans = query.order_by(
        models.SubscriptionPlan.sort_order, models.SubscriptionPlan.id
    ).all()
    return {"plans": [
        {
            "id": p.id, "name": p.name, "description": p.description,
            "price": float(p.price), "duration_days": p.duration_days,
            "features": p.features, "is_active": p.is_active,
            "is_popular": p.is_popular, "sort_order": p.sort_order,
            "realm_id": p.realm_id,
            "realm_name": (p.realm.name if p.realm else ""),
            "points_price": (float(p.points_price) if p.points_price is not None else None),
        }
        for p in plans
    ],
        "realm_id": scope_id,
        "active_realm_id": realms.active_realm_id(db),
        "realms": [{"id": r.id, "name": r.name} for r in realms.list_realms(db)]}


@admin_router.post("/economy/plans")
def economy_create_plan(
    request: PlanUpsertRequest,
    current_admin: models.WebUser = Depends(get_current_admin),
    db: Session = Depends(get_db),
):
    realm_id = request.realm_id or realms.active_realm_id(db)
    if not realms.get_realm(db, realm_id):
        raise HTTPException(status_code=400, detail=f"服不存在: #{realm_id}")
    plan = models.SubscriptionPlan(
        name=request.name, description=request.description,
        price=Decimal(str(request.price)), duration_days=request.duration_days,
        features=request.features, is_active=request.is_active,
        is_popular=request.is_popular, sort_order=request.sort_order,
        realm_id=realm_id,
        points_price=(Decimal(str(request.points_price)) if request.points_price is not None else None),
    )
    db.add(plan)
    db.commit()
    db.refresh(plan)
    _audit(db, current_admin, "economy_create_plan", "plan", plan.id,
           {"name": plan.name, "price": float(plan.price), "realm_id": realm_id,
            "points_price": (float(plan.points_price) if plan.points_price is not None else None)})
    db.commit()
    return {"success": True, "id": plan.id}


@admin_router.put("/economy/plans/{plan_id}")
def economy_update_plan(
    plan_id: int,
    request: PlanUpsertRequest,
    current_admin: models.WebUser = Depends(get_current_admin),
    db: Session = Depends(get_db),
):
    plan = db.query(models.SubscriptionPlan).filter(
        models.SubscriptionPlan.id == plan_id
    ).first()
    if not plan:
        raise HTTPException(status_code=404, detail="套餐不存在")

    plan.name = request.name
    plan.description = request.description
    plan.price = Decimal(str(request.price))
    plan.points_price = Decimal(str(request.points_price)) if request.points_price is not None else None
    plan.duration_days = request.duration_days
    plan.features = request.features
    plan.is_active = request.is_active
    plan.is_popular = request.is_popular
    plan.sort_order = request.sort_order
    if request.realm_id:
        if not realms.get_realm(db, request.realm_id):
            raise HTTPException(status_code=400, detail=f"服不存在: #{request.realm_id}")
        plan.realm_id = request.realm_id
    elif plan.realm_id is None:
        plan.realm_id = realms.active_realm_id(db)
    plan.updated_at = datetime.now()
    db.commit()

    _audit(db, current_admin, "economy_update_plan", "plan", plan.id)
    db.commit()
    return {"success": True}


@admin_router.delete("/economy/plans/{plan_id}")
def economy_delete_plan(
    plan_id: int,
    current_admin: models.WebUser = Depends(get_current_admin),
    db: Session = Depends(get_db),
):
    plan = db.query(models.SubscriptionPlan).filter(
        models.SubscriptionPlan.id == plan_id
    ).first()
    if not plan:
        raise HTTPException(status_code=404, detail="套餐不存在")

    in_use = db.query(models.SubscriptionOrder).filter(
        models.SubscriptionOrder.plan_id == plan_id
    ).first()
    if in_use:
        plan.is_active = False
        db.commit()
        return {"success": True, "soft_deleted": True,
                "message": "套餐已有订单引用，已停用而非删除"}

    db.delete(plan)
    db.commit()
    _audit(db, current_admin, "economy_delete_plan", "plan", plan_id)
    db.commit()
    return {"success": True}


# ---------- 充值套餐 CRUD ----------

class PackageUpsertRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=100)
    amount: int = Field(..., ge=1, description="积分数量")
    price: float = Field(..., ge=0)
    bonus: int = Field(default=0, ge=0)
    is_active: bool = True
    is_popular: bool = False
    sort_order: int = 0


@admin_router.get("/economy/packages")
def economy_list_packages(
    current_admin: models.WebUser = Depends(get_current_admin),
    db: Session = Depends(get_db),
):
    packages = db.query(models.RechargePackage).order_by(
        models.RechargePackage.sort_order, models.RechargePackage.id
    ).all()
    return {"packages": [
        {
            "id": p.id, "name": p.name, "amount": p.amount,
            "price": float(p.price), "bonus": p.bonus,
            "is_active": p.is_active, "is_popular": p.is_popular,
            "sort_order": p.sort_order,
        }
        for p in packages
    ]}


@admin_router.post("/economy/packages")
def economy_create_package(
    request: PackageUpsertRequest,
    current_admin: models.WebUser = Depends(get_current_admin),
    db: Session = Depends(get_db),
):
    package = models.RechargePackage(
        name=request.name, amount=request.amount,
        price=Decimal(str(request.price)), bonus=request.bonus,
        is_active=request.is_active, is_popular=request.is_popular,
        sort_order=request.sort_order,
    )
    db.add(package)
    db.commit()
    db.refresh(package)
    _audit(db, current_admin, "economy_create_package", "package", package.id,
           {"name": package.name})
    db.commit()
    return {"success": True, "id": package.id}


@admin_router.put("/economy/packages/{package_id}")
def economy_update_package(
    package_id: int,
    request: PackageUpsertRequest,
    current_admin: models.WebUser = Depends(get_current_admin),
    db: Session = Depends(get_db),
):
    package = db.query(models.RechargePackage).filter(
        models.RechargePackage.id == package_id
    ).first()
    if not package:
        raise HTTPException(status_code=404, detail="充值套餐不存在")

    package.name = request.name
    package.amount = request.amount
    package.price = Decimal(str(request.price))
    package.bonus = request.bonus
    package.is_active = request.is_active
    package.is_popular = request.is_popular
    package.sort_order = request.sort_order
    db.commit()
    _audit(db, current_admin, "economy_update_package", "package", package.id)
    db.commit()
    return {"success": True}


@admin_router.delete("/economy/packages/{package_id}")
def economy_delete_package(
    package_id: int,
    current_admin: models.WebUser = Depends(get_current_admin),
    db: Session = Depends(get_db),
):
    package = db.query(models.RechargePackage).filter(
        models.RechargePackage.id == package_id
    ).first()
    if not package:
        raise HTTPException(status_code=404, detail="充值套餐不存在")

    in_use = db.query(models.RechargeOrder).filter(
        models.RechargeOrder.package_id == package_id
    ).first()
    if in_use:
        package.is_active = False
        db.commit()
        return {"success": True, "soft_deleted": True,
                "message": "套餐已有订单引用，已停用而非删除"}

    db.delete(package)
    db.commit()
    _audit(db, current_admin, "economy_delete_package", "package", package_id)
    db.commit()
    return {"success": True}


# ---------- 兑换码管理 ----------

class ExchangeCodeBatchRequest(BaseModel):
    count: int = Field(default=1, ge=1, le=100)
    type: str = Field(default="points")  # points / subscription / discount
    points_value: int = Field(default=0, ge=0)
    plan_id: Optional[int] = None
    duration_days: int = Field(default=0, ge=0)
    discount_pct: int = Field(default=0, ge=0, le=99)  # discount 型：实付百分比，85=八五折
    max_uses: int = Field(default=1, ge=1, le=1000)
    expires_days: int = Field(default=30, ge=1, le=3650)
    note: str = ""


@admin_router.get("/economy/exchange-codes")
def economy_list_exchange_codes(
    current_admin: models.WebUser = Depends(get_current_admin),
    db: Session = Depends(get_db),
    limit: int = 100,
):
    codes = db.query(models.ExchangeCode).order_by(
        models.ExchangeCode.created_at.desc()
    ).limit(min(limit, 200)).all()

    plan_ids = {c.plan_id for c in codes if c.plan_id}
    plans = {p.id: p.name for p in db.query(models.SubscriptionPlan).filter(
        models.SubscriptionPlan.id.in_(plan_ids)).all()} if plan_ids else {}

    items = []
    for c in codes:
        used_by = []
        if c.used_by:
            ids = [int(i) for i in str(c.used_by).split(",") if i.strip().isdigit()]
            users = db.query(models.WebUser).filter(models.WebUser.id.in_(ids)).all()
            used_by = [{"id": u.id, "username": u.username} for u in users]
        items.append({
            "id": c.id, "code": c.code, "type": c.type,
            "points_value": c.points_value,
            "discount_pct": c.discount_pct,
            "plan_name": plans.get(c.plan_id),
            "duration_days": c.duration_days,
            "max_uses": c.max_uses, "use_count": c.use_count,
            "is_active": c.is_active, "note": c.note,
            "expires_at": c.expires_at.isoformat() if c.expires_at else None,
            "used_by": used_by,
            "created_at": _log_out(c),
        })
    return {"codes": items}


@admin_router.post("/economy/exchange-codes")
def economy_create_exchange_codes(
    request: ExchangeCodeBatchRequest,
    current_admin: models.WebUser = Depends(get_current_admin),
    db: Session = Depends(get_db),
):
    """批量生成兑换码（积分型 / 订阅型 / 折扣型）"""
    if request.type not in ("points", "subscription", "discount"):
        raise HTTPException(status_code=400, detail="type 必须是 points、subscription 或 discount")
    if request.type == "points" and request.points_value <= 0:
        raise HTTPException(status_code=400, detail="积分型兑换码必须设置 points_value")
    if request.type == "discount" and not (1 <= request.discount_pct <= 99):
        raise HTTPException(status_code=400, detail="折扣型兑换码的 discount_pct 必须在 1-99 之间（85=八五折）")
    if request.type == "subscription":
        if not request.plan_id:
            raise HTTPException(status_code=400, detail="订阅型兑换码必须选择套餐")
        if request.duration_days <= 0:
            raise HTTPException(status_code=400, detail="订阅型兑换码必须设置时长")

    expires_at = datetime.now() + timedelta(days=request.expires_days)
    created = []
    for _ in range(request.count):
        code = models.ExchangeCode(
            code=_generate_code(),
            type=request.type,
            points_value=request.points_value if request.type == "points" else 0,
            plan_id=request.plan_id if request.type == "subscription" else None,
            duration_days=request.duration_days if request.type == "subscription" else 0,
            discount_pct=request.discount_pct if request.type == "discount" else 0,
            max_uses=request.max_uses,
            is_active=True,
            note=request.note or None,
            expires_at=expires_at,
            created_by=current_admin.id,
        )
        db.add(code)
        created.append(code)

    db.commit()
    for c in created:
        db.refresh(c)

    _audit(db, current_admin, "economy_create_exchange_codes", "exchange_code",
           created[0].id if created else None,
           {"count": request.count, "type": request.type})
    db.commit()
    return {
        "success": True,
        "codes": [{"id": c.id, "code": c.code, "type": c.type} for c in created],
    }


class ExchangeCodeUpdateRequest(BaseModel):
    is_active: Optional[bool] = None


@admin_router.put("/economy/exchange-codes/{code_id}")
def economy_update_exchange_code(
    code_id: int,
    request: ExchangeCodeUpdateRequest,
    current_admin: models.WebUser = Depends(get_current_admin),
    db: Session = Depends(get_db),
):
    """启用/停用兑换码"""
    code = db.query(models.ExchangeCode).filter(
        models.ExchangeCode.id == code_id
    ).first()
    if not code:
        raise HTTPException(status_code=404, detail="兑换码不存在")

    code.is_active = request.is_active
    db.commit()
    _audit(db, current_admin, "economy_update_exchange_code", "exchange_code",
           code.id, {"is_active": request.is_active})
    db.commit()
    return {"success": True}


@admin_router.delete("/economy/exchange-codes/{code_id}")
def economy_delete_exchange_code(
    code_id: int,
    current_admin: models.WebUser = Depends(get_current_admin),
    db: Session = Depends(get_db),
):
    """删除兑换码（Fix 5）

    P0-2 修复：检查与删除必须是原子的。此前先查 use_count 再按主键删，
    并发兑换可在检查之后提交使用次数，删除仍执行，删掉已使用记录及其痕迹。
    现在用带 COALESCE(use_count,0)=0 条件的原子 DELETE：0 行说明
    "不存在或并发中被使用"，不存在已在前面排除，故 0 行即并发使用。
    """
    code_row = db.query(models.ExchangeCode.code).filter(
        models.ExchangeCode.id == code_id
    ).first()
    if not code_row:
        raise HTTPException(status_code=404, detail="兑换码不存在")

    deleted = (
        db.query(models.ExchangeCode)
        .filter(
            models.ExchangeCode.id == code_id,
            func.coalesce(models.ExchangeCode.use_count, 0) == 0,
        )
        .delete(synchronize_session=False)
    )
    if deleted == 0:
        # 行存在但 use_count > 0：并发兑换在我们前面检查后使用了它
        db.rollback()
        raise HTTPException(status_code=400, detail="已使用过的码不允许删除（请停用）")

    _audit(db, current_admin, "economy_delete_exchange_code", "exchange_code",
           code_id, {"code": code_row[0]})
    db.commit()
    return {"success": True}


# ---------- 订单管理 ----------

@admin_router.get("/economy/orders")
def economy_list_orders(
    status_filter: Optional[str] = None,
    kind: Optional[str] = None,
    search: str = "",
    limit: int = 50,
    offset: int = 0,
    current_admin: models.WebUser = Depends(get_current_admin),
    db: Session = Depends(get_db),
):
    """充值/订阅订单列表（合并视图）"""
    # F-P1-3：分页参数统一钳制（limit∈[1,200]，offset≥0）
    limit, offset = pagination_params(limit, offset)
    # F-P1-2：两分支各取 offset+limit 条再归并排序。
    # 原实现两分支各按 (offset, limit) 取再合并：全局第 N 页的订单可能落在
    # 某分支被截掉的部分，翻页永久遗漏。归并 K 个有序流取全局 [offset, offset+limit)
    # 时，每流取前 offset+limit 条即足够（全局前 offset+limit 条必来自各流前 offset+limit 条）。
    fetch = offset + limit
    total = 0

    want_recharge = kind in (None, "recharge")
    want_sub = kind in (None, "subscription")

    recharge_rows: list = []
    sub_rows: list = []
    if want_recharge:
        q = db.query(models.RechargeOrder)
        if status_filter:
            q = q.filter(models.RechargeOrder.status == status_filter)
        if search:
            like = f"%{search}%"
            q = q.join(models.WebUser, models.RechargeOrder.user_id == models.WebUser.id).filter(
                or_(models.RechargeOrder.order_id.ilike(like),
                    models.WebUser.username.ilike(like))
            )
        total += q.count()
        # P3-5：PG 下 ORDER BY created_at DESC 默认 NULLS FIRST，而 Python 侧归并
        # `x["created_at"] or ""` 把 NULL 排最后——两边口径对不齐时分页边界可能错位。
        # created_at 列可空（无 nullable=False），故 SQL 侧显式 nullslast() 与 Python 对齐。
        recharge_rows = q.order_by(models.RechargeOrder.created_at.desc().nullslast()).offset(0).limit(fetch).all()

    if want_sub:
        q = db.query(models.SubscriptionOrder)
        if status_filter:
            q = q.filter(models.SubscriptionOrder.status == status_filter)
        if search:
            like = f"%{search}%"
            q = q.join(models.WebUser, models.SubscriptionOrder.user_id == models.WebUser.id).filter(
                or_(models.SubscriptionOrder.order_id.ilike(like),
                    models.WebUser.username.ilike(like))
            )
        total += q.count()
        sub_rows = q.order_by(models.SubscriptionOrder.created_at.desc().nullslast()).offset(0).limit(fetch).all()

    # F-P1-1：两类订单的用户一次 IN 查询（原逐单查询 N+1）
    user_ids = {o.user_id for o in recharge_rows} | {o.user_id for o in sub_rows}
    usernames = {
        u.id: u.username
        for u in db.query(models.WebUser.id, models.WebUser.username)
        .filter(models.WebUser.id.in_(list(user_ids) or [-1])).all()
    } if user_ids else {}

    items = []
    for o in recharge_rows:
        items.append({
            "order_id": o.order_id, "kind": "recharge",
            "item_name": f"积分充值（+{o.amount}）",
            "username": usernames.get(o.user_id, "未知"),
            "user_id": o.user_id,
            "amount": float(o.price), "status": o.status,
            "payment_method": o.payment_method,
            "created_at": o.created_at.isoformat() if o.created_at else None,
            "paid_at": o.paid_at.isoformat() if o.paid_at else None,
        })
    for o in sub_rows:
        items.append({
            "order_id": o.order_id, "kind": "subscription",
            "item_name": f"订阅 - {o.item_name}",
            "username": usernames.get(o.user_id, "未知"),
            "user_id": o.user_id,
            "amount": float(o.amount), "status": o.status,
            "payment_method": o.payment_method,
            "created_at": o.created_at.isoformat() if o.created_at else None,
            "paid_at": o.paid_at.isoformat() if o.paid_at else None,
        })

    items.sort(key=lambda x: x["created_at"] or "", reverse=True)
    return {"total": total, "orders": items[offset:offset + limit]}


def _mark_order_paid(db: Session, order_id: str, admin_id: int) -> list:
    """人工补单的同步核心：查单 → 反幂等 → 履约 → 审计（由路由下放线程池执行）

    独立成模块级函数（而不是嵌在路由里）是因为整段都是同步 SQLAlchemy：
    ``economy._fulfill_order`` 内部一个 await 都没有，一次提交就能把事件循环按住。
    """
    from backend.api.economy import FulfillmentError, _fulfill_order

    recharge_order = db.query(models.RechargeOrder).filter(
        models.RechargeOrder.order_id == order_id
    ).first()
    subscription_order = None
    if not recharge_order:
        subscription_order = db.query(models.SubscriptionOrder).filter(
            models.SubscriptionOrder.order_id == order_id
        ).first()
    if not recharge_order and not subscription_order:
        raise HTTPException(status_code=404, detail="订单不存在")

    if (recharge_order and recharge_order.status == "paid") or (
        subscription_order and subscription_order.status == "paid"
    ):
        raise HTTPException(status_code=400, detail="订单已是已支付状态")

    try:
        pending = _fulfill_order(db, recharge_order=recharge_order,
                                 subscription_order=subscription_order)
        commit_with_retry(db, label="人工补单")
    except FulfillmentError as exc:
        # 缺料（套餐 / 用户已不存在）：订单保持未支付，管理员先修好再补单，
        # 不能出现「标了已支付、权益没发」
        db.rollback()
        raise HTTPException(status_code=400, detail=f"履约失败：{exc}") from exc

    _audit(db, admin_id, "economy_mark_order_paid", "order", None,
           {"order_id": order_id})
    db.commit()
    return pending


@admin_router.post("/economy/orders/{order_id}/mark-paid")
async def economy_mark_order_paid(
    order_id: str,
    current_admin: models.WebUser = Depends(get_current_admin),
    db: Session = Depends(get_db),
):
    """人工补单（标记已支付并履约）— 用于线下收款/支付回调丢失

    履约（``_mark_order_paid``）整段是同步 SQLAlchemy，下放线程池执行；
    通知要走 WebSocket，必须留在事件循环上 await。
    """
    from backend.api.economy import send_fulfill_notifications

    pending = await run_in_threadpool(_mark_order_paid, db, order_id, current_admin.id)

    # 先提交再发通知：履约事务里另开会话写站内信会撞 SQLite 写锁，通知会被静默丢掉
    await send_fulfill_notifications(pending)
    return {"success": True, "message": "订单已标记支付并发货"}


# ==================== 导出 ====================

__all__ = ["admin_router"]


# ==================== 多版本管理（对标 StrmAssistant #4） ====================

@admin_router.get("/media/versions/{item_id}")
def admin_get_versions(
    item_id: str,
    current_admin: models.WebUser = Depends(get_current_admin),
    db: Session = Depends(get_db),
):
    """查看某条目的所有多版本（管理后台用）。"""
    from backend.emby_server import models as em
    from backend.emby_server import merge_versions_worker as _mvw

    item = db.query(em.MediaItem).filter(em.MediaItem.guid == item_id).first()
    if not item:
        raise HTTPException(status_code=404, detail="条目不存在")
    primary_id = item.merged_into_id or item.id
    versions = _mvw.get_alternate_versions(db, primary_id)
    return {
        "primary_id": primary_id,
        "versions": [
            {
                "id": v.id,
                "guid": v.guid,
                "name": v.name,
                "file_path": v.file_path,
                "height": v.height or 0,
                "size": v.size or 0,
                "is_primary": v.id == primary_id,
                "is_merged": v.merged_into_id is not None,
            }
            for v in sorted(versions, key=lambda s: s.id)
        ],
    }


@admin_router.post("/media/versions/{item_id}/unmerge")
def admin_unmerge_version(
    item_id: str,
    current_admin: models.WebUser = Depends(get_current_admin),
    db: Session = Depends(get_db),
):
    """解除单个版本的合并（恢复为独立条目）。"""
    from backend.emby_server import models as em
    from backend.emby_server import merge_versions_worker as _mvw

    item = db.query(em.MediaItem).filter(em.MediaItem.guid == item_id).first()
    if not item:
        raise HTTPException(status_code=404, detail="条目不存在")
    ok = _mvw.unmerge_version(db, item.id)
    if not ok:
        raise HTTPException(status_code=400, detail="该条目未被合并")
    _audit(db, current_admin, "unmerge_version", "media_item", item.id,
           {"name": item.name})
    db.commit()
    return {"success": True, "message": f"已解除合并：{item.name}"}


@admin_router.post("/media/versions/{item_id}/unmerge-all")
def admin_unmerge_all(
    item_id: str,
    current_admin: models.WebUser = Depends(get_current_admin),
    db: Session = Depends(get_db),
):
    """解除某主记录下所有版本的合并。"""
    from backend.emby_server import models as em
    from backend.emby_server import merge_versions_worker as _mvw

    item = db.query(em.MediaItem).filter(em.MediaItem.guid == item_id).first()
    if not item:
        raise HTTPException(status_code=404, detail="条目不存在")
    primary_id = item.merged_into_id or item.id
    count = _mvw.unmerge_all(db, primary_id)
    _audit(db, current_admin, "unmerge_all_versions", "media_item", primary_id,
           {"count": count})
    db.commit()
    return {"success": True, "message": f"已解除 {count} 个版本的合并"}


@admin_router.post("/media/episode-versions/merge")
def admin_merge_episode_versions(
    library_id: Optional[int] = None,
    current_admin: models.WebUser = Depends(get_current_admin),
    db: Session = Depends(get_db),
):
    """手动触发单集多版本合并（存量回填 / 主动整理）。

    同一 (series, season, episode) 的多个文件合并为多版本：
    主记录展示，其他版本 merged_into_id 指向主记录，
    通过 AlternateVersions 查看/选择播放。
    不传 library_id 则全库执行。
    """
    from backend.emby_server import episode_versions as _ev

    if not _ev.EPISODE_VERSIONS_ENABLED:
        raise HTTPException(status_code=400, detail="单集多版本功能已关闭（EPISODE_VERSIONS_ENABLED=0）")
    stats = _ev.merge_episode_versions(db, library_id)
    _audit(db, current_admin, "merge_episode_versions", "library",
           library_id or 0, stats)
    db.commit()
    return {"success": True, **stats}
