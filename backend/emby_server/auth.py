"""自建 Emby 服务器：认证与 Token 管理"""
import hashlib
import logging
import secrets
from datetime import datetime
from typing import Optional

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.orm import Session
from backend.emby_server.env_util import env_float


def hash_emby_token(token_value: str) -> str:
    """Emby 客户端 token 的 SHA256 哈希。

    P1 安全修复：库里只存哈希不存明文（防数据库文件泄露导致会话被接管）。
    签发时把明文给客户端一次，之后所有校验都是"哈希后再比对"。
    """
    return hashlib.sha256(token_value.encode("utf-8")).hexdigest()

from backend import models
from backend.database import get_db
from backend.emby_server import models as emby_models
from backend.security import hash_password, needs_password_rehash, verify_password

bearer_scheme = HTTPBearer(auto_error=False)

# 客户端 token 的 last_used_at 写库节流间隔（秒）
TOKEN_TOUCH_INTERVAL = env_float("EMBY_TOKEN_TOUCH_SECONDS", 60.0)

#: 鉴权腿计数 Redis 哈希（播放全切观测口径：哪条鉴权腿还在被真实请求走）。
#: 读取：docker exec aetrix-redis redis-cli HGETALL aetrix:auth_legs:v1
#: 字段：play_sign（短期签名）/ emby_token（Emby 客户端 token）/
#: emby_jwt（get_emby_user 的 JWT 回退）/ resolve_token / resolve_jwt
#: （resolve_request_user 的两腿，供下载策略侧观测）。
AUTH_LEG_COUNTER_KEY = "aetrix:auth_legs:v1"


def _count_auth_leg(leg: str) -> None:
    """鉴权腿计数（best-effort）：Redis 不可用/出错时静默跳过，绝不影响鉴权。"""
    try:
        from backend.database import redis_call

        redis_call(lambda c: c.hincrby(AUTH_LEG_COUNTER_KEY, leg, 1))
    except Exception:  # noqa: BLE001 - 计数失败不许影响鉴权
        pass

# 请求头里的客户端信息（X-Emby-Authorization: MediaBrowser Client="...", Device="...", DeviceId="...", Version="..."）
DEVICE_HEADERS = ["X-Emby-Authorization", "X-MediaBrowser-Token"]


def parse_emby_authorization(header_value: Optional[str]) -> dict:
    """解析 X-Emby-Authorization 头"""
    result: dict = {}
    if not header_value:
        return result
    for part in header_value.split(","):
        part = part.strip()
        if "=" in part:
            key, _, value = part.partition("=")
            key = key.strip()
            # 首个字段带着认证方案前缀，例如 "MediaBrowser Client=\"Infuse\""；
            # 不剥掉前缀就取不到 Client，设备审查里所有客户端都会显示成默认名。
            if " " in key:
                key = key.split(" ", 1)[1].strip()
            value = value.strip().strip('"')
            if key:
                result[key] = value
    return result


def verify_emby_password(plain: str, stored: str) -> bool:
    """校验 Emby 播放密码：新 bcrypt 摘要、旧 bcrypt 与历史明文均兼容。"""
    if not stored:
        return False
    if stored.startswith(("$2", "$bcrypt-sha256$")):
        return verify_password(plain, stored)
    try:
        return secrets.compare_digest(plain.encode("utf-8"), stored.encode("utf-8"))
    except UnicodeEncodeError:
        return False


def verify_and_migrate_emby_password(db: Session, user: models.WebUser, password: str) -> bool:
    """校验 Emby 播放密码；成功时若为明文/旧格式则透明升级为 bcrypt-sha256（A-P1-3）。

    把"校验+升级"收敛到这一处：调用方不再需要手写 startswith 分支判断，
    避免将来只改一处导致的升级遗漏。ensure_emby_credentials 内部已含
    needs_password_rehash 判断，明文/旧 $2 格式会在成功登录后被升级，
    已是新格式时为无操作。
    """
    if not verify_emby_password(password, user.emby_password or ""):
        return False
    ensure_emby_credentials(db, user, password=password)
    return True


def ensure_emby_credentials(db: Session, user: models.WebUser, password: Optional[str] = None,
                            fill_only: bool = False) -> Optional[str]:
    """按需写入 Emby 播放密码。从不自动生成 ``emby_username``。

    接线用户名只能由用户在 ``POST /api/user/emby/password`` 首次提交时写入，
    写好后不可改。本函数不创建、不修改用户名。

    fill_only=True（门户登录专用）：
    - 没有 ``emby_username``：什么都不做。新用户去接线员页自己设账号，
      登录不再把门户密码当成播放密码。
    - 已有用户名但还没有播放密码：用本次门户密码补一次（老数据兼容）。
    - 已有播放密码：绝不覆盖。接线员页自设的密码曾被每次网站登录顶掉，
      播放器攥着旧凭据拿 401（2026-10-23 修）。

    fill_only=False（显式改密 / 管理员重置 / 登录后透明升级哈希）：
    传入 ``password`` 时写入哈希（相同且已是新格式则跳过）。
    没有用户名时仍允许写入密码哈希，供管理员重置、建号脚本和部署自检使用；
    用户端接口在调用前自行拒绝「还没有用户名就只改密码」。
    """
    changed = False
    # 没有接线用户名时，登录路径不能顺手补密码；显式调用不受这条限制。
    if password is not None and not (fill_only and (not user.emby_username or user.emby_password)):
        # 门户登录会频繁调用本函数：相同的新格式哈希不应每次都重新 bcrypt，
        # 否则登录一次就产生一次昂贵哈希并迫使所有旧客户端重新走同步。
        same = bool(user.emby_password) and verify_emby_password(password, user.emby_password)
        if not same or needs_password_rehash(user.emby_password or ""):
            user.emby_password = hash_password(password)
            changed = True
    if changed:
        db.commit()
    return user.emby_password if password is not None else None


def issue_token(db: Session, user: models.WebUser, request: Request) -> tuple[str, emby_models.EmbyApiToken]:
    """为用户签发 Emby 客户端 Token（每次登录签发新行，与官方 Emby 一致：

    AuthenticateByName 每次调用都产生一个新会话/token，旧 token 在过期或注销前
    仍然有效，不会被新登录顶掉；r50 修正 docstring，此前"同设备复用"的写法与实现不符）。

    签发前先做设备登记与设备数上限校验；超限时抛 ``DeviceLimitExceeded``
    （由 AuthenticateByName 转成可读响应），避免在函数内部直接返回 HTTP 错误。
    """
    auth = parse_emby_authorization(request.headers.get("X-Emby-Authorization"))
    # P3 修复（审查）：客户端 DeviceId 头未经截断直接写入 EmbyApiToken.device_id
    #（String(100)），超长 → DB 报错 → 登录 500。截断到 100。
    device_id = (auth.get("DeviceId") or request.headers.get("X-Device-Id")
                 or "unknown-device")[:100]
    app_name = auth.get("Client") or "Emby Client"
    app_version = auth.get("Version") or "1.0"

    from backend.authlog import client_ip
    from backend.devices import register_device

    register_device(
        db, user,
        device_id=device_id,
        name=auth.get("Device") or app_name,
        client=app_name,
        app_version=app_version,
        ip=client_ip(request),
    )

    token_value = secrets.token_hex(20)

    from backend.emby_server import play_sign

    row = emby_models.EmbyApiToken(
        token=hash_emby_token(token_value),  # P1：只存哈希，不存明文
        user_id=user.id,
        device_id=device_id,
        app_name=app_name,
        app_version=app_version,
        last_ip=client_ip(request),
        # token 有效期：新签发默认 30 天；过期后客户端需重新登录
        expires_at=play_sign.token_expiry_default(),
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return token_value, row


def _token_from_request(request: Request) -> Optional[str]:
    token = request.headers.get("X-Emby-Token") or request.headers.get("X-MediaBrowser-Token")
    if token:
        return token.strip()
    # r50 补齐官方口径：X-Emby-Authorization 头里的 Token="" 字段
    auth_header = parse_emby_authorization(request.headers.get("X-Emby-Authorization"))
    token = (auth_header.get("Token") or "").strip()
    if token:
        return token
    auth = request.headers.get("Authorization") or request.headers.get("X-Emby-Authorization")
    if auth and auth.lower().startswith("bearer "):
        return auth[7:].strip()
    # Emby 传统 query 参数 api_key
    token = request.query_params.get("api_key")
    if token:
        return token
    return None


def resolve_token(db: Session, request: Request) -> Optional[tuple[models.WebUser, emby_models.EmbyApiToken]]:
    """从请求解析 Emby token，返回 (user, token_row)"""
    token_value = _token_from_request(request)
    if not token_value:
        return None
    row = (
        db.query(emby_models.EmbyApiToken)
        .filter(
            emby_models.EmbyApiToken.token == hash_emby_token(token_value),  # P1：哈希后比对
            emby_models.EmbyApiToken.is_revoked == False,  # noqa: E712
        )
        .first()
    )
    if not row:
        return None
    from backend.emby_server import play_sign

    if not play_sign.token_is_usable(row):
        # 过期/吊销的 token 一律视为无效，客户端需重新走 AuthenticateByName
        return None
    user = db.query(models.WebUser).filter(models.WebUser.id == row.user_id).first()
    if not user or not user.is_active:
        return None
    # 令牌被使用即视为设备在线（带节流，避免每个请求都写库）
    from backend.authlog import client_ip
    from backend.devices import touch_device

    touch_device(db, user, row.device_id, client_ip(request))
    return user, row


def resolve_request_user(db: Session, request: Request) -> Optional[models.WebUser]:
    """解析请求对应的用户：只认 Emby 客户端 token。

    与 ``get_emby_user`` 同一口径，但不抛异常、可用于依赖注入之外的位置
    （例如下载策略的网关级中间件）。解析不到用户时返回 None。
    """
    resolved = resolve_token(db, request)
    if resolved is not None:
        _count_auth_leg("resolve_token")
        return resolved[0]
    # JWT 回退已删（2026-10-25，/watch 下掉后无网页端播放依赖）：
    # 拿不到 token 即 None，交回调用方处理（下载策略侧通常是 401），不改错误语义。
    return None


def get_emby_user(
    request: Request,
    db: Session = Depends(get_db),
    credentials: HTTPAuthorizationCredentials = Depends(bearer_scheme),
):
    """Emby 客户端鉴权依赖：只认 Emby 客户端 token（AuthenticateByName 签发）。

    门户 JWT 回退已删（2026-10-25，/watch 下掉后无网页端播放依赖）：
    JWT 不再被接受，第三方客户端只走 token 腿；未带有效 token 即 401。
    """
    result = None
    if credentials and credentials.credentials:
        row = (
            db.query(emby_models.EmbyApiToken)
            .filter(
                emby_models.EmbyApiToken.token == hash_emby_token(credentials.credentials),  # P1：哈希后比对
                emby_models.EmbyApiToken.is_revoked == False,  # noqa: E712
            )
            .first()
        )
        if row:
            from backend.emby_server import play_sign

            if not play_sign.token_is_usable(row):
                row = None
        if row:
            user = db.query(models.WebUser).filter(models.WebUser.id == row.user_id).first()
            if user and user.is_active:
                result = (user, row)
    if result is None:
        result = resolve_token(db, request)
    if result is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid access token",
        )
    user, token_row = result
    _count_auth_leg("emby_token")
    # 每个播放请求都写一次 last_used_at = 每个请求都要等一次提交（SQLite 下是一次 fsync）。
    # 客户端轮询进度、请求切片非常密集，而这个字段只是「最近使用时间」，
    # 按间隔节流写库即可（EMBY_TOKEN_TOUCH_SECONDS，设为 0 回到每次请求都写）。
    now = datetime.now()
    if TOKEN_TOUCH_INTERVAL <= 0 or (
        token_row.last_used_at is None
        or (now - token_row.last_used_at).total_seconds() >= TOKEN_TOUCH_INTERVAL
    ):
        token_row.last_used_at = now
        db.commit()
    return user


def get_admin_or_emby_user(request: Request, db: Session = Depends(get_db)) -> models.WebUser:
    """门户端鉴权：JWT 优先，回退到 Emby 客户端 token

    只接受本项目签发的 JWT 或自建 Emby 的客户端 token。
    「纯数字即 user_id」的旧版兼容分支已彻底移除：它让任何人只要猜到一个小整数
    就能冒充任意用户，且历史部署里曾被默认开启。
    """
    # H2：JWT 只从 Authorization 头取，URL 查询串里的 JWT 一律不认
    raw = request.headers.get("Authorization", "").replace("Bearer ", "").strip()
    if raw:
        from backend.security import decode_token, enforce_token_freshness, resolve_jwt_user_id

        jwt_user_id = resolve_jwt_user_id(raw)
        if jwt_user_id is not None:
            user = db.query(models.WebUser).filter(models.WebUser.id == jwt_user_id).first()
            if user and user.is_active:
                # A-P1-2：JWT 回退路径补做 token 新鲜度校验，改密/登出后旧 token 立即失效
                payload = decode_token(raw, expected_type="access")
                if payload is not None:
                    enforce_token_freshness(db, payload, user)
                    return user

    # Emby 客户端 token
    result = resolve_token(db, request)
    if result:
        return result[0]

    raise HTTPException(status_code=401, detail="未提供认证凭证")



def get_play_user(
    request: Request,
    db: Session = Depends(get_db),
    credentials: HTTPAuthorizationCredentials = Depends(bearer_scheme),
) -> models.WebUser:
    """播放端点鉴权依赖：短期签名优先，回退 Emby token 口径。

    - URL 带 ``uid/exp/sign`` 且能对应上路径里的 ``item_id`` 时走签名校验：
      签名对不上或已过期 → 403；用户不存在/禁用 → 401。
    - 否则（老客户端、缓存的旧 URL）走 ``get_emby_user`` 原有逻辑，
      行为与升级前完全一致。双轨并行，逐步迁移。
    """
    q = request.query_params
    sign = q.get("sign")
    exp_s = q.get("exp")
    uid_s = q.get("uid")
    item_id = (request.path_params or {}).get("item_id")
    if sign and exp_s and uid_s and item_id:
        from backend.emby_server import play_sign

        # 防盗链总开关（默认开）：关闭时跳过签名分支，走老客户端回退逻辑。
        # hotlink_enabled 内部 fail-open：配置读失败时视为启用，不弱化验签。
        if not play_sign.hotlink_enabled(db):
            return get_emby_user(request, db, credentials)

        try:
            exp = int(exp_s)
            uid = int(uid_s)
        except (TypeError, ValueError):
            raise HTTPException(status_code=403, detail="播放签名无效")
        if not play_sign.verify_play_sign(uid, item_id, exp, sign):
            logger = logging.getLogger(__name__)
            logger.warning("播放签名校验失败: uid=%s item=%s", uid_s, item_id)
            raise HTTPException(status_code=403, detail="播放签名无效或已过期")
        user = db.query(models.WebUser).filter(models.WebUser.id == uid).first()
        if not user or not user.is_active:
            raise HTTPException(status_code=401, detail="用户不存在或已禁用")
        _count_auth_leg("play_sign")
        return user
    return get_emby_user(request, db, credentials)
