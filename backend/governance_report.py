"""等级公益服活动治理 Step 2：举报＋复核投票＋管理员终审队列（后端服务层）。

设计稿：~/workspace/reports/等级公益服活动治理设计-20261029.md（Step 2、Q4/Q5/Q7/Q8/Q9/Q10）。
本单只做 Step 2：举报（v1 仅求片条目）＋复核投票（quorum＋阈值升级终审）
＋管理员终审队列（受理／驳回）＋恶意举报停权（自动＋手动冻结）。

硬线（违反即打回）：
- 仅 free realm 生效（用 subscriptions.is_free_realm 同一信号，按举报目标的服判定，
  目标无服时按当前服判定）；付费服下创建／复核一律 403。
- 举报／复核动作不产生经验；经验绝不换积分／活力。
- 不卡核心权益：举报与复核不影响任何人的播放／查看／基础求片额度；
  也不碰、不绕过想看商店会员锁（去留悬置）。
- 终审受理不自动隐藏／删除求片（v1 必须人工终审，自动处理误伤不可逆）。

与设计稿的出入（有意为之，PR body 有说明）：
- 复核不同意达 quorum＋阈值时，举报行以 review_rejected 关闭（不进终审），
  避免无结论报告永久挂起；设计稿只定义了同意侧升级， disagreements 侧未定义。
- 终审结果通过列表／详情接口对举报人与复核员可见；v1 不另建推送通道
  （全仓无可复用的举报通知发送器，media_seek 仅有通知开关、无发送实现）。
- 可调参数走 SystemConfig 键（缺省／脏值回设计稿推荐值）；管理端字段暴露
  留给 Step 4 的管理端工具单，本单不在 admin UI 加字段。

状态机：pending（待复核）→ escalated（已升级终审队列）
       → accepted（终审受理）/ rejected（终审驳回）；
       pending 时复核不同意达标 → review_rejected（复核关闭）。
"""

from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import case, func
from sqlalchemy.orm import Session

from backend import governance, models
from backend.subscriptions import is_free_realm

REPORT_TARGET_MEDIA_SEEK = "media_seek"
REPORT_REASONS = ("spam", "duplicate", "violation")
REPORT_STATUS_PENDING = "pending"
REPORT_STATUS_ESCALATED = "escalated"
REPORT_STATUS_ACCEPTED = "accepted"
REPORT_STATUS_REJECTED = "rejected"
REPORT_STATUS_REVIEW_REJECTED = "review_rejected"
TERMINAL_STATUSES = (
    REPORT_STATUS_ACCEPTED,
    REPORT_STATUS_REJECTED,
    REPORT_STATUS_REVIEW_REJECTED,
)

# 可调参数（SystemConfig 键，缺省／脏值回默认值＝设计稿推荐值）。
CONFIG_ENABLED = "governance_report_enabled"
CONFIG_MIN_REVIEW_LEVEL = "governance_review_min_level"
CONFIG_QUORUM = "governance_review_quorum"
CONFIG_AGREE_NUM = "governance_review_agree_num"
CONFIG_AGREE_DEN = "governance_review_agree_den"
CONFIG_ABUSE_WINDOW_DAYS = "governance_abuse_window_days"
CONFIG_ABUSE_REJECT_COUNT = "governance_abuse_reject_count"
CONFIG_ABUSE_SUSPEND_DAYS = "governance_abuse_suspend_days"

DEFAULT_ENABLED = True
DEFAULT_MIN_REVIEW_LEVEL = 3
DEFAULT_QUORUM = 3
DEFAULT_AGREE_NUM = 2
DEFAULT_AGREE_DEN = 3
DEFAULT_ABUSE_WINDOW_DAYS = 30
DEFAULT_ABUSE_REJECT_COUNT = 5
DEFAULT_ABUSE_SUSPEND_DAYS = 30

# 复核投票权重（按复核员等级；Lv.1–Lv.2 无复核权；Lv.6 封顶 3）。
REVIEW_WEIGHTS = {3: 1, 4: 2, 5: 2, 6: 3}


class GovernanceError(Exception):
    """服务层错误：路由层直接转 HTTPException(status_code, detail)。"""

    def __init__(self, status_code: int, detail: str):
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


def _get_bool(db: Session, key: str, default: bool = True) -> bool:
    """读布尔配置：'1'/'true' 为开，'0'/'false' 为关；缺省/脏值回 default。"""
    row = (
        db.query(models.SystemConfig)
        .filter(models.SystemConfig.key == key)
        .first()
    )
    raw = str(row.value).strip().lower() if row and row.value is not None else ""
    if raw in ("1", "true", "yes", "on"):
        return True
    if raw in ("0", "false", "no", "off"):
        return False
    return default


def _get_int(db: Session, key: str, default: int) -> int:
    """读整数配置；缺省/脏值回 default。"""
    row = (
        db.query(models.SystemConfig)
        .filter(models.SystemConfig.key == key)
        .first()
    )
    if row is None or row.value is None:
        return default
    try:
        return int(str(row.value).strip())
    except (TypeError, ValueError):
        return default


def _get_ratio(db: Session) -> tuple[int, int]:
    """同意阈值（分子，分母）：非法配置回默认值 2/3。"""
    num = _get_int(db, CONFIG_AGREE_NUM, DEFAULT_AGREE_NUM)
    den = _get_int(db, CONFIG_AGREE_DEN, DEFAULT_AGREE_DEN)
    if den <= 0 or num < 0 or num > den:
        return DEFAULT_AGREE_NUM, DEFAULT_AGREE_DEN
    return num, den


def review_weight(level: int | None) -> int:
    """复核投票权重：Lv.3→1、Lv.4→2、Lv.5→2、Lv.6＋→3；Lv.2 及以下 0（无复核权）。"""
    level = level or 1
    if level >= 6:
        return 3
    if level >= 4:
        return 2
    if level >= 3:
        return 1
    return 0


def report_enabled(db: Session) -> bool:
    """举报总开关：关闭后举报入口与复核投票 403，终审队列数据保留可读。"""
    return _get_bool(db, CONFIG_ENABLED, DEFAULT_ENABLED)


def _require_enabled(db: Session) -> None:
    if not report_enabled(db):
        raise GovernanceError(403, "举报功能已关闭")


def _require_free_realm(db: Session, realm_id: int | None) -> None:
    if not is_free_realm(db, realm_id):
        raise GovernanceError(403, "治理举报仅公益服开放")


def active_suspension(
    db: Session, user_id: int, now: datetime | None = None
) -> models.GovernanceSuspension | None:
    """该用户当前生效中的治理停权（无则 None）。"""
    now = now or datetime.now()
    return (
        db.query(models.GovernanceSuspension)
        .filter(
            models.GovernanceSuspension.user_id == user_id,
            models.GovernanceSuspension.revoked_at.is_(None),
            models.GovernanceSuspension.ends_at > now,
        )
        .order_by(models.GovernanceSuspension.id.desc())
        .first()
    )


def _require_not_suspended(db: Session, user_id: int) -> None:
    if active_suspension(db, user_id) is not None:
        raise GovernanceError(403, "治理权限已被暂停")


def _require_not_frozen(db: Session, user_id: int) -> None:
    """Step 4 管理员停权（GovernanceFreeze）检查：与 Step 2 的自动暂停
    （GovernanceSuspension，恶意举报达标触发）是两套独立机制，互不替代。"""
    if governance.is_governance_frozen(db, user_id):
        raise GovernanceError(403, "治理特权已被冻结")


def _vote_stats(db: Session, report_ids: list[int]) -> dict[int, dict]:
    """批量取复核统计：{report_id: {ballots, agree_weight, disagree_weight}}。"""
    stats: dict[int, dict] = {
        rid: {"ballots": 0, "agree_weight": 0, "disagree_weight": 0}
        for rid in report_ids
    }
    if not report_ids:
        return stats
    rows = (
        db.query(
            models.GovernanceReviewVote.report_id,
            func.count(models.GovernanceReviewVote.id),
            func.coalesce(
                func.sum(
                    case(
                        (
                            models.GovernanceReviewVote.decision == "agree",
                            models.GovernanceReviewVote.weight,
                        ),
                        else_=0,
                    )
                ),
                0,
            ),
            func.coalesce(
                func.sum(
                    case(
                        (
                            models.GovernanceReviewVote.decision == "disagree",
                            models.GovernanceReviewVote.weight,
                        ),
                        else_=0,
                    )
                ),
                0,
            ),
        )
        .filter(models.GovernanceReviewVote.report_id.in_(report_ids))
        .group_by(models.GovernanceReviewVote.report_id)
        .all()
    )
    for report_id, ballots, agree_weight, disagree_weight in rows:
        stats[report_id] = {
            "ballots": int(ballots),
            "agree_weight": int(agree_weight),
            "disagree_weight": int(disagree_weight),
        }
    return stats


def _report_dict(
    row: models.GovernanceReport, stats: dict | None = None
) -> dict:
    out = {
        "id": row.id,
        "reporter_id": row.reporter_id,
        "target_type": row.target_type,
        "target_id": row.target_id,
        "reason": row.reason,
        "detail": row.detail,
        "status": row.status,
        "admin_note": row.admin_note,
        "admin_id": row.admin_id,
        "escalated_at": row.escalated_at,
        "decided_at": row.decided_at,
        "created_at": row.created_at,
        "updated_at": row.updated_at,
    }
    if stats is not None:
        out.update(stats)
    return out


def _target_realm_id(db: Session, report: models.GovernanceReport) -> int | None:
    """举报目标的服：求片行的 realm_id（老行无服时 None，走当前服判定）。"""
    if report.target_type != REPORT_TARGET_MEDIA_SEEK:
        return None
    target = (
        db.query(models.MovieRequest)
        .filter(models.MovieRequest.id == report.target_id)
        .first()
    )
    return target.realm_id if target is not None else None


def create_report(
    db: Session, user, target_id: int, reason: str, detail: str | None = None
) -> dict:
    """举报一条求片。返回举报行 dict。"""
    _require_enabled(db)
    target = (
        db.query(models.MovieRequest)
        .filter(models.MovieRequest.id == target_id)
        .first()
    )
    if target is None:
        raise GovernanceError(404, "求片不存在")
    _require_free_realm(db, target.realm_id)
    _require_not_suspended(db, user.id)
    _require_not_frozen(db, user.id)
    if reason not in REPORT_REASONS:
        raise GovernanceError(400, "举报原因无效")
    existing = (
        db.query(models.GovernanceReport)
        .filter(
            models.GovernanceReport.reporter_id == user.id,
            models.GovernanceReport.target_type == REPORT_TARGET_MEDIA_SEEK,
            models.GovernanceReport.target_id == target_id,
        )
        .first()
    )
    if existing is not None:
        raise GovernanceError(409, "已举报过该求片")
    row = models.GovernanceReport(
        reporter_id=user.id,
        target_type=REPORT_TARGET_MEDIA_SEEK,
        target_id=target_id,
        reason=reason,
        detail=(detail or "").strip() or None,
        status=REPORT_STATUS_PENDING,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return _report_dict(row, _vote_stats(db, [row.id])[row.id])


def list_my_reports(db: Session, user, limit: int = 100) -> list[dict]:
    """举报人自己的举报列表（最新在前，含复核统计）。"""
    limit = min(max(int(limit), 1), 200)
    rows = (
        db.query(models.GovernanceReport)
        .filter(models.GovernanceReport.reporter_id == user.id)
        .order_by(models.GovernanceReport.id.desc())
        .limit(limit)
        .all()
    )
    stats = _vote_stats(db, [r.id for r in rows])
    return [_report_dict(r, stats[r.id]) for r in rows]


def get_report_detail(
    db: Session, viewer, report_id: int, is_admin: bool = False
) -> dict:
    """举报详情：举报人看统计，管理员看全部复核票；无关用户 404（不泄露举报人）。"""
    row = (
        db.query(models.GovernanceReport)
        .filter(models.GovernanceReport.id == report_id)
        .first()
    )
    if row is None:
        raise GovernanceError(404, "举报不存在")
    if not is_admin and row.reporter_id != viewer.id:
        raise GovernanceError(404, "举报不存在")
    out = _report_dict(row, _vote_stats(db, [row.id])[row.id])
    if is_admin:
        votes = (
            db.query(models.GovernanceReviewVote)
            .filter(models.GovernanceReviewVote.report_id == row.id)
            .order_by(models.GovernanceReviewVote.id.asc())
            .all()
        )
        out["votes"] = [
            {
                "id": v.id,
                "reviewer_id": v.reviewer_id,
                "decision": v.decision,
                "weight": v.weight,
                "created_at": v.created_at,
            }
            for v in votes
        ]
    return out


def cast_review_vote(
    db: Session, user, report_id: int, decision: str
) -> dict:
    """复核投票。quorum 按人数（防单人高权重凑 quorum），同意阈值按权重算。

    达标后：同意侧达标 → escalated（进终审队列）；不同意侧达标 →
    review_rejected（复核关闭，不进终审）。权重为投票时刻等级快照。
    """
    _require_enabled(db)
    report = (
        db.query(models.GovernanceReport)
        .filter(models.GovernanceReport.id == report_id)
        .with_for_update()
        .first()
    )
    if report is None:
        raise GovernanceError(404, "举报不存在")
    if report.status != REPORT_STATUS_PENDING:
        raise GovernanceError(409, "该举报已不在复核中")
    _require_free_realm(db, _target_realm_id(db, report))
    _require_not_suspended(db, user.id)
    _require_not_frozen(db, user.id)
    if decision not in ("agree", "disagree"):
        raise GovernanceError(400, "复核决定无效")
    weight = review_weight(user.member_level)
    if weight <= 0:
        raise GovernanceError(403, "复核需要 Lv.3 及以上")
    if report.reporter_id == user.id:
        raise GovernanceError(403, "不能复核自己举报的")
    existing = (
        db.query(models.GovernanceReviewVote)
        .filter(
            models.GovernanceReviewVote.report_id == report_id,
            models.GovernanceReviewVote.reviewer_id == user.id,
        )
        .first()
    )
    if existing is not None:
        raise GovernanceError(409, "已复核过该举报")
    db.add(
        models.GovernanceReviewVote(
            report_id=report_id,
            reviewer_id=user.id,
            decision=decision,
            weight=weight,
        )
    )
    db.flush()
    stats = _vote_stats(db, [report_id])[report_id]
    quorum = max(1, _get_int(db, CONFIG_QUORUM, DEFAULT_QUORUM))
    num, den = _get_ratio(db)
    total = stats["agree_weight"] + stats["disagree_weight"]
    now = datetime.now()
    if stats["ballots"] >= quorum and total > 0:
        if stats["agree_weight"] * den >= num * total:
            report.status = REPORT_STATUS_ESCALATED
            report.escalated_at = now
        elif stats["disagree_weight"] * den >= num * total:
            report.status = REPORT_STATUS_REVIEW_REJECTED
    db.commit()
    db.refresh(report)
    return _report_dict(report, stats)


def admin_list_reports(
    db: Session, status: str | None = None, limit: int = 100
) -> list[dict]:
    """管理端举报列表：默认只列终审队列（escalated，最早在前）；传 status 可筛。"""
    limit = min(max(int(limit), 1), 200)
    query = db.query(models.GovernanceReport)
    if status:
        query = query.filter(models.GovernanceReport.status == status)
    else:
        query = query.filter(
            models.GovernanceReport.status == REPORT_STATUS_ESCALATED
        )
    rows = (
        query.order_by(models.GovernanceReport.id.asc())
        .limit(limit)
        .all()
    )
    stats = _vote_stats(db, [r.id for r in rows])
    return [_report_dict(r, stats[r.id]) for r in rows]


def _maybe_auto_suspend(db: Session, user_id: int, now: datetime) -> dict | None:
    """终审驳回计数触发自动停权：窗口内被驳回达标且无生效中停权时，新建 30 天停权。"""
    if active_suspension(db, user_id, now) is not None:
        return None
    window = _get_int(db, CONFIG_ABUSE_WINDOW_DAYS, DEFAULT_ABUSE_WINDOW_DAYS)
    needed = _get_int(db, CONFIG_ABUSE_REJECT_COUNT, DEFAULT_ABUSE_REJECT_COUNT)
    days = _get_int(db, CONFIG_ABUSE_SUSPEND_DAYS, DEFAULT_ABUSE_SUSPEND_DAYS)
    if window <= 0 or needed <= 0 or days <= 0:
        return None
    since = now - timedelta(days=window)
    count = (
        db.query(func.count(models.GovernanceReport.id))
        .filter(
            models.GovernanceReport.reporter_id == user_id,
            models.GovernanceReport.status == REPORT_STATUS_REJECTED,
            models.GovernanceReport.decided_at >= since,
        )
        .scalar()
    )
    if count is None or count < needed:
        return None
    row = models.GovernanceSuspension(
        user_id=user_id,
        reason="auto_abuse",
        note=f"{window} 天内终审驳回 {count} 次，自动暂停治理权限 {days} 天",
        created_by_id=None,
        starts_at=now,
        ends_at=now + timedelta(days=days),
    )
    db.add(row)
    db.commit()
    return _suspension_dict(row)


def admin_decide(
    db: Session, admin, report_id: int, decision: str, note: str | None = None
) -> dict:
    """管理员终审：仅终审队列（escalated）可判。受理不自动处理求片本身；
    驳回计入举报人的终审驳回计数，可能触发自动停权。"""
    report = (
        db.query(models.GovernanceReport)
        .filter(models.GovernanceReport.id == report_id)
        .with_for_update()
        .first()
    )
    if report is None:
        raise GovernanceError(404, "举报不存在")
    if report.status != REPORT_STATUS_ESCALATED:
        raise GovernanceError(409, "该举报不在终审队列")
    if decision not in (REPORT_STATUS_ACCEPTED, REPORT_STATUS_REJECTED):
        raise GovernanceError(400, "终审决定无效")
    now = datetime.now()
    report.status = decision
    report.admin_id = admin.id
    report.admin_note = (note or "").strip() or None
    report.decided_at = now
    db.commit()
    db.refresh(report)
    out = _report_dict(report, _vote_stats(db, [report.id])[report.id])
    suspension = None
    if decision == REPORT_STATUS_REJECTED:
        suspension = _maybe_auto_suspend(db, report.reporter_id, now)
    if suspension is not None:
        out["auto_suspension"] = suspension
    return out


def _suspension_dict(row: models.GovernanceSuspension) -> dict:
    return {
        "id": row.id,
        "user_id": row.user_id,
        "reason": row.reason,
        "note": row.note,
        "created_by_id": row.created_by_id,
        "starts_at": row.starts_at,
        "ends_at": row.ends_at,
        "revoked_at": row.revoked_at,
        "revoked_by_id": row.revoked_by_id,
        "created_at": row.created_at,
    }


def admin_list_suspensions(
    db: Session,
    user_id: int | None = None,
    active_only: bool = False,
    limit: int = 100,
) -> list[dict]:
    """管理端停权列表（最新在前；active_only 只列生效中的）。"""
    limit = min(max(int(limit), 1), 200)
    query = db.query(models.GovernanceSuspension)
    if user_id is not None:
        query = query.filter(models.GovernanceSuspension.user_id == user_id)
    if active_only:
        now = datetime.now()
        query = query.filter(
            models.GovernanceSuspension.revoked_at.is_(None),
            models.GovernanceSuspension.ends_at > now,
        )
    rows = (
        query.order_by(models.GovernanceSuspension.id.desc())
        .limit(limit)
        .all()
    )
    return [_suspension_dict(r) for r in rows]


def admin_suspend(
    db: Session, admin, user_id: int, days: int = 30, note: str | None = None
) -> dict:
    """管理员手动停权：冻结治理特权（举报／复核），不扣经验、不降级。"""
    target = (
        db.query(models.WebUser).filter(models.WebUser.id == user_id).first()
    )
    if target is None:
        raise GovernanceError(404, "用户不存在")
    try:
        days = int(days)
    except (TypeError, ValueError):
        raise GovernanceError(400, "停权天数无效") from None
    if days < 1:
        raise GovernanceError(400, "停权天数无效")
    if active_suspension(db, user_id) is not None:
        raise GovernanceError(409, "该用户已有生效中的停权")
    now = datetime.now()
    row = models.GovernanceSuspension(
        user_id=user_id,
        reason="manual",
        note=(note or "").strip() or None,
        created_by_id=admin.id,
        starts_at=now,
        ends_at=now + timedelta(days=days),
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return _suspension_dict(row)


def admin_unsuspend(db: Session, admin, suspension_id: int) -> dict:
    """管理员撤销停权（仅生效中的可撤销）。"""
    row = (
        db.query(models.GovernanceSuspension)
        .filter(models.GovernanceSuspension.id == suspension_id)
        .first()
    )
    if row is None:
        raise GovernanceError(404, "停权不存在")
    if row.revoked_at is not None or row.ends_at <= datetime.now():
        raise GovernanceError(409, "停权已不在生效中")
    row.revoked_at = datetime.now()
    row.revoked_by_id = admin.id
    db.commit()
    db.refresh(row)
    return _suspension_dict(row)


__all__ = [
    "GovernanceError",
    "REPORT_REASONS",
    "REPORT_STATUS_ACCEPTED",
    "REPORT_STATUS_ESCALATED",
    "REPORT_STATUS_PENDING",
    "REPORT_STATUS_REJECTED",
    "REPORT_STATUS_REVIEW_REJECTED",
    "active_suspension",
    "admin_decide",
    "admin_list_reports",
    "admin_list_suspensions",
    "admin_suspend",
    "admin_unsuspend",
    "cast_review_vote",
    "create_report",
    "get_report_detail",
    "list_my_reports",
    "report_enabled",
    "review_weight",
]
