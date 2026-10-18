"""管理后台 · 播放与客户端策略（v2.26.0）

策略本身与判定都在 ``backend/playback_policy.py``（EM 与 EA 共用同一个库，所以面板上
改完，出流的 EA 立刻按新策略放行/拒绝）。这一组端点只做两件事：

- ``GET  /api/admin/playback/policy``：当前策略 + **运行态**（由谁出流）；
- ``PUT  /api/admin/playback/policy``：写回策略（仅超级管理员，前缀规则见 admin_roles）。

播放可观测（2026-10 简化：单路径）：

- ``GET  /api/admin/playback/lines``：单路径快照——播放流量（本进程请求数/出流量）、
  本地缓存层状态（启用/命中率/占用）。**纯只读**。

VPS 本地缓存（播放线路「本地缓存」）：

- ``GET  /api/admin/playback/local-cache``：配置 + 占用/命中率统计 + 条目列表；
- ``PUT  /api/admin/playback/local-cache``：写回开关/目录/配额/热门规则/限速；
- ``POST /api/admin/playback/local-cache/clean``：手动清理（ready / failed / all）。
  写端点一律仅超级管理员，并写审计日志。

运行态只反映**本进程**：一体化部署时它就是全部；分离部署时转码跑在 EA 上，
面板这边会显示 ``playback_node=ea``，并提示去「服务器与线路」看节点状态——
不谎报一个自己看不到的数字。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel
from sqlalchemy.orm import Session

from backend import playback_policy
from backend.api.admin_core import _audit, get_current_admin
from backend.database import get_db
from backend.ratelimit import get_client_ip
from backend.emby_server import line_health, mount_health

router = APIRouter(prefix="/api/admin/playback", tags=["管理后台·播放与客户端策略"])


def _runtime(db: Session) -> dict:
    """本进程的播放运行态（不谎报自己看不到的东西）"""
    try:
        node = mount_health.playback_node(db, None)
    except Exception:  # noqa: BLE001 — 出流方式读不到不影响策略页
        node = "panel"
    return {
        "playback_node": node,
    }


@router.get("/lines")
def get_play_lines(
    current_admin=Depends(get_current_admin),
    db: Session = Depends(get_db),
):
    """单路径播放可观测快照（流量 / 本地缓存层 / CDN 域名状态）"""
    return line_health.snapshot(db)


@router.get("/policy")
def get_playback_policy(
    current_admin=Depends(get_current_admin),
    db: Session = Depends(get_db),
):
    return {
        "policy": playback_policy.policy_payload(db),
        "keys": dict(playback_policy.POLICY_KEYS),
        "runtime": _runtime(db),
    }


class PlaybackPolicyRequest(BaseModel):
    policy: dict


@router.put("/policy")
def update_playback_policy(
    payload: PlaybackPolicyRequest,
    request: Request,
    current_admin=Depends(get_current_admin),
    db: Session = Depends(get_db),
):
    """写回策略（只认白名单里的键，非法值保持原值不动）"""
    applied = playback_policy.write_policy(db, payload.policy)
    _audit(db, current_admin, "playback_policy_update", "system", None,
           {"applied": applied}, ip=_client_ip(request))
    db.commit()
    return {"success": True, "applied": applied, "policy": playback_policy.policy_payload(db)}


def _client_ip(request: Request) -> str:
    # 统一用防伪造的 IP 获取（可信代理校验），保留函数名做兼容
    return get_client_ip(request)
