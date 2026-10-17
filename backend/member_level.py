# -*- coding: utf-8 -*-
"""会员经验核心模块：等级种子、经验自增、等级重算与用户端会员信息。"""

import json
import logging

from sqlalchemy import case, func
from sqlalchemy.orm import Session

from backend import models

logger = logging.getLogger(__name__)

# 默认 6 个等级的种子数据（v2：暗房影院主题命名）
#
# 命名理念：影迷在暗房影院的进阶之旅——
#   初幕（幕布初启）→ 影迷（爱上电影）→ 鉴赏家（懂得光影）
#   → 放映师（掌管暗房）→ 造梦者（编织梦境）→ 传奇（影殿不朽）
DEFAULT_LEVELS = [
    {
        "level": 1,
        "name": "初幕",
        "xp_threshold": 0,
        "badge_icon": "Ticket",
        "badge_color": "#9ca3af",
        "benefits": ["基础观影权益", "每日签到"],
    },
    {
        "level": 2,
        "name": "影迷",
        "xp_threshold": 100,
        "badge_icon": "Clapperboard",
        "badge_color": "#f59e0b",
        "benefits": ["影迷专属徽章", "邀请奖励加成"],
    },
    {
        "level": 3,
        "name": "鉴赏家",
        "xp_threshold": 500,
        "badge_icon": "Glasses",
        "badge_color": "#7dd3fc",
        "benefits": ["鉴赏家专属徽章", "优先客服"],
    },
    {
        "level": 4,
        "name": "放映师",
        "xp_threshold": 1500,
        "badge_icon": "Projector",
        "badge_color": "#d97706",
        "benefits": ["放映师专属徽章", "专属客服通道"],
    },
    {
        "level": 5,
        "name": "造梦者",
        "xp_threshold": 5000,
        "badge_icon": "Sparkles",
        "badge_color": "#a78bfa",
        "benefits": ["造梦者专属徽章", "新片优先通知"],
    },
    {
        "level": 6,
        "name": "传奇",
        "xp_threshold": 15000,
        "badge_icon": "Crown",
        "badge_color": "#eab308",
        "benefits": ["传奇专属徽章", "专属活动邀请"],
    },
]

# v1 旧命名 → v2 新命名的迁移映射（仅用于升级仍在使用旧默认名的数据库行；
# 管理员已手动改过名的行不会被触碰）
LEGACY_NAME_MIGRATION = {
    "普通会员": "初幕",
    "铜牌会员": "影迷",
    "白银会员": "鉴赏家",
    "黄金会员": "放映师",
    "铂金会员": "造梦者",
    "钻石会员": "传奇",
}

# v1 旧徽章 → v2 新徽章（与名称迁移配套）
LEGACY_BADGE_MIGRATION = {
    1: ("Ticket", "#9ca3af"),
    2: ("Clapperboard", "#f59e0b"),
    3: ("Glasses", "#7dd3fc"),
    4: ("Projector", "#d97706"),
    5: ("Sparkles", "#a78bfa"),
    6: ("Crown", "#eab308"),
}


def _parse_benefits(benefits_json):
    """容错解析 benefits_json，失败时返回空列表。"""
    if not benefits_json:
        return []
    try:
        data = json.loads(benefits_json)
    except (ValueError, TypeError) as exc:
        logger.warning("benefits_json 解析失败: %s", exc)
        return []
    return data if isinstance(data, list) else []


def migrate_legacy_level_names(db: Session) -> int:
    """v1 → v2 命名迁移：把仍在使用旧默认名的等级行升级为暗房影院主题命名。

    只更新 name 仍在 LEGACY_NAME_MIGRATION 键中的行（即管理员未手动改过名的）；
    徽章图标/颜色同步更新为新主题。返回更新的行数。幂等：跑多次结果一致。

    性能：先用轻量查询判断是否存在旧名，无则直接返回 0，避免每次全表加载。
    """
    legacy_names = list(LEGACY_NAME_MIGRATION.keys())
    has_legacy = (
        db.query(models.MemberLevel.id)
        .filter(models.MemberLevel.name.in_(legacy_names))
        .first()
        is not None
    )
    if not has_legacy:
        return 0

    updated = 0
    rows = (
        db.query(models.MemberLevel)
        .filter(models.MemberLevel.name.in_(legacy_names))
        .all()
    )
    for row in rows:
        new_name = LEGACY_NAME_MIGRATION.get(row.name)
        if not new_name:
            continue
        row.name = new_name
        icon, color = LEGACY_BADGE_MIGRATION.get(row.level, (row.badge_icon, row.badge_color))
        row.badge_icon = icon
        row.badge_color = color
        # 权益文案中的旧名同步替换（如"铜牌专属徽章"→"影迷专属徽章"）
        benefits = _parse_benefits(row.benefits_json)
        new_benefits = []
        for b in benefits:
            for old, new in LEGACY_NAME_MIGRATION.items():
                old_short = old.replace("会员", "")
                if old_short and old_short in b:
                    b = b.replace(old_short, new)
            new_benefits.append(b)
        row.benefits_json = json.dumps(new_benefits, ensure_ascii=False)
        updated += 1
    if updated:
        try:
            db.commit()
        except Exception:
            db.rollback()
            raise
        logger.info("会员等级命名迁移完成，共更新 %d 行", updated)
    return updated


# 会员等级订阅折扣默认值（种子写入 + 首次回填用，之后以数据库值为准，后台可改）
DISCOUNT_DEFAULTS = {1: 0, 2: 2, 3: 5, 4: 8, 5: 12, 6: 15}


def ensure_member_levels_seeded(db: Session) -> int:
    """幂等种子：member_levels 表为空时插入 DEFAULT_LEVELS，返回插入数量。"""
    count = db.query(models.MemberLevel).count()
    if count > 0:
        return 0
    for item in DEFAULT_LEVELS:
        db.add(
            models.MemberLevel(
                level=item["level"],
                name=item["name"],
                xp_threshold=item["xp_threshold"],
                discount_pct=DISCOUNT_DEFAULTS.get(item["level"], 0),
                badge_icon=item["badge_icon"],
                badge_color=item["badge_color"],
                benefits_json=json.dumps(item["benefits"], ensure_ascii=False),
                is_active=True,
            )
        )
    try:
        db.commit()
    except Exception:
        db.rollback()
        # 并发场景下可能已被其他进程播种
        if db.query(models.MemberLevel).count() > 0:
            return 0
        raise
    logger.info("会员等级种子写入完成，共 %d 条", len(DEFAULT_LEVELS))
    return len(DEFAULT_LEVELS)


def _recompute_level(db: Session, user: models.WebUser) -> int:
    """根据当前 member_xp 计算应属等级，仅在变化时用 SQL 级 update 写回。

    不提交事务：调用方负责 commit（履约等场景要求「更新与发货在同一事务里」）。
    """
    # 从库里重读真实 xp：调用方可能刚做了 SQL 级自增，ORM 对象未必已同步
    xp = int(
        db.query(models.WebUser.member_xp)
        .filter(models.WebUser.id == user.id)
        .scalar()
        or 0
    )
    levels = (
        db.query(models.MemberLevel)
        .filter(models.MemberLevel.is_active == True)  # noqa: E712
        .order_by(models.MemberLevel.xp_threshold.asc())
        .all()
    )
    # 取阈值 <= xp 的最大 level，没有则返回 1
    candidates = [lv.level for lv in levels if (lv.xp_threshold or 0) <= xp]
    target = max(candidates) if candidates else 1
    current = user.member_level or 1
    if current != target:
        db.query(models.WebUser).filter(models.WebUser.id == user.id).update(
            {models.WebUser.member_level: target},
            synchronize_session="fetch",
        )
        logger.info("用户 %s 等级变更: %s -> %s", user.id, current, target)
    return target


def add_xp(
    db: Session,
    user: models.WebUser,
    delta: int,
    source: str,
    ref_id: str | None = None,
) -> dict:
    """累加会员经验并写流水、重算等级。

    约定与 ``_add_points`` 一致：**不提交事务**，只做 SQL 级自增 + 添流水对象，
    由调用方统一 commit（履约场景要求积分/经验/订单状态在同一事务里）。

    ``delta`` 可正可负：正数是发放（如充值履约），负数是回滚（如订单退款
    撤回履约时发的经验）。负数时经验钳制在 0（不扣成负数），等级按阈值
    重算——``_recompute_level`` 取阈值 <= xp 的最大等级，降级天然支持。
    """
    if delta == 0:
        return {"added": False}

    old_level = user.member_level or 1

    if delta > 0:
        new_xp_expr = func.coalesce(models.WebUser.member_xp, 0) + delta
    else:
        # 回滚：经验不能扣成负数。greatest/max 在 SQLite/PG 下不通用，
        # 用 CASE WHEN 保证两边都跑得通。
        _after = func.coalesce(models.WebUser.member_xp, 0) + delta
        new_xp_expr = case((_after < 0, 0), else_=_after)

    # SQL 级增减 member_xp，防并发丢更新
    db.query(models.WebUser).filter(models.WebUser.id == user.id).update(
        {models.WebUser.member_xp: new_xp_expr},
        synchronize_session="fetch",
    )

    # 查回真实 xp_after（读自己事务内的写入）
    xp_after = int(
        db.query(models.WebUser.member_xp)
        .filter(models.WebUser.id == user.id)
        .scalar()
        or 0
    )

    # 写经验流水
    db.add(
        models.MemberXpLog(
            user_id=user.id,
            xp_delta=delta,
            xp_after=xp_after,
            source=source,
            ref_id=ref_id,
        )
    )

    # 重算等级并检测是否升级/降级
    level = _recompute_level(db, user)
    leveled_up = level > old_level
    if leveled_up:
        logger.info("用户 %s 升级: %s -> %s", user.id, old_level, level)
    elif level < old_level:
        logger.info("用户 %s 降级: %s -> %s", user.id, old_level, level)

    return {
        "added": True,
        "xp_after": xp_after,
        "level": level,
        "leveled_up": leveled_up,
        "leveled_down": level < old_level,
        "old_level": old_level,
    }


_MIGRATION_FLAG = "member_discount_pct_migrated"
def migrate_discount_pct(db: Session) -> int:
    """幂等回填：把存量等级行的 discount_pct 按等级回填默认值。

    只在首次运行时回填（用 SystemConfig 标记），避免覆盖管理员后续手动调整。
    首次运行回填所有 discount_pct 为 NULL 或 0 的行；之后不再碰已有值。
    返回回填数量。
    """
    flag = db.query(models.SystemConfig).filter(
        models.SystemConfig.key == _MIGRATION_FLAG).first()
    if flag is not None:
        return 0
    rows = (
        db.query(models.MemberLevel)
        .filter(
            (models.MemberLevel.discount_pct.is_(None))
            | (models.MemberLevel.discount_pct == 0)
        )
        .all()
    )
    count = 0
    for lv in rows:
        default = DISCOUNT_DEFAULTS.get(lv.level, 0)
        if (lv.discount_pct or 0) != default:
            lv.discount_pct = default
            count += 1
    db.add(models.SystemConfig(key=_MIGRATION_FLAG, value="1",
                               description="会员等级折扣回填已执行"))
    db.commit()
    if count:
        logger.info("会员等级折扣回填完成，共 %d 条", count)
    return count


def get_user_discount_pct(db: Session, user: models.WebUser) -> int:
    """返回用户当前等级的订阅折扣百分比（0-100）。查不到按 0 处理。"""
    level = user.member_level or 1
    lv = (
        db.query(models.MemberLevel)
        .filter(
            models.MemberLevel.level == level,
            models.MemberLevel.is_active == True,  # noqa: E712
        )
        .first()
    )
    if lv is None or lv.discount_pct is None:
        return 0
    return max(0, min(100, int(lv.discount_pct)))


def get_member_info(db: Session, user: models.WebUser) -> dict:
    """用户端 /api/user/member 使用：等级列表 + 当前等级 + 进度。"""
    # 确保种子存在，并把旧命名迁移到新主题（幂等），再回填存量等级的折扣
    ensure_member_levels_seeded(db)
    migrate_legacy_level_names(db)
    migrate_discount_pct(db)

    rows = (
        db.query(models.MemberLevel)
        .filter(models.MemberLevel.is_active == True)  # noqa: E712
        .order_by(models.MemberLevel.level.asc())
        .all()
    )
    levels = []
    for lv in rows:
        levels.append(
            {
                "level": lv.level,
                "name": lv.name,
                "xp_threshold": lv.xp_threshold or 0,
                "discount_pct": lv.discount_pct or 0,
                "benefits": _parse_benefits(lv.benefits_json),
                "badge_icon": lv.badge_icon,
                "badge_color": lv.badge_color,
            }
        )

    xp = user.member_xp or 0
    level = user.member_level or 1

    # 当前等级信息
    current = next((item for item in levels if item["level"] == level), None)
    if current is None:
        # 兜底：取不超过当前经验的最高等级，否则用第一级
        fallback = [item for item in levels if item["xp_threshold"] <= xp]
        current = fallback[-1] if fallback else (levels[0] if levels else None)
        level = current["level"] if current else 1

    level_name = current["name"] if current else "初幕"
    badge_icon = current["badge_icon"] if current else "User"
    badge_color = current["badge_color"] if current else "#9ca3af"
    cur_threshold = current["xp_threshold"] if current else 0

    # 下一级阈值与进度
    higher = [item for item in levels if item["level"] > level]
    if higher:
        nxt = higher[0]
        next_level = nxt["level"]
        next_threshold = nxt["xp_threshold"]
        xp_to_next = max(0, next_threshold - xp)
        span = next_threshold - cur_threshold
        if span <= 0:
            progress_pct = 100.0
        else:
            progress_pct = min(
                100.0, max(0.0, (xp - cur_threshold) / span * 100.0)
            )
    else:
        # 满级
        next_level = None
        next_threshold = None
        xp_to_next = None
        progress_pct = 100.0

    return {
        "level": level,
        "level_name": level_name,
        "xp": xp,
        "badge_icon": badge_icon,
        "badge_color": badge_color,
        "discount_pct": get_user_discount_pct(db, user),
        "next_level": next_level,
        "next_threshold": next_threshold,
        "xp_to_next": xp_to_next,
        "progress_pct": round(progress_pct, 2),
        "levels": levels,
    }

