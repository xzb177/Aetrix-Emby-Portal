"""播放节点与「服」的内容隔离：多台 EA 同时出流

## 为什么需要它

一台机器只能碰到**自己那台上的存储**（本机目录、rclone rc 地址、rclone 可执行文件、
网盘凭据所在的网络位置…）。所以「一个后端服可以部署到多台服务器、多台同时出流」
不是一个开关，而是要回答四个问题：

1. **我是谁** —— EA 用环境变量 ``NODE_KEY`` 认领面板「服务器」里的一条 EA 记录
   （``remote_servers.node_key``）。没配或认领不到就**不过滤**，单节点部署行为一字不变。
2. **我属于哪个服** —— ``REALM``（服标识或 id），或直接由认领到的那条服务器记录的
   ``realm_id`` 决定。服定了内容边界：**只提供本服的媒体库，只认本服的订阅**。
3. **哪些库归我这台机器** —— ``emby_libraries.node_id``。**NULL = 未分配**：所有节点
   都看得见，由面板（EM）扫描——于是老部署、以及「刚加完节点还没来得及分配」的过渡期
   都不会出现「什么都看不到」。已分配的库由**归属节点**扫描（只有那台机器碰得到文件），
   面板点「扫描」时会转发过去（``node_router`` + ``portal.scan_library_endpoint``）。
4. **谁在放** —— 客户端连接哪台 EA，就看到哪台的库；直连别的节点的条目一律 404。

可见性与扫描归属用**同一份规则**，所以不会出现「一台 EA 显示出自己根本播不了的条目」。

## 过滤是怎么挂上去的

不逐个端点改查询（漏一个就是内容泄漏），而是用 SQLAlchemy 的 ``do_orm_execute`` 给
``Library`` / ``MediaItem`` 的**所有 ORM 查询**统一加上条件（``install_scope``）。

- **EA 进程**：按「服」+「本节点」过滤（``role="ea"``）；
- **面板（EM）自带的网关**：只按「服」过滤，且默认就是**默认服**——一体化部署升级上来
  行为不变，多服的内容由各自的 EA 出流（可用 ``EM_GATEWAY_REALM`` 指定别的服）；
- 两者都没配置时**完全不过滤**，单服单机部署零影响。

写入路径（扫描、建库、改挂载）不受影响：``do_orm_execute`` 只作用于 SELECT。
"""
from __future__ import annotations

import logging
import os
import threading
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import and_, event, or_, select
from sqlalchemy.orm import Session, with_loader_criteria

from backend import models, realms
from backend.database import get_db
from backend.emby_server import models as em
from backend.emby_server.mount_health import PANEL_KEY_HEADER, require_panel_key
from backend.emby_server.env_util import env_float

logger = logging.getLogger(__name__)

# EA 认领自己身份用的环境变量名（兼容两种写法）
NODE_KEY_ENVS = ("NODE_KEY", "EMBY_NODE_KEY")
# 面板自带网关（一体化部署）默认服务哪个服：留空 = 默认服
GATEWAY_REALM_ENV = "EM_GATEWAY_REALM"

# 进程内身份缓存：节点身份与服在运行期不变，没必要每个请求都查一次
_self_cache: dict = {"key": None, "node_id": None, "realm_id": None, "resolved": False}
# 已安装的可见性作用域（None = 尚未安装/不过滤）
_scope: dict = {"active": False, "role": "", "realm_id": None, "node_id": None}
_scope_lock = threading.Lock()


# ==================== 本进程身份 ====================

def configured_key() -> str:
    """本进程配置的节点标识（没配返回空串）"""
    for name in NODE_KEY_ENVS:
        value = (os.getenv(name) or "").strip()
        if value:
            return value
    return ""


def configured_realm() -> str:
    """本进程配置的服标识（没配返回空串）"""
    return (os.getenv("REALM") or os.getenv("SERVER_REALM") or "").strip()


def reset_cache() -> None:
    """测试或配置变更后清缓存"""
    _self_cache.update({"key": None, "node_id": None, "realm_id": None, "resolved": False})


def self_node(db: Session) -> Optional[models.RemoteServer]:
    """本进程是哪台节点（没配 NODE_KEY 或面板里找不到时返回 None = 不过滤节点）"""
    key = configured_key()
    if not key:
        return None
    if _self_cache["resolved"] and _self_cache["key"] == key:
        node_id = _self_cache["node_id"]
        if node_id is None:
            return None
        return db.query(models.RemoteServer).filter(models.RemoteServer.id == node_id).first()
    node = (db.query(models.RemoteServer)
            .filter(models.RemoteServer.node_key == key)
            .first())
    _self_cache.update({
        "key": key,
        "node_id": node.id if node else None,
        "realm_id": node.realm_id if node else None,
        "resolved": True,
    })
    if node is None:
        logger.warning("NODE_KEY=%s 在「服务器」里没有对应的记录：本进程不做节点过滤（所有库都可见）", key)
    return node


def self_node_id(db: Session) -> Optional[int]:
    node = self_node(db)
    return node.id if node else None


def self_realm(db: Session) -> Optional[models.ServerRealm]:
    """本进程服务哪个服

    优先级：认领到的服务器记录的 ``realm_id`` → 环境变量 ``REALM``。
    都没有则返回 None（= 不分服，兼容单服部署）。
    """
    node = self_node(db)
    if node is not None and node.realm_id:
        return realms.get_realm(db, node.realm_id)
    return realms.realm_from_env(db)


def self_realm_id(db: Session) -> Optional[int]:
    realm = self_realm(db)
    return realm.id if realm else None


def describe(db: Session) -> dict:
    """本进程的节点 / 服身份（供 /api/health、节点端点与排查用）"""
    node = self_node(db)
    realm = self_realm(db)
    return {
        "service_role": _scope.get("role") or "",
        "node_key": configured_key(),
        "node_id": node.id if node else None,
        "node_name": node.name if node else "",
        "realm_id": realm.id if realm else None,
        "realm_slug": realm.slug if realm else "",
        "realm_name": realm.name if realm else "",
        "filtering": bool(_scope.get("active")),
    }


# ==================== EA 启动时认领身份 ====================

def register_self(db: Session, *, url: str = "") -> Optional[models.RemoteServer]:
    """按 NODE_KEY 认领 / 自动登记本机的服务器记录，并把它归到 ``REALM``

    - 已有同 ``node_key`` 的记录：只补空地址与空归属（不动管理员填过的名字与地址）；
    - 没有：自动建一条（名字形如「EA 节点 <key>」），方便刚部署完就能在面板里看到它、
      直接把某些媒体库分配过来。

    没配 NODE_KEY 时什么都不做（单节点部署不产生多余记录）。
    """
    key = configured_key()
    if not key:
        return None
    node = db.query(models.RemoteServer).filter(models.RemoteServer.node_key == key).first()
    realm = realms.realm_from_env(db)
    if node is None:
        node = models.RemoteServer(
            name=registry_unique_name(db, f"EA 节点 {key}"),
            kind="ea", node_key=key, realm_id=realm.id if realm else None,
            url=(url or "").strip().rstrip("/") or f"http://127.0.0.1:{os.getenv('EMBY_API_PORT', '8001')}",
            is_enabled=True, is_active=False,
            remark="由 EA 启动时自动登记（在「媒体库」页把库分配给它即可）",
        )
        db.add(node)
        db.commit()
        db.refresh(node)
        logger.info("已自动登记为播放节点：%s（node_key=%s，服=%s）；请到「服管理 / 媒体库」页把库分配给它",
                    node.name, key, realm.name if realm else "未指定")
        reset_cache()
        return node
    changed = False
    if not (node.url or "").strip() and url:
        node.url = url.strip().rstrip("/")
        changed = True
    if node.kind != "ea":
        node.kind = "ea"
        changed = True
    if realm and not node.realm_id:
        node.realm_id = realm.id
        changed = True
    if changed:
        db.commit()
    reset_cache()
    logger.info("本进程作为播放节点运行：%s（node_key=%s，服=%s）",
                node.name, key, realm.name if realm else "未指定")
    return node


def registry_unique_name(db: Session, base: str) -> str:
    from backend import servers as registry

    return registry.unique_name(db, base)


# ==================== 可见性（直接调用的版本）====================

def visible_library_ids(db: Session, realm_id: Optional[int] = None,
                        node_id: Optional[int] = None) -> Optional[set[int]]:
    """本进程能看到的媒体库 id 集合；返回 ``None`` 表示**不过滤**

    规则同 ``install_scope``：服边界（``realm_id`` 为空的库对所有服可见）+
    节点边界（``node_id`` 为空的库对所有节点可见）。
    """
    if realm_id is None and node_id is None:
        realm_id = self_realm_id(db)
        node_id = self_node_id(db)
    if realm_id is None and node_id is None:
        return None
    query = db.query(em.Library.id)
    if realm_id is not None:
        query = query.filter(or_(em.Library.realm_id.is_(None), em.Library.realm_id == realm_id))
    if node_id is not None:
        query = query.filter(or_(em.Library.node_id.is_(None), em.Library.node_id == node_id))
    return {row[0] for row in query.all()}


def library_owner(db: Session, library) -> Optional[models.RemoteServer]:
    """这条库归哪台节点（未分配返回 None）"""
    node_id = getattr(library, "node_id", None)
    if not node_id:
        return None
    return db.query(models.RemoteServer).filter(models.RemoteServer.id == node_id).first()


def library_visible(db: Session, library_id: Optional[int], node_id: Optional[int] = None,
                    realm_id: Optional[int] = None) -> bool:
    if library_id is None:
        return True
    visible = visible_library_ids(db, realm_id, node_id)
    if visible is None:
        return True
    return library_id in visible


def item_visible(db: Session, item, node_id: Optional[int] = None,
                 realm_id: Optional[int] = None) -> bool:
    """条目在本节点可见吗（按它所属媒体库判断）"""
    if item is None:
        return False
    return library_visible(db, getattr(item, "library_id", None), node_id, realm_id)


# ==================== 可见性（挂到所有 ORM 查询上）====================

def _library_condition(realm_id: Optional[int], node_id: Optional[int]):
    clauses = []
    if realm_id is not None:
        clauses.append(or_(em.Library.realm_id.is_(None), em.Library.realm_id == realm_id))
    if node_id is not None:
        clauses.append(or_(em.Library.node_id.is_(None), em.Library.node_id == node_id))
    if not clauses:
        return None
    return and_(*clauses) if len(clauses) > 1 else clauses[0]


def install_scope(role: str) -> dict:
    """把「服 + 节点」可见性挂到本进程的所有 ORM 查询上（幂等，可重复调用）

    ``role``：
    - ``"ea"``：分离部署的网关，按 ``REALM`` + ``NODE_KEY`` 过滤；
    - ``"em"``：面板自带的网关，只按「服」过滤（默认服），节点归属无从谈起。

    不满足条件（既没有服也没有节点身份）时不安装任何过滤——单服单机部署零影响。
    """
    with _scope_lock:
        return _install_scope_locked(role)


def _install_scope_locked(role: str) -> dict:
    from backend.database import SessionLocal

    db = SessionLocal()
    try:
        if role == "ea":
            realm_id = self_realm_id(db)
            node_id = self_node_id(db)
        else:
            node_id = None
            ref = (os.getenv(GATEWAY_REALM_ENV) or "").strip()
            realm = realms.resolve(db, ref) if ref else realms.legacy_realm(db)
            realm_id = realm.id if realm else None
    finally:
        db.close()

    if realm_id is None and node_id is None:
        _scope.update({"active": False, "role": role, "realm_id": None, "node_id": None})
        logger.info("未配置服/节点身份：不过滤内容（单服单机部署）")
        return dict(_scope)

    _scope.update({"active": True, "role": role, "realm_id": realm_id, "node_id": node_id})
    if not getattr(install_scope, "_installed", False):
        event.listen(Session, "do_orm_execute", _apply_scope)
        install_scope._installed = True  # type: ignore[attr-defined]
    logger.info("内容可见性已生效（role=%s，服=%s，节点=%s）：本进程只提供该范围内的媒体库与条目",
                role, realm_id, node_id)
    return dict(_scope)


def _apply_scope(state) -> None:
    """给所有 ORM SELECT 加上内容可见性条件（写入与列/关系懒加载不受影响）"""
    scope = _scope
    if not scope.get("active"):
        return
    if not state.is_select or state.is_column_load or state.is_relationship_load:
        return
    condition = _library_condition(scope.get("realm_id"), scope.get("node_id"))
    if condition is None:
        return
    options = [with_loader_criteria(em.Library, condition)]
    options.append(with_loader_criteria(
        em.MediaItem,
        or_(em.MediaItem.library_id.is_(None),
            em.MediaItem.library_id.in_(select(em.Library.id).where(condition))),
    ))
    state.statement = state.statement.options(*options)


# ==================== 面板 ↔ 节点 的内部端点 ====================

node_router = APIRouter(prefix="/api/admin/nodes", tags=["节点（多机播放）"])

# EM 调节点的超时：体检要逐条挂载走一遍，给足余量
NODE_IO_TIMEOUT = env_float("NODE_IO_TIMEOUT", 60.0)


def _node_libraries(db: Session, realm_id: Optional[int], node_id: Optional[int]) -> list[dict]:
    """本节点负责的库（与 ``visible_library_ids`` 同一份规则：未标注/未分配的对谁都可见）"""
    query = db.query(em.Library)
    if realm_id is not None:
        query = query.filter(or_(em.Library.realm_id.is_(None), em.Library.realm_id == realm_id))
    if node_id is not None:
        query = query.filter(or_(em.Library.node_id.is_(None), em.Library.node_id == node_id))
    return [
        {
            "id": lib.id,
            "name": lib.name,
            "guid": lib.guid,
            "realm_id": lib.realm_id,
            "node_id": lib.node_id,
            "is_enabled": bool(lib.is_enabled),
            "item_count": lib.item_count,
            "mount_ids": [int(x) for x in (lib.mount_ids or "").split(",") if x.strip().isdigit()],
            "paths": [p for p in (lib.paths or "").split(",") if p],
        }
        for lib in query.order_by(em.Library.id).all()
    ]


@node_router.get("/me", dependencies=[Depends(require_panel_key)])
def node_me(db: Session = Depends(get_db)):
    """这台节点是谁、属于哪个服、负责哪些库（面板用它核对 REALM / NODE_KEY 配置）

    鉴权走节点签名 / 节点密钥（``backend/node_auth.py``），与挂载体检同一套：
    EA 上不存在后台会话，这条端点只该由 EM 调用。
    """
    realm_id = self_realm_id(db)
    node_id = self_node_id(db)
    return {
        "service": "ea",
        "node": describe(db),
        "claimed": node_id is not None,
        "realm_id": realm_id,
        "realm_slug": (realms.get_realm(db, realm_id).slug if realm_id else ""),
        "node_key": configured_key(),
        "libraries": _node_libraries(db, realm_id, node_id),
    }


@node_router.get("/libraries", dependencies=[Depends(require_panel_key)])
def node_libraries(db: Session = Depends(get_db)):
    realm_id = self_realm_id(db)
    return {"node": describe(db), "libraries": _node_libraries(db, realm_id, self_node_id(db))}


@node_router.post("/libraries/{library_id}/scan", dependencies=[Depends(require_panel_key)])
def node_scan_library(library_id: int, full: bool = False,
                      db: Session = Depends(get_db)):
    """由归属节点执行一次扫描（面板点「扫描」时转发过来）

    只有能碰到文件的那台机器扫得动，所以已分配的库必须走这条路径。
    ``full=true`` 是面板转发过来的「全量扫描」，只影响这一轮。
    """
    lib = db.query(em.Library).filter(em.Library.id == library_id).first()
    if not lib:
        raise HTTPException(404, "媒体库不存在")
    node_id = self_node_id(db)
    if node_id is not None and lib.node_id not in (None, node_id):
        raise HTTPException(409, f"这个库归另一台节点负责（node_id={lib.node_id}），不该由本节点扫描")
    realm_id = self_realm_id(db)
    if realm_id is not None and lib.realm_id not in (None, realm_id):
        raise HTTPException(409, f"这个库属于另一个服（realm_id={lib.realm_id}），不该由本节点扫描")
    try:
        start_local_scan(db, lib, force_full=bool(full))
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    return {"success": True, "message": "扫描已在本节点启动", "library_id": library_id,
            "full": bool(full), "node": describe(db)}


def start_local_scan(db: Session, lib, force_full: bool = False) -> dict:
    """在本进程里启动一次后台扫描（与面板的扫描按钮同一条路径）

    v2.27.0 起走扫描队列：多台节点上同时点、或同一个远程挂载被多个库引用时，
    由队列按挂载串行化（节点被转发扫描的那一台，队列就是它的）。
    """
    from backend.emby_server import scan_queue

    # 这个库归本节点：面板点「扫描」时会转发到这台机器，流水里标成 node
    result = scan_queue.enqueue(lib, trigger="node", force_full=bool(force_full))
    task = result["task"]
    if not result["created"] and task.get("state") == "running":
        raise ValueError("该媒体库正在扫描中")
    return {"library_id": lib.id, "scrape_policy": lib.scrape_policy or "missing_only",
            "task": task, "queued": bool(result["created"]), "position": task.get("position")}


# ==================== EM 侧：调用节点 ====================

async def _get_node(url: str, path: str, timeout: float = NODE_IO_TIMEOUT) -> dict:
    import httpx

    from backend.node_auth import signed_headers

    target = f"{(url or '').rstrip('/')}{path}"
    headers = signed_headers("GET", target)
    if not headers:
        return {"ok": False, "error": "EM 未配置 SECRET_KEY / NODE_SHARED_SECRET，无法向节点证明身份"}
    try:
        # S3：只发签名、不跟随重定向
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=False) as client:
            resp = await client.get(target, headers=headers)
    except Exception as exc:  # noqa: BLE001 — 网络问题只报告，不影响主流程
        return {"ok": False, "error": f"无法连接节点: {exc}"}
    if 300 <= resp.status_code < 400:
        return {"ok": False, "error": f"节点返回重定向 HTTP {resp.status_code}（为安全不跟随，请填写最终地址）"}
    if resp.status_code == 401:
        return {"ok": False, "error": "节点拒绝了签名（两端 NODE_SHARED_SECRET / SECRET_KEY 必须一致，且节点需升级）"}
    if resp.status_code >= 400:
        return {"ok": False, "error": f"节点返回 HTTP {resp.status_code}"}
    try:
        data = resp.json()
    except ValueError:
        return {"ok": False, "error": "节点返回的不是 JSON（地址可能指向别的服务）"}
    if data.get("service") != "ea":
        return {"ok": False, "error": "该地址不是 EA（缺少 service=ea 标识）"}
    return {"ok": True, "data": data, "checked_at": datetime.now().isoformat()}


async def fetch_node_identity(url: str, timeout: float = 12.0) -> dict:
    """拉一次节点身份（服 / 节点标识 / 负责的库）"""
    return await _get_node(url, "/api/admin/nodes/me", timeout)


async def push_scan(url: str, library_id: int, timeout: float = 20.0,
                    full: bool = False) -> dict:
    """让节点扫描它负责的库（EM 侧的「扫描」路由转发用）

    ``full=True`` 把「全量扫描」转发过去（参数名与本地扫描接口一致）。
    """
    import httpx

    from backend.node_auth import signed_headers

    target = f"{(url or '').rstrip('/')}/api/admin/nodes/libraries/{int(library_id)}/scan"
    headers = signed_headers("POST", target)
    if not headers:
        return {"ok": False, "error": "EM 未配置 SECRET_KEY / NODE_SHARED_SECRET，无法让节点执行扫描"}
    params = {"full": "true"} if full else None
    try:
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=False) as client:
            resp = await client.post(target, headers=headers, params=params)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"无法连接节点: {exc}"}
    if 300 <= resp.status_code < 400:
        return {"ok": False, "error": f"节点返回重定向 HTTP {resp.status_code}（为安全不跟随）"}
    if resp.status_code == 401:
        return {"ok": False, "error": "节点拒绝了签名（两端 NODE_SHARED_SECRET / SECRET_KEY 必须一致，且节点需升级）"}
    if resp.status_code >= 400:
        try:
            detail = (resp.json() or {}).get("detail")
        except ValueError:
            detail = None
        return {"ok": False, "error": str(detail or f"节点返回 HTTP {resp.status_code}")}
    try:
        return {"ok": True, "data": resp.json()}
    except ValueError:
        return {"ok": False, "error": "节点返回的不是 JSON"}


__all__ = [
    "GATEWAY_REALM_ENV",
    "NODE_KEY_ENVS",
    "NODE_IO_TIMEOUT",
    "PANEL_KEY_HEADER",
    "configured_key",
    "configured_realm",
    "describe",
    "fetch_node_identity",
    "install_scope",
    "item_visible",
    "library_owner",
    "library_visible",
    "node_libraries",
    "node_me",
    "node_router",
    "push_scan",
    "register_self",
    "reset_cache",
    "self_node",
    "self_node_id",
    "self_realm",
    "self_realm_id",
    "start_local_scan",
    "visible_library_ids",
]
