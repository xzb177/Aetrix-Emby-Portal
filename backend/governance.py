"""等级公益服活动治理 Step 4：治理审计日志与停权工具（领域逻辑，无 HTTP）。

约定与 ``member_level.add_xp`` 一致：**不提交事务**，只做写入／更新，
由调用方统一 commit。

硬线（设计稿）：
- 审计日志只追加、不改不删；Lv.6 与管理员只读可见。
- 停权只冻结治理特权（权重／提案／复核／举报），保留基础附议 1 票与
  等级展示，不影响播放／查看／基础求片额度，不扣经验。
- 降级仅管理员手动：走 ``add_xp`` 负数（source=governance_penalty），
  理由必填，全程审计。
- 治理动作不产生经验；经验绝不换积分／活力。
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta

from sqlalchemy.orm import Session

from backend import models
from backend.member_level import add_xp

logger = logging.getLogger(__name__)

# 治理审计动作名：Step 4 自有动作；Step 1–3 的动作名由各单定义后写这里。
ACTION_FREEZE = "freeze"
ACTION_RELEASE = "release"
ACTION_DOWNGRADE = "downgrade"


def record_governance_audit(
    db: Session,
    *,
    action: str,
    actor_user_id: int | None,
    target_user_id: int | None = None,
    target_ref: str | None = None,
    detail: dict | None = None,
) -> models.GovernanceAuditLog:
    """追加一条治理审计记录（只 insert，不 commit、不 update、不 delete）。"""
    row = models.GovernanceAuditLog(
        action=action,
        actor_user_id=actor_user_id,
        target_user_id=target_user_id,
        target_ref=target_ref,
        detail=json.dumps(detail or {}, ensure_ascii=False),
    )
    db.add(row)
    db.flush()
    return row


def active_freeze(db: Session, user_id: int) -> models.GovernanceFreeze | None:
    """该用户当前生效中的停权行（未解封且未过期），无则返回 None。"""
    now = datetime.now()
    rows = (
        db.query(models.GovernanceFreeze)
        .filter(
            models.GovernanceFreeze.user_id == user_id,
            models.GovernanceFreeze.released_at.is_(None),
        )
        .order_by(models.GovernanceFreeze.id.desc())
        .all()
    )
    for row in rows:
        if row.expires_at is None or row.expires_at > now:
            return row
    return None


def is_governance_frozen(db: Session, user_id: int) -> bool:
    """该用户治理特权是否被冻结（供 Step 1–3 的治理入口调用）。"""
    return active_freeze(db, user_id) is not None


def freeze_governance(
    db: Session,
    *,
    user_id: int,
    reason: str,
    days: int | None,
    admin_id: int,
) -> models.GovernanceFreeze:
    """冻结指定用户的治理特权。理由必填；已有生效中停权时拒绝（先解封再重冻）。"""
    if not (reason or "").strip():
        raise ValueError("停权理由必填")
    if days is not None and days <= 0:
        raise ValueError("停权天数须为正数")
    if active_freeze(db, user_id) is not None:
        raise ValueError("该用户治理特权已被冻结")
    row = models.GovernanceFreeze(
        user_id=user_id,
        reason=reason.strip(),
        expires_at=datetime.now() + timedelta(days=days) if days else None,
        created_by=admin_id,
    )
    db.add(row)
    db.flush()
    record_governance_audit(
        db,
        action=ACTION_FREEZE,
        actor_user_id=admin_id,
        target_user_id=user_id,
        detail={"reason": row.reason, "days": days,
                "expires_at": row.expires_at.isoformat() if row.expires_at else None},
    )
    logger.info("治理停权：用户 %s 被管理员 %s 冻结（%s）", user_id, admin_id, row.reason)
    return row


def release_governance_freeze(
    db: Session,
    *,
    user_id: int,
    admin_id: int,
) -> models.GovernanceFreeze:
    """解封指定用户的治理停权；无生效中停权时拒绝。"""
    row = active_freeze(db, user_id)
    if row is None:
        raise ValueError("该用户没有生效中的治理停权")
    row.released_at = datetime.now()
    row.released_by = admin_id
    db.flush()
    record_governance_audit(
        db,
        action=ACTION_RELEASE,
        actor_user_id=admin_id,
        target_user_id=user_id,
        detail={"freeze_id": row.id, "reason": row.reason},
    )
    logger.info("治理解封：用户 %s 被管理员 %s 解封", user_id, admin_id)
    return row


def governance_downgrade(
    db: Session,
    *,
    user_id: int,
    xp: int,
    reason: str,
    admin_id: int,
) -> dict:
    """管理员手动降级：扣经验（钳制在 0）＋重算等级＋审计。理由必填。

    只动经验与等级，不动积分／活力／其它权益。
    """
    if xp <= 0:
        raise ValueError("扣减经验须为正数")
    if not (reason or "").strip():
        raise ValueError("降级理由必填")
    user = db.query(models.WebUser).filter(models.WebUser.id == user_id).first()
    if user is None:
        raise ValueError("用户不存在")
    level_before = user.member_level or 1
    xp_before = user.member_xp or 0
    add_xp(db, user, -xp, source="governance_penalty",
           ref_id=f"governance:{admin_id}")
    db.flush()
    xp_after = int(
        db.query(models.WebUser.member_xp).filter(models.WebUser.id == user_id).scalar() or 0
    )
    level_after = int(
        db.query(models.WebUser.member_level).filter(models.WebUser.id == user_id).scalar() or 1
    )
    record_governance_audit(
        db,
        action=ACTION_DOWNGRADE,
        actor_user_id=admin_id,
        target_user_id=user_id,
        detail={"reason": reason.strip(), "xp_deducted": xp,
                "xp_before": xp_before, "xp_after": xp_after,
                "level_before": level_before, "level_after": level_after},
    )
    logger.info("治理降级：用户 %s 被管理员 %s 扣 %s 经验（Lv%s -> Lv%s）",
                user_id, admin_id, xp, level_before, level_after)
    return {"xp_before": xp_before, "xp_after": xp_after,
            "level_before": level_before, "level_after": level_after}


def list_audit_logs(
    db: Session,
    *,
    limit: int = 50,
    action: str | None = None,
    actor_user_id: int | None = None,
    target_user_id: int | None = None,
) -> list[models.GovernanceAuditLog]:
    """按时间倒序读治理审计日志（只读查询，不写）。"""
    q = db.query(models.GovernanceAuditLog)
    if action:
        q = q.filter(models.GovernanceAuditLog.action == action)
    if actor_user_id is not None:
        q = q.filter(models.GovernanceAuditLog.actor_user_id == actor_user_id)
    if target_user_id is not None:
        q = q.filter(models.GovernanceAuditLog.target_user_id == target_user_id)
    return (
        q.order_by(models.GovernanceAuditLog.id.desc())
        .limit(max(1, min(limit, 200)))
        .all()
    )
