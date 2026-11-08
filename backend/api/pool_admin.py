"""
管理后台 · 公共贡献池成员管理（goal-33 PR-4 两端界面）

池本体与生命周期服务在 backend/emby_server/pool_gate.py；配置
读写在 backend/api/admin_pool.py（PR-1）。本文件是成员运营面
（design §8.1）：总览、成员列表与生命周期操作（授予候选资格/
批准/拒绝/暂停/恢复/清退/清除违规标志/单盘上限覆盖）、按条目
查池副本与手动下线。鉴权口径与 admin_pool 一致（get_current_admin
+ _audit；只读角色能不能写由 admin_roles 集中判定）。

写口一律转调 pool_gate 生命周期服务函数（状态变更经
transition_member_status 收口落 H2 流水），本文件不直接写
member.status。
"""
from __future__ import annotations

import logging
from datetime import date
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy import func
from sqlalchemy.orm import Session

from backend import models
from backend.api.admin_core import _audit, get_current_admin
from backend.database import get_db
from backend.emby_server import models as em
from backend.emby_server import pool_gate, pool_reward

logger = logging.getLogger(__name__)

pool_admin_router = APIRouter(
    prefix="/api/admin/pool", tags=["管理后台·公共贡献池"])


class GrantCandidateRequest(BaseModel):
    user_id: int


class EjectMemberRequest(BaseModel):
    violation: bool = False
    violation_reason: Optional[str] = None


class SuspendMemberRequest(BaseModel):
    reason: Optional[str] = None


class PatchMemberRequest(BaseModel):
    capacity_cap_bytes: Optional[int] = None


def _member_payload(db: Session, member: em.PoolMember) -> dict:
    """成员行管理端序列化：贡献者/账号名 + 状态 + 违规标志 +
    副本与今日量聚合（口径与用户端 member_public_payload 的
    统计部分共用 pool_gate._member_stats，不两处各算一份）。"""
    user = (db.query(models.WebUser)
            .filter(models.WebUser.id == member.user_id).first())
    binding = None
    if member.binding_id is not None:
        binding = (db.query(em.UserStorageBinding)
                   .filter(em.UserStorageBinding.id == member.binding_id)
                   .first())
    payload = {
        "id": member.id,
        "user_id": member.user_id,
        "username": user.username if user is not None else None,
        "binding_id": member.binding_id,
        "account_label": binding.account_label if binding else None,
        "binding_status": binding.status if binding else None,
        "provider": member.provider,
        "status": member.status,
        "suspended_reason": member.suspended_reason,
        "breaker_until": (member.breaker_until.isoformat()
                          if member.breaker_until else None),
        "capacity_cap_bytes": member.capacity_cap_bytes,
        "effective_capacity_bytes": (
            member.capacity_cap_bytes
            if member.capacity_cap_bytes is not None
            else pool_gate.pool_config_payload(db)[
                "pool_member_capacity_bytes"]),
        "violation_flagged": bool(member.violation_flagged),
        "violation_reason": member.violation_reason,
        "violation_flagged_at": (member.violation_flagged_at.isoformat()
                                 if member.violation_flagged_at else None),
        "violation_flagged_by": member.violation_flagged_by,
        "violation_cleared_at": (member.violation_cleared_at.isoformat()
                                 if member.violation_cleared_at else None),
        "violation_cleared_by": member.violation_cleared_by,
        "joined_at": (member.joined_at.isoformat()
                      if member.joined_at else None),
        "status_changed_at": (member.status_changed_at.isoformat()
                              if member.status_changed_at else None),
        "created_at": (member.created_at.isoformat()
                       if member.created_at else None),
    }
    payload.update(pool_gate._member_stats(db, member))
    return payload


def _lifecycle_http(exc: pool_gate.PoolLifecycleError) -> HTTPException:
    status = 404 if exc.kind == "not_found" else 400
    return HTTPException(status_code=status, detail=exc.message)


def _client_ip(request: Request):
    return request.client.host if request.client else None


# ------------------------------------------------------------ 总览与列表


@pool_admin_router.get("/overview")
def pool_overview(
    current_admin: models.WebUser = Depends(get_current_admin),
    db: Session = Depends(get_db),
):
    """总览条：成员分状态计数、池副本总数/总占用、今日转存与
    起播（pool_usage_daily 当日行聚合）、熔断中成员数。"""
    status_counts = dict(
        db.query(em.PoolMember.status, func.count(em.PoolMember.id))
        .group_by(em.PoolMember.status).all())
    copies, copies_bytes = (
        db.query(func.count(em.TransferVariant.id),
                 func.coalesce(func.sum(em.TransferVariant.size_bytes), 0))
        .filter(em.TransferVariant.owner_scope == "pool",
                em.TransferVariant.status.in_(("ready", "stale", "orphan")))
        .one())
    today = (
        db.query(
            func.coalesce(func.sum(em.PoolUsageDaily.transfer_jobs), 0),
            func.coalesce(func.sum(em.PoolUsageDaily.transfer_bytes), 0),
            func.coalesce(func.sum(em.PoolUsageDaily.play_starts), 0),
            func.coalesce(func.sum(em.PoolUsageDaily.quota_hits), 0))
        .filter(em.PoolUsageDaily.day == date.today())
        .one())
    breaker = (
        db.query(func.count(em.PoolMember.id))
        .filter(em.PoolMember.status == "suspended",
                em.PoolMember.suspended_reason == "breaker")
        .scalar())
    return {
        "members_total": sum(status_counts.values()),
        "members_candidate": int(status_counts.get("candidate", 0)),
        "members_pending": int(status_counts.get("pending", 0)),
        "members_active": int(status_counts.get("active", 0)),
        "members_suspended": int(status_counts.get("suspended", 0)),
        "members_breaker": int(breaker or 0),
        "copies_total": int(copies or 0),
        "copies_bytes": int(copies_bytes or 0),
        "today_transfer_jobs": int(today[0] or 0),
        "today_transfer_bytes": int(today[1] or 0),
        "today_play_starts": int(today[2] or 0),
        "today_quota_hits": int(today[3] or 0),
    }


@pool_admin_router.get("/members")
def pool_members_list(
    current_admin: models.WebUser = Depends(get_current_admin),
    db: Session = Depends(get_db),
):
    rows = (db.query(em.PoolMember)
            .order_by(em.PoolMember.id.asc()).all())
    return {"members": [_member_payload(db, m) for m in rows]}


# ------------------------------------------------------------ 生命周期写口


@pool_admin_router.post("/candidates")
def pool_grant_candidate(
    request: GrantCandidateRequest,
    raw_request: Request,
    current_admin: models.WebUser = Depends(get_current_admin),
    db: Session = Depends(get_db),
):
    """授予池候选资格（grant_pool_candidate 管理入口，通审决议 2）：
    给站内账号建候选行（预批资格；贡献者绑盘走池上限、申请时挂接
    同一行）。幂等：已有非终态行直接返回。"""
    user = (db.query(models.WebUser)
            .filter(models.WebUser.id == request.user_id).first())
    if user is None:
        raise HTTPException(status_code=404, detail="用户不存在。")
    member = pool_gate.grant_pool_candidate(db, request.user_id)
    _audit(db, current_admin, "pool_grant_candidate", "pool_member",
           member.id, {"user_id": request.user_id},
           ip=_client_ip(raw_request))
    db.commit()
    return _member_payload(db, member)


def _run_action(db, fn, *args, **kwargs):
    """生命周期动作端点共用外壳：转调服务函数，业务拒绝映射
    400（not_found 映射 404），失败回滚不留半截状态。"""
    try:
        return fn(db, *args, **kwargs)
    except pool_gate.PoolLifecycleError as exc:
        db.rollback()
        raise _lifecycle_http(exc) from exc


def _notify_member_review(member: em.PoolMember, *, approved: bool,
                          admin_id: int) -> None:
    """审核结果通知申请人（站内信 + TG，2026-11-08 补承诺落点）。

    审核已落库后调用；通知走 quiet 守卫、失败只记日志，不影响审核结果。
    本端点是同步 def，通知服务是 async 的，用 asyncio.run 在本线程跑完
    （与 new_media_notify / maintenance 的同步上下文发通知同款范式）。
    """
    import asyncio

    from backend.notifications import AdminEvent, notify_admin_event_quiet

    if approved:
        event_type = AdminEvent.POOL_MEMBER_APPROVED
        title = "贡献池申请已通过"
        content = ("你申请加入公共贡献池已经通过啦。你的网盘会开始为池子"
                   "提供副本，贡献情况可以在个人中心的贡献池页面查看。")
    else:
        event_type = AdminEvent.POOL_MEMBER_REJECTED
        title = "贡献池申请未通过"
        content = ("你申请加入公共贡献池这次没有通过。如需重新申请，"
                   "可以在个人中心的贡献池页面再次提交。")
    try:
        asyncio.run(notify_admin_event_quiet(
            event_type=event_type,
            log_ctx=f"pool_member_review member_id={member.id}",
            user_id=member.user_id,
            title=title,
            content=content,
            related_id=member.id,
            from_admin_id=admin_id,
            channels=["in_app", "telegram"],
        ))
    except Exception:
        logger.warning("贡献池审核通知失败 member=%s", member.id,
                       exc_info=True)


@pool_admin_router.post("/members/{member_id}/approve")
def pool_approve_member(
    member_id: int,
    raw_request: Request,
    current_admin: models.WebUser = Depends(get_current_admin),
    db: Session = Depends(get_db),
):
    member = _run_action(
        db, pool_gate.approve_pool_member, member_id,
        actor_id=current_admin.id)
    _audit(db, current_admin, "pool_member_approve", "pool_member",
           member.id, {}, ip=_client_ip(raw_request))
    db.commit()
    _notify_member_review(member, approved=True,
                          admin_id=current_admin.id)
    return _member_payload(db, member)


@pool_admin_router.post("/members/{member_id}/reject")
def pool_reject_member(
    member_id: int,
    raw_request: Request,
    current_admin: models.WebUser = Depends(get_current_admin),
    db: Session = Depends(get_db),
):
    member = _run_action(
        db, pool_gate.reject_pool_member, member_id,
        actor_id=current_admin.id)
    _audit(db, current_admin, "pool_member_reject", "pool_member",
           member.id, {}, ip=_client_ip(raw_request))
    db.commit()
    _notify_member_review(member, approved=False,
                          admin_id=current_admin.id)
    return _member_payload(db, member)


@pool_admin_router.post("/members/{member_id}/resume")
def pool_resume_member(
    member_id: int,
    raw_request: Request,
    current_admin: models.WebUser = Depends(get_current_admin),
    db: Session = Depends(get_db),
):
    member = _run_action(
        db, pool_gate.resume_pool_member, member_id,
        actor_id=current_admin.id)
    _audit(db, current_admin, "pool_member_resume", "pool_member",
           member.id, {}, ip=_client_ip(raw_request))
    db.commit()
    return _member_payload(db, member)


@pool_admin_router.post("/members/{member_id}/clear-violation")
def pool_clear_violation(
    member_id: int,
    raw_request: Request,
    current_admin: models.WebUser = Depends(get_current_admin),
    db: Session = Depends(get_db),
):
    member = _run_action(
        db, pool_gate.clear_member_violation, member_id,
        actor_id=current_admin.id)
    _audit(db, current_admin, "pool_member_clear_violation",
           "pool_member", member.id, {}, ip=_client_ip(raw_request))
    db.commit()
    return _member_payload(db, member)


@pool_admin_router.post("/members/{member_id}/suspend")
def pool_suspend_member(
    member_id: int,
    raw_request: Request,
    body: Optional[SuspendMemberRequest] = None,
    current_admin: models.WebUser = Depends(get_current_admin),
    db: Session = Depends(get_db),
):
    reason = (body.reason if body and body.reason else "manual")
    try:
        member = pool_gate.suspend_pool_member(
            db, member_id, actor_id=current_admin.id, reason=reason)
    except pool_gate.PoolLifecycleError as exc:
        db.rollback()
        raise _lifecycle_http(exc) from exc
    _audit(db, current_admin, "pool_member_suspend", "pool_member",
           member.id, {"reason": reason},
           ip=_client_ip(raw_request))
    db.commit()
    return _member_payload(db, member)


@pool_admin_router.post("/members/{member_id}/eject")
def pool_eject_member(
    member_id: int,
    body: EjectMemberRequest,
    raw_request: Request,
    current_admin: models.WebUser = Depends(get_current_admin),
    db: Session = Depends(get_db),
):
    try:
        member = pool_gate.eject_pool_member(
            db, member_id, actor_id=current_admin.id,
            violation=body.violation,
            violation_reason=body.violation_reason)
    except pool_gate.PoolLifecycleError as exc:
        db.rollback()
        raise _lifecycle_http(exc) from exc
    _audit(db, current_admin, "pool_member_eject", "pool_member",
           member.id, {"violation": body.violation},
           ip=_client_ip(raw_request))
    db.commit()
    return _member_payload(db, member)


@pool_admin_router.patch("/members/{member_id}")
def pool_patch_member(
    member_id: int,
    body: PatchMemberRequest,
    raw_request: Request,
    current_admin: models.WebUser = Depends(get_current_admin),
    db: Session = Depends(get_db),
):
    """单盘上限覆盖：capacity_cap_bytes 传 null 回落全局配置，
    传非负整数为该盘覆盖值（design §2.1）。"""
    member = (db.query(em.PoolMember)
              .filter(em.PoolMember.id == member_id).first())
    if member is None:
        raise HTTPException(status_code=404, detail="池成员不存在。")
    if "capacity_cap_bytes" in body.model_fields_set:
        value = body.capacity_cap_bytes
        if value is not None and value < 0:
            raise HTTPException(
                status_code=400, detail="单盘上限不能为负数。")
        member.capacity_cap_bytes = value
    _audit(db, current_admin, "pool_member_update", "pool_member",
           member.id,
           {"capacity_cap_bytes": member.capacity_cap_bytes},
           ip=_client_ip(raw_request))
    db.commit()
    db.refresh(member)
    return _member_payload(db, member)


# ------------------------------------------------------------ 副本面


@pool_admin_router.get("/copies")
def pool_copies_list(
    item_id: int,
    current_admin: models.WebUser = Depends(get_current_admin),
    db: Session = Depends(get_db),
):
    """按条目查池副本（design §8.1 副本表）：所在盘（成员）、
    状态、大小、最近播放。"""
    rows = (db.query(em.TransferVariant)
            .filter(em.TransferVariant.owner_scope == "pool",
                    em.TransferVariant.item_id == item_id)
            .order_by(em.TransferVariant.id.asc()).all())
    item = (db.query(em.MediaItem)
            .filter(em.MediaItem.id == item_id).first())
    out = []
    for row in rows:
        member_id = int(row.owner_ref) if (row.owner_ref or "").isdigit() \
            else None
        out.append({
            "id": row.id,
            "item_id": row.item_id,
            "item_name": getattr(item, "name", None),
            "member_id": member_id,
            "status": row.status,
            "size_bytes": row.size_bytes,
            "last_played_at": (row.last_played_at.isoformat()
                               if row.last_played_at else None),
            "last_checked_at": (row.last_checked_at.isoformat()
                                if row.last_checked_at else None),
            "created_at": (row.created_at.isoformat()
                           if row.created_at else None),
        })
    return {"copies": out}


@pool_admin_router.post("/copies/{variant_id}/release")
def pool_copy_release(
    variant_id: int,
    raw_request: Request,
    current_admin: models.WebUser = Depends(get_current_admin),
    db: Session = Depends(get_db),
):
    """手动下线一份池副本：标 stale 停发 + best-effort 删文件。"""
    try:
        row = pool_gate.release_pool_copy(db, variant_id)
    except pool_gate.PoolLifecycleError as exc:
        db.rollback()
        raise _lifecycle_http(exc) from exc
    _audit(db, current_admin, "pool_copy_release", "transfer_variant",
           row.id, {"item_id": row.item_id}, ip=_client_ip(raw_request))
    db.commit()
    return {"id": row.id, "status": row.status, "item_id": row.item_id}


# ------------------------------------------------------------ 激励结算（PR-6）


class SettleRewardsRequest(BaseModel):
    period_key: Optional[str] = None


def _reward_http(exc: pool_reward.PoolRewardError) -> HTTPException:
    status = 404 if exc.kind == "not_found" else 400
    return HTTPException(status_code=status, detail=exc.message)


def _settlement_payload(db: Session, batch) -> dict:
    counts = dict(
        db.query(em.PoolRewardDetail.status,
                 func.count(em.PoolRewardDetail.id))
        .filter(em.PoolRewardDetail.settlement_id == batch.id)
        .group_by(em.PoolRewardDetail.status).all())
    points_total = (
        db.query(func.coalesce(func.sum(em.PoolRewardDetail.points), 0))
        .filter(em.PoolRewardDetail.settlement_id == batch.id,
                em.PoolRewardDetail.status == "paid")
        .scalar())
    return {
        "id": batch.id,
        "period_key": batch.period_key,
        "period_kind": batch.period_kind,
        "period_start": (batch.period_start.isoformat()
                         if batch.period_start else None),
        "period_end": (batch.period_end.isoformat()
                       if batch.period_end else None),
        "status": batch.status,
        "shadow": bool(batch.shadow),
        "created_by": batch.created_by,
        "last_error": batch.last_error,
        "members": int(sum(counts.values())),
        "paid": int(counts.get("paid", 0)),
        "shadow_rows": int(counts.get("shadow", 0)),
        "frozen": int(counts.get("frozen", 0)),
        "skipped": int(counts.get("skipped", 0)),
        "paid_points_total": int(points_total or 0),
        "created_at": (batch.created_at.isoformat()
                       if batch.created_at else None),
        "finished_at": (batch.finished_at.isoformat()
                        if batch.finished_at else None),
    }


def _detail_payload(db: Session, detail) -> dict:
    user = (db.query(models.WebUser)
            .filter(models.WebUser.id == detail.user_id).first())
    return {
        "id": detail.id,
        "settlement_id": detail.settlement_id,
        "member_id": detail.member_id,
        "user_id": detail.user_id,
        "username": user.username if user is not None else None,
        "counted_plays": detail.counted_plays,
        "active_days": detail.active_days,
        "raw_plays": detail.raw_plays,
        "transfer_bytes": detail.transfer_bytes,
        "points": detail.points,
        "xp": detail.xp,
        "welfare_days": detail.welfare_days,
        "status": detail.status,
        "reason": detail.reason,
        "points_log_id": detail.points_log_id,
        "granted_at": (detail.granted_at.isoformat()
                       if detail.granted_at else None),
        "released_by": detail.released_by,
        "released_at": (detail.released_at.isoformat()
                        if detail.released_at else None),
        "created_at": (detail.created_at.isoformat()
                       if detail.created_at else None),
    }


@pool_admin_router.get("/rewards/preview")
def pool_rewards_preview(
    period_key: Optional[str] = None,
    current_admin: models.WebUser = Depends(get_current_admin),
    db: Session = Depends(get_db),
):
    """结算预览（只算不落行）：每位成员当期 counted/在线日/应发
    积分与预计走向（paid/shadow/frozen/skipped）。与执行结算
    共用 pool_reward 同一份计量，总开关关时也可预览。"""
    try:
        return pool_reward.preview_settlement(db, period_key)
    except pool_reward.PoolRewardError as exc:
        raise _reward_http(exc) from exc


@pool_admin_router.post("/rewards/settle")
def pool_rewards_settle(
    raw_request: Request,
    body: Optional[SettleRewardsRequest] = None,
    current_admin: models.WebUser = Depends(get_current_admin),
    db: Session = Depends(get_db),
):
    """执行结算（手动触发，自动定时默认不开）：period_key 缺省
    结上一个完整周期。影子模式（真发开关关）只落行不发钱；
    重跑幂等不双发。总开关关时 400 不建批。"""
    try:
        summary = pool_reward.run_settlement(
            db, body.period_key if body else None,
            created_by=current_admin.id)
    except pool_reward.PoolRewardError as exc:
        db.rollback()
        raise _reward_http(exc) from exc
    _audit(db, current_admin, "pool_reward_settle", "pool_reward",
           summary["settlement_id"],
           {"period_key": summary["period_key"],
            "payout": summary["payout"]},
           ip=_client_ip(raw_request))
    db.commit()
    batch = (db.query(em.PoolRewardSettlement)
             .filter(em.PoolRewardSettlement.id
                     == summary["settlement_id"]).first())
    return {"summary": summary,
            "settlement": _settlement_payload(db, batch)}


@pool_admin_router.get("/rewards/settlements")
def pool_rewards_settlements(
    current_admin: models.WebUser = Depends(get_current_admin),
    db: Session = Depends(get_db),
):
    rows = (db.query(em.PoolRewardSettlement)
            .order_by(em.PoolRewardSettlement.id.desc()).all())
    return {"settlements": [_settlement_payload(db, b) for b in rows]}


@pool_admin_router.get("/rewards/settlements/{settlement_id}/details")
def pool_rewards_settlement_details(
    settlement_id: int,
    current_admin: models.WebUser = Depends(get_current_admin),
    db: Session = Depends(get_db),
):
    batch = (db.query(em.PoolRewardSettlement)
             .filter(em.PoolRewardSettlement.id == settlement_id)
             .first())
    if batch is None:
        raise HTTPException(status_code=404, detail="结算批次不存在。")
    rows = (db.query(em.PoolRewardDetail)
            .filter(em.PoolRewardDetail.settlement_id == settlement_id)
            .order_by(em.PoolRewardDetail.id.asc()).all())
    return {"settlement": _settlement_payload(db, batch),
            "details": [_detail_payload(db, d) for d in rows]}


@pool_admin_router.post("/rewards/details/{detail_id}/release")
def pool_rewards_release_detail(
    detail_id: int,
    raw_request: Request,
    current_admin: models.WebUser = Depends(get_current_admin),
    db: Session = Depends(get_db),
):
    """frozen 明细人工审放行：按已 capped 的计量数发放（真发
    关时落 shadow 待开闸补发）。"""
    try:
        detail = pool_reward.release_frozen_detail(
            db, detail_id, actor_id=current_admin.id)
    except pool_reward.PoolRewardError as exc:
        db.rollback()
        raise _reward_http(exc) from exc
    _audit(db, current_admin, "pool_reward_release", "pool_reward",
           detail.id, {"member_id": detail.member_id},
           ip=_client_ip(raw_request))
    db.commit()
    return _detail_payload(db, detail)
