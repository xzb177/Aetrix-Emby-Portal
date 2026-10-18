"""用户/管理门户 API：自建 Emby 集成端点

用户端（/api/user/emby/...）：
- 账号卡（服务器地址/用户名/密码/一键导入 scheme）
- 续看列表、继续播放、收藏
- 统计（观看时长/次数/最近观看）

管理端（/api/admin/emby/...）：
- 媒体库 CRUD + 扫描
- 条目搜索/删除
- 在线会话监控/强制下线
- 概览统计
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Optional
from urllib.parse import quote, urlsplit

from fastapi import APIRouter, Depends, File, HTTPException, Request, Response, UploadFile
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse
from pydantic import BaseModel
from sqlalchemy import and_, case, func, or_
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, aliased

from backend import config_service, models, realms, subscriptions
from backend.database import get_db, SessionLocal

from backend.emby_server import models as em
from backend.emby_server import nodes as node_lib
from backend.emby_server.api import (
    TICKS, SERVER_ID,
    _base_url, _item_dto, _library_scope, _prefetch_list_data, _scope_items,
    season_poster_url,
)
from backend.emby_server.auth import (
    ensure_emby_credentials,
    get_admin_or_emby_user,
)
from backend.emby_server.facets import count_virtual_items  # 索引版（虚拟库条目数）
from backend.emby_server.scanner import (
    PLATFORM_LABELS,
    SCAN_RUN_KEEP,
    LibrarySnapshot,
    ScanInProgress,
    is_scan_active,
    normalize_scrape_policy,
    scan_library_sync,
    scan_result_payload,
    scan_runs_payload,
)
# （转码已删除：同步变体已随 streaming 转码模块移除）
from backend.emby_server import facets
from backend.emby_server import image_store
from backend.emby_server import mounts as mount_lib
# 扫描队列（v2.27.0）：按远程挂载串行化 + 并发上限 + 排队状态/进度，见 scan_queue.py
from backend.emby_server import reachability
from backend.emby_server import scan_queue
from backend.emby_server import soft_delete
from backend.emby_server import transfer115

logger = logging.getLogger(__name__)


def _config_value(db: Session | None, key: str, realm_id: int | None = None) -> str:
    """读取 SystemConfig 原始值（不做大小写转换——URL 路径区分大小写）

    Emby 入口那几个键是**一个服一个**的（默认服沿用历史键名），见 ``backend/realms.py``。
    """
    if db is None:
        return ""
    try:
        if key in realms.REALM_CONFIG_BASES:
            return realms.realm_config(db, key, realm_id)
        row = db.query(models.SystemConfig).filter(models.SystemConfig.key == key).first()
    except Exception:  # noqa: BLE001 — 读配置失败不应影响接口返回
        return ""
    return ((row.value if row else "") or "").strip()


def emby_active_mode(db: Session | None, realm_id: int | None = None) -> str:
    """某个服生效的 Emby 服务入口模式：managed_ea（自建/单进程）或 external（已有 Emby 服）"""
    return (_config_value(db, "emby_active_mode", realm_id) or "managed_ea").lower()


def configured_emby_url(db: Session | None = None, realm_id: int | None = None) -> str:
    """只返回该服「Emby 服务入口」里配置的地址（未配置返回空串）

    1. 已有 Emby 服（external）→ emby_external_url
    2. 分离部署 EA（managed_ea 且已填地址）→ emby_managed_url
    另外修复了旧实现把配置值整体 `.lower()` 的问题：URL 路径区分大小写，
    之前保存 `https://Host/Media` 会被降成 `https://host/media`。
    db 传 None 时自建一个短连接，便于非请求上下文（如 EM 的客户端指引）复用。
    """
    own_session = db is None
    if own_session:
        try:
            db = SessionLocal()

        except Exception:  # noqa: BLE001
            db = None
    try:
        if emby_active_mode(db, realm_id) == "external":
            return _config_value(db, "emby_external_url", realm_id).rstrip("/")
        return _config_value(db, "emby_managed_url", realm_id).rstrip("/")
    finally:
        if own_session and db is not None:
            db.close()


# 用户端地址是从哪来的：面板要把「配好的」与「按当前访问地址推出来的」分开说
URL_SOURCE_REALM = "realm"      # 服自己填的对外地址
URL_SOURCE_CONFIG = "config"    # 「服务器」页的 Emby 服务入口
URL_SOURCE_ENV = "env"          # 环境变量 EMBY_PUBLIC_URL
URL_SOURCE_REQUEST = "request"  # 当前请求用的地址（什么都没配时的兜底）
URL_SOURCE_NONE = "none"


def _request_base_url(request: Request | None) -> str:
    """当前请求自己用的对外地址（形如 ``http://192.0.2.10:8000``；拿不到返回空串）

    这是「什么都没配」时的兜底口径。以前这里写死 ``http://localhost:8000``，于是：

    - 一台刚装好的服务器（管理员正用 ``http://<服务器IP>:8000`` 访问面板，协议面也开在
      面板上）会被判成「只有这台机器自己能连」而报红——测试部署第一眼看到的就是它；
    - 用户端账号卡真的把 localhost 下发下去，用户照抄进播放器就连不上。

    取请求自己的 scheme + Host（uvicorn 默认信任本机反向代理的
    ``X-Forwarded-Proto`` / ``X-Forwarded-Host``，所以反代后面拿到的也是对外那一个）。
    """
    if request is None:
        return ""
    try:
        base = str(request.base_url).rstrip("/")
    except Exception:  # noqa: BLE001 — 拿不到地址就当作没推断出来
        return ""
    return base if base.startswith(("http://", "https://")) else ""


def resolve_emby_base_url_with_source(
    db: Session | None = None, realm_id: int | None = None,
    request: Request | None = None,
) -> tuple[str, str]:
    """同上，同时告诉调用方这个地址是「配出来的」还是「推出来的」

    面板上必须分得清：按当前访问地址推出来的地址能直接照抄使用，但它不是一条落库的配置
    （换域名 / 上反代后就变了），所以界面上要写明「自动推断」。
    """
    if realm_id is not None and db is not None:
        realm = realms.get_realm(db, realm_id)
        if realm and (realm.url or "").strip():
            return realm.url.strip().rstrip("/"), URL_SOURCE_REALM
    url = configured_emby_url(db, realm_id)
    if url:
        return url, URL_SOURCE_CONFIG
    if realm_id is not None and db is not None and realm_id != realms.legacy_realm_id(db):
        # 非默认服还没配自己的地址：不能回退到默认服的地址（那会把用户导到别的服）
        return "", URL_SOURCE_NONE
    env = os.getenv("EMBY_PUBLIC_URL", "").rstrip("/")
    if env:
        return env, URL_SOURCE_ENV
    inferred = _request_base_url(request)
    if inferred:
        return inferred, URL_SOURCE_REQUEST
    return "http://localhost:8000", URL_SOURCE_NONE


def resolve_emby_base_url(db: Session | None = None, realm_id: int | None = None,
                          request: Request | None = None) -> str:
    """解析「用户应该连接的 Emby 服务器地址」（某个服的）

    必须与后台「服务器」页保存的 Emby 入口一致，否则会出现“后台填了 EA 地址、
    用户个人中心却仍显示旧的环境变量地址”这种配置不生效的问题。
    优先级：服自己填的对外地址 → 该服的服务入口配置 → 环境变量 EMBY_PUBLIC_URL
    → 当前请求用的地址（什么都没配时用它，而不是写死 localhost）。
    """
    return resolve_emby_base_url_with_source(db, realm_id, request)[0]


def _is_account_card_request(request: Request | None) -> bool:
    """是否为只读的账号卡请求（仅用户端 GET /api/user/emby/server）"""
    path = (getattr(getattr(request, "url", None), "path", "") or "").rstrip("/")
    return path.endswith("/api/user/emby/server")


def ensure_emby_backend_available(request: Request, db: Session = Depends(get_db)) -> None:
    """自建 Emby 的统一闸门。

    不配置服务地址时代表单进程模式，继续使用 EM 内置网关；配置了分离 EA
    后则必须通过面板的连接测试。切到“已有 Emby”时，本项目自建媒体库/扫描
    功能明确停用，避免用户误以为它能管理另一台服务器。
    """
    realm_id = realms.active_realm_id(db)

    def config(key: str, default: str = "") -> str:
        if key in realms.REALM_CONFIG_BASES:
            return realms.realm_config(db, key, realm_id, default).strip().lower()
        row = db.query(models.SystemConfig).filter(models.SystemConfig.key == key).first()
        return (row.value if row and row.value is not None else default).strip().lower()

    mode = config("emby_active_mode", "managed_ea")
    if mode == "external":
        # 账号卡只是只读信息（告诉用户该连哪台服务器、账号归谁管），
        # 外部模式也必须能返回，否则用户在个人中心既看不到地址也不知道找谁开号
        if _is_account_card_request(request):
            return
        raise HTTPException(status_code=503, detail="当前已接入已有 Emby 服，本项目自建媒体库功能已停用")

    managed_url = config("emby_managed_url")
    if managed_url and (config("emby_managed_enabled") != "true" or config("emby_managed_reachable") != "true"):
        raise HTTPException(status_code=503, detail="分离部署的 EA 尚未连接成功，请先部署 EA 并在后台「服务器」页添加它为后端服")


_admin_bearer = HTTPBearer(auto_error=False)


def _bearer_admin(
    request: Request,
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(_admin_bearer),
    db: Session = Depends(get_db),
) -> models.WebUser:
    """与 ``/api/admin/*`` 的 ``get_current_admin`` 同一实现（只认 Authorization 头里的 JWT）"""
    from backend.api.admin_core import get_current_admin

    return get_current_admin(request, credentials, db)


def require_staff(
    request: Request,
    user: models.WebUser = Depends(_bearer_admin),
    _: None = Depends(ensure_emby_backend_available),
) -> models.WebUser:
    """管理端鉴权：仅 is_staff 用户可访问（/api/admin/emby/* 全部端点）

    安全修复 H3：与 ``/api/admin/*`` 的 ``get_current_admin`` **完全同一口径**——
    只接受 ``Authorization: Bearer <access JWT>``；不再接受 Emby 客户端 token
    （管理员在 Infuse 里登录后的 30 天 token 不能再调媒体库删除 / 挂载 / 115 Cookie），
    也不接受 URL 查询串里的 ``?api_key=``。鉴权先于「后端可用」判定（未登录得 401 而不是 503）。

    角色判定同 ``/api/admin/*``（backend/admin_roles.py，已在 get_current_admin 里做过）：
    只读角色不能扫描 / 改库 / 删条目这些写操作，否则「只读」在这个路由上是假的。
    """
    return user


user_emby_router = APIRouter(
    prefix="/api/user/emby",
    tags=["用户端-自建Emby"],
    dependencies=[Depends(ensure_emby_backend_available)],
)
admin_emby_router = APIRouter(prefix="/api/admin/emby", tags=["管理后台-自建Emby"], dependencies=[Depends(require_staff)])


# ==================== 用户端 ====================

# 公益服查看权限的积分价格与有效期键名及出厂默认值。
# 配置自愈（backend/config_self_heal.py）引用这里 —— 默认值只许在这里定义一次。
VIEW_UNLOCK_POINTS_KEY = "emby_view_unlock_points"
VIEW_UNLOCK_POINTS_DEFAULT = 50
VIEW_UNLOCK_DAYS_KEY = "emby_view_unlock_days"
VIEW_UNLOCK_DAYS_DEFAULT = 365  # 0=永久


def _get_int_config(db: Session, key: str, default: int) -> int:
    row = db.query(models.SystemConfig).filter(models.SystemConfig.key == key).first()
    try:
        return int(row.value) if row and row.value is not None else default
    except (TypeError, ValueError):
        return default


def _view_unlock_pricing(db: Session) -> tuple[int, int]:
    """公益服查看权限的积分价格与有效期（天，0=永久），后台 SystemConfig 可调。"""
    points = _get_int_config(db, VIEW_UNLOCK_POINTS_KEY, VIEW_UNLOCK_POINTS_DEFAULT)
    days = _get_int_config(db, VIEW_UNLOCK_DAYS_KEY, VIEW_UNLOCK_DAYS_DEFAULT)
    return max(points, 0), max(days, 0)


class ViewUnlockSettingsIn(BaseModel):
    points: int
    days: int


@admin_emby_router.get("/view-unlock")
def admin_view_unlock_get(db: Session = Depends(get_db)):
    """管理后台：读公益服查看权限的积分价格与有效期。"""
    points, days = _view_unlock_pricing(db)
    return {"points": points, "days": days}


@admin_emby_router.put("/view-unlock")
def admin_view_unlock_put(body: ViewUnlockSettingsIn, db: Session = Depends(get_db)):
    """管理后台：改公益服查看权限的积分价格与有效期（days=0 为永久）。"""
    if body.points < 0 or body.days < 0:
        raise HTTPException(status_code=400, detail="points/days 不能为负")
    # 配置瘦身第 4 批 4-4：走统一写入通道（校验→落库→写时失效）；
    # store.write_values 内部已按 store 纪律失效，4-1 的失效覆盖由正统通道承接。
    config_service.save_config(
        db,
        {VIEW_UNLOCK_POINTS_KEY: body.points, VIEW_UNLOCK_DAYS_KEY: body.days},
        schema="view_unlock",
    )
    db.commit()
    return {"points": body.points, "days": body.days}


def _realm_view_granted(db: Session, user: models.WebUser, realm_id: int | None
                        ) -> tuple[bool, models.EmbyViewUnlock | None]:
    """用户是否有权查看某服的 Emby 账号/线路（口径见 ``subscriptions.view_grant``）。

    规则：付费服 = 有效订阅即权限；公益服 = 花积分解锁即权限
    （见 unlock_emby_view）。没权限时账号卡不下发地址，前端也不展示。

    判定本身在 ``backend/subscriptions.py``——它是全站唯一口径，管理端的
    「用户授权资源卡片」（backend/user_grants.py）读同一份，两边不会漂。
    """
    if realm_id is None:
        realm_id = realms.active_realm_id(db)
    return subscriptions.view_grant(db, user, realm_id)


# Rex 播放器一键导入的基础 URL（不含查询参数）。
# 参数格式由用户 2026-10-04 分享（见 workspace/docs/rex-deep-link-cheatsheet.md）：
# rex://import?type=emby&scheme=&host=&port=&username=&password=
REX_IMPORT_URL = "rex://import"


def _rex_import_link(url: str, username: str, password: str | None = None) -> str:
    """Build Rex player one-click import deep link.

    host/port parsed from server address; default 80/443 by scheme when missing.
    password is optional: appended only when the user typed it in the frontend
    (handled in frontend memory only, never stored server-side, never logged).
    """
    scheme = os.getenv("EMBY_URL_SCHEME", "") or urlsplit(url).scheme or "http"
    parsed = urlsplit(url if "://" in url else f"{scheme}://{url}")
    host = parsed.hostname or ""
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    link = (
        f"{REX_IMPORT_URL}?type=emby&scheme={scheme}"
        f"&host={host}&port={port}&username={quote(username or '', safe='')}"
    )
    # password 预留：后端当前不拼密码（_account_card 未传），密码由前端在内存中拼接；
    # 保留参数供未来服务端直拼场景。
    if password:
        link += f"&password={quote(password, safe='')}"
    return link


# VidHub 播放器一键导入的基础 URL（不含查询参数）。
# 参数格式由用户 2026-10-15 分享，与 Rex 同款口径：
# vidhub://import?type=emby&scheme=&host=&port=&username=&password=
VIDHUB_IMPORT_URL = "vidhub://import"


def _vidhub_import_link(url: str, username: str, password: str | None = None) -> str:
    """Build VidHub player one-click import deep link.

    host/port parsed from server address; default 80/443 by scheme when missing.
    password is optional: appended only when the user typed it in the frontend
    (handled in frontend memory only, never stored server-side, never logged).
    """
    scheme = os.getenv("EMBY_URL_SCHEME", "") or urlsplit(url).scheme or "http"
    parsed = urlsplit(url if "://" in url else f"{scheme}://{url}")
    host = parsed.hostname or ""
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    link = (
        f"{VIDHUB_IMPORT_URL}?type=emby&scheme={scheme}"
        f"&host={host}&port={port}&username={quote(username or '', safe='')}"
    )
    # password 预留：后端当前不拼密码（_account_card 未传），密码由前端在内存中拼接；
    # 保留参数供未来服务端直拼场景。
    if password:
        link += f"&password={quote(password, safe='')}"
    return link


def _senplayer_import_link(url: str, username: str, password: str | None = None) -> str:
    """Build SenPlayer one-click import deep link.

    password is optional: appended only when the user typed it in the frontend
    (handled in frontend memory only, never stored server-side, never logged).
    """
    link = (
        f"senplayer://importserver?type=emby&name=Aetrix"
        f"&address={quote(url or '', safe=':/')}"
        f"&username={quote(username or '', safe='')}"
    )
    # password 预留：后端当前不拼密码（_account_card 未传），密码由前端在内存中拼接；
    # 保留参数供未来服务端直拼场景。
    if password:
        link += f"&password={quote(password, safe='')}"
    return link

def _account_card(user: models.WebUser, db: Session, realm_id: int | None = None,
                  request: Request | None = None) -> dict:
    """构造账号卡（不含密码明文；导入 scheme 需用户已在播放器中保存密码）

    服务器地址来自该服的「Emby 服务入口」配置（见 ``resolve_emby_base_url``），
    什么都没配时按**用户当前访问用的地址**兜底（不再下发 localhost），
    接入已有 Emby 服时不再提供本项目的一键导入 scheme——那台服务器上的账号
    由对方管理，本项目的用户名/密码对它无效。

    多服部署下同一个用户可能在多个服都有订阅，所以账号卡会带上 **每个服的地址与订阅**
    （``realms``），让客户端知道该连哪一台；顶层字段保持默认服的口径不变。

    查看权限：付费服要求有效订阅，公益服要求积分解锁（见 ``_realm_view_granted``）。
    没权限时 ``base_url`` / ``import_schemes`` / ``emby_username`` 置空不下发，
    前端据 ``view_permission`` 展示解锁入口。
    """
    if realm_id is None:
        realm_id = realms.active_realm_id(db)
    url = resolve_emby_base_url(db, realm_id, request)
    mode = emby_active_mode(db, realm_id)
    external = mode == "external"
    realm = realms.get_realm(db, realm_id)
    granted, unlock = _realm_view_granted(db, user, realm_id)
    price_points, price_days = _view_unlock_pricing(db)
    card = {
        "server_id": SERVER_ID,
        "server_name": os.getenv("EMBY_SERVER_NAME", "Aetrix Media Server"),
        "base_url": url if granted else "",
        "mode": mode,
        "external": external,
        "account_managed_by": "external" if external else "portal",
        "emby_username": user.emby_username if granted else "",
        "emby_password": None,
        # has_password 不 gate：它只是用户自己的密码设置状态，不泄露服务器信息；
        # 冒烟测试也依赖它（set-password 后断言为 True）
        "has_password": bool(user.emby_password),
        "realm_id": realm.id if realm else None,
        "realm_name": realm.name if realm else "",
        # 接入方式：free = 公益服（免费开放，不需要订阅）；用户端据此换一套文案
        "access_mode": realms.normalize_access_mode(realm.access_mode) if realm else "paid",
        "is_free": realms.is_free_realm(db, realm_id),
        "access_note": realms.access_note_of(db, realm_id),
        "allow_download": subscriptions.download_allowed(db, realm_id),
        "import_schemes": {} if (external or not granted) else {
            # Rex 播放器一键导入（2026-10-10 用户要求替换 forward）。
            "Rex": _rex_import_link(url, user.emby_username),
            "senplayer": _senplayer_import_link(url, user.emby_username),
            # VidHub 播放器一键导入（2026-10-15 用户要求新增，参数格式与 Rex 同）。
            "VidHub": _vidhub_import_link(url, user.emby_username),
        },
        "view_permission": {
            "granted": granted,
            "realm_free": realms.is_free_realm(db, realm_id),
            "unlock_points": price_points,
            "unlock_days": price_days,
            "points_balance": int(user.points or 0),
            "expires_at": unlock.expires_at.isoformat() if unlock and unlock.expires_at else None,
        },
    }
    card["realms"] = _user_realm_cards(user, db, request)
    return card


def _user_realm_cards(user: models.WebUser, db: Session,
                      request: Request | None = None) -> list[dict]:
    """用户在各服的地址与订阅状态（多服时前端按卡片列出）

    **公益服不管有没有订阅都下发**：免费开放本身就是这个服对用户的承诺，
    没订阅不等于“不能看”（见 ``backend/subscriptions.py``）。
    """
    now = datetime.now()
    subs = (db.query(models.UserSubscription)
            .filter(models.UserSubscription.user_id == user.id,
                    models.UserSubscription.status == "active",
                    models.UserSubscription.end_date > now)
            .order_by(models.UserSubscription.end_date.desc())
            .all())
    by_realm: dict[int, models.UserSubscription] = {}
    for sub in subs:
        if sub.realm_id and sub.realm_id not in by_realm:
            by_realm[sub.realm_id] = sub
    cards: list[dict] = []
    for realm in realms.list_realms(db, include_disabled=False):
        sub = by_realm.get(realm.id)
        free = realms.is_free_realm(db, realm.id)
        if sub is None and not free and realm.id != realms.legacy_realm_id(db):
            continue  # 没订阅的付费服不往用户面前推（默认服保留，兼容老前端）
        mode = emby_active_mode(db, realm.id)
        granted, _ = _realm_view_granted(db, user, realm.id)
        cards.append({
            "id": realm.id,
            "name": realm.name,
            "slug": realm.slug,
            # 没查看权限不下发地址（默认服保留卡片结构，兼容老前端）
            "base_url": resolve_emby_base_url(db, realm.id, request) if granted else "",
            "view_granted": granted,
            "mode": mode,
            "external": mode == "external",
            "subscribed": sub is not None,
            # free = 公益服：不需要订阅也能看；"access" 是给前端的一句话口径
            "access_mode": realms.normalize_access_mode(realm.access_mode),
            "is_free": free,
            "access_note": realms.access_note_of(db, realm.id),
            "end_date": sub.end_date.isoformat() if sub and sub.end_date else None,
            "plan_name": (sub.plan.name if sub and sub.plan else ""),
            "is_default": realm.id == realms.legacy_realm_id(db),
        })
    return cards


@user_emby_router.get("/server")
async def get_server_info(request: Request,
                          request_user: models.WebUser = Depends(get_admin_or_emby_user),
                          db: Session = Depends(get_db)):
    """返回账号卡信息：服务器地址 + 自建 Emby 用户名 + 播放器导入 scheme

    ``?realm_id=`` 可以指定要哪个服的地址（多服部署下同一个用户可能持有几个服的会员），
    不传则用面板当前服。

    安全：不返回密码明文。密码仅注册/重置时一次性返回。
    """
    user = request_user
    ensure_emby_credentials(db, user)
    realm_id = None
    raw = request.query_params.get("realm_id")
    if raw and str(raw).strip().isdigit():
        realm_id = int(raw)
    return _account_card(user, db, realm_id, request)


class UnlockViewRequest(BaseModel):
    realm_id: int | None = None


@user_emby_router.post("/unlock-view")
def unlock_emby_view(
    req: UnlockViewRequest,
    request_user: models.WebUser = Depends(get_admin_or_emby_user),
    db: Session = Depends(get_db),
):
    """公益服：花积分解锁 Emby 账号/线路查看权限。

    幂等：已解锁且未过期直接返回成功，不重复扣费。扣费是 SQL 级原子
    更新（余额检查写在 WHERE 里），并发下不会扣成负数；解锁记录有
    (user_id, realm_id) 唯一约束，建记录冲突时回滚重读走幂等路径。
    付费服不走这里——查看权限来自有效订阅。
    """
    realm_id = req.realm_id or realms.active_realm_id(db)
    realm = realms.get_realm(db, realm_id)
    if realm is None:
        raise HTTPException(status_code=404, detail="服不存在")
    if not realms.is_free_realm(db, realm_id):
        raise HTTPException(status_code=400, detail="当前服为付费服，查看权限来自有效订阅，无需积分解锁")
    now = datetime.now()
    existing = (db.query(models.EmbyViewUnlock)
                  .filter(models.EmbyViewUnlock.user_id == request_user.id,
                          models.EmbyViewUnlock.realm_id == realm_id,
                          or_(models.EmbyViewUnlock.expires_at.is_(None),
                              models.EmbyViewUnlock.expires_at > now))
                  .first())
    if existing:
        return {"success": True, "already": True,
                "expires_at": existing.expires_at.isoformat() if existing.expires_at else None}
    price_points, price_days = _view_unlock_pricing(db)
    if price_points <= 0:
        raise HTTPException(status_code=400, detail="当前无需积分即可查看")
    # 原子扣费：余额检查写进 WHERE，并发下至多一个成功
    rows = (db.query(models.WebUser)
              .filter(models.WebUser.id == request_user.id,
                      func.coalesce(models.WebUser.points, 0) >= price_points)
              .update({models.WebUser.points: func.coalesce(models.WebUser.points, 0) - price_points},
                      synchronize_session="fetch"))
    if not rows:
        raise HTTPException(status_code=409, detail="积分不足")
    db.flush()
    balance = int(db.query(models.WebUser.points)
                    .filter(models.WebUser.id == request_user.id).scalar() or 0)
    from backend.api.economy import _append_points_log  # C3：流水 hash 链走统一入口
    _append_points_log(db, request_user.id, -price_points, balance, "view_unlock",
                       f"解锁{realm.name}查看权限", f"emby-view:{realm_id}")
    expires_at = None if price_days <= 0 else now + timedelta(days=price_days)
    db.add(models.EmbyViewUnlock(user_id=request_user.id, realm_id=realm_id,
                                 unlocked_at=now, expires_at=expires_at,
                                 points_spent=price_points))
    try:
        db.commit()
    except IntegrityError:
        # 并发下另一请求已建记录：回滚后走幂等路径（对方那次扣费有效）
        db.rollback()
        existing = (db.query(models.EmbyViewUnlock)
                      .filter(models.EmbyViewUnlock.user_id == request_user.id,
                              models.EmbyViewUnlock.realm_id == realm_id)
                      .first())
        if existing:
            return {"success": True, "already": True,
                    "expires_at": existing.expires_at.isoformat() if existing.expires_at else None}
        raise HTTPException(status_code=409, detail="解锁冲突，请重试")
    return {"success": True, "already": False,
            "expires_at": expires_at.isoformat() if expires_at else None,
            "points_spent": price_points, "balance": balance}


class SetPasswordRequest(BaseModel):
    password: str


@user_emby_router.post("/password")
async def set_emby_password(
    req: SetPasswordRequest,
    request_user: models.WebUser = Depends(get_admin_or_emby_user),
    db: Session = Depends(get_db),
):
    """设置/修改自建 Emby 播放密码（bcrypt 哈希存储）"""
    if not (3 <= len(req.password) <= 64):
        raise HTTPException(status_code=400, detail="密码长度需为 3-64 位")
    ensure_emby_credentials(db, request_user, password=req.password)
    return {"success": True, "emby_username": request_user.emby_username}


@user_emby_router.get("/play-line")
def get_play_line_pref(request_user: models.WebUser = Depends(get_admin_or_emby_user),
                       db: Session = Depends(get_db)):
    """播放路径（2026-10 简化后）：Drive 302 直链 + 远程挂载专用中继。

    保留此接口仅为兼容老客户端：永远返回 relay（值保留，语义已随中继全删变更）。
    """
    return {"line": "relay"}


@user_emby_router.put("/play-line")
def set_play_line_pref(req: dict,
                       request_user: models.WebUser = Depends(get_admin_or_emby_user),
                       db: Session = Depends(get_db)):
    """设置播放线路偏好（2026-10 已废弃）：任何值都返回 relay（兼容老客户端，值保留）。"""
    return {"line": "relay"}


@user_emby_router.get("/resume")
def get_resume_list(request_user: models.WebUser = Depends(get_admin_or_emby_user),
                          db: Session = Depends(get_db),
                          limit: int = 12):
    """门户首页「继续观看」：有进度、未播完，最近播放的在前。

    与 Emby 兼容层的 ``/Users/{uid}/Items/Resume`` 分工不同：那条给三方客户端，
    按「只出电影 / 剧」的口径把单集整个排除了（见 tests/test_resume_item_type.py），
    剧集看到一半在那边是看不到的。门户这条只给 web 首页用，口径是：

    - 电影按自身一行；单集**按剧聚合**，每部剧只留最近在看的那一集，行上带
      ``episode_id / season_number / episode_number``（顶层仍只有 movie / series）；
    - 隐藏条目、不在本账号可见媒体库里的条目不出；
    - 每行附上**最近一次播放会话**的客户端 / 设备（``client`` / ``device``），
      全页一次分组查询取齐，不逐行查。

    原有字段（id / name / type / year / position_ticks / duration_ticks / progress /
    poster_url）保持不变，新增字段只追加。
    """
    limit = max(1, min(int(limit or 12), 50))
    UMD, MI, PS = em.UserMediaData, em.MediaItem, em.PlaybackSession
    rows = (
        _scope_items(
            db.query(UMD, MI)
            .join(MI, MI.id == UMD.item_id)
            .filter(
                UMD.user_id == request_user.id,
                UMD.playback_position_ticks > 0,
                UMD.played == False,  # noqa: E712
                MI.item_type.in_(["movie", "episode"]),
                MI.is_hidden == False,  # noqa: E712
            ),
            _library_scope(db, request_user),
        )
        # 同一部剧的几集会被聚合成一行，多取一些候选再截断
        .order_by(UMD.last_played_at.desc().nullslast(), UMD.id.desc())
        .limit(limit * 5 + 20)
        .all()
    )

    # 单集 → 所属剧（一次取齐）
    series_ids = {it.series_id for _u, it in rows if it.item_type == "episode" and it.series_id}
    series_map: dict[int, em.MediaItem] = {}
    if series_ids:
        series_map = {
            s.id: s for s in db.query(MI).filter(
                MI.id.in_(series_ids), MI.is_hidden == False,  # noqa: E712
            ).all()
        }

    page: list[tuple] = []  # (umd, 展示条目, 单集|None)
    seen_series: set[int] = set()
    for umd, item in rows:
        if item.item_type == "episode":
            series = series_map.get(item.series_id) if item.series_id else None
            if series is None or series.id in seen_series:
                continue  # 孤儿 / 隐藏剧的单集不出；同剧只留最近一集
            seen_series.add(series.id)
            page.append((umd, series, item))
        else:
            page.append((umd, item, None))
        if len(page) >= limit:
            break

    # 每个实际播放条目（电影 / 单集）的最近一次会话：一条分组子查询 + 一次 join
    played_ids = [(ep or it).id for _u, it, ep in page]
    last_session: dict[int, em.PlaybackSession] = {}
    if played_ids:
        latest = (
            db.query(PS.item_id.label("item_id"), func.max(PS.last_update_at).label("ts"))
            .filter(PS.user_id == request_user.id, PS.item_id.in_(played_ids))
            .group_by(PS.item_id)
            .subquery()
        )
        for ps in (
            db.query(PS)
            .join(latest, (PS.item_id == latest.c.item_id) & (PS.last_update_at == latest.c.ts))
            .filter(PS.user_id == request_user.id)
            .order_by(PS.id.desc())
            .all()
        ):
            last_session.setdefault(ps.item_id, ps)  # 同一时刻两条会话时取后建的那条

    items = []
    for umd, item, ep in page:
        played = ep or item
        pos = umd.playback_position_ticks or 0
        duration = played.duration_ticks or 0
        session = last_session.get(played.id)
        entry = {
            "id": item.guid,
            "name": item.name,
            "type": item.item_type,
            "year": item.production_year,
            "position_ticks": pos,
            "duration_ticks": duration,
            # 没有时长就不猜百分比（旧实现除以 1，会算出几百万 %）
            "progress": min(100.0, round(pos / duration * 100, 1)) if duration else 0.0,
            # 季无图时回退父剧集海报（season_poster_url），其余类型行为不变
            "poster_url": season_poster_url(item, db),
            "tmdb_id": item.tmdb_id or None,
            "last_played_at": umd.last_played_at.isoformat() if umd.last_played_at else None,
            "client": (session.client_name or None) if session else None,
            "device": (session.device_name or None) if session else None,
        }
        if ep is not None:
            entry.update({
                "episode_id": ep.guid,
                "episode_name": ep.name,
                "season_number": ep.season_number,
                "episode_number": ep.episode_number,
                # 明确给出剧名，前端不再靠 name 猜（issue: 继续观看只显示集数）
                "series_name": item.name,
            })
        items.append(entry)
    return {"items": items}


@user_emby_router.get("/favorites")
def get_favorite_list(request_user: models.WebUser = Depends(get_admin_or_emby_user),
                            db: Session = Depends(get_db)):
    rows = (
        db.query(em.MediaItem)
        .join(em.UserMediaData, em.UserMediaData.item_id == em.MediaItem.id)
        .filter(em.UserMediaData.user_id == request_user.id,
                em.UserMediaData.is_favorite == True)  # noqa: E712
        .order_by(em.UserMediaData.updated_at.desc())
        .all()
    )
    return {"items": [
        # 季无图时回退父剧集海报（season_poster_url），其余类型行为不变
        {"id": i.guid, "name": i.name, "type": i.item_type, "year": i.production_year,
         "rating": i.community_rating,
         "poster_url": season_poster_url(i, db)}
        for i in rows
    ]}


@user_emby_router.post("/favorites/{item_id}")
def toggle_favorite(item_id: str, request_user: models.WebUser = Depends(get_admin_or_emby_user),
                          db: Session = Depends(get_db)):
    from backend.emby_server.api import _item_visible

    item = db.query(em.MediaItem).filter(em.MediaItem.guid == item_id).first()
    if not item:
        raise HTTPException(status_code=404, detail="条目不存在")
    # H1：不可见库里的条目不能收藏（否则可借此确认 / 读取隐藏条目）
    if not _item_visible(db, request_user, item):
        raise HTTPException(status_code=403, detail="该条目所在媒体库对你不可见")
    umd = db.query(em.UserMediaData).filter(
        em.UserMediaData.user_id == request_user.id, em.UserMediaData.item_id == item.id
    ).first()
    if not umd:
        umd = em.UserMediaData(user_id=request_user.id, item_id=item.id)
        db.add(umd)
    umd.is_favorite = not umd.is_favorite
    db.commit()
    return {"success": True, "is_favorite": umd.is_favorite}


# ==================== 追新日历 ====================

#: 一次最多查多少天。日历按月翻页，给到「一个季度」已经远超翻页粒度；
#: 上限存在的意义是有人直接打 ``/calendar?start=1970-01-01`` 时不会把整库拉出来。
CALENDAR_MAX_DAYS = 93

#: 单日最多回多少条条目详情。日历格子只画得下几张封面，超出的只给计数
#: （前端显示「+N」），点开那天再按需拉详情。
CALENDAR_MAX_ITEMS_PER_DAY = 24

#: 日历支持的条目类型。**不含 season**：季是剧集的中间层，
#: 出现在「今天新上了什么」里只会占格子、没信息量。
CALENDAR_ITEM_TYPES = ("movie", "series", "episode")


def _calendar_day(raw: str | None) -> datetime | None:
    """``YYYY-MM-DD`` → datetime；空值或格式不对都返回 None（走默认口径）"""
    try:
        return datetime.strptime((raw or "").strip(), "%Y-%m-%d")
    except ValueError:
        return None


def _calendar_month_bounds() -> tuple[datetime, datetime]:
    """没有给区间时看当月（两端都是**含当天**的日历日，Asia/Shanghai）"""
    # r35："当月"按上海自然月；此前用服务器本地（生产 UTC），每月 1 日 00:00–08:00
    # （CST）会显示上个月。
    from zoneinfo import ZoneInfo
    try:
        _now = datetime.now(ZoneInfo("Asia/Shanghai")).replace(tzinfo=None)
    except Exception:  # noqa: BLE001 — 缺 tzdata 回退旧行为
        _now = datetime.now()
    first = _now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    next_month = datetime(first.year + (first.month == 12), first.month % 12 + 1, 1)
    return first, next_month - timedelta(days=1)


def _resolve_calendar_library(db: Session, raw: str | None) -> int | None:
    """把前端传来的库标识解析成 ``Library.id``；无法解析时返回 None（不过滤）

    前端的库下拉直接用 ``/emby/Users/me/Views`` 的 ``Id``，那是 **guid**；
    这里两种都收（纯数字当库 id，其余当 guid），免得前端为了筛选再换一套标识。
    """
    text = (raw or "").strip()
    if not text:
        return None
    if text.isdigit():
        return int(text)
    lib = db.query(em.Library.id).filter(em.Library.guid == text).first()
    return lib[0] if lib else None


@user_emby_router.get("/calendar")
def get_chase_calendar(request: Request,
                       request_user: models.WebUser = Depends(get_admin_or_emby_user),
                       db: Session = Depends(get_db)):
    """追新日历：按入库日期分组的新增条目

    **口径**：以 ``MediaItem.date_added``（条目入库时间）为准，**不用
    ``date_modified``**。后者带 ``onupdate=datetime.now``，后台补全 / 重刮一次
    元数据就刷新一次，拿它当「上线时间」会让每条片子在日历上反复横跳。
    扫描把新文件写进库时写的就是 ``date_added``，那才是用户感知的「上新了」。

    **区间口径**：``start`` / ``end`` 都是**含当天**的日历日（前端按月翻页，
    「2026-10-01 ~ 2026-10-31」理应包含 10 月 31 日晚上入库的东西）。
    落到 SQL 上是 ``>= start 00:00`` 且 ``< end+1 天 00:00``。

    **分组时区**：``date_added`` 是 UTC-naive 入库（scanner 以容器本地时间写入），
    分组时 +8h 换算成 Asia/Shanghai 自然日再分格；起止参数是上海日历日，过滤界
    同样换算成 UTC 帧。r35 之前这里按 UTC 日分组，每天 00:00–08:00（CST）入库
    的条目会落在前一天格子。

    **可见范围**：与媒体库列表、条目浏览同一个口径（``_library_scope``），
    否则会出现「客户端里看不到的库，日历里却有新片」。

    **``types`` 不受 ``item_type`` 筛选影响**：它是「这个区间里各类各有多少」，
    前端拿它给筛选按钮写角标。跟着筛选走的话，选了剧集之后电影按钮就永远显示 0。
    """
    q = request.query_params
    start = _calendar_day(q.get("start"))
    end = _calendar_day(q.get("end"))
    if start is None and end is None:
        start, end = _calendar_month_bounds()
    elif start is None:
        start = end
    elif end is None:
        end = start
    if end < start:
        end = start
    if (end - start).days >= CALENDAR_MAX_DAYS:
        end = start + timedelta(days=CALENDAR_MAX_DAYS - 1)
    # 闭区间 → SQL 的左闭右开
    end_exclusive = end + timedelta(days=1)
    # r35：入参起止是上海日历日，date_added 是 UTC-naive 入库 —— 过滤界换算成
    # UTC 帧（否则每月首尾 00:00–08:00 CST 入库的条目在边界月里丢失）。
    start_utc = start - timedelta(hours=8)
    end_exclusive_utc = end_exclusive - timedelta(hours=8)

    types = [
        t for t in (q.get("item_type") or "").split(",")
        if t.strip() in CALENDAR_ITEM_TYPES
    ] or list(CALENDAR_ITEM_TYPES)

    allowed = _library_scope(db, request_user)
    library_id = _resolve_calendar_library(db, q.get("library_id"))
    # 指定了却解析不出库 → 落到空结果而不是「不过滤」：
    # 库被删 / guid 过期时，宁可空着也不能悄悄把全库的新片端出来。
    library_missing = bool((q.get("library_id") or "").strip()) and library_id is None

    base_query = None if library_missing else _scope_items(
        db.query(em.MediaItem.id, em.MediaItem.date_added,
                 em.MediaItem.item_type, em.MediaItem.library_id)
        .filter(
            em.MediaItem.is_hidden == False,  # noqa: E712
            em.MediaItem.date_added >= start_utc,
            em.MediaItem.date_added < end_exclusive_utc,
        ),
        allowed,
    )
    if base_query is not None and library_id is not None:
        base_query = base_query.filter(em.MediaItem.library_id == library_id)

    # 只取四个小列做聚合：一天的条目可能上千，构造 DTO 才是贵的部分，
    # 计数 / 分组没必要把整行实体拉出来。
    # **类型筛选在 Python 侧做**：``types`` 要报的是「不受当前类型筛选影响」的
    # 分类型计数（前端拿它给筛选按钮写角标——「只看剧集时电影显示 0」没有意义，
    # 用户要看到的是「这个月电影有多少」）。下推到 SQL 拿不到这个数。
    rows = base_query.order_by(
        em.MediaItem.date_added.desc(), em.MediaItem.id.desc()
    ).all() if base_query is not None else []

    type_counts: dict[str, int] = {}
    for _id, _added, item_type, _lib in rows:
        # 只统计真会出现在日历上的类型：season 是剧集的中间层，
        # 把它算进角标会让「剧集」那个数字比日历上实际能看到的条目还多
        if item_type in CALENDAR_ITEM_TYPES:
            type_counts[item_type] = type_counts.get(item_type, 0) + 1
    rows = [r for r in rows if r[2] in types]

    day_ids: dict[str, list[int]] = {}
    day_counts: dict[str, int] = {}
    for item_id, date_added, _type, _lib in rows:
        # r35：按上海自然日分格（date_added 是 UTC-naive）；此前按 UTC 日分组，
        # 每天 00:00–08:00（CST）入库的条目落在前一天格子。
        day = ((date_added + timedelta(hours=8)).date()
               if date_added else start.date()).isoformat()
        day_counts[day] = day_counts.get(day, 0) + 1
        bucket = day_ids.setdefault(day, [])
        # rows 已按 date_added DESC 排序，先到先得 —— 留下的是当天最新入库的那批
        if len(bucket) < CALENDAR_MAX_ITEMS_PER_DAY:
            bucket.append(item_id)

    # 只为要塞进格子的那批条目取实体（每天至多 CALENDAR_MAX_ITEMS_PER_DAY 条），
    # DTO 里的 UserData / ChildCount 都要额外查询，只算在真要返回的条目上
    wanted = [i for ids in day_ids.values() for i in ids]
    by_id = {
        it.id: it for it in (
            db.query(em.MediaItem).filter(em.MediaItem.id.in_(wanted)).all()
            if wanted else []
        )
    }
    _prefetch_list_data(db, request_user.id, list(by_id.values()))

    base_url = _base_url(request)
    lib_names = {row[0]: row[1] for row in db.query(em.Library.id, em.Library.name).all()}
    days = [
        {
            "date": day,
            # count 是当天的**全部**条数（含被截断没回详情的），
            # 前端用它在格子上写「+N」，不能拿 len(items) 冒充
            "count": day_counts[day],
            "items": [
                dict(_item_dto(by_id[i], base_url, request_user.id, db),
                     LibraryName=lib_names.get(by_id[i].library_id))
                for i in day_ids[day] if i in by_id
            ],
        }
        for day in sorted(day_ids)
    ]

    # 保密新大陆占位卡（后台可配的揭晓位）：状态由服务端时间判定，
    # 揭晓前只下发保密文案——揭晓后内容在到日子前永远不出现在这个响应里。
    from backend.emby_server import teaser as _teaser_mod
    return {
        "start": start.date().isoformat(),
        "end": end.date().isoformat(),
        "total": len(rows),
        "days": days,
        "types": type_counts,
        "teaser": _teaser_mod.public_view(db),
    }


@user_emby_router.get("/stats")
def get_watch_stats(request_user: models.WebUser = Depends(get_admin_or_emby_user),
                          db: Session = Depends(get_db)):
    """观看统计：总时长/次数/最近观看"""
    total_rows = db.query(
        func.count(em.UserMediaData.id),
        func.coalesce(func.sum(em.UserMediaData.play_count), 0),
    ).filter(em.UserMediaData.user_id == request_user.id).first()

    sessions = (
        db.query(em.PlaybackSession, em.MediaItem)
        .join(em.MediaItem, em.MediaItem.id == em.PlaybackSession.item_id)
        .filter(em.PlaybackSession.user_id == request_user.id)
        .order_by(em.PlaybackSession.last_update_at.desc())
        .limit(20)
        .all()
    )
    total_seconds = 0
    for session, _item in sessions:
        runtime = _item.duration_ticks or 0
        pos = session.position_ticks or 0
        total_seconds += min(pos, runtime) // TICKS if runtime else pos // TICKS

    recent = [
        {
            "item": _item.name, "type": _item.item_type,
            "device": session.device_name, "client": session.client_name,
            "position_ticks": session.position_ticks,
            "duration_ticks": _item.duration_ticks,
            "at": session.last_update_at.isoformat() if session.last_update_at else None,
        }
        for session, _item in sessions[:10]
    ]
    return {
        "total_plays": int(total_rows[1] or 0),
        "watched_items": int(total_rows[0] or 0),
        "total_seconds": total_seconds,
        "recent": recent,
    }


@user_emby_router.get("/leaderboard")
def get_leaderboard(
    board: str,
    period: str = "day",
    request_user: models.WebUser = Depends(get_admin_or_emby_user),
    db: Session = Depends(get_db),
):
    """探险世界 · 观影碑榜单。

    口径：hot=热播榜（窗口内开播次数，剧集按整剧聚合）；duration=观影时长榜
    （窗口内每用户观影总时长秒数，只统计允许上榜的用户）。
    period：day=日榜（今天自然日），week=周榜（本周自然周，周一起算），按 Asia/Shanghai。
    """
    if board not in ("hot", "duration"):
        raise HTTPException(status_code=400, detail="board 只能是 hot 或 duration")
    if period not in ("day", "week"):
        raise HTTPException(status_code=400, detail="period 只能是 day 或 week")

    # 时区口径与 #910 问候语保持一致，统一按 Asia/Shanghai 计算自然日 / 自然周
    from zoneinfo import ZoneInfo

    try:
        tz = ZoneInfo("Asia/Shanghai")
    except Exception:  # noqa: BLE001 — 缺 tzdata 时回退（与 panel.py 同口径）
        tz = None
    now = datetime.now(tz) if tz is not None else datetime.now()
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    if period == "week":
        # 本周一 0 点（weekday()：周一 = 0）
        window_start = today_start - timedelta(days=today_start.weekday())
    else:
        window_start = today_start

    # 对外展示的窗口起点（上海口径，前端照此画榜单日期）
    window_start_naive = window_start.replace(tzinfo=None)
    # r35：PlaybackSession.start_time 是 UTC-naive 入库（容器本地时间写入），
    # 此前的注释"假定其本身就是上海本地时间"不成立 —— 每天 00:00–08:00（CST）
    # 的播放会被排除在榜外（窗口起点超前 8 小时）。比较用 UTC 帧换算值。
    if tz is not None:
        window_start_utc = window_start.astimezone(timezone.utc).replace(tzinfo=None)
    else:
        window_start_utc = window_start_naive

    if board == "hot":
        # 热播榜：一次取出窗口内的 (user_id, item_id)，Python 侧聚合（表不大，不写复杂 SQL）
        rows = (
            db.query(em.PlaybackSession.user_id, em.PlaybackSession.item_id)
            .filter(em.PlaybackSession.start_time >= window_start_utc)
            .all()
        )

        raw_item_ids = {iid for _, iid in rows if iid is not None}

        # 被播放的条目本身必须未删除、未合并，并映射到聚合键（剧集 → series_id，电影/剧本体 → 自身 id）
        key_by_item: dict[int, int] = {}
        if raw_item_ids:
            key_by_item = {
                mid: (sid or mid)
                for mid, sid in db.query(em.MediaItem.id, em.MediaItem.series_id)
                .filter(
                    em.MediaItem.id.in_(list(raw_item_ids)),
                    em.MediaItem.deleted_at.is_(None),
                    em.MediaItem.merged_into_id.is_(None),
                )
                .all()
            }

        # 聚合键对应的剧 / 片实体同样必须未删除、未合并
        agg_keys = set(key_by_item.values())
        info_map: dict[int, tuple] = {}
        if agg_keys:
            info_map = {
                mid: (name, item_type)
                for mid, name, item_type in db.query(
                    em.MediaItem.id, em.MediaItem.name, em.MediaItem.item_type
                )
                .filter(
                    em.MediaItem.id.in_(list(agg_keys)),
                    em.MediaItem.deleted_at.is_(None),
                    em.MediaItem.merged_into_id.is_(None),
                )
                .all()
            }

        # 榜 + 每用户总播放次数（算我自己的排名用）
        counts: dict[int, int] = {}
        user_totals: dict[int, int] = {}
        for uid, iid in rows:
            key = key_by_item.get(iid)
            if key is None or key not in info_map:
                continue
            counts[key] = counts.get(key, 0) + 1
            if uid is not None:
                user_totals[uid] = user_totals.get(uid, 0) + 1

        top = sorted(counts.items(), key=lambda kv: kv[1], reverse=True)[:20]
        items = [
            {
                "item_id": key,
                "name": info_map[key][0],
                "item_type": info_map[key][1],
                "count": cnt,
            }
            for key, cnt in top
        ]
        my_total = user_totals.get(request_user.id, 0)
        my_rank = 1 + sum(1 for t in user_totals.values() if t > my_total) if my_total else None
    else:
        # 观影时长榜：时长 = (ended_at or last_update_at) − start_time；复播虚增已在
        # compat_routes 复活分支重置 start_time 处修掉（复播即新一轮）。
        sessions = (
            db.query(em.PlaybackSession)
            .filter(em.PlaybackSession.start_time >= window_start_utc)
            .all()
        )
        totals: dict[int, float] = {}
        for s in sessions:
            if s.user_id is None or not s.start_time:
                continue
            end = s.ended_at or s.last_update_at or s.start_time
            secs = (end - s.start_time).total_seconds()
            if secs < 0:
                continue  # 脏数据（时钟回拨等）不计入
            totals[s.user_id] = totals.get(s.user_id, 0.0) + secs

        # 只统计允许上榜的用户；isnot(False) 让 NULL 也视为允许（老库补列前兼容）
        shown: set[int] = set()
        names: dict[int, str] = {}
        if totals:
            uids = list(totals.keys())
            shown = {
                uid
                for (uid,) in db.query(models.WebUser.id)
                .filter(
                    models.WebUser.id.in_(uids),
                    models.WebUser.show_on_leaderboard.isnot(False),
                )
                .all()
            }
            names = {
                uid: name
                for uid, name in db.query(models.WebUser.id, models.WebUser.username)
                .filter(models.WebUser.id.in_(uids))
                .all()
            }

        ranked = sorted(
            ((uid, sec) for uid, sec in totals.items() if uid in shown),
            key=lambda kv: kv[1],
            reverse=True,
        )
        items = [
            {"user_id": uid, "nickname": names.get(uid, ""), "seconds": int(sec)}
            for uid, sec in ranked[:20]
        ]
        # 我自己即使关了上榜，也返回真实排名（只给自己看）
        my_total = totals.get(request_user.id, 0)
        my_rank = (
            1 + sum(1 for _uid, sec in ranked if sec > my_total)
            if my_total
            else None
        )

    return {
        "board": board,
        "period": period,
        "window_start": window_start_naive.isoformat(),
        "items": items,
        "my_rank": my_rank,
    }


@user_emby_router.get("/sessions")
def get_my_sessions(request_user: models.WebUser = Depends(get_admin_or_emby_user),
                          db: Session = Depends(get_db)):
    """我的正在播放会话（设备 / 客户端 / 进度），与管理员会话监控同源"""
    rows = (
        db.query(em.PlaybackSession, em.MediaItem)
        .join(em.MediaItem, em.MediaItem.id == em.PlaybackSession.item_id)
        .filter(
            em.PlaybackSession.user_id == request_user.id,
            em.PlaybackSession.ended_at.is_(None),
        )
        .order_by(em.PlaybackSession.last_update_at.desc())
        .all()
    )
    return {
        "sessions": [
            {
                "session_key": s.session_key,
                "item_id": i.guid,
                "item": i.name,
                "item_type": i.item_type,
                "device": s.device_name,
                "client": s.client_name,
                "remote_addr": s.remote_addr,
                "play_method": s.play_method,
                "is_paused": bool(s.is_paused),
                "position_ticks": s.position_ticks,
                "duration_ticks": i.duration_ticks,
                "progress": round((s.position_ticks or 0) / (i.duration_ticks or 1) * 100, 1),
                "started_at": s.start_time.isoformat() if s.start_time else None,
                "updated_at": s.last_update_at.isoformat() if s.last_update_at else None,
            }
            for s, i in rows
        ]
    }


@user_emby_router.delete("/sessions/{session_key}")
async def stop_my_session(session_key: str,
                          request_user: models.WebUser = Depends(get_admin_or_emby_user),
                          db: Session = Depends(get_db)):
    """结束自己的播放会话"""
    def _end_session() -> None:
        "结束会话的同步段：查会话 / 写结束时间（下放线程池）"
        session = (
            db.query(em.PlaybackSession)
            .filter(
                em.PlaybackSession.session_key == session_key,
                em.PlaybackSession.user_id == request_user.id,
            )
            .first()
        )
        if not session:
            raise HTTPException(status_code=404, detail="播放会话不存在")
        session.ended_at = datetime.now()
        db.commit()

    await run_in_threadpool(_end_session)
    return {"success": True}


@user_emby_router.get("/history")
def get_watch_history(request_user: models.WebUser = Depends(get_admin_or_emby_user),
                           db: Session = Depends(get_db),
                           limit: int = 30,
                           offset: int = 0,
                           item_type: str = ""):
    """观看历史：按条目去重，取最近一次播放的设备/客户端/进度

    直接读本地会话与用户媒体数据（不触发媒体库扫描），与第三方客户端记录同源。

    顶层口径：单集观看记录按剧聚合（每部剧只保留最近看的那集，行上标注
    "第X季第X集 · 集名"，点行进剧集详情并定位到该集）；季/单集不出现在顶层。
    """
    limit = max(1, min(limit, 100))
    offset = max(0, offset)

    # 前端传 Movie/Series（Emby 惯例首字母大写），库里存小写；此前直接 == 比对，
    # 在 SQLite 下筛选恒为空。归一化后再比；筛"剧集"时把单集也带上（后面按剧聚合）。
    type_norm = (item_type or "").strip().lower()
    item_filter = []
    if type_norm == "series":
        item_filter.append(em.MediaItem.item_type.in_(["series", "episode"]))
    elif type_norm:
        item_filter.append(em.MediaItem.item_type == type_norm)

    # G1-P1-3：去重下推 SQL。原实现 .all() 拉出用户全部会话再内存去重/聚合，
    # PlaybackSession 随使用无界增长，请求代价随历史单调变大。
    # 1) 每条目最近一次会话时间（GROUP BY），2) 分块取候选做单集→剧聚合。
    sess = em.PlaybackSession
    latest = (
        db.query(
            sess.item_id.label("item_id"),
            func.max(sess.last_update_at).label("max_at"),
        )
        .join(em.MediaItem, em.MediaItem.id == sess.item_id)
        .filter(sess.user_id == request_user.id, *item_filter)
        .group_by(sess.item_id)
        .subquery()
    )
    total = (
        db.query(func.count(sess.id))
        .join(em.MediaItem, em.MediaItem.id == sess.item_id)
        .filter(sess.user_id == request_user.id, *item_filter)
        .scalar()
        or 0
    )
    # unique_total：去重+单集→剧聚合后的展示行数（与 Python 聚合同口径）。
    # 孤儿单集（series 行缺失）Python 侧会跳过不展示，SQL 侧按自身计数——仅脏数据有差异。
    _series = aliased(em.MediaItem)
    _display_key = case(
        (
            and_(
                em.MediaItem.item_type == "episode",
                em.MediaItem.series_id.isnot(None),
                _series.id.isnot(None),
            ),
            _series.id,
        ),
        else_=em.MediaItem.id,
    )
    unique_total = (
        db.query(func.count(func.distinct(_display_key)))
        .select_from(latest)
        .join(em.MediaItem, em.MediaItem.id == latest.c.item_id)
        .outerjoin(_series, _series.id == em.MediaItem.series_id)
        .scalar()
        or 0
    )

    # 分块取候选（按 max_at 倒序）：聚合只会减少行数，块内保持全局顺序；
    # 攒够 offset+limit 个聚合行或取完为止。单块 200 行，代价有界。
    _CHUNK = 200
    need = offset + limit
    aggregated: list[tuple] = []  # (session, 展示条目, 单集|None)
    seen_series: set[int] = set()
    series_cache: dict[int, em.MediaItem] = {}
    chunk_idx = 0
    while len(aggregated) < need:
        cand_rows = (
            db.query(sess, em.MediaItem)
            .join(em.MediaItem, em.MediaItem.id == sess.item_id)
            .join(
                latest,
                and_(
                    latest.c.item_id == sess.item_id,
                    latest.c.max_at == sess.last_update_at,
                ),
            )
            .filter(sess.user_id == request_user.id, *item_filter)
            .order_by(latest.c.max_at.desc(), sess.id.desc())
            .offset(chunk_idx * _CHUNK)
            .limit(_CHUNK)
            .all()
        )
        if not cand_rows:
            break
        chunk_idx += 1
        # 块内按条目去重（时间并列时 join 可能带出多行，取 id 最大的那条会话）
        seen_item: set[int] = set()
        deduped: list[tuple] = []
        for s, it in cand_rows:
            if it.id in seen_item:
                continue
            seen_item.add(it.id)
            deduped.append((s, it))
        # 父剧集批量取（原逐个查询的 N+1）
        need_series = {
            it.series_id
            for _, it in deduped
            if it.item_type == "episode" and it.series_id and it.series_id not in series_cache
        }
        if need_series:
            for sr in db.query(em.MediaItem).filter(em.MediaItem.id.in_(need_series)).all():
                series_cache[sr.id] = sr
        # 单集 → 父剧集聚合（保持最近在看的顺序，每部剧只留一行）
        for s, it in deduped:
            ep = None
            show = it
            if it.item_type == "episode" and it.series_id:
                series = series_cache.get(it.series_id)
                if series is None:
                    continue  # 孤儿单集：归属丢失，不展示
                if series.id in seen_series:
                    continue
                seen_series.add(series.id)
                ep = it
                show = series
            aggregated.append((s, show, ep))
    page = aggregated[offset:offset + limit]

    # 进度取"实际播放的那集"的用户数据（聚合行取单集的，电影行取自身的）
    lookup_ids = [it.id for _s, it, _e in page]
    lookup_ids += [e.id for _s, _i, e in page if e is not None]
    umd_map: dict[int, em.UserMediaData] = {}
    if lookup_ids:
        for umd in db.query(em.UserMediaData).filter(
            em.UserMediaData.user_id == request_user.id,
            em.UserMediaData.item_id.in_(lookup_ids),
        ).all():
            umd_map[umd.item_id] = umd

    items = []
    for session, item, ep in page:
        umd = umd_map.get(ep.id if ep is not None else item.id)
        # 进度口径：会话是最新一次播放的实际位置；umd 可能因「标为已看」/
        # 播完被清零（playback_position_ticks=0），此时若仍优先 umd，
        # 会把「看到一半」的进度显示成 0。会话有位置时优先用会话的。
        _pos = session.position_ticks or (umd.playback_position_ticks if umd else 0)
        entry = {
            "id": item.guid,
            "name": item.name,
            "type": item.item_type,
            "year": item.production_year,
            # 季无图时回退父剧集海报（season_poster_url），其余类型行为不变
            "poster_url": season_poster_url(item, db),
            "duration_ticks": (ep or item).duration_ticks,
            "position_ticks": _pos,
            "played": bool(umd.played) if umd else False,
            "is_favorite": bool(umd.is_favorite) if umd else False,
            "play_count": int(umd.play_count or 0) if umd else 0,
            "device": session.device_name,
            "client": session.client_name,
            "play_method": session.play_method,
            "watched_at": session.last_update_at.isoformat() if session.last_update_at else None,
        }
        if ep is not None:
            entry.update({
                "episode_id": ep.guid,
                "episode_name": ep.name,
                "season_number": ep.season_number,
                "episode_number": ep.episode_number,
            })
        items.append(entry)

    # unique_total 为 SQL 去重计数：旧 Python 版先全量聚合再切片，len(aggregated) 本就是
    # 全量去重数（不是截断后的值），与这里的 unique_total 语义一致。
    return {"total": total, "unique_total": unique_total, "items": items}


# ==================== 管理端 ====================

_LIBRARY_COVER_MAX_BYTES = 8 * 1024 * 1024
_LIBRARY_COVER_TYPES = {
    ".jpg": ("image/jpeg", b"\xff\xd8\xff"),
    ".jpeg": ("image/jpeg", b"\xff\xd8\xff"),
    ".png": ("image/png", b"\x89PNG\r\n\x1a\n"),
    ".webp": ("image/webp", b"RIFF"),
}


def _validate_library_cover(filename: str, content_type: str | None, data: bytes) -> tuple[str, str]:
    """校验直传封面，只接受扩展名、声明类型与文件头都一致的 JPEG / PNG / WebP。"""
    if not data:
        raise HTTPException(status_code=400, detail="封面图片不能为空")
    if len(data) > _LIBRARY_COVER_MAX_BYTES:
        raise HTTPException(status_code=413, detail="封面图片不能超过 8 MB")
    extension = os.path.splitext(filename or "")[1].lower()
    expected = _LIBRARY_COVER_TYPES.get(extension)
    if not expected:
        raise HTTPException(status_code=400, detail="封面仅支持 JPG、PNG 或 WebP 图片")
    media_type, signature = expected
    if (content_type or media_type).split(";", 1)[0].lower() != media_type:
        raise HTTPException(status_code=400, detail="图片文件类型与扩展名不一致")
    valid_signature = data.startswith(signature)
    if media_type == "image/webp":
        valid_signature = valid_signature and data[8:12] == b"WEBP"
    if not valid_signature:
        raise HTTPException(status_code=400, detail="图片内容不是有效的 JPG、PNG 或 WebP")
    return extension, media_type


def _library_cover_file(relative_path: str | None) -> str | None:
    """把库里的相对路径解析到图片目录内；越界或文件不存在都返回 None。"""
    if not relative_path:
        return None
    root = os.path.abspath(image_store.image_dir())
    path = os.path.abspath(os.path.join(root, relative_path))
    if not path.startswith(root + os.sep) or not os.path.isfile(path):
        return None
    return path


def _remove_library_cover(relative_path: str | None) -> None:
    path = _library_cover_file(relative_path)
    if not path:
        return
    try:
        os.remove(path)
    except OSError as exc:
        logger.warning("删除媒体库封面失败 %s: %s", path, exc)


class LibraryPathEntry(BaseModel):
    """媒体库的一条路径（**路径与存储后端分开**，界面不出现 mount:// 前缀）

    - ``path``：本机来源给绝对路径；挂载来源给挂载内路径（``/视频/剧集/国产剧``）；
      高级模式下也允许直接写 ``rclone:gdrive/Movies`` / ``115:/0`` 这种前缀路径。
    - ``backend``：``local`` / ``rclone`` / ``115``，决定选哪个存储挂载。
    - ``mount_id``：远程来源必填（本地来源忽略）。
    """
    path: str
    backend: str = "local"
    mount_id: int | None = None
    mount_name: str = ""
    backend_label: str = ""


class LibraryCreate(BaseModel):
    name: str
    collection_type: str = "movies"
    # 内容来源：本机目录（可多个）与存储挂载（storage_mounts.id）至少给一个
    paths: list[str] = []
    # 简化形式（推荐用这个）：路径与存储后端分开，不出现 mount:// 前缀。
    # 传了它就以它为准；不传则回退到老的 paths 写法。
    path_entries: list[LibraryPathEntry] | None = None
    # v2.46.0 起前端不再下发这个字段（挂载已在每条 path_entry 上表达）。
    # 保留是为了兼容老前端与外部调用方：传了照旧写入，新界面一律不传。
    mount_ids: list[int] = []
    is_enabled: bool = True
    # 刮削策略：missing_only（只补缺，默认）/ 3m / 6m / 1y / all（每次全量重刮）
    scrape_policy: str = "missing_only"
    # 绑定 115 账号配置档（不同媒体库可用不同账号转存/下载）
    account_115_id: int | None = None
    # 归属：服（多服运营）与播放节点（多机同时出流）；留空 = 当前服 / 未分配节点
    realm_id: int | None = None
    node_id: int | None = None
    # 封面自动生成：样式 + 标题文字；空 = 用直传的 cover_path
    cover_template: str | None = None
    cover_title: str | None = None
    cover_subtitle: str | None = None
    # 新片入库后自动重生成封面（默认关）
    cover_auto_regen: bool = False
    # 扫描策略开关（v2.44.0，默认开）
    incremental_scan: bool = True
    fs_watch: bool = True


class LibraryUpdate(BaseModel):
    name: str | None = None
    collection_type: str | None = None
    paths: list[str] | None = None
    path_entries: list[LibraryPathEntry] | None = None
    # v2.46.0 起前端不再下发这个字段。**省略 = 不修改**（老数据原样保留，不会被清空），
    # 只有显式传列表才会覆盖 —— 这样前端「不展示」不等于「抹掉」。
    mount_ids: list[int] | None = None
    is_enabled: bool | None = None
    scrape_policy: str | None = None
    account_115_id: int | None = None
    # 归属：服（多服运营）与播放节点（多机同时出流）。显式传 null 表示「不分配」
    realm_id: int | None = None
    node_id: int | None = None
    # 封面自动生成：传 null/空串表示清除（回退直传封面），省略则不修改
    cover_template: str | None = None
    cover_title: str | None = None
    cover_subtitle: str | None = None
    cover_auto_regen: bool | None = None
    # 扫描策略开关（v2.44.0）：不传 = 不修改
    incremental_scan: bool | None = None
    fs_watch: bool | None = None


def _validate_library_sources(db: Session, paths: list[str], mount_ids: list[int]) -> None:
    """校验媒体库来源：本机路径必须存在，挂载必须存在且启用。

    ``paths`` 里还可以写 ``mount://<挂载 id>/<子目录>``（只扫描该挂载下的子目录），
    此时校验挂载存在、启用且子目录可读。

    空条目一律跳过：``path_entries=[]`` 会被拼成空串再 ``split(",")`` 成 ``[""]``，
    不跳过的话「一个路径都没配」会被误判成「目录不存在」（v2.46.0 前老库里只剩
    ``mount_ids`` 的那种库，改个名字都存不进去）。

    ``rclone:gdrive/Movies`` 这种**前缀路径**同样跳过：它根本不是本机目录，拿
    ``os.path.isdir`` 判必然是「不存在」，于是老库上的这类存量条目会把整个表单卡死
    （存不进去 ≠ 报错提示对，是最难查的一种）。它到底通不通得由扫描期说（那时会把它
    记成「不可用来源」并跳过清理，而不是在这里拒绝保存）。
    """
    for path in paths:
        if not (path or "").strip():
            continue
        detected = mount_lib.detect_mount_type(path)
        if detected and detected != mount_lib.MOUNT_LOCAL:
            continue
        parsed = mount_lib.parse_mount_path(path)
        if parsed is not None:
            mount_id, rel = parsed
            try:
                mount_lib.check_mount_subpath(db, mount_id, rel)
            except mount_lib.MountError as exc:
                raise HTTPException(status_code=400, detail=f"挂载子目录不可用 {path}: {exc}")
            continue
        if not os.path.isdir(path):
            # 与扫描期同一套措辞：写错格式（带方括号/引号、mount:// 前缀不对）时
            # 说清真原因，别让人存进去之后在扫描里猜（见 mounts.explain_local_path_failure）
            raise HTTPException(
                status_code=400,
                detail=f"{mount_lib.explain_local_path_failure(path)}（{path}）",
            )
    if not mount_ids:
        return
    found = {
        m.id: m for m in db.query(em.StorageMount).filter(em.StorageMount.id.in_(mount_ids)).all()
    }
    for mount_id in mount_ids:
        mount = found.get(mount_id)
        if mount is None:
            raise HTTPException(status_code=400, detail=f"挂载不存在: #{mount_id}")
        if not mount.is_enabled:
            raise HTTPException(status_code=400, detail=f"挂载「{mount.name}」已停用")


def _mount_ids_field(db: Session, mount_ids: list[int]) -> str:
    return ",".join(str(i) for i in dict.fromkeys(mount_ids))


def _resolve_entry_mounts(db: Session, entries: list[LibraryPathEntry]) -> list[dict]:
    """路径条目 → 入库形态（自动拼回 mount://，并校验挂载与后端对得上）

    返回 ``[{"raw", "backend"}, …]``，顺序与入参一致（``storage_backends`` 按位存）。
    """
    out: list[dict] = []
    for entry in entries:
        path = (entry.path or "").strip()
        if not path:
            raise HTTPException(status_code=400, detail="媒体路径不能为空")
        backend = mount_lib.normalize_storage_backend(entry.backend) or "local"
        mount = None
        if entry.mount_id is not None:
            mount = db.query(em.StorageMount).filter(
                em.StorageMount.id == entry.mount_id).first()
            if mount is None:
                raise HTTPException(status_code=400,
                                    detail=f"存储挂载不存在: #{entry.mount_id}")
            if not mount.is_enabled:
                raise HTTPException(
                    status_code=400, detail=f"挂载「{mount.name}」已停用")
            if not mount_lib.backend_matches_mount(backend, mount):
                raise HTTPException(
                    status_code=400,
                    detail=(f"「{path}」选的是{mount_lib.storage_backend_label(backend)}，"
                            f"但挂载「{mount.name}」是"
                            f"{mount_lib.storage_backend_label(mount.mount_type)}"),
                )
        item = {"path": path, "backend": backend, "mount_id": mount.id if mount else None}
        try:
            item["raw"] = mount_lib.assemble_library_path(item)
        except mount_lib.MountError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        out.append(item)
    return out


def _library_path_payload(db: Session, entries: list[LibraryPathEntry]) -> tuple[str, str]:
    """路径条目 → ``(paths 字段, storage_backends 字段)``（两者逐条对应，一次写入）"""
    resolved = _resolve_entry_mounts(db, entries)
    raws: list[str] = []
    for item in resolved:
        if item["raw"] not in raws:
            raws.append(item["raw"])
    # 去重后后端列要跟着重排：按 raw 回填，重复项沿用第一次的后端
    backend_by_raw = {item["raw"]: item["backend"] for item in resolved}
    backends = [backend_by_raw.get(raw, "") for raw in raws]
    return ",".join(raws), mount_lib.dump_storage_backends(backends)


class VirtualLibraryRequest(BaseModel):
    """按发行平台生成虚拟媒体库

    - platforms 省略 = 按库里**实际出现过的**平台标签逐个生成
    - enabled 控制生成后是否对客户端可见（关闭时客户端与直达接口都看不到）
    - source_library_id 省略 = 跨全部媒体库聚合
    """
    platforms: list[str] | None = None
    enabled: bool = True
    source_library_id: int | None = None
    prune: bool = False  # 清理不再有内容的虚拟库


@admin_emby_router.get("/overview")
def admin_overview(staff: models.WebUser = Depends(require_staff), db: Session = Depends(get_db),
                         realm_id: int | None = None):
    """媒体库概览：默认只统计当前服（“全部服”传 realm_id=0）"""
    scope_id = None if realm_id == 0 else (realm_id or realms.active_realm_id(db))
    # 内容按「NULL = 所有服」处理（见 realms.scope_inclusive）：未标注服的老库不会被藏起来
    lib_query = realms.scope_inclusive(db.query(em.Library), em.Library.realm_id, scope_id)
    total_libraries = lib_query.count()
    lib_ids = [row[0] for row in realms.scope_inclusive(
        db.query(em.Library.id), em.Library.realm_id, scope_id).all()]
    total_items = (soft_delete.count_visible(db, em.MediaItem.library_id.in_(lib_ids))
                   if lib_ids else 0)
    active_sessions = (
        db.query(em.PlaybackSession).filter(em.PlaybackSession.ended_at.is_(None)).count()
    )
    total_users = db.query(models.WebUser).count()
    realm = realms.get_realm(db, scope_id) if scope_id else None
    return {
        "total_items": total_items,
        "total_libraries": total_libraries,
        "active_sessions": active_sessions,
        "total_users": total_users,
        "server_id": SERVER_ID,
        "realm_id": scope_id,
        "realm_name": realm.name if realm else "全部服",
    }


@admin_emby_router.get("/libraries")
def list_libraries(staff: models.WebUser = Depends(require_staff), db: Session = Depends(get_db),
                         realm_id: int | None = None):
    """媒体库清单（按服；realm_id=0 表示全部服）"""
    scope_id = None if realm_id == 0 else (realm_id or realms.active_realm_id(db))
    query = realms.scope_inclusive(db.query(em.Library), em.Library.realm_id, scope_id)
    libs = query.order_by(em.Library.id).all()
    nodes = {n.id: n for n in db.query(models.RemoteServer)
             .filter(models.RemoteServer.kind == "ea").all()}
    realm_names = {r.id: r.name for r in realms.list_realms(db)}
    # 本机目录可读性（v2.46.0）：Dashboard 之前看的是 EA 可达性（“旧挂载状态”），
    # 但面板真正会因挂载断掉而扫不到的是**本机路径**。这里只做真实 stat/列目录，
    # 不做 os.access 单判；远程来源（mount:// / rclone: / 115:）不归它管，置 null。
    local_readable: dict = {}

    def _local_read_state(lib) -> dict:
        from backend.emby_server import fs_watcher

        state = {"ok": None, "missing": [], "denied": [], "remote": False}
        for raw in mount_lib.split_library_paths(getattr(lib, "paths", "")):
            if mount_lib.parse_mount_path(raw) is not None or not raw.startswith("/"):
                state["remote"] = True          # 远程来源：本机读不到是正常的
                continue
            # FUSE / 网络盘不在这儿 scandir：一个卡死的挂载点会把**这个列表接口**
            # 同步拖住（§五验收：API 请求不能被阻塞）。它们由 fs_watcher 与扫描器负责。
            fstype = fs_watcher.fstype_of(raw)
            if fstype and fstype in fs_watcher.FUSE_FSTYPES:
                state["remote"] = True
                continue
            try:
                # 真正列一次目录：只 isdir 会把「挂载断了但空目录还在」判成正常
                with os.scandir(raw) as it:
                    next(it, None)
            except PermissionError:
                state["denied"].append(raw)
            except (FileNotFoundError, NotADirectoryError):
                state["missing"].append(raw)
            except OSError as exc:               # I/O 错误 / FUSE 挂掉
                state["denied"].append(f"{raw}（{exc.strerror or exc}）")
            else:
                state["ok"] = True
        # 口径：任一本机路径读不到即 False——missing / denied 谁非空都算，哪怕别的路径可读
        if state["missing"] or state["denied"]:
            state["ok"] = False
        return state
    # 路径回显要靠挂载表把 ``mount://<id>/…`` 还原成「挂载内路径 + 后端标签」（挂载数很少，一次查完）
    mounts_by_id = {m.id: m for m in db.query(em.StorageMount).all()}
    # 播放可达性：库的「归属节点」决定了内容要在哪台机器上真正存在（见 reachability 模块）
    # 按服各算一份上下文：一个服一条库地混在一个列表里时，不能用甲服的节点去判乙服的库
    reach_ctxs: dict = {}

    def _reach_ctx(lib_realm):
        key = lib_realm if lib_realm is not None else 0
        if key not in reach_ctxs:
            reach_ctxs[key] = reachability.build_context(
                db, lib_realm if lib_realm is not None else scope_id)
        return reach_ctxs[key]

    for _lib in libs:
        local_readable[_lib.id] = _local_read_state(_lib)

    return {"libraries": [
        {
            "id": lib.id, "guid": lib.guid, "name": lib.name,
            "collection_type": lib.collection_type,
            "paths": [p for p in (lib.paths or "").split(",") if p],
            # 简化路径条目：界面用它渲染列表（不带 mount:// 前缀，带存储后端标签）
            "path_entries": mount_lib.library_path_entries(lib, mounts_by_id),
            # 本机目录可读性（v2.46.0）：ok=可列举 / False=有路径读不到 / null=没有本机路径
            # 注意：下发的是布尔/None（前端用 === true/=== false 比较），不能把整个 state 字典塞进去
            "local_dirs_readable": local_readable.get(lib.id, {}).get("ok"),
            "mount_ids": mount_lib.parse_mount_ids(lib),
            "is_enabled": lib.is_enabled,
            # 正在扫描以进程内任务为准：数据库标志在进程崩溃后会残留为真
            "is_scanning": is_scan_active(lib.id) or lib.is_scanning,
            # 实时状态（v2.27.0）：排队中 / 扫描中（阶段、已发现、已处理、当前目录、本轮远程请求数）
            # 空闲时为 null——列表刷新就能接上刚才那几秒的进度，不用另开接口轮询
            "scan_live": scan_queue.live_payload(lib),
            "scrape_policy": normalize_scrape_policy(lib.scrape_policy),
            "is_virtual": bool(getattr(lib, "is_virtual", False)),
            "platform": lib.platform,
            "cover_url": f"/api/admin/emby/libraries/{lib.id}/cover" if lib.cover_path else None,
            # 封面配置：样式回显 + “新片入库后自动更新”开关（老库没这列时按关闭处理）
            "cover_template": lib.cover_template,
            "cover_title": lib.cover_title,
            "cover_subtitle": lib.cover_subtitle,
            "cover_auto_regen": bool(getattr(lib, "cover_auto_regen", False)),
            # 扫描策略开关（v2.44.0）：老库没值时按默认「都开」处理，与升级前行为一致
            "incremental_scan": getattr(lib, "incremental_scan", True) is not False,
            "fs_watch": getattr(lib, "fs_watch", True) is not False,
            "account_115_id": getattr(lib, "account_115_id", None),
            "last_scan_at": lib.last_scan_at.isoformat() if lib.last_scan_at else None,
            # 最近一次扫描的结果：新增/更新/删除多少、哪些来源读不到、有没有异常
            "last_scan": scan_result_payload(lib),
            "item_count": lib.item_count,
            # 服与播放节点：多服 / 多机部署下“这个库归谁”必须一眼可见
            "realm_id": lib.realm_id,
            "realm_name": realm_names.get(lib.realm_id, "") if lib.realm_id else "",
            "node_id": lib.node_id,
            "node_name": (nodes[lib.node_id].name if lib.node_id in nodes else ""),
            "node_online": (nodes[lib.node_id].last_check_ok is True) if lib.node_id in nodes else None,
            # 播放可达性（ok/warn/bad + 原因 + 改法）：面板扫描正常但出流节点读不到内容时，
            # 这里就会是 warn/bad，而不是等客户端点播放才暴露成 404/502
            "playback": reachability.library_reachability(db, lib, _reach_ctx(lib.realm_id)),
        }
        for lib in libs
    ],
        "realm_id": scope_id,
        "active_realm_id": realms.active_realm_id(db),
        "scrape_policies": [
            {"value": "missing_only", "label": "仅缺失时刮削"},
            {"value": "3m", "label": "3 个月重刮"},
            {"value": "6m", "label": "半年重刮"},
            {"value": "1y", "label": "一年重刮"},
            {"value": "all", "label": "全部重刮"},
        ],
        # 存储后端下拉（本地文件 / Rclone / 115 网盘）——**由后端下发**，前端不自己维护一份
        "storage_backends": [
            {"value": value, "label": label}
            for value, label in mount_lib.STORAGE_BACKEND_LABELS.items()
        ]}


@admin_emby_router.post("/libraries")
def create_library(req: LibraryCreate, staff: models.WebUser = Depends(require_staff), db: Session = Depends(get_db)):
    import uuid

    # 简化形式优先：界面传的是「裸路径 + 存储后端」，mount:// 前缀在后端拼回去
    backends_field = ""
    if req.path_entries is not None:
        paths_field, backends_field = _library_path_payload(db, req.path_entries)
        req.paths = paths_field.split(",") if paths_field else []
    if not req.paths and not req.mount_ids:
        raise HTTPException(status_code=400, detail="请至少配置一个路径或一个存储挂载")
    _validate_library_sources(db, req.paths, req.mount_ids)
    guid = uuid.uuid4().hex[:32]
    realm_id = req.realm_id or realms.active_realm_id(db)
    if not realms.get_realm(db, realm_id):
        raise HTTPException(status_code=400, detail=f"服不存在: #{realm_id}")
    node_id = req.node_id
    if node_id is not None:
        node = db.query(models.RemoteServer).filter(models.RemoteServer.id == node_id).first()
        if not node or node.kind != "ea":
            raise HTTPException(status_code=400, detail="只能把媒体库分配给一台后端服（EA）")
        if node.realm_id and node.realm_id != realm_id:
            raise HTTPException(status_code=400, detail="这台节点属于另一个服，不能分配本服的媒体库")
    lib = em.Library(
        guid=guid, name=req.name, collection_type=req.collection_type,
        paths=",".join(req.paths),
        storage_backends=backends_field,
        mount_ids=_mount_ids_field(db, req.mount_ids),
        is_enabled=req.is_enabled,
        scrape_policy=normalize_scrape_policy(req.scrape_policy),
        account_115_id=req.account_115_id,
        realm_id=realm_id, node_id=node_id,
        cover_template=req.cover_template, cover_title=req.cover_title,
        cover_subtitle=req.cover_subtitle,
        cover_auto_regen=bool(req.cover_auto_regen),
        incremental_scan=bool(req.incremental_scan),
        fs_watch=bool(req.fs_watch),
    )
    db.add(lib)
    db.commit()
    db.refresh(lib)
    return {"success": True, "id": lib.id, "guid": lib.guid}


@admin_emby_router.post("/libraries/{lib_id}/cover")
def upload_library_cover(
    lib_id: int,
    file: UploadFile = File(...),
    staff: models.WebUser = Depends(require_staff),
    db: Session = Depends(get_db),
):
    """直接上传媒体库封面；先校验图片并原子落盘，再更新数据库引用。"""
    lib = db.query(em.Library).filter(em.Library.id == lib_id).first()
    if not lib:
        raise HTTPException(status_code=404, detail="媒体库不存在")
    data = file.file.read(_LIBRARY_COVER_MAX_BYTES + 1)
    extension, media_type = _validate_library_cover(file.filename or "", file.content_type, data)
    relative_path = os.path.join("library-covers", f"{lib.guid}{extension}")
    target = _library_cover_file(relative_path)
    if target is None:
        target = os.path.abspath(os.path.join(image_store.image_dir(), relative_path))
        os.makedirs(os.path.dirname(target), exist_ok=True)
    temporary = f"{target}.{os.getpid()}.uploading"
    try:
        with open(temporary, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, target)
        old_cover = lib.cover_path
        lib.cover_path = relative_path
        db.commit()
    except Exception:
        db.rollback()
        try:
            os.remove(temporary)
        except OSError:
            pass
        raise
    if old_cover and old_cover != relative_path:
        _remove_library_cover(old_cover)
    return {
        "success": True,
        "cover_url": f"/api/admin/emby/libraries/{lib.id}/cover",
        "content_type": media_type,
    }


@admin_emby_router.get("/libraries/{lib_id}/cover")
def get_library_cover(
    lib_id: int,
    staff: models.WebUser = Depends(require_staff),
    db: Session = Depends(get_db),
):
    lib = db.query(em.Library).filter(em.Library.id == lib_id).first()
    if not lib:
        raise HTTPException(status_code=404, detail="媒体库不存在")
    path = _library_cover_file(lib.cover_path)
    if not path:
        raise HTTPException(status_code=404, detail="媒体库尚未设置封面")
    media_type = _LIBRARY_COVER_TYPES.get(os.path.splitext(path)[1].lower(), ("application/octet-stream", b""))[0]
    return FileResponse(path, media_type=media_type, headers={"Cache-Control": "no-store"})


@admin_emby_router.get("/images/thumbs/status")
def thumbs_backfill_status(
    staff: models.WebUser = Depends(require_staff),
    db: Session = Depends(get_db),
):
    """缩略图补生成进度：水位 id / 宽度档 / Pillow 是否可用"""
    return {"success": True, **image_store.backfill_status(db)}


@admin_emby_router.post("/images/thumbs/backfill")
def thumbs_backfill_run(
    staff: models.WebUser = Depends(require_staff),
    db: Session = Depends(get_db),
):
    """手动触发一轮缩略图补生成（限速+断点续跑；维护周期每天也会自动跑一批）"""
    result = image_store.backfill_thumbnails(db)
    return {"success": True, **result}


@admin_emby_router.delete("/libraries/{lib_id}/cover")
def remove_library_cover(
    lib_id: int,
    staff: models.WebUser = Depends(require_staff),
    db: Session = Depends(get_db),
):
    lib = db.query(em.Library).filter(em.Library.id == lib_id).first()
    if not lib:
        raise HTTPException(status_code=404, detail="媒体库不存在")
    old_cover = lib.cover_path
    lib.cover_path = None
    db.commit()
    _remove_library_cover(old_cover)
    return {"success": True}


@admin_emby_router.put("/libraries/{lib_id}")
def update_library(lib_id: int, req: LibraryUpdate, staff: models.WebUser = Depends(require_staff), db: Session = Depends(get_db)):
    lib = db.query(em.Library).filter(em.Library.id == lib_id).first()
    if not lib:
        raise HTTPException(status_code=404, detail="媒体库不存在")
    if req.name is not None:
        lib.name = req.name
    if req.collection_type is not None:
        lib.collection_type = req.collection_type
    if req.path_entries is not None:
        paths_field, backends_field = _library_path_payload(db, req.path_entries)
        _validate_library_sources(db, paths_field.split(","), [])
        lib.paths = paths_field
        lib.storage_backends = backends_field
    elif req.paths is not None:
        _validate_library_sources(db, req.paths, [])
        lib.paths = ",".join(req.paths)
        # 老写法（直接给 paths）没有后端信息：把这一列清空，让回显层按「挂载类型 + 路径
        # 前缀」现推。写死一个猜出来的值更糟——把 115 的库标成「本地文件」会让管理员
        # 去查错的挂载。
        lib.storage_backends = ""
    if req.mount_ids is not None:
        _validate_library_sources(db, [], req.mount_ids)
        lib.mount_ids = _mount_ids_field(db, req.mount_ids)
    if req.is_enabled is not None:
        lib.is_enabled = req.is_enabled
    if req.scrape_policy is not None:
        lib.scrape_policy = normalize_scrape_policy(req.scrape_policy)
    if "cover_template" in req.model_fields_set:
        lib.cover_template = req.cover_template or None
    if "cover_title" in req.model_fields_set:
        lib.cover_title = req.cover_title or None
    if "cover_subtitle" in req.model_fields_set:
        lib.cover_subtitle = req.cover_subtitle or None
    if "cover_auto_regen" in req.model_fields_set:
        # 没选封面样式时这个开关是空转的，但不拦着存：扫描侧的 _cover_autogen_after_scan
        # 会自己跳过（没有可复用的样式，重画必然失败），没必要为此拒绝保存整个表单
        lib.cover_auto_regen = bool(req.cover_auto_regen)
    if req.incremental_scan is not None:
        lib.incremental_scan = bool(req.incremental_scan)
    if req.fs_watch is not None:
        lib.fs_watch = bool(req.fs_watch)
    if "account_115_id" in req.model_fields_set:
        # 允许显式解绑（传 null）
        lib.account_115_id = req.account_115_id
    if "realm_id" in req.model_fields_set:
        if req.realm_id is None:
            # 显式解绑：未标注服 = 所有服可见（与老数据、未分配节点的口径一致）
            lib.realm_id = None
        else:
            if not realms.get_realm(db, req.realm_id):
                raise HTTPException(status_code=400, detail=f"服不存在: #{req.realm_id}")
            # 换服时把绑定的挂载一起带过去，否则库会引用到别的服的存储
            lib.realm_id = req.realm_id
            for mount_id in mount_lib.parse_mount_ids(lib):
                mount = db.query(em.StorageMount).filter(em.StorageMount.id == mount_id).first()
                if mount and mount.realm_id != req.realm_id:
                    mount.realm_id = req.realm_id
            if lib.node_id:
                node = db.query(models.RemoteServer).filter(models.RemoteServer.id == lib.node_id).first()
                if node and node.realm_id != req.realm_id:
                    lib.node_id = None  # 节点属于别的服，解绑避免跨服出流
    if "node_id" in req.model_fields_set:
        if req.node_id is None:
            lib.node_id = None
        else:
            node = db.query(models.RemoteServer).filter(models.RemoteServer.id == req.node_id).first()
            if not node or node.kind != "ea":
                raise HTTPException(status_code=400, detail="只能把媒体库分配给一台后端服（EA）")
            if node.realm_id and lib.realm_id and node.realm_id != lib.realm_id:
                raise HTTPException(status_code=400, detail="这台节点属于另一个服，不能分配本服的媒体库")
            lib.node_id = node.id
    db.commit()
    # 配置变更后需重新触发扫描才生效：扫描任务使用固定配置快照，
    # 所以旧路径不会被正在跑的任务继续扫描，新路径也不会被旧快照漏掉
    return {"success": True, "rescan_required": True}


@admin_emby_router.delete("/libraries/{lib_id}")
def delete_library(lib_id: int, staff: models.WebUser = Depends(require_staff), db: Session = Depends(get_db)):
    lib = db.query(em.Library).filter(em.Library.id == lib_id).first()
    if not lib:
        raise HTTPException(status_code=404, detail="媒体库不存在")
    # 扫描中的库先不删：边扫边删会给已删库继续插条目，而 SQLite 会复用 rowid，
    # 新建一个库撞上同一个 id 时那些孤儿条目会「复活」。
    # 排队中的也算（v2.27.0）：等它排到时会发现库没了——不如在删的时候就说清楚。
    if is_scan_active(lib.id):
        raise HTTPException(status_code=409, detail="该媒体库正在扫描，请等扫描结束后再删除")
    if scan_queue.is_busy(lib.id):
        raise HTTPException(status_code=409, detail="该媒体库在扫描队列中，请先取消排队再删除")

    # 先断开自引用（parent_id / series_id）：分批删时父条目可能先于子条目被删，
    # 自引用外键没有级联会直接 500。整个库都要删掉，置空没有任何副作用。
    db.query(em.MediaItem).filter(
        em.MediaItem.library_id == lib.id
    ).update(
        {em.MediaItem.parent_id: None, em.MediaItem.series_id: None},
        synchronize_session=False,
    )
    db.commit()

    # 分批删除：老实现把整库条目一次载入内存再逐条删（十万级库会直接把面板拖死）
    # 删除顺序铁律：所有外键指向 emby_items 的子表（且无 ON DELETE CASCADE）
    # 都必须先于条目本身删除，漏掉任何一张都会 500
    # （2026-10-02：ItemFacet / PlaybackSession 漏删致删库 500；LocalCacheEntry 已随本地缓存全删移除）。
    removed = 0
    # include_deleted（v2.48.0 软删除）：删库是物理删除，必须把**已下架**的行也算进来，
    # 否则它们会被可见性过滤挡在分批循环外，变成一批指向已删媒体的孤儿行。
    with soft_delete.include_deleted():
        while True:
            chunk = [
                row[0] for row in db.query(em.MediaItem.id)
                .filter(em.MediaItem.library_id == lib.id)
                .limit(500)
                .all()
            ]
            if not chunk:
                break
            db.query(em.MediaStream).filter(
                em.MediaStream.item_id.in_(chunk)
            ).delete(synchronize_session=False)
            db.query(em.UserMediaData).filter(
                em.UserMediaData.item_id.in_(chunk)
            ).delete(synchronize_session=False)
            db.query(em.ItemFacet).filter(
                em.ItemFacet.item_id.in_(chunk)
            ).delete(synchronize_session=False)
            db.query(em.PlaybackSession).filter(
                em.PlaybackSession.item_id.in_(chunk)
            ).delete(synchronize_session=False)
            db.query(em.MediaItem).filter(
                em.MediaItem.id.in_(chunk)
            ).delete(synchronize_session=False)
            db.commit()
            removed += len(chunk)
    old_cover = lib.cover_path
    db.delete(lib)
    db.commit()
    _remove_library_cover(old_cover)
    return {"success": True, "items_removed": removed}


@admin_emby_router.get("/libraries/{lib_id}/scans")
def list_library_scans(lib_id: int, limit: int = 20,
                       staff: models.WebUser = Depends(require_staff), db: Session = Depends(get_db)):
    """某个媒体库最近的扫描流水（新的在前）

    只看「最近一次」分不出「这个库每轮都失败」和「只是最近一轮失败」——
    流水里带状态、触发方、耗时与同一份统计，失败原因也在。
    """
    lib = db.query(em.Library).filter(em.Library.id == lib_id).first()
    if not lib:
        raise HTTPException(status_code=404, detail="媒体库不存在")
    return {
        "library_id": lib_id,
        "keep": SCAN_RUN_KEEP,
        "runs": scan_runs_payload(db, lib_id, limit=limit),
    }


@admin_emby_router.post("/libraries/{lib_id}/scan")
async def scan_library_endpoint(lib_id: int, full: bool = False,
                                staff: models.WebUser = Depends(require_staff),
                                db: Session = Depends(get_db)):
    """触发一轮扫描。``full=true`` = 全量扫描（无视所有指纹，完整处理一遍）

    全量只影响**这一轮**（打在扫描快照上），不会把这个库或别的库改成“以后都全量”。
    归属到别的节点的库仍然转发过去，全量标记跟着一起过去。
    """
    def _enqueue() -> dict:
        """查库 + 归属节点判定 + 入队：整段同步 SQLAlchemy，下放线程池执行

        只有「转发给归属节点」这一步要 await 网络（push_scan），所以它留在事件循环上。
        """
        lib = db.query(em.Library).filter(em.Library.id == lib_id).first()
        if not lib:
            raise HTTPException(status_code=404, detail="媒体库不存在")

        # 已分配给某台节点的库，只有那台机器碰得到文件（本机路径 / rclone / 挂载）——
        # 由面板本地扫描只会得到一堆 failed_roots，所以转发过去让归属节点扫。
        owner = node_lib.library_owner(db, lib)
        if owner is not None and owner.id != node_lib.self_node_id(db):
            return {"forward_to": {"id": owner.id, "name": owner.name, "url": owner.url,
                                   "force_full": bool(full)}}

        # 入队而不是直接起线程（v2.27.0）：不同媒体库引用同一个远程挂载时排队跑，
        # 不让四个任务同时打同一个 WebDAV；重复点击不报 409，直接告诉你「已经在队列/正在扫」。
        # 后台线程用独立 Session（请求结束时请求级 Session 会被关闭，复用会导致
        # "transaction is closed" 与 SQLite 写锁冲突）——那一层现在在 scan_queue 里。
        result = scan_queue.enqueue(lib, trigger="manual", force_full=bool(full))
        task = result["task"]
        if not result["created"]:
            return {"response": {
                "success": True, "queued": False, "already": True, "state": task["state"],
                "message": ("该媒体库正在扫描中" if task["state"] == "running"
                            else f"该媒体库已在扫描队列中（第 {task.get('position') or '-'} 位）"),
                "task": task}}
        if task["state"] == "running":
            # 没被任何东西挡住：入队即开扫（就地派发），如实说「已启动」而不是「排队第 1 位」
            return {"response": {
                "success": True, "queued": True, "already": False, "started": True,
                "task": task, "message": "扫描已启动"}}
        waiting = [mount_lib.mount_label(m)
                   for m in _waiting_mount_objects(db, task.get("waiting_for"))]
        return {"response": {
            "success": True, "queued": True, "already": False, "started": False, "task": task,
            "message": (f"已加入扫描队列（第 {task.get('position') or '-'} 位）"
                        + (f"，正在等挂载：{'、'.join(waiting)}" if waiting
                           else "，前面还有扫描在跑")),
        }}

    prepared = await run_in_threadpool(_enqueue)

    target = prepared.get("forward_to")
    if target:
        # 全量标记跟着转发：不然点「全量扫描」在多节点部署下会被默默降成普通增量
        forward = await node_lib.push_scan(target["url"], lib_id,
                                          full=bool(target.get("force_full")))
        if not forward.get("ok"):
            raise HTTPException(
                status_code=502,
                detail=f"这个库归「{target['name']}」扫描，但转发失败了：{forward.get('error')}"
                "（请检查该节点的地址、两端 SECRET_KEY / NODE_SHARED_SECRET 是否一致，节点是否已升级）",
            )
        return {"success": True, "message": f"已让节点「{target['name']}」开始扫描",
                "forwarded_to": target, "library_id": lib_id}
    return prepared["response"]


def _waiting_mount_objects(db: Session, mount_ids) -> list:
    """把「在等哪些挂载」变成挂载对象（消息里要写得出名字，而不是只给 id）"""
    ids = [int(m) for m in (mount_ids or [])]
    if not ids:
        return []
    return db.query(em.StorageMount).filter(em.StorageMount.id.in_(ids)).all()


@admin_emby_router.post("/scan/all")
async def scan_all_libraries_endpoint(
    staff: models.WebUser = Depends(require_staff),
    db: Session = Depends(get_db),
):
    """一键扫描全部：把所有启用的库按顺序加入扫描队列（增量扫描）。

    新用户挂载后点这个，不用一个个点 11 次。
    正在扫的库会自动跳过，不会重复加入。
    """
    def _plan():
        # server_ops.scan_plan 会算清哪些入队、哪些已在队列、哪些跳过
        # 必须传真实本机节点：scan_plan 按 server.id 匹配 Library.node_id，
        # 传 None 会让 node_id=1 的库一个都匹配不上（一键扫描返回 0 个库）
        from backend.emby_server import server_ops as _so
        from backend.emby_server import nodes as node_lib
        server = node_lib.self_node(db)
        # include_unassigned=True：node_id 为空的库（后台新建库默认不选节点，例如
        # 新加的 .strm 直链库）由面板自己扫——单库「扫描」与定时扫描一直这么做，
        # 一键扫描漏掉它们就会出现「单独点能扫、一键扫描扫不到」。
        return _so.scan_plan(db, server, include_unassigned=True)

    result = await run_in_threadpool(_plan)
    queued = result.get("queued", [])
    already = result.get("already", [])
    skipped = result.get("skipped", [])

    # 归别的节点的库：scan_plan 只计划不执行，转发是网络调用，在这里 await
    forwarded: list[dict] = []
    failed: list[dict] = []
    for entry in result.get("forward", []) or []:
        res = await node_lib.push_scan(entry["url"], entry["id"])
        row = {"id": entry["id"], "name": entry["name"], "node_name": entry.get("node_name")}
        if res.get("ok"):
            forwarded.append(row)
        else:
            failed.append({**row, "error": str(res.get("error") or "转发失败")[:200]})

    return {
        "success": True,
        "queued_count": len(queued),
        "already_count": len(already),
        "skipped_count": len(skipped),
        "forwarded_count": len(forwarded),
        "queued": queued,
        "already": already,
        "skipped": skipped,
        "forwarded": forwarded,
        "failed": failed,
        "message": f"已加入 {len(queued)} 个库的扫描队列"
        + (f"，{len(forwarded)} 个已转发给归属节点" if forwarded else "")
        + (f"，{len(failed)} 个转发失败" if failed else "")
        + (f"，{len(already)} 个正在扫/已在队列（已跳过）" if already else "")
        + (f"，{len(skipped)} 个已停用/虚拟（已跳过）" if skipped else ""),
    }


@admin_emby_router.get("/fs-watch/status")
def fs_watch_status(staff: models.WebUser = Depends(require_staff)):
    """本机目录实时监听的状态（设置页 / Dashboard 回显用）

    单独一个只读端点而不是拼进 scan-queue：它是**另一个线程**的健康状况，
    且降级原因（路径不可读 / inotify 实例耗尽）必须让人看得见，
    否则就是默默不工作。
    """
    from backend.emby_server import fs_watcher

    return fs_watcher.watcher_status()


@admin_emby_router.get("/scan-queue")
def scan_queue_snapshot(staff: models.WebUser = Depends(require_staff), db: Session = Depends(get_db)):
    """扫描队列快照：正在跑的、排队等着的、最近完成的，以及远程 IO 计数

    为什么需要它：串联化之后「点了扫描却没动」变成了一件正常的事（在排队），
    没有这个面板就只能靠日志猜「到底在等谁」。
    """
    data = scan_queue.snapshot()
    names = {
        mount.id: mount_lib.mount_label(mount)
        for mount in _waiting_mount_objects(db, data.get("mount_owners", {}).keys())
    }
    # 排队中的任务再带上「在等哪个挂载」的名字：面板要能直接写出名字，
    # 否则管理员只能看到一串 id，还得去存储来源页对号
    waiting_ids = {
        int(m) for task in data.get("waiting", []) for m in (task.get("waiting_for") or [])
    } - set(names)
    if waiting_ids:
        names.update({mount.id: mount_lib.mount_label(mount)
                      for mount in _waiting_mount_objects(db, waiting_ids)})
    data["mount_names"] = {str(key): value for key, value in names.items()}
    return data


@admin_emby_router.delete("/scan-queue/{lib_id}")
def cancel_queued_scan(lib_id: int, staff: models.WebUser = Depends(require_staff)):
    """取消一个**还在排队**的扫描（正在跑的不能取消：停了会留下半个库的状态）

    写操作审计（v2.30.0）由 `emby_server/audit.py` 的中间件统一记录，
    这里不再各自写一行 `_audit(...)`：端点会新增，而「记得补审计」不是一种机制。
    """
    outcome = scan_queue.cancel(lib_id)
    if outcome == "running":
        raise HTTPException(status_code=409, detail="该媒体库正在扫描中，无法取消（请等它跑完）")
    if outcome == "missing":
        raise HTTPException(status_code=404, detail="该媒体库不在扫描队列里")
    return {"success": True, "library_id": lib_id}


@admin_emby_router.post("/libraries/virtual")
def generate_virtual_libraries(
    req: VirtualLibraryRequest,
    staff: models.WebUser = Depends(require_staff),
    db: Session = Depends(get_db),
):
    """按发行平台自动生成虚拟媒体库（Netflix / Disney+ / Apple TV+ …）

    虚拟媒体库没有自己的目录，它是跨库的“平台视图”：条目仍归属原媒体库，
    打开虚拟库时按 `MediaItem.platforms` 里的平台标签聚合。
    """
    requested = req.platforms
    if requested:
        unknown = [p for p in requested if p not in PLATFORM_LABELS]
        if unknown:
            raise HTTPException(status_code=400, detail=f"未知平台: {', '.join(unknown)}")
        targets = list(dict.fromkeys(requested))
    else:
        # 库里实际出现过的平台标签：走分类关联表的索引（旧实现要扫一遍全库 platforms 列）
        targets = sorted(facets.kind_values(db, facets.KIND_PLATFORM))

    created: list[dict] = []
    updated: list[dict] = []
    for platform in targets:
        lib = (
            db.query(em.Library)
            .filter(em.Library.is_virtual == True, em.Library.platform == platform)  # noqa: E712
            .first()
        )
        label = PLATFORM_LABELS.get(platform, platform)
        if not lib:
            import uuid

            lib = em.Library(
                guid=uuid.uuid4().hex[:32], name=label, collection_type="mixed",
                paths="", is_enabled=req.enabled, is_virtual=True, platform=platform,
                scrape_policy="missing_only",
            )
            db.add(lib)
            db.commit()
            db.refresh(lib)
            created.append({"id": lib.id, "platform": platform, "name": label})
        else:
            lib.is_enabled = req.enabled
            lib.name = lib.name or label
            db.commit()
            updated.append({"id": lib.id, "platform": platform, "name": lib.name})
        lib.item_count = count_virtual_items(db, lib)
        db.commit()

    pruned: list[dict] = []
    if req.prune:
        for lib in db.query(em.Library).filter(em.Library.is_virtual == True).all():  # noqa: E712
            if count_virtual_items(db, lib) == 0:
                pruned.append({"id": lib.id, "platform": lib.platform, "name": lib.name})
                db.delete(lib)
        db.commit()

    return {
        "success": True,
        "created": created,
        "updated": updated,
        "pruned": pruned,
        "available_platforms": [
            {"platform": p, "name": n} for p, n in sorted(PLATFORM_LABELS.items())
        ],
    }


@admin_emby_router.get("/libraries/repair/queue")
def repair_queue(staff: models.WebUser = Depends(require_staff), db: Session = Depends(get_db)):
    """待修复条目：数据库里有图片记录但取不到图（本地文件丢失 / 远程图失效）"""
    rows = (
        db.query(em.MediaItem)
        .filter(em.MediaItem.repair_requested_at.isnot(None))
        .order_by(em.MediaItem.repair_requested_at.desc())
        .limit(200)
        .all()
    )
    return {
        "total": soft_delete.count_visible(db,
                                            em.MediaItem.repair_requested_at.isnot(None)),
        "items": [
            {"id": r.guid, "name": r.name, "type": r.item_type, "library_id": r.library_id,
             "requested_at": r.repair_requested_at.isoformat() if r.repair_requested_at else None,
             "file_exists": bool(r.file_path and os.path.isfile(r.file_path))}
            for r in rows
        ],
    }


@admin_emby_router.post("/libraries/repair/run")
def run_repair_queue(staff: models.WebUser = Depends(require_staff), db: Session = Depends(get_db)):
    """立即处理修复队列（重新刮削取图）；不传 library_ids 则处理全部启用库"""
    # 走扫描队列（v2.27.0）：修复也要按远程挂载串行化，不然「一键修复」会把前面那批
    # 刚从点击开始跑的库一起推到 WebDAV 上。已在队列/正在扫的库会自动合并，不重复入队。
    libs = [
        lib for lib in db.query(em.Library).filter(em.Library.is_enabled == True).all()  # noqa: E712
    ]
    queued: list[int] = []
    already: list[int] = []
    for lib in libs:
        result = scan_queue.enqueue(lib, trigger="repair")
        (queued if result["created"] else already).append(lib.id)
    return {"success": True, "libraries": queued, "already": already}


@admin_emby_router.get("/reachability")
def reachability_report(staff: models.WebUser = Depends(require_staff),
                        db: Session = Depends(get_db), realm_id: int | None = None):
    """播放可达性报告：出流方式 + 逐库判定 + 用户端地址一致性

    回答的唯一问题是「控制面扫描没问题，数据面（出流的机器）能不能拿到这些内容」。
    分离部署（EM 只跑面板、EA 出流、媒体放共享 WebDAV/rclone）时这是最该先看的一页。
    """
    scope_id = None if realm_id == 0 else (realm_id or realms.active_realm_id(db))
    return reachability.summary(db, scope_id)


@admin_emby_router.get("/libraries/{lib_id}/scan-live")
def library_scan_live(lib_id: int, staff: models.WebUser = Depends(require_staff),
                      db: Session = Depends(get_db)):
    """单个媒体库的实时扫描状态（排队/进度）；空闲返回 204

    列表接口已经带了这个字段，这个端点只是给「盯着一个库看」的页面（轮询间隔更短）。
    """
    lib = db.query(em.Library).filter(em.Library.id == lib_id).first()
    if not lib:
        raise HTTPException(status_code=404, detail="媒体库不存在")
    live = scan_queue.live_payload(lib)
    if live is None:
        return Response(status_code=204)
    return live


@admin_emby_router.get("/items")
def admin_search_items(staff: models.WebUser = Depends(require_staff), db: Session = Depends(get_db),
                             search: str = "", type: str = "", limit: int = 50, offset: int = 0):
    query = db.query(em.MediaItem)
    if search:
        query = query.filter(em.MediaItem.name.ilike(f"%{search}%"))
    if type:
        query = query.filter(em.MediaItem.item_type == type)
    total = query.count()
    items = query.order_by(em.MediaItem.date_added.desc()).offset(offset).limit(limit).all()
    return {"total": total, "items": [
        {"id": i.guid, "name": i.name, "type": i.item_type, "year": i.production_year,
         "library_id": i.library_id, "file_path": i.file_path, "size": i.size,
         "duration_ticks": i.duration_ticks,
         "added_at": i.date_added.isoformat() if i.date_added else None}
        for i in items
    ]}


@admin_emby_router.delete("/items/{item_id}")
def admin_delete_item(item_id: str, staff: models.WebUser = Depends(require_staff), db: Session = Depends(get_db)):
    item = db.query(em.MediaItem).filter(em.MediaItem.guid == item_id).first()
    if not item:
        raise HTTPException(status_code=404, detail="条目不存在")
    db.query(em.MediaStream).filter(em.MediaStream.item_id == item.id).delete()
    db.query(em.UserMediaData).filter(em.UserMediaData.item_id == item.id).delete()
    db.delete(item)
    db.commit()
    return {"success": True}


@admin_emby_router.get("/sessions")
def admin_sessions(staff: models.WebUser = Depends(require_staff), db: Session = Depends(get_db)):
    sessions = (
        db.query(em.PlaybackSession, models.WebUser, em.MediaItem)
        .join(models.WebUser, models.WebUser.id == em.PlaybackSession.user_id)
        .join(em.MediaItem, em.MediaItem.id == em.PlaybackSession.item_id)
        .filter(em.PlaybackSession.ended_at.is_(None))
        .order_by(em.PlaybackSession.last_update_at.desc())
        .all()
    )
    return {"sessions": [
        {
            "session_key": s.session_key, "username": u.username,
            "item": i.name, "item_type": i.item_type,
            "device": s.device_name, "client": s.client_name,
            "remote_addr": s.remote_addr,
            "play_method": s.play_method,
            "position_ticks": s.position_ticks,
            "duration_ticks": i.duration_ticks,
            "is_paused": s.is_paused,
            "started_at": s.start_time.isoformat() if s.start_time else None,
        }
        for s, u, i in sessions
    ]}


@admin_emby_router.delete("/sessions/{session_key}")
async def admin_stop_session(session_key: str, staff: models.WebUser = Depends(require_staff), db: Session = Depends(get_db)):
    def _end_session() -> bool:
        """查会话 / 写结束时间（同步段下放线程池）"""
        session = db.query(em.PlaybackSession).filter(
            em.PlaybackSession.session_key == session_key
        ).first()
        if not session:
            return False
        session.ended_at = datetime.now()
        db.commit()
        return True

    await run_in_threadpool(_end_session)
    return {"success": True}


@admin_emby_router.post("/transcodes/stop-all")
async def admin_stop_all_transcodes(staff: models.WebUser = Depends(require_staff)):
    # 转码已删除（2026-10-17）：不再有转码会话。端点保留成功回包，
    # 兼容管理后台旧版本调用（删端点会让旧面板吃 404）。
    return {"success": True, "stopped": 0}


# ==================== 存储挂载（已拆出） ====================
# 端点实现在 backend/emby_server/portal_mount_routes.py，与本文件共用同一个 admin_emby_router。

# ==================== 115 账号与目录浏览 ====================


def _mask_cookie(cookie: str) -> str:
    """Cookie 不回传明文：只展示长度与尾部片段，便于管理员核对是哪一个"""
    value = (cookie or "").strip()
    if not value:
        return ""
    return f"…{value[-6:]}" if len(value) > 6 else "…"


def _serialize_account(account: em.Pan115Account) -> dict:
    return {
        "id": account.id,
        "name": account.name,
        "cookie_preview": _mask_cookie(account.cookie),
        "has_cookie": bool((account.cookie or "").strip()),
        "ua": account.ua or "",
        "is_default": bool(account.is_default),
        "is_enabled": bool(account.is_enabled),
        "remark": account.remark or "",
        "last_verified_at": account.last_verified_at.isoformat() if account.last_verified_at else None,
        "last_verify_ok": account.last_verify_ok,
        "last_verify_message": account.last_verify_message,
        "created_at": account.created_at.isoformat() if account.created_at else None,
    }


class Pan115AccountCreate(BaseModel):
    name: str
    cookie: str
    is_default: bool = False
    is_enabled: bool = True
    remark: str = ""
    # 该配置档发请求用的 UA（留空 = 服务器级 MOUNT_UA）
    ua: str = ""


class Pan115AccountUpdate(BaseModel):
    name: str | None = None
    cookie: str | None = None
    is_default: bool | None = None
    is_enabled: bool | None = None
    remark: str | None = None
    ua: str | None = None


class Pan115VerifyRequest(BaseModel):
    cookie: str


@admin_emby_router.get("/115/accounts")
def list_pan115_accounts(staff: models.WebUser = Depends(require_staff),
                               db: Session = Depends(get_db)):
    accounts = db.query(em.Pan115Account).order_by(em.Pan115Account.id).all()
    env_cookie = os.getenv(transfer115.PAN115_COOKIE_ENV, "").strip()
    return {
        "accounts": [_serialize_account(a) for a in accounts],
        "env_cookie_configured": bool(env_cookie),
        # UA 预置项由后端下发（115 直链接口对 UA 有偏好）
        "ua_presets": [dict(p) for p in mount_lib.UA_PRESETS],
    }


@admin_emby_router.post("/115/accounts")
def create_pan115_account(req: Pan115AccountCreate,
                                staff: models.WebUser = Depends(require_staff),
                                db: Session = Depends(get_db)):
    name = req.name.strip()
    if not name:
        raise HTTPException(status_code=400, detail="请填写账号名称")
    if not transfer115.normalize_cookie(req.cookie):
        raise HTTPException(status_code=400, detail="请填写 115 Cookie")
    if db.query(em.Pan115Account).filter(em.Pan115Account.name == name).first():
        raise HTTPException(status_code=400, detail=f"账号名称「{name}」已存在")
    account = em.Pan115Account(
        name=name, cookie=transfer115.normalize_cookie(req.cookie),
        is_default=req.is_default, is_enabled=req.is_enabled, remark=req.remark or "",
        ua=(req.ua or "").strip()[:300],
    )
    db.add(account)
    db.commit()
    db.refresh(account)
    if account.is_default:
        transfer115.ensure_single_default(db, account.id)
        db.commit()
    return {"success": True, "account": _serialize_account(account)}


@admin_emby_router.put("/115/accounts/{account_id}")
def update_pan115_account(account_id: int, req: Pan115AccountUpdate,
                                staff: models.WebUser = Depends(require_staff),
                                db: Session = Depends(get_db)):
    account = db.query(em.Pan115Account).filter(em.Pan115Account.id == account_id).first()
    if not account:
        raise HTTPException(status_code=404, detail="账号配置档不存在")
    if req.name is not None and req.name.strip() and req.name.strip() != account.name:
        name = req.name.strip()
        if db.query(em.Pan115Account).filter(em.Pan115Account.name == name).first():
            raise HTTPException(status_code=400, detail=f"账号名称「{name}」已存在")
        account.name = name
    if req.cookie is not None:
        cookie = transfer115.normalize_cookie(req.cookie)
        if not cookie:
            raise HTTPException(status_code=400, detail="Cookie 不能为空")
        account.cookie = cookie
        # Cookie 变了，上次校验结论作废
        account.last_verify_ok = None
        account.last_verify_message = None
    if req.is_enabled is not None:
        account.is_enabled = req.is_enabled
    if req.ua is not None:
        account.ua = req.ua.strip()[:300]
    if req.remark is not None:
        account.remark = req.remark
    if req.is_default:
        account.is_default = True
    elif req.is_default is False:
        account.is_default = False
    db.commit()
    if account.is_default:
        transfer115.ensure_single_default(db, account.id)
        db.commit()
    return {"success": True, "account": _serialize_account(account)}


@admin_emby_router.delete("/115/accounts/{account_id}")
def delete_pan115_account(account_id: int,
                                staff: models.WebUser = Depends(require_staff),
                                db: Session = Depends(get_db)):
    account = db.query(em.Pan115Account).filter(em.Pan115Account.id == account_id).first()
    if not account:
        raise HTTPException(status_code=404, detail="账号配置档不存在")
    # 媒体库绑定随之解除（回退默认账号），避免留下悬空引用
    for lib in db.query(em.Library).filter(em.Library.account_115_id == account_id).all():
        lib.account_115_id = None
    db.delete(account)
    db.commit()
    return {"success": True}


@admin_emby_router.post("/115/accounts/{account_id}/verify")
async def verify_pan115_account(account_id: int,
                                staff: models.WebUser = Depends(require_staff),
                                db: Session = Depends(get_db)):
    def load() -> tuple[em.Pan115Account | None, str]:
        """取配置档与 Cookie（同步；下放线程池——路由是 async，不能占事件循环）"""
        row = db.query(em.Pan115Account).filter(em.Pan115Account.id == account_id).first()
        return row, (row.cookie if row is not None else "")

    account, cookie = await run_in_threadpool(load)
    if not account:
        raise HTTPException(status_code=404, detail="账号配置档不存在")
    # 校验要真的发一次请求：放到线程池执行，避免阻塞整个事件循环
    result = await run_in_threadpool(transfer115.verify_account, cookie)

    def record() -> dict:
        """写回校验结果并序列化（同步；下放线程池）"""
        account.last_verified_at = datetime.now()
        account.last_verify_ok = bool(result.get("ok"))
        account.last_verify_message = str(
            result.get("message") or ("有效" if result.get("ok") else "")
        )[:300]
        db.commit()
        return _serialize_account(account)

    return {"success": True, "result": result,
            "account": await run_in_threadpool(record)}


@admin_emby_router.post("/115/verify")
async def verify_pan115_cookie(req: Pan115VerifyRequest,
                               staff: models.WebUser = Depends(require_staff)):
    """校验尚未保存的 Cookie（新建配置档前先确认可用）"""
    result = await run_in_threadpool(transfer115.verify_account, req.cookie)
    return {"success": True, "result": result}


@admin_emby_router.get("/115/browse")
async def browse_pan115(
    cid: str = "0",
    account_id: int | None = None,
    cookie: str = "",
    staff: models.WebUser = Depends(require_staff),
    db: Session = Depends(get_db),
):
    """浏览 115 目录（目标路径选择器）

    表单 Cookie 优先，其次账号配置档，最后已保存的默认账号 / 环境变量——
    这样"刚粘贴还没保存"的 Cookie 也能直接用来浏览路径。
    """
    resolved, source = transfer115.resolve_cookie(
        db, account_id=account_id, explicit_cookie=cookie or None,
    )
    if not resolved:
        raise HTTPException(status_code=400, detail="未配置 115 Cookie，请先在账号配置档里添加")
    try:
        entries = await run_in_threadpool(transfer115.Pan115Client(resolved).list_dir, cid or "0")
    except transfer115.Pan115AuthError as exc:
        raise HTTPException(status_code=401, detail=f"115 登录态失效：{exc}")
    except transfer115.Pan115Error as exc:
        raise HTTPException(status_code=502, detail=str(exc))
    return {
        "cid": cid or "0",
        "cookie_source": source,
        "entries": [e for e in entries if e.get("is_dir")],
        "total": len(entries),
    }




# ===== 公益服身份与资格（模块 1）=====

def is_welfare_user(db, user) -> bool:
    """判定用户是否为有效公益用户。

    有效公益用户需满足：is_welfare=True 且资格未过期
    （welfare_expires_at 为 NULL 视为永不过期）。
    """
    if user is None:
        return False
    if not getattr(user, 'is_welfare', False):
        return False
    expires_at = getattr(user, 'welfare_expires_at', None)
    if expires_at is None:
        return True
    return expires_at > datetime.now()


def grant_welfare(db, user, channel, days, granted_by=None):
    """开通或续期用户的公益资格。

    规则：
    - days=0 表示永不过期；
    - 已有未过期资格时，在原到期时间上累加天数；
    - 资格已过期（或从未开通）时，从当前时间开始计算；
    - 已是永不过期的用户再次开通有限天数，仍保持永不过期。

    并发修复：
    - 使用 Compare-And-Swap + 有界重试（最多 5 次）实现原子发放；
    - 每轮重新读取最新的 (is_welfare, welfare_expires_at)，避免并发丢失更新；
    - 若连续 5 次 CAS 失败则抛出 RuntimeError。
    """
    uid = user.id
    # R18-P3：类型归一——days 为真值非数字（如字符串 "-5"）时，旧 `days < 0`
    # 在 Python 3 会抛 TypeError 而非 ValueError。先做类型归一：数字字符串
    # 转 int，转不动则按非法输入拒绝。当前调用方（welfare_admin 的 ge=0 校验、
    # redeem_welfare 只传 7/30、单测传 -5 int）均无字符串调用路径，此为纵深防御。
    if isinstance(days, str):
        days = days.strip()
        try:
            days = int(days)
        except ValueError:
            raise ValueError("days 必须为数字")
    days = days or 0
    if not isinstance(days, (int, float)) or days < 0:
        raise ValueError("days 不能为负数")

    new_expires = None
    now = None

    for _ in range(5):
        now = datetime.now()

        row = (
            db.query(
                models.WebUser.is_welfare,
                models.WebUser.welfare_expires_at,
            )
            .filter(models.WebUser.id == uid)
            .one_or_none()
        )
        if row is None:
            raise RuntimeError(f"grant_welfare CAS failed for user {uid}: user not found")

        old_is_welfare_value = row[0]
        old_expires = row[1]
        old_is_welfare = bool(old_is_welfare_value)

        if days > 0:
            # 注意：welfare_expires_at 为 None 有两种互斥含义——
            #   1) 永不过期：用户已拥有公益资格（is_welfare 为真），只是没有到期时间；
            #   2) 从未开通：新建 WebUser 行的该列默认值就是 None，此时 is_welfare 为假。
            # 必须结合 is_welfare 标志区分，不能仅凭 expires_at 是否为 None 判断，
            # 否则从未开通的用户首次发放有限天数会被误判为永久。
            if old_is_welfare and old_expires is None:
                new_expires = None
            elif old_expires is not None and old_expires > now:
                new_expires = old_expires + timedelta(days=days)
            else:
                new_expires = now + timedelta(days=days)
        else:
            new_expires = None

        q = db.query(models.WebUser).filter(models.WebUser.id == uid)

        if old_is_welfare:
            q = q.filter(models.WebUser.is_welfare.is_(True))
        else:
            q = q.filter(models.WebUser.is_welfare.is_not(True))

        if old_expires is None:
            q = q.filter(models.WebUser.welfare_expires_at.is_(None))
        else:
            q = q.filter(models.WebUser.welfare_expires_at == old_expires)

        updated = q.update(
            {
                models.WebUser.welfare_expires_at: new_expires,
                models.WebUser.is_welfare: True,
                models.WebUser.welfare_grant_channel: channel,
                models.WebUser.welfare_granted_at: now,
            },
            synchronize_session=False,
        )

        if updated == 1:
            break
        # CAS 未命中：行被并发改了，重试前记一条 warning（5 次连败时可回溯）
        logger.warning("grant_welfare CAS 未命中 user=%s channel=%s（并发重试中）",
                       uid, channel)
    else:
        # 有界重试耗尽：回滚本事务（SELECT/UPDATE 均未提交），再把响亮的错
        # 抛给调用方——总比带着半开事务悄悄返回强。lottery 按 winner 捕获，
        # welfare_admin 会 500（极端并发才触发，可接受）。
        db.rollback()
        raise RuntimeError(f"grant_welfare CAS failed for user {uid} after 5 retries")

    db.add(
        models.WelfareGrantLog(
            user_id=uid,
            channel=channel,
            days=days,
            granted_by=granted_by,
        )
    )
    db.commit()

    user.is_welfare = True
    user.welfare_expires_at = new_expires
    user.welfare_grant_channel = channel
    user.welfare_granted_at = now

    return new_expires


def get_welfare_status(db, user) -> dict:
    """获取用户的公益资格状态。"""
    if user is None:
        return {'is_welfare': False, 'expires_at': None, 'days_left': 0, 'channel': None}
    now = datetime.now()
    expires_at = getattr(user, 'welfare_expires_at', None)
    if expires_at is None:
        days_left = None
    elif expires_at > now:
        days_left = (expires_at - now).days
    else:
        days_left = 0
    return {
        'is_welfare': is_welfare_user(db, user),
        'expires_at': expires_at,
        'days_left': days_left,
        'channel': getattr(user, 'welfare_grant_channel', None),
    }
