"""管理员配置：分离部署的 EA，或已有的外部 Emby。

保存 / 启用 EA 时会顺带拉一次**EA 视角的挂载体检**并落库（见
``backend/emby_server/mount_health``）：挂载里的本机路径、rclone RC 地址这类配置是
主机相对的，「面板测试通过」不代表那台 EA 能播，必须在添加服务入口时就把它查出来。
"""
import logging
from typing import Optional
from urllib.parse import urlparse

from fastapi import APIRouter, Depends, HTTPException
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from backend import models, realms
from backend import servers as registry
from backend.api.admin_core import _audit, get_current_admin
from backend.database import get_db
from backend.emby_server import mount_health

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/admin/emby", tags=["Emby 服务连接"])


class ConnectionRequest(BaseModel):
    mode: str
    url: str = Field(..., min_length=8, max_length=500)
    api_key: Optional[str] = Field(default=None, max_length=500)
    enabled: bool = True


def _validate_url(raw_url: str) -> str:
    """地址必须是完整的 http(s) 地址：只有端口的 "emby.local" 之类会在这里被拒。

    probe() 也要经过这里，否则「测试连接」可以绕过保存时的校验。
    """
    url = raw_url.strip().rstrip("/")
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise HTTPException(400, "地址必须是完整的 http:// 或 https:// 地址")
    return url


def _same_url(a: str, b: str) -> bool:
    """两个 URL 是否指向同一地址（P0-3 密钥复用判定）。

    归一化：去首尾空白/末尾斜杠、scheme+host 小写、默认端口折叠。
    只要有一个解析失败就判为不同（fail-closed：不复用旧密钥）。
    """
    try:
        na = urlparse(a.strip().rstrip("/"))
        nb = urlparse(b.strip().rstrip("/"))
        if not na.hostname or not nb.hostname:
            return False
        def _norm(p):
            scheme = p.scheme.lower()
            host = p.hostname.lower()
            port = p.port
            if (scheme == "http" and port == 80) or (scheme == "https" and port == 443):
                port = None
            path = (p.path or "").rstrip("/")
            return (scheme, host, port, path)
        return _norm(na) == _norm(nb)
    except Exception:
        return False


def value(db: Session, key: str, default: str = "", realm_id: Optional[int] = None) -> str:
    """读配置：收敛到 ``backend.servers.config_value``（横切能力只许一套）"""
    return registry.config_value(db, key, default, realm_id)


def save_value(db: Session, key: str, new_value: str, description: str,
               realm_id: Optional[int] = None) -> None:
    """写配置：收敛到 ``backend.servers.set_config``（横切能力只许一套）"""
    registry.set_config(db, key, new_value, description, realm_id)


async def _resolve_probe_key(db: Session, mode: str, url: str, api_key: Optional[str],
                             scope_id: Optional[int]) -> str:
    """解析探测用密钥。

    规则（按 mode 对应的 prefix 读取本模式已保存的配置，绝不允许 managed_ea 跨模式
    复用 emby_external_api_key）：

    1. 请求显式提供了非空 api_key：直接使用；
    2. 目标 URL 与本模式已保存 URL 一致（_same_url）：复用本模式已存密钥，
       即使已存密钥为空也返回 ""（空密钥探测）——这是恢复 P1-4 之前 test_server 的
       旧行为：同 URL、无新密钥、无已存密钥时走空密钥探测而非 400；且 P1-4 提交说明
       宣称 "external 语义不变"，故此处必须保持宽容。空密钥探测无安全风险，它不向
       任何主机发送密钥，只是会因认证失败而探测不通过；
    3. URL 确实发生变化且为 external 模式且无新密钥：抛 400（旧密钥跨 URL 复用不安全）；
    4. managed_ea：自建 EA 探测不需要密钥，返回 ""（即使存在旧 key，也绝不跨模式
       复用外部密钥）。
    """
    prefix = "emby_managed" if mode == "managed_ea" else "emby_external"
    old_url = await run_in_threadpool(value, db, f"{prefix}_url", "", scope_id)
    old_key = await run_in_threadpool(value, db, f"{prefix}_api_key", "", scope_id)

    provided = (api_key or "").strip()
    if provided:
        return provided

    # 同 URL：允许复用本模式旧密钥（old_key 为空也允许，即空密钥探测，保持旧行为宽容）。
    if _same_url(url, old_url):
        return old_key or ""

    if mode == "external":
        raise HTTPException(400, "更换服务器地址时必须提供新的 API 密钥，不能复用旧密钥")

    # managed_ea：自建 EA 探测不需要密钥；即使存在旧 key，也绝不跨模式复用外部密钥。
    return ""


async def refresh_mount_health(db: Session, url: str, realm_id: Optional[int] = None) -> dict:
    """拉一次 EA 视角的挂载体检并落库（按服保存：挂载跟着服走）

    失败时**保留上一次的逐条结果**（只标记快照失效），避免一次网络抖动就把面板上的
    「EA 可达」全部抹成未知。

    只有 ``fetch_ea_health`` 是 await（HTTP）；读旧快照与写新快照都是同步 SQLAlchemy，
    整段下放线程池——这条链路被「保存入口 / 手动体检 / 总览 live / 激活 EA」共用，
    留在事件循环上就是一次网络请求卡住全站（见 docs/performance.md 二·一〇）。
    """
    if realm_id is None:
        realm_id = await run_in_threadpool(realms.active_realm_id, db)
    result = await mount_health.fetch_ea_health(url)

    def store() -> dict:
        """写快照（同步；下放线程池）"""
        if result.get("ok"):
            data = result.get("data") or {}
            mount_health.write_ea_health(db, {
                "ok": True,
                "checked_at": data.get("checked_at"),
                "error": "",
                "playback_node": data.get("playback_node"),
                "mounts": data.get("mounts") or [],
            }, realm_id)
            db.commit()
            return {
                "ok": True,
                "checked_at": data.get("checked_at"),
                "total": data.get("total"),
                "failed_count": data.get("failed_count"),
                "unreachable": data.get("unreachable") or [],
            }

        previous = mount_health.read_ea_health(db, realm_id)
        error = str(result.get("error") or "EA 体检失败")[:300]
        mount_health.write_ea_health(db, {
            "ok": False,
            "checked_at": previous.get("checked_at"),
            "error": error,
            "playback_node": previous.get("playback_node"),
            "mounts": previous.get("mounts") or [],
        }, realm_id)
        db.commit()
        return {"ok": False, "error": error, "checked_at": previous.get("checked_at")}

    return await run_in_threadpool(store)


async def probe(mode: str, url: str, api_key: str = "") -> dict:
    """检查服务是否真的可用，而不是只检查端口是否能打开。

    实现落在 ``backend.servers``（``probe_ea`` / ``probe_emby``）：服务器清单页与这里
    用的是同一套探测，避免两处对「连得上」的口径出现分岐。
    """
    target = _validate_url(url)
    if mode == "managed_ea":
        return await registry.probe_ea(target)
    return await registry.probe_emby(target, api_key)


@router.get("/servers")
def get_servers(_: models.WebUser = Depends(get_current_admin), db: Session = Depends(get_db),
                realm_id: Optional[int] = None):
    """当前服的 Emby 服务入口（两个格子：自建 EA / 已有 Emby）

    入口是**一个服一个**的：多服部署下每个服各自填自己的地址，默认服沿用历史配置键。
    """
    scope_id = realm_id or realms.active_realm_id(db)
    realm = realms.get_realm(db, scope_id)
    health = mount_health.read_ea_health(db, scope_id)
    mounts = health.get("mounts") or []
    return {
        "realm_id": scope_id,
        "realm_name": realm.name if realm else "",
        "managed_ea": {
            "url": value(db, "emby_managed_url", realm_id=scope_id),
            "enabled": value(db, "emby_managed_enabled", "false", scope_id).lower() == "true",
            "reachable": value(db, "emby_managed_reachable", "", scope_id).lower() == "true",
        },
        "external": {
            "url": value(db, "emby_external_url", realm_id=scope_id),
            "enabled": value(db, "emby_external_enabled", "false", scope_id).lower() == "true",
            "has_api_key": bool(value(db, "emby_external_api_key", realm_id=scope_id)),
            "reachable": value(db, "emby_external_reachable", "", scope_id).lower() == "true",
        },
        "active_mode": value(db, "emby_active_mode", "managed_ea", scope_id),
        # EA 视角的挂载体检快照（汇总；逐条结果在「存储挂载」页）
        "mounts_health": {
            "ok": bool(health.get("ok")),
            "checked_at": health.get("checked_at"),
            "error": health.get("error") or "",
            "total": len(mounts),
            "failed_count": len([m for m in mounts if isinstance(m, dict) and m.get("ok") is False]),
            "unreachable": health.get("unreachable") or [],
        },
    }


@router.put("/servers")
async def save_server(
    request: ConnectionRequest,
    admin: models.WebUser = Depends(get_current_admin),
    db: Session = Depends(get_db),
):
    if request.mode not in ("managed_ea", "external"):
        raise HTTPException(400, "mode 必须是 managed_ea 或 external")
    admin_id = admin.id          # 纯值：下面有提交，之后再读 ORM 属性会在事件循环上回查
    scope_id = await run_in_threadpool(realms.active_realm_id, db)
    url = _validate_url(request.url)
    prefix = "emby_managed" if request.mode == "managed_ea" else "emby_external"
    api_key = request.api_key or ""

    # P1-3 修复：先解析/校验探测密钥（可能抛 400），再落库，避免失败时新地址已写入 DB。
    probe_key = await _resolve_probe_key(db, request.mode, url, api_key, scope_id)

    # F-P1-17 修复：先探测、再落库，一次提交。原先 save_connection 先 commit，
    # 探测失败只记 reachable=false，已写入的新地址/密钥不会回滚——两次提交之间
    # 崩溃还会留下"新地址已存但探测结论未知"的不一致状态。
    # （合并注：密钥解析沿用 r12 的 _resolve_probe_key——按 mode 读本模式配置，
    #  managed_ea 绝不跨模式复用外部密钥；不回退到 #664 的内联 external 硬编码版。）
    result = await probe(request.mode, url, probe_key)

    def save_all() -> None:
        """配置 + 探测结论 + 清单同步，同一事务一次提交（同步；下放线程池）"""
        save_value(db, f"{prefix}_url", url, "Emby 服务地址", scope_id)
        save_value(db, f"{prefix}_enabled", "true" if request.enabled else "false",
                   "Emby 服务是否启用", scope_id)
        if request.mode == "external" and api_key.strip():
            save_value(db, "emby_external_api_key", api_key.strip(),
                       "外部 Emby API 密钥", scope_id)
        save_value(db, f"{prefix}_reachable", "true" if result.get("ok") else "false",
                   "Emby 服务最近一次连接结果", scope_id)
        # 只有真正探测成功且勾选启用，才切换当前入口；避免保存一个不存在的服务后全站假运行。
        if request.enabled and result.get("ok"):
            save_value(db, "emby_active_mode", request.mode, "当前 Emby 服务模式", scope_id)
        # F-P1-17 一次提交（r13）：中间的 db.commit() 已移除——配置写与清单同步
        # 都在下面 upsert_legacy 内部的 commit 一起提交（它内部尾部自带 commit）。
        # 选此方案不选 commit=False 参数：upsert_legacy 已有 commit，删中间
        # commit 即实现一次提交，侵入更小；它抛错时整事务回滚，语义更严格。
        # 注意：此后的挂载体检/审计是另一次提交（中间夹网络 IO），非本事务。
        # 两个页面（旧的两个格子 / 新的清单）操作的是同一件事，收敛到一条真相，
        # 旧页保存后新页面就能看到它是「当前使用」。
        registry.upsert_legacy(
            db, "ea" if request.mode == "managed_ea" else "emby", url,
            api_key=api_key if request.mode == "external" else "",
            enabled=request.enabled, reachable=bool(result.get("ok")), realm_id=scope_id,
        )

    await run_in_threadpool(save_all)

    # 挂载是主机相对的：EA 能连通不等于它能碰到面板里配的存储。
    # 所以保存 EA 服务入口时顺带做一次 EA 视角的挂载体检，把结果落到面板上。
    health: dict | None = None
    if request.mode == "managed_ea" and result.get("ok"):
        health = await refresh_mount_health(db, url, scope_id)

    _audit(db, admin_id, "save_emby_server_connection", "system", None,
           {"mode": request.mode, "ok": result.get("ok"), "realm_id": scope_id,
            "mount_health_ok": None if health is None else health.get("ok")})
    await run_in_threadpool(db.commit)
    return {"success": True, "mode": request.mode, "probe": result,
            "mounts_health": health, "realm_id": scope_id}


@router.post("/servers/mounts/refresh")
async def refresh_server_mounts(
    admin: models.WebUser = Depends(get_current_admin),
    db: Session = Depends(get_db),
):
    """重新拉一次 EA 视角的挂载体检（「存储挂载」页的「EA 体检」按钮）

    只对 managed EA 有意义：一体化（EM 自己出流）与外部 Emby 都不存在这台 EA。
    """
    admin_id = admin.id          # 纯值：下面有提交，之后再读 ORM 属性会在事件循环上回查
    scope_id = await run_in_threadpool(realms.active_realm_id, db)
    url = await run_in_threadpool(value, db, "emby_managed_url", "", scope_id)
    if not url:
        raise HTTPException(400, "这个服还没有配置 EA 服务地址：请先在「服务器」页添加一台后端服（EA）并设为当前使用")
    mode = await run_in_threadpool(value, db, "emby_active_mode", "managed_ea", scope_id)
    if mode != "managed_ea":
        # 不阻断：切回 EA 之前也想先把那台机器的存储情况看清楚
        logger.info("当前模式是 %s，仍按 EA 地址体检挂载", mode)
    health = await refresh_mount_health(db, url, scope_id)
    _audit(db, admin_id, "refresh_ea_mounts_health", "system", None,
           {"ok": health.get("ok")})
    await run_in_threadpool(db.commit)
    return {"success": bool(health.get("ok")), "health": health}


@router.post("/servers/test")
async def test_server(
    request: ConnectionRequest,
    _: models.WebUser = Depends(get_current_admin),
    db: Session = Depends(get_db),
):
    if request.mode not in ("managed_ea", "external"):
        raise HTTPException(400, "mode 必须是 managed_ea 或 external")
    # 先校验地址再探测：校验不能只挂在 probe 里，否则替换探测器时就绕过了
    scope_id = await run_in_threadpool(realms.active_realm_id, db)
    url = _validate_url(request.url)
    # P1-4 修复：按请求模式读取本模式旧配置，复用仅限同 URL。
    # 行为变化：managed_ea 换地址不再 400（自建 EA 本来就不需要密钥）；external 语义不变。
    api_key = await _resolve_probe_key(db, request.mode, url, request.api_key, scope_id)
    result = await probe(request.mode, url, api_key)
    prefix = "emby_managed" if request.mode == "managed_ea" else "emby_external"

    def record() -> None:
        """记下最近一次连接结果（同步；下放线程池）"""
        save_value(db, f"{prefix}_reachable", "true" if result.get("ok") else "false",
                   "Emby 服务最近一次连接结果", scope_id)
        db.commit()

    await run_in_threadpool(record)
    return result
