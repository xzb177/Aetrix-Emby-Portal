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
import time
from datetime import datetime
from prometheus_client import make_asgi_app

from backend.metrics_guard import MetricsGuard
from backend.ratelimit import get_client_ip, is_authenticated_request
from sqlalchemy import text

from backend.database import SessionLocal, engine, get_db, init_db, DATABASE_TYPE
from backend.security import validate_secret_key
from backend.version import app_version
from backend import models  # 导入所有模型
from backend.config_schema import ConfigValueError
from backend.download_guard import DownloadGuardMiddleware
from backend.access_guard import AccessGuardMiddleware
from backend.emby_server.audit import AdminWriteAuditMiddleware
# P3-3（2026-10-12）：顶层导入——observability 中间件每请求都跑，import 不许留在函数体内
from backend.emby_server import observability as _observability
from backend.websocket import websocket_router, notification_router, manager
from backend.api import user_router, admin_router
from backend.api.emby_servers import router as emby_servers_router
from backend.api.admins_admin import router as admins_router
# 首次运行向导（setup_mode）：建第一个管理员，完成后入口永久关闭
from backend.api.setup import router as setup_router
from backend.api.playback_admin import router as playback_admin_router
from backend.api.series_follow_api import router as series_follow_router
from backend.api.admin_readonly import router as admin_readonly_router
from backend.api.media_sets_api import (
    admin_media_sets_router,
    user_media_sets_router,
)
from backend.api.store import router as store_router
from backend.api.store_requests import admin_router as store_requests_admin_router
from backend.api.store_requests import router as store_requests_router
from backend.api.governance_report import admin_router as governance_report_admin_router
from backend.api.governance_report import router as governance_report_router
from backend.api.source_discovery import router as source_discovery_router
from backend.api.library_cover import router as library_cover_router
from backend.api.media_library import mirror_router as cover_mirror_router
from backend.api.media_library import router as media_library_router
from backend.api.nl_search import router as nl_search_router
from backend.api.next302 import router as next302_router
from backend.api.admin_artwork import router as admin_artwork_router
from backend.api.library_order import router as library_order_router
from backend.api.smart_subtitles import router as smart_subtitles_router
from backend.api.home_summary import router as home_summary_router
from backend.api.homepage_sections import router as homepage_sections_router
from backend.api.panel import router as panel_router, admin_router as panel_admin_router
from backend.api.realms import router as realms_router
from backend.api.servers import router as servers_router
from backend.emby_server.stream_nodes import admin_router as stream_nodes_router
from backend import realms
from backend.emby_server import nodes as node_lib
from backend.emby_server import maintenance
from backend import reminders
from backend.api.admin_ops import admin_ops_router
from backend.api.admin_services import services_router
from backend.api.admin_tasks import tasks_router
from backend.api.reminders_admin import admin_reminders_router
from backend.api.admin_alerts import admin_alerts_router
from backend.api.admin_diagnosis import admin_diagnosis_router
from backend.api.tg_bot_admin import tg_bot_admin_router
from backend.api.admin_apikey import router as admin_apikey_router
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
from backend.emby_server import playback_bus_admin
from backend.emby_server import play_start_admin
from backend.emby_server import stream_routes  # noqa: F401
from backend.emby_server.image_routes import install_image_routes
from backend.emby_server.mount_routes import install_mount_routes
from backend.emby_server.session_routes import install_session_routes
from backend.emby_server.search_api import search_router
from backend.emby_server.play_preflight_api import preflight_router
from backend.emby_server.portal import user_emby_router, admin_emby_router, configured_emby_url
# 存储挂载端点已从 portal.py 拆出（portal.py 尾部超出编辑窗口）：导入即注册到同一个
# admin_emby_router 上，因此必须放在 app.include_router(admin_emby_router) 之前。
from backend.emby_server import portal_mount_routes  # noqa: F401
from backend.api.emby_portal import auth_router
from backend.api.economy import router as economy_router
from backend.api.invitation import router as invitation_router
from backend.api.points import router as welfare_points_router
from backend.api.welfare_reviews import router as welfare_reviews_router
from backend.api.tg_bind import router as tg_bind_router
from backend.api.lottery import router as lottery_router
from backend.emby_server.env_util import env_float

# 配置日志
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
# 日志脱敏：密钥/令牌不得进日志（TMDB api_key 曾明文进 docker logs）
from backend.log_redact import install_secret_redaction  # noqa: E402

install_secret_redaction()
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
    # 转码删除（2026-10-24）：启动时不再做硬件转码自测（hwaccel 已删）。

    # JWT 密钥是 EM/EA 认证与节点配对的根信任。未配置或过短时必须 fail-closed，
    # 不能让服务用导入期随机值带病上线。
    validate_secret_key()

    # 初始化数据库。schema 不完整时必须拒绝启动，不能把半可用服务暴露给用户。
    try:
        init_db()
    except Exception:
        logger.exception("数据库初始化失败，服务拒绝启动")
        raise

    # 115 sidecar 显式拉起（生产接线）：开闸时随服务启动拉起 sidecar 子进程
    # 并握手验活；拉起失败只记 ERROR（supervisor 里已写清诊断），不阻断启动——
    # 115 挂载首次使用时仍会响亮报错，不静默回落进程内。关闸时本调用是空操作。
    try:
        from backend.emby_server import p115_sidecar_supervisor
        if not p115_sidecar_supervisor.ensure_started():
            logger.error("115 sidecar 拉起失败：115 挂载暂不可用，"
                         "诊断见上一条 ERROR；回滚 = PAN115_SIDECAR_ENABLED=0 后重启")
    except Exception as e:  # noqa: BLE001
        logger.warning(f"115 sidecar 拉起异常（可忽略，首次使用时再报错）: {e}")

    # EMBY_PUBLIC_URL 不合规只告警：http 或域名守卫不允许的主机不能把进程打挂。
    try:
        from backend.emby_server.public_url import warn_public_url_on_startup
        warn_public_url_on_startup()
    except Exception:
        logger.warning("EMBY_PUBLIC_URL 启动校验失败，服务继续启动", exc_info=True)

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

    # P0-3（2026-10-12）：SA token 冷启动预铸。部署后第一次播放要现换 token
    # （约 300~600ms），这里提前为所有 SA 换好写进缓存。后台线程跑，
    # 不阻塞启动；失败静默（播放时按需换取兜底）。
    if not _is_api_role:
        try:
            import threading

            def _premint_in_bg():
                try:
                    from backend.emby_server import drive_auth
                    # 启动期错峰，避免 100 个 RSA 签名+HTTPS POST 集中打满 CPU 与刚启动的维护任务抢资源
                    _delay = env_float("SA_PREMINT_STARTUP_DELAY_SECONDS", 60.0, 0.0)
                    _stagger = env_float("SA_PREMINT_STAGGER_SECONDS", 180.0, 0.0)
                    time.sleep(_delay)
                    drive_auth.premint_tokens(max_workers=2, stagger_seconds=_stagger)
                except Exception as e:  # noqa: BLE001
                    logger.warning(f"SA token 预铸失败（可忽略）: {e}")

            _t = threading.Thread(target=_premint_in_bg,
                                  name="sa-premint-startup", daemon=True)
            _t.start()
            logger.info("SA token 预铸已在后台启动")
        except Exception as e:  # noqa: BLE001
            logger.warning(f"启动 SA token 预铸失败（可忽略）: {e}")


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

    # 系统告警：后台指标越线时通知管理员。总开关默认关（见 backend/system_alerts.py），
    # 没开时线程空转等待，不打扰任何现有行为；EA 侧不启动（多台 EA 会重复打扰）。
    if not _is_api_role:
        try:
            from backend import system_alerts
            system_alerts.start_alert_scheduler()
        except Exception as e:  # noqa: BLE001
            logger.warning(f"启动系统告警检查失败（可忽略）: {e}")

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

    logger.info("✅ Aetrix Portal 启动完成")

    yield

    # 关闭时：跑退出收尾（转码删除后收尾已是空操作，保留调用点做兼容）
    logger.info("👋 Aetrix Portal 正在关闭...")
    try:
        maintenance.shutdown_cleanup()
    except Exception as e:  # noqa: BLE001
        logger.warning(f"退出收尾失败（可忽略）: {e}")
    # 115 sidecar：只停本服务拉起的子进程，复用的外部 sidecar 不碰。
    try:
        from backend.emby_server import p115_sidecar_supervisor as _sup
        _sup.stop()
    except Exception as e:  # noqa: BLE001
        logger.warning(f"停止 115 sidecar 失败（可忽略）: {e}")
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
# 白名单解析保留在此处；注册挪到所有守卫之后（最外层），见文件下方。
_cors_origins_env = os.getenv("CORS_ORIGINS", "").strip()
_cors_origins = [o.strip() for o in _cors_origins_env.split(",") if o.strip()]


# ============ API 限流（Redis 固定窗口）============
# 保护 API 不被滥用；健康检查不限流；认证用户限额更高
# 注意：这是固定窗口（每分钟重置），不是滑动窗口
# 规则表收敛到 backend.ratelimit.API_RATE_LIMITS（横切能力只许一套）：
# EM 与 EA 共用，改限额只改一处。
from backend.ratelimit import API_RATE_LIMITS as RATE_LIMITS  # noqa: E402

def _get_client_ip(request) -> str:
    """获取真实客户端 IP（防伪造）。

    已统一使用 backend.ratelimit.get_client_ip：
    只有直连方是可信代理（TRUSTED_PROXIES）时才信任 XFF/X-Real-IP，
    否则用直连 IP，防止伪造头绕过限流。
    保留此函数名做兼容。
    """
    return get_client_ip(request)

def _is_authenticated(request) -> bool:
    """是否认证用户：只认合法的 access JWT（验签），不只看头在不在。

    判定逻辑收敛到 backend.ratelimit.is_authenticated_request（横切能力只许一套）：
    EM 限流中间件与 EA 限流中间件共用同一实现。旧实现只看 Authorization 头有没有，
    攻击者发任意伪造头就能拿到认证档限额；现纯验签，验签失败一律 False（fail-closed）。
    保留本函数名做兼容（tests/test_ratelimit_auth.py 直接引用）。
    """
    return is_authenticated_request(request)

def _check_rate_limit(ip: str, path: str, authenticated: bool) -> tuple[bool, str]:
    """返回 (是否允许, 原因)"""
    # r4-auth：登录四条路径归一到同一桶（大小写/有无 /emby 前缀不拆桶）。
    from backend.ratelimit import canonical_rate_limit_path as _canon

    path = _canon(path)
    for prefix, limit_anon, limit_auth in RATE_LIMITS:
        if path.startswith(prefix):
            limit = limit_auth if authenticated else limit_anon
            if limit == 0:
                return True, ""
            try:
                from backend import database as db
                import time

                # 前台活跃打点（扫描等后台重活让路用，见 foreground_activity）：
                # 复用本次线程池调用顺手写 Redis，不新增请求路径；健康检查
                # （limit==0）在上面已提前返回，不会打点。
                try:
                    from backend.emby_server import foreground_activity as _fg

                    _fg.note_redis(db.redis_client)
                except Exception:
                    pass

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
    # r4-auth：裸 /Users/ 登录路径也要进限流（旧门只认 /api/ 与 /emby/，
    # 裸登录在 EM/EA 两边都绕过了网关限流）。非登录 /Users/ 路径无匹配
    # 规则，_check_rate_limit 直接放行，不产生额外限流。
    if path.startswith(("/api/", "/emby/", "/Users/")):
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


# 可观测指标采集（v2.56.0）：记录 API 耗时/P99/错误率，进程内环形缓冲。
# 开关默认关（OBSERVABILITY_ENABLED=1 才采集）；记录失败永不影响主链路。
@app.middleware("http")
async def observability_middleware(request: Request, call_next):
    # P3-3：import 已移到模块顶层，这里不再逐请求 import
    if not _observability.enabled():
        return await call_next(request)
    start = time.monotonic()
    status = 500
    try:
        response = await call_next(request)
        status = response.status_code
        return response
    finally:
        try:
            elapsed_ms = (time.monotonic() - start) * 1000
            _observability.record_request(request.url.path, request.method, status, elapsed_ms)
        except Exception:  # noqa: BLE001
            pass


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
    max_mb = env_float("MAX_REQUEST_BODY_MB", 10.0)
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

# CORS 最后注册 = 最外层（Starlette 逆序执行中间件）：外层守卫拦截返回的 403
# 也要带 CORS 头，否则浏览器只报笼统 CORS 错误、前端读不到真实状态码。
# 白名单解析见上方 _cors_origins，为空时不注册（同源不需要）。
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


# ==================== 异常处理 ====================

@app.exception_handler(ConfigValueError)
async def config_value_error_handler(request: Request, exc: ConfigValueError):
    """配置校验失败 → 400（config_schema 的约定：非法值由调用方转成 400）。

    横切能力只许一套：管理端保存接口经 config_service.save_config 抛出的所有
    校验错误统一在这里转 400，各端点不再各自 try/except。返回体沿用 FastAPI
    标准 {"detail"} 形状，管理后台按 detail 透传中文人话。
    """
    return JSONResponse(
        status_code=status.HTTP_400_BAD_REQUEST,
        content={"detail": str(exc)},
    )


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
    """跑一次健康聚合；任何异常都退化成 warn，绝不让健康检查 500

    2026-10-16 网页加载二轮优化：走 collect_cached() 的短 TTL 缓存入口。
    collect() 在扫描打满 2 核生产机时约 5–10 秒，「服务器」页的生命体区与
    /api/health 都曾因此每次全量重跑；TTL 经 HEALTH_REPORT_TTL_SECONDS 可配。
    panel.py 的 pulse/vitals 经此处 import，自动同口径。
    """
    from backend import health_report
    try:
        db = SessionLocal()
        try:
            return health_report.collect_cached(db)
        finally:
            db.close()
    except Exception as e:  # noqa: BLE001
        logger.warning("健康检查聚合失败: %s", e)
        return {"level": "warn", "status": "degraded",
                "issues": [{"level": "warn", "key": "health",
                            "message": f"健康检查无法完成：{str(e)[:120]}"}],
                "metrics": {}}


def _collect_observability() -> dict:
    """跑一次可观测聚合；任何异常都退化，绝不让健康检查 500"""
    from backend.emby_server import observability as _obs
    try:
        db = SessionLocal()
        try:
            return _obs.snapshot(db)
        finally:
            db.close()
    except Exception as e:  # noqa: BLE001
        logger.warning("可观测聚合失败: %s", e)
        return {"enabled": True, "level": "warn", "subsystems": {},
                "error": str(e)[:120]}


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
    # 可观测底座（v2.56.0）：开关默认关；开时返回各子系统健康探针 + API 指标
    try:
        from backend.emby_server import observability as _obs
        if _obs.enabled():
            _obs_snapshot = await run_in_threadpool(_collect_observability)
        else:
            _obs_snapshot = {"enabled": False}
    except Exception:  # noqa: BLE001
        _obs_snapshot = {"enabled": False, "error": "snapshot_failed"}
    return {
        "status": health["status"],
        "health_level": health["level"],
        "health_issues": health["issues"],
        "health_metrics": health["metrics"],
        "observability": _obs_snapshot,
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
app.include_router(series_follow_router)
# 智能字幕（第三批第一单，默认关；只挂用户端路由，不碰首页）
app.include_router(smart_subtitles_router)
# 首页聚合：9 项首屏数据一次取回（HomeView 骨架屏优化，见 backend/api/home_summary.py）
app.include_router(home_summary_router)
app.include_router(homepage_sections_router)
# v4 探险家日志面板（特性开关 PANEL_V4_ENABLED，只做加法）
app.include_router(panel_router)
app.include_router(panel_admin_router)

# 管理后台 API 路由
app.include_router(admin_router)
app.include_router(admin_ops_router)
app.include_router(services_router)
app.include_router(tasks_router)
# 订阅到期提醒的面板口径与手动执行（见 backend/api/reminders_admin.py）
app.include_router(admin_reminders_router)
# 系统告警：规则管理 / 告警记录 / 总开关（见 backend/api/admin_alerts.py）
app.include_router(admin_alerts_router)
# 一键体检：只读诊断报告（见 backend/api/admin_diagnosis.py）
app.include_router(admin_diagnosis_router)
app.include_router(tg_bot_admin_router)
# API Key / Service Token：创建/列表/吊销 + 连通性测试（见 backend/api/admin_apikey.py）
app.include_router(admin_apikey_router)
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
# 播放实时总线管理端（FMBY #24 接线）：命令表 + 快照读数；总线仍默认关，
# 关时发命令回 503、不执行（见 backend/emby_server/playback_bus_admin.py）。
# 挂载即导入模型，lifespan 的 init_db create_all 随既有表建出
# playback_control_commands 新表（部署时须亲验新表已建出）。
app.include_router(playback_bus_admin.router)
# 起播事件聚合读口（FMBY #2 闭环接线）：管理端只读口直接调
# play_events.aggregate_play_start_events，见 play_start_admin.py。
app.include_router(play_start_admin.router)
# 管理后台只读观测口（专线 B 第一阶段：补口）：绑定 / 采样进度 / sidecar /
# 映射 / 139 池，全部 GET，见 backend/api/admin_readonly.py
app.include_router(admin_readonly_router)
# 媒体库合集域 L3：管理端 CRUD/规则/刷新/模板 + 用户端架与成员读口。
# 总闸 COLLECTIONS_ENABLED 默认关，关时本组接口惰性 404，现有路由不动。
app.include_router(admin_media_sets_router)
app.include_router(user_media_sets_router)
# 想看商店骨架（§7 / R0）：总闸 STORE_ENABLED 默认关，关时本组接口惰性 404。
# 不入队转存、不接播放。见 backend/api/store.py 与 store_contract.py。
app.include_router(store_router)
# 想看商店求片 R1：登记 + 榜单 + 入库联动（读时同步翻 completed）。
# 复用 movie_requests / movie_request_votes，不另建表；只受 STORE_ENABLED 总闸 + 会员闸管。
app.include_router(store_requests_router)
app.include_router(store_requests_admin_router)
app.include_router(governance_report_router)
app.include_router(governance_report_admin_router)
# 找源通用能力：总闸 SOURCE_DISCOVERY_ENABLED 默认关，关时本组接口惰性 404。
# 只做找候选（上游聚合映射源）+ 确认后复用既有转存链路，不挂前端。
app.include_router(source_discovery_router)
# 媒体库封面自动生成：预览 / 渲染保存 / 按已存配置重新生成
app.include_router(library_cover_router)
# 媒体库学习（封面/排序/货架）：门户端游标分页 / 分区 / 藏 / 封面五件套
# 与管理端覆盖 / 库设序，见 backend/api/media_library.py 等
app.include_router(media_library_router)
# 自然语言搜片（第三批第二单，默认关；只挂用户端搜索路由，不碰首页）
app.include_router(nl_search_router)
# 302 Next MVP 非商店部分（默认关；只挂用户端搜索路由，不碰首页/商店）
app.include_router(next302_router)
# 图床镜像出图（批一）：无前缀，即 /cdn/covers/{hash}，公开出图
app.include_router(cover_mirror_router)
app.include_router(admin_artwork_router)
app.include_router(library_order_router)
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
    # 播放前体检（第二批第 2 单，只读诊断口，不下发播放链接）：
    # 与搜索接口同挂网关块，关时 404，全站逐字不变。
    app.include_router(preflight_router)
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
    import logging

    import uvicorn

    try:
        try:
            from backend.domain_guard import uvicorn_proxy_kwargs
        except ImportError:
            from domain_guard import uvicorn_proxy_kwargs

        proxy_kwargs = uvicorn_proxy_kwargs()
    except Exception:
        logging.getLogger(__name__).warning(
            "构造代理头信任名单失败，uvicorn 只信回环", exc_info=True
        )
        proxy_kwargs = {"proxy_headers": True, "forwarded_allow_ips": "127.0.0.1,::1"}
    uvicorn.run(
        "main:app",
        host="0.0.0.0",
        port=8000,
        reload=True,
        log_level="info",
        **proxy_kwargs,
    )
