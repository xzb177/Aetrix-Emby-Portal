"""服务器管理 API：添加 / 编辑 / 测试 / 删除 / 设为当前使用

面板以前只能填一台 EA 和一台外部 Emby，MoviePilot 与 qBittorrent 更是完全没有入口，
于是「求片」批了以后没有任何办法把片子真的弄进来。这里把「服务器」变成一份可增删改的清单：

- `GET    /api/admin/servers`            清单 + 类型元数据 + 统计（面板顶部与数据概览都用它）
- `GET    /api/admin/servers/summary`    只取统计（数据概览的信息展示用）
- `GET    /api/admin/servers/{id}/ops`   媒体运维快照（**服务器维度**：整服扫描历史 / 扫描任务 / 刮削补全 / 内容转交）
- `POST   /api/admin/servers/{id}/ops/scan` 一键把这台节点负责的库推入扫描队列
- `POST   /api/admin/servers`            新增（保存即体检一次，结果落到列表）
- `PUT    /api/admin/servers/{id}`       修改（密钥留空 = 不改）
- `POST   /api/admin/servers/{id}/test`  重新体检
- `POST   /api/admin/servers/{id}/activate`  设为该类型的「当前使用」
- `POST   /api/admin/servers/{id}/toggle`    启用 / 停用
- `DELETE /api/admin/servers/{id}`       删除
- `POST   /api/admin/servers/test`       测试「还没保存」的配置
- `GET    /api/admin/servers/{id}/rclone-conf`  这台 EA 的 rclone.conf（**只返 remote 名**）
- `POST   /api/admin/servers/{id}/rclone-conf`  保存这台 EA 的 rclone.conf

EA / Emby 被激活时会同步写回既有的 `emby_managed_*` / `emby_external_*` / `emby_active_mode`，
所以网关闸门、挂载体检、客户端指引、用户端账号卡全部照旧生效（见 ``backend.servers.sync_legacy``）。
所有写操作都落管理审计。
"""
from __future__ import annotations

import asyncio
import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from backend import models, realms
from backend import servers as registry
from backend.api.admin_core import _audit, get_current_admin
from backend.database import SessionLocal, get_db
from backend.emby_server import models as em
from backend.emby_server import mount_health
from backend.emby_server import nodes as node_lib
from backend.emby_server import server_ops
from backend.emby_server import mount_rclone
from backend.emby_server import mount_drive  # noqa: F401 — 导入即注册 drive 提供者

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/admin/servers", tags=["服务器管理"])


class ServerPayload(BaseModel):
    name: str = Field(..., min_length=1, max_length=80)
    kind: str = Field(..., min_length=1, max_length=20)
    url: str = Field(..., min_length=8, max_length=500)
    config: dict = Field(default_factory=dict)
    is_enabled: bool = True
    remark: str = Field(default="", max_length=300)
    # 属于哪个服（留空 = 新增时归当前服，修改时不改归属）
    realm_id: Optional[int] = None
    # MoviePilot / qBittorrent 专用：声明「全服共用」（realm_id 置空，每个服都看得到）
    shared: bool = False


class ProbePayload(BaseModel):
    """测试「还没保存」的配置：新增对话框里的「测试连接」按钮用"""
    kind: str = Field(..., min_length=1, max_length=20)
    url: str = Field(..., min_length=8, max_length=500)
    config: dict = Field(default_factory=dict)
    server_id: Optional[int] = None  # 编辑时带上：密钥留空则沿用已保存的那份


def _check_kind(kind: str) -> str:
    kind = (kind or "").strip().lower()
    if kind not in registry.KIND_MAP:
        raise HTTPException(400, f"服务器类型必须是：{'、'.join(registry.KINDS)}")
    return kind


def _check_url(url: str) -> str:
    try:
        return registry._validate_url(url)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


def _effective_config(kind: str, config: dict, server_id: Optional[int], db: Session) -> dict:
    """体检用的配置：密钥留空且带 server_id 时，沿用已保存的那份（否则「测一下」永远失败）"""
    merged = dict(config or {})
    if server_id:
        saved = registry.get_server(db, server_id)
        if saved and saved.kind == kind:
            for key in registry.secret_keys(kind):
                if not str(merged.get(key) or "").strip():
                    value = registry.parse_config(saved).get(key)
                    if value:
                        merged[key] = value
    return merged


def _name_conflict(db: Session, name: str, exclude_id: Optional[int] = None) -> bool:
    query = db.query(models.RemoteServer).filter(models.RemoteServer.name == name.strip())
    if exclude_id:
        query = query.filter(models.RemoteServer.id != exclude_id)
    return db.query(query.exists()).scalar() is True


def _missing_required(kind: str, url: str, config: dict) -> Optional[str]:
    """类型层面的必填校验（比「地址填了就行」更早给出可读提示）"""
    if kind == "qbittorrent" and not str(config.get("username") or "").strip():
        return "qBittorrent 需要用户名（Web UI 的登录名）"
    if kind == "moviepilot" and not (str(config.get("api_key") or "").strip()
                                     or str(config.get("username") or "").strip()):
        return "MoviePilot 至少填一项凭据：API 密钥（查订阅）或用户名 + 密码（提交订阅）"
    return None


@router.get("")
def list_servers(_: models.WebUser = Depends(get_current_admin), db: Session = Depends(get_db),
                      realm_id: Optional[int] = None):
    """服务器清单（按服；``realm_id=0`` 看全部服）

    ``ea`` / ``emby`` 这两类属于某个服（一个服一个入口）；MoviePilot / qBittorrent
    是内容自动化，多服可以共用，所以“全部服”视图里它们也一并列出。
    """
    scope_id = None if realm_id == 0 else (realm_id or realms.active_realm_id(db))
    # EA / Emby 按归属服；MoviePilot / qB 共用的（realm_id 为空）在每个服里都列出
    rows = (registry.realm_scope(db.query(models.RemoteServer), scope_id)
            .order_by(models.RemoteServer.kind, models.RemoteServer.id).all())
    return {
        "servers": [registry.serialize(db, r) for r in rows],
        # 类型元数据由后端下发，前端不再自己维护一份字段表
        "kinds": registry.SERVER_KINDS,
        "summary": registry.summary(db, scope_id),
        "realm_id": scope_id,
        "active_realm_id": realms.active_realm_id(db),
        "realms": [{"id": r.id, "name": r.name, "slug": r.slug} for r in realms.list_realms(db)],
    }


# Emby 入口模式 → 人话（面板、网关闸门与用户端账号卡读的是同一个键）
ENTRY_LABELS = {
    "managed_ea": "后端服（EA）出流",
    "external": "已有 Emby 服出流",
    "panel": "面板自己出流",
}


def _realm_entry(db: Session, realm_id: int) -> dict:
    """这个服当前用哪个入口出流（按服的 ``emby_active_mode``）"""
    mode = realms.realm_config(db, "emby_active_mode", realm_id, "managed_ea") or "managed_ea"
    if mode == "external":
        url = realms.realm_config(db, "emby_external_url", realm_id)
    else:
        url = realms.realm_config(db, "emby_managed_url", realm_id)
    return {"mode": mode, "label": ENTRY_LABELS.get(mode, mode), "url": url}


def _mount_summary(db: Session, realm_id: int) -> dict:
    """EA 视角的挂载体检快照（按服保存）：挂载里的本机路径是主机相对的，只有那台机器说了算"""
    health = mount_health.read_ea_health(db, realm_id)
    mounts = [m for m in (health.get("mounts") or []) if isinstance(m, dict)]
    failed = [m for m in mounts if m.get("ok") is False]
    return {
        "ok": bool(health.get("ok")),
        "checked_at": health.get("checked_at"),
        "error": health.get("error") or "",
        "total": len(mounts),
        "failed_count": len(failed),
        "unreachable": [m.get("name") or f"#{m.get('id')}" for m in failed],
        # 从没查过：不是错误，但面板上要提示「还不知道」
        "never_checked": not mounts and not health.get("checked_at") and not health.get("error"),
    }


def _library_counts(db: Session, realm_id: Optional[int]) -> dict:
    """媒体库归属：已分配给某台节点的 / 还没分配（未分配 = 所有节点可见、由面板扫描）"""
    query = realms.scope_inclusive(db.query(em.Library), em.Library.realm_id, realm_id)
    rows = query.all()
    unassigned = [lib for lib in rows if not getattr(lib, "node_id", None)]
    return {
        "total": len(rows),
        "unassigned": len(unassigned),
        "by_node": {
            node_id: len([lib for lib in rows if getattr(lib, "node_id", None) == node_id])
            for node_id in {getattr(lib, "node_id", None) for lib in rows if getattr(lib, "node_id", None)}
        },
    }


def _node_key_claim_warnings(*, reported_key: str, claimed: bool, registered_key: str) -> list:
    """NODE_KEY 认领一致性判定（纯函数）。

    ``registered_key`` 必须是库里的登记原值（``RemoteServer.node_key``），**不许**
    拿 ``servers.serialize()`` 打码后的 ``"已配置"``/``""`` 来比对——打码是 P2 安全修复的
    有意为之（对外不暴露凭证明文），但用它比对会让「ea-01 vs 已配置」永远误报不一致。
    """
    warnings: list = []
    if reported_key and not claimed:
        warnings.append(
            f"EA 上报的 NODE_KEY「{reported_key}」在面板的服务器清单里认领不到对应记录"
        )
    elif reported_key and registered_key and reported_key != registered_key:
        warnings.append(
            f"EA 上报的 NODE_KEY「{reported_key}」与面板登记的「{registered_key}」不一致"
        )
    elif not reported_key and registered_key:
        warnings.append("面板给这台入口登记了 NODE_KEY，但它自己没配——请核对 EA 的 NODE_KEY")
    return warnings


@router.get("/overview")
async def emby_overview(
    _: models.WebUser = Depends(get_current_admin),
    db: Session = Depends(get_db),
    realm_id: Optional[int] = None,
    live: bool = False,
):
    """**Emby 总览**：一行一台出流入口（EA / 已有 Emby），把散落各处的事实汇到一页

    以前这些信息分布在三个页面：「Emby 服务入口」看入口、「存储挂载」看 EA 的可达性、
    「媒体库」看库归谁扫——想知道「这台 EA 到底在不在服务这个服」得来回跳着拼。这里按服
    给出每台入口的：连接体检、节点认领（``NODE_KEY``）、归属节点与媒体库、EA 视角的挂载
    体检，并把不一致直接标成告警。

    ``live=True`` 会真的去问每台已启用的 EA「你是谁、属于哪个服、负责哪些库」
    （``GET /api/admin/nodes/me``，节点签名鉴权，见 backend/node_auth.py）并重拉一次当前出流 EA 的挂载体检；
    默认只读已落库的结论，避免打开页面就被慢节点拖住。
    """
    def load() -> tuple[Optional[int], list, list, list, list]:
        """读范围 / 服清单 / 入口清单，并把要探测的目标取成纯值（同步；下放线程池）

        **必须在任何 await 之前**把这些读出来：探测会提交，提交之后这一行的 ORM 属性
        全部过期，再去读 ``row.url`` 就会在事件循环上触发一次隐式回查。
        """
        scope = None if realm_id == 0 else (realm_id or realms.active_realm_id(db))
        rows_ = [realms.get_realm(db, scope)] if scope else realms.list_realms(db)
        rows_ = [r for r in rows_ if r is not None]
        servers_ = (registry.realm_scope(db.query(models.RemoteServer), scope)
                    .order_by(models.RemoteServer.kind, models.RemoteServer.id).all())
        entries = [s for s in servers_ if s.kind in registry.LEGACY_PREFIX]
        identity_targets = [(s.id, s.url) for s in entries if s.kind == "ea" and s.is_enabled]
        mount_targets = [
            (r.id, active.url)
            for r in rows_
            for active in [registry.active_server(db, "ea", r.id)]
            if active and active.is_enabled
        ]
        # F-P1-22：把 ORM 对象剥离会话——探测阶段要归还会话连接，
        # 后续 build() 用新会话；已加载的列属性在剥离后仍可读。
        # 警示：剥离后对象已 detached，_build_impl/serialize 只可读已加载的列属性，
        # 任何关系（relationship）访问都会触发 DetachedInstanceError。新增访问
        # 前请先把所需值取成纯值（见 identity_targets/mount_targets 的做法）。
        db.expunge_all()
        return scope, rows_, entries, identity_targets, mount_targets

    scope_id, realm_rows, entry_rows, identity_targets, mount_targets = (
        await run_in_threadpool(load)
    )
    # F-P1-22：load() 已结束，归还连接到池。探测阶段是纯网络 IO，
    # 不再让请求会话的连接被 8 秒×N 台的探测长期占用。
    db.close()

    # live：先问节点身份（认领 / 自称的服 / 负责哪些库），再重拉当前出流 EA 的挂载体检。
    # 两者都有超时上限：一台掉线的机器不应该让整个总览转圈。
    identities: dict[int, dict] = {}
    if live:
        # F-P1-23：串行改并发（原 N 台 × 8 秒串行累加）。单台仍 8 秒超时，
        # 另加总超时上限兜底（8×台数，上限 60 秒）。
        identities = await _probe_identities(identity_targets)

        from backend.api.emby_servers import refresh_mount_health

        for target_realm, target_url in mount_targets:
            # F-P1-22：体检写库用独立短会话，用完即关，不占用请求会话
            probe_db = SessionLocal()
            try:
                await refresh_mount_health(probe_db, target_url, target_realm)
            except Exception as exc:  # noqa: BLE001
                logger.warning("服 #%s 的 EA 挂载体检没能完成: %s", target_realm, exc)
            finally:
                probe_db.close()

    def _build_impl(db: Session) -> dict:
        # 注意：realm_rows/entry_rows 是 load() 里 expunge 剥离出的 detached 对象，
        # 这里只能读已加载的列属性（realm.id/realm.slug/…），不要访问任何 relationship，
        # 否则直接 DetachedInstanceError。需要新数据时用 db 重新查。
        rows: list[dict] = []
        totals = {"entry": 0, "online": 0, "warnings": 0, "libraries": 0, "libraries_unassigned": 0}
        for realm in realm_rows:
            realm_libraries = _library_counts(db, realm.id)
            realm_nodes = [s for s in entry_rows if s.realm_id == realm.id]
            realm_ea_count = len([s for s in realm_nodes if s.kind == "ea"])
            realm_mounts = _mount_summary(db, realm.id)

            for server in realm_nodes:
                data = registry.serialize(db, server)
                warnings: list[str] = []
                # 登记原值（打码前的 node_key）：identity 比对与多 EA 检查都用它，
                # 不许拿 data["node_key"]（serialize() 打码后的「已配置」/"")。
                registered_key = (server.node_key or "").strip()

                identity: dict = {}
                probe = identities.get(server.id)
                if probe is not None:
                    if probe.get("ok"):
                        payload = probe.get("data") or {}
                        node_info = payload.get("node") or {}
                        identity = {
                            "ok": True,
                            "claimed": bool(payload.get("claimed")),
                            "node_key": node_info.get("node_key") or "",
                            "node_name": node_info.get("node_name") or "",
                            "realm_slug": payload.get("realm_slug") or "",
                            "realm_name": node_info.get("realm_name") or "",
                            "libraries": len(payload.get("libraries") or []),
                            "filtering": bool(node_info.get("filtering")),
                        }
                        # 多节点下最容易配错、又最难看出来的一件事：EA 自称的服与面板登记的不一致
                        reported = (identity["realm_slug"] or "").strip()
                        if reported and realm.slug and reported != realm.slug:
                            warnings.append(
                                f"这台 EA 自称属于「{identity['realm_name'] or reported}」（{reported}），"
                                f"但面板把它登记在「{realm.name}」——两边不一致，请核对 EA 的 REALM 或面板的归属服"
                            )
                        # 认领关系两边都要能对上：一边有一边没有，说明是「换了一台机器」或「忘了配」
                        # data["node_key"] 是 serialize() 打码后的「已配置」/""，比对必须用登记原值。
                        reported_key = (identity["node_key"] or "").strip()
                        warnings.extend(
                            _node_key_claim_warnings(
                                reported_key=reported_key,
                                claimed=identity["claimed"],
                                registered_key=registered_key,
                            )
                        )
                    else:
                        identity = {"ok": False, "error": str(probe.get("error") or "节点身份探测失败")}
                        warnings.append(f"节点身份探测失败：{identity['error']}")

                if server.kind == "ea":
                    # 单台 EA 不需要 NODE_KEY（面板自己扫就行）；多台才必须认得出来谁是谁
                    if not registered_key and realm_ea_count > 1:
                        warnings.append("这个服有多台 EA，但这台没配 NODE_KEY：面板无法把媒体库分配给它")
                    assigned = realm_libraries["by_node"].get(server.id, 0)
                    if realm_mounts["never_checked"]:
                        warnings.append("还没做过这台 EA 视角的挂载体检，无法说明它能不能碰到面板里配的存储")
                    elif realm_mounts["failed_count"]:
                        warnings.append(
                            f"有 {realm_mounts['failed_count']} / {realm_mounts['total']} 条挂载从这台 EA 不可达："
                            + "、".join(realm_mounts["unreachable"][:5])
                        )
                    elif realm_mounts["error"]:
                        warnings.append(f"挂载体检失败：{realm_mounts['error']}")
                else:
                    assigned = 0

                if data["last_check_ok"] is False:
                    warnings.append(f"最近一次连接失败：{data['last_check_message'] or '未知原因'}")
                elif data["last_check_ok"] is None:
                    warnings.append("还没有体检过这台入口（点「测试」或「体检」）")

                payload_row = {
                    **data,
                    "realm_slug": realm.slug,
                    "realm_is_default": realm.id == realms.legacy_realm_id(db),
                    "is_entry": server.kind in registry.LEGACY_PREFIX,
                    "is_current_entry": bool(data["is_active"]) and data["is_enabled"],
                    "identity": identity,
                    "mounts": realm_mounts if server.kind == "ea" else None,
                    "libraries_assigned": assigned,
                    "libraries_unassigned": realm_libraries["unassigned"] if server.kind == "ea" else 0,
                    "warnings": warnings,
                }
                rows.append(payload_row)
                totals["entry"] += 1
                totals["warnings"] += len(warnings)
                if data["last_check_ok"] is True:
                    totals["online"] += 1

            realm_libraries_total = realm_libraries["total"]
            totals["libraries"] += realm_libraries_total
            totals["libraries_unassigned"] += realm_libraries["unassigned"]

        realm_payloads = []
        for realm in realm_rows:
            data = realms.serialize(db, realm, with_stats=False)
            entry = _realm_entry(db, realm.id)
            realm_servers = [s for s in entry_rows if s.realm_id == realm.id]
            active = next((s for s in realm_servers if s.is_active and s.is_enabled), None)
            libraries = _library_counts(db, realm.id)
            warnings: list[str] = []
            if realm_servers and active is None:
                warnings.append("这个服还没有「当前使用」的入口：去下面那一行点「设为当前」")
            if not realm_servers:
                # 「面板自己出流」是单进程部署的正常冬态，不当成错误来吓人
                hint = (
                    "；当前由面板自己出流，单机单服部署可以保持这样"
                    if entry["mode"] == "panel"
                    else "；请添加一台后端服（EA）或已有的 Emby 服"
                )
                warnings.append("这个服还没有添加任何 Emby 入口（EA 或已有 Emby 服）" + hint)
            realm_payloads.append({
                **data,
                "entry": {
                    **entry,
                    "server_id": active.id if active else None,
                    "server_name": active.name if active else "",
                },
                "libraries": libraries,
                "mounts": _mount_summary(db, realm.id),
                "warnings": warnings,
            })

        return {
            "realm_id": scope_id,
            "active_realm_id": realms.active_realm_id(db),
            "live": bool(live),
            "realms": realm_payloads,
            "rows": rows,
            "totals": totals,
            "summary": registry.summary(db, scope_id),
            "kinds": registry.SERVER_KINDS,
        }

    def build() -> dict:
        """组装总览（同步；下放线程池）——整页要读几十次库，不能占事件循环。

        F-P1-22：用独立会话（函数结束即归还连接），不用已关闭的请求会话。
        """
        build_db = SessionLocal()
        try:
            return _build_impl(build_db)
        finally:
            build_db.close()

    return await run_in_threadpool(build)


async def _probe_identities(
    identity_targets: list[tuple[int, str]],
) -> dict[int, dict]:
    """F-P1-23：并发探测各 EA 节点身份（asyncio.gather）。

    原实现串行 ``for`` + 每次 8 秒超时，N 台掉线即 N×8 秒。
    单台超时仍 8 秒；另加总超时（8×台数，上限 60 秒）兜底；
    单台失败只记 error，不影响整页。
    """
    if not identity_targets:
        return {}

    async def _one(target_id: int, target_url: str) -> tuple[int, dict]:
        try:
            probe = await node_lib.fetch_node_identity(target_url, timeout=8.0)
        except Exception as exc:  # noqa: BLE001 — 单台失败不影响整页
            probe = {"ok": False, "error": str(exc)[:200]}
        return target_id, probe

    total_timeout = min(8.0 * len(identity_targets) + 5.0, 60.0)
    try:
        results = await asyncio.wait_for(
            asyncio.gather(*[_one(tid, url) for tid, url in identity_targets]),
            timeout=total_timeout,
        )
        return dict(results)
    except asyncio.TimeoutError:
        logger.warning("节点身份探测总超时（%.0f 秒），按全部失败处理", total_timeout)
        return {
            tid: {"ok": False, "error": f"探测总超时（>{total_timeout:.0f}s）"}
            for tid, _ in identity_targets
        }


@router.get("/summary")
def servers_summary(_: models.WebUser = Depends(get_current_admin), db: Session = Depends(get_db)):
    """只有统计结果：给「数据概览」的信息展示卡用"""
    return registry.summary(db)


@router.post("")
async def create_server(
    payload: ServerPayload,
    admin: models.WebUser = Depends(get_current_admin),
    db: Session = Depends(get_db),
):
    kind = _check_kind(payload.kind)
    url = _check_url(payload.url)
    name = payload.name.strip()
    admin_id = admin.id          # 纯值：下面有提交，之后再读 ORM 属性会在事件循环上回查

    # 内容自动化（MoviePilot / qB）可以选择「全服共用」：归属服留空，多服一起用一套
    shared = bool(payload.shared) and kind in registry.PUSH_TARGETS

    def insert() -> tuple[models.RemoteServer, int]:
        """唯一性检查 + 必填校验 + 落库（同步；下放线程池）

        顺序与拆分前一致：重名（409）先于「凭据没填全」（400）——两个都不满足时
        面板要看到的是「这个名字被占了」，而不是去补一台本来就不该新建的服务的凭据。
        """
        if _name_conflict(db, name):
            raise HTTPException(409, "已经有同名服务器了，换个名字吧")
        required_error = _missing_required(kind, url, payload.config or {})
        if required_error:
            raise HTTPException(400, required_error)
        server = models.RemoteServer(
            name=name, kind=kind, url=url,
            config=registry.json_dumps(registry.only_known(kind, payload.config or {})),
            is_enabled=payload.is_enabled, remark=payload.remark or "",
            realm_id=None if shared else (payload.realm_id or realms.active_realm_id(db)),
        )
        # F-P1-24 修复：只 flush 不提交，与探测、审计同事务。
        # 原先这里 commit 后探测抛错会留下"已入库但无审计、无可达状态"的脏行。
        # flush 后 id 已可用；probe_and_store 内部 store() 会提交（含探测结论），
        # 探测抛错则整个事务回滚，服务器行不会残留。
        db.add(server)
        db.flush()
        return server, int(server.id)

    server, server_id = await run_in_threadpool(insert)

    result = await registry.probe_and_store(db, server)
    _audit(db, admin_id, "create_server", "server", server_id,
           {"kind": kind, "ok": result.get("ok"), "url": url})
    await run_in_threadpool(db.commit)
    data = await run_in_threadpool(registry.serialize, db, server)
    return {"success": True, "server": data, "probe": result}


@router.put("/{server_id}")
async def update_server(
    server_id: int,
    payload: ServerPayload,
    admin: models.WebUser = Depends(get_current_admin),
    db: Session = Depends(get_db),
):
    kind = _check_kind(payload.kind)
    url = _check_url(payload.url)
    name = payload.name.strip()
    admin_id = admin.id          # 纯值：下面有提交，之后再读 ORM 属性会在事件循环上回查

    def apply() -> models.RemoteServer:
        """校验 + 落库（同步；下放线程池）"""
        server = registry.get_server(db, server_id)
        if not server:
            raise HTTPException(404, "服务器不存在")
        if _name_conflict(db, name, exclude_id=server_id):
            raise HTTPException(409, "已经有同名服务器了，换个名字吧")

        kind_changed = kind != server.kind
        # F-P1-25：记录旧 kind，换类型时先对旧 kind deactivate_legacy。
        # 原代码在 sync_entry 里用改后的 server.kind 调 deactivate_legacy，
        # 换类型后旧类型的配置键未被收回，is_active 沿用旧值。
        old_kind = server.kind
        server.name = name
        server.kind = kind
        server.url = url
        server.is_enabled = payload.is_enabled
        server.remark = payload.remark or ""
        if payload.shared and kind in registry.PUSH_TARGETS:
            # 内容自动化改成「全服共用」
            server.realm_id = None
        elif payload.realm_id:
            if not realms.get_realm(db, payload.realm_id):
                raise HTTPException(400, f"服不存在: #{payload.realm_id}")
            server.realm_id = payload.realm_id
        # 换类型时旧类型的字段不再适用，直接以新配置为准
        server.config = registry.json_dumps(
            registry.only_known(kind, payload.config or {})
            if kind_changed else registry.merge_config(server, payload.config or {})
        )
        # F-P1-25：换类型时先收回旧类型的 legacy 配置键，并重置激活状态
        if kind_changed and old_kind in registry.LEGACY_PREFIX:
            registry.deactivate_legacy(db, old_kind)
            server.is_active = False
        db.commit()
        return server

    server = await run_in_threadpool(apply)

    result = await registry.probe_and_store(db, server)

    def sync_entry() -> None:
        """收回 / 同步旧配置键（同步；下放线程池）

        停用 / 类型变更后旧配置键要跟着收回，否则会出现「面板说没接入、网关却还在跑」。
        """
        if not server.is_enabled and server.is_active and server.kind in registry.LEGACY_PREFIX:
            server.is_active = False
            registry.deactivate_legacy(db, server.kind)
            db.commit()
        elif server.is_active and server.kind in registry.LEGACY_PREFIX:
            registry.sync_legacy(db, server, reachable=bool(result.get("ok")))
            db.commit()

    await run_in_threadpool(sync_entry)

    _audit(db, admin_id, "update_server", "server", server_id,
           {"kind": kind, "ok": result.get("ok")})
    await run_in_threadpool(db.commit)
    data = await run_in_threadpool(registry.serialize, db, server)
    return {"success": True, "server": data, "probe": result}


@router.post("/test")
async def test_unsaved(
    payload: ProbePayload,
    _: models.WebUser = Depends(get_current_admin),
    db: Session = Depends(get_db),
):
    kind = _check_kind(payload.kind)
    url = _check_url(payload.url)
    config = await run_in_threadpool(
        _effective_config, kind, payload.config or {}, payload.server_id, db)
    required_error = _missing_required(kind, url, config)
    if required_error:
        return {"ok": False, "message": required_error}
    return await registry.probe_server(kind, url, config)


@router.post("/{server_id}/test")
async def test_server(
    server_id: int,
    _: models.WebUser = Depends(get_current_admin),
    db: Session = Depends(get_db),
):
    server = await run_in_threadpool(registry.get_server, db, server_id)
    if not server:
        raise HTTPException(404, "服务器不存在")
    return await registry.probe_and_store(db, server)


@router.post("/{server_id}/activate")
async def activate_server(
    server_id: int,
    admin: models.WebUser = Depends(get_current_admin),
    db: Session = Depends(get_db),
):
    """设为该类型的「当前使用」

    激活前会**重新体检一次**：面板不能拿一份几小时前的结论去切换全站入口。
    """
    admin_id = admin.id          # 纯值：下面有提交，之后再读 ORM 属性会在事件循环上回查

    def load() -> tuple[Optional[models.RemoteServer], str, str, Optional[int]]:
        """取这一行，并顺手读出后面要用的纯值（同步；下放线程池）"""
        row = registry.get_server(db, server_id)
        if row is None:
            return None, "", "", None
        return row, row.kind, row.url, row.realm_id

    server, kind, url, realm_id = await run_in_threadpool(load)
    if server is None:
        raise HTTPException(404, "服务器不存在")

    probe = await registry.probe_and_store(db, server)
    if not probe.get("ok"):
        _audit(db, admin_id, "activate_server_failed", "server", server_id,
               {"kind": kind, "message": str(probe.get("message") or "")[:200]})
        await run_in_threadpool(db.commit)
        return {"success": False, "activated": False, "probe": probe,
                "message": str(probe.get("message") or "连接测试未通过")}

    result = await run_in_threadpool(registry.activate, db, server, reachable=True)
    if not result.get("ok"):
        return {"success": False, "activated": False, "probe": probe,
                "message": str(result.get("message") or "无法设为当前使用")}

    # EA 一旦成为出流节点，就顺手把「EA 视角的挂载体检」拉一次：
    # 挂载里的本机路径 / rclone 地址是跟着那台机器走的，这里不查就得等播放 502 才发现。
    mounts_health = None
    if kind == "ea":
        from backend.api.emby_servers import refresh_mount_health

        mounts_health = await refresh_mount_health(db, url, realm_id)

    _audit(db, admin_id, "activate_server", "server", server_id,
           {"kind": kind, "mode": result.get("mode"),
            "mount_health_ok": None if mounts_health is None else mounts_health.get("ok")})
    await run_in_threadpool(db.commit)
    return {
        "success": True,
        "activated": bool(result.get("activatable")),
        "configurable": bool(result.get("activatable")),
        "mode": result.get("mode"),
        "message": result.get("message"),
        "probe": probe,
        "mounts_health": mounts_health,
        "server": await run_in_threadpool(registry.serialize, db, server),
    }


@router.post("/{server_id}/toggle")
def toggle_server(
    server_id: int,
    admin: models.WebUser = Depends(get_current_admin),
    db: Session = Depends(get_db),
):
    server = registry.get_server(db, server_id)
    if not server:
        raise HTTPException(404, "服务器不存在")
    server.is_enabled = not bool(server.is_enabled)
    if not server.is_enabled:
        was_active = bool(server.is_active)
        server.is_active = False
        if was_active and server.kind in registry.LEGACY_PREFIX:
            registry.deactivate_legacy(db, server.kind)
    db.commit()
    _audit(db, admin, "toggle_server", "server", server.id,
           {"kind": server.kind, "enabled": bool(server.is_enabled)})
    db.commit()
    return {"success": True, "server": registry.serialize(db, server), "summary": registry.summary(db)}


@router.delete("/{server_id}")
def delete_server(
    server_id: int,
    admin: models.WebUser = Depends(get_current_admin),
    db: Session = Depends(get_db),
):
    server = registry.get_server(db, server_id)
    if not server:
        raise HTTPException(404, "服务器不存在")
    kind = server.kind
    was_active = bool(server.is_active)
    name = server.name
    db.delete(server)
    db.commit()
    # 删掉的正好是「当前使用」时，必须把旧配置键收回，不能让面板继续指着一台不存在的服务
    if was_active and kind in registry.LEGACY_PREFIX:
        registry.deactivate_legacy(db, kind)
        db.commit()
    _audit(db, admin, "delete_server", "server", server_id, {"kind": kind, "name": name})
    db.commit()
    return {"success": True, "summary": registry.summary(db)}


# ==================== 每台 EA 一份 rclone.conf ====================
# rclone.conf 里全是 token / secret，所以：
#   1) 只在「添加/编辑服务器」弹窗里由用户自己粘贴，服务器级配一次；
#   2) **只返 remote 名**（带尾冒号），永不回明文；
#   3) 仅超级管理员可读写——它等同一份凭据文件。

class RcloneConfPayload(BaseModel):
    conf: str = Field(default="", max_length=200_000)


def _require_super(user) -> None:
    from backend import admin_roles

    if not admin_roles.is_super(user):
        raise HTTPException(
            status_code=403,
            detail="rclone.conf 含网盘凭据，仅超级管理员可读写（当前角色：{}）".format(
                admin_roles.role_label(admin_roles.role_of(user))),
        )


def _ea_server_or_404(db: Session, server_id: int):
    server = registry.get_server(db, server_id)
    if not server:
        raise HTTPException(404, "服务器不存在")
    if server.kind != "ea":
        raise HTTPException(
            400, f"「{server.name}」不是 EA（{server.kind}），rclone.conf 只对 EA 有意义")
    return server


@router.get("/{server_id}/rclone-conf")
async def get_server_rclone_conf(
    server_id: int,
    admin: models.WebUser = Depends(get_current_admin),
    db: Session = Depends(get_db),
):
    """这台 EA 的 rclone.conf：只返 remote 名，**不返明文**"""
    _require_super(admin)
    server = _ea_server_or_404(db, server_id)
    path = mount_rclone.conf_path_for_server(server.id)
    text = await run_in_threadpool(mount_rclone.read_rclone_conf, path)
    try:
        remotes = mount_rclone.conf_remote_names(text)
    except mount_rclone.mount_lib.MountError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {
        "path": path,
        "server_id": server.id,
        "server_name": server.name,
        "configured": bool(remotes),
        "remotes": remotes,
        "total": len(remotes),
    }


@router.post("/{server_id}/rclone-conf")
async def save_server_rclone_conf(
    server_id: int,
    req: RcloneConfPayload,
    admin: models.WebUser = Depends(get_current_admin),
    db: Session = Depends(get_db),
):
    """保存这台 EA 的 rclone.conf（整份替换，写到 ``rclone-<id>.conf``，权限 600）"""
    _require_super(admin)
    server = _ea_server_or_404(db, server_id)
    path = mount_rclone.conf_path_for_server(server.id)
    try:
        written = await run_in_threadpool(mount_rclone.write_rclone_conf, req.conf, path)
        remotes = mount_rclone.conf_remote_names(req.conf)
    except mount_rclone.mount_lib.MountError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    # 落审计 + 提交是同步 DB 操作，不能在 async 路由的事件循环里直接调
    await run_in_threadpool(_audit, db, admin, "save_server_rclone_conf", "remote_server",
                            server_id, {"remotes": len(remotes)})
    await run_in_threadpool(db.commit)
    return {"success": True, "path": written, "remotes": remotes, "total": len(remotes)}


@router.post("/mounts/health")
async def refresh_mount_health_now(
    admin: models.WebUser = Depends(get_current_admin),
    db: Session = Depends(get_db),
):
    """手动重拉一次当前服的「EA 视角的挂载体检」（服务器页的「EA 体检」按钮）"""
    admin_id = admin.id          # 纯值：下面有提交，之后再读 ORM 属性会在事件循环上回查

    def current_ea() -> Optional[dict]:
        """当前服的 EA 入口配置（同步；下放线程池）"""
        realm_id = realms.active_realm_id(db)
        cfg = registry.active_config(db, "ea", realm_id)
        return None if not cfg else {**cfg, "realm_id": realm_id}

    ea = await run_in_threadpool(current_ea)
    if not ea:
        raise HTTPException(400, "当前服还没有可用的后端服（EA）：请先添加并测试连接")
    from backend.api.emby_servers import refresh_mount_health

    health = await refresh_mount_health(db, ea["url"], ea["realm_id"])
    _audit(db, admin_id, "refresh_ea_mounts_health", "server", ea["id"],
           {"ok": health.get("ok")})
    await run_in_threadpool(db.commit)
    return {"success": bool(health.get("ok")), "health": health, "server": ea["name"]}


# ==================== 媒体运维（服务器维度） ====================

def _ops_server(db: Session, server_id: int):
    """取出这台服务器，并挡住「媒体运维只对后端服（EA）开放」

    EA 才是扫内容的机器：外部 Emby / MoviePilot / qBittorrent 都不在
    ``emby_libraries.node_id`` 的另一端，给它们开一个空的运维面板只会让人以为配错了。
    """
    server = registry.get_server(db, server_id)
    if not server:
        raise HTTPException(404, "服务器不存在")
    if server.kind != "ea":
        raise HTTPException(400, "媒体运维只对后端服（EA）开放：它才是扫描与出流内容的机器")
    return server


@router.get("/{server_id}/ops")
def server_ops_snapshot(
    server_id: int,
    _: models.WebUser = Depends(get_current_admin),
    db: Session = Depends(get_db),
    include_unassigned: bool = False,
    runs_limit: int = server_ops.DEFAULT_RUNS,
):
    """这台节点的媒体运维快照（整服扫描历史 + 扫描任务 + 刮削补全 + 内容转交）

    纯读：按库的配置不在这里改，这里只回答「这台机器现在跑得怎么样」。
    ``include_unassigned=true`` 把同服里还没分配节点的库也算进来（默认不算——
    那些库由面板扫，不属于这台节点）。
    """
    server = _ops_server(db, server_id)
    snapshot = server_ops.ops_snapshot(
        db, server, include_unassigned=include_unassigned, runs_limit=runs_limit)
    snapshot["server"] = registry.serialize(db, server)
    return snapshot


class ServerOpsScanRequest(BaseModel):
    """一键扫描的范围（body 而不是 query：这是一个有语义的写动作）"""

    include_unassigned: bool = Field(
        default=False,
        description="是否连同服里还没分配节点的库一起扫（默认只扫归这台节点的）",
    )


@router.post("/{server_id}/ops/scan")
async def server_ops_scan(
    server_id: int,
    req: Optional[ServerOpsScanRequest] = None,
    admin: models.WebUser = Depends(get_current_admin),
    db: Session = Depends(get_db),
):
    """一键把这台节点负责的库推入扫描队列（扫描 → 入库 → 刮削流水线）

    与逐库「扫描」同一套机制：面板能扫的本地入队，归其它节点管的转发过去
    （只有那台机器碰得到文件），已在队列 / 正在扫的不重复推。所以这里不新建
    任何执行路径，只是把「按库点」换成「按节点点」。
    """
    include_unassigned = bool(req.include_unassigned) if req is not None else False
    admin_id = admin.id          # 纯值：下面有提交，之后再读 ORM 属性会在事件循环上回查

    def load():
        return _ops_server(db, server_id)

    server = await run_in_threadpool(load)
    server_name = server.name

    plan = await run_in_threadpool(server_ops.scan_plan, db, server, include_unassigned)

    # 转发是网络调用，必须 await（留在事件循环上）；本地入队是同步 SQLAlchemy，
    # 已经在上面的线程池任务里做完了。
    forwarded: list[dict] = []
    failed: list[dict] = []
    for entry in plan["forward"]:
        result = await node_lib.push_scan(entry["url"], entry["id"])
        row = {"id": entry["id"], "name": entry["name"],
               "node_id": entry["node_id"], "node_name": entry["node_name"]}
        if result.get("ok"):
            forwarded.append(row)
        else:
            failed.append({**row, "error": str(result.get("error") or "转发失败")[:200]})

    _audit(db, admin_id, "server_ops_scan", "server", server_id,
           {"name": server_name, "include_unassigned": include_unassigned,
            "queued": len(plan["queued"]), "already": len(plan["already"]),
            "forwarded": len(forwarded), "failed": len(failed),
            "skipped": len(plan["skipped"])})
    await run_in_threadpool(db.commit)

    return {
        "success": not failed,
        "server_id": server_id,
        "server_name": server_name,
        "queued": plan["queued"],
        "already": plan["already"],
        "forwarded": forwarded,
        "failed": failed,
        "skipped": plan["skipped"],
        "message": _ops_scan_message(plan, forwarded, failed),
    }


def _ops_scan_message(plan: dict, forwarded: list, failed: list) -> str:
    """一句话说清这一键到底做了什么（面板直接显示，不要自己拼）"""
    parts: list[str] = []
    if plan["queued"]:
        started = len([row for row in plan["queued"] if row.get("message")])
        parts.append(f"已推入扫描队列 {len(plan['queued'])} 个库"
                     + (f"（其中 {started} 个已开始）" if started else ""))
    if plan["already"]:
        parts.append(f"{len(plan['already'])} 个已在队列或正在扫")
    if forwarded:
        parts.append(f"{len(forwarded)} 个已转发给归属节点")
    if failed:
        parts.append(f"{len(failed)} 个转发失败")
    if plan["skipped"]:
        parts.append(f"{len(plan['skipped'])} 个被跳过（停用 / 虚拟库 / 超出单次上限）")
    return "；".join(parts) or "没有可扫的启用库"


__all__ = ["router"]
