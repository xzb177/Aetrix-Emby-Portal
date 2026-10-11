"""
Aetrix Portal - 统一后端主入口
整合用户端和管理后台的所有 API，并托管用户前端（Vue SPA）静态资源
"""
from pathlib import Path

import os

from fastapi import FastAPI, Request, status, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from contextlib import asynccontextmanager
import logging
from datetime import datetime
from prometheus_client import make_asgi_app

from backend.metrics_guard import MetricsGuard
from backend.ratelimit import get_client_ip
from sqlalchemy import text

from backend.database import SessionLocal, engine, get_db, init_db, DATABASE_TYPE
from backend.security import validate_secret_key
from backend.version import app_version
from backend import models  # 导入所有模型
from backend.download_guard import DownloadGuardMiddleware
from backend.access_guard import AccessGuardMiddleware
from backend.emby_server.audit import AdminWriteAuditMiddleware
from backend.websocket import websocket_router, notification_router, manager
from backend.api import user_router, admin_router
from backend.api.emby_servers import router as emby_servers_router
from backend.api.admins_admin import router as admins_router
# 首次运行向导（setup_mode）：建第一个管理员，完成后入口永久关闭
from backend.api.setup import router as setup_router
from backend.api.playback_admin import router as playback_admin_router
from backend.api.library_cover import router as library_cover_router
from backend.api.home_summary import router as home_summary_router
from backend.api.homepage_sections import router as homepage_sections_router
from backend.api.realms import router as realms_router
from backend.api.servers import router as servers_router
from backend.emby_server.stream_nodes import admin_router as stream_nodes_router
from backend import realms
from backend.emby_server import nodes as node_lib
from backend.emby_server import maintenance
from backend import reminders
from backend.api.admin_ops import admin_ops_router
from backend.api.admin_services import services_router
from backend.api.reminders_admin import admin_reminders_router
from backend.api.orders_admin import admin_orders_router
from backend.api.coupons_admin import admin_coupons_router
# v2.19.0：外部服务能力中心（代理 / 人机验证 / 邮件 / Telegram / AI / IP 归属地）。
# 只提供能力，密钥一律由管理员自己填。
from backend.api.capabilities_admin import capabilities_router
from backend.api.assistant import assistant_router
# 站点品牌公开端点（前端启动时读一次，决定站名 / 主题色 / 标题）
from backend.api.site import site_router
from backend import integrations
from backend.emby_server.api import emby_router

# v2.15.0：分类关联表（筛选走索引）。导入即注册 ORM flush 钩子——
# 任何写 genres/studios/tags/platforms 的代码（扫描器、图片修复…）都会在同一个事务里
# 把关联行同步好，不需要每个调用点各自记得调一次。
from backend.emby_server import facets  # noqa: F401

# v2.13.0：api.py 拆分出来的协议路由模块。导入即把路由注册到同一个 emby_router 上，
# 这里的顺序（media → compat → stream）与拆分前的定义顺序一致，不能调换。
from backend.emby_server import media_routes  # noqa: F401
from backend.emby_server import compat_routes  # noqa: F401
from backend.emby_server import stream_routes  # noqa: F401
from backend.emby_server.image_routes import install_image_routes
from backend.emby_server.mount_routes import install_mount_routes
from backend.emby_server.session_routes import install_session_routes
from backend.emby_server.search_api import search_router
from backend.emby_server.portal import user_emby_router, admin_emby_router, configured_emby_url
# 存储挂载端点已从 portal.py 拆出（portal.py 尾部超出编辑窗口）：导入即注册到同一个
# admin_emby_router 上，因此必须放在 app.include_router(admin_emby_router) 之前。
from backend.emby_server import portal_mount_routes  # noqa: F401
# .strm 生成器管理端点：导入即把 5 个路由（config/trigger/progress/missing）注册到
# 同一个 admin_emby_router 上，必须放在 app.include_router(admin_emby_router) 之前。
from backend.api import admin_strm_gen  # noqa: F401
from backend.api.emby_portal import auth_router
from backend.api.economy import router as economy_router
from backend.api.invitation import router as invitation_router
from backend.api.points import router as welfare_points_router
from backend.api.welfare_requests import router as welfare_requests_router
from backend.api.welfare_reviews import router as welfare_reviews_router
from backend.api.tg_bind import router as tg_bind_router
from backend.api.lottery import router as lottery_router

# 配置日志
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """应用生命周期管理"""
    # 启动时
    logger.info("🚀 Aetrix Portal 正在启动...")
    # 后端拆分（v2.41.0）：AETRIX_ROLE 环境变量
    # - "api"：只跑 HTTP，不启动任何后台任务（后台任务由 aetrix-worker 进程负责）
    # - "worker"：不应走 main.py（用 backend/worker.py），这里做保护性提示
    # - 未设置：单体模式，保持原有行为（向后兼容）
    _role = os.getenv("AETRIX_ROLE", "").strip().lower()
    _is_api_role = (_role == "api")
    if _role == "worker":
        logger.warning("AETRIX_ROLE=worker 但走了 main.py 入口：后台任务不会在这里启动，请用 backend/worker.py")
    if _is_api_role:
        logger.info("AETRIX_ROLE=api：API 模式，后台任务由 worker 进程负责，这里跳过启动")

    logger.info(f"📊 数据库类型: {DATABASE_TYPE}")
    # Hardware transcode detection (Linger): one-frame self-test at startup
    try:
        from backend.emby_server import hwaccel as _hwaccel
        _hwaccel.detect_hardware()
    except Exception as e:
        logger.warning(f"HW transcode check failed, using software: {e}")

    # JWT 密钥是 EM/EA 认证与节点配对的根信任。未配置或过短时必须 fail-closed，
    # 不能让服务用导入期随机值带病上线。
    validate_secret_key()

    # 初始化数据库。schema 不完整时必须拒绝启动，不能把半可用服务暴露给用户。
    try:
        init_db()
    except Exception:
        logger.exception("数据库初始化失败，服务拒绝启动")
        raise

    # 配置自愈（backend/config_self_heal.py，借鉴 twilight-kotomi）：
    # env.example / 代码默认值 → .env / system_configs，只补缺失、不覆盖；
    # 失败只记日志，不阻断启动。
    try:
        from backend import config_self_heal
        with SessionLocal() as db:
            config_self_heal.run_config_self_heal(db)
    except Exception as e:  # noqa: BLE001
        logger.warning(f"配置自愈失败（可忽略）: {e}")

    # 内容可见性：面板自带的网关按「服」过滤（默认服 → 与单服部署完全一致）。
    # 多服部署时各服的 EA 各自提供本服内容，这里只保证面板不会把别的服的内容也端出去。
    try:
        node_lib.install_scope("em")
    except Exception as e:  # noqa: BLE001 — 过滤装不上也不能阻止面板启动
        logger.warning(f"内容可见性未生效（按不过滤处理）: {e}")

    # 崩溃残留的收尾 + 长期运行的后台维护（扫描标志 / 过期会话 / 转码目录 / 字幕缓存），
    # 见 backend/emby_server/maintenance.py。维护失败不影响启动。
    if not _is_api_role:
        try:
            maintenance.run_startup_maintenance()
            maintenance.start_janitor()
        except Exception as e:  # noqa: BLE001
            logger.warning(f"启动维护失败（可忽略）: {e}")


        # 分层扫描 L2/L3（v2.40.0）：SCAN_LAYERED=1 时启动后台补全 worker，
        # 把 L1 落下的待补全条目（side 图片/NFO/TMDB）慢慢补完。失败不影响启动。
    if not _is_api_role:
        try:
            from backend.emby_server import enrich_worker
            enrich_worker.start()
            logger.info("后台补全 worker 已启动（分层扫描 L2/L3）")
        except Exception as e:  # noqa: BLE001
            logger.warning(f"启动补全 worker 失败（可忽略）: {e}")

        # 媒体信息探测（M11）：以前只有 backend/worker.py 起它，单体部署（python serve.py、
        # 未设 AETRIX_ROLE）从来没有探测 worker —— pending 只增不减。
        # 与 worker.py 共用同一个幂等入口 probe_worker.start()。
    if not _is_api_role:
        try:
            from backend.emby_server import probe_worker
            if probe_worker.start():
                logger.info("媒体信息探测 worker 已启动")
        except Exception as e:  # noqa: BLE001
            logger.warning(f"启动探测 worker 失败（可忽略）: {e}")

        # 订阅到期提醒：会员到期前按 7/3/1 天提前通知（否则只能等用户自己想起来续费）。
        # 与维护一样是后台线程，失败不影响启动；EA 侧不启动（见 backend/reminders.py）。
    if not _is_api_role:
        try:
            reminders.start_reminder_scheduler()
        except Exception as e:  # noqa: BLE001
            logger.warning(f"启动到期提醒失败（可忽略）: {e}")

        # 定时扫描：用户在后台「元数据与刮削」里自己决定开不开、几点扫；
        # 默认关闭，不打扰任何现有行为。失败不影响启动。
    if not _is_api_role:
        try:
            from backend.emby_server import auto_scan
            auto_scan.start_auto_scan_scheduler()
        except Exception as e:  # noqa: BLE001
            logger.warning(f"启动定时扫描调度失败（可忽略）: {e}")
        # 数据库定时备份：默认开启（每天 03:00，保留 7 天），后台可改；失败不影响启动。
    if not _is_api_role:
        try:
            from backend.emby_server import db_backup
            db_backup.start_backup_scheduler()
        except Exception as e:  # noqa: BLE001
            logger.warning(f"启动数据库备份调度失败（可忽略）: {e}")
        # 公益服到期检查：每天一次，处理过期保留和不活跃用户；失败不影响启动。
    if not _is_api_role:
        try:
            from backend import welfare_expiry
            welfare_expiry.start_welfare_expiry_scheduler()
        except Exception as e:  # noqa: BLE001
            logger.warning(f"启动公益到期检查调度失败（可忽略）: {e}")
        # 追新：默认关闭，不打扰任何现有行为。失败不影响启动。
    if not _is_api_role:
        try:
            from backend.emby_server import change_watcher
            change_watcher.start_chase_new_watcher()
        except Exception as e:  # noqa: BLE001
            logger.warning(f"启动追新线程失败（可忽略）: {e}")

        # 本机目录实时监听（v2.44.0）：容器重启后自动重建，路径从库里读、不写死。
        # 失败/不可用就降级为定时扫描，不影响启动，也不影响扫描与播放。
        try:
            from backend.emby_server import fs_watcher
            if fs_watcher.start_fs_watcher():
                logger.info("本机目录实时监听已启动（新片入库会自动触发增量扫描）")
            else:
                logger.info("本机目录实时监听未启用，已降级为定时扫描")
        except Exception as e:  # noqa: BLE001
            logger.warning(f"启动目录监听失败（已降级为定时扫描）: {e}")

        # VPS 本地缓存（播放线路 cache）：默认关闭；开启后把热门片串行、限速地
        # 拉到本机（播放中自动让路），超配额按 LRU 淘汰。失败不影响启动。
    if not _is_api_role:
        try:
            from backend.emby_server import local_cache_worker
            local_cache_worker.start_local_cache_worker()
            logger.info("本地缓存 worker 已启动（默认关闭，后台可开）")
        except Exception as e:  # noqa: BLE001
            logger.warning(f"启动本地缓存 worker 失败（可忽略）: {e}")

        # 外部服务能力落地：管理员配过的出站代理要写回进程环境变量（否则重启后失效），
        # 邮件 / Telegram 通知渠道也在这里按最新配置重建。没配过的能力什么都不做。
    try:
        with SessionLocal() as db:
            integrations.apply_all(db)
    except Exception as e:  # noqa: BLE001 — 能力落地失败不能拦住启动
        logger.warning(f"外部服务能力未落地（可忽略）: {e}")

    # 节点健康检查：后台进程探测流媒体节点，摘除不健康的（2026-10-09）
    try:
        from backend.emby_server import node_health
        if _is_api_role:
            node_health.start_health_daemon(SessionLocal)
    except Exception as e:  # noqa: BLE001
        logger.warning(f"节点健康检查启动失败（可忽略）: {e}")

    # .strm 生成器定时调度：必须在 API 进程启动（只有 aetrix-api 的 /strm 是 rw
    # 挂载，worker/ea 都是 ro 写不进去）。这里不按 _is_api_role 区分——单体模式
    # （未设 AETRIX_ROLE）同样走 main.py，调度器要在两种模式下都跑起来；
    # worker 进程用 backend/worker.py 入口，根本不走这里，不会重复启动。
    # 调度器内部按 strm_gen_enabled / strm_gen_schedule 配置决定是否真正执行。
    try:
        from backend.emby_server import strm_gen
        if strm_gen.start_scheduler():
            logger.info(".strm 生成器定时调度已启动")
    except Exception as e:  # noqa: BLE001
        logger.warning(f".strm 生成器调度启动失败（可忽略）: {e}")

    logger.info("✅ Aetrix Portal 启动完成")

    yield

    # 关闭时：先收掉子进程与临时文件，避免 ffmpeg 变成孤儿继续吃 CPU/磁盘
    logger.info("👋 Aetrix Portal 正在关闭...")
    try:
        maintenance.shutdown_cleanup()
    except Exception as e:  # noqa: BLE001
        logger.warning(f"退出收尾失败（可忽略）: {e}")
    # 探测 worker：已抢未处理的放回 pending（API 角色下它没启动，stop 是空操作）
    try:
        from backend.emby_server import probe_worker as _pw
        _pw.stop(timeout=2.0)
    except Exception as e:  # noqa: BLE001
        logger.warning(f"停止探测 worker 失败（可忽略）: {e}")
    # 封面异步重生成线程（v2.48.0）：它是惰性起的，但排到队还没画完就退出会丢任务
    try:
        from backend.api.library_cover import stop_cover_worker
        stop_cover_worker(timeout=5.0)
    except Exception as e:  # noqa: BLE001
        logger.warning(f"停止封面 worker 失败（可忽略）: {e}")
    # 关掉中转代理的共享连接池（秒播那套）。不关的话 httpx 会报未关闭的
    # 客户端；而且它绑定在创建它的事件循环上，换循环后复用会直接炸。
    try:
        from backend.emby_server.streaming import close_relay_client

        await close_relay_client()
    except Exception as e:  # noqa: BLE001
        logger.warning(f"关闭中转连接池失败（可忽略）: {e}")


# 创建 FastAPI 应用
app = FastAPI(
    title="Aetrix Portal",
    description="Aetrix 统一门户 API",
    # 版本号单一来源：根目录 VERSION（改版本只改那一个文件）
    version=app_version(),
    docs_url="/api/docs",
    redoc_url="/api/redoc",
    openapi_url="/api/openapi.json",
    lifespan=lifespan,
)


# ==================== 中间件配置 ====================

# CORS 中间件：CORS_ORIGINS 为空时不添加（同源不需要），绝不回退到 ["*"]
_cors_origins_env = os.getenv("CORS_ORIGINS", "").strip()
_cors_origins = [o.strip() for o in _cors_origins_env.split(",") if o.strip()]
if _cors_origins:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=_cors_origins,
        allow_credentials=True,
        allow_methods=["GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS"],
        allow_headers=["Content-Type", "Authorization", "X-Emby-Authorization", "X-Emby-Token",
                       "X-MediaBrowser-Token", "X-Device-Id"],
        expose_headers=["Content-Range", "Accept-Ranges", "Content-Length"],
    )


# ============ API 限流（Redis 固定窗口）============
# 保护 API 不被滥用；健康检查不限流；认证用户限额更高
# 注意：这是固定窗口（每分钟重置），不是滑动窗口
RATE_LIMITS = [
    # (路径前缀, 未认证限额/分钟, 认证用户限额/分钟)
    ("/api/health", 0, 0),              # 健康检查：不限流
    ("/emby/Users/AuthenticateByName", 15, 15),  # Emby 客户端登录：每分钟 15 次/IP（防暴力破解，对标 go-emby）
    ("/api/admin/emby/login", 10, 10),  # 登录：每分钟 10 次（防暴力破解）
    ("/api/user/login", 10, 10),
    ("/api/admin/", 60, 300),           # 管理接口：未认证 60，认证 300
    ("/api/", 120, 600),               # 普通 API：未认证 120，认证 600
]

def _get_client_ip(request) -> str:
    """获取真实客户端 IP（防伪造）。

    已统一使用 backend.ratelimit.get_client_ip：
    只有直连方是可信代理（TRUSTED_PROXIES）时才信任 XFF/X-Real-IP，
    否则用直连 IP，防止伪造头绕过限流。
    保留此函数名做兼容。
    """
    return get_client_ip(request)

def _is_authenticated(request) -> bool:
    """检查是否有认证头（简单判断，不验证有效性）"""
    auth = request.headers.get("authorization", "")
    token = request.headers.get("x-emby-token", "") or request.headers.get("x-mediabrowser-token", "")
    return bool(auth or token)

def _check_rate_limit(ip: str, path: str, authenticated: bool) -> tuple[bool, str]:
    """返回 (是否允许, 原因)"""
    for prefix, limit_anon, limit_auth in RATE_LIMITS:
        if path.startswith(prefix):
            limit = limit_auth if authenticated else limit_anon
            if limit == 0:
                return True, ""
            try:
                from backend import database as db
                import time

                window = int(time.time() // 60)
                # 区分认证/未认证的 key，避免互相影响
                auth_tag = "auth" if authenticated else "anon"
                key = f"ratelimit:{ip}:{prefix}:{auth_tag}:{window}"
                # S4：Redis 正常走 Redis；运行期故障 / 熔断中改用进程内计数（不再每请求
                # 卡满 socket 超时）；未启用 Redis 时返回 None = 不限流（与升级前一致）
                count = db.rate_limit_incr(key, 70)  # 窗口 60 秒 + 10 秒缓冲
                if count is not None and count > limit:
                    return False, f"每分钟最多 {limit} 次"
            except Exception:
                return True, ""  # 异常时不限流（降级）
            return True, ""
    return True, ""

@app.middleware("http")
async def rate_limit_middleware(request, call_next):
    # API 路径 + Emby 客户端路径都限流。Emby 登录（/emby/Users/AuthenticateByName）
    # 单独给更严的 per-IP 限额（15次/分钟，对标 go-emby），防暴力破解。
    path = request.url.path
    if path.startswith(("/api/", "/emby/")):
        ip = _get_client_ip(request)
        authenticated = _is_authenticated(request)
        # redis-py 是同步客户端：直接在事件循环里 incr/expire，就把一次 Redis 往返
        # 压在了全进程上（单进程部署下所有请求——包括正在播放的——都排队等它）。
        # 下放线程池，与 EA 侧（emby_api/main.py）保持同一手法。
        from starlette.concurrency import run_in_threadpool

        allowed, reason = await run_in_threadpool(_check_rate_limit, ip, path, authenticated)
        if not allowed:
            from fastapi.responses import JSONResponse
            return JSONResponse(
                status_code=429,
                content={"error": "请求太频繁，请稍后再试", "reason": reason},
                headers={"Retry-After": "60"},
            )
    return await call_next(request)


# 安全响应头
@app.middleware("http")
async def security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
    path = request.url.path
    if path.startswith(("/api/", "/emby/")):
        # video segments cacheable via cdn.is_segment_path; skip forced no-store
        from backend.emby_server.cdn import is_segment_path
        if not is_segment_path(path):
            response.headers.setdefault("Cache-Control", "no-store")
    elif response.status_code == 200 and path.startswith(("/assets/", "/admin/assets/")):
        # Vite 产物文件名带内容哈希：内容不变则文件名不变，可以长期强缓存
        response.headers.setdefault("Cache-Control", "public, max-age=31536000, immutable")
    elif "text/html" in response.headers.get("content-type", ""):
        # SPA 入口每次都要回源校验：发了新版本用户刷新就能拿到新构建
        response.headers.setdefault("Cache-Control", "no-cache")
    return response

@app.middleware("http")
async def request_body_limit_middleware(request, call_next):
    # 请求体大小上限：防恶意大包打爆内存（借鉴 go-emby 的 MaxBytesReader 思路）。
    # P2 修复（审查）：此前只看 Content-Length 头，分块传输（无该头）的请求
    # 完全绕过上限——Starlette 默认不截断，大包会完整读进内存。
    # 现用包装 receive 的方式强制上限：读超即 413。
    try:
        max_mb = float(os.getenv("MAX_REQUEST_BODY_MB", "10"))
    except ValueError:
        max_mb = 10
    max_bytes = int(max_mb * 1024 * 1024)

    clen = request.headers.get("content-length")
    if clen:
        try:
            if int(clen) > max_bytes:
                from fastapi.responses import JSONResponse
                return JSONResponse(
                    status_code=413,
                    content={"error": f"请求体过大，上限 {max_mb:g}MB"},
                )
        except ValueError:
            pass

    # 分块传输没有 Content-Length：包装 receive 累计计数，超限即抛 413
    _receive = request._receive
    _consumed = 0

    class _BodyTooLarge(Exception):
        pass

    async def _limited_receive():
        nonlocal _consumed
        message = await _receive()
        if message.get("type") == "http.request":
            body = message.get("body", b"")
            _consumed += len(body)
            if _consumed > max_bytes:
                raise _BodyTooLarge()
        return message

    request._receive = _limited_receive
    try:
        return await call_next(request)
    except _BodyTooLarge:
        from fastapi.responses import JSONResponse
        return JSONResponse(
            status_code=413,
            content={"error": f"请求体过大，上限 {max_mb:g}MB"},
        )
    finally:
        request._receive = _receive


# GZip：EM 不加 GZipMiddleware（JSON 压缩交给前端 nginx）。/emby/* 曾因三方 iOS 客户端
# gzip+chunked 解压 bug 而跳过，但条件中间件实现有缺陷，于是全局禁用保稳定；
# 如需恢复，只对非 /emby 路径加，并排除 application/octet-stream / application/zip。

# 下载策略兜底（覆盖 /Download 与 /Items/{id}/File 等全部下载类路径）
app.add_middleware(DownloadGuardMiddleware)

# 访问拦截（UA 关键词 + IP 归属地）：**两个开关默认都是关的**，
# 未配置时中间件只做一次时间比较就透传（零 DB / 零 await）。
# 放在下载兜底之后注册 = 它更靠外层：先判「这个客户端该不该进来」，
# 再判「进来之后能不能下载」。
app.add_middleware(AccessGuardMiddleware)

# 媒体与交付域的写操作审计（v2.30.0）：``/api/admin/emby/*`` 的成功写操作落进操作日志。
# 这一域长期不写审计：删媒体库 / 删条目 / 删 115 账号 / 停全站转码，在「操作日志」里
# 都查不到是谁干的。做成中间件而不是逐端点调用，是因为端点会新增，
# 而「记得补审计」不是一种机制（详见 backend/emby_server/audit.py）。
app.add_middleware(AdminWriteAuditMiddleware)


# ==================== 异常处理 ====================

@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    """全局异常处理"""
    logger.error(f"未处理的异常: {exc}", exc_info=True)
    return JSONResponse(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        content={
            "success": False,
            "message": "服务器内部错误",
            "detail": str(exc) if app.debug else None,
        }
    )


# ==================== Prometheus 监控 ====================

# 挂载 Prometheus metrics 端点
metrics_app = make_asgi_app()
# 默认只允许本机/内网采集：指标会把全部路由与计数摊开，不该裸挂公网
app.mount("/metrics", MetricsGuard(metrics_app))


# ==================== 健康检查 ====================

def _collect_health() -> dict:
    """跑一次健康聚合；任何异常都退化成 warn，绝不让健康检查 500"""
    from backend import health_report
    try:
        db = SessionLocal()
        try:
            return health_report.collect(db)
        finally:
            db.close()
    except Exception as e:  # noqa: BLE001
        logger.warning("健康检查聚合失败: %s", e)
        return {"level": "warn", "status": "degraded",
                "issues": [{"level": "warn", "key": "health",
                            "message": f"健康检查无法完成：{str(e)[:120]}"}],
                "metrics": {}}


@app.get("/api/health")
async def health_check():
    """健康检查端点

    status **不再硬编码 healthy**：以前数据库断、Redis 挂、worker 刷几千条探测
    失败，这里一律返回 healthy——「服务健康」页一片绿，出问题只能去翻日志。
    现在由 health_report 依据真实指标判定（healthy / degraded / unhealthy）。

    P1 修复（审查）：_collect_health 约 20 个 SQL + 目录 walk + Redis ping，
    同步跑会卡住事件循环几十到几百毫秒（探针 10~30s 打一次）。扔线程池。
    """
    from starlette.concurrency import run_in_threadpool
    health = await run_in_threadpool(_collect_health)
    # 播放预热指标（并发/去重/超时计数 + 平均耗时；模块内部永不抛异常）
    try:
        from backend.emby_server import playback_prewarm as _pb_prewarm
        _prewarm_metrics = _pb_prewarm.metrics_snapshot()
    except Exception:  # noqa: BLE001
        _prewarm_metrics = {}
    return {
        "status": health["status"],
        "health_level": health["level"],
        "health_issues": health["issues"],
        "health_metrics": health["metrics"],
        "playback_prewarm": _prewarm_metrics,
        "timestamp": datetime.now().isoformat(),
        "database": DATABASE_TYPE,
        "online_users": manager.get_online_count(),
        "emby_server": os.getenv("EMBY_SERVER_NAME", "Aetrix Media Server"),
        "service": "em",
        "version": app.version,
        "emby_gateway": _ENABLE_EMBY_GATEWAY,
        # 长期运行的体检口径：正在扫描的库 / 转码会话 / 临时目录占用 / 磁盘余量
        "runtime": _runtime_report(),
        # Redis 状态：队列/熔断器/分布式锁都依赖它
        "redis": _redis_status(),
    }


def _redis_status() -> dict:
    """Redis 连通性（健康检查用；异常不抛，只报告）"""
    try:
        from backend import database as db
        r = db.redis_client
        if r is None:
            return {"ok": False, "reason": "not_configured"}
        r.ping()
        return {"ok": True}
    except Exception as e:
        return {"ok": False, "reason": str(e)[:100]}


def _runtime_report() -> dict:
    """运行期资源快照（健康检查用；任何异常都不该让健康检查变 500）"""
    try:
        report = maintenance.resource_report()
        report["active_scans"] = maintenance.active_scan_count()
        return report
    except Exception as e:  # noqa: BLE001
        logger.debug(f"读取运行期资源失败: {e}")
        return {}


@app.get("/api/health/detailed")
def detailed_health_check():
    """详细健康检查"""
    health_status = {
        "status": "healthy",
        "timestamp": datetime.now().isoformat(),
        "services": {}
    }

    # 检查数据库
    try:
        db = SessionLocal()
        try:
            # 必须用 text() 包装：SQLAlchemy 2.0 不再接受裸字符串，
            # 之前这里恒抛异常导致数据库永远报 unhealthy（狼来了）
            db.execute(text("SELECT 1"))
            health_status["services"]["database"] = {"status": "healthy"}
        finally:
            db.close()
    except Exception as e:
        health_status["services"]["database"] = {"status": "unhealthy", "error": str(e)}
        health_status["status"] = "unhealthy"

    # 检查 Redis
    # 必须读 backend.database 的模块级 redis_client：CacheManager 上并没有
    # redis_client 属性，之前这里恒抛 AttributeError，导致整体状态永远是 degraded
    try:
        from backend import database as _db_module

        if _db_module.redis_client:
            _db_module.redis_client.ping()
            health_status["services"]["redis"] = {"status": "healthy"}
        else:
            health_status["services"]["redis"] = {"status": "disabled"}
    except Exception as e:
        health_status["services"]["redis"] = {"status": "unhealthy", "error": str(e)}
        health_status["status"] = "degraded"

    # WebSocket 连接数
    health_status["services"]["websocket"] = {
        "status": "healthy",
        "online_users": manager.get_online_count()
    }

    # 业务侧健康（探测失败率 / 补全堆积 / 扫描失败库）——以前这个端点只看
    # 数据库与 Redis，worker 刷爆、队列堆积一概看不见。与 /api/health 同一口径。
    agg = _collect_health()
    health_status["level"] = agg["level"]
    health_status["issues"] = agg["issues"]
    health_status["metrics"] = agg["metrics"]
    order = {"healthy": 0, "degraded": 1, "unhealthy": 2}
    if order.get(agg["status"], 0) > order.get(health_status["status"], 0):
        health_status["status"] = agg["status"]

    return health_status


# ==================== 路由注册 ====================

# WebSocket 路由
app.include_router(websocket_router)
app.include_router(notification_router)

# 用户端 API 路由
app.include_router(user_router)
# 首页聚合：9 项首屏数据一次取回（HomeView 骨架屏优化，见 backend/api/home_summary.py）
app.include_router(home_summary_router)
app.include_router(homepage_sections_router)

# 管理后台 API 路由
app.include_router(admin_router)
app.include_router(admin_ops_router)
app.include_router(services_router)
# 订阅到期提醒的面板口径与手动执行（见 backend/api/reminders_admin.py）
app.include_router(admin_reminders_router)
# 订单关单与退款（见 backend/api/orders_admin.py）
app.include_router(admin_orders_router)
# 优惠券管理（v2.10.0）：券的 CRUD / 核销记录 / 开关，见 backend/api/coupons_admin.py
app.include_router(admin_coupons_router)
# 外部服务能力中心（v2.19.0）：能力总览 / 配置 / 测试，见 backend/api/capabilities_admin.py
app.include_router(capabilities_router)
# 用户端 AI 助手（v2.19.0）：能力「AI 模型设置」的消费点，见 backend/api/assistant.py
app.include_router(assistant_router)
# 站点信息（v2.20.0）：能力「站点与品牌」的消费点，公开无需鉴权，见 backend/api/site.py
app.include_router(site_router)
# 管理员与权限（v2.26.0）：角色清单 / 授权 / 改角色 / 撤销，见 backend/api/admins_admin.py
app.include_router(admins_router)
# 首次运行向导（setup_mode）：GET /api/admin/setup/status 与 POST /api/admin/setup
app.include_router(setup_router)
# 播放与客户端策略（v2.26.0）：转码开关 / 并发上限 / 码率上限 / 客户端准入，见 backend/api/playback_admin.py
app.include_router(playback_admin_router)
# 媒体库封面自动生成：预览 / 渲染保存 / 按已存配置重新生成
app.include_router(library_cover_router)
# 多服运营：服的增删改查 / 每服运营数据 / 切换当前服（见 backend/realms.py）
app.include_router(realms_router)
app.include_router(emby_servers_router)
app.include_router(servers_router)
# 流节点（分离架构）管理：一键部署脚本的注册/配置包接口
app.include_router(stream_nodes_router)

# ==================== 自建 Emby 协议网关 ====================
# 默认由 EM 一并提供（单进程模式，现有部署行为不变）；
# 分离部署时置 ENABLE_EMBY_GATEWAY=false，协议面交给独立的 EA 服务，见 docs/deploy-ea.md
_ENABLE_EMBY_GATEWAY = os.getenv("ENABLE_EMBY_GATEWAY", "true").strip().lower() not in {
    "0", "false", "no", "off",
}

if _ENABLE_EMBY_GATEWAY:
    # 搜索接口先注册：FastAPI 按注册顺序取第一个匹配，
    # 这样 /Search/Hints 走 search_api 的相关度排序版（api.py 里的同名历史实现会被遮蔽）
    app.include_router(search_router)
    # 挂载来源：把只认本机文件的 /Items/{id}/File 换成挂载感知实现（必须在 include_router 前）
    install_mount_routes(emby_router)
    # 会话端点：补鉴权（普通用户只看/只能停自己）并把会话键改为随机（必须在 include_router 前）
    install_session_routes(emby_router)
    # 图片端点：接上条件请求（客户端缓存仍有效时 304），媒体库滚动/切页不再重传同一张海报
    install_image_routes(emby_router)
    # Emby 客户端直接连接本后端：https://host:port/emby（另含裸根路径 /System/Info 等）
    app.include_router(emby_router)
    logger.info("Emby 协议网关已挂载于 EM（单进程模式）")
else:
    logger.info("Emby 协议网关未在 EM 启用（分离部署）；客户端请连接 EA")

    async def _ea_pointer():
        """分离部署下，客户端误连面板时给出明确指引，而不是返回 SPA 的 HTML

        地址优先取管理后台「Emby 服务入口」里保存的分离部署地址（每次请求实时解析），
        没有再回退到环境变量，保证管理员在面板上改完地址后这里的指引也跟着变。
        """
        hint = (
            configured_emby_url()
            or os.getenv("EMBY_API_PUBLIC_URL", "").strip().rstrip("/")
            or os.getenv("EMBY_PUBLIC_URL", "").strip().rstrip("/")
            or "EA 服务地址"
        )
        raise HTTPException(
            status_code=404,
            detail=f"本地址（EM 面板）不提供 Emby 协议面，请把客户端指向 EA：{hint}",
        )

    # 裸根协议路径（/System/Info、/Users/AuthenticateByName …）与 /emby/* 各注册一份指引
    for _route in emby_router.routes:
        _route_path = getattr(_route, "path", "")
        if _route_path and not _route_path.startswith("/emby"):
            app.api_route(
                _route_path,
                methods=["GET", "POST", "PUT", "DELETE", "PATCH"],
                include_in_schema=False,
            )(_ea_pointer)
    app.api_route(
        "/emby/{rest:path}",
        methods=["GET", "POST", "PUT", "DELETE", "PATCH"],
        include_in_schema=False,
    )(_ea_pointer)

# 自建 Emby 门户 API（用户端账号卡/续看/收藏 + 管理端媒体库管理）
app.include_router(user_emby_router)
app.include_router(admin_emby_router)
app.include_router(portal_mount_routes.mount_picker_router)

# 用户门户认证 API（注册/登录/JWT）
app.include_router(auth_router)

# 用户经济系统（签到/兑换码/支付/订单）与邀请返利
app.include_router(economy_router)
app.include_router(invitation_router)
app.include_router(welfare_points_router)
app.include_router(welfare_requests_router)
app.include_router(welfare_reviews_router)
app.include_router(tg_bind_router)

# 群抽奖公开核验（无需登录，纯只读）
app.include_router(lottery_router)


# ==================== 根路径 ====================

@app.get("/")
async def root():
    """根路径"""
    index_file = _FRONTEND_DIST / "index.html"
    if index_file.is_file():
        return FileResponse(index_file)
    return {
        "name": "Aetrix Portal",
        "version": app.version,
        "status": "running",
        "timestamp": datetime.now().isoformat(),
        "docs": "/api/docs",
    }


# ==================== 用户前端静态资源托管 ====================
# 优先级顺序：先注册 API/WS 路由，再挂载静态资源与 SPA fallback，
# 保证 /api/*、/emby/*、/ws 不被前端兜底路由吞掉。

_FRONTEND_DIST = Path(
    os.getenv(
        "FRONTEND_DIST",
        os.path.join(os.path.dirname(os.path.dirname(__file__)), "user_frontend", "dist"),
    )
)

# 管理后台静态资源（/admin/*，Vite base=/admin/，构建产物在 admin_frontend/dist）
_ADMIN_DIST = Path(
    os.getenv(
        "ADMIN_DIST",
        os.path.join(os.path.dirname(os.path.dirname(__file__)), "admin_frontend", "dist"),
    )
)

if _ADMIN_DIST.is_dir():
    app.mount("/admin/assets", StaticFiles(directory=str(_ADMIN_DIST / "assets")), name="admin-assets")

    @app.get("/admin", include_in_schema=False)
    async def admin_root():
        """管理后台根路径（重定向到带斜杠的 SPA 入口）"""
        return RedirectResponse(url="/admin/")

    @app.get("/admin/{full_path:path}", include_in_schema=False)
    async def admin_spa_fallback(full_path: str):
        """管理后台 SPA 兜底路由：/admin/* 返回 admin index.html"""
        candidate = (_ADMIN_DIST / full_path).resolve()
        if candidate.is_file() and str(candidate).startswith(str(_ADMIN_DIST.resolve())):
            return FileResponse(candidate)
        return FileResponse(_ADMIN_DIST / "index.html")

    logger.info("管理后台静态资源已挂载: %s", _ADMIN_DIST)
else:
    logger.warning("管理后台构建产物不存在（%s），/admin 不可用", _ADMIN_DIST)

if _FRONTEND_DIST.is_dir():
    # 注意：/assets 由 StaticFiles 直接服务（构建产物固定输出到 assets/）
    app.mount("/assets", StaticFiles(directory=str(_FRONTEND_DIST / "assets")), name="assets")

    @app.get("/{full_path:path}", include_in_schema=False)
    async def spa_fallback(full_path: str):
        """SPA 兜底路由：非 API/WS 路径全部返回前端 index.html"""
        if full_path.startswith(("api/", "emby/", "ws", "metrics")):
            raise HTTPException(status_code=404, detail="Not Found")
        # 真实存在的静态文件直接返回（favicon.ico / manifest.webmanifest 等），并防目录穿越
        candidate = (_FRONTEND_DIST / full_path).resolve()
        if candidate.is_file() and str(candidate).startswith(str(_FRONTEND_DIST.resolve())):
            return FileResponse(candidate)
        return FileResponse(_FRONTEND_DIST / "index.html")

    logger.info("用户前端静态资源已挂载: %s", _FRONTEND_DIST)
else:
    logger.warning("前端构建产物不存在（%s），仅提供 API 服务", _FRONTEND_DIST)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        "main:app",
        host="0.0.0.0",
        port=8000,
        reload=True,
        log_level="info"
    )
