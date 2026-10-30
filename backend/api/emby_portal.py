"""用户门户认证 API：注册 / 登录 / JWT 刷新 / 当前用户 / 改密

端点（前缀 /api/user/auth）：
- POST /register          注册新用户，返回 access_token + refresh_token + user
- POST /login             用户名密码登录，返回 access_token + refresh_token + user
- POST /refresh           用 refresh_token 换新 access_token
- GET  /me                当前用户信息（JWT 鉴权）
- POST /logout            登出（无状态 JWT：客户端清除 token 即可）
- POST /change-password   修改密码（JWT 鉴权，需验证旧密码）
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from backend import models, register_channel
from backend.integrations.store import switch_env_value as _switch_env_value
from backend.db_retry import commit_with_retry
from backend.authlog import REASONS as AUTHLOG_REASONS, client_ip as log_ip, record_event, user_agent
from backend import share_guard
from backend.database import get_db
from backend.devices import device_limit
from backend.emby_server.auth import ensure_emby_credentials
from backend.subscriptions import (
    download_allowed,
    has_active_subscription,
    is_free_realm,
    realm_access_note,
    realm_requires_subscription,
)
from backend.ratelimit import check_rate_limit, client_ip, reset_rate_limit
# 人机验证（能力中心）：用户端登录/注册与后台登录共用同一个守卫（含安全日志）
from backend.integrations import captcha
from backend.security import (
    ACCESS_TOKEN_EXPIRE_MINUTES,
    create_access_token,
    create_refresh_token,
    decode_token,
    hash_password,
    resolve_jwt_user_id,
    verify_password,
)

logger = logging.getLogger(__name__)

auth_router = APIRouter(prefix="/api/user/auth", tags=["用户端-认证"])

bearer_scheme = HTTPBearer(auto_error=False)

USERNAME_RE = re.compile(r"^[a-zA-Z0-9_]{3,32}$")


# ==================== Schemas ====================

class RegisterRequest(BaseModel):
    username: str = Field(..., min_length=3, max_length=32)
    password: str = Field(..., min_length=6, max_length=64)
    email: str | None = None
    invitation_code: str | None = None  # 邀请码（选填，双向奖励+返利绑定）
    captcha_token: str | None = None  # 人机验证令牌（管理员开启保护时必填）


class LoginRequest(BaseModel):
    username: str
    password: str
    captcha_token: str | None = None  # 人机验证令牌（管理员开启保护时必填）


class RefreshRequest(BaseModel):
    refresh_token: str


class ChangePasswordRequest(BaseModel):
    old_password: str
    new_password: str = Field(..., min_length=6, max_length=64)


class WornBadgeOut(BaseModel):
    key: str
    name: str
    icon: str


class UserOut(BaseModel):
    id: int
    username: str
    email: str | None = None
    emby_username: str | None = None
    is_vip: bool = False
    is_active: bool = True
    # 是否管理员：用户端据此显示「管理后台」入口（管理后台与门户同一套 JWT）
    is_staff: bool = False
    # 是否公益服用户：活力值等公益专属功能只对这类用户开放，用户端据此
    # 决定要不要发那些专属请求（非公益用户发了必 403，白跑一趟还刷日志）
    is_welfare: bool = False
    # 付费墙是否开启（开启且非会员时播放会被拦截）；公益服恒为 false
    subscription_required: bool = False
    # 站点是否允许下载 + 每用户设备上限（0 表示不限）
    download_allowed: bool = True
    device_limit: int = 0
    # 接入方式（v2.7.0 公益服）：free = 本服免费开放，不需要订阅
    realm_access_mode: str = "paid"
    is_free_realm: bool = False
    # 公益服规则文案（用户端展示；付费服为空串）
    realm_access_note: str = ""
    # 佩戴中的成就徽章（存账号：换设备也在、换账号不串）；没戴为 None
    worn_badge: WornBadgeOut | None = None
    created_at: str | None = None


class AuthResponse(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"
    expires_in: int
    user: UserOut


# ==================== Helpers ====================

def _user_out(user: models.WebUser, db: Session | None = None) -> UserOut:
    """用户信息出参：is_vip 由生效中的订阅派生（无 db 时回退到库内标记）

    ``subscription_required`` 告知前端「这个服要不要会员才能看」，用于提前展示开通引导——
    公益服（``realm_access_mode='free'``）恒为 false，前端据此换一套「免费开放」的文案，
    而不是把一个不需要付费的服宣传成付费墙。
    """
    is_vip = has_active_subscription(db, user.id) if db is not None else bool(user.is_vip)
    requires_sub = realm_requires_subscription(db) if db is not None else False
    free_realm = is_free_realm(db) if db is not None else False
    # 佩戴徽章是纯展示信息：查不到/异常一律 None，绝不拖垮登录出参
    worn_badge = None
    if db is not None:
        try:
            from backend.panel_achievements import worn_badge_payload
            worn_badge = worn_badge_payload(db, user.id)
        except Exception:
            worn_badge = None
    return UserOut(
        id=user.id,
        username=user.username,
        email=user.email,
        emby_username=user.emby_username,
        is_vip=is_vip,
        is_active=user.is_active,
        is_staff=bool(user.is_staff),
        is_welfare=bool(getattr(user, "is_welfare", False)),
        subscription_required=requires_sub,
        download_allowed=download_allowed(db) if db is not None else True,
        device_limit=device_limit(db) if db is not None else 0,
        realm_access_mode="free" if free_realm else "paid",
        is_free_realm=free_realm,
        realm_access_note=realm_access_note(db) if db is not None else "",
        worn_badge=worn_badge,
        created_at=user.created_at.isoformat() if user.created_at else None,
    )


def _issue_auth_response(
    user: models.WebUser, db: Session, plain_password: str | None = None
) -> AuthResponse:
    """签发 access + refresh token。

    不再自动生成接线用户名，也不为还没有接线用户名的用户把门户密码写成播放密码。
    用户去接线员页自行设置（``POST /api/user/emby/password``）。

    兼容：已经有 ``emby_username``、但还没有 ``emby_password`` 的老用户，
    登录时用本次已验证的门户密码补一次（fill_only）。已有播放密码的不被覆盖。
    没带明文密码的路径（刷新后的换票、Telegram 免密登录）什么都不补。
    """
    if plain_password:
        ensure_emby_credentials(db, user, password=plain_password, fill_only=True)
    access = create_access_token(user.id, {"username": user.username},
                                  token_version=user.token_version or 0)
    refresh = create_refresh_token(user.id, token_version=user.token_version or 0)

    return AuthResponse(
        access_token=access,
        refresh_token=refresh,
        expires_in=ACCESS_TOKEN_EXPIRE_MINUTES * 60,
        user=_user_out(user, db),
    )


def get_current_user_jwt(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer_scheme),
    db: Session = Depends(get_db),
) -> models.WebUser:
    """JWT 鉴权依赖：Authorization: Bearer <access_token>"""
    if credentials is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="未提供认证凭证")
    from backend.security import decode_token, enforce_token_freshness
    payload = decode_token(credentials.credentials, expected_type="access")
    if not payload:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="无效或已过期的凭证")
    user_id = resolve_jwt_user_id(credentials.credentials)
    if user_id is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="无效或已过期的凭证")
    user = db.query(models.WebUser).filter(models.WebUser.id == user_id).first()
    if not user:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="用户不存在")
    if not user.is_active:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="用户已被禁用")
    # P2/P3：吊销 + token_version 双检查（横切能力只许一套，见 security.enforce_token_freshness）
    enforce_token_freshness(db, payload, user)
    return user


# ==================== Endpoints ====================

@auth_router.get("/captcha")
def captcha_config(request: Request, db: Session = Depends(get_db)):
    """人机验证挂件信息（公开）：前端据此决定要不要渲染挂件

    只下发公开的站点密钥与脚本地址；私钥永不出现在这里。未配置时 ``enabled=false``，
    前端直接不渲染挂件——不会把站点锁在门外。
    """
    return captcha.widget_info(db)


def _normalize_registration_mode(raw) -> str:
    """注册模式归一：未配置 → open；open/closed 原样；其余（含已下线的 code）→ closed。"""
    value = (raw or "").strip().lower()
    if not value:
        return "open"
    return value if value in ("open", "closed") else "closed"


@auth_router.get("/register-config")
def register_config(db: Session = Depends(get_db)):
    """公开：注册页需要的开关状态（未登录可调）。

    前端据此决定显示/隐藏邀请码输入框、是否关闭注册：
    - registration_mode: open=开放注册 / closed=关闭注册（code 模式已下线，历史值/未知值归一为 closed）
    - invitation_enabled: 邀请码功能开关
    """
    from backend.api.invitation import get_invite_config

    row = (
        db.query(models.SystemConfig)
        .filter(models.SystemConfig.key == "registration_mode")
        .first()
    )
    mode = _normalize_registration_mode(row.value if row else None)
    return {
        "registration_mode": mode,
        "invitation_enabled": get_invite_config(db)["enabled"],
    }


def _get_register_ratelimit(db: Session) -> tuple[bool, int, int]:
    """读取注册限流配置。

    配置键（均存于 SystemConfig 表）：
    - register_ratelimit_enabled：是否启用注册限流，值为 "true"（忽略首尾空格与大小写）时启用，默认启用；
    - register_ratelimit_max：限流时间窗口内允许的最大注册次数，默认 5；
    - register_ratelimit_window：限流时间窗口长度（秒），默认 3600。

    任何读取或转换异常均回退到默认值，保证注册流程不会因配置读取失败而报错。
    非法的非正数值同样回退默认值（管理后台写入时已有 >=1 / >=60 校验，
    这里防的是直接改库的脏数据）。
    """
    enabled = True
    max_events = 5
    window_seconds = 3600
    try:
        # 一次查询取回三个键，避免三次 round trip；每个键独立容错
        rows = (
            db.query(models.SystemConfig)
            .filter(models.SystemConfig.key.in_([
                "register_ratelimit_enabled",
                "register_ratelimit_max",
                "register_ratelimit_window",
            ]))
            .all()
        )
        values = {r.key: r.value for r in rows if r.value}
    except Exception:
        values = {}
    # A 类摘面板开关（2026-10-21）：env 显式设置即接管，口径见 integrations.store
    _env_v = _switch_env_value("register_ratelimit_enabled")
    if _env_v is not None:
        values["register_ratelimit_enabled"] = _env_v
    if "register_ratelimit_enabled" in values:
        enabled = values["register_ratelimit_enabled"].strip().lower() == "true"
    try:
        if "register_ratelimit_max" in values:
            v = int(values["register_ratelimit_max"])
            max_events = v if v >= 1 else 5
    except (TypeError, ValueError):
        pass
    try:
        if "register_ratelimit_window" in values:
            v = int(values["register_ratelimit_window"])
            window_seconds = v if v >= 1 else 3600
    except (TypeError, ValueError):
        pass
    return enabled, max_events, window_seconds


# 公益服冷启动赠送（2026-10-28）：新注册默认送 30 天公益资格 + 30 天查看行，
# 均不扣积分。资格复用 grant_welfare（channel=register_gift）；查看行复用
# EmbyViewUnlock（points_spent=0 标记赠送）。只给新用户，不动现有用户行。
# 2026-10-29 用户拍板：公益资格改由总开关 register_gift_welfare_enabled
# 控制、默认关（开放注册但不送资格，管理端可重开）。
# 2026-10-29 用户再拍板：查看凭证改由总开关 register_gift_view_enabled
# 控制、默认关（新注册不送凭证、可以送资格）；两个开关彼此独立。
# 天数走 SystemConfig（管理端公益配置里改；未配行／非法行回退下面默认值）：
# register_gift_welfare_days（0=永不过期，grant_welfare 口径），
# register_gift_view_days（最小 1；0 天按 timedelta 算是“立即过期”，不是永久）。
# 注意：关资格只能用上面的布尔开关，天数 0 是“永不过期”，不是关闭。
REGISTER_GIFT_WELFARE_DAYS = 30
REGISTER_GIFT_VIEW_DAYS = 30


def _get_register_gift_days(db, key, default, min_value):
    """读新注册赠送天数：直查 SystemConfig，无进程缓存，管理端保存即生效。"""
    row = db.query(models.SystemConfig).filter(models.SystemConfig.key == key).first()
    if row is None or row.value is None or str(row.value).strip() == "":
        return default
    try:
        value = int(str(row.value).strip())
    except (TypeError, ValueError):
        return default
    return value if value >= min_value else default


def _get_register_gift_welfare_enabled(db):
    """读新注册送公益资格总开关：直查 SystemConfig，无进程缓存，保存即生效。

    默认 False（2026-10-29 用户拍板不送）；行缺失／空值／非法值一律按关。
    真值口径与 config_schema 的布尔真值集对齐（"1"/"true"/"yes"/"on"）。
    """
    row = db.query(models.SystemConfig).filter(
        models.SystemConfig.key == "register_gift_welfare_enabled").first()
    if row is None or row.value is None:
        return False
    return str(row.value).strip().lower() in ("1", "true", "yes", "on")


def _get_register_gift_view_enabled(db):
    """读新注册送查看凭证总开关：直查 SystemConfig，无进程缓存，保存即生效。

    默认 False（2026-10-29 用户拍板不送）；行缺失／空值／非法值一律按关。
    真值口径与 config_schema 的布尔真值集对齐（"1"/"true"/"yes"/"on"）。
    与资格开关 register_gift_welfare_enabled 彼此独立（那把归那把读）。
    """
    row = db.query(models.SystemConfig).filter(
        models.SystemConfig.key == "register_gift_view_enabled").first()
    if row is None or row.value is None:
        return False
    return str(row.value).strip().lower() in ("1", "true", "yes", "on")


def _grant_register_gifts(db, user):
    """新注册冷启动赠送：资格与查看行各由自己的总开关把关，彼此独立。

    资格开关 register_gift_welfare_enabled、凭证开关
    register_gift_view_enabled 均默认关（2026-10-29 用户拍板：开放注册
    默认不送资格、不送凭证，管理端可分别重开）；任一开时行为与此前
    一致，天数走 register_gift_welfare_days（0=永不过期口径不变）／
    register_gift_view_days（最小 1 口径不变）。
    """
    from backend import realms
    from backend.emby_server.portal import grant_welfare

    if _get_register_gift_welfare_enabled(db):
        welfare_days = _get_register_gift_days(
            db, "register_gift_welfare_days", REGISTER_GIFT_WELFARE_DAYS, 0)
        grant_welfare(db, user, channel="register_gift", days=welfare_days)
    else:
        logger.info("新注册送公益资格开关关闭，跳过资格赠送: user=%s", user.id)
    if not _get_register_gift_view_enabled(db):
        logger.info("新注册送查看凭证开关关闭，跳过凭证赠送: user=%s", user.id)
        return
    view_days = _get_register_gift_days(
        db, "register_gift_view_days", REGISTER_GIFT_VIEW_DAYS, 1)
    realm_id = realms.active_realm_id(db)
    existing = (
        db.query(models.EmbyViewUnlock)
        .filter(models.EmbyViewUnlock.user_id == user.id,
                models.EmbyViewUnlock.realm_id == realm_id)
        .first()
    )
    if existing is None:
        now = datetime.now()
        db.add(models.EmbyViewUnlock(
            user_id=user.id,
            realm_id=realm_id,
            unlocked_at=now,
            expires_at=now + timedelta(days=view_days),
            points_spent=0,
        ))
        db.commit()


@auth_router.post("/register", response_model=AuthResponse, status_code=status.HTTP_201_CREATED)
def register(request: Request, req: RegisterRequest, db: Session = Depends(get_db)):
    """注册新用户（用户名唯一，密码 bcrypt 存储）。

    不自动生成接线用户名或播放密码；用户在接线员页自行设置。
    门户用户名也不能与别人已有的接线用户名相同：客户端登录两个字段都认。
    """
    rl_enabled, rl_max, rl_window = _get_register_ratelimit(db)
    rl_key = f"register:{client_ip(request)}"
    if rl_enabled:
        allowed, retry_after = check_rate_limit(rl_key, rl_max, rl_window)
        if not allowed:
            raise HTTPException(status_code=429, detail="注册过于频繁，请稍后再试")
    # 人机验证（能力中心）：未配置 / 未开保护时直接放行；失败一律 400 + 安全日志
    captcha.guard(db, request, "register", req.captcha_token, username=req.username.strip())
    username = req.username.strip()
    if not USERNAME_RE.match(username):
        raise HTTPException(
            status_code=400,
            detail="用户名只能包含字母、数字、下划线，长度 3-32 位",
        )

    # ===== 注册模式开关（r4-auth：必须在撞名检查之前）=====
    # open: 开放注册 / closed: 关闭注册
    # 「code」（注册码门禁）已于 v2.7.x 下线。DB 里残留的 "code"（及任何未知值）
    # 一律按 closed 处理（fail-closed）：管理员当初设 code 是为了「不对外开放」，
    # 升级后不能静默变成开放注册，需管理员在后台显式改成 open。
    # 卡码体系（钱包/个人中心核销注册码/续期码/白名单码）不受影响。
    # 顺序是安全语义：旧顺序先查撞名、后查模式，关闭注册时探一个已占用的
    # 名字回 409、探未占用的名字回 403，等于用注册接口枚举门户名与接线名
    # 是否存在。先判模式，关闭时一律 403，不泄露撞名信息。
    mode_config = db.query(models.SystemConfig).filter(
        models.SystemConfig.key == "registration_mode"
    ).first()
    reg_mode = _normalize_registration_mode(mode_config.value if mode_config else None)

    if reg_mode == "closed":
        closed_msg_config = db.query(models.SystemConfig).filter(
            models.SystemConfig.key == "registration_closed_message"
        ).first()
        raise HTTPException(
            status_code=403,
            detail=(closed_msg_config.value if closed_msg_config and closed_msg_config.value
                    else "当前未开放注册"),
        )

    existing = db.query(models.WebUser).filter(models.WebUser.username == username).first()
    if existing:
        raise HTTPException(status_code=409, detail="用户名已被注册")
    # 新用户自己还没有接线名，只查别人。撞上已有 emby_username 时
    # AuthenticateByName 的 OR 查询会不确定命中谁，与设号撞名同一 409。
    taken_emby = (
        db.query(models.WebUser.id)
        .filter(models.WebUser.emby_username == username)
        .first()
    )
    if taken_emby:
        raise HTTPException(status_code=409, detail="用户名已被占用")
    if req.email:
        email = req.email.strip()
        if "@" not in email:
            raise HTTPException(status_code=400, detail="邮箱格式不正确")
        existing_email = db.query(models.WebUser).filter(models.WebUser.email == email).first()
        if existing_email:
            raise HTTPException(status_code=409, detail="邮箱已被注册")

    user = models.WebUser(
        username=username,
        password_hash=hash_password(req.password),
        email=req.email.strip() if req.email else None,
        is_active=True,
        # 注册渠道归因（v2.44.0）：注册码门禁已下线，新用户只有开放注册/邀请两种入口
        register_channel=register_channel.OPEN,
    )
    db.add(user)
    commit_with_retry(db, label="注册落库")  # 一次提交落盘
    db.refresh(user)

    # 邀请返利：注册时应用邀请码（双向发奖，失败静默不阻塞注册）
    if req.invitation_code:
        try:
            from backend.api.invitation import apply_invitation

            result = apply_invitation(db, user, req.invitation_code)
            # 归因收尾：码无效/被拒时这个号就是自己注册的，记成 invitation 会把
            # 渠道分析带偏。优先级（邀请码 > 开放）由 resolve 统一裁决。
            user.register_channel = register_channel.resolve(
                user.register_channel, bool((result or {}).get("applied")))
            db.commit()
        except Exception:  # noqa: BLE001 — 邀请奖励失败不阻塞注册
            db.rollback()
            logger.warning("邀请码应用失败: user=%s code=%s", user.id, req.invitation_code,
                           exc_info=True)

    # 公益服冷启动赠送：失败静默不阻塞注册（与邀请奖励同一容错口径）。
    try:
        _grant_register_gifts(db, user)
    except Exception:  # noqa: BLE001 — 赠送失败不阻塞注册
        db.rollback()
        logger.warning("新注册赠送失败: user=%s", user.id, exc_info=True)

    logger.info("新用户注册: %s (id=%s, mode=%s)", username, user.id, reg_mode)
    # 计数语义：注册成功后退还该 IP 的注册限流计数，失败尝试（验证码错 /
    # 用户名已存在 / 邮箱已注册等）保留计数防爆破。否则同一 NAT 出口下
    # 一个人注册成功，就把同出口后来者的 5 次/小时额度白白烧掉一格。
    if rl_enabled:
        reset_rate_limit(rl_key)
    return _issue_auth_response(user, db, plain_password=req.password)


@auth_router.post("/login", response_model=AuthResponse)
def login(request: Request, req: LoginRequest, db: Session = Depends(get_db)):
    """用户名密码登录，成功返回 JWT（同 IP 每分钟最多 8 次尝试）"""
    allowed, retry_after = check_rate_limit(f"login:{client_ip(request)}", 8, 60)
    if not allowed:
        raise HTTPException(status_code=429, detail="尝试过于频繁，请稍后再试")
    captcha.guard(db, request, "login", req.captcha_token, username=req.username.strip())
    user = (
        db.query(models.WebUser)
        .filter(models.WebUser.username == req.username.strip())
        .first()
    )
    if not user or not verify_password(req.password, user.password_hash):
        record_event(
            db, username=req.username.strip(), user_id=user.id if user else None,
            ip=log_ip(request), agent=user_agent(request), success=False,
            reason="portal_login_failed", detail="用户名或密码错误",
        )
        raise HTTPException(status_code=401, detail="用户名或密码错误")
    if not user.is_active:
        record_event(
            db, username=user.username, user_id=user.id, ip=log_ip(request),
            agent=user_agent(request), success=False, reason="portal_login_failed",
            detail="账户已被禁用",
        )
        raise HTTPException(status_code=403, detail="账户已被禁用")

    user.last_login_at = datetime.now()
    db.commit()
    record_event(
        db, username=user.username, user_id=user.id, ip=log_ip(request),
        agent=user_agent(request), success=True, reason="portal_login",
    )
    # 防共享·跨城市轨迹（默认 off，不判定也不写库）。命中 enforce 时账号已被停用，
    # 这里直接拒绝本次登录——先落安全日志，管理员才能查到「为什么这个号突然登不上」
    verdict = share_guard.note_activity(db, user, log_ip(request))
    if verdict and verdict.get("blocked"):
        raise HTTPException(
            status_code=403,
            detail="检测到该账号在短时间内于多个城市登录，已被暂停使用，请联系管理员",
        )
    # 已有接线用户名但还没有播放密码的老用户：登录成功后用已验证的门户密码补一次。
    # 没有接线用户名的新用户什么都不写，去接线员页自己设。
    return _issue_auth_response(user, db, plain_password=req.password)


# E1（P2-6）：refresh 轮换容忍窗口——客户端成功换新后因网络重试/并发双发再次携带
# 旧 refresh 时，旧 jti 虽已在吊销表但属良性重复，不应触发应急吊销（误杀全部会话）。
REFRESH_ROTATION_GRACE_SEC = 120


def _refresh_rotation_in_grace(db, old_jti: str) -> bool:
    """判断旧 refresh 的 jti 是否处于轮换容忍窗口内（视为良性重复）。

    仅当吊销原因恰为 refresh_rotation，且吊销时间在 REFRESH_ROTATION_GRACE_SEC 内返回 True；
    行不存在 / 原因不符 / 已超窗 均返回 False。
    """
    if not old_jti:
        return False
    # 延迟导入，避免循环导入
    from datetime import datetime, timedelta
    from backend.models import RevokedJwt

    row = db.query(RevokedJwt).filter(RevokedJwt.jti == old_jti).first()
    if row is None:
        return False
    if row.reason != "refresh_rotation":
        return False
    return (datetime.now() - row.revoked_at) <= timedelta(seconds=REFRESH_ROTATION_GRACE_SEC)


@auth_router.post("/refresh")
def refresh(req: RefreshRequest, db: Session = Depends(get_db)):
    """用 refresh_token 换取新的 access_token（+ 可选轮换的 refresh_token）"""
    payload = decode_token(req.refresh_token, expected_type="refresh")
    if not payload:
        raise HTTPException(status_code=401, detail="无效或已过期的刷新凭证")
    try:
        user_id = int(payload.get("sub"))
    except (TypeError, ValueError):
        raise HTTPException(status_code=401, detail="无效的刷新凭证")

    user = db.query(models.WebUser).filter(models.WebUser.id == user_id).first()
    if not user or not user.is_active:
        raise HTTPException(status_code=401, detail="用户不存在或已禁用")

    from backend.security import (
        enforce_token_freshness, is_jti_revoked, revoke_all_user_tokens, revoke_jti,
    )

    # A-P1-1：refresh 复用检测——旧 refresh 的 jti 已在吊销表中，
    # 说明同一凭证被重复使用（疑似泄露），触发应急响应吊销该用户全部 token。
    # P2-6：轮换容忍窗口内的重试视为良性重复（网络重试/并发双发），放行不误杀。
    old_jti = payload.get("jti")
    reuse_suspected = bool(old_jti) and is_jti_revoked(db, old_jti)
    in_grace = reuse_suspected and _refresh_rotation_in_grace(db, old_jti)
    if reuse_suspected and not in_grace:
        revoke_all_user_tokens(db, user.id, reason="refresh_reuse_detected")  # 内部原子 bump + commit
        raise HTTPException(status_code=401, detail="检测到异常的刷新凭证，已吊销全部会话")
    if in_grace:
        # P1（安全）：容忍窗口只豁免"旧 jti 已在吊销表"这一项，不能豁免改密/登出。
        # 注意：此处绝对不能调用 enforce_token_freshness——它内部会检查 payload["jti"] 是否在吊销表，
        # 而轮换时旧 refresh 的 jti 已被 revoke（refresh_rotation），直接调用会把合法的良性重试误杀。
        # 因此这里只单独比对 token 版本号，语义与 enforce_token_freshness 中的 tv 校验保持一致：
        # 改密/登出 bump token_version 后，容忍窗口内旧 refresh 重放必须 401，否则 120s 内
        # 旧凭证仍能换出新 access，违反"改密/登出后旧 token 立即作废"的安全契约。
        if int(payload.get("tv", 0) or 0) != int(getattr(user, "token_version", 0) or 0):
            raise HTTPException(status_code=401, detail="密码已变更，请重新登录")
        logger.warning("refresh 轮换容忍窗口内重试 jti=%s user_id=%s：视为良性重复，放行，不触发应急吊销",
                       old_jti, user.id)
    else:
        # P3（审查第八批）：改密/登出后旧 refresh_token 作废，否则换新 access 形同虚设
        enforce_token_freshness(db, payload, user)

    # A-P1-1：轮换——旧 refresh 立即进吊销表，避免同一 refresh 被无限重用
    if old_jti:
        revoke_jti(db, old_jti, user.id, reason="refresh_rotation")

    return {
        "access_token": create_access_token(user.id, {"username": user.username},
                                          token_version=user.token_version or 0),
        "refresh_token": create_refresh_token(user.id, token_version=user.token_version or 0),  # 轮换
        "token_type": "bearer",
        "expires_in": ACCESS_TOKEN_EXPIRE_MINUTES * 60,
        "user": _user_out(user, db),
    }


@auth_router.get("/me", response_model=UserOut)
def me(
    current_user: models.WebUser = Depends(get_current_user_jwt),
    db: Session = Depends(get_db),
):
    """当前登录用户信息（含派生的 VIP 状态）"""
    return _user_out(current_user, db)


@auth_router.post("/logout")
def logout(request: Request,
           current_user: models.WebUser = Depends(get_current_user_jwt),
           db: Session = Depends(get_db)):
    """登出：吊销当前 access token 的 jti，并 bump token_version 使全部 refresh 失效。

    P2 修复（审查）：此前登出只是客户端清除，服务端无吊销能力，
    偷到的 token 在过期前一直有效。
    A-P1-1 修复：仅吊销 access 的 jti 时 refresh 仍可无限续签，登出形同虚设；
    此处同步 bump token_version，令该用户所有已签发的 refresh（及 access）立即失效。
    """
    from backend.security import decode_token, revoke_jti
    token = None
    try:
        auth = request.headers.get("Authorization", "")
        if auth.lower().startswith("bearer "):
            token = auth[7:].strip()
    except Exception:
        pass
    # 1) 吊销当前 access token，缩短其残余有效窗口
    if token:
        payload = decode_token(token, expected_type="access")
        if payload:
            revoke_jti(db, payload.get("jti"), current_user.id, reason="logout")
    # 2) A-P1-1：bump token_version —— 该用户全部已签发 refresh 立即失效，
    #    否则攻击者可用旧 refresh 无限换取新 access，登出形同虚设
    try:
        current_user.token_version = (current_user.token_version or 0) + 1
        db.commit()
    except Exception:
        db.rollback()
        raise
    return {"success": True, "message": "已登出"}


class TgLoginRequest(BaseModel):
    token: str


@auth_router.post("/tg-login")
def tg_login(request: Request, req: TgLoginRequest, db: Session = Depends(get_db)):
    """bot 一键免密登录：一次性 token 换本站登录态（与密码登录一样记日志 + 防共享判定）"""
    from backend.tg_bot import login_token as _lt
    payload = _lt.consume_login_token((req.token or "").strip())
    if not payload:
        raise HTTPException(status_code=401, detail="登录链接无效或已过期，请在机器人中重新获取")
    try:
        user_id = int(payload.get("user_id"))
    except (TypeError, ValueError):
        raise HTTPException(status_code=401, detail="登录链接无效")
    user = db.query(models.WebUser).filter(models.WebUser.id == user_id).first()
    if not user or not user.is_active:
        raise HTTPException(status_code=401, detail="用户不存在或已禁用")
    if user.telegram_id != payload.get("telegram_id"):
        raise HTTPException(status_code=401, detail="身份校验失败")
    user.last_login_at = datetime.now()
    db.commit()
    record_event(
        db, username=user.username, user_id=user.id, ip=log_ip(request),
        agent=user_agent(request), success=True, reason="tg_login",
    )
    verdict = share_guard.note_activity(db, user, log_ip(request))
    if verdict and verdict.get("blocked"):
        raise HTTPException(
            status_code=403,
            detail="检测到该账号在短时间内于多个城市登录，已被暂停使用，请联系管理员",
        )
    return _issue_auth_response(user, db)


@auth_router.post("/change-password")
def change_password(
    req: ChangePasswordRequest,
    current_user: models.WebUser = Depends(get_current_user_jwt),
    db: Session = Depends(get_db),
):
    """修改密码（验证旧密码，同步更新自建 Emby 播放密码）"""
    if not verify_password(req.old_password, current_user.password_hash):
        raise HTTPException(status_code=400, detail="旧密码错误")
    if req.old_password == req.new_password:
        raise HTTPException(status_code=400, detail="新密码不能与旧密码相同")

    current_user.password_hash = hash_password(req.new_password)
    # 同步自建 Emby 播放密码（bcrypt 哈希存储），保持两端一致
    current_user.emby_password = hash_password(req.new_password)
    # P3 修复（审查）：改密码后旧 token 全部作废（防盗号后持续登录）
    current_user.token_version = int(current_user.token_version or 0) + 1
    db.commit()
    return {"success": True, "message": "密码已更新"}


@auth_router.get("/login-history")
def get_login_history(
    limit: int = 20,
    offset: int = 0,
    current_user: models.WebUser = Depends(get_current_user_jwt),
    db: Session = Depends(get_db),
):
    """本人登录记录（只读）：只查自己名下的登录与安全事件，分页倒序。

    字段白名单：时间 / IP / 地区 / 设备（UA）/ 成败 / 事件类别。
    不返回 detail（内部备注口径）等白名单外字段；查询条件写死
    user_id == 当前登录用户，不接受任何指定他人的参数。
    """
    limit = min(max(limit, 1), 100)
    offset = max(offset, 0)
    q = db.query(models.LoginLog).filter(
        models.LoginLog.user_id == current_user.id)
    total = q.count()
    rows = (
        q.order_by(models.LoginLog.created_at.desc(), models.LoginLog.id.desc())
        .offset(offset)
        .limit(limit)
        .all()
    )
    return {
        "total": total,
        "items": [
            {
                "created_at": r.created_at.isoformat() if r.created_at else None,
                "ip": r.ip,
                "region": r.region or "",
                "user_agent": r.user_agent,
                "success": bool(r.success),
                "reason": r.reason,
                "reason_label": AUTHLOG_REASONS.get(r.reason or "", r.reason or "-"),
            }
            for r in rows
        ],
    }
