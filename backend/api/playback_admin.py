"""管理后台 · 播放与客户端策略（v2.26.0）

策略本身与判定都在 ``backend/playback_policy.py``（EM 与 EA 共用同一个库，所以面板上
改完，出流的 EA 立刻按新策略放行/拒绝）。这一组端点只做两件事：

- ``GET  /api/admin/playback/policy``：当前策略 + **运行态**（由谁出流）；
- ``PUT  /api/admin/playback/policy``：写回策略（仅超级管理员，前缀规则见 admin_roles）。

播放可观测（2026-10 简化：单路径）：

- ``GET  /api/admin/playback/lines``：单路径快照——播放流量（本进程请求数/出流量）、
  本地缓存层状态（启用/命中率/占用）。**纯只读**。
- ``GET  /api/admin/playback/risk``：播放风控看板（2026-10-21）——正在播会话数、
  同一文件同时在播人数 Top10（≥2 标叠播）、302 次数、Drive 存在性检查
  命中/未命中/404/503、Drive API 403/429。**纯只读**，口径见 playback_risk。

VPS 本地缓存（播放线路「本地缓存」）段落已随 #957/#958/#959 删除（本地缓存整套移除）——
本文件当前仅实现 /lines 与 /policy。

转码删除（2026-10-24）：运行态不再有转码路数/上限/ffmpeg 可用性，
策略面只剩 Drive 直链总开关（只读）。

运行态只反映**本进程**：一体化部署时它就是全部；分离部署时面板这边
会显示 ``playback_node=ea``，并提示去「服务器与线路」看节点状态——
不谎报一个自己看不到的数字。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy.orm import Session
from typing import Optional

from backend import playback_policy
from backend.api.admin_core import _audit, get_current_admin
from backend.database import get_db
from backend.ratelimit import get_client_ip
from backend.emby_server import line_health, mount_health, playback_risk

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


@router.get("/risk")
def get_play_risk(
    current_admin=Depends(get_current_admin),
    db: Session = Depends(get_db),
):
    """播放风控看板（只读）：叠播 Top10 + Drive 调用计数"""
    return playback_risk.snapshot(db)


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


@router.post("/stream-path-policies/import")
def import_stream_path_policies(
    request: Request,
    current_admin=Depends(get_current_admin),
    db: Session = Depends(get_db),
):
    """显式导入旧配置里的源路径限流策略成表行（#1240 落库、本 PR 接线）。

    幂等：只补（mount_id, prefix）缺失的行，不删、不改已有行；
    导入本身不切换策略来源——表里有行后下一次 load_policies 自然用表行。
    """
    from backend.emby_server import stream_limits

    result = stream_limits.import_config_policies(db)
    _audit(db, current_admin, "stream_path_policies_import", "system", None,
           result, ip=_client_ip(request))
    db.commit()
    return {"success": True, **result}


@router.post("/stream-path-policies/seed-templates")
def seed_stream_path_templates(
    request: Request,
    current_admin=Depends(get_current_admin),
    db: Session = Depends(get_db),
):
    """显式建种子角色模板（User / RestrictedUser / Admin，闭环 #11）。

    落点说明：``path_auth.ensure_seed_templates`` 的 docstring 明写不 wired
    进 init_db，种子落点是管理端接线时的后续项——本端点就是该调用口。
    幂等：按名查已存在再插，二次调用不产生重复行；与 import 端点同款
    鉴权（get_current_admin）与审计 + db.commit 口径。
    """
    from backend.emby_server import path_auth

    result = path_auth.ensure_seed_templates(db)
    _audit(db, current_admin, "stream_path_policies_seed_templates",
           "system", None, result, ip=_client_ip(request))
    db.commit()
    return {"success": True, "templates": result}


class TransferRequest(BaseModel):
    force: bool = False


def _transfer_error_to_http(exc) -> HTTPException:
    mapping = {"master_off": 409, "no_drive_file": 400,
               "has_ready_variant": 409, "throttled": 409}
    return HTTPException(status_code=mapping.get(exc.kind, 409),
                         detail=str(exc))


@router.post("/transfer-variants/{item_id}/transfer")
def transfer_variant_transfer(
    item_id: int,
    body: TransferRequest = TransferRequest(),
    current_admin=Depends(get_current_admin),
    db: Session = Depends(get_db),
):
    """手动触发转存（管理端单条目「转存」按钮；转存变体第一步）。

    条目查不到 → 404；原件盘不可定位 → 409；总闸关 / 已有健康服主
    副本 / 24h 内已尝试（且 force=False）→ 409；无 drive_file_id → 400。
    force=True 为管理员显式越过 24h 节流（审计留痕）。只建队、不
    立即执行——工人守护按轮次跑（总闸关时工人空转，任务挂起）。
    """
    from backend.emby_server import models as em
    from backend.emby_server import transfer_variant as _tv

    # 先取主键整数再做任何提交：create_manual_job 内部 commit 后 ORM
    # 属性会过期，_audit 传对象可能触发跨会话刷新，传整数可避该坑。
    admin_id = getattr(current_admin, "id", None)
    item = (db.query(em.MediaItem)
            .filter(em.MediaItem.id == item_id,
                    em.MediaItem.deleted_at.is_(None))
            .first())
    if item is None:
        raise HTTPException(status_code=404, detail="条目不存在")
    # 函数级 import：emby_server.api 是重模块（main 已加载，调用时只是
    # 字典查找）；_drive_mount_id_for_item 的定位逻辑只许一套，不复制。
    from backend.emby_server.api import _drive_mount_id_for_item
    drive_id = _drive_mount_id_for_item(db, item)
    if not drive_id:
        raise HTTPException(status_code=409, detail="条目所在 Drive 盘不可定位")
    try:
        job = _tv.create_manual_job(db, item, drive_id, force=body.force)
    except _tv.TransferError as exc:
        raise _transfer_error_to_http(exc) from exc
    _audit(db, admin_id, "transfer_variant_transfer", "emby_item", item_id,
           {"job_id": job.id, "drive_id": drive_id, "force": body.force})
    db.commit()
    return {"success": True, "job_id": job.id, "item_id": item_id,
            "status": job.status}


@router.post("/transfer-variants/{item_id}/unbind")
def transfer_variant_unbind(
    item_id: int,
    current_admin=Depends(get_current_admin),
    db: Session = Depends(get_db),
):
    """条目级解除绑定（回滚第 2 级）：该条目全部 ready 行变 released，
    下次播放回原件；行与副本文件都保留（不自动删）。无 ready 行 → 404。"""
    from backend.emby_server import transfer_variant as _tv

    admin_id = getattr(current_admin, "id", None)
    released = _tv.release_binding(db, item_id)
    if not released:
        raise HTTPException(status_code=404, detail="该条目无生效中的转存副本")
    _audit(db, admin_id, "transfer_variant_unbind", "emby_item", item_id,
           {"released": released})
    db.commit()
    return {"success": True, "item_id": item_id, "released": released}


def _transfer_variant_row(row) -> dict:
    return {
        "id": row.id, "item_id": row.item_id,
        "source_file_id": row.source_file_id,
        "variant_file_id": row.variant_file_id,
        "variant_drive_id": row.variant_drive_id,
        "owner_scope": row.owner_scope, "owner_ref": row.owner_ref,
        "status": row.status, "size_bytes": row.size_bytes,
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "last_played_at": (row.last_played_at.isoformat()
                            if row.last_played_at else None),
        "last_checked_at": (row.last_checked_at.isoformat()
                             if row.last_checked_at else None),
        "stale_count": row.stale_count,
    }


@router.get("/transfer-variants")
def list_transfer_variants(
    limit: int = 50,
    current_admin=Depends(get_current_admin),
    db: Session = Depends(get_db),
):
    """转存副本登记列表（管理端专属口；fid 不是凭据，可见无妨）。
    按 id 倒序，limit 默认 50、上限 200。"""
    from backend.emby_server import models as em

    limit = max(1, min(limit, 200))
    rows = (db.query(em.TransferVariant)
            .order_by(em.TransferVariant.id.desc())
            .limit(limit).all())
    return {"items": [_transfer_variant_row(r) for r in rows]}


@router.get("/transfer-variants/jobs")
def list_transfer_jobs(
    status: Optional[str] = None,
    limit: int = 50,
    current_admin=Depends(get_current_admin),
    db: Session = Depends(get_db),
):
    """转存任务列表（管理端专属口；status 可选过滤，limit 同款）。"""
    from backend.emby_server import models as em

    limit = max(1, min(limit, 200))
    q = db.query(em.TransferJob).order_by(em.TransferJob.id.desc())
    if status:
        q = q.filter(em.TransferJob.status == status)
    rows = q.limit(limit).all()
    return {"items": [{
        "id": r.id, "item_id": r.item_id, "trigger": r.trigger,
        "drive_id": r.drive_id, "status": r.status,
        "attempts": r.attempts, "error_kind": r.error_kind,
        "error_detail": r.error_detail,
        "next_retry_at": (r.next_retry_at.isoformat()
                           if r.next_retry_at else None),
        "created_at": r.created_at.isoformat() if r.created_at else None,
        "updated_at": r.updated_at.isoformat() if r.updated_at else None,
    } for r in rows]}
