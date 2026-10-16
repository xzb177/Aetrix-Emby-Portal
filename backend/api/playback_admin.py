"""管理后台 · 播放与客户端策略（v2.26.0）

策略本身与判定都在 ``backend/playback_policy.py``（EM 与 EA 共用同一个库，所以面板上
改完，出流的 EA 立刻按新策略放行/拒绝）。这一组端点只做两件事：

- ``GET  /api/admin/playback/policy``：当前策略 + **运行态**（本进程正在转码几路、
  上限多少、由谁出流）——运营改「并发上限」之前得先知道现在跑到什么程度；
- ``PUT  /api/admin/playback/policy``：写回策略（仅超级管理员，前缀规则见 admin_roles）。

播放可观测（2026-10 简化：单路径）：

- ``GET  /api/admin/playback/lines``：单路径快照——播放流量（本进程请求数/出流量）、
  本地缓存层状态（启用/命中率/占用）、CDN 域名状态。**纯只读**。

CDN 域名预留（播放三层第 2/3 层极简预留版）：

- ``GET  /api/admin/playback/cdn``：当前域名/开关/归一化结果/分片缓存头口径；
- ``PUT  /api/admin/playback/cdn``：写回域名与启用开关（仅超级管理员）。
  只做域名预留：不做备案、不做厂商对接；CDN 侧的回源与缓存规则由管理员
  在厂商控制台配置，本服务只保证「启用后播放 URL 走该域名」。

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

import os
import shutil

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy.orm import Session

from backend import playback_policy
from backend.api.admin_core import _audit, get_current_admin
from backend.database import get_db
from backend.ratelimit import get_client_ip
from backend.emby_server import cdn, line_health, mount_health
from backend.emby_server.streaming import (
    active_transcode_ids,
    transcode_capacity,
    transcode_idle_seconds,
)

router = APIRouter(prefix="/api/admin/playback", tags=["管理后台·播放与客户端策略"])


def _runtime(db: Session) -> dict:
    """本进程的转码运行态（不谎报自己看不到的东西）"""
    active = active_transcode_ids()
    try:
        node = mount_health.playback_node(db, None)
    except Exception:  # noqa: BLE001 — 出流方式读不到不影响策略页
        node = "panel"
    return {
        "active_transcodes": len(active),
        "capacity": transcode_capacity(),
        "idle_timeout_seconds": int(transcode_idle_seconds()),
        "playback_node": node,
        "ffmpeg_available": bool(shutil.which(os.getenv("EMBY_FFMPEG_PATH", "ffmpeg"))),
        "max_transcodes_env": (os.getenv("EMBY_MAX_TRANSCODES") or "").strip(),
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


# ==================== CDN 域名预留（播放三层第 2/3 层） ====================

@router.get("/cdn")
def get_cdn_config(
    current_admin=Depends(get_current_admin),
    db: Session = Depends(get_db),
):
    """当前 CDN 预留配置（域名/开关/归一化结果/分片缓存头口径）"""
    return {"success": True, "cdn": cdn.config_payload(db),
            "play_lines": list(cdn.play_lines())}


class CdnConfigRequest(BaseModel):
    domain: str = ""
    enabled: bool = False


@router.put("/cdn")
def update_cdn_config(
    payload: CdnConfigRequest,
    request: Request,
    current_admin=Depends(get_current_admin),
    db: Session = Depends(get_db),
):
    """写回 CDN 预留配置；域名非法时 400 并说清怎么改（不会存半个坏配置）"""
    try:
        state = cdn.write_config(db, payload.domain, payload.enabled)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    _audit(db, current_admin, "playback_cdn_update", "system", None,
           {"domain": state.get("domain"), "enabled": state.get("enabled")},
           ip=_client_ip(request))
    db.commit()
    return {"success": True, "cdn": state}
