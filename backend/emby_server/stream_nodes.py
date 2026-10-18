"""流节点（分离架构）：高宽带大盘机器独立出流，主服务只改播放 URL。

## 架构

单机部署（默认）：``stream_nodes`` 无配置 → 所有行为与升级前逐字节一致，
播放 URL 指向本机，零影响。

分离部署：管理员在主服务后台（或一键脚本）注册一台/多台流节点
``https://stream.example.com``（高宽带 + 大盘 + CF 橙云隐藏 IP），
PlaybackInfo 生成的 ``DirectStreamUrl`` 自动改写为
流节点地址。URL 上的短期签名（``uid/exp/sign``）原样保留——流节点与主服务
共享 ``SECRET_KEY``，验签不需要额外对接。

## 与现有机制的关系（横切能力只许一套）

- 节点列表、健康检查、加权选择：复用 ``node_health``（``stream_nodes`` 键），
  本模块不另起一套；
- CDN 域名改写（``cdn`` 模块）是另一套：流节点命中时跳过 CDN 改写——
  节点域名本身就是边缘入口（CF 在前），再套一层没有意义；
- 鉴权：签名验签只依赖 ``SECRET_KEY``（``play_sign``），流节点读主库做
  用户/订阅校验（只读），无状态，可横向扩展。

## 安全

- 永远不把播放 URL 改写到内网/回环地址（``is_public_node_url`` 守卫）：
  ``node_health`` 的默认本机条目（``http://127.0.0.1:8000``）只在无配置时
  兜底，改写层直接忽略它；
- 管理接口（``admin_router``）用节点密钥（``X-Panel-Key`` = NODE_SHARED_SECRET 或其派生值，
  见 backend/node_auth.py；不再接受 SECRET_KEY 原文）
  鉴权，与节点间既有机制一致（``mount_health.require_panel_key``）。
"""

from __future__ import annotations

import ipaddress
import json
import logging
import os
from typing import Optional
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session

from backend.database import get_db
from backend.emby_server import node_health
from backend.emby_server.mount_health import require_panel_key
from backend import node_auth

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# URL 改写
# ---------------------------------------------------------------------------

def _is_public_node_url(url: str) -> bool:
    """节点地址必须是公网可达的：拒绝回环/内网/空地址。"""
    try:
        host = (urlsplit(url).hostname or "").strip().lower()
    except Exception:
        return False
    if not host:
        return False
    try:
        addr = ipaddress.ip_address(host)
        return not (addr.is_loopback or addr.is_private or addr.is_reserved)
    except ValueError:
        # 非 IP 即域名：拒绝 localhost 这类
        return host not in ("localhost",)


def _same_origin(a: str, b: str) -> bool:
    """两个基址是否同一源（scheme+host+port 一致）。"""
    try:
        pa, pb = urlsplit(a.rstrip("/")), urlsplit(b.rstrip("/"))
    except Exception:
        return False
    def _port(p):
        try:
            return p.port
        except ValueError:
            return None
    return (
        (pa.scheme or "http") == (pb.scheme or "http")
        and (pa.hostname or "").lower() == (pb.hostname or "").lower()
        and _port(pa) == _port(pb)
    )


def pick_stream_node(db: Session) -> Optional[dict]:
    """选一个可用的远端流节点；无远端节点时返回 None（= 本机出流）。"""
    node = node_health.pick_node(db)
    if not node:
        return None
    url = (node.get("url") or "").rstrip("/")
    if not url or not _is_public_node_url(url):
        return None
    return node


def rewrite_playback_urls(
    db: Session, stream_url: str, base: str,
) -> tuple[str, bool]:
    """有远端健康流节点时，把播放 URL 基址换成流节点。

    转码已删除（2026-10-17）：不再处理 TranscodingUrl。
    返回 ``(stream_url, hit)``。未命中时原样返回，
    调用方行为与升级前逐字节一致。
    """
    node = pick_stream_node(db)
    if node is None:
        return stream_url, False
    node_base = (node.get("url") or "").rstrip("/")
    base = (base or "").rstrip("/")
    if not base or _same_origin(node_base, base):
        return stream_url, False
    def _boundary_match(url: str) -> bool:
        # base 耗尽处必须是路径/查询/片段边界，否则按未命中处理
        # （r30 F1：base 落在路径段中间时 startswith 命中会造出坏 URL）
        if not url.startswith(base):
            return False
        rest = url[len(base):]
        return rest == "" or rest[0] in ("/", "?", "#")

    new_stream = (
        stream_url.replace(base, node_base, 1)
        if _boundary_match(stream_url) else stream_url
    )
    # 没换上说明 base 对不上：宁可不改写，也不能给客户端一个坏 URL
    if new_stream == stream_url:
        return stream_url, False
    logger.debug("播放 URL 改写到流节点 %s", node_base)
    return new_stream, True


# ---------------------------------------------------------------------------
# 注册 / 管理
# ---------------------------------------------------------------------------

def _raw_nodes(db: Session) -> list:
    """读原始配置（不带 node_health 的默认本机兜底）。"""
    from backend.models import SystemConfig
    row = db.query(SystemConfig).filter(
        SystemConfig.key == node_health.CONFIG_KEY).first()
    if row and row.value:
        try:
            nodes = json.loads(row.value)
            if isinstance(nodes, list):
                return nodes
        except (json.JSONDecodeError, TypeError):
            pass
    return []


def register_node(db: Session, url: str, name: str = "", weight: int = 100) -> dict:
    """注册/更新一个流节点（按 URL 去重）。返回注册后的节点。"""
    url = (url or "").strip().rstrip("/")
    if not url:
        raise ValueError("节点 URL 不能为空")
    scheme = urlsplit(url).scheme.lower()
    if scheme not in ("http", "https"):
        raise ValueError("节点 URL 必须以 http:// 或 https:// 开头")
    if not _is_public_node_url(url):
        raise ValueError("节点 URL 必须是公网地址（不能是内网/回环）")
    try:
        weight = max(1, int(weight))
    except (TypeError, ValueError):
        weight = 100
    nodes = _raw_nodes(db)
    node = {"url": url, "name": name or url, "weight": weight}
    for i, n in enumerate(nodes):
        if isinstance(n, dict) and (n.get("url") or "").rstrip("/") == url:
            nodes[i] = node
            break
    else:
        nodes.append(node)
    node_health.set_nodes(db, nodes)
    logger.info("注册流节点: %s（%s，权重 %s）", url, node["name"], weight)
    return node


def unregister_node(db: Session, url: str) -> bool:
    """按 URL 移除流节点；返回是否真的删掉了。"""
    url = (url or "").strip().rstrip("/")
    nodes = _raw_nodes(db)
    kept = [n for n in nodes
            if not (isinstance(n, dict) and (n.get("url") or "").rstrip("/") == url)]
    if len(kept) == len(nodes):
        return False
    node_health.set_nodes(db, kept)
    logger.info("移除流节点: %s", url)
    return True


# ---------------------------------------------------------------------------
# 管理接口（一键部署脚本调用）
# ---------------------------------------------------------------------------

admin_router = APIRouter(prefix="/api/admin/stream-nodes", tags=["stream-nodes"])


def _panel_auth(request: Request) -> None:
    try:
        require_panel_key(request)
    except HTTPException as e:
        raise HTTPException(status_code=e.status_code, detail=e.detail)


@admin_router.get("")
def list_nodes(request: Request, db: Session = Depends(get_db)):
    _panel_auth(request)
    return {"nodes": _raw_nodes(db), "healthy": [
        n.get("url") for n in node_health.healthy_nodes(db)
    ]}


@admin_router.post("/register")
def api_register(request: Request, payload: dict, db: Session = Depends(get_db)):
    _panel_auth(request)
    try:
        node = register_node(
            db,
            payload.get("url", ""),
            payload.get("name", ""),
            payload.get("weight", 100),
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"ok": True, "node": node, "nodes": _raw_nodes(db)}


@admin_router.delete("")
def api_unregister(request: Request, url: str, db: Session = Depends(get_db)):
    _panel_auth(request)
    return {"ok": unregister_node(db, url), "nodes": _raw_nodes(db)}


@admin_router.get("/bundle")
def api_bundle(request: Request, db: Session = Depends(get_db)):
    """一键部署包：流节点建自身所需的主服务侧配置。

    包含：SECRET_KEY（验签）、DATABASE_URL（只读主库）、rclone.conf 内容、
    SA 文件（{文件名: 内容}）、rclone 远端列表。走 HTTPS + 节点签名头。

    P1 修复（审查）：此前接受长期静态 X-Panel-Key bearer 头，而该头明文躺在
    每台流节点的 .env、部署脚本、shell 历史里——拿到它就能换走全部根凭据。
    现只接受有时效（±5 分钟）+ nonce 防重放 + 绑定 method+path 的 HMAC 签名头。
    部署脚本已同步改为 openssl 本地签名（scripts/deploy-streaming-node.sh）。
    """
    # 只认签名头，不认静态 bearer：根凭据下发必须用一次一签的短期凭证
    if not node_auth.verify_signed_headers(
        request.headers, request.method, request.url.path
    ):
        raise HTTPException(
            status_code=401,
            detail="bundle 下发只接受节点签名头（X-Panel-Ts/Nonce/Sign），"
                   "不接受静态 X-Panel-Key；请用新版部署脚本拉取",
        )
    from backend.security import SECRET_KEY

    bundle: dict = {
        "secret_key": SECRET_KEY,
        # 显式配置了节点密钥时一起下发（未配置时节点由 SECRET_KEY 派生出同一个值）
        "node_shared_secret": (os.environ.get("NODE_SHARED_SECRET") or "").strip(),
        "database_url": os.environ.get("DATABASE_URL", ""),
        "database_type": os.environ.get("DATABASE_TYPE", "postgresql"),
    }

    # rclone.conf
    conf_path = os.environ.get("MOUNT_RCLONE_CONF", "/config/rclone/rclone.conf")
    try:
        with open(conf_path, "r", encoding="utf-8") as f:
            bundle["rclone_conf"] = f.read()
    except OSError:
        bundle["rclone_conf"] = ""
    # 远端列表（供部署脚本给挂载点做默认建议）
    bundle["rclone_remotes"] = _parse_remotes(bundle["rclone_conf"])

    # SA 文件
    sa_dir = os.environ.get("RCLONE_SA_DIR", "/sa-accounts")
    sa_files: dict[str, str] = {}
    try:
        import pathlib
        for p in sorted(pathlib.Path(sa_dir).glob("*.json"))[:200]:
            try:
                sa_files[p.name] = p.read_text(encoding="utf-8")
            except OSError:
                continue
    except OSError:
        pass
    bundle["sa_files"] = sa_files
    bundle["sa_count"] = len(sa_files)
    return bundle


def _parse_remotes(conf_text: str) -> list[str]:
    """从 rclone.conf 文本提取远端名（[xxx] 节）。"""
    remotes: list[str] = []
    for line in (conf_text or "").splitlines():
        line = line.strip()
        if line.startswith("[") and line.endswith("]") and len(line) > 2:
            remotes.append(line[1:-1])
    return remotes
