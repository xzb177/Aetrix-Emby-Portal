# -*- coding: utf-8 -*-
"""求片中心业务逻辑（公益服模块3-娱乐板块）

.. deprecated::
    求片额度逻辑已并入 backend/media_seek.py（月度额度：公益/付费区分）。
    本模块保留仅供旧端点兼容，不再演进。

规则：
- 月额度：公益用户 welfare_request_monthly（默认3），付费用户 paid_request_monthly（默认10）
- 去重：同一 tmdb_id 有未处理（pending/approved）的请求时，不允许重复提交
- 状态机：pending → approved/rejected → done

SystemConfig 键（backend/api/economy._get_int_config 读取）：
- welfare_request_monthly=3
- paid_request_monthly=10
"""

from __future__ import annotations

import logging
from datetime import datetime

from sqlalchemy import func
from sqlalchemy.orm import Session

from backend import models
from backend.api import economy

logger = logging.getLogger(__name__)

# 未处理状态：这些状态的请求存在时，不允许重复提交同一 tmdb_id
OPEN_STATUSES = ("pending", "approved")


def _month_start() -> datetime:
    """本月1日零点（r34：上海自然月，UTC 入库帧——与 media_seek.used_this_month 同口径。

    此前服务器本地月界，生产 UTC 下每月 1 日 08:00（北京时间）才重置。
    """
    return economy._month_start_utc_naive()


def _get_int(db: Session, key: str, default: int) -> int:
    return economy._get_int_config(db, key, default)


def _is_welfare_active(user: models.WebUser) -> bool:
    """用户公益资格是否有效"""
    if not getattr(user, "is_welfare", False):
        return False
    expires_at = getattr(user, "welfare_expires_at", None)
    if expires_at is None:
        return True  # 永不过期
    return expires_at > datetime.now()


def get_monthly_quota(db: Session, user: models.WebUser) -> dict:
    """本月求片额度：上限 / 已用 / 剩余"""
    if _is_welfare_active(user):
        limit = _get_int(db, "welfare_request_monthly", 3)
        kind = "welfare"
    else:
        limit = _get_int(db, "paid_request_monthly", 10)
        kind = "paid"

    used = db.query(func.count(models.MediaRequest.id)).filter(
        models.MediaRequest.user_id == user.id,
        models.MediaRequest.created_at >= _month_start(),
    ).scalar() or 0

    return {"kind": kind, "limit": limit, "used": int(used), "remaining": max(0, limit - int(used))}


def submit_request(
    db: Session,
    user: models.WebUser,
    tmdb_id: str,
    media_type: str,
    title: str,
) -> models.MediaRequest:
    """提交求片

    校验：
    - tmdb_id/title 非空
    - media_type 只能是 movie/tv
    - 本月额度未用完
    - 同一 tmdb_id 没有未处理的请求（去重）
    """
    tmdb_id = (tmdb_id or "").strip()
    title = (title or "").strip()
    if not tmdb_id:
        raise ValueError("tmdb_id 不能为空")
    if not title:
        raise ValueError("标题不能为空")
    if media_type not in ("movie", "tv"):
        raise ValueError("media_type 只能是 movie 或 tv")

    quota = get_monthly_quota(db, user)
    if quota["remaining"] <= 0:
        raise ValueError(f"本月求片额度已用完（{quota['used']}/{quota['limit']}）")

    dup = db.query(models.MediaRequest).filter(
        models.MediaRequest.tmdb_id == tmdb_id,
        models.MediaRequest.status.in_(OPEN_STATUSES),
    ).first()
    if dup:
        raise ValueError(f"该影片已有人求片（状态：{dup.status}），请勿重复提交")

    req = models.MediaRequest(
        user_id=user.id,
        tmdb_id=tmdb_id,
        media_type=media_type,
        title=title,
        status="pending",
    )
    db.add(req)
    db.commit()
    db.refresh(req)
    logger.info("求片提交: user=%s tmdb=%s title=%s", user.id, tmdb_id, title)
    return req


def list_my_requests(
    db: Session,
    user: models.WebUser,
    page: int = 1,
    page_size: int = 20,
) -> dict:
    """我的求片列表（分页）"""
    q = db.query(models.MediaRequest).filter(
        models.MediaRequest.user_id == user.id
    ).order_by(models.MediaRequest.id.desc())
    total = q.count()
    items = q.offset((page - 1) * page_size).limit(page_size).all()
    return {
        "total": total,
        "page": page,
        "page_size": page_size,
        "items": [_to_dict(r) for r in items],
    }


def list_all_requests(
    db: Session,
    status: str | None = None,
    page: int = 1,
    page_size: int = 20,
) -> dict:
    """管理员：全部求片列表（分页，可按状态过滤）"""
    q = db.query(models.MediaRequest)
    if status:
        q = q.filter(models.MediaRequest.status == status)
    q = q.order_by(models.MediaRequest.id.desc())
    total = q.count()
    items = q.offset((page - 1) * page_size).limit(page_size).all()

    user_ids = {r.user_id for r in items}
    users = {
        u.id: u.username for u in db.query(models.WebUser).filter(
            models.WebUser.id.in_(user_ids)).all()
    } if user_ids else {}

    return {
        "total": total,
        "page": page,
        "page_size": page_size,
        "items": [{**_to_dict(r), "username": users.get(r.user_id, "未知")} for r in items],
    }


def review_request(
    db: Session,
    request_id: int,
    approve: bool,
    admin_note: str = "",
) -> models.MediaRequest:
    """管理员审核：通过 / 拒绝"""
    req = db.query(models.MediaRequest).filter(
        models.MediaRequest.id == request_id).first()
    if req is None:
        raise ValueError("求片记录不存在")
    if req.status != "pending":
        raise ValueError(f"只能审核 pending 状态的请求（当前：{req.status}）")

    req.status = "approved" if approve else "rejected"
    req.admin_note = admin_note or ""
    db.commit()
    db.refresh(req)
    logger.info("求片审核: id=%s approve=%s", request_id, approve)
    return req


def mark_done(db: Session, request_id: int) -> models.MediaRequest:
    """管理员标记已入库"""
    req = db.query(models.MediaRequest).filter(
        models.MediaRequest.id == request_id).first()
    if req is None:
        raise ValueError("求片记录不存在")
    if req.status != "approved":
        raise ValueError(f"只能标记 approved 的请求为 done（当前：{req.status}）")
    req.status = "done"
    db.commit()
    db.refresh(req)
    return req


def _to_dict(r: models.MediaRequest) -> dict:
    return {
        "id": r.id,
        "user_id": r.user_id,
        "tmdb_id": r.tmdb_id,
        "media_type": r.media_type,
        "title": r.title,
        "status": r.status,
        "admin_note": r.admin_note,
        "created_at": r.created_at.isoformat() if r.created_at else None,
        "updated_at": r.updated_at.isoformat() if r.updated_at else None,
    }
