# -*- coding: utf-8 -*-
"""等级治理 Step 3：活动提案与直接发起（公益服，free realm 专属）。

设计稿：~/workspace/reports/等级公益服活动治理设计-20261029.md（Step 3 一节 + Q6 分级表 + Q10 生效范围）。

分级（设计稿 Q6，Lv.1 无权）：
- Lv.2+：活动提案权（提案 -> 管理员批准 -> 建轮次 -> 正常开奖流程）。
- Lv.4+：每月活动提案数 2（Lv.2/Lv.3 为 1）。
- Lv.5+：直接发起活动（月限 Lv.5=1、Lv.6=2，事后审计）。
- 提案与直接发起均有审计记录：提案行 / 直接发起行本身就是审计链
  （谁、何时、什么活动、谁审批、结果）；管理员批准/拒绝另走 _audit 写 AdminLog。

硬线（设计稿 + 任务书）：
- 提案/发起/批准/拒绝全程不产生经验（本模块不调任何 add_xp）。
- 奖品仍走现有抽奖口径（days/points/whitelist），不新设货币。
- 仅 free realm 生效；想看商店会员锁不碰、不绕过。
- 不动 lottery.py 的开奖/发放路径、不改管理端现有创建端点：
  批准与直接发起都复用 lottery.create_round 建轮次，建好后走现有开奖机制
  （管理员手动开奖 / 自动开奖调度），本模块不另起开奖流程。

月限口径：
- 自然月：以 created_at 的服务器本地日历月为准（created_at 由 datetime.now()
  生成，同一时钟口径，月边界不做时区换算，配额场景可接受）。
- 提案计数：本月新建提案行全部计入（pending/approved/rejected 都算），
  防 spam 刷提案；直接发起计数：仅成功发起的记录行计入。
- 月限数值走 SystemConfig 键（缺键/非法值回落设计默认值），管理端字段与
  env.example 文档是后续跟进项（见 PR body），本单先做到键可读、可调：
  lottery_governance_proposal_cap_base（默认 1，Lv.2/Lv.3 提案月限）、
  lottery_governance_proposal_cap_lv4（默认 2，Lv.4+ 提案月限）、
  lottery_governance_direct_cap_lv5（默认 1）、
  lottery_governance_direct_cap_lv6（默认 2）。

审计与 AdminLog 的边界：
- 用户动作（提案/直接发起）不走 AdminLog：AdminLog.admin_user_id 外键指向
  admin_users.id，普通用户 id 写进去会在 PG 上触发外键违规；提案行与
  直接发起行已完整记录谁/何时/什么活动，即为审计。
- 管理员动作（批准/拒绝）走 admin_core._audit（与现有管理端创建端点同口径，
  审批人是 is_staff 的 WebUser，与现有 lottery_round_create 的调用方一致）。
"""

import json
import logging
from datetime import datetime
from typing import Any

from sqlalchemy import func
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from backend import governance, lottery, member_level, models
from backend.api.admin_core import _audit
# 注意：必须用 subscriptions.is_free_realm（无 realm_id 时解析为面板当前服）；
# realms.is_free_realm(db, None) 会因查不到服而一律按 paid 处理，公益服下会误拦。
from backend.subscriptions import is_free_realm

logger = logging.getLogger(__name__)

# ---- 分级门槛（设计稿 Q6） ----
MIN_PROPOSE_LEVEL = 2
MIN_DIRECT_LEVEL = 5
PROPOSAL_CAP_LV4_LEVEL = 4  # Lv.4+ 提案月限走 lv4 档

# ---- SystemConfig 月限键与设计默认值 ----
KEY_PROPOSAL_CAP_BASE = "lottery_governance_proposal_cap_base"
KEY_PROPOSAL_CAP_LV4 = "lottery_governance_proposal_cap_lv4"
KEY_DIRECT_CAP_LV5 = "lottery_governance_direct_cap_lv5"
KEY_DIRECT_CAP_LV6 = "lottery_governance_direct_cap_lv6"
DEFAULT_PROPOSAL_CAP_BASE = 1
DEFAULT_PROPOSAL_CAP_LV4 = 2
DEFAULT_DIRECT_CAP_LV5 = 1
DEFAULT_DIRECT_CAP_LV6 = 2


class LotteryGovernanceError(Exception):
    """Step 3 服务层错误：端点层直接映射为同状态码的 HTTPException。"""

    def __init__(self, status_code: int, detail: str):
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


def _config_int(db: Session, key: str, default: int) -> int:
    """读 SystemConfig 整数键；缺键/非法值/非正值一律回落默认值。"""
    row = db.query(models.SystemConfig).filter(models.SystemConfig.key == key).first()
    if row is None:
        return default
    try:
        value = int(str(row.value).strip())
    except (TypeError, ValueError):
        return default
    return value if value > 0 else default


def user_level(db: Session, user_id: int) -> int:
    """按 member_xp 纯计算等级（不写库、不触发种子/迁移副作用）。

    阈值取 member_levels 表的 active 行；表空时回落 member_level.DEFAULT_LEVELS。
    注意：不调用 member_level.get_member_info（它有种子写入副作用），
    也不信任 web_users.member_level 缓存列（可能滞后）。
    """
    user = db.query(models.WebUser).filter(models.WebUser.id == user_id).first()
    if user is None:
        return 1
    xp = user.member_xp or 0
    levels = (
        db.query(models.MemberLevel)
        .filter(models.MemberLevel.is_active.is_(True))
        .order_by(models.MemberLevel.level.asc())
        .all()
    )
    thresholds = (
        [(lv.level, lv.xp_threshold or 0) for lv in levels]
        if levels
        else [(lv["level"], lv["xp_threshold"]) for lv in member_level.DEFAULT_LEVELS]
    )
    level = 1
    for lv, threshold in thresholds:
        if xp >= threshold:
            level = lv
    return level


def _month_window(now: datetime) -> tuple[datetime, datetime]:
    """服务器本地自然月窗口 [月初, 下月初)。"""
    start = datetime(now.year, now.month, 1)
    if now.month == 12:
        end = datetime(now.year + 1, 1, 1)
    else:
        end = datetime(now.year, now.month + 1, 1)
    return start, end


def proposal_count_this_month(db: Session, user_id: int, now: datetime) -> int:
    start, end = _month_window(now)
    return (
        db.query(func.count(models.LotteryActivityProposal.id))
        .filter(
            models.LotteryActivityProposal.user_id == user_id,
            models.LotteryActivityProposal.created_at >= start,
            models.LotteryActivityProposal.created_at < end,
        )
        .scalar()
        or 0
    )


def direct_count_this_month(db: Session, user_id: int, now: datetime) -> int:
    start, end = _month_window(now)
    return (
        db.query(func.count(models.LotteryDirectLaunch.id))
        .filter(
            models.LotteryDirectLaunch.user_id == user_id,
            models.LotteryDirectLaunch.created_at >= start,
            models.LotteryDirectLaunch.created_at < end,
        )
        .scalar()
        or 0
    )


def proposal_cap_for_level(db: Session, level: int) -> int:
    if level >= PROPOSAL_CAP_LV4_LEVEL:
        return _config_int(db, KEY_PROPOSAL_CAP_LV4, DEFAULT_PROPOSAL_CAP_LV4)
    return _config_int(db, KEY_PROPOSAL_CAP_BASE, DEFAULT_PROPOSAL_CAP_BASE)


def direct_cap_for_level(db: Session, level: int) -> int:
    if level >= 6:
        return _config_int(db, KEY_DIRECT_CAP_LV6, DEFAULT_DIRECT_CAP_LV6)
    return _config_int(db, KEY_DIRECT_CAP_LV5, DEFAULT_DIRECT_CAP_LV5)


def _require_free_realm(db: Session) -> None:
    if not is_free_realm(db):
        raise LotteryGovernanceError(403, "活动提案与直接发起仅在公益服开放")


def _require_lottery_enabled(db: Session) -> None:
    if not lottery.is_enabled(db):
        raise LotteryGovernanceError(403, "群抽奖功能未开启")


def validate_round_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """校验轮次载荷，镜像管理端 create_lottery_round 的校验（不改管理端端点）。

    镜像来源：backend/api/welfare_admin.py::create_lottery_round。
    报错文案与管理端逐字一致，测试钉死；管理端校验若改，这里须同步改。
    返回规范化后的载荷：title / chat_id / prizes / draw_at（上海 naive datetime 或 None）
    / max_participants（None -> 0）/ lottery_type / password_keyword。
    """
    title = payload.get("title")
    if not title or not str(title).strip():
        raise LotteryGovernanceError(400, "活动标题不能为空")
    prizes = payload.get("prizes")
    if not prizes:
        raise LotteryGovernanceError(400, "至少需要配置 1 个奖品")
    norm_prizes = []
    for p in prizes:
        ptype = p.get("type") if isinstance(p, dict) else None
        if ptype not in ("days", "points", "whitelist"):
            raise LotteryGovernanceError(400, "奖品类型只能是 days/points/whitelist")
        try:
            value = int(p.get("value"))
        except (TypeError, ValueError):
            raise LotteryGovernanceError(400, "奖品价值必须是整数")
        if ptype in ("days", "points") and value <= 0:
            raise LotteryGovernanceError(
                400, f"{'能量' if ptype == 'points' else '天数'}奖品的价值必须大于 0"
            )
        if ptype == "whitelist" and value < 0:
            raise LotteryGovernanceError(400, "奖品价值不能为负数")
        try:
            quantity = int(p.get("quantity"))
        except (TypeError, ValueError):
            raise LotteryGovernanceError(400, "奖品数量必须是整数")
        if quantity < 1:
            raise LotteryGovernanceError(400, "奖品数量至少为 1")
        norm_prizes.append(
            {
                "name": p.get("name") or "",
                "type": ptype,
                "value": value,
                "quantity": quantity,
            }
        )
    lottery_type = payload.get("lottery_type") or "button"
    if lottery_type not in ("button", "password"):
        raise LotteryGovernanceError(400, "抽奖类型只能是 button 或 password")
    password_keyword = payload.get("password_keyword")
    if lottery_type == "password" and not password_keyword:
        raise LotteryGovernanceError(400, "口令抽奖需要设置口令")
    max_participants = payload.get("max_participants")
    if max_participants is not None:
        try:
            max_participants = int(max_participants)
        except (TypeError, ValueError):
            raise LotteryGovernanceError(400, "最大参与人数必须是整数")
        if max_participants < 1:
            raise LotteryGovernanceError(400, "最大参与人数至少为 1")
    draw_at_raw = payload.get("draw_at")
    draw_at = None
    if draw_at_raw:
        if isinstance(draw_at_raw, datetime):
            # 已是 naive datetime（批准路径重校验已入库的提案值）：直接用，
            # 绝不能再过 to_shanghai_naive，否则上海 naive 会被当成 UTC 再 +8 小时。
            draw_at = draw_at_raw
        else:
            try:
                draw_at = lottery.to_shanghai_naive(datetime.fromisoformat(str(draw_at_raw)))
            except ValueError:
                raise LotteryGovernanceError(400, "draw_at 时间格式无效")
    return {
        "title": str(title),
        "chat_id": payload.get("chat_id"),
        "prizes": norm_prizes,
        "draw_at": draw_at,
        "max_participants": max_participants,
        "lottery_type": lottery_type,
        "password_keyword": password_keyword,
    }


def resolve_chat_id(db: Session, chat_id: Any) -> int:
    """解析生效群：镜像管理端规则——给定则用给定值；为空时仅当允许群恰好一个才自动取。

    返回 int：lottery_rounds.chat_id 是 BigInteger，allowed_group_ids 是 set[int]，
    全链路以整数比较（字符串进 PG 会类型报错）。
    """
    if chat_id is not None and str(chat_id).strip() != "":
        try:
            return int(chat_id)
        except (TypeError, ValueError):
            raise LotteryGovernanceError(400, "chat_id 必须是整数")
    groups = lottery.allowed_group_ids(db)
    if len(groups) == 1:
        return next(iter(groups))
    raise LotteryGovernanceError(400, "请先在功能开关里把允许群ID设置为一个群，再来新建活动")


def _check_group_allowed(db: Session, chat_id: int) -> None:
    groups = lottery.allowed_group_ids(db)
    if groups and chat_id not in groups:
        raise LotteryGovernanceError(403, "该群未被允许")


def _check_no_active_round(db: Session, chat_id: int) -> None:
    if lottery.get_active_round(db, chat_id) is not None:
        raise LotteryGovernanceError(400, "该群已有进行中的抽奖活动")


def submit_proposal(db: Session, user_id: int, payload: dict[str, Any]) -> models.LotteryActivityProposal:
    """提交活动提案：只写提案行，不建轮次，不产生经验。

    门禁顺序：free realm -> 抽奖开关 -> 停权检查 -> 等级 >= Lv.2 -> 本月提案上限 ->
    载荷校验 -> 群解析 -> 群白名单。提交时不查同群进行中活动，冲突留到批准时查
    （批准时群里可能已有新活动在跑，提交时查了也拦不住）。
    """
    _require_free_realm(db)
    _require_lottery_enabled(db)
    if governance.is_governance_frozen(db, user_id):
        raise LotteryGovernanceError(403, "治理特权已被冻结")
    level = user_level(db, user_id)
    if level < MIN_PROPOSE_LEVEL:
        raise LotteryGovernanceError(403, "等级不足：活动提案需要 Lv.2 及以上")
    cap = proposal_cap_for_level(db, level)
    if proposal_count_this_month(db, user_id, datetime.now()) >= cap:
        raise LotteryGovernanceError(400, f"本月活动提案已达上限（Lv.{level} 每月 {cap} 次）")
    norm = validate_round_payload(payload)
    chat_id = resolve_chat_id(db, norm["chat_id"])
    _check_group_allowed(db, chat_id)
    proposal = models.LotteryActivityProposal(
        user_id=user_id,
        title=norm["title"],
        chat_id=chat_id,
        lottery_type=norm["lottery_type"],
        password_keyword=norm["password_keyword"],
        prizes_json=json.dumps(norm["prizes"], ensure_ascii=False),
        draw_at=norm["draw_at"],
        max_participants=norm["max_participants"] or 0,
        level_snapshot=level,
        status="pending",
    )
    db.add(proposal)
    db.commit()
    db.refresh(proposal)
    return proposal


def approve_proposal(
    db: Session, admin_id: int, proposal_id: int, note: str | None = None
) -> tuple[models.LotteryActivityProposal, models.LotteryRound]:
    """批准提案：建轮次（created_by=提案人），提案置 approved 并关联轮次。

    批准时重校验载荷（提案内容是提交时校验过的快照，重校验防脏数据入轮次）、
    重查群白名单与同群进行中活动；建轮次失败（部分唯一索引竞态）回滚并报 400。
    批准不重查提案人等级：审批决定以提案内容为准，不随提案人后续升级/降级回改。
    建轮次后走现有开奖机制（管理员手动开奖 / 自动开奖调度），本模块不另起开奖。
    """
    proposal = (
        db.query(models.LotteryActivityProposal)
        .filter(models.LotteryActivityProposal.id == proposal_id)
        .first()
    )
    if proposal is None:
        raise LotteryGovernanceError(404, "提案不存在")
    if proposal.status != "pending":
        raise LotteryGovernanceError(400, "提案已处理")
    _require_free_realm(db)
    _require_lottery_enabled(db)
    _check_group_allowed(db, proposal.chat_id)
    _check_no_active_round(db, proposal.chat_id)
    try:
        prizes = json.loads(proposal.prizes_json or "[]")
    except (TypeError, ValueError):
        raise LotteryGovernanceError(400, "提案奖品数据损坏")
    norm = validate_round_payload(
        {
            "title": proposal.title,
            "prizes": prizes,
            "lottery_type": proposal.lottery_type,
            "password_keyword": proposal.password_keyword,
            # 传 datetime 对象（validate 内直接取用，不再做时区换算）：
            # 若传 iso 字符串会被当成 UTC 再转上海，draw_at 漂移 +8 小时。
            "draw_at": proposal.draw_at,
            "max_participants": proposal.max_participants or None,
        }
    )
    try:
        round_obj = lottery.create_round(
            db,
            title=norm["title"],
            chat_id=proposal.chat_id,
            prizes=norm["prizes"],
            draw_at=norm["draw_at"],
            max_participants=norm["max_participants"] or 0,
            created_by=proposal.user_id,
            lottery_type=norm["lottery_type"],
            password_keyword=norm["password_keyword"],
        )
    except IntegrityError:
        db.rollback()
        raise LotteryGovernanceError(400, "该群已有进行中的抽奖活动")
    proposal.status = "approved"
    proposal.reviewer_id = admin_id
    proposal.reviewed_at = datetime.now()
    proposal.review_note = note
    proposal.round_id = round_obj.id
    _audit(
        db,
        admin_id,
        "lottery_proposal_approve",
        "lottery_proposal",
        proposal.id,
        {"round_id": round_obj.id, "proposer_id": proposal.user_id},
    )
    db.commit()
    db.refresh(proposal)
    db.refresh(round_obj)
    return proposal, round_obj


def reject_proposal(
    db: Session, admin_id: int, proposal_id: int, note: str | None = None
) -> models.LotteryActivityProposal:
    """拒绝提案：只改提案行，不产生任何东西。

    拒绝不要求 free realm：它不产生轮次、不发奖，是安全清理动作；
    服切回付费后仍可清理积压提案。
    """
    proposal = (
        db.query(models.LotteryActivityProposal)
        .filter(models.LotteryActivityProposal.id == proposal_id)
        .first()
    )
    if proposal is None:
        raise LotteryGovernanceError(404, "提案不存在")
    if proposal.status != "pending":
        raise LotteryGovernanceError(400, "提案已处理")
    proposal.status = "rejected"
    proposal.reviewer_id = admin_id
    proposal.reviewed_at = datetime.now()
    proposal.review_note = note
    _audit(
        db,
        admin_id,
        "lottery_proposal_reject",
        "lottery_proposal",
        proposal.id,
        {"proposer_id": proposal.user_id},
    )
    db.commit()
    db.refresh(proposal)
    return proposal


def direct_launch(
    db: Session, user_id: int, payload: dict[str, Any]
) -> tuple[models.LotteryRound, models.LotteryDirectLaunch]:
    """直接发起活动：Lv.5+ 不经审批建轮次，并写直接发起记录行（事后审计）。

    门禁顺序：free realm -> 抽奖开关 -> 停权检查 -> 等级 >= Lv.5 -> 本月直接发起上限 ->
    载荷校验 -> 群解析 -> 群白名单 -> 同群进行中活动 -> 建轮次。
    滥用时管理员用现有取消端点取消轮次（事后审计有据可查）。
    """
    _require_free_realm(db)
    _require_lottery_enabled(db)
    if governance.is_governance_frozen(db, user_id):
        raise LotteryGovernanceError(403, "治理特权已被冻结")
    level = user_level(db, user_id)
    if level < MIN_DIRECT_LEVEL:
        raise LotteryGovernanceError(403, "等级不足：直接发起活动需要 Lv.5 及以上")
    cap = direct_cap_for_level(db, level)
    if direct_count_this_month(db, user_id, datetime.now()) >= cap:
        raise LotteryGovernanceError(400, f"本月直接发起已达上限（Lv.{level} 每月 {cap} 次）")
    norm = validate_round_payload(payload)
    chat_id = resolve_chat_id(db, norm["chat_id"])
    _check_group_allowed(db, chat_id)
    _check_no_active_round(db, chat_id)
    try:
        round_obj = lottery.create_round(
            db,
            title=norm["title"],
            chat_id=chat_id,
            prizes=norm["prizes"],
            draw_at=norm["draw_at"],
            max_participants=norm["max_participants"] or 0,
            created_by=user_id,
            lottery_type=norm["lottery_type"],
            password_keyword=norm["password_keyword"],
        )
    except IntegrityError:
        db.rollback()
        raise LotteryGovernanceError(400, "该群已有进行中的抽奖活动")
    record = models.LotteryDirectLaunch(
        user_id=user_id,
        round_id=round_obj.id,
        level_snapshot=level,
    )
    db.add(record)
    db.commit()
    db.refresh(round_obj)
    db.refresh(record)
    return round_obj, record


def proposal_to_dict(p: models.LotteryActivityProposal) -> dict[str, Any]:
    try:
        prizes = json.loads(p.prizes_json or "[]")
    except (TypeError, ValueError):
        prizes = []
    return {
        "id": p.id,
        "user_id": p.user_id,
        "title": p.title,
        "chat_id": p.chat_id,
        "lottery_type": p.lottery_type,
        "password_keyword": p.password_keyword,
        "prizes": prizes,
        "draw_at": p.draw_at.isoformat() if p.draw_at else None,
        "max_participants": p.max_participants,
        "level_snapshot": p.level_snapshot,
        "status": p.status,
        "reviewer_id": p.reviewer_id,
        "reviewed_at": p.reviewed_at.isoformat() if p.reviewed_at else None,
        "review_note": p.review_note,
        "round_id": p.round_id,
        "created_at": p.created_at.isoformat() if p.created_at else None,
    }


def list_proposals(
    db: Session,
    user_id: int | None = None,
    status: str | None = None,
    page: int = 1,
    page_size: int = 20,
) -> tuple[int, list[dict[str, Any]]]:
    """分页列提案：user_id 给定则只看自己的（用户端），不给定则全量（管理端）。"""
    q = db.query(models.LotteryActivityProposal)
    if user_id is not None:
        q = q.filter(models.LotteryActivityProposal.user_id == user_id)
    if status:
        q = q.filter(models.LotteryActivityProposal.status == status)
    total = q.count()
    rows = (
        q.order_by(models.LotteryActivityProposal.id.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
        .all()
    )
    return total, [proposal_to_dict(r) for r in rows]
